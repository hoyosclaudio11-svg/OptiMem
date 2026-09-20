"""
Deteccion de procesos criticos.

Dos trabajos distintos, y conviene no confundirlos:

1. PROTECCION (evitar que OptiMem cause un cierre o un freeze). Antes de tocar
   cualquier proceso se pregunta aca. Si no esta clasificado como "libre", no
   se toca. Esto incluye el proceso en foco: trimear la ventana en la que estas
   escribiendo es exactamente el freeze que este proyecto dice evitar.

2. VIGILANCIA (detectar cierres que no provocamos nosotros). Los procesos de
   `vigilar` se siguen ciclo a ciclo; si desaparecen, queda un evento. Un
   proceso que muere a los pocos segundos de arrancar se marca como grave,
   porque eso es un ciclo de crash, no un cierre normal.

El modulo NUNCA mata nada. No existe una funcion para terminar procesos, a
proposito.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import psutil

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel.OpenProcess.restype = wt.HANDLE
_kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel.CloseHandle.argtypes = [wt.HANDLE]
_kernel.QueryFullProcessImageNameW.restype = wt.BOOL
_kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                               ctypes.POINTER(wt.DWORD)]

_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Categorias, de mas protegida a menos.
CAT_NUCLEO = "nucleo"           # Windows. Inaccesible igual sin admin.
CAT_PROTEGIDO = "protegido"     # lista del usuario: su ecosistema
CAT_FOCO = "foco"               # ventana activa: la estas usando AHORA
CAT_VISIBLE = "visible"         # app interactiva con ventana
CAT_VIGILADO = "vigilado"       # se sigue su vida
CAT_LIBRE = "libre"             # candidato a que el agente lo toque

# Nombres que jamas se tocan aunque el usuario los saque de la config.
NUCLEO_DURO = {
    "system", "system idle process", "registry", "memory compression",
    "memcompression", "smss", "csrss", "wininit", "winlogon", "services",
    "lsass", "dwm", "fontdrvhost", "sihost", "ctfmon", "audiodg",
    "secure system", "msmpeng", "nissrv", "securityhealthservice",
    "sgrmbroker", "wudfhost", "spoolsv", "searchindexer", "startmenuexperiencehost",
    "shellexperiencehost", "textinputhost", "applicationframehost",
}

# Nombres que casi siempre son el shell: si los tocamos, se nota.
SHELL = {"explorer", "explorer.exe"}


def _normalizar(nombre: str) -> str:
    n = (nombre or "").lower().strip()
    return n[:-4] if n.endswith(".exe") else n


def _identidad(cmdline, nombre: str) -> str:
    """
    Identidad de un programa para detectar ciclos de crash.

    Usa el ejecutable y el primer argumento con aspecto de ruta (el script).
    No usa la linea completa porque los argumentos cambian entre corridas
    legitimamente (una fecha, un id), y entonces cada corrida pareceria un
    programa distinto y nunca se detectaria el ciclo.

    Devuelve "" si no hay informacion: en ese caso no se afirma nada, que es
    mejor que afirmar de mas.
    """
    if not cmdline:
        return ""
    partes = [str(x) for x in cmdline]
    if not partes:
        return ""
    piezas = [partes[0]]
    for arg in partes[1:]:
        # El primer argumento que parezca un archivo (.py, .js, ruta) define
        # el programa. Los demas suelen ser parametros variables.
        a = arg.strip('"')
        if a.lower().endswith((".py", ".pyw", ".js", ".mjs", ".cjs", ".ps1", ".bat", ".cmd")):
            piezas.append(a)
            break
        if not a.startswith("-") and ("\\" in a or "/" in a):
            piezas.append(a)
            break
    return " ".join(piezas).lower()


def pid_en_foco() -> int | None:
    """PID de la ventana activa. None si no se puede determinar."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def pids_con_ventana() -> set[int]:
    """
    PIDs con al menos una ventana de nivel superior visible.

    Un proceso con ventana es interactivo: si le vaciamos el working set, el
    usuario lo siente. Los que no tienen ventana son los buenos candidatos
    (sidecars, backends, indexadores).
    """
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return pids


def ruta_proceso(pid: int) -> str | None:
    h = _kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        tam = wt.DWORD(len(buf))
        if _kernel.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(tam)):
            return buf.value
        return None
    finally:
        _kernel.CloseHandle(h)


def usuario_proceso(pid: int) -> str | None:
    try:
        return psutil.Process(pid).username()
    except Exception:
        return None


@dataclass
class Clasificacion:
    pid: int
    nombre: str
    categoria: str
    motivo: str
    es_critico: bool

    @property
    def se_puede_tocar(self) -> bool:
        return self.categoria == CAT_LIBRE


@dataclass
class Muerte:
    clave: str
    pid: int
    nombre: str
    vivio_seg: float
    ts: float
    grave: bool
    detalle: str
    identidad: str = ""   # linea de comandos: distingue un script de otro


class DetectorCriticos:
    """
    Clasifica procesos y vigila los que no deben morir.

    Se instancia una vez y se le pide `clasificar()` en cada ciclo. Guarda
    estado interno (que PIDs tenia cada proceso protegido) para detectar
    desapariciones.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.protegidos = {_normalizar(n) for n in cfg.procesos_protegidos}
        self.vigilados = {_normalizar(n) for n in cfg.vigilar}
        self.mi_usuario = usuario_proceso(psutil.Process().pid)
        self.mi_pid = psutil.Process().pid
        # clave -> (pid, nombre, ts_arranque, arranque_del_proceso)
        self._vivos: dict[str, tuple[int, str, float, float]] = {}
        self._muertes: list[Muerte] = []
        self._pid_foco: int | None = None
        self._pids_ventana: set[int] = set()
        self._ts_foco = 0.0
        # Muertes tempranas por nombre, dentro de una ventana de una hora.
        # Sirve para distinguir "un proceso suelto termino" (normal, no se
        # reporta) de "esto se muere una y otra vez" (patron, si se reporta).
        self._muertes_tempranas: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self.min_muertes_para_avisar = 3
        self._muertes_sueltas = 0

    # -- refresco de lo que cambia rapido -----------------------------------
    def refrescar_ventanas(self) -> None:
        """Relee la ventana activa y las ventanas visibles. Barato pero no gratis."""
        self._pid_foco = pid_en_foco()
        self._pids_ventana = pids_con_ventana()
        self._ts_foco = time.time()

    # -- clasificacion ------------------------------------------------------
    def clasificar(self, pid: int, nombre: str, *, usuario: str | None = None,
                   crear_time: float | None = None) -> Clasificacion:
        n = _normalizar(nombre)

        if pid in (0, 4) or n in NUCLEO_DURO:
            return Clasificacion(pid, nombre, CAT_NUCLEO,
                                 "proceso del nucleo de Windows", True)

        # Otro usuario = otro contexto de seguridad. No lo tocamos ni aunque
        # tecnicamente pudieramos: no sabemos que esta haciendo.
        if usuario and self.mi_usuario and usuario != self.mi_usuario:
            return Clasificacion(pid, nombre, CAT_NUCLEO,
                                 "corre como otro usuario", True)

        if pid == self.mi_pid:
            return Clasificacion(pid, nombre, CAT_PROTEGIDO,
                                 "es el propio OptiMem", True)

        if n in self.protegidos:
            return Clasificacion(pid, nombre, CAT_PROTEGIDO,
                                 "esta en la lista de protegidos", True)

        if self._pid_foco and pid == self._pid_foco:
            return Clasificacion(pid, nombre, CAT_FOCO,
                                 "es la ventana activa: la estas usando ahora", True)

        if pid in self._pids_ventana:
            return Clasificacion(pid, nombre, CAT_VISIBLE,
                                 "tiene ventana abierta (app interactiva)", True)

        if n in self.vigilados:
            return Clasificacion(pid, nombre, CAT_VIGILADO,
                                 "se vigila su vida, pero se puede ajustar", False)

        return Clasificacion(pid, nombre, CAT_LIBRE, "proceso de fondo sin ventana", False)

    # -- vigilancia ---------------------------------------------------------
    def vigilar(self, procesos: list[dict],
                excluir_pids: set[int] | None = None) -> list[Muerte]:
        """
        Recibe los procesos vivos del ciclo y devuelve las muertes detectadas.

        OJO CON EL DISEÑO, porque es facil hacer ruido aca. Un proceso que
        nace, hace algo y muere es NORMAL: en este ecosistema hay tareas
        programadas y publicadores de un solo uso que son asi por diseño. Si
        gritaramos por cada uno, esto seria otro healthcheck que "reporta ERR
        siempre" y que nadie mira por ruidoso.

        Se probo primero contar por NOMBRE y 3 muertes cortas en una hora, y
        seguia dando falsos positivos: cuatro scripts distintos de 40 segundos
        no son un ciclo de crash, son martes. Lo que distingue un ciclo de
        verdad es que sea EL MISMO programa: por eso la identidad es la linea
        de comandos, no el nombre.

            cuatro one-offs distintos  -> normal, no se reporta
            el mismo script 3+ veces   -> ciclo de crash, se reporta
        """
        ahora = time.time()
        excluir = excluir_pids or set()
        vistos: dict[str, tuple[int, str, float, float, str]] = {}

        for p in procesos:
            n = _normalizar(p.get("nombre", ""))
            if n not in self.vigilados:
                continue
            if p["pid"] in excluir or p.get("es_propio"):
                continue
            arranque = p.get("create_time") or (ahora - 3600)
            ident = _identidad(p.get("cmdline"), p.get("nombre", ""))
            vistos[p["clave"]] = (p["pid"], p.get("nombre", ""), ahora, arranque, ident)

        recien_muertos: list[Muerte] = []
        for clave, (pid, nombre, ts_visto, arranque, ident) in self._vivos.items():
            if clave in vistos:
                continue
            vivio = ts_visto - arranque
            recien_muertos.append(Muerte(
                clave=clave, pid=pid, nombre=nombre, vivio_seg=vivio,
                ts=ahora, grave=False, identidad=ident,
                detalle=f"vivio {vivio:.0f} s" if vivio < 120 else f"vivio {vivio / 60:.1f} min",
            ))

        # Cuantas veces murio temprano ESTE programa en la ultima hora.
        # La clave es la linea de comandos, no el nombre: python.exe lo usan
        # todos tus scripts y contarlos juntos seria ruido puro.
        for m in recien_muertos:
            if m.vivio_seg < 60 and m.identidad:
                self._muertes_tempranas[m.identidad].append(ahora)

        for ident in list(self._muertes_tempranas):
            cola = self._muertes_tempranas[ident]
            while cola and ahora - cola[0] > 3600:
                cola.popleft()
            if not cola:
                del self._muertes_tempranas[ident]

        reportar: list[Muerte] = []
        for m in recien_muertos:
            if m.vivio_seg >= 60:
                continue   # vivio un rato: cierre normal
            if not m.identidad:
                # Sin linea de comandos no podemos distinguir un script de
                # otro, asi que no afirmamos nada.
                self._muertes_sueltas += 1
                continue
            cuantas = len(self._muertes_tempranas.get(m.identidad, ()))
            if cuantas >= self.min_muertes_para_avisar:
                reportar.append(Muerte(
                    clave=m.clave, pid=m.pid, nombre=m.nombre, vivio_seg=m.vivio_seg,
                    ts=m.ts, grave=True, identidad=m.identidad,
                    detalle=(f"el mismo programa murio {cuantas} veces en la "
                             f"ultima hora al poco de arrancar (esta: "
                             f"{m.vivio_seg:.0f} s). Ciclo de crash: {m.identidad[:90]}"),
                ))
            else:
                self._muertes_sueltas += 1

        self._vivos = vistos
        self._muertes.extend(reportar)
        return reportar

    def muertes_pendientes(self) -> list[Muerte]:
        m = self._muertes
        self._muertes = []
        return m

    # -- resumen para el panel ---------------------------------------------
    def resumen(self, clasificaciones: list[Clasificacion]) -> dict:
        conteo: dict[str, int] = {}
        for c in clasificaciones:
            conteo[c.categoria] = conteo.get(c.categoria, 0) + 1
        return {
            "por_categoria": conteo,
            "vigilados_vivos": len(self._vivos),
            "pid_en_foco": self._pid_foco,
            "ts_foco": self._ts_foco,
            # Muertes individuales que NO se reportaron por no ser patron.
            "muertes_sueltas": self._muertes_sueltas,
            "patrones_crash": {
                n: len(c) for n, c in self._muertes_tempranas.items()
                if len(c) >= self.min_muertes_para_avisar
            },
        }

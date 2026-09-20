"""
Capa de acceso a las APIs de memoria de Windows.

Todo lo que toca el sistema vive aca. El resto del proyecto no llama a ctypes
directamente, para que haya un solo lugar donde mirar cuando algo no funciona.

CONTEXTO IMPORTANTE (validado en esta maquina, ver README):
  - Sin admin se pueden abrir ~271 de 420 procesos con permiso de escritura.
    Eso alcanza para trimear y para cambiar la prioridad de memoria.
  - La purga de standby list y el file cache SI requieren admin
    (STATUS_PRIVILEGE_NOT_HELD = 0xC0000061). Se detectan y se apagan solas.
  - PageReadCount sale de una estructura NO documentada. Se autovalida contra
    psutil al arrancar; si no cuadra, se marca como no disponible en vez de
    reportar basura.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
from dataclasses import dataclass

# --- librerias ---
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

# ---------------------------------------------------------------------------
# Firmas de funciones.
#
# OJO: sin argtypes/restype, ctypes asume c_int de 32 bits y los handles de
# x64 se truncan. Ese fue un bug real en la validacion: GetCurrentProcess()
# devuelve el pseudo-handle -1 y sin restype=HANDLE la llamada siguiente
# fallaba con "OpenProcessToken fallo" sin explicacion util.
# ---------------------------------------------------------------------------
_kernel.GetCurrentProcess.restype = wt.HANDLE
_kernel.GetCurrentProcess.argtypes = []

_kernel.GetCurrentProcessId.restype = wt.DWORD
_kernel.GetCurrentProcessId.argtypes = []

_kernel.OpenProcess.restype = wt.HANDLE
_kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

_kernel.CloseHandle.restype = wt.BOOL
_kernel.CloseHandle.argtypes = [wt.HANDLE]

_kernel.SetProcessWorkingSetSize.restype = wt.BOOL
_kernel.SetProcessWorkingSetSize.argtypes = [wt.HANDLE, ctypes.c_size_t, ctypes.c_size_t]

_kernel.SetProcessInformation.restype = wt.BOOL
_kernel.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]

_kernel.GetProcessInformation.restype = wt.BOOL
_kernel.GetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]

_kernel.CreateFileW.restype = wt.HANDLE
_kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
_kernel.ReadFile.restype = wt.BOOL
_kernel.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

_psapi.GetProcessMemoryInfo.restype = wt.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
_psapi.GetPerformanceInfo.restype = wt.BOOL
_psapi.GetPerformanceInfo.argtypes = [ctypes.c_void_p, wt.DWORD]

_advapi.OpenProcessToken.restype = wt.BOOL
_advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
_advapi.LookupPrivilegeValueW.restype = wt.BOOL
_advapi.LookupPrivilegeValueW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p]
_advapi.AdjustTokenPrivileges.restype = wt.BOOL
_advapi.AdjustTokenPrivileges.argtypes = [wt.HANDLE, wt.BOOL, ctypes.c_void_p,
                                          wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]

# --- derechos de acceso a procesos ---
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# --- privilegios ---
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300

# --- clases de informacion de proceso ---
ProcessMemoryPriority = 0

# --- niveles de prioridad de memoria (a mas bajo, antes lo desaloja Windows) ---
MEMORY_PRIORITY = {
    "very_low": 1,
    "low": 2,
    "medium": 3,   # default real de Windows para procesos normales
    "below_normal": 4,
    "normal": 5,
}
MEMORY_PRIORITY_NOMBRE = {v: k for k, v in MEMORY_PRIORITY.items()}

# --- SystemMemoryListInformation (requiere admin) ---
SystemMemoryListInformation = 80
MemoryPurgeStandbyList = 4


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wt.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    """GetPerformanceInfo (psapi). Documentada y estable."""
    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """GetProcessMemoryInfo (psapi). Documentada y estable."""
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
    _fields_ = [("MemoryPriority", wt.ULONG)]


# ---------------------------------------------------------------------------
# Privilegios
# ---------------------------------------------------------------------------
def es_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _habilitar_privilegio(nombre: str) -> tuple[bool, str]:
    token = wt.HANDLE()
    if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(token)):
        return False, f"OpenProcessToken err={ctypes.get_last_error()}"
    try:
        luid = _LUID()
        if not _advapi.LookupPrivilegeValueW(None, nombre, ctypes.byref(luid)):
            return False, "privilegio inexistente"
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not _advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None):
            return False, f"AdjustTokenPrivileges err={ctypes.get_last_error()}"
        err = ctypes.get_last_error()
        if err == ERROR_NOT_ALL_ASSIGNED:
            return False, "requiere admin"
        return True, "habilitado"
    finally:
        _kernel.CloseHandle(token)


@dataclass
class Capacidades:
    """Que puede hacer OptiMem en esta sesion. Se detecta, no se asume."""
    admin: bool
    trim_proceso: bool          # SetProcessWorkingSetSize sobre procesos propios
    prioridad_memoria: bool     # SetProcessInformation(ProcessMemoryPriority)
    purga_standby: bool         # NtSetSystemInformation - SOLO admin
    file_cache: bool            # SetSystemFileCacheSize - SOLO admin
    lecturas_disco: bool        # PageReadCount de la estructura no documentada
    detalle: dict


def detectar_capacidades() -> Capacidades:
    """
    Sondea que palancas estan realmente disponibles.

    No asumimos por el resultado de es_admin(): probamos cada privilegio,
    porque un admin puede tener el privilegio deshabilitado por politica.
    """
    detalle: dict[str, str] = {}
    admin = es_admin()

    _, msg_perfil = _habilitar_privilegio("SeProfileSingleProcessPrivilege")
    _, msg_quota = _habilitar_privilegio("SeIncreaseQuotaPrivilege")
    detalle["SeProfileSingleProcessPrivilege"] = msg_perfil
    detalle["SeIncreaseQuotaPrivilege"] = msg_quota

    tiene_perfil = msg_perfil == "habilitado"
    tiene_quota = msg_quota == "habilitado"

    # Comprobamos el trim sobre nuestro propio proceso: siempre deberia andar.
    trim_ok = False
    h = _kernel.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False,
                            _pid_actual())
    if h:
        trim_ok = bool(_kernel.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value,
                                                        ctypes.c_size_t(-1).value))
        _kernel.CloseHandle(h)
    detalle["trim"] = "disponible" if trim_ok else "no disponible"

    # La prioridad de memoria la probamos igual, sobre nosotros mismos.
    prio_ok = _fijar_prioridad_raw(_pid_actual(), MEMORY_PRIORITY["medium"]) is True
    detalle["prioridad_memoria"] = "disponible" if prio_ok else "no disponible"

    # PageReadCount: pedimos la estructura y verificamos que AvailablePages cuadre.
    lecturas_ok = _validar_estructura_no_documentada()
    detalle["lecturas_disco"] = ("validada" if lecturas_ok
                                 else "estructura no coincide, se omite")

    return Capacidades(
        admin=admin,
        trim_proceso=trim_ok,
        prioridad_memoria=prio_ok,
        purga_standby=tiene_perfil,
        file_cache=tiene_quota,
        lecturas_disco=lecturas_ok,
        detalle=detalle,
    )


def _pid_actual() -> int:
    return _kernel.GetCurrentProcessId()


# ---------------------------------------------------------------------------
# Memoria del sistema
# ---------------------------------------------------------------------------
@dataclass
class MemoriaSistema:
    page_size: int
    total: int              # RAM fisica total, bytes
    disponible: int         # RAM disponible, bytes
    commit_total: int       # memoria comprometida, bytes
    commit_limite: int      # limite de commit (RAM + pagefile), bytes
    commit_pico: int
    cache_sistema: int      # standby + modified: la cache de disco
    pool_paginado: int
    pool_no_paginado: int
    procesos: int
    hilos: int

    # Contadores acumulados desde el arranque (van siempre para arriba).
    # Sirven por su DELTA entre muestras, no por su valor absoluto.
    fallos_pagina: int | None
    lecturas_pagina: int | None   # paginas traidas de DISCO = swapping duro
    io_lecturas_pagina: int | None

    @property
    def en_uso(self) -> int:
        return self.total - self.disponible

    @property
    def presion_commit(self) -> float:
        """0..1. Cuanto del limite de commit esta consumido. >0.9 es peligroso."""
        return self.commit_total / self.commit_limite if self.commit_limite else 0.0

    @property
    def presion_ram(self) -> float:
        """0..1. Cuanto de la RAM esta en uso."""
        return self.en_uso / self.total if self.total else 0.0


def memoria_sistema() -> MemoriaSistema | None:
    """Foto de la memoria global. None si las APIs fallan."""
    pi = _PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    pg = pi.PageSize or 4096

    fallos = lecturas = io_lecturas = None
    raw = _leer_spi()
    if raw and len(raw) >= 88:
        try:
            fallos = struct.unpack_from("<I", raw, 60)[0]
            lecturas = struct.unpack_from("<I", raw, 80)[0]
            io_lecturas = struct.unpack_from("<I", raw, 84)[0]
        except struct.error:
            pass

    return MemoriaSistema(
        page_size=pg,
        total=pi.PhysicalTotal * pg,
        disponible=pi.PhysicalAvailable * pg,
        commit_total=pi.CommitTotal * pg,
        commit_limite=pi.CommitLimit * pg,
        commit_pico=pi.CommitPeak * pg,
        cache_sistema=pi.SystemCache * pg,
        pool_paginado=pi.KernelPaged * pg,
        pool_no_paginado=pi.KernelNonpaged * pg,
        procesos=pi.ProcessCount,
        hilos=pi.ThreadCount,
        fallos_pagina=fallos,
        lecturas_pagina=lecturas,
        io_lecturas_pagina=io_lecturas,
    )


def _leer_spi() -> bytes | None:
    """NtQuerySystemInformation(SystemPerformanceInformation=2)."""
    try:
        buf = ctypes.create_string_buffer(4096)
        ret = ctypes.c_ulong(0)
        _ntdll.NtQuerySystemInformation(2, buf, ctypes.sizeof(buf), ctypes.byref(ret))
        return buf.raw[: ret.value]
    except Exception:
        return None


def _validar_estructura_no_documentada() -> bool:
    """
    SYSTEM_PERFORMANCE_INFORMATION no esta documentada y sus offsets cambian
    entre versiones. Antes de confiar en PageReadCount, verificamos que
    AvailablePages y CommittedPages coincidan con fuentes documentadas.

    Si no coinciden, es mejor no reportar el dato que reportar basura.
    """
    raw = _leer_spi()
    if not raw or len(raw) < 56:
        return False
    pi = _PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return False
    try:
        avail_pag = struct.unpack_from("<I", raw, 44)[0]
        commit_pag = struct.unpack_from("<I", raw, 48)[0]
    except struct.error:
        return False
    if pi.PhysicalAvailable == 0 or pi.CommitTotal == 0:
        return False
    # OJO CON LAS UNIDADES: los cuatro valores estan en PAGINAS.
    # Compararlos en bytes contra paginas da una diferencia gigante y un
    # falso negativo, que apaga una metrica que en realidad funciona.
    d1 = abs(avail_pag - pi.PhysicalAvailable) / pi.PhysicalAvailable
    d2 = abs(commit_pag - pi.CommitTotal) / pi.CommitTotal
    return d1 < 0.15 and d2 < 0.10


# ---------------------------------------------------------------------------
# Memoria por proceso
# ---------------------------------------------------------------------------
@dataclass
class MemoriaProceso:
    working_set: int
    working_set_pico: int
    fallos_pagina: int      # acumulado desde que arranco el proceso
    commit: int             # PagefileUsage: memoria virtual COMPROMETIDA.
                            # OJO: NO es "cuanto tiene swapeado". Queda igual
                            # despues de un trim. No usar como medida de swap.


def _abrir_proceso(pid: int, acceso: int):
    return _kernel.OpenProcess(acceso | PROCESS_QUERY_INFORMATION, False, pid)


def memoria_proceso(pid: int) -> MemoriaProceso | None:
    h = _abrir_proceso(pid, PROCESS_VM_READ)
    if not h:
        return None
    try:
        m = _PROCESS_MEMORY_COUNTERS()
        m.cb = ctypes.sizeof(m)
        if not _psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            return None
        return MemoriaProceso(
            working_set=m.WorkingSetSize,
            working_set_pico=m.PeakWorkingSetSize,
            fallos_pagina=m.PageFaultCount,
            commit=m.PagefileUsage,
        )
    finally:
        _kernel.CloseHandle(h)


def puede_escribir(pid: int) -> bool:
    """Podemos trimear / cambiar prioridad a este proceso?"""
    h = _abrir_proceso(pid, PROCESS_SET_QUOTA | PROCESS_SET_INFORMATION)
    if not h:
        return False
    _kernel.CloseHandle(h)
    return True


# ---------------------------------------------------------------------------
# Palanca 1: trim del working set (dura, tiene costo)
# ---------------------------------------------------------------------------
def trimear(pid: int) -> tuple[bool, int]:
    """
    Vacia el working set del proceso. Devuelve (exito, bytes_liberados).

    Esto es lo que el spec llama "ajustar paging": las paginas sucias van al
    pagefile y las limpias a la standby list. SI EL PROCESO LAS VUELVE A TOCAR
    hay que traerlas de vuelta -> eso es un swap innecesario, y es exactamente
    lo que el modelo aprende a predecir.

    Nunca llamar a esto sin consultar al modelo primero.
    """
    antes = memoria_proceso(pid)
    h = _abrir_proceso(pid, PROCESS_SET_QUOTA)
    if not h:
        return False, 0
    try:
        ok = bool(_kernel.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value,
                                                   ctypes.c_size_t(-1).value))
    finally:
        _kernel.CloseHandle(h)
    if not ok:
        return False, 0
    despues = memoria_proceso(pid)
    if antes and despues:
        return True, max(0, antes.working_set - despues.working_set)
    return True, 0


# ---------------------------------------------------------------------------
# Palanca 2: prioridad de memoria (suave, sin costo inmediato)
# ---------------------------------------------------------------------------
def _fijar_prioridad_raw(pid: int, nivel: int) -> bool | None:
    h = _abrir_proceso(pid, PROCESS_SET_INFORMATION)
    if not h:
        return None
    try:
        mpi = _MEMORY_PRIORITY_INFORMATION(nivel)
        return bool(_kernel.SetProcessInformation(h, ProcessMemoryPriority,
                                                  ctypes.byref(mpi), ctypes.sizeof(mpi)))
    finally:
        _kernel.CloseHandle(h)


def fijar_prioridad_memoria(pid: int, nivel: str) -> bool:
    """
    Marca al proceso como candidato preferido (o no) para desalojo.

    Nivel "low"/"very_low" le dice a Windows: cuando necesites RAM, sacala de
    aca primero. NO fuerza el swap ahora. Es la version segura de la idea:
    el sistema desaloja solo las paginas que realmente estan frias, en vez de
    que nosotros adivinemos con un trim ciego.
    """
    n = MEMORY_PRIORITY.get(nivel)
    if n is None:
        return False
    return _fijar_prioridad_raw(pid, n) is True


def leer_prioridad_memoria(pid: int) -> str | None:
    h = _abrir_proceso(pid, 0)
    if not h:
        return None
    try:
        mpi = _MEMORY_PRIORITY_INFORMATION()
        if not _kernel.GetProcessInformation(h, ProcessMemoryPriority,
                                             ctypes.byref(mpi), ctypes.sizeof(mpi)):
            return None
        return MEMORY_PRIORITY_NOMBRE.get(mpi.MemoryPriority, f"?{mpi.MemoryPriority}")
    finally:
        _kernel.CloseHandle(h)


# ---------------------------------------------------------------------------
# Palanca 3: purga de standby list (SOLO admin, normalmente apagada)
# ---------------------------------------------------------------------------
def purgar_standby() -> tuple[bool, str]:
    """
    Tira la cache de disco del sistema.

    REQUIERE ADMIN. Y aunque estuviera disponible, esto EMPEORA la respuesta:
    obliga a re-leer de disco todo lo que estaba cacheado. Existe por
    completitud, no porque convenga usarla. El agente nunca la llama solo.
    """
    try:
        cmd = ctypes.c_int(MemoryPurgeStandbyList)
        ret = _ntdll.NtSetSystemInformation(SystemMemoryListInformation,
                                            ctypes.byref(cmd), ctypes.sizeof(cmd))
        ret &= 0xFFFFFFFF
        if ret == 0:
            return True, "ok"
        if ret == 0xC0000061:
            return False, "requiere admin (STATUS_PRIVILEGE_NOT_HELD)"
        if ret == 0xC000000D:
            return False, "esta version de Windows no acepta esta llamada"
        return False, f"error 0x{ret:08X}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Lectura sin cache, para la sonda de tiempo de respuesta
# ---------------------------------------------------------------------------
def crear_archivo_prueba(ruta: str, mb: int) -> bool:
    """Crea un archivo de prueba con datos incompresibles."""
    try:
        import os
        datos = os.urandom(1024 * 1024)
        with open(ruta, "wb") as f:
            for _ in range(mb):
                f.write(datos)
            f.flush()
            import os as _os
            _os.fsync(f.fileno())
        return True
    except Exception:
        return False


def leer_sin_cache(ruta: str, mb_a_leer: int = 4) -> tuple[float, int]:
    """
    Lee un archivo salteando la cache del sistema (FILE_FLAG_NO_BUFFERING).

    Devuelve (segundos, bytes_leidos). El tamaño del buffer DEBE ser multiplo
    del sector (4096) o ReadFile falla; usamos 1 MB.

    Esta es la medicion de latencia de disco real, la que se degrada cuando
    el sistema esta paginando de mas.
    """
    import time

    GENERIC_READ = 0x80000000
    FILE_FLAG_NO_BUFFERING = 0x20000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = -1

    h = _kernel.CreateFileW(ruta, GENERIC_READ, 1, None, OPEN_EXISTING,
                            FILE_FLAG_NO_BUFFERING | FILE_ATTRIBUTE_NORMAL, None)
    if not h or h == INVALID_HANDLE_VALUE:
        return -1.0, 0
    try:
        bufo = ctypes.create_string_buffer(1024 * 1024)
        leidos = wt.DWORD(0)
        total = 0
        objetivo = mb_a_leer * 1024 * 1024
        t0 = time.perf_counter()
        while total < objetivo:
            if not _kernel.ReadFile(h, bufo, len(bufo), ctypes.byref(leidos), None):
                break
            if leidos.value == 0:
                break
            total += leidos.value
        dt = time.perf_counter() - t0
        return dt, total
    finally:
        _kernel.CloseHandle(h)

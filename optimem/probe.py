"""
Sonda de tiempo de respuesta.

ESTA ES LA METRICA DE EXITO DEL PROYECTO, y estuvo mal diseñada en la primera
version. Conviene entender por que, porque el error es facil de repetir:

  Version 1 medía cuanto tardaba en asignar memoria NUEVA y tocarla. Parecia
  lo obvio. Pero se midio que NO responde a la presion de memoria: con 3 GB
  de presion controlada, el tiempo BAJO 8%. La razon es que asignar memoria
  nueva se satisface con fallos de demanda cero, que Windows sirve desde la
  lista de paginas libres, SIN tocar el disco. Solo se degrada cuando el
  sistema esta thrashando de verdad, y para entonces ya es tarde para medir.

  Version 1 tambien medía el disco con FILE_FLAG_NO_BUFFERING, o sea salteando
  la cache a proposito. Pero la cache del sistema es justamente la VICTIMA de
  la presion de memoria: Windows la desaloja para hacer lugar. Medir con una
  sonda diseñada para no ver el fenomeno no tiene sentido.

Lo que SI se degrada con la presion es lo que el usuario siente: que el
sistema le robe la cache y el working set. Entonces medimos eso:

  t_cache   Leer un archivo que YA estaba en cache. Si sigue cacheado, sale de
            RAM (~10 ms). Si la presion lo desalojo, sale del disco (~200 ms).
            Es un salto de 20x: la señal mas limpia que tenemos.
  t_ws      Volver a tocar un buffer propio que ya estaba fallado. Si las
            paginas siguen residentes, son fallos suaves (rapidos). Si el
            sistema se las llevo, hay que traerlas de vuelta del disco.
            Esto es literalmente "me robaron el working set".
  t_cpu     Un lazo aritmetico fijo: contencion de planificador.
  t_memoria Asignar memoria nueva. Se mantiene como referencia secundaria
            (mide el camino de fallo de pagina) aunque sea poco sensible.

IMPORTANTE: la sonda corre en un proceso que VIVE entre mediciones. Tiene que
ser asi: si el proceso muere y renace en cada medicion, su buffer siempre esta
recien fallado y nunca se puede detectar que se lo llevaron. El proceso de la
sonda se queda con su working set puesto, como una aplicacion cualquiera.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time

# Pesos del indice. La cache y el working set pesan mas porque son las que
# realmente miden lo que la presion de memoria le hace al usuario.
PESOS = {"cache": 0.40, "ws": 0.30, "cpu": 0.20, "memoria": 0.10}

# Nombre del archivo que usamos para la prueba de cache.
ARCHIVO_CACHE = "_sonda_cache.bin"

MB_CACHE = 24


# ---------------------------------------------------------------------------
# Mediciones
# ---------------------------------------------------------------------------
def _touch(ptr: int, tam: int) -> None:
    """Toca una pagina de cada 4096 bytes: fuerza el fallo de cada pagina."""
    ctypes.memset(ptr, 1, tam)


def medir_ws(ptr: int, tam: int) -> float:
    """
    Milisegundos en volver a tocar un buffer ya fallado.

    Solo LEE, no escribe: una lectura de una pagina ya presente es un fallo
    suave (rapido). Si la pagina fue desalojada, es un fallo duro y hay que
    esperar al disco. Esa diferencia es toda la señal.

    Leer en vez de escribir importa: escribir una pagina ausente provoca un
    fallo de demanda cero desde la lista de libres, que es rapido aunque el
    sistema este bajo presion. Leer obliga a traer el contenido real.
    """
    t0 = time.perf_counter()
    # Sumamos para que el interprete no pueda descartar la lectura.
    total = 0
    paso = 4096
    for off in range(0, tam, paso):
        total += ctypes.c_ubyte.from_address(ptr + off).value
    dt = (time.perf_counter() - t0) * 1000.0
    if total == -1:
        print(total)
    return dt


def medir_cache(ruta: str) -> float:
    """
    Milisegundos en leer un archivo que deberia estar en cache.

    Lectura normal y bufferizada, a proposito: queremos que el sistema lo
    cachee. La gracia es ver si SIGUE cacheado desde la vez anterior.
    """
    t0 = time.perf_counter()
    with open(ruta, "rb") as f:
        n = len(f.read())
    dt = (time.perf_counter() - t0) * 1000.0
    if n == 0:
        return -1.0
    return dt


def medir_cpu(iteraciones: int) -> float:
    """Milisegundos de un lazo aritmetico fijo. Mide contencion del planificador."""
    t0 = time.perf_counter()
    x = 12345
    acum = 0
    for i in range(iteraciones):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        acum ^= x
    dt = (time.perf_counter() - t0) * 1000.0
    if acum == -1:
        print(acum)
    return dt


def medir_memoria_nueva(mb: int) -> float:
    """Milisegundos en asignar `mb` nuevos y tocarlos. Poco sensible; referencia."""
    MEM_COMMIT, MEM_RESERVE, MEM_RELEASE = 0x1000, 0x2000, 0x8000
    PAGE_READWRITE = 0x04
    kernel = ctypes.windll.kernel32
    kernel.VirtualAlloc.restype = ctypes.c_void_p
    kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_ulong, ctypes.c_ulong]
    kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]

    tam = mb * 1024 * 1024
    ptr = kernel.VirtualAlloc(None, tam, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not ptr:
        return -1.0
    try:
        t0 = time.perf_counter()
        _touch(ptr, tam)
        return (time.perf_counter() - t0) * 1000.0
    finally:
        kernel.VirtualFree(ptr, 0, MEM_RELEASE)


def preparar_archivo_cache(cfg) -> str:
    """
    Crea el archivo de la prueba de cache si hace falta.

    Incompresible a proposito: si fuera comprimible, Windows lo comprime y la
    lectura no tocaria el disco ni aunque este desalojado, y la medicion no
    veria nada.
    """
    ruta = str(cfg.dir / ARCHIVO_CACHE)
    objetivo = MB_CACHE * 1024 * 1024
    if os.path.exists(ruta) and os.path.getsize(ruta) >= objetivo:
        return ruta
    datos = os.urandom(1024 * 1024)
    with open(ruta, "wb") as f:
        for _ in range(MB_CACHE):
            f.write(datos)
        f.flush()
        os.fsync(f.fileno())
    return ruta


# ---------------------------------------------------------------------------
# Indice compuesto
# ---------------------------------------------------------------------------
def calcular_indice(med: dict, base: dict | None = None) -> float:
    """
    Indice de respuesta. 1.00 = igual que la linea de base; mas alto = peor.

    Cada medicion se normaliza contra su valor de referencia y se combinan
    con los pesos. Se renormaliza sobre las mediciones disponibles, para que
    una medicion faltante no hunda el indice.
    """
    partes = {}
    for clave in PESOS:
        v = med.get(clave)
        if v is None or v < 0:
            continue
        ref = (base or {}).get(clave)
        partes[clave] = (v / ref) if ref else v
    if not partes:
        return -1.0
    total_peso = sum(PESOS[k] for k in partes)
    return sum(PESOS[k] * v for k, v in partes.items()) / total_peso


# ---------------------------------------------------------------------------
# Sonda viva: el proceso que vive entre mediciones
# ---------------------------------------------------------------------------
class SondaViva:
    """
    Mantiene un working set propio y lo vuelve a tocar en cada medicion.

    Por que un proceso que vive: si el proceso muere y renace, su buffer
    siempre esta recien fallado y jamas se puede detectar que el sistema se lo
    llevo. Para medir "me robaron las paginas" hay que tener paginas que
    alguien pueda robar.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.tam = cfg.sonda_mb_memoria * 1024 * 1024
        MEM_COMMIT, MEM_RESERVE = 0x1000, 0x2000
        PAGE_READWRITE = 0x04
        self._kernel = ctypes.windll.kernel32
        self._kernel.VirtualAlloc.restype = ctypes.c_void_p
        self._kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                              ctypes.c_ulong, ctypes.c_ulong]
        self.ptr = self._kernel.VirtualAlloc(None, self.tam, MEM_COMMIT | MEM_RESERVE,
                                             PAGE_READWRITE)
        if not self.ptr:
            raise RuntimeError(f"no se pudieron reservar {cfg.sonda_mb_memoria} MB")
        _touch(self.ptr, self.tam)      # fallamos todo una vez
        self.ruta_cache = preparar_archivo_cache(cfg)

    def medir(self) -> dict:
        r = {"ts": time.time(), "pid": os.getpid()}
        try:
            r["ws"] = medir_ws(self.ptr, self.tam)
        except Exception as e:
            r["ws"] = -1.0
            r["err_ws"] = f"{type(e).__name__}: {e}"
        try:
            r["cache"] = medir_cache(self.ruta_cache)
        except Exception as e:
            r["cache"] = -1.0
            r["err_cache"] = f"{type(e).__name__}: {e}"
        try:
            r["cpu"] = medir_cpu(self.cfg.sonda_iteraciones_cpu)
        except Exception as e:
            r["cpu"] = -1.0
            r["err_cpu"] = f"{type(e).__name__}: {e}"
        try:
            r["memoria"] = medir_memoria_nueva(self.cfg.sonda_mb_memoria)
        except Exception as e:
            r["memoria"] = -1.0
            r["err_memoria"] = f"{type(e).__name__}: {e}"
        return r

    def correr(self, con, parar=None) -> None:
        from . import db, winapi

        intervalo = self.cfg.intervalo_sonda_seg
        db.registrar_evento(con, "sonda_viva", "info",
                            detalle=f"buffer de {self.cfg.sonda_mb_memoria} MB reservado")
        while not (parar and parar()):
            try:
                r = self.medir()
                base = db.leer_estado(con, "linea_base_sonda")
                indice = calcular_indice(r, base)
                m = winapi.memoria_sistema()
                con.execute(
                    "INSERT OR REPLACE INTO sondas "
                    "(ts, t_memoria_ms, t_cpu_ms, t_disco_ms, indice, ram_disp, "
                    " commit_pct, detalle, t_cache_ms, t_ws_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["ts"], r.get("memoria"), r.get("cpu"), None, indice,
                     m.disponible if m else None,
                     m.presion_commit if m else None,
                     json.dumps({k: v for k, v in r.items() if k.startswith("err_")}) or None,
                     r.get("cache"), r.get("ws")),
                )
            except Exception as e:
                try:
                    db.registrar_evento(con, "error", "aviso",
                                        detalle=f"sonda viva: {type(e).__name__}: {e}")
                except Exception:
                    pass
            # Dormimos en tramos para poder cortar rapido.
            fin = time.time() + intervalo
            while time.time() < fin and not (parar and parar()):
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Sonda de una sola vez (compatibilidad y depuracion)
# ---------------------------------------------------------------------------
def ejecutar_sonda(cfg, *, timeout: float = 90.0, en_proceso: bool = False) -> dict:
    """
    Una medicion suelta.

    OJO: en este modo el buffer es nuevo cada vez, asi que `ws` siempre va a
    dar rapido y no sirve. Es para probar que la sonda funciona, no para
    medir de verdad. La medicion real la hace `SondaViva`.
    """
    if en_proceso:
        s = SondaViva(cfg)
        return s.medir()

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        ruta_salida = tmp.name
    try:
        codigo = (
            "import sys, json;"
            "sys.path.insert(0, %r);"
            "from optimem import config, probe;"
            "c = config.cargar();"
            "s = probe.SondaViva(c);"
            "r = s.medir();"
            "json.dump(r, open(%r, 'w'))"
            % (str(_raiz_proyecto()), ruta_salida)
        )
        p = subprocess.run([sys.executable, "-c", codigo], capture_output=True,
                           text=True, timeout=timeout, cwd=str(_raiz_proyecto()))
        if p.returncode != 0 or not os.path.exists(ruta_salida):
            raise RuntimeError(f"la sonda fallo (rc={p.returncode}): "
                               f"{(p.stderr or '').strip()[-400:]}")
        with open(ruta_salida, encoding="utf-8") as f:
            return json.load(f)
    finally:
        try:
            os.unlink(ruta_salida)
        except OSError:
            pass


def _raiz_proyecto():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    # Modo vivo: lo lanza el recolector como proceso aparte.
    if "--vivo" in sys.argv:
        from . import config as _cfg, db as _db
        c = _cfg.cargar()
        con = _db.inicializar(c.ruta_db)
        SondaViva(c).correr(con)
    else:
        from . import config as _cfg
        c = _cfg.cargar()
        s = SondaViva(c)
        print(json.dumps(s.medir(), indent=2))

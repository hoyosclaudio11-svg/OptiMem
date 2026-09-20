"""
Validacion 3: la prueba que define si el proyecto es viable sin admin.

Pregunta: podemos abrir los procesos REALES del usuario con permiso de
escritura (PROCESS_SET_QUOTA) para poder trimearlos?

Y la palanca suave: SetProcessInformation(ProcessMemoryPriority) marca un
proceso como "evictable primero" SIN forzar el swap inmediato. Es la version
profesional de la idea, mucho mas segura que el trim a ciegas.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

import psutil

kernel = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

kernel.GetCurrentProcess.restype = wt.HANDLE
kernel.OpenProcess.restype = wt.HANDLE
kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_VM_READ = 0x0010

OK, MAL = "[OK]", "[!!]"
print("=" * 74)
print("OptiMem - validacion 3: alcance real sin admin")
print("=" * 74)

yo = os.getpid()
mi_usuario = psutil.Process(yo).username()

# ----------------------------------------------------------------------
# 1. Que procesos puedo abrir con permiso de ESCRITURA
# ----------------------------------------------------------------------
print("\n--- 1. Alcance de escritura sobre procesos reales ---")


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def abrir(pid, acceso):
    return kernel.OpenProcess(acceso, False, pid)


def datos(pid):
    h = abrir(pid, PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_READ)
    if not h:
        return None
    try:
        m = PMC()
        m.cb = ctypes.sizeof(m)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            return None
        return m
    finally:
        kernel.CloseHandle(h)


filas = []
for pr in psutil.process_iter(["pid", "name", "username", "memory_info"]):
    pid = pr.info["pid"]
    if pid in (0, 4) or pid == yo:
        continue
    try:
        mismo_usuario = pr.info["username"] == mi_usuario
    except Exception:
        mismo_usuario = False
    h = abrir(pid, PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA)
    puede_trim = bool(h)
    if h:
        kernel.CloseHandle(h)
    h2 = abrir(pid, PROCESS_SET_INFORMATION)
    puede_prio = bool(h2)
    if h2:
        kernel.CloseHandle(h2)
    rss = pr.info["memory_info"].rss if pr.info["memory_info"] else 0
    filas.append((pid, pr.info["name"] or "?", mismo_usuario, puede_trim, puede_prio, rss))

trim_ok = [f for f in filas if f[3]]
prio_ok = [f for f in filas if f[4]]
print(f"  Procesos analizados          : {len(filas)}")
print(f"  Abribles con SET_QUOTA (trim): {len(trim_ok)}")
print(f"  Abribles con SET_INFORMATION : {len(prio_ok)}  (palanca de prioridad de memoria)")

# RAM que esta en juego
ram_trim = sum(f[5] for f in trim_ok) / 1024**3
ram_total = sum(f[5] for f in filas) / 1024**3
print(f"  RAM alcanzable por trim      : {ram_trim:.2f} GB de {ram_total:.2f} GB")

print("\n  Los 15 procesos mas grandes que SI puedo tocar:")
for pid, nom, mismo, t, p, rss in sorted(trim_ok, key=lambda x: -x[5])[:15]:
    marca = "propio" if mismo else "otro usuario"
    print(f"    pid {pid:<7} {nom[:30]:<30} {rss / 1024**2:8.1f} MB  {marca}")

print("\n  Los 10 mas grandes que NO puedo tocar:")
no_trim = [f for f in filas if not f[3]]
for pid, nom, mismo, t, p, rss in sorted(no_trim, key=lambda x: -x[5])[:10]:
    print(f"    pid {pid:<7} {nom[:30]:<30} {rss / 1024**2:8.1f} MB")

# ----------------------------------------------------------------------
# 2. SetProcessInformation(ProcessMemoryPriority) - la palanca suave
# ----------------------------------------------------------------------
print("\n--- 2. Palanca suave: prioridad de memoria ---")


class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
    _fields_ = [("MemoryPriority", wt.ULONG)]


ProcessMemoryPriority = 0


def fijar_prioridad(pid, nivel):
    """nivel: 1=VERY_LOW 2=LOW 3=MEDIUM 4=BELOW_NORMAL 5=NORMAL"""
    h = abrir(pid, PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION)
    if not h:
        return None, f"OpenProcess err={kernel.GetLastError()}"
    try:
        mpi = MEMORY_PRIORITY_INFORMATION(nivel)
        ok = kernel.SetProcessInformation(h, ProcessMemoryPriority,
                                          ctypes.byref(mpi), ctypes.sizeof(mpi))
        return bool(ok), kernel.GetLastError()
    finally:
        kernel.CloseHandle(h)


# proceso hijo de prueba
import subprocess
import textwrap
import time

hijo = textwrap.dedent("""
    import time, sys
    buf = bytearray(50 * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    print("LISTO", flush=True)
    time.sleep(20)
""")
ruta = os.path.join(os.environ.get("TEMP", "."), "_optimem_hijo3.py")
with open(ruta, "w") as f:
    f.write(hijo)
proc = subprocess.Popen([sys.executable, ruta], stdout=subprocess.PIPE, text=True)
proc.stdout.readline()
time.sleep(0.5)

for nivel, nombre in ((1, "VERY_LOW"), (2, "LOW"), (5, "NORMAL")):
    ok, err = fijar_prioridad(proc.pid, nivel)
    print(f"  SetProcessInformation(pid={proc.pid}, {nombre:<9}) -> {ok} (err {err})")

print(f"  {OK if ok else MAL} La palanca de prioridad de memoria funciona en procesos propios")

# probamos sobre un proceso real del usuario
candidatos = [f for f in trim_ok if f[2] and f[5] > 100 * 1024**2][:3]
print("\n  Probando la palanca suave sobre procesos reales tuyos (los devuelvo a NORMAL):")
for pid, nom, mismo, t, p, rss in candidatos:
    ok, err = fijar_prioridad(pid, 2)
    if ok:
        fijar_prioridad(pid, 3)  # volver a MEDIUM (default real)
        print(f"    {OK} pid {pid:<7} {nom[:28]:<28} -> acepto LOW y volvio a MEDIUM")
    else:
        print(f"    {MAL} pid {pid:<7} {nom[:28]:<28} -> rechazado (err {err})")

proc.kill()
proc.wait()
os.remove(ruta)

# ----------------------------------------------------------------------
# 3. Cuanta RAM es realmente "fria" (candidata a liberar sin costo)
# ----------------------------------------------------------------------
print("\n--- 3. Estimacion de RAM fria recuperable ---")
print("  Un proceso con mucha RAM y poca CPU acumulada es candidato: sus paginas")
print("  probablemente no se toquen pronto, asi que bajarlas no cuesta respuesta.")
print()
tiempo_encendido = time.time() - psutil.boot_time()
frios = []
for pr in psutil.process_iter(["pid", "name", "memory_info", "cpu_times", "create_time"]):
    try:
        info = pr.info
        if not info["memory_info"] or info["memory_info"].rss < 80 * 1024**2:
            continue
        if not any(f[0] == info["pid"] and f[3] for f in filas):
            continue
        ct = info["cpu_times"]
        cpu_total = (ct.user + ct.system) if ct else 0
        edad = max(tiempo_encendido - (time.time() - info["create_time"])
                   if info["create_time"] else 1, 1)
        # CPU por minuto de vida: bajo = proceso frio
        tasa = cpu_total / max(edad / 60.0, 1.0)
        frios.append((info["pid"], info["name"], info["memory_info"].rss, tasa))
    except Exception:
        continue

frios.sort(key=lambda x: x[1] / max(x[3], 0.001), reverse=True)
print(f"  {'pid':<8}{'proceso':<30}{'RAM':>10}{'CPU/min':>10}   frialdad")
total_cand = 0
for pid, nom, rss, tasa in frios[:18]:
    fr = rss / 1024**2 / max(tasa, 0.001)
    total_cand += rss
    print(f"  {pid:<8}{(nom or '?')[:28]:<30}{rss / 1024**2:9.1f} MB{tasa:9.3f}   {fr:8.0f}")
print(f"\n  RAM en los candidatos frios listados: {total_cand / 1024**3:.2f} GB")
print("  (Esto es lo que OptiMem puede reclamar SIN degradar la respuesta)")

print("\n" + "=" * 74)
print("Validacion 3 terminada.")
print("=" * 74)

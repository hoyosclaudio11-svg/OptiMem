"""
Validacion de las APIs de Windows que va a usar OptiMem.

No construimos nada hasta confirmar que estas llamadas devuelven datos
coherentes en ESTA maquina. Cada bloque imprime el valor crudo y una
verificacion cruzada contra psutil (que ya es una fuente confiable).

Ejecutar:  python _validar_apis.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

import psutil

OK = "[OK]"
MAL = "[!!]"

print("=" * 72)
print("OptiMem - validacion de APIs de Windows")
print("=" * 72)


# ----------------------------------------------------------------------
# 1. Privilegios
# ----------------------------------------------------------------------
print("\n--- 1. Privilegios ---")
es_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
print(f"  IsUserAnAdmin() = {es_admin}  {'(admin)' if es_admin else '(usuario normal)'}")

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def habilitar_privilegio(nombre):
    """Habilita un privilegio en el token actual. Devuelve True si quedo habilitado."""
    advapi = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),
                                   TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token)):
        return False, "OpenProcessToken fallo"
    try:
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, nombre, ctypes.byref(luid)):
            return False, "LookupPrivilegeValue fallo (privilegio inexistente)"
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp),
                                            ctypes.sizeof(tp), None, None):
            return False, "AdjustTokenPrivileges fallo"
        err = kernel.GetLastError()
        if err == 1300:  # ERROR_NOT_ALL_ASSIGNED
            return False, "ERROR_NOT_ALL_ASSIGNED (no esta en el token)"
        return True, "habilitado"
    finally:
        kernel.CloseHandle(token)


for priv in ("SeProfileSingleProcessPrivilege", "SeIncreaseQuotaPrivilege"):
    ok, msg = habilitar_privilegio(priv)
    print(f"  {OK if ok else MAL} {priv}: {msg}")

print("\n  Nota: sin estos privilegios solo funcionan las palancas de proceso propio.")


# ----------------------------------------------------------------------
# 2. GetPerformanceInfo - memoria global
# ----------------------------------------------------------------------
print("\n--- 2. GetPerformanceInfo (psapi) ---")


class PERFORMANCE_INFORMATION(ctypes.Structure):
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


try:
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    ok = ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb)
    if not ok:
        raise ctypes.WinError()
    pg = pi.PageSize
    tot = pi.PhysicalTotal * pg
    avail = pi.PhysicalAvailable * pg
    vm = psutil.virtual_memory()
    print(f"  PageSize            = {pg} bytes")
    print(f"  PhysicalTotal       = {tot / 1024**3:.2f} GB   (psutil: {vm.total / 1024**3:.2f} GB)")
    print(f"  PhysicalAvailable   = {avail / 1024**3:.2f} GB   (psutil avail: {vm.available / 1024**3:.2f} GB)")
    print(f"  CommitTotal         = {pi.CommitTotal * pg / 1024**3:.2f} GB")
    print(f"  CommitLimit         = {pi.CommitLimit * pg / 1024**3:.2f} GB")
    print(f"  SystemCache         = {pi.SystemCache * pg / 1024**3:.2f} GB   <- standby + modified")
    print(f"  KernelPaged         = {pi.KernelPaged * pg / 1024**2:.1f} MB")
    print(f"  KernelNonpaged      = {pi.KernelNonpaged * pg / 1024**2:.1f} MB")
    print(f"  ProcessCount        = {pi.ProcessCount}   (psutil: {len(psutil.pids())})")
    dif = abs(avail - vm.available) / max(vm.available, 1)
    print(f"  {OK if dif < 0.15 else MAL} Coincide con psutil (dif {dif*100:.1f}%)")
except Exception as e:
    print(f"  {MAL} Fallo: {e}")


# ----------------------------------------------------------------------
# 3. NtQuerySystemInformation class 2 - fallos de pagina duros (PagesRead)
# ----------------------------------------------------------------------
print("\n--- 3. NtQuerySystemInformation(SystemPerformanceInformation) ---")
print("  Este es el numero real de paginas leidas de disco (swaps duros).")
print("  La estructura NO esta documentada: validamos contra el disco real.")

ntdll = ctypes.windll.ntdll


def spi_raw():
    buf = ctypes.create_string_buffer(4096)
    ret = ctypes.c_ulong(0)
    ntdll.NtQuerySystemInformation(2, buf, ctypes.sizeof(buf), ctypes.byref(ret))
    return buf.raw[: ret.value]


try:
    raw = spi_raw()
    print(f"  Tamano devuelto: {len(raw)} bytes")
    # SYSTEM_PERFORMANCE_INFORMATION (x64):
    #   0  IdleProcessTime      LARGE_INTEGER
    #   8  IoReadTransferCount  LARGE_INTEGER
    #  16  IoWriteTransferCount LARGE_INTEGER
    #  24  IoOtherTransferCount LARGE_INTEGER
    #  32  IoReadOperationCount  ULONG
    #  36  IoWriteOperationCount ULONG
    #  40  IoOtherOperationCount ULONG
    #  44  AvailablePages         ULONG
    #  48  CommittedPages         ULONG
    #  52  CommitLimit            ULONG
    #  56  PeakCommitment         ULONG
    #  60  PageFaultCount         ULONG
    #  64  CopyOnWriteCount       ULONG
    #  68  TransitionCount        ULONG
    #  72  CacheTransitionCount   ULONG
    #  76  DemandZeroCount        ULONG
    #  80  PageReadCount          ULONG
    #  84  PageReadIoCount        ULONG
    #  88  CacheReadCount         ULONG
    #  92  CacheIoCount           ULONG
    #  96  DirtyPagesWriteCount   ULONG
    # 100  DirtyWriteIoCount      ULONG
    # 104  MappedPagesWriteCount  ULONG
    # 108  MappedWriteIoCount     ULONG
    # 112  PagedPoolPages         ULONG
    # 116  NonPagedPoolPages      ULONG
    import struct

    def u32(off):
        return struct.unpack_from("<I", raw, off)[0]

    pag = pi.PageSize or 4096
    avail_pages = u32(44)
    commit_pages = u32(48)
    commit_limit = u32(52)
    pagefaults = u32(60)
    pagereads = u32(80)
    readios = u32(84)

    print(f"  AvailablePages  = {avail_pages * pag / 1024**3:.2f} GB   (psutil avail: {vm.available / 1024**3:.2f} GB)")
    print(f"  CommittedPages  = {commit_pages * pag / 1024**3:.2f} GB   (GetPerfInfo: {pi.CommitTotal * pag / 1024**3:.2f} GB)")
    print(f"  CommitLimit     = {commit_limit * pag / 1024**3:.2f} GB")
    print(f"  PageFaultCount  = {pagefaults:,}   (total de fallos de pagina, software+disco)")
    print(f"  PageReadCount   = {pagereads:,}   <- paginas traidas de DISCO")
    print(f"  PageReadIoCount = {readios:,}")

    d1 = abs(avail_pages * pag - vm.available) / max(vm.available, 1)
    d2 = abs(commit_pages * pag - pi.CommitTotal * pag) / max(pi.CommitTotal * pag, 1)
    valido = d1 < 0.15 and d2 < 0.10
    print(f"  {OK if valido else MAL} Offsets validados (avail dif {d1*100:.1f}%, commit dif {d2*100:.1f}%)")
    if not valido:
        print("  >>> Los offsets NO son confiables en esta version. Usaremos el fallback.")

    # Prueba de que PageReadCount se mueve: forzamos I/O de disco
    a = u32(80)
    datos = os.urandom(40 * 1024 * 1024)
    ruta = os.path.join(os.environ.get("TEMP", "."), "_optimem_io_test.bin")
    t0 = time.time()
    with open(ruta, "wb") as f:
        f.write(datos)
        f.flush()
        os.fsync(f.fileno())
    with open(ruta, "r+b") as f:
        f.seek(0)
        f.read()
    os.remove(ruta)
    time.sleep(0.3)
    b = u32(80)
    print(f"  Tras escribir+leer 40 MB: PageReadCount {a:,} -> {b:,}  (delta {b - a:,})")
    print(f"  {OK if b > a else MAL} Contador vivo -> sirve como metrica de swapping duro")
except Exception as e:
    print(f"  {MAL} Fallo: {type(e).__name__}: {e}")


# ----------------------------------------------------------------------
# 4. Fallos de pagina por proceso (psapi)
# ----------------------------------------------------------------------
print("\n--- 4. GetProcessMemoryInfo por proceso ---")
print("  PageFaultCount por proceso: la senal para detectar re-lecturas tras un trim.")


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
PROCESS_VM_READ = 0x0010


def leer_mem_proceso(pid):
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return pmc
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


leidos = 0
sin_acceso = 0
muestras = []
for p in psutil.process_iter(["pid", "name", "memory_info"]):
    r = leer_mem_proceso(p.info["pid"])
    if r is None:
        sin_acceso += 1
    else:
        leidos += 1
        if len(muestras) < 4:
            muestras.append((p.info["pid"], p.info["name"],
                             r.WorkingSetSize, p.info["memory_info"].rss, r.PageFaultCount))
print(f"  Procesos leidos OK: {leidos}   sin acceso: {sin_acceso}")
for pid, nom, ws, rss, pf in muestras:
    dif = abs(ws - rss) / max(rss, 1)
    print(f"    pid {pid:<7} {nom[:26]:<26} WS {ws/1024**2:8.1f} MB  rss {rss/1024**2:8.1f} MB "
          f"({'coincide' if dif < 0.20 else 'DIFIERE'})  faults {pf:,}")


# ----------------------------------------------------------------------
# 5. Trim de working set sobre un proceso propio (prueba real y segura)
# ----------------------------------------------------------------------
print("\n--- 5. Prueba de trim sobre un proceso de prueba (NO sobre tus servicios) ---")
print("  Lanzamos un hijo que reserva y toca 200 MB, lo medimos, lo trimeamos y vemos")
print("  si al volver a tocarlo se re-lee de disco. Eso define el 'swap innecesario'.")

import subprocess
import textwrap

hijo_py = textwrap.dedent("""
    import time, sys
    buf = bytearray(200 * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    print("LISTO", flush=True)
    time.sleep(30)
""")
ruta_hijo = os.path.join(os.environ.get("TEMP", "."), "_optimem_hijo.py")
with open(ruta_hijo, "w") as f:
    f.write(hijo_py)

proc = subprocess.Popen([sys.executable, ruta_hijo], stdout=subprocess.PIPE, text=True)
proc.stdout.readline()  # espera "LISTO"
time.sleep(1.0)

pid = proc.pid
p = psutil.Process(pid)
antes = p.memory_info().rss
m_antes = leer_mem_proceso(pid)
print(f"  Hijo pid {pid}: RSS antes del trim = {antes / 1024**2:.1f} MB")

# Trim
h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
if h:
    ok = ctypes.windll.kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value,
                                                         ctypes.c_size_t(-1).value)
    ctypes.windll.kernel32.CloseHandle(h)
    print(f"  SetProcessWorkingSetSize(-1,-1) -> {bool(ok)}")
else:
    print(f"  {MAL} OpenProcess fallo")
time.sleep(1.5)
try:
    despues = p.memory_info().rss
    m_despues = leer_mem_proceso(pid)
    print(f"  RSS despues del trim = {despues / 1024**2:.1f} MB "
          f"(libero {(antes - despues) / 1024**2:.1f} MB)")
    if m_antes and m_despues:
        print(f"  Faults delta durante el trim = {m_despues.PageFaultCount - m_antes.PageFaultCount:,}")
    print(f"  {OK if despues < antes * 0.7 else MAL} El trim funciona en esta maquina")
except psutil.NoSuchProcess:
    print(f"  {MAL} El proceso hijo desaparecio")

proc.kill()
proc.wait()
os.remove(ruta_hijo)


# ----------------------------------------------------------------------
# 6. psutil.swap_memory en Windows - que significa sin/sout aca
# ----------------------------------------------------------------------
print("\n--- 6. psutil.swap_memory() en Windows ---")
sm = psutil.swap_memory()
print(f"  total={sm.total / 1024**3:.2f} GB  used={sm.used / 1024**3:.2f} GB  "
      f"free={sm.free / 1024**3:.2f} GB  percent={sm.percent}%")
print(f"  sin={sm.sin:,}  sout={sm.sout:,}")
print("  (En Windows psutil los reporta en BYTES; ojo al interpretarlos como paginas)")
pf = psutil.disk_partitions()
print(f"  Particiones: {[d.mountpoint for d in pf]}")


print("\n" + "=" * 72)
print("Validacion terminada.")
print("=" * 72)

"""
El experimento decisivo para la parte de machine learning.

El agente hace una sola cosa que cuesta algo: vaciar el working set de un
proceso. La pregunta que define si el modelo tiene algo que predecir es:

    CUANTO CUESTA, de verdad, que a un proceso le vacien el working set?

Si el costo es cero o indetectable, no hay nada que predecir y el modelo
sobra. Si el costo es grande, entonces SI importa elegir bien a quien
trimear, y ahi el modelo tiene sentido.

Metodo, sobre un proceso hijo controlado:
  1. reservar y tocar N MB            -> paginas residentes
  2. medir cuanto tarda en re-tocarlas  (residente: fallos suaves)
  3. trimear el working set
  4. medir cuanto tarda en re-tocarlas  (desalojado: hay que traerlas)
  5. ademas, comparar la sonda de cache: el trim contamina la cache?

Se hace con y sin presion de fondo, para ver si el costo depende de ella.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path
import textwrap
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimem import config, probe, winapi

MB = 300

HIJO = textwrap.dedent("""
    import ctypes, sys, time, json
    MB = %d
    kernel = ctypes.windll.kernel32
    kernel.VirtualAlloc.restype = ctypes.c_void_p
    kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_ulong, ctypes.c_ulong]
    tam = MB * 1024 * 1024
    ptr = kernel.VirtualAlloc(None, tam, 0x1000 | 0x2000, 0x04)
    ctypes.memset(ptr, 1, tam)          # fallar todas las paginas

    def retocar():
        t0 = time.perf_counter()
        total = 0
        for off in range(0, tam, 4096):
            total += ctypes.c_ubyte.from_address(ptr + off).value
        dt = (time.perf_counter() - t0) * 1000.0
        if total == -1:
            print(total)
        return dt

    # Ordenes por stdin: "r" = retocar y reportar, "q" = salir
    print(json.dumps({"listo": True}), flush=True)
    for linea in sys.stdin:
        c = linea.strip()
        if c == "r":
            print(json.dumps({"t": retocar()}), flush=True)
        elif c == "q":
            break
""" % MB)


def lanzar():
    ruta = os.path.join(os.environ.get("TEMP", "."), "_optimem_trim_hijo.py")
    with open(ruta, "w") as f:
        f.write(HIJO)
    p = subprocess.Popen([sys.executable, ruta], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1)
    p.stdout.readline()
    return p, ruta


def retocar(p):
    import json
    p.stdin.write("r\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())["t"]


def correr_tanda(presion_mb, etiqueta):
    print(f"\n--- {etiqueta} ---")
    p, ruta = lanzar()
    time.sleep(1.0)

    residente = [retocar(p) for _ in range(3)]
    prom_res = sum(residente) / len(residente)
    print(f"  re-tocar con paginas residentes : {prom_res:8.2f} ms")

    ok, liberado = winapi.trimear(p.pid)
    print(f"  trim -> {ok}, libero {liberado / 1024**2:.0f} MB")
    time.sleep(1.0)

    desalojado = [retocar(p) for _ in range(3)]
    prom_des = sum(desalojado) / len(desalojado)
    print(f"  re-tocar tras el trim           : {prom_des:8.2f} ms")

    factor = prom_des / prom_res if prom_res > 0 else 0
    print(f"  >>> el trim multiplico el costo por {factor:.2f}x"
          f"  (costo {prom_des - prom_res:+.2f} ms cada {MB} MB)")

    p.stdin.write("q\n")
    p.stdin.flush()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    os.remove(ruta)
    return factor, prom_res, prom_des


print("=" * 78)
print("Cuanto cuesta realmente vaciar un working set")
print("=" * 78)

# Sonda viva para ver si el trim tambien ensucia la cache del sistema.
sonda = probe.SondaViva(config.cargar())
sonda.medir()
time.sleep(0.5)
c0 = sonda.medir()["cache"]
print(f"\nLectura de cache antes de todo: {c0:.1f} ms")

f1, r1, d1 = correr_tanda(0, "Sin presion de fondo")

c1 = sonda.medir()["cache"]
print(f"  lectura de cache despues del trim: {c1:.1f} ms "
      f"({(c1 - c0) / c0 * 100:+.0f}%)")

print("\n" + "=" * 78)
print("VEREDICTO")
print("=" * 78)
if f1 > 1.5:
    print(f"  El trim tiene un costo real y medible: {f1:.2f}x mas lento al re-tocar.")
    print("  Entonces SI importa elegir a quien trimear, y el modelo tiene")
    print("  algo concreto que predecir: cuando ese costo se va a pagar.")
    print(f"\n  Traducido: vaciar {MB} MB que estaban en uso cuesta "
          f"{d1 - r1:.1f} ms de re-lectura.")
else:
    print(f"  El trim casi no costo nada ({f1:.2f}x).")
    print("  Si el sistema cumple, no hay nada que optimizar y el modelo sobra.")
    print("  Habria que replantear el enfoque hacia el problema real.")

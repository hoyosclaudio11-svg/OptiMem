"""
Sensibilidad de la sonda REDISEÑADA a la presion de memoria.

La version 1 de la sonda fallo este test: midio que asignar memoria nueva NO
se degrada con la presion (los fallos de demanda cero se sirven desde la lista
de paginas libres, sin tocar el disco).

La version 2 mide dos cosas distintas:
  cache  leer un archivo que deberia seguir en cache del sistema
  ws     re-tocar nuestro propio working set ya fallado
Las dos se degradan si Windows desaloja paginas para hacer lugar.

Metodo: linea de base -> 3 GB de presion -> recuperacion.
"""
import os
import subprocess
import sys
from pathlib import Path
import textwrap
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimem import config, probe, winapi

cfg = config.cargar()
MB = 3072


def tanda(sonda, n, etiqueta):
    acc = {}
    for _ in range(n):
        r = sonda.medir()
        for k in ("cache", "ws", "cpu", "memoria"):
            acc.setdefault(k, []).append(r.get(k, -1))
    m = winapi.memoria_sistema()
    prom = {k: sum(v) / len(v) for k, v in acc.items()}
    print(f"  {etiqueta:<16} cache {prom['cache']:7.1f}  ws {prom['ws']:7.1f}  "
          f"cpu {prom['cpu']:6.0f}  mem {prom['memoria']:6.1f} ms  "
          f"| libre {m.disponible/1024**3:5.2f} GB")
    return prom


print("=" * 78)
print("Sensibilidad de la sonda v2")
print("=" * 78)

# La sonda VIVA: mantiene su buffer entre mediciones. Es la diferencia clave
# con la v1 -> si el proceso renace, su working set siempre esta recien
# fallado y nunca se puede detectar que se lo llevaron.
sonda = probe.SondaViva(cfg)
print(f"\nSonda viva: buffer de {cfg.sonda_mb_memoria} MB reservado y fallado.")
print(f"Archivo de cache: {os.path.basename(sonda.ruta_cache)} "
      f"({probe.MB_CACHE} MB)\n")

# Calentamos la cache: la primera lectura siempre va a disco.
sonda.medir()
time.sleep(1)

print("Tanda 1 - linea de base:")
base = tanda(sonda, 3, "sin presion")

print(f"\nCreando {MB/1024:.1f} GB de presion...")
hijo = textwrap.dedent(f"""
    import time
    buf = bytearray({MB} * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    print("LISTO", flush=True)
    time.sleep(180)
""")
ruta = os.path.join(os.environ.get("TEMP", "."), "_optimem_presion2.py")
with open(ruta, "w") as f:
    f.write(hijo)
proc = subprocess.Popen([sys.executable, ruta], stdout=subprocess.PIPE, text=True)
proc.stdout.readline()
time.sleep(4)

print("\nTanda 2 - CON presion:")
pres = tanda(sonda, 3, "con presion")

print("\nLiberando...")
proc.kill()
proc.wait()
os.remove(ruta)
time.sleep(6)

print("\nTanda 3 - despues de liberar:")
post = tanda(sonda, 3, "recuperado")

print("\n" + "=" * 78)
print("VEREDICTO")
print("=" * 78)
ok_alguno = False
for k, nombre in (("cache", "cache"), ("ws", "working set"),
                  ("cpu", "cpu"), ("memoria", "memoria")):
    b, p, q = base[k], pres[k], post[k]
    if b <= 0:
        continue
    subida = (p - b) / b * 100
    vuelta = (q - b) / b * 100
    marca = "  <-- RESPONDE" if subida > 40 else ""
    print(f"  {nombre:<12} {b:9.2f} -> {p:9.2f} -> {q:9.2f} ms"
          f"   sube {subida:+8.1f}%  vuelve {vuelta:+7.1f}%{marca}")
    if subida > 40:
        ok_alguno = True

print()
if ok_alguno:
    print("  La sonda v2 SI responde a la presion de memoria.")
    print("  La metrica del spec ahora mide lo que dice medir.")
else:
    print("  Ninguna medicion respondio. Hay que replantear la metrica.")

# El indice compuesto con los pesos reales.
base_idx = {k: base[k] for k in ("cache", "ws", "cpu", "memoria")}
i_base = probe.calcular_indice(base, base_idx)
i_pres = probe.calcular_indice(pres, base_idx)
i_post = probe.calcular_indice(post, base_idx)
print(f"\n  INDICE compuesto (1.00 = linea de base):")
print(f"    sin presion {i_base:.3f}   con presion {i_pres:.3f}   recuperado {i_post:.3f}")
print(f"    el indice se movio {(i_pres - i_base) / i_base * 100:+.0f}% con la presion")

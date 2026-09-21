"""
Por que el umbral no puede quedar por debajo de `umbral_seguridad`.

El agente bloquea todo trim cuya P(thrash) supere el umbral del modelo, asi que
el umbral no es un detalle de calibracion: define cuantas acciones se hacen. Por
debajo de `umbral_seguridad` (0,35) la rama de prioridad -la que solo le pide a
Windows que prefiera ese proceso para desalojo, en vez de vaciarle el working
set- queda inalcanzable: todo lo que llegaria ahi ya se bloqueo antes. La zona
gris de tres niveles queda como codigo muerto, y el agente se vuelve binario sin
que nadie lo haya decidido.

Este script imprime, para el modelo activo y su test temporal, la probabilidad
que asigna a cada muestra y que pasaria con cada umbral de la grilla.

Dos cosas que ya se aprendieron con datos reales del 21/09/2026:

1. **El desempate importa.** Con las primeras 125 etiquetas la separacion era
   amplia (malos >= 0,698, buenos <= 0,388), asi que cualquier umbral entre 0,40
   y 0,69 daba la misma matriz. El desempate viejo -quedarse con el primer umbral
   que alcanzaba el recall maximo- elegia 0,05 y sacrificaba la mitad de los
   trims buenos sin ganar un solo bloqueo.
2. **El umbral elegido puede matar la zona gris, y a veces esta bien.** El
   criterio prioriza recall, asi que si conviene puede elegir un umbral por
   debajo de `umbral_seguridad` y la rama de prioridad no se alcanza nunca: el
   agente queda binario (trimea o bloquea). Con las etiquetas que llegaron
   despues, el criterio elige 0,30 y la zona gris muere. Forzarlo a la zona gris
   no es gratis: el trim que ese umbral bloquea y el otro deja pasar, con
   P = 0,31, **si causo thrash**. La tension es real y no se resuelve con una
   linea de codigo; queda anotada.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimem import config as cfgmod
from optimem import db, features, trainer

cfg = cfgmod.cargar()
con = db.inicializar(cfg.ruta_db)

mod = trainer.cargar_activo(cfg, con)
if mod is None:
    print("No hay modelo activo. Entrena uno con `python optimem.py entrenar`.")
    sys.exit(1)

X, y, _ = trainer.construir_dataset(con, cfg)
if len(y) < cfg.min_muestras_entrenar:
    print(f"Hay {len(y)} muestras etiquetadas y se necesitan "
          f"{cfg.min_muestras_entrenar}. Deja el recolector corriendo.")
    sys.exit(1)

# El mismo corte por tiempo que usa el entrenador: entrena con el pasado,
# evalua con el futuro.
corte = int(len(y) * (1 - cfg.test_split))
corte = max(1, min(corte, len(y) - 1))
y_te = y[corte:]

print("OptiMem - umbral, zona gris y trims que se sacrifican")
print("=" * 62)
print(f"  modelo : {mod['version']} ({mod['algoritmo']}) "
      f"umbral guardado {mod['umbral']:.2f}")
print(f"  test   : {len(y_te)} muestras, {int(y_te.sum())} malas "
      f"({int((y_te == 0).sum())} buenas)")
print(f"  zona gris: prioridad para P entre {cfg.umbral_seguridad:.2f} y "
      f"{cfg.umbral_prioridad:.2f}")

prob = mod["modelo"].predict_proba(X[corte:])[:, 1]

print("\n  Probabilidad que asigna a cada muestra del test:")
for p, real in sorted(zip(prob, y_te), reverse=True):
    print(f"    P(thrash)={p:.3f}   {'MALO (causo thrash)' if real else 'bueno'}")

print("\n  Que pasaria con cada umbral (bloquea si P >= umbral):")
print(f"    {'umbral':>7} {'bloquea':>8} {'deja pasar':>11} "
      f"{'malos que se cuelan':>20} {'zona gris':>10}")
for u in [0.05, 0.1, 0.2, 0.3, 0.35, 0.4, 0.5, 0.6, 0.7]:
    bloquea = int((prob >= u).sum())
    pasan = int((prob < u).sum())
    malos_pasan = int(((prob < u) & (y_te == 1)).sum())
    # La zona gris es inalcanzable si el umbral bloquea todo lo que cae entre
    # umbral_seguridad y umbral_prioridad: nunca se llega a esa rama.
    alcanzable = "si" if cfg.umbral_seguridad < u <= cfg.umbral_prioridad else "NO"
    print(f"    {u:>7.2f} {bloquea:>8} {pasan:>11} {malos_pasan:>20} {alcanzable:>10}")

umbral, _ = trainer._umbral_optimo(y_te, prob)
print(f"\n  El criterio del entrenador elige: {umbral:.2f}")
if umbral <= cfg.umbral_seguridad:
    print(f"  Con ese umbral la rama de prioridad no se alcanza nunca: todo lo")
    print(f"  que llegaria a la zona gris queda bloqueado antes. El agente pasa a")
    print(f"  ser binario (trimea o bloquea), sin usar la palanca suave.")
    print(f"  El criterio prioriza recall, y bajar el umbral a {umbral:.2f} evita")
    print(f"  los trims malos que la zona gris dejaria pasar. No es un error de")
    print(f"  calculo: es la tension entre las dos cosas, y hay que decidirla.")
else:
    print(f"  La zona gris queda usable: P entre {cfg.umbral_seguridad:.2f} y "
          f"{umbral:.2f} baja prioridad en vez de trimear.")
print(f"\n  Un umbral mas alto bloquea menos y arriesga mas thrash; uno mas bajo")
print(f"  bloquea mas y deja pasar menos trims que estaban bien. El recall solo")
print(f"  no alcanza para elegir: la precision en el empate tambien cuenta.")

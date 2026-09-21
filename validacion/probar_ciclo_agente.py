"""
Que haria el agente en este momento, sin que lo haga.

Arma el mismo ciclo que el recolector corre cada 30 s -presion, candidatos,
features, prediccion- sobre la ultima foto real de procesos, y muestra la
decision que saldria para cada uno. Es de SOLO LECTURA: no trimea nada.

Existe porque el agente puede quedarse mudo sin estar roto, y desde afuera no
se distingue "no hay nada que hacer" de "algo se rompio". Los tres motivos de
silencio, y ninguno deja error visible:

  1. No hay presion -> no toca nada y no registra eventos. Es lo correcto.
  2. El presupuesto de MB de la hora se agoto. Ojo con este: el presupuesto lo
     comparten el agente y los trims exploratorios del modo aprendizaje, y un
     candidato que no entra en lo que queda se saltea sin dejar rastro. Con los
     1.500 MB de la hora ya gastados, el agente parece apagado por hasta una
     hora.
  3. El modelo bloquea a todos los candidatos -> quedan eventos
     `decision_frenada`, pero ningun trim y ninguna linea de log.

Los tres se ven juntos aca, con los numeros.

Que NO prueba: usa la ultima foto de la base, no el estado en memoria del
recolector, asi que sirve para ver el criterio del modelo, no para auditarlo.
Lo que el recolector decidio de verdad queda en `acciones` y `eventos`.
"""
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimem import agent as _agente
from optimem import config as cfgmod
from optimem import db, features, trainer, winapi

cfg = cfgmod.cargar()
con = db.inicializar(cfg.ruta_db)

print("OptiMem - que haria el agente ahora")
print("=" * 62)

mod = trainer.cargar_activo(cfg, con)
if mod is None:
    print("  No hay modelo activo: entrena uno con `python optimem.py entrenar`.")
    print("  (En modo aprendizaje el agente igual trimea, pero a ciegas.)")
    sys.exit(1)
print(f"  modelo      : {mod['version']} ({mod['algoritmo']}) "
      f"umbral {mod['umbral']:.2f}")

# Ultima foto de procesos que quedo en la base.
t0 = con.execute("SELECT MAX(ts) m FROM muestras_proceso").fetchone()["m"]
if t0 is None:
    print("  No hay muestras de procesos todavia.")
    sys.exit(1)
print(f"  foto        : hace {time.time() - t0:.0f} s")
filas = con.execute("SELECT * FROM muestras_proceso WHERE ts=?", (t0,)).fetchall()

# Los crudos se arman con las mismas claves que usa el recolector.
crudos = []
for f in filas:
    try:
        crear = float(str(f["clave"]).split(":")[1])
    except (IndexError, ValueError):
        crear = 0.0
    crudos.append({
        "clave": f["clave"], "pid": f["pid"], "nombre": f["nombre"] or "",
        "ws": f["ws"], "cpu_pct": f["cpu_pct"], "fallos": f["fallos_pagina"],
        "create_time": crear, "critico": bool(f["critico"]),
        "motivo": f["motivo"], "categoria": f["categoria"],
        "usuario": None, "cmdline": None, "es_propio": False,
    })
print(f"  procesos    : {len(crudos)} en la foto")

# Historial por proceso: lo mismo que guarda el agente en memoria, pero con los
# ts de la base en lugar de la hora de ahora.
ag = _agente.Agente(cfg, con, modelo=mod)
for c in crudos:
    h = con.execute(
        "SELECT ts, ws, fallos_pagina, cpu_pct FROM muestras_proceso "
        "WHERE clave=? ORDER BY ts DESC LIMIT 40", (c["clave"],)
    ).fetchall()
    ag.historial[c["clave"]] = deque((dict(x) for x in h), maxlen=40)
fila_sis = con.execute(
    "SELECT * FROM muestras_sistema ORDER BY ts DESC LIMIT 1").fetchone()
if fila_sis is None:
    print("  No hay muestras de sistema: no se pueden armar las features.")
    sys.exit(1)
ag.ultimo_sistema = dict(fila_sis)

m = winapi.memoria_sistema()
presion, motivo = ag._hay_presion(m)
print(f"  presion     : {'SI' if presion else 'NO'} - {motivo}")

acciones_libres, mb_libres = ag._presupuesto()
print(f"  presupuesto : {acciones_libres} acciones y {mb_libres} MB en la hora")

if not presion:
    print("\n  Sin presion no toca nada, y no registra nada. Es lo correcto.")
    sys.exit(0)

cands = ag._candidatos(crudos)
print(f"  candidatos  : {len(cands)} (>= {cfg.ws_minimo_mb} MB, sin cooldown)")
if not cands:
    print("\n  Ningun candidato cumple los minimos. El agente no actua.")
    sys.exit(0)

print(f"\n  {'proceso':<26} {'ws':>9}  {'P(thrash)':>9}  decision")
print(f"  {'-' * 26} {'-' * 9}  {'-' * 9}  {'-' * 22}")

cuenta = {"BLOQUEA": 0, "trim": 0, "prioridad": 0, "sin_presupuesto": 0,
          "sin_contexto": 0, "nada": 0}
for c in cands:
    nombre = (c["nombre"] or "")[:26]
    ws_mb = (c["ws"] or 0) / 1024**2
    if ws_mb > mb_libres:
        # Este es el salto silencioso: no se registra en ningun lado.
        cuenta["sin_presupuesto"] += 1
        print(f"  {nombre:<26} {ws_mb:>7.1f} MB  {'-':>9}  "
              f"salteado: no entra en {mb_libres} MB")
        continue
    ctx = ag._contexto(c)
    if ctx is None:
        cuenta["sin_contexto"] += 1
        print(f"  {nombre:<26} {ws_mb:>7.1f} MB  {'-':>9}  sin contexto")
        continue
    p = ag._predecir(ctx)
    if p is None:
        cuenta["nada"] += 1
        print(f"  {nombre:<26} {ws_mb:>7.1f} MB  {'-':>9}  no pudo predecir")
        continue
    if p >= mod["umbral"]:
        decision = f"BLOQUEA (>= {mod['umbral']:.2f})"
    elif p <= cfg.umbral_seguridad:
        decision = f"trim (<= {cfg.umbral_seguridad:.2f})"
    elif p <= cfg.umbral_prioridad:
        decision = "prioridad (zona gris)"
    else:
        decision = "nada (entre umbral y prioridad)"
    cuenta[decision.split()[0]] = cuenta.get(decision.split()[0], 0) + 1
    print(f"  {nombre:<26} {ws_mb:>7.1f} MB  {p:>9.3f}  {decision}")

print(f"\n  resumen: {cuenta['BLOQUEA']} bloqueados, {cuenta['trim']} trims, "
      f"{cuenta['prioridad']} prioridad, {cuenta['sin_presupuesto']} sin presupuesto")

# El diagnostico que importa cuando parece que no hace nada.
if cuenta["sin_presupuesto"] == len(cands):
    print(f"\n  El agente no puede hacer NADA con {acciones_libres} acciones libres:")
    print(f"  el candidato mas chico pesa mas que los {mb_libres} MB que quedan en")
    print(f"  la hora. Se destraba solo cuando los trims viejos salen de la ventana")
    print(f"  de una hora, o subiendo max_liberado_por_hora_mb en config.json.")
elif cuenta["BLOQUEA"] == len(cands) - cuenta["sin_presupuesto"]:
    print(f"\n  El modelo bloquea a todos: no hay nada que trimear sin costo.")
    print(f"  Queda el evento `decision_frenada` en la base como constancia.")

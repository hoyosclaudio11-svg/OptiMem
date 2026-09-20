"""Volcado del estado de la base, para ver que hay realmente guardado."""
import os
import sqlite3

con = sqlite3.connect(os.path.expandvars(r"%LOCALAPPDATA%\OptiMem\optimem.db"))
con.row_factory = sqlite3.Row

print("=== ACCIONES ===")
for r in con.execute("SELECT * FROM acciones ORDER BY ts"):
    print(f"  id={r['id']} {r['nombre'][:26]:<26} libero={r['liberado']/1024**2:6.1f} MB "
          f"autor={r['autor']} p_thrash={r['p_thrash']}")

print("\n=== RESULTADOS (etiquetas) ===")
for r in con.execute("SELECT * FROM resultados ORDER BY accion_id"):
    print(f"  accion={r['accion_id']}  etiqueta={r['etiqueta']}")
    print(f"     base={r['fallos_base']}  post={r['fallos_post']}  "
          f"reincidencia={r['reincidencia']}")
    print(f"     {r['motivo']}")

print("\n=== SONDAS ===")
n = con.execute("SELECT COUNT(*) c FROM sondas").fetchone()["c"]
print(f"  {n} sondas guardadas")
for r in con.execute("SELECT * FROM sondas ORDER BY ts LIMIT 4"):
    print(f"  cache={r['t_cache_ms']}  ws={r['t_ws_ms']}  cpu={r['t_cpu_ms']}  "
          f"mem={r['t_memoria_ms']}  indice={r['indice']}")

print("\n=== MUESTRAS ===")
for t in ("muestras_sistema", "muestras_proceso"):
    f = con.execute(f"SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b FROM {t}").fetchone()
    dur = (f["b"] - f["a"]) / 60 if f["b"] and f["a"] else 0
    print(f"  {t:<20} {f['c']:>8,} filas en {dur:.1f} min")

print("\n=== PROCESOS CANDIDATOS (ultima muestra) ===")
ult = con.execute("SELECT MAX(ts) t FROM muestras_proceso").fetchone()["t"]
for r in con.execute(
    "SELECT nombre, ws, categoria, critico, motivo FROM muestras_proceso "
    "WHERE ts=? AND ws > 200*1024*1024 ORDER BY ws DESC LIMIT 10", (ult,)
):
    print(f"  {r['nombre'][:28]:<28} {r['ws']/1024**2:7.0f} MB  {r['categoria']:<10} "
          f"critico={r['critico']}")

print("\n=== EVENTOS ===")
for r in con.execute("SELECT tipo, COUNT(*) c FROM eventos GROUP BY tipo ORDER BY c DESC"):
    print(f"  {r['tipo']:<20} {r['c']}")
for r in con.execute(
    "SELECT ts, tipo, severidad, nombre, detalle FROM eventos "
    "WHERE severidad!='info' ORDER BY ts DESC LIMIT 6"
):
    print(f"  [{r['severidad']}] {r['tipo']} {r['nombre'] or ''} :: {(r['detalle'] or '')[:110]}")

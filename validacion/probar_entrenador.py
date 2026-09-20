"""
Prueba del entrenador, que es la unica pieza que no se puede ejercitar con
15 minutos de datos: hacen falta 60 muestras etiquetadas y la exploracion
junta ~4 por hora.

QUE PRUEBA ESTO Y QUE NO, porque es importante no confundirse:

  SI prueba que el circuito completo funciona: armar el dataset desde la base,
  calcular las features, partir por tiempo, entrenar, elegir umbral, guardar
  el modelo y que el agente despues lo pueda cargar y usar.

  NO prueba que el modelo sirva. Las acciones que inserta son INVENTADAS:
  nadie trimeo esos procesos, asi que las etiquetas salen de series de fallos
  que no reflejan ningun trim. Sirve como prueba de cañeria, no como
  validacion.

Se corre sobre una copia de la base para no ensuciar los datos reales.
"""
import os
import random
import shutil
import sqlite3
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optimem import config as cfgmod

REAL = os.path.expandvars(r"%LOCALAPPDATA%\OptiMem")
COPIA = os.path.join(os.environ.get("TEMP", "."), "_optimem_prueba")
if os.path.exists(COPIA):
    shutil.rmtree(COPIA)
os.makedirs(COPIA)
for f in os.listdir(REAL):
    if f.startswith("optimem.db"):
        shutil.copy(os.path.join(REAL, f), os.path.join(COPIA, f))

cfg = cfgmod.cargar()
cfg.directorio_datos = COPIA
con = sqlite3.connect(cfg.ruta_db)
con.row_factory = sqlite3.Row

# --- acciones sinteticas sobre procesos y momentos reales ---
print("Insertando acciones sinteticas para ejercitar el entrenador...")
candidatos = con.execute(
    "SELECT clave, pid, nombre, COUNT(*) n FROM muestras_proceso "
    "WHERE ws > 150*1024*1024 AND categoria IN ('libre','vigilado') "
    "GROUP BY clave HAVING n >= 8 ORDER BY MAX(ws) DESC LIMIT 30"
).fetchall()
print(f"  {len(candidatos)} procesos candidatos con historia suficiente")

rnd = random.Random(7)
ins = 0
for c in candidatos:
    filas = con.execute(
        "SELECT ts, ws FROM muestras_proceso WHERE clave=? ORDER BY ts", (c["clave"],)
    ).fetchall()
    # Elegimos momentos que dejen ventana de evaluacion hacia adelante.
    for f in filas[::max(1, len(filas) // 6)][:6]:
        ts = f["ts"]
        hay_futuro = con.execute(
            "SELECT COUNT(*) n FROM muestras_proceso WHERE clave=? AND ts > ? AND ts <= ?",
            (c["clave"], ts, ts + cfg.ventana_evaluacion_seg),
        ).fetchone()["n"]
        if hay_futuro < 3:
            continue
        cur = con.execute(
            "INSERT INTO acciones (ts, clave, pid, nombre, tipo, ws_antes, "
            "ws_despues, liberado, autor, modelo_id, p_thrash) "
            "VALUES (?,?,?,?,'trim',?,?,?,'exploracion',NULL,NULL)",
            (ts, c["clave"], c["pid"], c["nombre"], f["ws"],
             int(f["ws"] * 0.5), int(f["ws"] * 0.5)),
        )
        ins += 1
con.commit()
print(f"  {ins} acciones insertadas\n")

# --- evaluar (aca se calculan las etiquetas con datos REALES de fallos) ---
from optimem import evaluator

n = evaluator.cerrar_acciones(con, cfg)
e = evaluator.estadisticas(con)
print(f"Evaluadas: {n}")
print(f"  con etiqueta : {e['listas_para_entrenar']} "
      f"(malas {e['malas']}, buenas {e['buenas']})")
print(f"  descartadas  : {e['descartadas']}")

# --- entrenar ---
from optimem import trainer

print("\nEntrenando...")
inf = trainer.entrenar(cfg, con)
print(f"  apto: {inf['apto']}")
if not inf["apto"]:
    print(f"  motivo: {inf['motivo']}")
    sys.exit(1 if inf["n_positivos"] == 0 else 0)

print(f"  algoritmo : {inf['algoritmo']}")
print(f"  train/test: {inf['n_train']}/{inf['n_test']} "
      f"(positivos {inf['positivos_train']}/{inf['positivos_test']})")
for nombre, m in inf["candidatos"].items():
    print(f"    {nombre:<20} AUC {m.get('auc')}  F1 {m['f1']:.3f}  "
          f"P {m['precision']:.3f}  R {m['recall']:.3f}")
print(f"  umbral elegido: {inf['umbral']:.2f}")
lb = inf["linea_base"]
print(f"  regla tonta acertaria: {lb['regla_trimear_siempre_accuracy']*100:.1f}%")
print(f"  malos evitados: {lb['malos_evitados']}/{lb['malos_en_test']}")
print(f"  modelo guardado en: {inf['ruta']}")

# --- el agente puede cargarlo? ---
print("\nProbando que el agente cargue el modelo...")
from optimem import agent, features

mod = trainer.cargar_activo(cfg, con)
print(f"  modelo activo: {mod['version'] if mod else None} "
      f"({mod['algoritmo'] if mod else '-'})")
if mod:
    ag = agent.Agente(cfg, con, modelo=mod)
    fila = con.execute(
        "SELECT * FROM muestras_sistema ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    print(f"  features del modelo: {len(mod['features'])}")
    print(f"  coincide el orden con el codigo: {mod['features'] == features.FEATURES}")
    # Una prediccion suelta con un contexto real.
    ctx = None
    for c in candidatos[:5]:
        h = con.execute(
            "SELECT * FROM muestras_proceso WHERE clave=? ORDER BY ts DESC LIMIT 20",
            (c["clave"],),
        ).fetchall()
        if not h:
            continue
        ctx = features.contexto_desde_db(con, cfg, c["clave"], h[0]["ts"], historial=h)
        if ctx:
            p = ag._predecir(ctx)
            print(f"  prediccion sobre {c['nombre'][:24]:<24} -> P(thrash)={p:.3f}")
            break
    if ctx is None:
        print("  no se pudo armar un contexto para predecir")

print("\nPrueba del entrenador terminada.")

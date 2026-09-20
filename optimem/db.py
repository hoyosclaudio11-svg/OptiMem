"""
Base de datos SQLite de OptiMem.

Esquema pensado alrededor de una idea: toda accion que el agente tome tiene
que poder evaluarse despues. Sin eso no hay etiquetas, y sin etiquetas no hay
modelo. Las tablas `acciones` y `resultados` son el corazón del aprendizaje,
no un log decorativo.

Se usa WAL para que el panel pueda leer mientras el recolector escribe.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ESQUEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- Memoria global, una fila por segundo.
-- Los contadores *_pag son acumulados desde el arranque: importan sus deltas.
CREATE TABLE IF NOT EXISTS muestras_sistema (
    ts              REAL PRIMARY KEY,
    ram_total       INTEGER,
    ram_disponible  INTEGER,
    ram_en_uso      INTEGER,
    commit_total    INTEGER,
    commit_limite   INTEGER,
    cache_sistema   INTEGER,
    pool_paginado   INTEGER,
    pool_no_paginado INTEGER,
    fallos_pagina   INTEGER,
    lecturas_pagina INTEGER,   -- paginas traidas de DISCO = swapping duro
    io_lecturas     INTEGER,
    procesos        INTEGER,
    hilos           INTEGER,
    cpu_global      REAL,
    disco_lect_bps  REAL,
    disco_esc_bps   REAL
);
CREATE INDEX IF NOT EXISTS idx_ms_ts ON muestras_sistema(ts);

-- Foto por proceso. `clave` = "pid:create_time" para sobrevivir a la
-- reutilizacion de PIDs, que si no contamina las series en silencio.
CREATE TABLE IF NOT EXISTS muestras_proceso (
    ts            REAL NOT NULL,
    clave         TEXT NOT NULL,
    pid           INTEGER,
    nombre        TEXT,
    ws            INTEGER,
    ws_pico       INTEGER,
    -- "commit" es palabra reservada de SQL: la columna lleva sufijo.
    commit_bytes  INTEGER,
    fallos_pagina INTEGER,
    cpu_pct       REAL,
    hilos         INTEGER,
    prioridad_mem TEXT,
    critico       INTEGER DEFAULT 0,
    motivo        TEXT,
    categoria     TEXT,
    en_foco       INTEGER DEFAULT 0,
    PRIMARY KEY (ts, clave)
);
CREATE INDEX IF NOT EXISTS idx_mp_clave ON muestras_proceso(clave, ts);
CREATE INDEX IF NOT EXISTS idx_mp_ts ON muestras_proceso(ts);

-- Cada vez que OptiMem toca algo. autor: agente | exploracion | manual
CREATE TABLE IF NOT EXISTS acciones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    clave         TEXT NOT NULL,
    pid           INTEGER,
    nombre        TEXT,
    tipo          TEXT NOT NULL,     -- trim | prioridad
    nivel         TEXT,              -- nivel de prioridad, si aplica
    ws_antes      INTEGER,
    ws_despues    INTEGER,
    liberado      INTEGER DEFAULT 0,
    fallos_antes  INTEGER,
    modelo_id     INTEGER,           -- que modelo decidio
    p_thrash      REAL,              -- probabilidad que estimo el modelo
    autor         TEXT,
    notas         TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_ts ON acciones(ts);
CREATE INDEX IF NOT EXISTS idx_acc_clave ON acciones(clave, ts);

-- El resultado de cada accion, medido. Aca vive la etiqueta del modelo.
CREATE TABLE IF NOT EXISTS resultados (
    accion_id       INTEGER PRIMARY KEY REFERENCES acciones(id) ON DELETE CASCADE,
    ts_eval         REAL,
    segundos        REAL,
    fallos_base     INTEGER,     -- fallos/min ANTES del trim
    fallos_post     INTEGER,     -- fallos/min DESPUES (salteando el pico)
    reincidencia    REAL,        -- post / base
    delta_ws        INTEGER,     -- cuanto volvio a crecer el working set
    etiqueta        INTEGER,     -- 1 = el trim fue un error (thrash)
    motivo          TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_etiqueta ON resultados(etiqueta);

-- La sonda sintetica: la metrica de exito del proyecto.
--
-- t_cache_ms y t_ws_ms son las que importan. Miden si el sistema le robo la
-- cache y el working set a una aplicacion, que es lo que el usuario siente.
-- t_disco_ms quedo en desuso: medía con FILE_FLAG_NO_BUFFERING, o sea
-- salteando la cache, que es justamente la victima de la presion.
CREATE TABLE IF NOT EXISTS sondas (
    ts          REAL PRIMARY KEY,
    t_memoria_ms REAL,     -- latencia de fallar memoria nueva (poco sensible)
    t_cpu_ms    REAL,      -- contencion del planificador
    t_disco_ms  REAL,      -- en desuso, se deja por los datos ya guardados
    indice      REAL,      -- indice compuesto: la "respuesta del sistema"
    ram_disp    INTEGER,
    commit_pct  REAL,
    detalle     TEXT,
    t_cache_ms  REAL,      -- leer un archivo que deberia seguir en cache
    t_ws_ms     REAL       -- re-tocar nuestro propio working set
);
CREATE INDEX IF NOT EXISTS idx_sondas_ts ON sondas(ts);

-- Eventos: muertes de procesos vigilados, cambios de estado, problemas.
CREATE TABLE IF NOT EXISTS eventos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    tipo      TEXT NOT NULL,      -- muerte_proceso | bloqueo_accion | error | inicio | fin
    severidad TEXT,               -- info | aviso | grave
    clave     TEXT,
    pid       INTEGER,
    nombre    TEXT,
    detalle   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON eventos(ts);

-- Modelos entrenados.
CREATE TABLE IF NOT EXISTS modelos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    version       TEXT,
    algoritmo     TEXT,
    ruta          TEXT,
    n_muestras    INTEGER,
    n_positivos   INTEGER,
    metricas      TEXT,     -- JSON: auc, precision, recall, f1, matriz
    features      TEXT,     -- JSON: lista de features en orden
    umbral        REAL,
    activo        INTEGER DEFAULT 0
);

-- Estado interno persistente (contadores de limites por hora, etc.)
CREATE TABLE IF NOT EXISTS estado (
    clave TEXT PRIMARY KEY,
    valor TEXT,
    ts    REAL
);
"""


def conectar(ruta: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(ruta), timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def inicializar(ruta: Path | str) -> sqlite3.Connection:
    con = conectar(ruta)
    con.executescript(ESQUEMA)
    _migrar(con)
    return con


# Columnas agregadas despues de la primera version. CREATE TABLE IF NOT EXISTS
# no las agrega sobre una base que ya existe, asi que hay que hacerlo a mano.
# Sin esto, una base vieja hace fallar los INSERT con un error que no dice
# nada util sobre la causa real.
_MIGRACIONES = [
    ("sondas", "t_cache_ms", "REAL"),
    ("sondas", "t_ws_ms", "REAL"),
    ("muestras_proceso", "categoria", "TEXT"),
]


def _migrar(con) -> None:
    for tabla, columna, tipo in _MIGRACIONES:
        try:
            cols = {f["name"] for f in con.execute(f"PRAGMA table_info({tabla})")}
        except sqlite3.Error:
            continue
        if not cols:
            continue
        if columna not in cols:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


# ---------------------------------------------------------------------------
# Helpers de escritura
# ---------------------------------------------------------------------------
def registrar_evento(con, tipo: str, severidad: str = "info", *,
                     clave: str | None = None, pid: int | None = None,
                     nombre: str | None = None, detalle: str | None = None) -> None:
    con.execute(
        "INSERT INTO eventos (ts, tipo, severidad, clave, pid, nombre, detalle) "
        "VALUES (?,?,?,?,?,?,?)",
        (time.time(), tipo, severidad, clave, pid, nombre, detalle),
    )


def registrar_accion(con, *, clave: str, pid: int, nombre: str, tipo: str,
                     nivel: str | None = None, ws_antes: int | None = None,
                     ws_despues: int | None = None, liberado: int = 0,
                     fallos_antes: int | None = None, modelo_id: int | None = None,
                     p_thrash: float | None = None, autor: str = "agente",
                     notas: str | None = None) -> int:
    cur = con.execute(
        "INSERT INTO acciones (ts, clave, pid, nombre, tipo, nivel, ws_antes, "
        "ws_despues, liberado, fallos_antes, modelo_id, p_thrash, autor, notas) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), clave, pid, nombre, tipo, nivel, ws_antes, ws_despues,
         liberado, fallos_antes, modelo_id, p_thrash, autor, notas),
    )
    return cur.lastrowid


def acciones_pendientes(con, ahora: float | None = None) -> list[sqlite3.Row]:
    """Acciones ya cumplieron su ventana y todavia no tienen resultado."""
    ahora = ahora or time.time()
    return con.execute(
        "SELECT * FROM acciones a LEFT JOIN resultados r ON r.accion_id = a.id "
        "WHERE r.accion_id IS NULL AND a.tipo = 'trim' ORDER BY a.ts"
    ).fetchall()


def guardar_estado(con, clave: str, valor) -> None:
    con.execute(
        "INSERT INTO estado (clave, valor, ts) VALUES (?,?,?) "
        "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, ts=excluded.ts",
        (clave, json.dumps(valor), time.time()),
    )


def leer_estado(con, clave: str, por_defecto=None):
    fila = con.execute("SELECT valor FROM estado WHERE clave=?", (clave,)).fetchone()
    if not fila:
        return por_defecto
    try:
        return json.loads(fila["valor"])
    except Exception:
        return por_defecto


def limpiar_viejos(con, dias: int) -> dict:
    """Borra muestras mas viejas que `dias`. Devuelve cuantas filas borro."""
    corte = time.time() - dias * 86400
    borrados = {}
    for tabla in ("muestras_sistema", "muestras_proceso", "sondas"):
        cur = con.execute(f"DELETE FROM {tabla} WHERE ts < ?", (corte,))
        borrados[tabla] = cur.rowcount
    cur = con.execute("DELETE FROM eventos WHERE ts < ? AND severidad = 'info'", (corte,))
    borrados["eventos"] = cur.rowcount
    return borrados

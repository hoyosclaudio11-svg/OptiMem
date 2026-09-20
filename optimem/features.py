"""
Ingenieria de features.

Un solo diccionario de contexto alimenta tanto al entrenamiento como al
agente en vivo. Esto es a proposito: si el entrenamiento arma las features de
una forma y el runtime de otra, el modelo funciona perfecto en el notebook y
predice basura en produccion. Ese error no da ningun sintoma visible, solo
decisiones malas.

La otra regla, igual de importante: TODA feature se calcula con datos
ANTERIORES O IGUALES al momento de la accion. Nada posterior. Si dejamos
entrar un solo dato del futuro, el modelo aprende a leer la respuesta y las
metricas salen espectaculares mientras las decisiones son inutiles.

Un contexto se arma distinto segun el origen:
  - `contexto_desde_db`   -> para entrenar (lee del historial)
  - `contexto_en_vivo`    -> para decidir (usa el historial en memoria)
Ambos devuelven las mismas claves, y `vector` las ordena siempre igual.
"""
from __future__ import annotations

import math
import time

# --- orden canonico de las features ---
# El modelo se guarda con esta lista. Si cambia el orden o se agrega una
# feature, los modelos viejos no se pueden usar: se descartan y se reentrena.
FEATURES = [
    "log_ws",              # tamano del working set
    "ws_ratio_ram",        # que fraccion de la RAM ocupa
    "ws_pico_ratio",       # ws actual / pico historico: <1 = ya libero antes
    "crecimiento_ws",      # cuanto crecio el ws en los ultimos minutos
    "fallos_por_min",      # fallos de pagina por minuto (linea de base del proceso)
    "fallos_por_mb_min",   # idem, normalizado por tamano
    "cpu_pct",             # CPU reciente
    "cpu_pct_max",         # pico de CPU reciente
    "cpu_por_mb",          # CPU por MB: frialdad
    "hilos",               # cantidad de hilos
    "edad_min",            # edad del proceso
    "commit_ratio",        # commit / ws: cuanto esta respaldado por pagefile
    "presion_ram",         # RAM en uso del sistema, 0..1
    "presion_commit",      # commit sobre el limite, 0..1
    "lecturas_disco_min",  # paginas leidas de disco por minuto (sistema)
    "cache_ratio",         # cache del sistema / RAM total
    "n_procesos",          # procesos vivos
    "io_lect_bps_log",     # I/O de lectura del sistema
    "categoria",           # 0 libre, 1 vigilado, 2 visible
    "hora_sin",
    "hora_cos",
]

NOMBRES_LEGIBLES = {
    "log_ws": "tamaño del working set",
    "ws_ratio_ram": "fracción de RAM ocupada",
    "ws_pico_ratio": "working set vs su pico",
    "crecimiento_ws": "crecimiento reciente del ws",
    "fallos_por_min": "fallos de página por minuto",
    "fallos_por_mb_min": "fallos por MB por minuto",
    "cpu_pct": "CPU reciente",
    "cpu_pct_max": "pico de CPU reciente",
    "cpu_por_mb": "CPU por MB (frialdad)",
    "hilos": "hilos",
    "edad_min": "edad del proceso",
    "commit_ratio": "comprometido sobre residente",
    "presion_ram": "presión de RAM del sistema",
    "presion_commit": "presión de commit",
    "lecturas_disco_min": "páginas leídas de disco por minuto",
    "cache_ratio": "caché del sistema sobre RAM",
    "n_procesos": "cantidad de procesos",
    "io_lect_bps_log": "I/O de lectura del sistema",
    "categoria": "categoría del proceso",
    "hora_sin": "hora del día (sin)",
    "hora_cos": "hora del día (cos)",
}

CATEGORIA_NUM = {"libre": 0, "vigilado": 1, "visible": 2, "protegido": 3,
                 "foco": 3, "nucleo": 3}


def vector(ctx: dict) -> list[float]:
    """Convierte un contexto en el vector de features, en orden canonico."""
    fila = []
    for f in FEATURES:
        v = ctx.get(f)
        if v is None:
            v = 0.0
        # Un NaN o un infinito hace explotar a sklearn con un error que no
        # dice de donde vino. Mejor sanear aca y que se note en el dato.
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            v = 0.0
        fila.append(float(v))
    return fila


def _hora_ciclica(ts: float) -> tuple[float, float]:
    h = time.localtime(ts).tm_hour + time.localtime(ts).tm_min / 60.0
    ang = 2 * math.pi * h / 24.0
    return math.sin(ang), math.cos(ang)


def _seguro(v, por_defecto=0.0) -> float:
    try:
        if v is None:
            return por_defecto
        return float(v)
    except (TypeError, ValueError):
        return por_defecto


# ---------------------------------------------------------------------------
# Contexto desde la base (entrenamiento)
# ---------------------------------------------------------------------------
def contexto_desde_db(con, cfg, clave: str, ts: float,
                      historial: list | None = None,
                      sistema: dict | None = None) -> dict | None:
    """
    Arma el contexto de una accion usando SOLO datos hasta `ts`.

    Devuelve None si no hay suficiente historia: es mejor descartar una
    muestra que inventarle features.
    """
    if historial is None:
        historial = con.execute(
            "SELECT * FROM muestras_proceso WHERE clave=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 40",
            (clave, ts),
        ).fetchall()
    if not historial:
        return None

    actual = historial[0]
    ws = _seguro(actual["ws"])
    if ws <= 0:
        return None

    # Ventana base de fallos: la tasa ANTES del trim, que es contra lo que se
    # va a comparar despues. Se calcula con filas anteriores al momento dado.
    t_base = ts - cfg.ventana_base_seg
    en_base = [h for h in historial if h["ts"] >= t_base and h["fallos_pagina"] is not None]
    fallos_por_min = 0.0
    if len(en_base) >= 2:
        mas_nuevo, mas_viejo = en_base[0], en_base[-1]
        dt_min = (mas_nuevo["ts"] - mas_viejo["ts"]) / 60.0
        if dt_min > 0.05:
            d = mas_nuevo["fallos_pagina"] - mas_viejo["fallos_pagina"]
            fallos_por_min = max(0.0, d / dt_min)

    fallos_por_mb = fallos_por_min / max(ws / 1024**2, 1.0)

    # Series recientes para CPU y crecimiento.
    recientes = [h for h in historial if h["ts"] >= ts - 300]
    cpus = [_seguro(h["cpu_pct"]) for h in recientes if h["cpu_pct"] is not None]
    cpu_pct = cpus[0] if cpus else 0.0
    cpu_max = max(cpus) if cpus else 0.0

    # Crecimiento del working set: comparamos contra la muestra mas vieja de
    # los ultimos 5 minutos. Positivo = esta creciendo (probablemente activo).
    ws_viejo = None
    for h in reversed(recientes):
        ws_viejo = _seguro(h["ws"])
    crecimiento = 0.0
    if ws_viejo and ws_viejo > 0:
        crecimiento = (ws - ws_viejo) / ws_viejo

    edad_min = 0.0
    if actual["pid"]:
        # La edad sale del create_time embebido en la clave "pid:create_time".
        try:
            crear = int(clave.split(":")[1])
            if crear > 0:
                edad_min = max(0.0, (ts - crear) / 60.0)
        except (IndexError, ValueError):
            pass

    ws_pico = _seguro(actual["ws_pico"], ws)
    commit = _seguro(actual["commit_bytes"])

    if sistema is None:
        fila = con.execute(
            "SELECT * FROM muestras_sistema WHERE ts<=? ORDER BY ts DESC LIMIT 1",
            (ts,),
        ).fetchone()
        sistema = dict(fila) if fila else {}

    ram_total = _seguro(sistema.get("ram_total"), 1.0) or 1.0
    commit_lim = _seguro(sistema.get("commit_limite"), 1.0) or 1.0

    # Tasa de lecturas de disco: el delta entre la ultima muestra del sistema
    # y la anterior. Necesitamos dos filas para el delta.
    lect_min = 0.0
    sm = con.execute(
        "SELECT ts, lecturas_pagina FROM muestras_sistema WHERE ts<=? "
        "ORDER BY ts DESC LIMIT 2",
        (ts,),
    ).fetchall()
    if len(sm) == 2 and sm[0]["lecturas_pagina"] is not None and sm[1]["lecturas_pagina"] is not None:
        dt_min = (sm[0]["ts"] - sm[1]["ts"]) / 60.0
        if dt_min > 0.001:
            lect_min = max(0.0, (sm[0]["lecturas_pagina"] - sm[1]["lecturas_pagina"]) / dt_min)

    h_sin, h_cos = _hora_ciclica(ts)

    return {
        "log_ws": math.log10(max(ws, 1.0)),
        "ws_ratio_ram": ws / ram_total,
        "ws_pico_ratio": ws / max(ws_pico, 1.0),
        "crecimiento_ws": crecimiento,
        "fallos_por_min": fallos_por_min,
        "fallos_por_mb_min": fallos_por_mb,
        "cpu_pct": cpu_pct,
        "cpu_pct_max": cpu_max,
        "cpu_por_mb": cpu_pct / max(ws / 1024**2, 1.0),
        "hilos": _seguro(actual["hilos"]),
        "edad_min": edad_min,
        "commit_ratio": commit / max(ws, 1.0),
        "presion_ram": 1.0 - (_seguro(sistema.get("ram_disponible")) / ram_total),
        "presion_commit": _seguro(sistema.get("commit_total")) / commit_lim,
        "lecturas_disco_min": lect_min,
        "cache_ratio": _seguro(sistema.get("cache_sistema")) / ram_total,
        "n_procesos": _seguro(sistema.get("procesos")),
        "io_lect_bps_log": math.log10(max(_seguro(sistema.get("disco_lect_bps")), 1.0)),
        "categoria": float(CATEGORIA_NUM.get(actual["categoria"] or "libre", 0)),
        "hora_sin": h_sin,
        "hora_cos": h_cos,
        "_ws": ws,
        "_fallos_por_min": fallos_por_min,
    }


# ---------------------------------------------------------------------------
# Contexto en vivo (decision del agente)
# ---------------------------------------------------------------------------
def contexto_en_vivo(cfg, *, datos_proceso: dict, datos_sistema: dict,
                     historial: list[dict], categoria: str = "libre") -> dict | None:
    """
    Arma el mismo contexto pero desde lo que el agente tiene en memoria.

    `historial` es una lista de dicts con ts, ws, fallos_pagina, cpu_pct,
    ordered del mas nuevo al mas viejo (igual que la consulta SQL).
    """
    if not historial:
        return None
    actual = historial[0]
    ws = _seguro(actual.get("ws"))
    if ws <= 0:
        return None

    ahora = actual.get("ts") or time.time()
    t_base = ahora - cfg.ventana_base_seg
    en_base = [h for h in historial if h.get("ts", 0) >= t_base
               and h.get("fallos_pagina") is not None]
    fallos_por_min = 0.0
    if len(en_base) >= 2:
        mas_nuevo, mas_viejo = en_base[0], en_base[-1]
        dt_min = (mas_nuevo["ts"] - mas_viejo["ts"]) / 60.0
        if dt_min > 0.05:
            d = mas_nuevo["fallos_pagina"] - mas_viejo["fallos_pagina"]
            fallos_por_min = max(0.0, d / dt_min)

    recientes = [h for h in historial if h.get("ts", 0) >= ahora - 300]
    cpus = [_seguro(h.get("cpu_pct")) for h in recientes if h.get("cpu_pct") is not None]
    cpu_pct = cpus[0] if cpus else 0.0
    cpu_max = max(cpus) if cpus else 0.0

    ws_viejo = None
    for h in reversed(recientes):
        ws_viejo = _seguro(h.get("ws"))
    crecimiento = 0.0
    if ws_viejo and ws_viejo > 0:
        crecimiento = (ws - ws_viejo) / ws_viejo

    crear = datos_proceso.get("create_time") or 0
    edad_min = max(0.0, (ahora - crear) / 60.0) if crear > 0 else 0.0

    ram_total = _seguro(datos_sistema.get("ram_total"), 1.0) or 1.0
    commit_lim = _seguro(datos_sistema.get("commit_limite"), 1.0) or 1.0
    h_sin, h_cos = _hora_ciclica(ahora)

    return {
        "log_ws": math.log10(max(ws, 1.0)),
        "ws_ratio_ram": ws / ram_total,
        "ws_pico_ratio": ws / max(_seguro(datos_proceso.get("ws_pico"), ws), 1.0),
        "crecimiento_ws": crecimiento,
        "fallos_por_min": fallos_por_min,
        "fallos_por_mb_min": fallos_por_min / max(ws / 1024**2, 1.0),
        "cpu_pct": cpu_pct,
        "cpu_pct_max": cpu_max,
        "cpu_por_mb": cpu_pct / max(ws / 1024**2, 1.0),
        "hilos": _seguro(datos_proceso.get("hilos")),
        "edad_min": edad_min,
        "commit_ratio": _seguro(datos_proceso.get("commit")) / max(ws, 1.0),
        "presion_ram": 1.0 - (_seguro(datos_sistema.get("ram_disponible")) / ram_total),
        "presion_commit": _seguro(datos_sistema.get("commit_total")) / commit_lim,
        "lecturas_disco_min": _seguro(datos_sistema.get("lecturas_disco_min")),
        "cache_ratio": _seguro(datos_sistema.get("cache_sistema")) / ram_total,
        "n_procesos": _seguro(datos_sistema.get("procesos")),
        "io_lect_bps_log": math.log10(max(_seguro(datos_sistema.get("disco_lect_bps")), 1.0)),
        "categoria": float(CATEGORIA_NUM.get(categoria, 0)),
        "hora_sin": h_sin,
        "hora_cos": h_cos,
        "_ws": ws,
        "_fallos_por_min": fallos_por_min,
    }

"""
Evaluacion de las acciones: aca se fabrican las etiquetas del modelo.

Que es un "swap innecesario" en terminos medibles:

  Trimear el working set del proceso P vacia sus paginas. Si P vuelve a tocar
  esas paginas poco despues, el sistema tiene que traerlas de vuelta (de la
  standby list o del pagefile). Ese trabajo no hizo falta: nadie gano nada,
  porque la RAM liberada no se uso para otra cosa. Eso es un swap innecesario.

  La etiqueta sale de comparar la tasa de fallos de pagina de P DESPUES del
  trim contra su tasa ANTES:

      reincidencia = fallos_por_minuto(despues) / fallos_por_minuto(antes)
      etiqueta = 1  si reincidencia es alta Y los fallos absolutos son significativos

El caso incomodo, que casi todos estos proyectos ignoran: si el proceso estaba
completamente inactivo antes del trim (tasa ~0), y despues se pone activo, NO
se puede saber si fue culpa nuestra o si simplemente le tocaba despertarse.
Dividir por cero da un numero espectacular y falso. En ese caso la muestra se
descarta en vez de etiquetarse. Menos datos limpios valen mas que muchos
sucios: una etiqueta inventada no se nota hasta que las decisiones salen mal.
"""
from __future__ import annotations

import time

FALLO_BASE_MINIMO = 60.0   # fallos/min por debajo de esto = proceso dormido


def _tasa_fallos(con, clave: str, t_desde: float, t_hasta: float) -> float | None:
    """Fallos de pagina por minuto en la ventana dada. None si no hay datos."""
    filas = con.execute(
        "SELECT ts, fallos_pagina FROM muestras_proceso "
        "WHERE clave=? AND ts>=? AND ts<=? AND fallos_pagina IS NOT NULL "
        "ORDER BY ts",
        (clave, t_desde, t_hasta),
    ).fetchall()
    if len(filas) < 2:
        return None
    dt_min = (filas[-1]["ts"] - filas[0]["ts"]) / 60.0
    if dt_min < 0.05:
        return None
    delta = filas[-1]["fallos_pagina"] - filas[0]["fallos_pagina"]
    return max(0.0, delta / dt_min)


def _ws_en(con, clave: str, ts: float) -> int | None:
    f = con.execute(
        "SELECT ws FROM muestras_proceso WHERE clave=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1",
        (clave, ts),
    ).fetchone()
    return f["ws"] if f else None


def cerrar_acciones(con, cfg, ahora: float | None = None) -> int:
    """
    Evalua las acciones cuya ventana ya vencio y guarda su resultado.

    Devuelve cuantas acciones cerro. Se puede llamar tantas veces como se
    quiera: las ya cerradas no se vuelven a tocar (LEFT JOIN en la consulta).
    """
    ahora = ahora or time.time()
    pendientes = con.execute(
        "SELECT a.* FROM acciones a LEFT JOIN resultados r ON r.accion_id = a.id "
        "WHERE r.accion_id IS NULL AND a.tipo = 'trim' AND a.ts <= ? "
        "ORDER BY a.ts",
        (ahora - cfg.ventana_evaluacion_seg,),
    ).fetchall()

    cerradas = 0
    for a in pendientes:
        clave, t0 = a["clave"], a["ts"]

        # Antes: la tasa contra la que comparamos.
        base = _tasa_fallos(con, clave, t0 - cfg.ventana_base_seg, t0)

        # Despues: salteamos los primeros segundos. Justo despues del trim hay
        # un pico de fallos que es el propio efecto de vaciar el working set,
        # no reincidencia. Medir desde ahi inflaria todo.
        post = _tasa_fallos(con, clave, t0 + cfg.retardo_evaluacion_seg,
                            t0 + cfg.ventana_evaluacion_seg)

        ws_post = _ws_en(con, clave, t0 + cfg.ventana_evaluacion_seg)
        ws_antes = a["ws_despues"] or a["ws_antes"]
        delta_ws = (ws_post - ws_antes) if (ws_post is not None and ws_antes is not None) else None

        # Columnas de `resultados`: accion_id, ts_eval, segundos, fallos_base,
        # fallos_post, reincidencia, delta_ws, etiqueta, motivo  -> 9.
        if base is None or post is None:
            con.execute(
                "INSERT OR REPLACE INTO resultados VALUES (?,?,?,?,?,?,?,?,?)",
                (a["id"], ahora, ahora - t0, None, None, None, delta_ws, None,
                 "sin datos suficientes en la ventana"),
            )
            cerradas += 1
            continue

        fallos_abs = post * (cfg.ventana_evaluacion_seg - cfg.retardo_evaluacion_seg) / 60.0
        suficiente = fallos_abs >= cfg.min_fallos_para_etiqueta

        if base < FALLO_BASE_MINIMO:
            if post < FALLO_BASE_MINIMO:
                # Dormido antes y dormido despues: el trim no costo nada.
                etiqueta, reinc, motivo = 0, 0.0, "proceso inactivo antes y despues"
            else:
                # Estaba dormido y se desperto. No se puede atribuir.
                etiqueta, reinc, motivo = None, None, (
                    "ambiguo: estaba inactivo antes del trim y se activo despues, "
                    "no se puede saber si fue por el trim")
        else:
            reinc = post / base
            if not suficiente:
                etiqueta, motivo = None, "pocos fallos para etiquetar con confianza"
            elif reinc > cfg.umbral_reincidencia:
                etiqueta, motivo = 1, (
                    f"thrash: {reinc:.1f}x su tasa base ({base:.0f} -> {post:.0f} fallos/min)")
            else:
                etiqueta, motivo = 0, f"sin thrash: {reinc:.2f}x su tasa base"

        con.execute(
            "INSERT OR REPLACE INTO resultados VALUES (?,?,?,?,?,?,?,?,?)",
            (a["id"], ahora, ahora - t0, base, post, reinc, delta_ws, etiqueta, motivo),
        )
        cerradas += 1

    return cerradas


def estadisticas(con) -> dict:
    """Resumen de etiquetas para el panel."""
    f = con.execute(
        "SELECT COUNT(*) n, "
        "SUM(CASE WHEN etiqueta=1 THEN 1 ELSE 0 END) malas, "
        "SUM(CASE WHEN etiqueta=0 THEN 1 ELSE 0 END) buenas, "
        "SUM(CASE WHEN etiqueta IS NULL THEN 1 ELSE 0 END) descartadas "
        "FROM resultados"
    ).fetchone()
    total = f["n"] or 0
    malas = f["malas"] or 0
    buenas = f["buenas"] or 0
    con_etiqueta = malas + buenas
    return {
        "evaluadas": total,
        "malas": malas,
        "buenas": buenas,
        "descartadas": f["descartadas"] or 0,
        "tasa_thrash": (malas / con_etiqueta) if con_etiqueta else None,
        "listas_para_entrenar": con_etiqueta,
    }

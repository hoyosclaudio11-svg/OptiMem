"""
Panel web.

Sirve una sola pagina y unos pocos endpoints JSON. Sin CDN, sin fuentes
externas, sin build: la pagina funciona con el disco desconectado.

El panel NO toca memoria. Puede cambiar el modo del agente y disparar
entrenamiento o calibracion, pero todo lo que mueve memoria pasa por el
recolector, que es el unico que tiene el criterio y los limites por hora.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import (calibracion, critical, db, evaluator, features, probe, trainer,
               winapi)

DIR_ESTATICO = Path(__file__).resolve().parent / "static"


def _estado(con, cfg) -> dict:
    m = winapi.memoria_sistema()
    memoria = None
    if m:
        memoria = {
            "total": m.total, "disponible": m.disponible, "en_uso": m.en_uso,
            "commit_total": m.commit_total, "commit_limite": m.commit_limite,
            "cache": m.cache_sistema,
            "presion_ram": m.presion_ram, "presion_commit": m.presion_commit,
            "procesos": m.procesos,
        }

    # Ultima sonda y linea de base.
    s = con.execute("SELECT * FROM sondas ORDER BY ts DESC LIMIT 1").fetchone()
    base = db.leer_estado(con, "linea_base_sonda")
    base_ts = db.leer_estado(con, "linea_base_ts")
    sonda = None
    if s:
        # Se recalcula con la base actual, igual que la serie: asi el numero
        # de la tarjeta y el ultimo punto del grafico siempre coinciden.
        idx = probe.calcular_indice(
            {"cache": s["t_cache_ms"], "ws": s["t_ws_ms"],
             "cpu": s["t_cpu_ms"], "memoria": s["t_memoria_ms"]}, base)
        sonda = {
            "ts": s["ts"], "indice": idx, "cache": s["t_cache_ms"],
            "ws": s["t_ws_ms"], "cpu": s["t_cpu_ms"], "memoria": s["t_memoria_ms"],
            "linea_base": base, "linea_base_ts": base_ts,
        }

    # Presupuesto del agente en la hora corriente.
    hace_una_hora = time.time() - 3600
    acc = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(liberado),0) mb, "
        "COALESCE(SUM(CASE WHEN autor='exploracion' THEN 1 ELSE 0 END),0) expl "
        "FROM acciones WHERE ts > ?", (hace_una_hora,),
    ).fetchone()
    acciones = con.execute(
        "SELECT a.ts, a.nombre, a.tipo, a.nivel, a.liberado, a.autor, a.p_thrash, "
        "       r.etiqueta, r.reincidencia, r.motivo "
        "FROM acciones a LEFT JOIN resultados r ON r.accion_id = a.id "
        "ORDER BY a.ts DESC LIMIT 25"
    ).fetchall()
    frenadas = con.execute(
        "SELECT ts, nombre, detalle FROM eventos WHERE tipo='decision_frenada' "
        "ORDER BY ts DESC LIMIT 15"
    ).fetchall()

    e = evaluator.estadisticas(con)
    mod = con.execute(
        "SELECT version, algoritmo, metricas, umbral, ts, n_muestras "
        "FROM modelos WHERE activo=1 ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    modelo = None
    if mod:
        try:
            met = json.loads(mod["metricas"] or "{}")
        except Exception:
            met = {}
        modelo = {"version": mod["version"], "algoritmo": mod["algoritmo"],
                  "umbral": mod["umbral"], "ts": mod["ts"],
                  "n_muestras": mod["n_muestras"], "metricas": met}

    eventos = con.execute(
        "SELECT ts, tipo, severidad, nombre, detalle FROM eventos "
        "WHERE severidad != 'info' ORDER BY ts DESC LIMIT 20"
    ).fetchall()

    # Candidatos: los procesos grandes que si se pueden tocar.
    ult = con.execute("SELECT MAX(ts) t FROM muestras_proceso").fetchone()["t"]
    candidatos = []
    if ult:
        for r in con.execute(
            "SELECT nombre, pid, ws, categoria, critico, motivo, cpu_pct, "
            "fallos_pagina, prioridad_mem FROM muestras_proceso "
            "WHERE ts=? AND ws > 100*1024*1024 ORDER BY ws DESC LIMIT 30", (ult,)
        ):
            candidatos.append(dict(r))

    cal = db.leer_estado(con, "calibracion")

    return {
        "ts": time.time(),
        "cfg": {
            "modo": cfg.modo, "agente_activo": cfg.agente_activo,
            "umbral_seguridad": cfg.umbral_seguridad,
            "umbral_prioridad": cfg.umbral_prioridad,
            "presion_ram_libre_min": cfg.presion_ram_libre_min,
            "presion_commit_max": cfg.presion_commit_max,
            "max_acciones_por_hora": cfg.max_acciones_por_hora,
            "max_liberado_por_hora_mb": cfg.max_liberado_por_hora_mb,
            "min_muestras_entrenar": cfg.min_muestras_entrenar,
            "intervalo_sonda_seg": cfg.intervalo_sonda_seg,
            "dir_datos": str(cfg.dir),
        },
        "capacidades": winapi.detectar_capacidades().__dict__,
        "memoria": memoria,
        "sonda": sonda,
        "agente": {
            "acciones_1h": acc["n"], "liberado_1h": acc["mb"],
            "exploraciones_1h": acc["expl"],
            "presupuesto_acciones": max(0, cfg.max_acciones_por_hora - acc["n"]),
            "presupuesto_mb": max(0, cfg.max_liberado_por_hora_mb - int(acc["mb"] / 1024**2)),
        },
        "aprendizaje": e,
        "modelo": modelo,
        "calibracion": cal,
        "acciones": [dict(a) for a in acciones],
        "frenadas": [dict(f) for f in frenadas],
        "eventos": [dict(x) for x in eventos],
        "candidatos": candidatos,
    }


def _serie(con, minutos: int) -> dict:
    """
    Series para los graficos.

    Se submuestrea por promedio de canastas cuando hay demasiados puntos: una
    semana a 1 Hz son 600.000 filas, y mandarlas al navegador no le sirve a
    nadie. Con 600 puntos la forma de la curva se ve igual.
    """
    desde = time.time() - minutos * 60
    max_puntos = 600

    def canastas(filas, campos):
        if not filas:
            return []
        paso = max(1, len(filas) // max_puntos)
        salida = []
        for i in range(0, len(filas), paso):
            trozo = filas[i:i + paso]
            punto = {"ts": trozo[0]["ts"]}
            for c in campos:
                vals = [f[c] for f in trozo if f[c] is not None]
                punto[c] = (sum(vals) / len(vals)) if vals else None
            punto["t"] = trozo[0]["ts"]
            salida.append(punto)
        return salida

    sondas = con.execute(
        "SELECT ts, indice, t_cache_ms, t_ws_ms, t_cpu_ms, t_memoria_ms, "
        "ram_disp, commit_pct FROM sondas WHERE ts >= ? ORDER BY ts", (desde,)
    ).fetchall()

    # El indice se RECALCULA aca en vez de leerse de la columna guardada.
    #
    # Motivo: las filas escritas antes de que existiera la linea de base
    # tienen guardada la suma cruda de milisegundos (~100), y las posteriores
    # tienen la razon (~1.0). Mezcladas en un mismo grafico dan un escalon
    # que no ocurrio: parece que el sistema empeoro de golpe y en realidad
    # solo cambio la unidad. Como los milisegundos crudos quedan guardados,
    # se puede recalcular todo el historial con la base actual y que la serie
    # entera sea comparable consigo misma.
    base = db.leer_estado(con, "linea_base_sonda")
    sondas = [dict(f) for f in sondas]
    for f in sondas:
        f["indice"] = probe.calcular_indice(
            {"cache": f.get("t_cache_ms"), "ws": f.get("t_ws_ms"),
             "cpu": f.get("t_cpu_ms"), "memoria": f.get("t_memoria_ms")},
            base,
        )
    sistema = con.execute(
        "SELECT ts, ram_total, ram_disponible, commit_total, commit_limite, "
        "cache_sistema, procesos FROM muestras_sistema WHERE ts >= ? ORDER BY ts",
        (desde,),
    ).fetchall()

    sc = canastas(sondas,
                  ["indice", "t_cache_ms", "t_ws_ms", "t_cpu_ms", "t_memoria_ms"])
    mc = canastas([dict(f) for f in sistema],
                  ["ram_total", "ram_disponible", "commit_total", "commit_limite",
                   "cache_sistema", "procesos"])

    # Primera accion del agente: el corte antes/despues del informe.
    f = con.execute(
        "SELECT MIN(ts) t FROM acciones WHERE autor IN ('agente','exploracion')"
    ).fetchone()
    return {"sondas": sc, "sistema": mc, "corte": f["t"] if f and f["t"] else None,
            "minutos": minutos}


def correr(cfg, abrir: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    app = FastAPI(title="OptiMem", docs_url=None, redoc_url=None)
    db.inicializar(cfg.ruta_db)   # crea el esquema una vez, al arrancar

    # OJO: una conexion por peticion, no una compartida.
    #
    # FastAPI ejecuta los endpoints sincronicos en un pool de hilos, y sqlite3
    # rechaza por defecto que una conexion se use desde un hilo distinto al que
    # la creo ("SQLite objects created in a thread can only be used in that
    # same thread"). El sintoma engania: llamado a mano desde un script anda
    # perfecto, y por HTTP devuelve 500 en todos los endpoints.
    #
    # Se podria pasar check_same_thread=False, pero entonces dos peticiones
    # simultaneas podrian usar la misma conexion a la vez. Una conexion por
    # peticion es mas simple y no tiene ese riesgo.
    def abrir():
        return db.conectar(cfg.ruta_db)

    @app.get("/", response_class=HTMLResponse)
    def raiz():
        ruta = DIR_ESTATICO / "index.html"
        if not ruta.exists():
            return HTMLResponse("<h1>Falta optimem/static/index.html</h1>", status_code=500)
        return HTMLResponse(ruta.read_text(encoding="utf-8"))

    @app.get("/api/estado")
    def api_estado():
        con = abrir()
        try:
            return JSONResponse(_estado(con, cfg))
        finally:
            con.close()

    @app.get("/api/serie")
    def api_serie(minutos: int = 180):
        minutos = max(5, min(minutos, 60 * 24 * 30))
        con = abrir()
        try:
            return JSONResponse(_serie(con, minutos))
        finally:
            con.close()

    @app.post("/api/modo/{modo}")
    def api_modo(modo: str):
        if modo not in ("observacion", "aprendizaje", "activo"):
            raise HTTPException(400, "modo invalido")
        # Se escribe en config.json: el recolector lo relee en cada ciclo, asi
        # que el cambio vale sin reiniciar nada.
        from . import config as cfgmod
        c = cfgmod.cargar()
        c.modo = modo
        c.agente_activo = modo != "observacion"
        cfgmod.guardar(c)
        cfg.modo = modo
        cfg.agente_activo = c.agente_activo
        con = abrir()
        try:
            db.registrar_evento(con, "cambio_modo", "info", detalle=f"modo -> {modo}")
        finally:
            con.close()
        return {"ok": True, "modo": modo}

    @app.post("/api/calibrar")
    def api_calibrar():
        con = abrir()
        try:
            r = calibracion.calibrar(cfg, con)
        finally:
            con.close()
        if not r:
            raise HTTPException(500, "la calibracion fallo")
        return {"ok": True, "calibracion": r}

    @app.post("/api/entrenar")
    def api_entrenar():
        con = abrir()
        try:
            inf = trainer.entrenar(cfg, con)
        finally:
            con.close()
        return {"ok": inf["apto"], "informe": inf}

    @app.get("/api/serie.csv")
    def api_csv(minutos: int = 1440):
        """Vista de tabla / exportacion, para no depender del grafico."""
        con = abrir()
        try:
            s = _serie(con, minutos)
        finally:
            con.close()
        lineas = ["tipo,ts,indice,cache_ms,ws_ms,cpu_ms,memoria_ms"]
        for f in s["sondas"]:
            lineas.append(f"sonda,{f['ts']},{f.get('indice')},{f.get('t_cache_ms')},"
                          f"{f.get('t_ws_ms')},{f.get('t_cpu_ms')},{f.get('t_memoria_ms')}")
        lineas.append("")
        lineas.append("tipo,ts,ram_total,ram_disponible,commit_total,commit_limite,cache")
        for f in s["sistema"]:
            lineas.append(f"sistema,{f['ts']},{f.get('ram_total')},"
                          f"{f.get('ram_disponible')},{f.get('commit_total')},"
                          f"{f.get('commit_limite')},{f.get('cache_sistema')}")
        return HTMLResponse("\n".join(lineas), media_type="text/csv")

    if abrir:
        def _abrir():
            time.sleep(1.2)
            webbrowser.open(f"http://{cfg.host}:{cfg.puerto}/")
        threading.Thread(target=_abrir, daemon=True).start()

    print(f"\nOptiMem: http://{cfg.host}:{cfg.puerto}/")
    print(f"Modo: {cfg.modo}   datos en {cfg.dir}")
    print("Ctrl+C para detener.\n")
    uvicorn.run(app, host=cfg.host, port=cfg.puerto, log_level="warning")


if __name__ == "__main__":
    from . import config as _cfg
    correr(_cfg.cargar())

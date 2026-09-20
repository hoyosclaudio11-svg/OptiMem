#!/usr/bin/env python
"""
OptiMem - linea de comandos.

    python optimem.py capacidades      Que puede hacer en esta sesion
    python optimem.py sonda            Corre la sonda de respuesta una vez
    python optimem.py recolectar       Recolecta metricas (dejar corriendo)
    python optimem.py evaluar          Estado de las etiquetas
    python optimem.py entrenar         Entrena el modelo
    python optimem.py informe          El informe de mejora (la metrica del spec)
    python optimem.py panel            Panel web
    python optimem.py estado           Resumen de todo
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from optimem import config as cfgmod


def _fmt_mb(b) -> str:
    if b is None:
        return "?"
    return f"{b / 1024**2:,.0f} MB"


def _fmt_gb(b) -> str:
    if b is None:
        return "?"
    return f"{b / 1024**3:.2f} GB"


# ---------------------------------------------------------------------------
def cmd_capacidades(args) -> int:
    from optimem import winapi

    c = winapi.detectar_capacidades()
    print("Capacidades detectadas en esta sesion")
    print("=" * 60)
    filas = [
        ("Admin", c.admin, "permite purgar standby list y file cache"),
        ("Trim de working set", c.trim_proceso, "vacia el working set de un proceso"),
        ("Prioridad de memoria", c.prioridad_memoria, "palanca suave, sin costo inmediato"),
        ("Purga de standby list", c.purga_standby, "requiere admin - normalmente apagada"),
        ("Ajuste de file cache", c.file_cache, "requiere admin - normalmente apagada"),
        ("Lecturas de disco", c.lecturas_disco, "paginas traidas de disco (swapping duro)"),
    ]
    for nombre, ok, desc in filas:
        print(f"  {'[si]' if ok else '[no]'} {nombre:<24} {desc}")
    print("\nDetalle:")
    for k, v in c.detalle.items():
        print(f"  {k}: {v}")
    if not c.admin:
        print("\nSin admin se puede igual: se validaron ~271 procesos abribles")
        print("con permiso de escritura (14 GB de RAM alcanzable). Las palancas")
        print("que requieren admin se apagan solas, sin romper nada.")
    return 0


def cmd_sonda(args) -> int:
    from optimem import probe, winapi

    cfg = cfgmod.cargar()
    print("Corriendo la sonda de tiempo de respuesta...")
    print("(una medicion suelta: el buffer es nuevo, asi que 'working set' sale")
    print(" rapido y no sirve de mucho. La medicion real la hace la sonda viva,")
    print(" que mantiene sus paginas entre mediciones.)\n")
    t0 = time.time()
    r = probe.ejecutar_sonda(cfg, en_proceso=args.en_proceso)
    dt = time.time() - t0
    print(f"Resultado (tardo {dt:.1f} s):")
    print(f"  cache   (leer {probe.MB_CACHE} MB que deberian estar cacheados) : "
          f"{r.get('cache', -1):8.2f} ms")
    print(f"  working set (re-tocar {cfg.sonda_mb_memoria} MB propios)          : "
          f"{r.get('ws', -1):8.2f} ms")
    print(f"  cpu     ({cfg.sonda_iteraciones_cpu:,} iteraciones)              : "
          f"{r.get('cpu', -1):8.2f} ms")
    print(f"  memoria (fallar {cfg.sonda_mb_memoria} MB nuevos)               : "
          f"{r.get('memoria', -1):8.2f} ms")
    m = winapi.memoria_sistema()
    if m:
        print(f"\n  RAM: {_fmt_gb(m.en_uso)} en uso de {_fmt_gb(m.total)} "
              f"({m.presion_ram * 100:.0f}%)")
        print(f"  commit: {_fmt_gb(m.commit_total)} de {_fmt_gb(m.commit_limite)} "
              f"({m.presion_commit * 100:.0f}%)")
    if args.json:
        print(json.dumps(r, indent=2))
    return 0


def cmd_recolectar(args) -> int:
    from optimem import collector, db

    cfg = cfgmod.cargar()
    if args.modo:
        cfg.modo = args.modo
    if args.activar:
        cfg.agente_activo = True
    if args.horas:
        cfg.retencion_dias = max(cfg.retencion_dias, int(args.horas / 24) + 1)

    print(f"Modo del agente: {cfg.modo}"
          f"{'' if cfg.agente_activo else ' (agente apagado)'}")
    print(f"Datos en: {cfg.dir}")
    print("Ctrl+C para detener.\n")

    dur = args.duracion * 60 if args.duracion else None
    con = db.inicializar(cfg.ruta_db)
    collector.Recolector(cfg, con).correr(duracion_seg=dur)
    return 0


def cmd_evaluar(args) -> int:
    from optimem import db, evaluator

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)
    e = evaluator.estadisticas(con)

    print("Etiquetas (el modelo aprende de esto)")
    print("=" * 60)
    print(f"  acciones evaluadas      : {e['evaluadas']}")
    print(f"  trims que salieron bien : {e['buenas']}")
    print(f"  trims que causaron thrash: {e['malas']}")
    print(f"  descartadas (ambiguas)  : {e['descartadas']}")
    if e["tasa_thrash"] is not None:
        print(f"  tasa de thrash          : {e['tasa_thrash'] * 100:.1f}%")
        print(f"\n  Comentario: si la tasa es muy baja, el problema no es grave y")
        print(f"  el modelo tendra poco que aportar. Si es alta, hay margen real.")
    print(f"\n  listas para entrenar    : {e['listas_para_entrenar']} "
          f"(minimo {cfg.min_muestras_entrenar})")
    if e["listas_para_entrenar"] < cfg.min_muestras_entrenar:
        faltan = cfg.min_muestras_entrenar - e["listas_para_entrenar"]
        print(f"  faltan {faltan} muestras. Con exploracion activa se juntan solas.")
    return 0


def cmd_entrenar(args) -> int:
    from optimem import db, trainer

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)
    inf = trainer.entrenar(cfg, con)

    print("Entrenamiento")
    print("=" * 60)
    print(f"  muestras etiquetadas: {inf['n_total']} "
          f"({inf['n_positivos']} malas, {inf['n_negativos']} buenas)")
    if not inf["apto"]:
        print(f"\n  NO SE ENTRENO: {inf['motivo']}")
        print("\n  Es a proposito: un modelo entrenado con una sola clase, o con")
        print("  pocos datos, da metricas que parecen buenas y decisiones malas.")
        return 1

    print(f"  entrenamiento: {inf['n_train']} (positivos {inf['positivos_train']})")
    print(f"  evaluacion   : {inf['n_test']} (positivos {inf['positivos_test']})")
    print("  El corte es POR TIEMPO: entrena con el pasado, evalua con el futuro.\n")

    print("  Algoritmos probados:")
    for nombre, m in inf["candidatos"].items():
        auc = m.get("auc")
        print(f"    {nombre:<20} AUC {auc if auc is None else round(auc, 3)}  "
              f"F1 {m['f1']:.3f}  precision {m['precision']:.3f}  recall {m['recall']:.3f}")
    print(f"\n  Elegido: {inf['algoritmo']}")

    lb = inf["linea_base"]
    ev = inf["evaluacion"]
    print(f"\n  Umbral de decision: {inf['umbral']:.2f}")
    print(f"    de los trims malos, habria frenado el "
          f"{(lb['pct_malos_evitados'] or 0) * 100:.0f}%  ({lb['malos_evitados']}/{lb['malos_en_test']})")
    print(f"    a costa de no hacer el {lb['pct_buenos_perdidos'] * 100:.0f}% "
          f"de los trims que estaban bien")
    print(f"    precision {ev['precision']:.3f}  recall {ev['recall']:.3f}  AUC "
          f"{ev['auc'] if ev['auc'] is None else round(ev['auc'], 3)}")
    print(f"\n  Regla tonta (trimear siempre) acertaria el "
          f"{lb['regla_trimear_siempre_accuracy'] * 100:.1f}% de las veces.")

    if inf.get("importancias"):
        print("\n  Que mira el modelo (importancia por permutacion):")
        for imp in inf["importancias"][:6]:
            if imp["importancia"] > 0:
                print(f"    {imp['nombre']:<34} {imp['importancia']:+.4f} "
                      f"+- {imp['desvio']:.4f}")

    print(f"\n  Modelo guardado en: {inf['ruta']}")
    return 0


def cmd_calibrar(args) -> int:
    from optimem import calibracion, db

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)
    print("Midiendo cuanto cuesta vaciar un working set en esta maquina...")
    print("(levanta un proceso hijo de 300 MB, lo mide, lo trimea y lo vuelve a medir)\n")
    r = calibracion.calibrar(cfg, con, verbose=True)
    if not r:
        print("\n  No se pudo calibrar (el trim no funciono sobre el hijo).")
        return 1
    print(f"\n  el costo de re-tocar se multiplico por {r['factor']:.2f}x")
    print(f"  {r['ms_residente']:.1f} ms residente  ->  {r['ms_tras_trim']:.1f} ms tras el trim")
    print(f"\n  >>> cada MB vaciado cuesta {r['ms_por_mb']:.3f} ms de re-lectura")
    print(f"      (un proceso de 1 GB: {r['ms_por_mb'] * 1024:.0f} ms de pausa)")
    print(f"\n  Guardado. El informe usa este numero para traducir las paginas")
    print(f"  que el modelo evito a milisegundos de pausa.")
    return 0


def cmd_informe(args) -> int:
    """El informe que pide el spec: reduccion del tiempo de respuesta en %."""
    from optimem import db

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)

    # El corte es la primera accion real del agente: antes de eso, el sistema
    # corria sin que OptiMem tocara nada. Se puede forzar otro con --desde-horas
    # para comparar dos periodos cualesquiera.
    if getattr(args, "desde_horas", None):
        corte = time.time() - args.desde_horas * 3600
        print(f"  (corte fijado a mano: hace {args.desde_horas} h)")
    else:
        f = con.execute(
            "SELECT MIN(ts) t FROM acciones WHERE autor IN ('agente','exploracion')"
        ).fetchone()
        corte = f["t"] if f and f["t"] else None

    print("Informe de mejora")
    print("=" * 66)

    s = con.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM sondas").fetchone()
    if not s["n"] or s["n"] < 4:
        print(f"  Solo hay {s['n'] or 0} sondas. Hacen falta al menos 4 para")
        print("  comparar algo. Deja el recolector corriendo un rato mas.")
        return 1

    if corte is None:
        print("  El agente todavia no actuo sobre nada, asi que no hay")
        print("  'despues' contra que comparar. El sistema esta en su estado")
        print("  de referencia.")
        print(f"\n  Sondas acumuladas: {s['n']} "
              f"({(s['b'] - s['a']) / 3600:.1f} h de observacion)")
        _tabla_sondas(con, None)
        return 0

    print(f"  Corte: {time.strftime('%Y-%m-%d %H:%M', time.localtime(corte))} "
          f"(primera accion de OptiMem)")
    antes = con.execute("SELECT COUNT(*) n FROM sondas WHERE ts < ?", (corte,)).fetchone()["n"]
    despues = con.execute("SELECT COUNT(*) n FROM sondas WHERE ts >= ?", (corte,)).fetchone()["n"]
    print(f"  Sondas antes: {antes}   despues: {despues}")

    if antes < 3 or despues < 3:
        print("\n  Faltan sondas de un lado del corte para comparar con sentido.")
        return 1

    _tabla_sondas(con, corte)

    # La metrica del spec.
    #
    # OJO: el indice se RECALCULA desde los milisegundos crudos con la linea
    # de base actual; no se lee la columna guardada.
    #
    # La columna mezcla dos escalas: las filas escritas antes de que existiera
    # la linea de base tienen la suma cruda (~100), y las posteriores la razon
    # (~1.0). Comparar las dos da una "mejora" del 99% que es puro cambio de
    # unidad. Es el error mas peligroso posible en este informe: un numero
    # enorme y falso que uno quiere creer.
    from optimem import probe as _probe

    base = db.leer_estado(con, "linea_base_sonda")
    if not base:
        print("\n  Sin linea de base todavia: no se puede comparar nada.")
        print("  Deja el recolector corriendo un rato mas.")
        return 1
    if not args.desde_horas:
        print("\n  Aviso: el indice se recalcula con la linea de base actual.")
        print("  Si el corte es anterior a la base, la comparacion mezcla la")
        print("  fase sin base con la fase con base. Conviene un corte posterior.")

    def _idx(fila) -> float:
        return _probe.calcular_indice(
            {"cache": fila["t_cache_ms"], "ws": fila["t_ws_ms"],
             "cpu": fila["t_cpu_ms"], "memoria": fila["t_memoria_ms"]}, base)

    m_antes = [_idx(r) for r in con.execute(
        "SELECT t_cache_ms, t_ws_ms, t_cpu_ms, t_memoria_ms FROM sondas "
        "WHERE ts < ? ORDER BY ts", (corte,)).fetchall()]
    m_desp = [_idx(r) for r in con.execute(
        "SELECT t_cache_ms, t_ws_ms, t_cpu_ms, t_memoria_ms FROM sondas "
        "WHERE ts >= ? ORDER BY ts", (corte,)).fetchall()]
    m_antes = [v for v in m_antes if v and v > 0]
    m_desp = [v for v in m_desp if v and v > 0]

    if m_antes and m_desp:
        ia = _mediana(m_antes)
        idp = _mediana(m_desp)
        if ia and idp:
            mejora = (ia - idp) / ia * 100
            print(f"\n  INDICE DE RESPUESTA (mediana de {len(m_antes)} "
                  f"y {len(m_desp)} sondas)")
            print(f"    antes   : {ia:.3f}")
            print(f"    despues : {idp:.3f}")
            signo = "mejoro" if mejora > 0 else "empeoro"
            print(f"    >>> la respuesta del sistema {signo} {abs(mejora):.1f}%")
            if abs(mejora) < 5:
                print(f"    (por debajo del ruido de la sonda, que es de ~3.5%:")
                print(f"     esto es 'sin cambio medible', no una mejora leve)")

    # Y el dato duro: paginas traidas de disco.
    _tabla_paging(con, corte)

    # Lo que el modelo aporto, en milisegundos.
    _tabla_modelo(con)
    return 0


def _tabla_modelo(con) -> None:
    """
    El aporte del modelo, traducido a milisegundos de pausa.

    OJO CON LA INTERPRETACION. El costo de un trim lo paga EL PROCESO
    trimeado, no el sistema entero. Por eso la sonda global puede no moverse
    aunque el modelo este evitando pausas reales. Aca se mira el otro lado:
    cuantas pausas se evitaron.
    """
    from optimem import calibracion

    print("\n  APORTE DEL MODELO")
    print("  " + "-" * 50)

    coef = calibracion.coeficiente(con)
    if coef is None:
        print("  Sin calibrar. Corre `python optimem.py calibrar` para poder")
        print("  traducir las paginas a milisegundos de pausa.")
        return

    todos = con.execute(
        "SELECT a.liberado, a.autor, r.fallos_post, r.fallos_base "
        "FROM acciones a JOIN resultados r ON r.accion_id = a.id "
        "WHERE a.tipo='trim'"
    ).fetchall()
    if not todos:
        print("  Todavia no hay acciones evaluadas.")
        return

    # Costo de las acciones que SI se hicieron: cuanto thrash causaron.
    con_costo = [f for f in todos if f["fallos_post"] is not None]
    if con_costo:
        prom_fallos = sum(f["fallos_post"] for f in con_costo) / len(con_costo)
        prom_mb = sum(f["liberado"] or 0 for f in con_costo) / len(con_costo) / 1024**2
        total_gb = sum(f["liberado"] or 0 for f in con_costo) / 1024**3
        print(f"  trims hechos            : {len(todos)}")
        print(f"  liberado promedio       : {prom_mb:,.0f} MB por trim")
        print(f"  fallos de pagina promedio que causaron: {prom_fallos:,.0f}/min")
        print(f"  costo estimado por trim : {prom_mb * coef:,.0f} ms de pausa")
        # El balance real de la operacion, que es lo que importa decidir.
        print(f"\n  BALANCE: libero {total_gb:.2f} GB a cambio de "
              f"{len(con_costo) * prom_mb * coef:,.0f} ms de pausa repartidos")
        print(f"  entre los procesos tocados. Si esa RAM se uso para evitar que")
        print(f"  algo se quedara sin memoria, el cambio conviene; si no, no.")

    frenados = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(1),0) FROM eventos "
        "WHERE tipo='decision_frenada'"
    ).fetchone()
    if frenados and frenados["n"]:
        print(f"\n  trims que el modelo FRENO : {frenados['n']}")
        # Cada freno evito una pausa del tamano tipico de un trim.
        if con_costo:
            print(f"  pausas evitadas (aprox)  : {frenados['n'] * prom_mb * coef:,.0f} ms")
        print("  (se calcula con el costo promedio; es una estimacion, no una")
        print("   medicion de cada caso)")


def _mediana(vals):
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _tabla_sondas(con, corte) -> None:
    campos = [("t_cache_ms", "cache"), ("t_ws_ms", "working set"),
              ("t_cpu_ms", "cpu"), ("t_memoria_ms", "memoria")]
    print(f"\n  {'medicion':<10} {'antes (ms)':>12} {'despues (ms)':>14} {'cambio':>10}")
    print("  " + "-" * 50)
    for campo, nombre in campos:
        if corte is None:
            filas = con.execute(f"SELECT {campo} v FROM sondas WHERE {campo} > 0").fetchall()
            m = _mediana([f["v"] for f in filas])
            print(f"  {nombre:<10} {m if m is None else round(m, 1):>12} {'-':>14} {'-':>10}")
        else:
            a = con.execute(f"SELECT {campo} v FROM sondas WHERE ts < ? AND {campo} > 0",
                            (corte,)).fetchall()
            d = con.execute(f"SELECT {campo} v FROM sondas WHERE ts >= ? AND {campo} > 0",
                            (corte,)).fetchall()
            ma, md = _mediana([x["v"] for x in a]), _mediana([x["v"] for x in d])
            if ma and md:
                cambio = (ma - md) / ma * 100
                print(f"  {nombre:<10} {ma:>12.1f} {md:>14.1f} {cambio:>+9.1f}%")
            else:
                print(f"  {nombre:<10} {'sin datos':>12} {'sin datos':>14} {'-':>10}")


def _tabla_paging(con, corte) -> None:
    """El dato duro: cuanto se leyo de disco antes y despues."""
    print("\n  PAGINAS LEIDAS DE DISCO (el swapping real)")
    print("  " + "-" * 50)
    for etiqueta, cond, params in (("antes", "ts < ?", (corte,)),
                                   ("despues", "ts >= ?", (corte,))):
        f = con.execute(
            f"SELECT ts, lecturas_pagina FROM muestras_sistema "
            f"WHERE {cond} AND lecturas_pagina IS NOT NULL ORDER BY ts", params
        ).fetchall()
        if len(f) < 2:
            print(f"  {etiqueta:<10} sin datos suficientes")
            continue
        dt_min = (f[-1]["ts"] - f[0]["ts"]) / 60.0
        if dt_min <= 0:
            continue
        delta = f[-1]["lecturas_pagina"] - f[0]["lecturas_pagina"]
        # Una ventana de segundos da un ritmo que parece enorme y no dice
        # nada. Mejor avisar que comparar peras con manzanas en silencio.
        if dt_min < 2:
            print(f"  {etiqueta:<10} ventana de {dt_min * 60:.0f} s: "
                  f"demasiado corta para un ritmo")
            continue
        print(f"  {etiqueta:<10} {delta / dt_min:>12,.0f} paginas/min   "
              f"({delta * 4096 / 1024**3:.2f} GB en {dt_min / 60:.1f} h)")


def cmd_avisar(args) -> int:
    from optimem import db, notificar

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)

    if args.probar:
        print("Mandando un mensaje de prueba por Telegram...")
        ok, msg = notificar.probar(cfg, con)
        print(f"\n  {msg}")
        if ok:
            print("\n  Si te llego al telefono, el aviso de las etiquetas va a funcionar.")
            print("  Si NO te llego, revisá el chat_id: el log dice el motivo exacto.")
        return 0 if ok else 1

    if args.ahora:
        print("Mandando el aviso real de umbral...")
        ok = notificar.avisar_si_corresponde(cfg, con, forzar=True)
        print("  enviado" if ok else "  no se pudo enviar")
        return 0 if ok else 1

    cred = notificar.credenciales(cfg)
    estado = "configurado" if cred else "SIN configurar"
    objetivo = cfg.min_muestras_entrenar
    n = con.execute("SELECT COUNT(*) n FROM resultados WHERE etiqueta IS NOT NULL").fetchone()["n"]
    print("Avisos por Telegram")
    print("=" * 60)
    print(f"  estado        : {estado}")
    print(f"  archivo .env  : {cfg.ruta_env or '(no configurado: solo variables de entorno)'}")
    print(f"  avisos activos: {'si' if cfg.avisos_activos else 'no'}")
    print(f"  etiquetas     : {n} de {objetivo}")
    if n < objetivo:
        print(f"  faltan        : {objetivo - n} para que dispare el aviso")
    else:
        print(f"  el umbral ya se cruzó; se avisa en el proximo ciclo del recolector")
    if cred:
        print(f"\n  El token no se muestra nunca, ni acá ni en el log.")
    else:
        print(f"\n  Para configurarlo, poné en config.json la ruta de tu .env:")
        print(f'      "ruta_env": "C:\\\\ruta\\\\a\\\\.env"')
        print(f"  o definí las variables OPTIMEM_TELEGRAM_TOKEN y OPTIMEM_TELEGRAM_CHAT_ID.")
    return 0


def cmd_panel(args) -> int:
    from optimem import server

    cfg = cfgmod.cargar()
    if args.puerto:
        cfg.puerto = args.puerto
    server.correr(cfg, abrir=not args.sin_abrir)
    return 0


def cmd_estado(args) -> int:
    from optimem import db, evaluator, trainer, winapi

    cfg = cfgmod.cargar()
    con = db.inicializar(cfg.ruta_db)

    print("OptiMem - estado")
    print("=" * 60)
    c = winapi.detectar_capacidades()
    print(f"  admin: {'si' if c.admin else 'no'}   "
          f"trim: {'si' if c.trim_proceso else 'no'}   "
          f"prioridad: {'si' if c.prioridad_memoria else 'no'}   "
          f"standby: {'si' if c.purga_standby else 'no'}")
    print(f"  modo: {cfg.modo}   agente activo: {'si' if cfg.agente_activo else 'no'}")
    print(f"  datos: {cfg.dir}")

    m = winapi.memoria_sistema()
    if m:
        print(f"\n  RAM: {_fmt_gb(m.en_uso)} / {_fmt_gb(m.total)} "
              f"({m.presion_ram * 100:.0f}%)   libre {_fmt_gb(m.disponible)}")
        print(f"  commit: {_fmt_gb(m.commit_total)} / {_fmt_gb(m.commit_limite)} "
              f"({m.presion_commit * 100:.0f}%)")
        print(f"  cache del sistema: {_fmt_gb(m.cache_sistema)}")

    ms = con.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM muestras_sistema").fetchone()
    if ms["n"]:
        horas = (ms["b"] - ms["a"]) / 3600
        print(f"\n  recoleccion: {ms['n']:,} muestras en {horas:.1f} h")
    mp = con.execute("SELECT COUNT(*) n FROM muestras_proceso").fetchone()
    print(f"  procesos: {mp['n']:,} muestras")

    e = evaluator.estadisticas(con)
    print(f"\n  acciones evaluadas: {e['evaluadas']}  "
          f"(malas {e['malas']}, buenas {e['buenas']}, descartadas {e['descartadas']})")
    if e["tasa_thrash"] is not None:
        print(f"  tasa de thrash: {e['tasa_thrash'] * 100:.1f}%")

    mod = trainer.cargar_activo(cfg, con)
    if mod:
        print(f"\n  modelo activo: {mod['version']} ({mod['algoritmo']}) "
              f"umbral {mod['umbral']:.2f}")
    else:
        print("\n  modelo activo: ninguno")

    ev = con.execute(
        "SELECT tipo, severidad, nombre, detalle, ts FROM eventos "
        "WHERE severidad != 'info' ORDER BY ts DESC LIMIT 8"
    ).fetchall()
    if ev:
        print("\n  Ultimos eventos que importan:")
        for e2 in ev:
            ts = time.strftime("%d/%m %H:%M", time.localtime(e2["ts"]))
            print(f"    {ts}  [{e2['severidad']}] {e2['tipo']} "
                  f"{e2['nombre'] or ''} {e2['detalle'] or ''}")
    return 0


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="optimem", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("capacidades", help="que palancas hay disponibles").set_defaults(fn=cmd_capacidades)

    s = sub.add_parser("sonda", help="correr la sonda de respuesta una vez")
    s.add_argument("--en-proceso", action="store_true",
                   help="sin subproceso (para depurar, contamina la medicion)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sonda)

    s = sub.add_parser("recolectar", help="recolectar metricas")
    s.add_argument("--duracion", type=float, metavar="MIN",
                   help="cuantos minutos correr (por defecto, hasta Ctrl+C)")
    s.add_argument("--activar", action="store_true", help="encender el agente")
    s.add_argument("--modo", choices=["observacion", "aprendizaje", "activo"],
                   help="modo del agente")
    s.add_argument("--horas", type=float, help="retencion deseada, en horas")
    s.set_defaults(fn=cmd_recolectar)

    sub.add_parser("evaluar", help="estado de las etiquetas").set_defaults(fn=cmd_evaluar)
    sub.add_parser("calibrar",
                   help="medir cuanto cuesta vaciar un working set").set_defaults(fn=cmd_calibrar)
    sub.add_parser("entrenar", help="entrenar el modelo").set_defaults(fn=cmd_entrenar)
    s = sub.add_parser("informe", help="informe de mejora (la metrica del spec)")
    s.add_argument("--desde-horas", type=float, metavar="H",
                   help="fijar el corte a mano, tantas horas atras")
    s.set_defaults(fn=cmd_informe)

    s = sub.add_parser("avisar", help="avisos por Telegram")
    s.add_argument("--probar", action="store_true",
                   help="mandar un mensaje de prueba (hacelo: un aviso sin probar es una intencion)")
    s.add_argument("--ahora", action="store_true", help="mandar el aviso de umbral ya")
    s.set_defaults(fn=cmd_avisar)

    s = sub.add_parser("panel", help="panel web")
    s.add_argument("--puerto", type=int)
    s.add_argument("--sin-abrir", action="store_true", help="no abrir el navegador")
    s.set_defaults(fn=cmd_panel)

    sub.add_parser("estado", help="resumen").set_defaults(fn=cmd_estado)

    args = p.parse_args(argv)
    return args.fn(args)


def _guardar_error_fatal(e: BaseException) -> str | None:
    """
    Deja rastro de un error que impide arrancar.

    Necesario porque la tarea programada corre con pythonw.exe, que no tiene
    consola: cualquier excepcion antes de que el logger este configurado
    desaparece sin dejar rastro, y el sintoma es "no recolecta nada" sin
    ninguna pista. Ya nos pasó con otros pipelines: corridas que mueren mudas.
    """
    try:
        from optimem import config as _c
        ruta = _c.cargar().dir / "errores_criticos.log"
    except Exception:
        import os
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
        ruta = __import__("pathlib").Path(base) / "OptiMem" / "errores_criticos.log"
        ruta.parent.mkdir(parents=True, exist_ok=True)
    try:
        import traceback
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            traceback.print_exception(type(e), e, e.__traceback__, file=f)
        return str(ruta)
    except Exception:
        return None


if __name__ == "__main__":
    try:
        codigo = main()
    except KeyboardInterrupt:
        codigo = 130
    except SystemExit:
        raise
    except BaseException as e:          # noqa: BLE001 - ultimo recurso
        destino = _guardar_error_fatal(e)
        try:
            sys.stderr.write(f"\nOptiMem no pudo arrancar: {type(e).__name__}: {e}\n")
            if destino:
                sys.stderr.write(f"Detalle en: {destino}\n")
        except Exception:
            pass
        codigo = 1
    sys.exit(codigo)

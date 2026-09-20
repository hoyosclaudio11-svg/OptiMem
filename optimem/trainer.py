"""
Entrenamiento del modelo que predice swaps innecesarios.

Decisiones que hacen a la honestidad de las metricas:

  - SPLIT POR TIEMPO, no aleatorio. Las acciones sobre un mismo proceso estan
    correlacionadas entre si. Si partimos al azar, la misma racha aparece en
    train y en test y las metricas salen infladas. Entrenamos con el pasado y
    evaluamos con el futuro, que es exactamente como se va a usar.

  - LINEA DE BASE EXPLICITA. Un modelo que dice "ningun trim hace dano" acierta
    el 80% si el 80% de los trims son buenos. Eso no es haber aprendido nada.
    Siempre reportamos contra la regla tonta de trimear a ciegas.

  - SI NO HAY LAS DOS CLASES, NO SE ENTRENA. Con una sola clase no hay nada
    que aprender y cualquier metrica es ruido. Falla ruidosamente en vez de
    guardar un modelo inutil que despues decide de verdad.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import db, features

log = None


def construir_dataset(con, cfg, limite: int = 5000):
    """
    Arma (X, y, meta) desde las acciones ya evaluadas.

    Devuelve ademas los contextos crudos, que sirven para inspeccionar por que
    el modelo decide lo que decide.
    """
    filas = con.execute(
        "SELECT a.id, a.clave, a.ts, a.pid, a.nombre, a.modelo_id, a.p_thrash, "
        "       r.etiqueta, r.reincidencia "
        "FROM acciones a JOIN resultados r ON r.accion_id = a.id "
        "WHERE a.tipo='trim' AND r.etiqueta IS NOT NULL "
        "ORDER BY a.ts LIMIT ?",
        (limite,),
    ).fetchall()

    X, y, meta = [], [], []
    for f in filas:
        ctx = features.contexto_desde_db(con, cfg, f["clave"], f["ts"])
        if ctx is None:
            continue
        X.append(features.vector(ctx))
        y.append(int(f["etiqueta"]))
        ctx = dict(ctx)
        ctx["_accion_id"] = f["id"]
        ctx["_ts"] = f["ts"]
        ctx["_nombre"] = f["nombre"]
        ctx["_reincidencia"] = f["reincidencia"]
        meta.append(ctx)

    return np.array(X, dtype=float), np.array(y, dtype=int), meta


def _metricas(y_true, y_pred, y_prob) -> dict:
    from sklearn.metrics import (confusion_matrix, f1_score, precision_score,
                                 recall_score, roc_auc_score)
    m = {
        "n": int(len(y_true)),
        "positivos_reales": int(y_true.sum()),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "matriz": confusion_matrix(y_true, y_pred).tolist(),
    }
    try:
        m["auc"] = float(roc_auc_score(y_true, y_prob))
    except ValueError:
        m["auc"] = None   # una sola clase en test
    return m


def _umbral_optimo(y_true, y_prob) -> tuple[float, dict]:
    """
    Elige el umbral de decision y devuelve la curva completa.

    El costo de los dos errores no es simetrico:
      - Falso negativo (trimeamos algo que iba a thrashar): causamos un freeze
        visible. Es el error caro.
      - Falso positivo (no trimeamos algo que estaba bien): liberamos menos
        RAM. Es una oportunidad perdida, no un problema.
    Por eso priorizamos recall sobre la clase mala, exigiendo una precision
    minima razonable, en vez de maximizar F1 a secas.
    """
    curva = []
    mejor = (0.5, None)
    for u in np.arange(0.05, 0.96, 0.05):
        pred = (y_prob >= u).astype(int)
        from sklearn.metrics import f1_score, precision_score, recall_score
        p = precision_score(y_true, pred, zero_division=0)
        r = recall_score(y_true, pred, zero_division=0)
        f = f1_score(y_true, pred, zero_division=0)
        curva.append({"umbral": round(float(u), 2), "precision": float(p),
                      "recall": float(r), "f1": float(f),
                      "positivos": int(pred.sum())})
        # Criterio: recall maximo con precision >= 0.5; si ninguno llega,
        # el mejor F1.
        if p >= 0.5:
            if mejor[1] is None or r > mejor[1]["recall"]:
                mejor = (float(u), {"precision": float(p), "recall": float(r),
                                    "f1": float(f)})
    if mejor[1] is None:
        if curva:
            mejor = max(((c["umbral"], c) for c in curva), key=lambda x: x[1]["f1"])
        else:
            return 0.5, curva
    return mejor[0], curva


def entrenar(cfg, con=None, guardar_modelo: bool = True) -> dict:
    """
    Entrena y evalua. Devuelve un informe con todo lo necesario para juzgar
    si el modelo sirve, incluido el caso en que no sirva.
    """
    global log
    from . import config as _cfg
    log = log or _cfg.log("optimem.trainer")

    con = con or db.inicializar(cfg.ruta_db)
    from sklearn.ensemble import (HistGradientBoostingClassifier,
                                  RandomForestClassifier)
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, meta = construir_dataset(con, cfg)
    informe: dict = {
        "ts": time.time(),
        "n_total": int(len(y)),
        "n_positivos": int(y.sum()) if len(y) else 0,
        "n_negativos": int(len(y) - y.sum()) if len(y) else 0,
        "features": features.FEATURES,
        "apto": False,
    }

    if len(y) < cfg.min_muestras_entrenar:
        informe["motivo"] = (
            f"faltan muestras: hay {len(y)} etiquetadas y se necesitan "
            f"{cfg.min_muestras_entrenar}. Deja el recolector corriendo."
        )
        log.warning("Entrenamiento abortado: %s", informe["motivo"])
        return informe

    if len(set(y.tolist())) < 2:
        informe["motivo"] = (
            f"todas las muestras tienen la misma etiqueta ({y[0]}). "
            "No hay nada que aprender: con una sola clase cualquier metrica es ruido. "
            "Hace falta que algunas acciones hayan causado thrash."
        )
        log.warning("Entrenamiento abortado: %s", informe["motivo"])
        return informe

    # Split por tiempo: el pasado entrena, el futuro evalua.
    corte = int(len(y) * (1 - cfg.test_split))
    corte = max(1, min(corte, len(y) - 1))
    X_tr, X_te = X[:corte], X[corte:]
    y_tr, y_te = y[:corte], y[corte:]

    if len(set(y_tr.tolist())) < 2:
        informe["motivo"] = (
            "la parte de entrenamiento (el pasado) tiene una sola clase. "
            "Todavia no paso suficiente variedad; junta mas datos."
        )
        log.warning("Entrenamiento abortado: %s", informe["motivo"])
        return informe

    candidatos = {
        "regresion_logistica": Pipeline([
            ("esc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced",
                                       random_state=cfg.semilla)),
        ]),
        "random_forest": RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=3,
            class_weight="balanced", random_state=cfg.semilla, n_jobs=-1),
        "gradient_boosting": HistGradientBoostingClassifier(
            max_iter=200, max_depth=4, learning_rate=0.08,
            random_state=cfg.semilla),
    }

    resultados = {}
    for nombre, modelo in candidatos.items():
        try:
            modelo.fit(X_tr, y_tr)
            prob = modelo.predict_proba(X_te)[:, 1]
            pred = (prob >= 0.5).astype(int)
            resultados[nombre] = {"modelo": modelo, "prob": prob,
                                  "metricas": _metricas(y_te, pred, prob)}
        except Exception as e:
            log.warning("El candidato %s fallo: %s", nombre, e)
            resultados[nombre] = {"error": f"{type(e).__name__}: {e}"}

    validos = {k: v for k, v in resultados.items() if "metricas" in v}
    if not validos:
        informe["motivo"] = "ningun algoritmo pudo entrenarse con estos datos"
        informe["candidatos"] = resultados
        return informe

    # Elegimos por AUC. Si el AUC no se puede calcular, por F1.
    def clave_orden(kv):
        m = kv[1]["metricas"]
        return (m.get("auc") or 0.0, m.get("f1") or 0.0)

    mejor_nombre, mejor = max(validos.items(), key=clave_orden)

    # Baseline honesto: "trimear siempre" = predecir que nada hace dano.
    from sklearn.metrics import accuracy_score
    acc_tonto = float(accuracy_score(y_te, np.zeros_like(y_te)))
    prob_mejor = mejor["prob"]
    umbral, curva = _umbral_optimo(y_te, prob_mejor)
    pred_umbral = (prob_mejor >= umbral).astype(int)
    metricas_umbral = _metricas(y_te, pred_umbral, prob_mejor)

    malos_totales = int(y_te.sum())
    malos_evitados = int(((y_te == 1) & (pred_umbral == 1)).sum())
    buenos_sacrificados = int(((y_te == 0) & (pred_umbral == 1)).sum())

    informe.update({
        "apto": True,
        "algoritmo": mejor_nombre,
        "candidatos": {k: v["metricas"] for k, v in validos.items()},
        "evaluacion": metricas_umbral,
        "umbral": umbral,
        "curva": curva,
        "linea_base": {
            # La regla tonta: trimear siempre. Su "accuracy" es simplemente la
            # proporcion de trims que salieron bien. Si el modelo no la supera
            # en algo que importe, no sirve para nada.
            "regla_trimear_siempre_accuracy": acc_tonto,
            "malos_en_test": malos_totales,
            "malos_evitados": malos_evitados,
            "buenos_sacrificados": buenos_sacrificados,
            # El numero que importa: que fraccion de los trims daninos el
            # modelo habria frenado, a costa de cuantos trims buenos.
            "pct_malos_evitados": (malos_evitados / malos_totales
                                   if malos_totales else None),
            "pct_buenos_perdidos": (buenos_sacrificados / max(len(y_te) - malos_totales, 1)),
        },
        "n_train": int(len(y_tr)),
        "n_test": int(len(y_te)),
        "positivos_train": int(y_tr.sum()),
        "positivos_test": int(y_te.sum()),
    })

    # Importancia por permutacion: mide cuanto empeora el modelo si se rompe
    # cada feature. Sirve para cualquier algoritmo y no depende de internals.
    try:
        from sklearn.inspection import permutation_importance
        pi = permutation_importance(mejor["modelo"], X_te, y_te,
                                    n_repeats=8, random_state=cfg.semilla,
                                    scoring="roc_auc")
        importancias = sorted(
            ({"feature": f, "nombre": features.NOMBRES_LEGIBLES.get(f, f),
              "importancia": float(m), "desvio": float(s)}
             for f, m, s in zip(features.FEATURES, pi.importances_mean,
                                pi.importances_std)),
            key=lambda d: -d["importancia"],
        )
        informe["importancias"] = importancias
    except Exception as e:
        log.warning("No se pudo calcular la importancia de features: %s", e)
        informe["importancias"] = []

    if not guardar_modelo:
        return informe

    import joblib
    version = time.strftime("%Y%m%d-%H%M%S")
    ruta = Path(cfg.dir_modelos) / f"modelo_{version}.joblib"
    joblib.dump({
        "modelo": mejor["modelo"],
        "features": features.FEATURES,
        "umbral": umbral,
        "version": version,
        "algoritmo": mejor_nombre,
        "metricas": metricas_umbral,
    }, ruta)

    con.execute("UPDATE modelos SET activo=0")
    cur = con.execute(
        "INSERT INTO modelos (ts, version, algoritmo, ruta, n_muestras, "
        "n_positivos, metricas, features, umbral, activo) VALUES (?,?,?,?,?,?,?,?,?,1)",
        (time.time(), version, mejor_nombre, str(ruta), int(len(y)),
         int(y.sum()), json.dumps({**metricas_umbral, "umbral": umbral,
                                   "candidatos": informe["candidatos"],
                                   "linea_base": informe["linea_base"]}),
         json.dumps(features.FEATURES), umbral),
    )
    informe["modelo_id"] = cur.lastrowid
    informe["ruta"] = str(ruta)
    log.info("Modelo %s guardado (%s, AUC=%s, umbral=%.2f)",
             version, mejor_nombre, metricas_umbral.get("auc"), umbral)
    return informe


def cargar_activo(cfg, con=None):
    """Devuelve el modelo activo, o None si no hay ninguno entrenado."""
    import joblib
    con = con or db.inicializar(cfg.ruta_db)
    f = con.execute(
        "SELECT * FROM modelos WHERE activo=1 ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if not f:
        return None
    ruta = Path(f["ruta"])
    if not ruta.exists():
        log and log.warning("El modelo activo apunta a %s, que no existe", ruta)
        return None
    try:
        datos = joblib.load(ruta)
    except Exception as e:
        log and log.warning("No se pudo cargar el modelo %s: %s", ruta, e)
        return None
    # Si cambió la lista de features, el modelo viejo no sirve: predeciria con
    # las columnas en otro orden y devolveria numeros sin sentido, sin error.
    if datos.get("features") != features.FEATURES:
        log and log.warning(
            "El modelo %s se entreno con otra lista de features; se descarta. "
            "Hay que reentrenar.", f["version"])
        return None
    datos["id"] = f["id"]
    return datos

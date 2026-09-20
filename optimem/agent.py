"""
El agente: decide que tocar y lo toca.

Tres modos, y el orden importa:

  observacion  No toca nada. Registra que habria hecho. Sirve para ver si el
               criterio tiene sentido antes de darle permiso de actuar.
  aprendizaje  Hace trims exploratorios acotados, solo para generar etiquetas.
               Sin esto no hay datos etiquetados y el modelo no se puede
               entrenar: nadie sabe de antemano que trims salen mal.
  activo       Usa el modelo entrenado. Es el unico modo que decide con datos.

Regla central, y es la que separa esto de un "liberador de RAM":

  EL AGENTE SOLO ACTUA BAJO PRESION. Si hay RAM de sobra, vaciar el working
  set de un proceso no libera nada util y solo agrega el riesgo de que ese
  proceso tenga que releer sus paginas. El agente es una respuesta a la
  presion, no un barredor que corre siempre.

Limites que no se pueden saltear: procesos protegidos, ventana activa,
procesos con ventana abierta, y los topes por hora.
"""
from __future__ import annotations

import random
import time
from collections import defaultdict, deque

from . import db, evaluator, features, winapi

log = None

# Cuanta historia por proceso guardamos en memoria para armar features.
MAX_HISTORIAL = 40
EDAD_MAX_HISTORIAL = 900.0   # 15 min


class Agente:
    def __init__(self, cfg, con, modelo=None):
        self.cfg = cfg
        self.con = con
        self.modelo = modelo
        self.historial: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORIAL))
        self.ultimo_sistema: dict = {}
        self._t_ultima_exploracion = 0.0
        self._rnd = random.Random(cfg.semilla)
        self._ultimo_ciclo = 0.0
        self.n_decisiones = 0
        self.n_acciones = 0

    # ------------------------------------------------------------------
    def alimentar(self, crudos: list[dict], sistema: dict) -> None:
        """
        Recibe el ciclo del recolector y guarda historia para armar features.

        Se guarda aunque el agente no actue: los modos observacion y
        aprendizaje necesitan la misma ventana de datos que el modo activo.
        """
        self.ultimo_sistema = sistema or {}
        ahora = time.time()
        for p in crudos:
            self.historial[p["clave"]].append({
                "ts": ahora,
                "ws": p.get("ws"),
                "fallos_pagina": p.get("fallos"),
                "cpu_pct": p.get("cpu_pct"),
            })
        # Poda por edad para que esto no crezca durante una semana.
        if len(self.historial) > 3000:
            for clave in list(self.historial):
                h = self.historial[clave]
                if not h or ahora - h[-1]["ts"] > EDAD_MAX_HISTORIAL:
                    del self.historial[clave]

    # ------------------------------------------------------------------
    def _hay_presion(self, m) -> tuple[bool, str]:
        """
        Hay motivo para intervenir?

        Sin presion no se toca nada. Es la diferencia entre optimizar y
        romper cosas al pedo.
        """
        if m is None:
            return False, "sin datos de memoria"
        libre_pct = m.disponible / m.total if m.total else 1.0
        commit_pct = m.presion_commit
        if libre_pct < self.cfg.presion_ram_libre_min:
            return True, f"RAM libre {libre_pct * 100:.0f}% < {self.cfg.presion_ram_libre_min * 100:.0f}%"
        if commit_pct > self.cfg.presion_commit_max:
            return True, f"commit {commit_pct * 100:.0f}% > {self.cfg.presion_commit_max * 100:.0f}%"
        return False, f"sin presion (RAM libre {libre_pct * 100:.0f}%, commit {commit_pct * 100:.0f}%)"

    # ------------------------------------------------------------------
    def _presupuesto(self) -> tuple[int, int]:
        """Cuantas acciones y cuantos MB quedan en la hora corriente."""
        hace_una_hora = time.time() - 3600
        f = self.con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(liberado),0) mb FROM acciones "
            "WHERE ts > ? AND autor != 'manual'",
            (hace_una_hora,),
        ).fetchone()
        return (max(0, self.cfg.max_acciones_por_hora - (f["n"] or 0)),
                max(0, self.cfg.max_liberado_por_hora_mb - int((f["mb"] or 0) / 1024**2)))

    def _en_espera(self, clave: str) -> bool:
        """Ya tocamos este proceso hace poco? No insistir."""
        f = self.con.execute(
            "SELECT MAX(ts) t FROM acciones WHERE clave=?", (clave,)
        ).fetchone()
        if not f or not f["t"]:
            return False
        return (time.time() - f["t"]) < self.cfg.cooldown_min_por_proceso * 60

    # ------------------------------------------------------------------
    def _candidatos(self, crudos: list[dict]) -> list[dict]:
        """Procesos que se pueden tocar, ordenados por tamano."""
        out = []
        for p in crudos:
            if p.get("categoria") not in ("libre", "vigilado"):
                continue
            ws_mb = (p.get("ws") or 0) / 1024**2
            if ws_mb < self.cfg.ws_minimo_mb:
                continue
            if self._en_espera(p["clave"]):
                continue
            out.append(p)
        out.sort(key=lambda p: -(p.get("ws") or 0))
        return out

    # ------------------------------------------------------------------
    def _contexto(self, p: dict) -> dict | None:
        h = list(self.historial.get(p["clave"], []))
        if len(h) < 3:
            return None
        h.reverse()   # el mas nuevo primero, igual que la consulta SQL
        ds = dict(self.ultimo_sistema)
        ds["lecturas_disco_min"] = self._lecturas_min()
        return features.contexto_en_vivo(
            self.cfg,
            datos_proceso={"ws": p.get("ws"), "ws_pico": p.get("ws_pico"),
                           "commit": p.get("commit"), "hilos": p.get("hilos"),
                           "create_time": p.get("create_time")},
            datos_sistema=ds,
            historial=h,
            categoria=p.get("categoria", "libre"),
        )

    def _lecturas_min(self) -> float:
        """Paginas leidas de disco por minuto, segun las ultimas muestras."""
        f = self.con.execute(
            "SELECT ts, lecturas_pagina FROM muestras_sistema "
            "WHERE lecturas_pagina IS NOT NULL ORDER BY ts DESC LIMIT 2"
        ).fetchall()
        if len(f) != 2:
            return 0.0
        dt_min = (f[0]["ts"] - f[1]["ts"]) / 60.0
        if dt_min <= 0.001:
            return 0.0
        return max(0.0, (f[0]["lecturas_pagina"] - f[1]["lecturas_pagina"]) / dt_min)

    # ------------------------------------------------------------------
    def _predecir(self, ctx: dict) -> float | None:
        """Probabilidad de que trimear este proceso cause thrash."""
        if not self.modelo:
            return None
        try:
            x = [features.vector(ctx)]
            return float(self.modelo["modelo"].predict_proba(x)[0][1])
        except Exception as e:
            if log:
                log.warning("Fallo la prediccion: %s", e)
            return None

    # ------------------------------------------------------------------
    def _actuar_trim(self, p: dict, autor: str, p_thrash: float | None) -> bool:
        """Trimea y registra. Devuelve True si efectivamente libero memoria."""
        clave, pid = p["clave"], p["pid"]
        ws_antes = p.get("ws")
        fallos_antes = p.get("fallos")

        ok, liberado = winapi.trimear(pid)
        if not ok:
            db.registrar_evento(
                self.con, "bloqueo_accion", "aviso", clave=clave, pid=pid,
                nombre=p.get("nombre"),
                detalle="el sistema rechazo el trim (permisos o proceso protegido)")
            return False

        ws_despues = (ws_antes - liberado) if ws_antes else None
        db.registrar_accion(
            self.con, clave=clave, pid=pid, nombre=p.get("nombre") or "",
            tipo="trim", ws_antes=ws_antes, ws_despues=ws_despues,
            liberado=liberado, fallos_antes=fallos_antes,
            modelo_id=self.modelo["id"] if self.modelo else None,
            p_thrash=p_thrash, autor=autor,
        )
        self.n_acciones += 1
        if log:
            log.info("Trim %s pid=%s %s: libero %.1f MB%s",
                     autor, pid, p.get("nombre"), liberado / 1024**2,
                     f" (P(thrash)={p_thrash:.2f})" if p_thrash is not None else "")
        return True

    def _actuar_prioridad(self, p: dict, nivel: str, p_thrash: float | None) -> bool:
        """
        Palanca suave: le dice a Windows que desaloje este proceso primero.

        No fuerza nada ahora. El sistema desaloja solo las paginas que
        realmente estan frias, cuando necesita RAM. Es lo que corresponde
        cuando no estamos seguros: deja decidir al que tiene la informacion.
        """
        ok = winapi.fijar_prioridad_memoria(p["pid"], nivel)
        if not ok:
            return False
        db.registrar_accion(
            self.con, clave=p["clave"], pid=p["pid"], nombre=p.get("nombre") or "",
            tipo="prioridad", nivel=nivel, ws_antes=p.get("ws"),
            fallos_antes=p.get("fallos"),
            modelo_id=self.modelo["id"] if self.modelo else None,
            p_thrash=p_thrash, autor="agente",
        )
        self.n_acciones += 1
        if log:
            log.info("Prioridad de memoria -> %s en pid=%s %s (P(thrash)=%s)",
                     nivel, p["pid"], p.get("nombre"),
                     f"{p_thrash:.2f}" if p_thrash is not None else "sin modelo")
        return True

    # ------------------------------------------------------------------
    def ciclo(self, crudos: list[dict], sistema) -> dict:
        """
        Un ciclo de decision. Devuelve un resumen para el panel.
        """
        res = {"ts": time.time(), "modo": self.cfg.modo, "acciones": 0,
               "evaluadas": 0, "motivo": "", "candidatos": 0}
        self._ultimo_ciclo = res["ts"]

        # Siempre cerramos las acciones vencidas, aunque no actuemos: las
        # etiquetas se necesitan para poder entrenar.
        res["evaluadas"] = evaluator.cerrar_acciones(self.con, self.cfg)

        modo = self.cfg.modo if self.cfg.agente_activo else "observacion"
        if modo == "observacion":
            res["motivo"] = "modo observacion: no se toca nada"
            return res

        presion, motivo = self._hay_presion(sistema)
        res["motivo"] = motivo
        if not presion:
            return res

        candidatos = self._candidatos(crudos)
        res["candidatos"] = len(candidatos)
        if not candidatos:
            res["motivo"] = motivo + " | sin candidatos que cumplan los minimos"
            return res

        acciones_restantes, mb_restantes = self._presupuesto()
        if acciones_restantes <= 0 or mb_restantes <= 0:
            res["motivo"] = motivo + " | tope por hora alcanzado"
            return res

        if modo == "aprendizaje":
            self._ciclo_exploracion(candidatos, res)
        else:
            self._ciclo_activo(candidatos, res, acciones_restantes, mb_restantes)

        return res

    # ------------------------------------------------------------------
    def _ciclo_exploracion(self, candidatos: list[dict], res: dict) -> None:
        """
        Un trim exploratorio cada tanto, para generar etiquetas.

        Se elige entre los procesos grandes con azar ponderado: si siempre
        eligieramos el mas grande, el modelo solo aprenderia de ese tipo de
        proceso y no sabria nada de los demas.
        """
        ahora = time.time()
        if ahora - self._t_ultima_exploracion < self.cfg.exploracion_cada_seg:
            res["motivo"] += " | esperando el proximo turno de exploracion"
            return

        elegibles = [
            p for p in candidatos
            if self.cfg.exploracion_ws_min_mb <= (p.get("ws") or 0) / 1024**2
            <= self.cfg.exploracion_ws_max_mb
        ]
        if not elegibles:
            res["motivo"] += " | ningun proceso en el rango de exploracion"
            return

        # Descartamos los que no van a poder etiquetarse: si el proceso viene
        # con una tasa de fallos casi nula, la regla de etiquetado lo va a
        # descartar como ambiguo (no se puede saber si se activo por el trim o
        # porque le tocaba). Medido: de 9 trims exploratorios, 5 se perdieron
        # asi. Elegir mejor el blanco no debilita la regla, solo deja de
        # gastar experimentos en casos que no van a enseñar nada.
        from .evaluator import FALLO_BASE_MINIMO
        con_base = []
        for p in elegibles:
            ctx = self._contexto(p)
            if ctx and ctx.get("_fallos_por_min", 0) >= FALLO_BASE_MINIMO:
                con_base.append(p)
        if not con_base:
            res["motivo"] += (" | ningun candidato con actividad medible: "
                              "no valdria la pena, se descartaria")
            return
        elegibles = con_base

        # Azar ponderado por tamano, con piso para que los chicos tengan chance.
        pesos = [max(1.0, (p.get("ws") or 0) / 1024**2) for p in elegibles]
        elegido = self._rnd.choices(elegibles, weights=pesos, k=1)[0]

        ctx = self._contexto(elegido)
        p_thrash = self._predecir(ctx) if ctx else None

        if self._actuar_trim(elegido, "exploracion", p_thrash):
            self._t_ultima_exploracion = ahora
            res["acciones"] = 1
            res["motivo"] += f" | trim exploratorio en {elegido.get('nombre')}"

    # ------------------------------------------------------------------
    def _ciclo_activo(self, candidatos: list[dict], res: dict,
                      acciones_restantes: int, mb_restantes: int) -> None:
        """
        Decision con el modelo. De mayor a menor tamano, hasta agotar el
        presupuesto de la hora.
        """
        if not self.modelo:
            res["motivo"] += (" | modo activo pero sin modelo entrenado: "
                              "nada que decidir")
            return

        umbral = self.modelo.get("umbral") or 0.5
        decididos = 0
        for p in candidatos:
            if decididos >= acciones_restantes:
                break
            ws_mb = (p.get("ws") or 0) / 1024**2
            if ws_mb > mb_restantes:
                continue

            ctx = self._contexto(p)
            if ctx is None:
                continue
            p_thrash = self._predecir(ctx)
            if p_thrash is None:
                continue

            self.n_decisiones += 1
            decididos += 1

            if p_thrash >= umbral:
                # El modelo dice que este trim sale mal. No se hace.
                db.registrar_evento(
                    self.con, "decision_frenada", "info", clave=p["clave"],
                    pid=p["pid"], nombre=p.get("nombre"),
                    detalle=(f"P(thrash)={p_thrash:.2f} >= {umbral:.2f}: "
                             f"se evito un swap innecesario"),
                )
                continue

            if p_thrash <= self.cfg.umbral_seguridad and not self.cfg.solo_prioridad:
                if self._actuar_trim(p, "agente", p_thrash):
                    res["acciones"] += 1
                    mb_restantes -= ws_mb
            elif p_thrash <= self.cfg.umbral_prioridad:
                # Zona gris: no lo trimеamos, pero le decimos a Windows que lo
                # prefiera para desalojo. La decision fina la toma el sistema.
                if self._actuar_prioridad(p, "low", p_thrash):
                    res["acciones"] += 1

    # ------------------------------------------------------------------
    def sombra(self, crudos: list[dict], sistema) -> list[dict]:
        """
        Que habria hecho el agente, sin hacerlo.

        Sirve para el modo observacion: ver el criterio en accion antes de
        darle permiso de tocar memoria. Se guarda como eventos, no como
        acciones, porque si no contaminaria las etiquetas con trims que
        nunca ocurrieron.
        """
        salida = []
        presion, _ = self._hay_presion(sistema)
        if not presion:
            return salida
        for p in self._candidatos(crudos)[:8]:
            ctx = self._contexto(p)
            if ctx is None:
                continue
            p_thrash = self._predecir(ctx)
            salida.append({
                "nombre": p.get("nombre"), "pid": p["pid"],
                "ws_mb": round((p.get("ws") or 0) / 1024**2, 1),
                "p_thrash": p_thrash,
                "harria": ("trim" if p_thrash is not None and p_thrash <= self.cfg.umbral_seguridad
                           else "prioridad" if p_thrash is not None and p_thrash <= self.cfg.umbral_prioridad
                           else "nada"),
            })
        return salida

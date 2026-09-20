"""
Recolector de metricas.

Tiene que poder correr una semana sin morirse. Reglas que sigue:

  - Ningun error de un proceso puntual puede tumbar el ciclo. psutil tira
    NoSuchProcess/AccessDenied todo el tiempo: se saltean, no se propagan.
  - El bucle usa tiempo absoluto, no `sleep(intervalo)`. Si un ciclo tarda de
    mas, el siguiente no se atrasa arrastrando el error.
  - Se registran eventos de inicio y fin para que despues se pueda distinguir
    "no habia presion" de "el recolector estaba caido". Esa diferencia es
    justo la que no se podia ver en otros pipelines.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import psutil

from . import critical, db, winapi

log = None  # se inicializa en correr()


def _clave(pid: int, crear: float | None) -> str:
    """
    Identidad estable de un proceso.

    Los PIDs se reutilizan. Si indexaramos solo por PID, la serie de un proceso
    muerto se fusionaria con la de uno nuevo y las etiquetas del modelo
    quedarian contaminadas en silencio. El create_time los separa.
    """
    return f"{pid}:{int(crear or 0)}"


class Recolector:
    def __init__(self, cfg, con):
        self.cfg = cfg
        self.con = con
        self.detector = critical.DetectorCriticos(cfg)
        self._parar = False

        self._t_sistema = 0.0
        self._t_procesos = 0.0
        self._t_sonda = 0.0
        self._t_limpieza = 0.0
        self._t_agente = 0.0
        self._t_base = 0.0
        self._t_avisos = 0.0

        # El agente vive en este mismo proceso a proposito: necesita la misma
        # ventana de historia que estamos recolectando. Si corriera aparte,
        # tendria que releer la base y las features se armarian con datos
        # distintos a los del entrenamiento.
        from . import agent as _agente
        self.agente = _agente.Agente(cfg, con)
        self._contexto_agente: dict = {}
        self._ultimos_crudos: list[dict] = []
        self._memoria_actual = None

        # estado para deltas
        self._cpu_prev: dict[str, tuple[float, float]] = {}   # clave -> (cpu_tot, ts)
        self._io_prev: tuple[float, float, float] | None = None  # (lect, esc, ts)
        self._fallos_prev: dict[str, int] = {}                # clave -> fallos acumulados

        self._n_sistema = 0
        self._n_procesos = 0

    # ------------------------------------------------------------------
    def _parar_handler(self, *_):
        self._parar = True
        if log:
            log.info("Senal de parada recibida; cerrando el ciclo en curso...")

    # ------------------------------------------------------------------
    def ciclo_sistema(self) -> None:
        m = winapi.memoria_sistema()
        if m is None:
            return
        cpu = psutil.cpu_percent(interval=None)

        lect_bps = esc_bps = None
        try:
            io = psutil.disk_io_counters()
            ahora = time.time()
            if io and self._io_prev:
                l0, e0, t0 = self._io_prev
                dt = max(ahora - t0, 0.001)
                lect_bps = max(0.0, (io.read_bytes - l0) / dt)
                esc_bps = max(0.0, (io.write_bytes - e0) / dt)
            if io:
                self._io_prev = (io.read_bytes, io.write_bytes, ahora)
        except Exception:
            pass

        self.con.execute(
            "INSERT OR REPLACE INTO muestras_sistema VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), m.total, m.disponible, m.en_uso, m.commit_total,
             m.commit_limite, m.cache_sistema, m.pool_paginado, m.pool_no_paginado,
             m.fallos_pagina, m.lecturas_pagina, m.io_lecturas_pagina,
             m.procesos, m.hilos, cpu, lect_bps, esc_bps),
        )
        self._n_sistema += 1
        # El agente arma features con el estado del sistema; guardamos el
        # ultimo para que lo use en su propio ciclo.
        self._contexto_agente = {
            "ram_total": m.total, "ram_disponible": m.disponible,
            "commit_total": m.commit_total, "commit_limite": m.commit_limite,
            "cache_sistema": m.cache_sistema, "procesos": m.procesos,
            "disco_lect_bps": lect_bps or 0.0,
        }
        self._memoria_actual = m

    # ------------------------------------------------------------------
    def ciclo_procesos(self) -> list[dict]:
        """Muestrea todos los procesos. Devuelve la lista cruda del ciclo."""
        ahora = time.time()
        self.detector.refrescar_ventanas()
        filas = []
        crudos = []
        vivo: set[str] = set()

        attrs = ["pid", "name", "memory_info", "cpu_times", "create_time",
                 "num_threads", "username"]
        for p in psutil.process_iter(attrs):
            try:
                info = p.info
                pid = info["pid"]
                if info["memory_info"] is None:
                    continue
                clave = _clave(pid, info["create_time"])
                vivo.add(clave)

                ws = info["memory_info"].rss
                mp = winapi.memoria_proceso(pid)
                fallos = mp.fallos_pagina if mp else None
                ws_pico = mp.working_set_pico if mp else ws
                commit = mp.commit if mp else 0

                # CPU por delta de cpu_times: mas estable que
                # Process.cpu_percent(), que pierde estado al recrear objetos.
                ct = info["cpu_times"]
                cpu_pct = None
                if ct is not None:
                    total_cpu = ct.user + ct.system
                    prev = self._cpu_prev.get(clave)
                    if prev:
                        dt = max(ahora - prev[1], 0.001)
                        cpu_pct = max(0.0, (total_cpu - prev[0]) / dt * 100.0)
                    self._cpu_prev[clave] = (total_cpu, ahora)

                cls = self.detector.clasificar(
                    pid, info["name"] or "", usuario=info.get("username"),
                    crear_time=info["create_time"],
                )

                # Leer la prioridad de memoria cuesta un OpenProcess por
                # proceso. Solo la consultamos donde importa: los candidatos
                # grandes. En el resto queda vacio, no en cero, para no
                # confundir "no lo mire" con "no la tiene puesta".
                prioridad = None
                if ws > 100 * 1024**2:
                    prioridad = winapi.leer_prioridad_memoria(pid)

                # La linea de comandos solo se lee para los procesos que
                # vigilamos: es la unica forma de distinguir "el mismo script
                # muriendo en loop" de "cuatro scripts distintos", y leerla
                # para los 420 procesos seria caro al pedo.
                cmdline = None
                if critical._normalizar(info["name"] or "") in self.detector.vigilados:
                    try:
                        cmdline = p.cmdline()
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        cmdline = None

                filas.append((
                    ahora, clave, pid, info["name"], ws, ws_pico, commit, fallos,
                    cpu_pct, info["num_threads"], prioridad,
                    1 if cls.es_critico else 0, cls.motivo, cls.categoria,
                    1 if pid == self.detector._pid_foco else 0,
                ))
                crudos.append({
                    "clave": clave, "pid": pid,
                    "nombre": info["name"] or "", "ws": ws,
                    "cpu_pct": cpu_pct, "fallos": fallos,
                    "create_time": info["create_time"],
                    "critico": cls.es_critico, "motivo": cls.motivo,
                    "categoria": cls.categoria,
                    "usuario": info.get("username"),
                    "cmdline": cmdline,
                    # El propio OptiMem y su sonda no se vigilan a si mismos:
                    # la sonda es un python que arranca y muere por diseño.
                    "es_propio": pid in self._pids_propios(),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        if filas:
            self.con.executemany(
                "INSERT OR REPLACE INTO muestras_proceso VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                filas,
            )
        self._n_procesos += len(filas)

        # Limpieza de estado de procesos que ya no estan, para que los
        # diccionarios de deltas no crezcan sin limite durante una semana.
        if len(self._cpu_prev) > 4000:
            self._cpu_prev = {k: v for k, v in self._cpu_prev.items() if k in vivo}
            self._fallos_prev = {k: v for k, v in self._fallos_prev.items() if k in vivo}

        # Alimentamos al agente con este ciclo. Se hace siempre, tambien en
        # modo observacion: sin la ventana de historia no se pueden armar las
        # features ni para decidir ni para mostrar que se habria hecho.
        try:
            self.agente.alimentar(crudos, self._contexto_agente)
        except Exception as e:
            log.warning("El agente no pudo procesar el ciclo: %s", e)

        # Vigilancia de los procesos que no deben morir. Excluimos los
        # nuestros: la sonda arranca y muere por diseño y no es un crash.
        for m in self.detector.vigilar(crudos, excluir_pids=self._pids_propios()):
            log.warning("Proceso vigilado caido: %s (pid %s) - %s",
                        m.nombre, m.pid, m.detalle)
            db.registrar_evento(
                self.con, "muerte_proceso",
                "grave" if m.grave else "aviso",
                clave=m.clave, pid=m.pid, nombre=m.nombre, detalle=m.detalle,
            )
        return crudos

    # ------------------------------------------------------------------
    def _arrancar_sonda(self) -> None:
        """
        Lanza la sonda como proceso aparte.

        Tiene que ser un proceso aparte que VIVE: la sonda necesita mantener
        su propio working set entre mediciones para poder detectar que el
        sistema se lo llevo. Si midieramos dentro del recolector, el buffer
        seria de este proceso y no podriamos distinguir "me sacaron las
        paginas a mi" de "al sistema le falta memoria".
        """
        if not self.cfg.sonda_activa:
            return
        try:
            self._sonda_proc = subprocess.Popen(
                [sys.executable, "-m", "optimem.probe", "--vivo"],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log.info("Sonda viva lanzada (pid %s)", self._sonda_proc.pid)
        except Exception as e:
            self._sonda_proc = None
            log.warning("No se pudo lanzar la sonda: %s", e)

    def _supervisar_sonda(self) -> None:
        """Si la sonda se murio, se relanza. Una sonda caida deja el informe sin datos."""
        if not self.cfg.sonda_activa:
            return
        p = getattr(self, "_sonda_proc", None)
        if p is not None and p.poll() is None:
            return
        if p is not None:
            log.warning("La sonda murio (rc=%s); relanzando", p.returncode)
            db.registrar_evento(self.con, "error", "aviso",
                                detalle=f"sonda viva termino con codigo {p.returncode}")
        self._arrancar_sonda()

    def _detener_sonda(self) -> None:
        p = getattr(self, "_sonda_proc", None)
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        log.info("Sonda viva detenida")

    # ------------------------------------------------------------------
    def _establecer_linea_base(self) -> None:
        """
        Fija la linea de base del indice con las primeras sondas.

        Sin esto el indice no es comparable consigo mismo y el "% de mejora"
        no significaria nada. Se usa la MEDIANA y no el promedio: una corrida
        mala al arranque (antivirus escaneando, indexador) no debe fijar la
        referencia para siempre.
        """
        # Se piden los campos que la sonda viva escribe de verdad. Antes esto
        # filtraba por t_disco_ms, que quedo en desuso: la condicion no la
        # cumplia ninguna fila y la linea de base no se fijaba nunca, en
        # silencio y sin ningun error.
        filas = self.con.execute(
            "SELECT t_cache_ms, t_ws_ms, t_cpu_ms, t_memoria_ms FROM sondas "
            "WHERE t_cache_ms > 0 AND t_ws_ms > 0 AND t_cpu_ms > 0 "
            "ORDER BY ts LIMIT 40"
        ).fetchall()
        if len(filas) < 10:
            return
        base = {}
        for campo, clave in (("t_cache_ms", "cache"), ("t_ws_ms", "ws"),
                             ("t_cpu_ms", "cpu"), ("t_memoria_ms", "memoria")):
            vals = sorted(f[campo] for f in filas if f[campo] and f[campo] > 0)
            if vals:
                base[clave] = vals[len(vals) // 2]
        db.guardar_estado(self.con, "linea_base_sonda", base)
        db.guardar_estado(self.con, "linea_base_ts", time.time())
        log.info("Linea de base fijada con %d sondas: %s", len(filas),
                 {k: round(v, 2) for k, v in base.items()})

    def _pids_propios(self) -> set[int]:
        """Nuestro PID y el de la sonda. No se vigilan ni se tocan a si mismos."""
        s = {os.getpid()}
        p = getattr(self, "_sonda_proc", None)
        if p is not None and p.poll() is None:
            s.add(p.pid)
        return s

    # ------------------------------------------------------------------
    def _ciclo_agente(self) -> None:
        """Corre un ciclo del agente. Nunca puede tumbar al recolector."""
        if not self._ultimos_crudos:
            return
        try:
            # Recargamos el modelo si hay uno nuevo: asi se puede entrenar
            # mientras el recolector sigue corriendo, sin reiniciar nada.
            from . import trainer
            activo = trainer.cargar_activo(self.cfg, self.con)
            if activo and (not self.agente.modelo
                           or activo["id"] != self.agente.modelo.get("id")):
                self.agente.modelo = activo
                log.info("Modelo cargado: %s (%s)", activo["version"],
                         activo["algoritmo"])

            res = self.agente.ciclo(self._ultimos_crudos, self._memoria_actual)
            self._ultimo_ciclo_agente = res
            if res.get("acciones"):
                log.info("Agente: %s acciones. %s", res["acciones"], res.get("motivo"))
        except Exception as e:
            log.exception("El ciclo del agente fallo: %s", e)
            db.registrar_evento(self.con, "error", "aviso",
                                detalle=f"agente: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def _revisar_avisos(self) -> None:
        """
        Avisa por Telegram cuando ya hay etiquetas suficientes para entrenar.

        Se revisa seguido y no una vez por hora: cruzar el umbral es el evento
        que el usuario esta esperando, y no tiene sentido que se entere hasta
        una hora despues. Es idempotente, asi que revisar de mas no molesta.
        """
        if not self.cfg.avisos_activos:
            return
        try:
            from . import notificar
            notificar.log = log
            if notificar.avisar_si_corresponde(self.cfg, self.con):
                log.info("Aviso de umbral enviado por Telegram")
        except Exception as e:
            # Un aviso que falla no puede tumbar una recoleccion de horas.
            log.warning("No se pudo revisar el aviso: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------
    def ciclo_limpieza(self) -> None:
        b = db.limpiar_viejos(self.con, self.cfg.retencion_dias)
        if any(b.values()):
            log.info("Limpieza: %s", b)
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ------------------------------------------------------------------
    def correr(self, duracion_seg: float | None = None) -> None:
        global log
        from . import config as _cfg
        log = _cfg.log("optimem.recolector")

        signal.signal(signal.SIGINT, self._parar_handler)
        try:
            signal.signal(signal.SIGTERM, self._parar_handler)
        except (AttributeError, ValueError):
            pass

        log.info("Recolector iniciado. Datos en %s", self.cfg.dir)
        log.info("Capacidades: %s", winapi.detectar_capacidades().detalle)
        db.registrar_evento(self.con, "inicio", "info", detalle="recolector")
        self._sonda_proc = None
        self._arrancar_sonda()

        # Primera pasada inmediata.
        self._fallos_prev.clear()
        t0 = time.time()

        try:
            while not self._parar:
                ahora = time.time()
                if duracion_seg and (ahora - t0) >= duracion_seg:
                    break

                try:
                    if ahora - self._t_sistema >= self.cfg.intervalo_sistema_seg:
                        self.ciclo_sistema()
                        self._t_sistema = ahora
                    if ahora - self._t_procesos >= self.cfg.intervalo_procesos_seg:
                        self._ultimos_crudos = self.ciclo_procesos()
                        self._t_procesos = ahora
                    if ahora - self._t_agente >= self.cfg.intervalo_agente_seg:
                        self._t_agente = ahora
                        self._ciclo_agente()
                    if ahora - self._t_sonda >= 30:
                        self._t_sonda = ahora
                        self._supervisar_sonda()
                    if ahora - self._t_avisos >= 120:
                        self._t_avisos = ahora
                        self._revisar_avisos()
                    if ahora - self._t_base >= 60:
                        self._t_base = ahora
                        # Se intenta seguido y no una vez por hora: hasta que
                        # no hay linea de base, el indice devuelve la suma
                        # cruda de milisegundos, que no es comparable consigo
                        # misma y hace que el informe no pueda decir nada.
                        if not db.leer_estado(self.con, "linea_base_sonda"):
                            self._establecer_linea_base()
                    if ahora - self._t_limpieza >= 3600:
                        self._t_limpieza = ahora
                        self.ciclo_limpieza()
                except Exception as e:
                    # Un ciclo roto no puede matar una recoleccion de una semana.
                    log.exception("Error en el ciclo: %s", e)
                    db.registrar_evento(self.con, "error", "aviso",
                                        detalle=f"ciclo: {type(e).__name__}: {e}")

                time.sleep(0.2)
        finally:
            self._detener_sonda()
            db.registrar_evento(self.con, "fin", "info", detalle="recolector")
            log.info("Recolector detenido. Muestras: %d sistema, %d proceso",
                     self._n_sistema, self._n_procesos)


def correr(cfg=None, duracion_seg: float | None = None) -> None:
    from . import config as _cfg
    cfg = cfg or _cfg.cargar()
    con = db.inicializar(cfg.ruta_db)
    Recolector(cfg, con).correr(duracion_seg=duracion_seg)


if __name__ == "__main__":
    correr()

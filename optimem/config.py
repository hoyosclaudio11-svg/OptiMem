"""
Configuracion de OptiMem.

El archivo config.json vive en la raiz del proyecto y se puede editar a mano.
Los datos (base de datos, modelos, logs) van por defecto a %LOCALAPPDATA%,
NO a la carpeta del proyecto: la base se escribe cada segundo y OneDrive
sincronizaria un archivo en cambio permanente, quemando CPU y ancho de banda,
con riesgo de corromper SQLite. Si preferis tenerla a mano, cambia
"directorio_datos" en config.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_CONFIG = RAIZ / "config.json"


def _dir_datos_por_defecto() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(RAIZ)
    return str(Path(base) / "OptiMem")


@dataclass
class Config:
    # --- donde vive todo ---
    directorio_datos: str = field(default_factory=_dir_datos_por_defecto)

    # --- recoleccion ---
    intervalo_sistema_seg: float = 1.0      # muestreo de memoria global
    intervalo_procesos_seg: float = 10.0    # muestreo por proceso (mas caro)

    # --- sonda de tiempo de respuesta (la metrica de exito) ---
    sonda_activa: bool = True
    intervalo_sonda_seg: float = 60.0
    sonda_mb_memoria: int = 48              # tamano del buffer a fallar
    sonda_mb_disco: int = 4                 # lectura sin cache
    sonda_iteraciones_cpu: int = 3_000_000

    # --- agente ---
    agente_activo: bool = False             # arranca apagado: primero observar
    modo: str = "observacion"               # observacion | aprendizaje | activo
    intervalo_agente_seg: float = 30.0
    ws_minimo_mb: int = 120                 # no tocamos procesos chicos: no vale la pena
    max_ws_a_liberar_mb: int = 400          # tope por proceso y por ciclo
    max_acciones_por_hora: int = 20
    max_liberado_por_hora_mb: int = 1500
    umbral_seguridad: float = 0.35          # si P(thrash) <= esto, se trimea
    umbral_prioridad: float = 0.55          # entre ambos: solo baja prioridad
    solo_prioridad: bool = False            # si True nunca trimea, solo prioridad
    cooldown_min_por_proceso: int = 10      # no insistir con el mismo proceso

    # --- umbral de presion ---
    # El agente SOLO actua si la memoria esta bajo presion. Con RAM de sobra,
    # vaciar un working set no libera nada util y solo agrega el riesgo de que
    # el proceso tenga que releer. Esto es lo que separa a OptiMem de un
    # liberador de RAM de esos que empeoran todo.
    # Medido en esta maquina: la RAM libre vive entre el 17% y el 23%. Con el
    # umbral en 0.20 el agente casi nunca se activaria y el proyecto quedaria
    # muerto en la practica. 0.25 entra en accion sin volverse agresivo.
    presion_ram_libre_min: float = 0.25
    presion_commit_max: float = 0.85        # o si el commit pasa el 85% del limite

    # --- exploracion (genera las etiquetas que el modelo necesita) ---
    # Sin exploracion no hay datos etiquetados y no se puede entrenar nada.
    exploracion_activa: bool = True
    # Una exploracion cada 5 min = 12/hora. Con el filtro de candidatos con
    # actividad medible, la mayoria produce etiqueta usable, asi que junta las
    # 60 que hacen falta para entrenar en ~6 h en vez de varios dias.
    # El tope real lo siguen poniendo max_acciones_por_hora y
    # max_liberado_por_hora_mb. Subilo si te molesta el ruido de fondo.
    exploracion_cada_seg: float = 300.0
    exploracion_ws_min_mb: int = 150
    exploracion_ws_max_mb: int = 600

    # --- evaluacion de resultados ---
    ventana_evaluacion_seg: float = 90.0    # cuanto miramos despues de actuar
    retardo_evaluacion_seg: float = 6.0     # ignoramos el pico inmediato del trim
    ventana_base_seg: float = 120.0         # linea de base previa, para comparar
    umbral_reincidencia: float = 2.0        # post/base por encima de esto = thrash
    min_fallos_para_etiqueta: int = 400     # evita etiquetar sobre numeros ruidosos

    # --- entrenamiento ---
    min_muestras_entrenar: int = 60
    test_split: float = 0.25
    semilla: int = 42

    # --- procesos criticos ---
    # Nombres que NUNCA se tocan. Se comparan en minusculas, sin .exe opcional.
    # Ya vienen los servicios del ecosistema: si alguno no lo tenes corriendo,
    # no molesta.
    procesos_protegidos: list[str] = field(default_factory=lambda: [
        # nucleo de Windows (igual no son accesibles sin admin)
        "system", "registry", "memory compression", "memcompression",
        "smss", "csrss", "wininit", "winlogon", "services", "lsass",
        "dwm", "fontdrvhost", "sihost", "ctfmon", "runtimebroker",
        "msmpeng", "nissrv", "securityhealthservice", "sgrmbroker",
        "audiodg", "conhost", "wudfhost", "spoolsv",
        # tu ecosistema: matarlos rompe cosas
        "terminal64",           # MetaTrader 5
        "metaeditor64",
        "python",               # Cerebro, publicadores, servicios
        "pythonw",
        "node",                 # FreeLLMAPI y paneles
        "tailscaled",
        "onedrive",
    ])
    # Procesos que ademas queremos VIGILAR: si desaparecen, avisar.
    vigilar: list[str] = field(default_factory=lambda: [
        "terminal64", "node", "python", "tailscaled",
    ])
    # Comando para reanimar un servicio caido, por nombre de proceso.
    # Ej: {"node": "C:\\ruta\\a\\FreeLLMAPI\\iniciar.bat"}
    reanimar: dict = field(default_factory=dict)
    auto_reanimar: bool = False

    # --- panel ---
    host: str = "127.0.0.1"
    puerto: int = 5215

    # --- log ---
    nivel_log: str = "INFO"
    retencion_dias: int = 21

    @property
    def dir(self) -> Path:
        p = Path(self.directorio_datos)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def ruta_db(self) -> Path:
        return self.dir / "optimem.db"

    @property
    def dir_modelos(self) -> Path:
        p = self.dir / "modelos"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def ruta_log(self) -> Path:
        return self.dir / "optimem.log"


def cargar() -> Config:
    """Lee config.json; si no existe lo crea con los valores por defecto."""
    if not RUTA_CONFIG.exists():
        c = Config()
        guardar(c)
        return c
    try:
        # utf-8-sig y no utf-8: PowerShell 5.1 (`Out-File -Encoding utf8`) y
        # el Bloc de notas escriben un BOM al principio, y json.loads lo
        # rechaza con "Unexpected UTF-8 BOM". utf-8-sig lo acepta si esta y
        # no molesta si no esta. Ya nos paso con freeapi_config.json.
        crudo = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise RuntimeError(
            f"config.json no se pudo leer ({e}). Revisalo o borralo para regenerarlo."
        ) from e
    validos = {f for f in Config.__dataclass_fields__}
    desconocidas = set(crudo) - validos
    if desconocidas:
        raise RuntimeError(
            f"config.json tiene claves que no existen: {sorted(desconocidas)}. "
            f"Claves validas: {sorted(validos)}"
        )
    return Config(**crudo)


def guardar(c: Config) -> None:
    RUTA_CONFIG.write_text(
        json.dumps(asdict(c), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def log(nombre: str):
    import logging
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger(nombre)
    if logger.handlers:
        return logger
    c = cargar()
    logger.setLevel(getattr(logging, c.nivel_log.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(c.ruta_log, maxBytes=8 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger

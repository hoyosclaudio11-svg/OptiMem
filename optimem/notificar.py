"""
Avisos por Telegram.

POR QUE ESTO VIVE EN EL RECOLECTOR Y NO EN UN TEMPORIZADOR APARTE

Recolectar las 60 etiquetas que el modelo necesita lleva horas. Un aviso atado
a una terminal, a una sesion o a un proceso que alguien tiene que dejar
abierto se pierde justo cuando mas hace falta: cuando pasaron seis horas y uno
ya se olvido de que estaba esperando algo. El recolector ya corre solo y
sobrevive reinicios, asi que el aviso sale de ahi.

EL TOKEN NUNCA ENTRA AL REPOSITORIO

Las credenciales se leen de variables de entorno o de un archivo .env que se
configura en `ruta_env`. El proyecto es publico: no hay ni un valor por
defecto, ni un token de ejemplo, ni una ruta fija a la configuracion de nadie.
Si no hay credenciales, esto simplemente no manda nada y lo deja anotado.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = None

# Nombres aceptados, en orden. Los genericos van al final para que cualquiera
# pueda reusar el modulo con su propio .env sin tocar nada.
VARIABLES = [
    ("OPTIMEM_TELEGRAM_TOKEN", "OPTIMEM_TELEGRAM_CHAT_ID"),
    ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
]

CLAVE_AVISADO = "aviso_umbral_enviado"


def _leer_env(ruta: str) -> dict:
    """Parser minimo de .env. No usa dotenv para no sumar una dependencia."""
    valores = {}
    try:
        with open(ruta, encoding="utf-8-sig") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                k, _, v = linea.partition("=")
                v = v.strip().strip('"').strip("'")
                valores[k.strip()] = v
    except OSError:
        pass
    return valores


def credenciales(cfg) -> tuple[str, str] | None:
    """
    Devuelve (token, chat_id) o None. Busca primero en el entorno y despues en
    el archivo indicado por `ruta_env`.
    """
    del_archivo = {}
    ruta = getattr(cfg, "ruta_env", "") or ""
    if ruta:
        del_archivo = _leer_env(os.path.expandvars(os.path.expanduser(ruta)))

    for var_token, var_chat in VARIABLES:
        token = os.environ.get(var_token) or del_archivo.get(var_token, "")
        chat = os.environ.get(var_chat) or del_archivo.get(var_chat, "")
        if token and chat:
            return token, chat
    return None


def enviar(cfg, mensaje: str) -> bool:
    """
    Manda un mensaje. Devuelve True si salio.

    Nunca levanta una excepcion: un aviso que falla no puede tumbar una
    recoleccion de horas. Se registra el problema y se sigue.
    """
    cred = credenciales(cfg)
    if not cred:
        if log:
            log.info("Sin credenciales de Telegram: no se envia el aviso")
        return False
    token, chat = cred

    datos = urllib.parse.urlencode({
        "chat_id": chat,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_notification": "false",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=datos, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status == 200
        if log:
            # Se registra el resultado, JAMAS el token ni la URL completa.
            log.info("Aviso por Telegram %s", "enviado" if ok else "rechazado")
        return ok
    except urllib.error.HTTPError as e:
        # El cuerpo del error puede traer el motivo (chat invalido, bot
        # bloqueado). No trae el token, asi que es seguro registrarlo.
        detalle = ""
        try:
            detalle = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if log:
            log.warning("Telegram rechazo el aviso: %s %s", e.code, detalle)
        return False
    except Exception as e:
        if log:
            log.warning("No se pudo enviar el aviso: %s: %s", type(e).__name__, e)
        return False


# ---------------------------------------------------------------------------
# Aviso de umbral, idempotente
# ---------------------------------------------------------------------------
def _texto_umbral(n: int, faltaban_antes: int, horas: float) -> str:
    ritmo = f"{n / horas:.1f} por hora" if horas > 0.2 else "recien arranca"
    return (
        "<b>OptiMem: hay etiquetas suficientes para entrenar</b>\n\n"
        f"Etiquetas listas: <b>{n}</b>\n"
        f"Recolectando desde hace {horas:.1f} h ({ritmo})\n\n"
        "El modelo ya se puede entrenar. Para hacerlo:\n"
        "<code>python optimem.py entrenar</code>\n\n"
        "Despues, para que el agente decida con el modelo:\n"
        "<code>python optimem.py recolectar --activar --modo activo</code>\n\n"
        "El panel esta en http://127.0.0.1:5215"
    )


def avisar_si_corresponde(cfg, con, *, forzar: bool = False) -> bool:
    """
    Avisa una sola vez cuando se cruza el umbral de entrenamiento.

    Idempotente: el flag queda en la base, asi que un reinicio del recolector
    no vuelve a mandar el mismo aviso. Con `forzar` manda igual, que es lo que
    usa el comando de prueba.
    """
    objetivo = cfg.min_muestras_entrenar
    f = con.execute(
        "SELECT COUNT(*) n FROM resultados WHERE etiqueta IS NOT NULL"
    ).fetchone()
    n = f["n"] or 0

    if not forzar:
        if n < objetivo:
            return False
        if db.leer_estado(con, CLAVE_AVISADO):
            return False   # ya avisado en una corrida anterior

    horas = 0.0
    primera = con.execute("SELECT MIN(ts) t FROM acciones").fetchone()["t"]
    if primera:
        horas = max(0.0, (time.time() - primera) / 3600)

    ok = enviar(cfg, _texto_umbral(n, objetivo, horas))
    if ok and not forzar:
        db.guardar_estado(con, CLAVE_AVISADO, {"n": n, "ts": time.time()})
        db.registrar_evento(con, "aviso_umbral", "info",
                            detalle=f"aviso enviado al llegar a {n} etiquetas")
    return ok


def probar(cfg, con=None) -> tuple[bool, str]:
    """
    Manda un mensaje de prueba. Devuelve (ok, explicacion).

    Existe para poder verificar la configuracion AHORA, en vez de descubrir en
    seis horas que el chat_id estaba mal y que nunca iba a llegar nada. Un
    aviso que no se probo no es un aviso: es una intencion.
    """
    cred = credenciales(cfg)
    if not cred:
        ruta = getattr(cfg, "ruta_env", "") or "(sin configurar)"
        return False, (
            "No encontre credenciales. Busque OPTIMEM_TELEGRAM_TOKEN / "
            "OPTIMEM_TELEGRAM_CHAT_ID, o TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, "
            f"en las variables de entorno y en el archivo: {ruta}"
        )

    n = 0
    objetivo = cfg.min_muestras_entrenar
    if con is not None:
        f = con.execute(
            "SELECT COUNT(*) n FROM resultados WHERE etiqueta IS NOT NULL"
        ).fetchone()
        n = f["n"] or 0

    ok = enviar(cfg, (
        "<b>OptiMem: prueba de aviso</b>\n\n"
        "Si ves esto, el aviso de las etiquetas va a llegar bien.\n"
        f"Etiquetas ahora: {n} de {objetivo}."
    ))
    return ok, ("Mensaje enviado" if ok else "Telegram lo rechazo; mira el log")

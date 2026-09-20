"""
Calibracion: cuanto cuesta, en milisegundos, vaciar un working set.

Para poder decir "OptiMem evito pausas por X milisegundos" hace falta saber
cuanto cuesta cada pagina que hay que traer de vuelta. Ese numero depende de
la maquina, del disco y del momento, asi que no se puede hardcodear: se mide.

El metodo esta validado contra esta maquina: se levanta un proceso hijo con N
MB ya fallados, se mide cuanto tarda en re-tocarlos, se le vacia el working
set, y se mide de nuevo. La diferencia es el costo, y da ~2,5x.

Ese dato es el que convierte el delta de fallos de pagina (que si medimos por
proceso, gratis) en milisegundos de pausa (que es lo que le importa al
usuario).

OJO: es una estimacion. Los fallos posteriores a un trim pueden servirse desde
la standby list (rapidos) o desde el disco (lentos), y no distinguimos cuales.
El coeficiente es un promedio de los dos. Se reporta siempre como estimacion.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from . import db, winapi

MB_CALIBRACION = 300

_HIJO = textwrap.dedent("""
    import ctypes, json, sys, time
    MB = %d
    k = ctypes.windll.kernel32
    k.VirtualAlloc.restype = ctypes.c_void_p
    k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_ulong, ctypes.c_ulong]
    tam = MB * 1024 * 1024
    ptr = k.VirtualAlloc(None, tam, 0x1000 | 0x2000, 0x04)
    ctypes.memset(ptr, 1, tam)

    def retocar():
        t0 = time.perf_counter()
        total = 0
        for off in range(0, tam, 4096):
            total += ctypes.c_ubyte.from_address(ptr + off).value
        dt = (time.perf_counter() - t0) * 1000.0
        if total == -1:
            print(total)
        return dt

    print(json.dumps({"listo": 1}), flush=True)
    for linea in sys.stdin:
        c = linea.strip()
        if c == "r":
            print(json.dumps({"t": retocar()}), flush=True)
        elif c == "q":
            break
""" % MB_CALIBRACION)


def _retocar(p) -> float:
    import json
    p.stdin.write("r\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())["t"]


def medir(repeticiones: int = 3, verbose: bool = False) -> dict | None:
    """
    Mide el costo de vaciar un working set. Devuelve el coeficiente o None.

    Devuelve un dict con:
      ms_por_mb      milisegundos de re-lectura por MB vaciado
      factor         cuanto se multiplico el costo de re-tocar
      mb             tamano de la prueba
    """
    ruta = os.path.join(os.environ.get("TEMP", "."), "_optimem_calib.py")
    with open(ruta, "w") as f:
        f.write(_HIJO)
    p = subprocess.Popen([sys.executable, ruta], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        p.stdout.readline()   # "listo"
        time.sleep(1.0)

        residentes, desalojados, liberados = [], [], []
        for i in range(max(1, repeticiones)):
            res = [_retocar(p) for _ in range(2)]
            residentes.append(sum(res) / len(res))

            ok, liberado = winapi.trimear(p.pid)
            if not ok:
                if verbose:
                    print("  el trim fallo; se aborta la calibracion")
                return None
            time.sleep(0.8)
            des = [_retocar(p) for _ in range(2)]
            desalojados.append(sum(des) / len(des))
            liberados.append(liberado)
            if verbose:
                print(f"  corrida {i + 1}: residente {residentes[-1]:7.2f} ms -> "
                      f"tras trim {desalojados[-1]:7.2f} ms "
                      f"(libero {liberado / 1024**2:.0f} MB)")

        prom_res = sum(residentes) / len(residentes)
        prom_des = sum(desalojados) / len(desalojados)
        mb_liberado = sum(liberados) / len(liberados) / 1024**2
        if mb_liberado <= 0 or prom_res <= 0:
            return None

        return {
            "ms_por_mb": (prom_des - prom_res) / mb_liberado,
            "ms_por_pagina": (prom_des - prom_res) / (mb_liberado * 256),
            "factor": prom_des / prom_res,
            "mb": mb_liberado,
            "ms_residente": prom_res,
            "ms_tras_trim": prom_des,
            "ts": time.time(),
        }
    finally:
        try:
            p.stdin.write("q\n")
            p.stdin.flush()
            p.wait(timeout=5)
        except Exception:
            p.kill()
        try:
            os.remove(ruta)
        except OSError:
            pass


def calibrar(cfg, con=None, verbose: bool = False) -> dict | None:
    """Mide y guarda el coeficiente en la base."""
    con = con or db.inicializar(cfg.ruta_db)
    r = medir(verbose=verbose)
    if r:
        db.guardar_estado(con, "calibracion", r)
    return r


def coeficiente(con) -> float | None:
    """
    ms por MB vaciado. None si nunca se calibro.

    Sin calibracion no se puede traducir paginas a milisegundos, y el informe
    lo dice en vez de inventar un numero.
    """
    c = db.leer_estado(con, "calibracion")
    return c.get("ms_por_mb") if c else None

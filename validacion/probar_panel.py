"""Prueba de la API del panel. Se usa un script y no curl porque en este
entorno las peticiones a 127.0.0.1 fallan dentro del sandbox."""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5215"


def pedir(ruta, método="GET"):
    req = urllib.request.Request(BASE + ruta, method=método)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


print("=" * 70)
print("Prueba del panel OptiMem")
print("=" * 70)

code, cuerpo = pedir("/")
print(f"\nGET /            -> {code}  ({len(cuerpo)} bytes)")
if code == 200:
    for marca in ("OptiMem", "graf-indice", "graf-memoria", "Índice de respuesta",
                  "--serie-1", "cross-"):
        print(f"   {'OK ' if marca in cuerpo else 'FALTA'} {marca}")

code, cuerpo = pedir("/api/estado")
print(f"\nGET /api/estado  -> {code}")
if code == 200:
    d = json.loads(cuerpo)
    print(f"   claves: {sorted(d.keys())}")
    m = d.get("memoria") or {}
    print(f"   memoria : {m.get('total', 0)/1024**3:.1f} GB total, "
          f"{m.get('disponible', 0)/1024**3:.2f} GB libre")
    print(f"   modo    : {d['cfg']['modo']}")
    print(f"   sonda   : indice={d['sonda']['indice'] if d.get('sonda') else None}")
    print(f"   acciones: {len(d.get('acciones', []))}")
    print(f"   candidatos: {len(d.get('candidatos', []))}")
    print(f"   eventos : {len(d.get('eventos', []))}")
    ap = d.get("aprendizaje", {})
    print(f"   etiquetas: {ap.get('listas_para_entrenar')} "
          f"(buenas {ap.get('buenas')}, malas {ap.get('malas')})")

code, cuerpo = pedir("/api/serie?minutos=180")
print(f"\nGET /api/serie   -> {code}")
if code == 200:
    s = json.loads(cuerpo)
    print(f"   sondas  : {len(s['sondas'])} puntos")
    print(f"   sistema : {len(s['sistema'])} puntos")
    print(f"   corte   : {s['corte']}")
    if s["sondas"]:
        p = s["sondas"][-1]
        print(f"   ultimo  : indice={p.get('indice')} cache={p.get('t_cache_ms')}")
    if s["sistema"]:
        p = s["sistema"][-1]
        print(f"   ultimo  : ram_disp={p.get('ram_disponible')} "
              f"commit={p.get('commit_total')}")

code, cuerpo = pedir("/api/entrenar", método="POST")
print(f"\nPOST /api/entrenar -> {code}")
if code == 200:
    j = json.loads(cuerpo)
    print(f"   ok={j.get('ok')}  motivo={j.get('informe', {}).get('motivo', 'entreno bien')}")

print("\n" + "=" * 70)

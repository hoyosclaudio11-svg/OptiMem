"""Captura del panel para revisar la maquetacion de verdad, no de memoria."""
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright no esta instalado:  python -m pip install playwright")
    sys.exit(1)

# Se puede pasar la ruta de salida; por defecto va a docs/panel.png, que es la
# que usa el README.
if len(sys.argv) > 1:
    SALIDA = Path(sys.argv[1])
else:
    SALIDA = RAIZ / "docs" / "panel.png"
SALIDA.parent.mkdir(parents=True, exist_ok=True)

URL = "http://127.0.0.1:5215/"
RAIZ = Path(__file__).resolve().parent.parent

errores = []
with sync_playwright() as p:
    nav = p.chromium.launch()
    pag = nav.new_page(viewport={"width": 1400, "height": 1200},
                       device_scale_factor=2)
    pag.on("console", lambda m: errores.append(f"[{m.type}] {m.text}")
           if m.type in ("error", "warning") else None)
    pag.on("pageerror", lambda e: errores.append(f"[pageerror] {e}"))
    pag.goto(URL, wait_until="networkidle")
    pag.wait_for_timeout(3500)

    # Datos que la pagina realmente pinto.
    print("--- estado del render ---")
    print(f"  titulo: {pag.title()}")
    for sel, nombre in (("#tarjetas .tarjeta", "tarjetas"),
                        ("#graf-indice svg", "svg del indice"),
                        ("#graf-memoria svg", "svg de memoria"),
                        ("#graf-indice path", "linea del indice"),
                        ("#graf-memoria path", "lineas de memoria"),
                        ("#tabla-acciones tbody tr", "filas de acciones"),
                        ("#tabla-candidatos tbody tr", "filas de candidatos"),
                        ("#tabla-eventos tbody tr", "filas de eventos")):
        print(f"  {nombre:<22}: {pag.locator(sel).count()}")

    print(f"  pastilla de modo: {pag.locator('#pastilla-modo').inner_text()}")
    print(f"  latido          : {pag.locator('#txt-latido').inner_text()}")
    txt = pag.locator("#tarjetas").inner_text().replace("\n", " | ")
    print(f"  tarjetas        : {txt[:220]}")

    # Que nada se desborde horizontalmente: es el defecto mas comun.
    desborde = pag.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    print(f"  desborde horizontal: {desborde} px")

    # Revisar que las etiquetas del eje no se salgan del svg.
    fuera = pag.evaluate("""() => {
        const malos = [];
        document.querySelectorAll('svg').forEach((s, i) => {
          const caja = s.getBoundingClientRect();
          s.querySelectorAll('text').forEach(t => {
            const r = t.getBoundingClientRect();
            if (r.left < caja.left - 2 || r.right > caja.right + 2) {
              malos.push(`svg${i}: "${t.textContent}"`);
            }
          });
        });
        return malos;
    }""")
    print(f"  etiquetas fuera del svg: {len(fuera)}")
    for f in fuera[:5]:
        print(f"     {f}")

    pag.screenshot(path=SALIDA, full_page=True)
    print(f"\n  captura guardada en {SALIDA}")
    nav.close()

print(f"\n--- errores de consola: {len(errores)} ---")
for e in errores[:10]:
    print("  " + e)

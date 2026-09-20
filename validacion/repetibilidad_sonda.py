import statistics as st
from optimem import config, probe
cfg = config.cargar()
print("Repetibilidad de la sonda: 10 corridas seguidas\n")
res = []
for i in range(10):
    r = probe.ejecutar_sonda(cfg)
    res.append(r)
    print(f"  {i+1:2d}  mem {r['t_memoria_ms']:8.2f} ms   cpu {r['t_cpu_ms']:8.1f} ms   disco {r['t_disco_ms']:7.2f} ms")
print()
for k, n in (("t_memoria_ms","memoria"), ("t_cpu_ms","cpu"), ("t_disco_ms","disco")):
    v = [r[k] for r in res if r.get(k, -1) > 0]
    if len(v) < 3: 
        print(f"  {n}: sin datos suficientes"); continue
    m, s = st.mean(v), st.stdev(v)
    print(f"  {n:<8} media {m:8.2f}   desvio {s:7.2f}   coef.variacion {s/m*100:5.1f}%   min {min(v):.2f} max {max(v):.2f}")
    print(f"           mediana {st.median(v):.2f}   rango/meseta {(max(v)-min(v))/m*100:.1f}%")

# Validación

Estos scripts no son tests unitarios: son los **experimentos** con los que se
decidió el diseño. Cada uno responde una pregunta que, contestada mal o por
intuición, lleva a construir algo que parece funcionar y no funciona.

Se corren desde la raíz del proyecto:

```bash
python validacion/nombre_del_script.py
```

Varios **modifican memoria del sistema a propósito** (crean presión, vacían
working sets). Son seguros y reversibles, pero conviene no correrlos mientras
hace algo que necesite la máquina estable.

---

## `verificar_apis_windows.py`

Qué APIs de memoria ofrece Windows y cuáles devuelven datos confiables.

Confirma contra fuentes documentadas (`psutil`) lo que devuelven
`GetPerformanceInfo` y `NtQuerySystemInformation`. La segunda usa una
**estructura no documentada** cuyos offsets cambian entre versiones de Windows:
por eso sus valores se validan antes de usarlos, y si no cuadran se descartan en
vez de reportar basura.

También prueba si los privilegios están disponibles. Resultado típico sin admin:

```
IsUserAnAdmin() = False
[!!] SeProfileSingleProcessPrivilege: no asignado al token (requiere admin)
[OK] SetProcessWorkingSetSize(-1,-1) -> True
```

---

## `alcance_sin_admin.py`

**Cuánto se puede hacer sin ser administrador.** Esta es la pregunta que decide
si el proyecto es viable.

Mide cuántos procesos se pueden abrir con permiso de escritura. Resultado en
una máquina con 420 procesos:

```
Abribles con SET_QUOTA (trim): 271
Abribles con SET_INFORMATION : 271
RAM alcanzable por trim      : 14.57 GB de 18.38 GB
```

Y lo que **no** se puede tocar es justo lo que no se debería tocar:
`MemCompression`, Defender, `dwm`, `svchost`. Windows ya protege el núcleo.

---

## `repetibilidad_sonda.py`

**Cuánto ruido tiene la sonda.** Sin esto no se puede afirmar que una mejora sea
real: si la medición varía ±30% entre corridas, un "mejoró 10%" no significa
nada.

Corridas típicas (10 seguidas):

| medición | coeficiente de variación |
|---|---|
| memoria | 2,2% |
| cpu | 1,2% |
| disco | 13,1% |

Con esos números, el índice compuesto queda en ±3,5%, así que una mejora de más
del 10% es distinguible del ruido. **Ese es el número que hace defendible el
resultado del informe.**

---

## `sensibilidad_presion.py`

**¿La sonda responde a la presión de memoria?** Este experimento es el que
refutó el primer diseño.

Crea 3 GB de presión controlada y mide antes / durante / después. La primera
versión de la sonda medía cuánto tarda en asignar memoria nueva, y el resultado
fue que el tiempo **bajó 8%** bajo presión.

La razón: asignar memoria nueva se satisface con fallos de demanda cero, que
Windows sirve desde la lista de páginas libres **sin tocar el disco**. Sólo se
degrada cuando el sistema ya está thrashando, y para entonces es tarde para
medir.

La versión actual mide otras dos cosas —leer un archivo que debería seguir en
caché, y re-tocar el propio working set— que sí se degradan cuando el sistema
desaloja páginas.

> Nota honesta: en la máquina donde se desarrolló, con 16 GB de RAM, la presión
> sintética de 3 GB **tampoco** movió las mediciones nuevas, porque todavía
> quedaba caché para ceder. Eso no invalida la sonda: significa que el sistema
> no estaba sufriendo. La sonda se activa cuando hay presión real, y el
> recolector la mide cada 60 s durante días, así que captura esos momentos sin
> necesidad de fabricarlos.

---

## `costo_del_trim.py`

**Cuánto cuesta de verdad vaciar un working set.** Es la premisa del modelo de
machine learning: si el costo fuera cero, no habría nada que predecir y el
modelo sobraría.

Levanta un proceso hijo con 300 MB ya fallados, mide cuánto tarda en re-tocarlos,
le vacía el working set y vuelve a medir.

```
re-tocar con paginas residentes :    21.86 ms
trim -> True, libero 342 MB
re-tocar tras el trim           :    54.38 ms
>>> el trim multiplico el costo por 2.49x
```

Conclusión: el trim tiene un costo real y medible, así que **elegir a quién
trimear importa**. La calibración del producto (`python optimem.py calibrar`)
usa este mismo método para traducir páginas a milisegundos en cada máquina.

---

## `probar_entrenador.py`

El circuito de entrenamiento completo: armar el dataset desde la base, calcular
features, partir por tiempo, entrenar, elegir umbral, guardar, y verificar que el
agente lo pueda cargar y usar.

Corre sobre una **copia** de la base para no ensuciar los datos reales.

**Qué prueba y qué no**, porque es importante no confundirse: prueba que el
circuito funciona. Las acciones que inserta son inventadas —nadie trimeó esos
procesos— así que las etiquetas salen de series de fallos que no reflejan ningún
trim. Sirve como prueba de cañería, **no como validación del modelo**. Un AUC
alto acá no significa nada.

---

## `probar_panel.py`

Los endpoints del panel: que respondan, que devuelvan las claves esperadas y que
`/api/entrenar` se niegue correctamente cuando no hay muestras suficientes.

---

## `inspeccionar_datos.py`

Volcado de lo que hay guardado: acciones, etiquetas, sondas, composición de la
base. Es la primera cosa que conviene mirar cuando algo no cuadra.

---

## `captura_panel.py`

Saca una captura del panel con Playwright y reporta problemas de maquetación
—desborde horizontal, etiquetas que se salen del SVG, errores de consola—.

Existe porque el validador de la paleta verifica el color, no la geometría: hay
defectos que sólo se ven mirando el render.

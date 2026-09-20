"""
OptiMem - gestion predictiva de memoria para Windows.

Reconfigura los parametros de paging que SI se pueden tocar en caliente
(working set por proceso y prioridad de memoria) y predice que trims van a
causar swaps innecesarios antes de hacerlos.

El pagefile en si NO se toca: cambiarlo requiere registro y reinicio, no es
una palanca en tiempo real. Ver README.
"""

__version__ = "0.1.0"

"""Identificadores estables del dominio.

Un único patrón para auditorías, evidencias y hallazgos: minúsculas, dígitos,
``-`` y ``_``, con longitud acotada y sin empezar por separador. Así un
identificador sirve igual en rutas de reporte, URLs y nombres de archivo.
"""

from __future__ import annotations

import re

IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

IDENTIFIER_RULE = (
    "debe tener entre 3 y 64 caracteres, empezar por letra o dígito y contener "
    "solo minúsculas, dígitos, '-' o '_'"
)


def is_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER_PATTERN.match(value))

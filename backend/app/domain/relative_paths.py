"""Reglas estructurales de rutas relativas al proyecto autorizado.

Se comparten entre evidencia y herramientas para no duplicar la misma
comprobación con mensajes distintos. Aquí solo se valida la **forma** de la
ruta; comprobar que existe, que está dentro de la raíz y que no es un enlace
simbólico es responsabilidad de la herramienta de lectura.
"""

from __future__ import annotations

import re

_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


def relative_path_problem(value: object) -> str | None:
    """Devuelve el problema encontrado, o ``None`` si la ruta es aceptable."""

    if not isinstance(value, str) or not value.strip():
        return "no puede estar vacía"
    if value != value.strip():
        return "no admite espacios en los extremos"
    if "\x00" in value:
        return "no admite caracteres nulos"
    if "\\" in value:
        return "debe usar '/' como separador"
    if value.startswith("/"):
        return "debe ser relativa al proyecto autorizado"
    if _WINDOWS_DRIVE_PATTERN.match(value):
        return "no admite rutas con unidad de disco"
    if value.startswith("~"):
        return "no admite expansión de inicio de usuario"

    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return "no admite segmentos vacíos, '.' ni '..'"

    return None

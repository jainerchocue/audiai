"""Evidencia: el material que respalda un hallazgo.

Una evidencia es un hecho observable, no una interpretación: ruta relativa
dentro del proyecto autorizado, líneas cuando corresponda, fragmento acotado y
la herramienta determinista que la produjo.

Decisiones de diseño:

- La validación de rutas aquí es **estructural** (relativa, sin ``..``, sin
  unidad de disco, sin separadores de Windows). La comprobación real de que la
  ruta existe, está dentro de la raíz y no es un enlace simbólico pertenece a
  la herramienta de lectura (Fase 3).
- El fragmento tiene tope duro: la evidencia se muestra, no se convierte en
  una copia del proyecto.
- No se guardan rutas absolutas: la evidencia se exporta por defecto y no debe
  filtrar la estructura del equipo del usuario.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.domain.identifiers import IDENTIFIER_RULE, is_identifier
from app.domain.relative_paths import relative_path_problem

MAX_SNIPPET_CHARS: Final = 4_000
TOOL_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class EvidenceError(ValueError):
    """Error de regla de dominio en una evidencia."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Fragmento observado de un archivo del proyecto autorizado.

    El identificador es único dentro de su auditoría; la colección que agrupa
    las evidencias es quien garantiza esa unicidad.
    """

    evidence_id: str
    source_path: str
    tool: str
    snippet: str
    line_start: int | None = None
    line_end: int | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_identifier(self.evidence_id):
            raise EvidenceError(f"evidence_id {IDENTIFIER_RULE}")
        _validate_source_path(self.source_path)
        _validate_tool(self.tool)
        _validate_snippet(self.snippet)
        _validate_line_range(self.line_start, self.line_end)
        _validate_limitations(self.limitations)

    @property
    def location(self) -> str:
        """Ubicación legible para el informe."""

        if self.line_start is None:
            return self.source_path
        if self.line_end is None or self.line_end == self.line_start:
            return f"{self.source_path}:{self.line_start}"
        return f"{self.source_path}:{self.line_start}-{self.line_end}"


def _validate_source_path(source_path: str) -> None:
    problem = relative_path_problem(source_path)
    if problem is not None:
        raise EvidenceError(f"source_path {problem}")


def _validate_tool(tool: str) -> None:
    if not isinstance(tool, str) or not TOOL_PATTERN.match(tool):
        raise EvidenceError(
            "tool debe ser un identificador de herramienta en minúsculas "
            "(por ejemplo 'read_file' o 'pattern_scan')"
        )


def _validate_snippet(snippet: str) -> None:
    if not isinstance(snippet, str) or not snippet.strip():
        raise EvidenceError("snippet no puede estar vacío")
    if len(snippet) > MAX_SNIPPET_CHARS:
        raise EvidenceError(f"snippet supera {MAX_SNIPPET_CHARS} caracteres")


def _validate_line_range(line_start: int | None, line_end: int | None) -> None:
    if line_start is not None:
        if not isinstance(line_start, int) or isinstance(line_start, bool):
            raise EvidenceError("line_start debe ser un entero")
        if line_start < 1:
            raise EvidenceError("line_start empieza en 1")

    if line_end is not None:
        if line_start is None:
            raise EvidenceError("line_end requiere line_start")
        if not isinstance(line_end, int) or isinstance(line_end, bool):
            raise EvidenceError("line_end debe ser un entero")
        if line_end < line_start:
            raise EvidenceError("line_end no puede ser menor que line_start")


def _validate_limitations(limitations: tuple[str, ...]) -> None:
    if not isinstance(limitations, tuple):
        raise EvidenceError("limitations debe ser una tupla de textos")
    if any(not isinstance(item, str) or not item.strip() for item in limitations):
        raise EvidenceError("limitations no admite entradas vacías")

"""Conector nulo para herramientas sin implementar.

Falla en cerrado: no devuelve evidencias vacías ni un resultado "correcto" sin
haber leído nada. Un ``ToolResult`` vacío sería indistinguible de un proyecto
sin hallazgos, y eso sería una afirmación falsa.
"""

from __future__ import annotations

from app.domain.tools import ToolName, ToolNotAvailable, ToolRequest, ToolResult


class UnavailableTool:
    """Implementación por defecto de una herramienta aún no construida."""

    def __init__(self, tool: ToolName, reason: str | None = None) -> None:
        self.tool = tool
        self._reason = reason or "herramienta no implementada todavía"

    def run(self, request: ToolRequest) -> ToolResult:
        if request.tool is not self.tool:
            raise ToolNotAvailable(
                f"esta instancia representa '{self.tool.value}', "
                f"no '{request.tool.value}'"
            )
        raise ToolNotAvailable(
            f"herramienta '{self.tool.value}' no disponible: {self._reason}"
        )

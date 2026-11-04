"""Conector nulo para roles sin implementar.

Falla en cerrado: si alguien invoca un rol que todavía no tiene ejecutor, el
proceso se detiene con un error explícito en lugar de devolver un resultado
inventado. Se sustituye por el ejecutor real cuando su implementación exista.
"""

from __future__ import annotations

from app.domain.roles import RoleNotConfigured, RoleRequest, RoleResult


class UnavailableRoleExecutor:
    """Implementación por defecto del puerto ``RoleExecutor``."""

    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason or "no hay ejecutor de roles conectado"

    def run(self, request: RoleRequest) -> RoleResult:
        raise RoleNotConfigured(
            f"rol '{request.role.value}' no disponible: {self._reason}. "
            "AudiAI no simula resultados de roles no implementados"
        )

"""MockRoleProvider: ejecutor de roles simulado y determinista (Fase 5).

Qué hace:

- Implementa el mismo puerto ``RoleExecutor`` que TODO ejecutor real de roles
  (incluidos los subagentes que se conecten después). No es un atajo: es el
  doble de prueba oficial.
- Nunca llama a la red, nunca ejecuta código del proyecto auditado, nunca
  inventa contenido dinámico: mismas entradas → mismas salidas, siempre.
- Todo resultado que produce lleva ``origin=mock`` (exigido por el dominio),
  y su ``summary`` empieza con el prefijo ``MOCK(<label>)``: una simulación no
  puede pasar por resultado real.

Cómo se usa en pruebas:

````python
provider = MockRoleProvider({
    RoleName.CAUSE_ANALYSIS: MockScript(
        outcome=RoleOutcome.COMPLETED,
        summary="causa simulada",
        proposals=(FindingProposal(...),),
    )
})
````

Errores de esquema gestionados explícitamente
---------------------------------------------
Los scripts pregrabados se expresan con ``MockScript`` (una forma cruda, más
libre que ``RoleResult``). La traducción a ``RoleResult`` valida su esquema;
una respuesta con esquema inválido NO se propaga tal cual: el provider la
convierte en un resultado ``FAILED`` con el motivo del esquema, de forma que
los errores de esquema se gestionen explícitamente, como pide el README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from app.domain.roles import (
    FindingProposal,
    RoleError,
    RoleName,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.domain.origin import ResultOrigin

_MOCK_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,24}$")


class MockProviderError(RuntimeError):
    """Error interno del mock: configuración imposible de traducir."""


@dataclass(frozen=True, slots=True)
class MockScript:
    """Respuesta pregrabada de una simulación, aún sin aplicar reglas de dominio."""

    outcome: RoleOutcome
    summary: str = ""
    proposals: tuple[FindingProposal, ...] = ()
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    citations: tuple[str, ...] = ()


class MockRoleProvider:
    """Doble de prueba del puerto ``RoleExecutor``."""

    def __init__(
        self,
        scripts: Mapping[RoleName, MockScript] | None = None,
        *,
        label: str = "mock",
    ) -> None:
        if not _MOCK_LABEL_PATTERN.match(label):
            raise ValueError(
                "label debe ser un identificador corto y minísculas "
                "(aparece en cada summary para permitir trazabilidad)"
            )
        self._scripts = dict(scripts) if scripts else {}
        self._prefix = f"MOCK({label}): "

    def run(self, request: RoleRequest) -> RoleResult:
        script = self._scripts.get(request.role)
        if script is None:
            return self._default_response(request)
        return self._materialized(request, script)

    # -- respuesta genérica ------------------------------------------------

    def _default_response(self, request: RoleRequest) -> RoleResult:
        if not request.context:
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.REFUSED,
                summary=(
                    f"{self._prefix}sin contexto entregado para el rol "
                    f"{request.role.value}; el mock no fabrica conclusiones"
                ),
            )

        requested = (
            f"recibió {len(request.context)} fragmento(s) "
            f"({request.context_bytes} bytes de máx "
            f"{request.limits.max_context_bytes}) para el rol {request.role.value}"
        )
        return RoleResult(
            role=request.role,
            outcome=RoleOutcome.COMPLETED,
            summary=f"{self._prefix}{requested}; sin hallazgos pregrabados",
        )

    # -- script pregrabado -------------------------------------------------

    def _materialized(self, request: RoleRequest, script: MockScript) -> RoleResult:
        """Traduce un ``MockScript`` a ``RoleResult`` aplicando las reglas.

        Un script inválido no se propaga tal cual: esquema inválido se
        convierte en un resultado FAILED con el motivo gestional [explicito].
        """

        try:
            return RoleResult(
                role=request.role,
                outcome=script.outcome,
                summary=self._labeled_summary(script),
                origin=ResultOrigin.MOCK,
                proposals=script.proposals,
                citations=script.citations,
                provider=script.provider,
                model=script.model,
                error=script.error,
            )
        except RoleError as error:
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.FAILED,
                summary=self._prefix,
                origin=ResultOrigin.MOCK,
                error=(
                    f"el script del mock tiene esquema inválido: {error} "
                    "(gestionado explícitamente, devuelto como FAILED)"
                ),
            )

    def _labeled_summary(self, script: MockScript) -> str:
        text = script.summary.strip()
        if not text:
            return ""
        return f"{self._prefix}{text}"

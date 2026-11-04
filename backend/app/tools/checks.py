"""Comprobaciones deterministas (Fase 4): evidencias sin IA.

Cada comprobación es una heurística de patrón que se ejecuta con el lector
acotado de la Fase 3. Producen evidencias verificables y **propuestas** de
hallazgo cuyo origen no es el modelo: síbőlamente deterministas.

REGLAS QUE CUMPLE CADA RESULTADO
--------------------------------
- Una coincidencia de patrón no es una vulnerabilidad: toda propuesta declara
  ``uncertainty`` con el aviso de revisión humana.
- Las evidencias existen porque las produjo el lector real; ninguna evidencia
  se inventa en esta capa.
- Si el lectura llega truncada, las propuestas solo se formulan sobre lo
  leído: el truncado se hereda de ``ToolResult`` y no se oculta.
- Exclusiones y límites los aplica el lector, no esta capa.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from app.domain.evidence import Evidence
from app.domain.identifiers import IDENTIFIER_RULE, is_identifier
from app.domain.roles import FindingProposal
from app.domain.severity import Severity
from app.domain.tools import ToolLimits, ToolName, ToolRequest, ToolResult
from app.tools.deterministic import LocalReadOnlyTool, _validate_root

CHECK_UNCERTAINTY = (
    "coincidencia de patrón (heurística); no confirma explotabilidad, "
    "requiere revisión humana"
)

_PATTERN_MAX_CHARS = 500
_DEFAULT_SCAN_LIMITS = ToolLimits()


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """Definición de una comprobación determinista."""

    check_id: str
    title: str
    pattern: str
    severity: Severity
    explanation: str

    def __post_init__(self) -> None:
        if not is_identifier(self.check_id):
            raise ValueError(f"check_id {IDENTIFIER_RULE}")
        if not self.title.strip() or not self.explanation.strip():
            raise ValueError("title y explanation no pueden estar vacíos")
        if not self.severity or self.severity is Severity.UNKNOWN:
            raise ValueError(
                "la severidad de una comprobación debe decidirse con criterio "
                "documentado; UNKNOWN no sirve como propuesta"
            )
        if not self.pattern.strip():
            raise ValueError("pattern no puede estar vacío")
        if len(self.pattern) > _PATTERN_MAX_CHARS:
            raise ValueError("pattern demasiado largo")
        try:
            re.compile(self.pattern)
        except re.error as error:
            raise ValueError(f"pattern inválido: {error}") from error


# -- comprobaciones incluidas --------------------------------------------

DYNAMIC_EXECUTION_CHECK = CheckSpec(
    check_id="chk-dynamic-execution",
    title="Ejecución dinámica de código",
    pattern=r"\b(eval|exec)\s*\(",
    severity=Severity.MEDIUM,
    explanation=(
        "Llamada a eval o exec: puede ejecutar código a partir de datos en "
        "tiempo de ejecución. Revisar el origen del argumento."
    ),
)

HARDCODED_CREDENTIAL_CHECK = CheckSpec(
    check_id="chk-hardcoded-credential",
    title="Credencial embebida en el código",
    pattern=(
        r"(?i)(password|passwd|secret|api_?key|token)[\"\s]*[=:][\"\s]*"
        r"[\"'][^\"']{8,}[\"']"
    ),
    severity=Severity.HIGH,
    explanation=(
        "Cadena que parece una credencial literal dentro del código. "
        "Verificar y mover a gestión de secretos."
    ),
)

BUNDLED_CHECKS: tuple[CheckSpec, ...] = (
    DYNAMIC_EXECUTION_CHECK,
    HARDCODED_CREDENTIAL_CHECK,
)


@dataclass(frozen=True, slots=True)
class CheckRun:
    """Resultado de una comprobación: evidencias reales y propuestas."""

    check_id: str
    result: ToolResult
    proposals: tuple[FindingProposal, ...]

    def __post_init__(self) -> None:
        if not is_identifier(self.check_id):
            raise ValueError(f"check_id {IDENTIFIER_RULE}")
        available = {item.evidence_id for item in self.result.evidence}
        for proposal in self.proposals:
            if not set(proposal.evidence_ids).issubset(available):
                raise ValueError(
                    "las propuestas citan evidencias que este resultado no produjo"
                )

    @property
    def is_truncated(self) -> bool:
        return self.result.outcome.value == "truncated"


class DeterministicChecks:
    """Ejecuta comprobaciones deterministas sobre una raíz autorizada.

    Cada comprobación corre en su **instancia propia** del lector con
    ``id_namespace`` del check, para que los identificadores de evidencia no
    colisionen entre comprobaciones ni entre ejecuciones.
    """

    def __init__(self, root: Path) -> None:
        _validate_root(root)
        self._root = root

    def run(
        self,
        spec: CheckSpec,
        *,
        limits: ToolLimits = _DEFAULT_SCAN_LIMITS,
    ) -> CheckRun:
        """Ejecuta una comprobación completa: la heurística pide el scan real."""

        reader = LocalReadOnlyTool(self._root, id_namespace=spec.check_id)
        request = ToolRequest(
            tool=ToolName.PATTERN_SCAN,
            pattern=spec.pattern,
            limits=limits,
        )
        result = reader.run(request)

        return CheckRun(
            check_id=spec.check_id,
            result=result,
            proposals=tuple(_proposals_for(spec, result)),
        )

    def run_all(
        self,
        specs: tuple[CheckSpec, ...] = BUNDLED_CHECKS,
        *,
        limits: ToolLimits = _DEFAULT_SCAN_LIMITS,
    ) -> tuple[CheckRun, ...]:
        """Ejecuta todas las comprobaciones incluidas, en orden determinista."""

        return tuple(self.run(spec, limits=limits) for spec in specs)


def _proposals_for(spec: CheckSpec, result: ToolResult) -> list[FindingProposal]:
    by_path: dict[str, list[Evidence]] = {}
    for item in result.evidence:
        by_path.setdefault(item.source_path, []).append(item)

    proposals: list[FindingProposal] = []
    for source_path, items in by_path.items():
        counted = f" ({len(items)} línea(s) detectada(s))."
        proposals.append(
            FindingProposal(
                title=f"{spec.title} en {source_path}",
                explanation=spec.explanation + counted,
                severity=spec.severity,
                evidence_ids=tuple(item.evidence_id for item in items),
                uncertainty=CHECK_UNCERTAINTY,
            )
        )
    return proposals


def evidence_index(runs: tuple[CheckRun, ...]) -> Mapping[str, Evidence]:
    """Índice usado luego para convertir propuestas en hallazgos confirmados."""

    combined: dict[str, Evidence] = {}
    for run in runs:
        combined.update({e.evidence_id: e for e in run.result.evidence})
    return combined

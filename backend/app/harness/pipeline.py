"""Harness de AudiAI (Fase 6): secuencia fija, límites y validación.

El flujo es fijo, sin exploración autónoma:

    listado → comprobaciones deterministas → análisis (rol) → validación

Reglas de ejecución que este harness aplica en código, no en prompts:

- Cada fase consume 1 paso del presupuesto (``max_steps``).
- El reloj (``clock``) se consulta antes de cada paso: si supera
  ``max_seconds`` el recorrido aborta con motivo explícito.
- ``should_stop`` permite cancelar entre pasos; produce estado ``cancelled``.
- Nada se ejecuta con capacidades implícitas: todo lo que ocurre queda en el
  ``trace`` y todo hallazgo exige evidencia producida en este mismo recorrido.
- Un proveedor u herramienta que falla aborta el recorrido: no se reporta una
  auditoría "completa" con partes muertas.
- Los hallazgos confirman solo evidencias generadas aquí: una propuesta que
  cite algo que este recorrido no produjo se descarta y queda en el trace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from app.domain.evidence import Evidence
from app.domain.finding import Finding, FindingOrigin, MissingEvidenceError
from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ContextItem,
    RoleError,
    RoleExecutor,
    RoleLimits,
    RoleName,
    RoleNotConfigured,
    RoleOutcome,
    RoleRequest,
)
from app.domain.tools import ToolError, ToolName, ToolRequest, ToolOutcome
from app.tools.checks import BUNDLED_CHECKS, CheckRun, CheckSpec, DeterministicChecks
from app.tools.deterministic import LocalReadOnlyTool

ANALYSIS_INSTRUCTION = (
    "Describe la causa probable de las evidencias entregadas, citando sus "
    "evidence_id en las propuestas. Si no puedes precisar una causa, declara "
    "refusal en lugar de inventar."
)


class PipelineError(ValueError):
    """El harness se construyó o se usó de forma inválida."""
class PipelineStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PipelineLimits:
    max_steps: int = 32
    max_seconds: float = 30.0
    max_context_bytes: int = 32_000

    def __post_init__(self) -> None:
        for field_name in ("max_steps", "max_seconds", "max_context_bytes"):
            if getattr(self, field_name) <= 0:
                raise PipelineError(f"{field_name} debe ser positivo")


# Presupuesto por defecto para ejecuciones de demostración vía API: inmutable
# y compartido sin riesgo (frozen). Un recorrido solo puede acortarlo.
DEFAULT_PIPELINE_LIMITS = PipelineLimits()


@dataclass(frozen=True, slots=True)
class StepRecord:
    step: str
    status: StepStatus
    detail: str


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    status: PipelineStatus
    reason: str | None
    evidence: tuple[Evidence, ...]
    findings: tuple[Finding, ...]
    trace: tuple[StepRecord, ...]
    steps_used: int
    analysis_origin: ResultOrigin | None = None
    analysis_provider: str | None = None
    analysis_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PipelineStatus):
            raise PipelineError("status debe ser un PipelineStatus")
        if self.status is PipelineStatus.COMPLETED and self.reason is not None:
            raise PipelineError("un recorrido completado no lleva reason")
        if self.status is not PipelineStatus.COMPLETED:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise PipelineError(
                    "un recorrido fallido o cancelado exige reason explícita"
                )
        if any(not isinstance(item, Finding) for item in self.findings):
            raise PipelineError("findings solo admite Finding")
        available = {item.evidence_id for item in self.evidence}
        for finding in self.findings:
            if not set(finding.evidence_ids).issubset(available):
                raise PipelineError(
                    "un hallazgo cita evidencias que este recorrido no produjo"
                )
        if self.analysis_origin is not None:
            if not isinstance(self.analysis_origin, ResultOrigin):
                raise PipelineError("analysis_origin debe ser un ResultOrigin")
            if self.analysis_origin is ResultOrigin.REAL:
                if not (
                    isinstance(self.analysis_provider, str)
                    and self.analysis_provider.strip()
                    and isinstance(self.analysis_model, str)
                    and self.analysis_model.strip()
                ):
                    raise PipelineError(
                        "un análisis real exige provider y model identificables"
                    )


class AuditHarness:
    """Coordina el recorrido completo de una auditoría sobre una raíz."""

    def __init__(
        self,
        root: Path,
        roles: RoleExecutor,
        *,
        limits: PipelineLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._root = root
        self._roles = roles
        self._limits = limits or PipelineLimits()
        self._clock = clock
        self._should_stop = should_stop or (lambda: False)
        # las comprobaciones se construyen de forma diferida: una raíz que ya
        # no existe debe fallar el paso, no el arranque del harness
        self._checks_lazy: DeterministicChecks | None = None

    def execute(self) -> PipelineOutcome:
        return _Execution(self).run()

    def _checks(self) -> DeterministicChecks:
        """Construcción diferida: una raíz que ya no existe falla el paso,
        no el arranque del harness."""

        if self._checks_lazy is None:
            self._checks_lazy = DeterministicChecks(self._root)
        return self._checks_lazy


class _Execution:
    """Estado mutable de un recorrido; se crea por ``execute``."""

    def __init__(self, harness: AuditHarness) -> None:
        self._harness = harness
        self._started = harness._clock()
        self._trace: list[StepRecord] = []
        self._evidence: list[Evidence] = []
        self._findings: list[Finding] = []
        self._check_runs: list[CheckRun] = []
        self._steps_used = 0
        self._finding_counter = 0
        self._cancelled = False
        self._analysis_origin: ResultOrigin | None = None
        self._analysis_provider: str | None = None
        self._analysis_model: str | None = None

    # -- recorrido ---------------------------------------------------------

    def run(self) -> PipelineOutcome:
        reason = self._gate("1.lectura")
        if reason is None:
            reason = self._collect_reading()
        if reason is None:
            for spec in BUNDLED_CHECKS:
                reason = self._gate(f"2.checks:{spec.check_id}")
                if reason is not None:
                    break
                reason = self._collect_check(spec)
        if reason is None:
            reason = self._gate("3.analysis")
        if reason is None:
            reason = self._run_analysis()
        if reason is None:
            reason = self._gate("4.validation")
        if reason is None:
            self._validate_all()

        if self._cancelled:
            return self._cancelled_outcome()
        if reason is not None:
            return self._failed_outcome(reason)
        return self._completed_outcome()

    # -- presupuesto, tiempo y cancelación ----------------------------------

    def _gate(self, step: str) -> str | None:
        """Devuelve la razón de abortar antes del paso, o ``None`` para seguir.

        No consume presupuesto: quien ejecuta el paso decide qué hace con el
        detalle. El paso se cuenta en el propio paso (``_begin_step``).
        """

        if self._harness._should_stop():
            self._cancelled = True
            self._trace.append(
                StepRecord(step, StepStatus.SKIPPED, "cancelado antes de ejecutar")
            )
            return f"se solicitó cancelar en el paso {step}"

        if self._steps_used >= self._harness._limits.max_steps:
            self._trace.append(
                StepRecord(
                    step, StepStatus.SKIPPED, "no ejecutado: presupuesto de pasos agotado"
                )
            )
            return (
                f"límite de pasos alcanzado ({self._harness._limits.max_steps}) "
                f"antes de {step}"
            )

        elapsed = self._harness._clock() - self._started
        if elapsed > self._harness._limits.max_seconds:
            self._trace.append(
                StepRecord(step, StepStatus.SKIPPED, "no ejecutado: límite de tiempo")
            )
            return (
                f"límite de tiempo alcanzado ({elapsed:.1f} s > "
                f"{self._harness._limits.max_seconds:.1f} s) antes de {step}"
            )

        self._steps_used += 1
        return None

    # -- pasos -------------------------------------------------------------

    def _collect_reading(self) -> str | None:
        step = "1.lectura"

        try:
            reader = LocalReadOnlyTool(self._harness._root, id_namespace="list")
            result = reader.run(ToolRequest(tool=ToolName.LIST_FILES))
        except ToolError as error:
            return self._step_failed(step, str(error))
        except OSError as error:
            return self._step_failed(
                step, f"error del sistema de archivos: {type(error).__name__}"
            )

        if result.outcome is ToolOutcome.FAILED:
            return self._step_failed(
                step, result.failure_reason or "motivo no declarado"
            )

        self._evidence.extend(result.evidence)
        self._trace.append(
            StepRecord(
                step,
                StepStatus.OK,
                f"{len(result.evidence)} evidencia(s), "
                f"{len(result.skipped)} omitida(s) por límite, "
                f"truncado={result.outcome is ToolOutcome.TRUNCATED}",
            )
        )
        return None

    def _collect_check(self, spec: CheckSpec) -> str | None:
        step = f"2.checks:{spec.check_id}"

        try:
            run = self._harness._checks().run(spec)
        except ToolError as error:
            return self._step_failed(step, str(error))
        except OSError as error:
            return self._step_failed(
                step, f"error del sistema de archivos: {type(error).__name__}"
            )

        if run.result.outcome is ToolOutcome.FAILED:
            return self._step_failed(
                step, run.result.failure_reason or "motivo no declarado"
            )

        self._check_runs.append(run)
        self._evidence.extend(run.result.evidence)
        self._trace.append(
            StepRecord(
                step,
                StepStatus.OK,
                f"{len(run.result.evidence)} evidencia(s), "
                f"{len(run.proposals)} propuesta(s), "
                f"truncado={run.is_truncated}",
            )
        )
        return None

    def _run_analysis(self) -> str | None:
        step = "3.analysis"
        context, omitted = self._context_items()

        try:
            result = self._harness._roles.run(
                RoleRequest(
                    role=RoleName.CAUSE_ANALYSIS,
                    instruction=ANALYSIS_INSTRUCTION,
                    context=context,
                    limits=RoleLimits(
                        max_context_bytes=self._harness._limits.max_context_bytes
                    ),
                )
            )
        except RoleError as error:
            return self._step_failed(step, f"contrato de rol violado: {error}")
        except RoleNotConfigured as error:
            return self._step_failed(step, str(error))
        except OSError as error:
            return self._step_failed(
                step, f"error del sistema de archivos: {type(error).__name__}"
            )

        if result.outcome is RoleOutcome.FAILED:
            return self._step_failed(
                step, result.error or "error no declarado por el ejecutor"
            )

        self._analysis_proposals = result.proposals
        self._analysis_origin = result.origin
        self._analysis_provider = result.provider
        self._analysis_model = result.model
        if result.origin is ResultOrigin.MOCK and (result.provider or result.model):
            return self._step_failed(
                step, "el ejecutor declaró provider/model con un resultado simulado"
            )
        self._trace.append(
            StepRecord(
                step,
                StepStatus.OK,
                f"outcome={result.outcome.value}, "
                f"{len(result.proposals)} propuesta(s), "
                f"{omitted} fragmento(s) omitidos por límite de contexto, "
                f"origen={result.origin.value}",
            )
        )
        return None

    def _validate_all(self) -> None:
        step = "4.validation"
        index = {item.evidence_id: item for item in self._evidence}
        confirmed = 0
        discarded = 0

        for run in self._check_runs:
            for proposal in run.proposals:
                if self._confirm(proposal, FindingOrigin.DETERMINISTIC, index):
                    confirmed += 1
                else:
                    discarded += 1

        for proposal in self._analysis_proposals:
            if self._confirm(proposal, FindingOrigin.MODEL, index):
                confirmed += 1
            else:
                discarded += 1

        self._trace.append(
            StepRecord(
                step,
                StepStatus.OK,
                f"{confirmed} hallazgo(s) confirmado(s), {discarded} descartado(s)",
            )
        )

    def _confirm(
        self,
        proposal,
        origin: FindingOrigin,
        index: dict[str, Evidence],
    ) -> bool:
        """Convierte una propuesta en hallazgo o la descarta con motivo."""

        self._finding_counter += 1
        try:
            finding = Finding.from_proposal(
                proposal,
                finding_id=f"fnd-h{self._finding_counter:04d}",
                evidence_index=index,
                origin=origin,
            )
        except (MissingEvidenceError, ValueError) as error:
            self._trace.append(
                StepRecord(
                    step="4.validation",
                    status=StepStatus.FAILED,
                    detail=f"propuesta descartada ('{proposal.title}'): {error}",
                )
            )
            return False

        self._findings.append(finding)
        return True

    # -- soporte -----------------------------------------------------------

    def _context_items(self) -> tuple[tuple[ContextItem, ...], int]:
        cap = self._harness._limits.max_context_bytes
        context: list[ContextItem] = []
        used = 0
        omitted = 0

        for item in self._evidence:
            candidate = ContextItem(evidence_id=item.evidence_id, snippet=item.snippet)
            if used + candidate.size > cap:
                omitted += 1
                continue
            used += candidate.size
            context.append(candidate)

        return tuple(context), omitted

    def _step_failed(self, step: str, reason: str) -> str:
        self._trace.append(StepRecord(step, StepStatus.FAILED, reason))
        return f"el paso {step} falló: {reason}"

    def _failed_outcome(self, reason: str) -> PipelineOutcome:
        return self._outcome_with_attribution(
            status=PipelineStatus.FAILED, reason=reason
        )

    def _cancelled_outcome(self) -> PipelineOutcome:
        return self._outcome_with_attribution(
            status=PipelineStatus.CANCELLED,
            reason="cancelado por el operador entre pasos",
        )

    def _completed_outcome(self) -> PipelineOutcome:
        return self._outcome_with_attribution(
            status=PipelineStatus.COMPLETED, reason=None
        )

    def _outcome_with_attribution(
        self, *, status: PipelineStatus, reason: str | None
    ) -> PipelineOutcome:
        """Construye el outcome con la atribución del paso de análisis.

        Si el análisis ni siquiera se alcanzó (fallo antes del paso 3), la
        atribución queda en ``None``: ningún modelo consultado, ninguna
        procedencia que declarar.
        """

        return PipelineOutcome(
            status=status,
            reason=reason,
            evidence=tuple(self._evidence),
            findings=tuple(self._findings),
            trace=tuple(self._trace),
            steps_used=self._steps_used,
            analysis_origin=self._analysis_origin,
            analysis_provider=self._analysis_provider,
            analysis_model=self._analysis_model,
        )

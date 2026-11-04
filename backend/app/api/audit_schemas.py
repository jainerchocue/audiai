"""Contrato de transporte de la API de auditorías (Fase 7).

Solo es una fachada de lectura sobre los objetos de dominio: ni la auditoría,
ni los hallazgos, ni las evidencias admiten mutación por el canal HTTP. Cualquier
objeto malformado en la respuesta (dominio) es imposible por los invariantes;
cualquier petición malformada (transporte) produce un error HTTP coherente.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.audit import Audit, AuditStatus
from app.domain.evidence import Evidence
from app.domain.finding import Finding
from app.harness.pipeline import StepRecord
from app.services.audits import AuditRun


class FindingPatchOut(BaseModel):
    """El parche sugerido viaja solo como texto; nunca se aplica."""

    file_path: str
    before: str
    after: str
    explanation: str


class FindingOut(BaseModel):
    """Hallazgo legible para humanos y exportable a JSON."""

    finding_id: str
    title: str
    explanation: str
    severity: str
    status: str
    origin: str
    evidence_ids: tuple[str, ...] = ()
    recommendation: str | None = None
    patch: FindingPatchOut | None = None
    uncertainty: str | None = None
    limitations: tuple[str, ...] = ()


class EvidenceOut(BaseModel):
    """Evidencia observable: ruta relativa, línea y fragmento acotado."""

    evidence_id: str
    source_path: str
    tool: str
    snippet: str
    line_start: int | None = None
    line_end: int | None = None
    limitations: tuple[str, ...] = ()


class TraceStepOut(BaseModel):
    """Un paso fijo del recorrido, con su resultado tal cual ocurrió."""

    step: str
    status: str
    detail: str


class AuditInfoOut(BaseModel):
    """Cabecera de auditoría: solo estado y trazabilidad, sin contenido pesado."""

    audit_id: str
    scope_root: str
    status: AuditStatus
    origin: str
    provider: str | None = None
    model: str | None = None
    failure_reason: str | None = None
    limitations: tuple[str, ...] = ()
    findings_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    steps_used: int = Field(default=0, ge=0)

    @classmethod
    def from_run(cls, run: AuditRun) -> "AuditInfoOut":
        """Proyecta un run (estado + conteos) sin exponer el material completo."""

        return cls(
            audit_id=run.audit.audit_id,
            scope_root=run.audit.scope_root,
            status=run.audit.status,
            origin=run.audit.origin.value,
            provider=run.audit.provider,
            model=run.audit.model,
            failure_reason=run.audit.failure_reason,
            limitations=run.audit.limitations,
            findings_count=len(run.findings),
            evidence_count=len(run.evidence),
            steps_used=_steps_of(run),
        )


class CreateAuditRequest(BaseModel):
    """Cuerpo de ``POST /audits``.

    ``audit_id`` es opcional: un valor vacío genera ``audit-demo-N`` cuando el
    servidor puede elegir un nombre libre; en la demo, el caso común es que
    quien llama ya sabe cómo nombrar su corrida.
    """

    audit_id: str | None = Field(default=None, max_length=40)
    scope_root: str = Field(min_length=1)

    model_config = {"json_schema_extra": {"examples": [
        {"audit_id": "demo-001", "scope_root": "C:/proyectos/demo"}
    ]}}


class AuditReportOut(BaseModel):
    """Informe completo de una auditoría terminada (estado + hallazgos)."""

    audit: AuditInfoOut
    findings: tuple[FindingOut, ...] = ()
    evidence: tuple[EvidenceOut, ...] = ()
    trace: tuple[TraceStepOut, ...] = ()

    @classmethod
    def from_run(cls, run: AuditRun) -> "AuditReportOut":
        return cls(
            audit=AuditInfoOut.from_run(run),
            findings=tuple(_finding_out(item) for item in run.findings),
            evidence=tuple(_evidence_out(item) for item in run.evidence),
            trace=tuple(_trace_out(item) for item in run.trace),
        )


# -- proyección ------------------------------------------------------------


def _steps_of(run: AuditRun) -> int:
    """Pasos realmente ejecutados según el trace publicado."""
    return sum(
        1 for record in run.trace if record.status.value != "skipped"
    )


def _finding_out(finding: Finding) -> FindingOut:
    return FindingOut(
        finding_id=finding.finding_id,
        title=finding.title,
        explanation=finding.explanation,
        severity=finding.severity.value,
        status=finding.status.value,
        origin=finding.origin.value,
        evidence_ids=finding.evidence_ids,
        recommendation=finding.recommendation,
        patch=(
            FindingPatchOut(
                file_path=finding.patch.file_path,
                before=finding.patch.before,
                after=finding.patch.after,
                explanation=finding.patch.explanation,
            )
            if finding.patch is not None
            else None
        ),
        uncertainty=finding.uncertainty,
        limitations=finding.limitations,
    )


def _evidence_out(evidence: Evidence) -> EvidenceOut:
    return EvidenceOut(
        evidence_id=evidence.evidence_id,
        source_path=evidence.source_path,
        tool=evidence.tool,
        snippet=evidence.snippet,
        line_start=evidence.line_start,
        line_end=evidence.line_end,
        limitations=evidence.limitations,
    )


def _trace_out(record: StepRecord) -> TraceStepOut:
    return TraceStepOut(
        step=record.step, status=record.status.value, detail=record.detail
    )


def audit_status_of(audit: Audit) -> str:
    """Atajo legible para el estado en respuestas cortas."""
    return audit.status.value


__all__ = [
    "AuditInfoOut",
    "AuditReportOut",
    "AuditStatus",
    "CreateAuditRequest",
    "EvidenceOut",
    "FindingOut",
    "FindingPatchOut",
    "TraceStepOut",
    "audit_status_of",
]

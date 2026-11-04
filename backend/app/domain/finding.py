"""Hallazgo: interpretación respaldada por evidencia.

Un hallazgo no es una opinión. Existen dos estados y la diferencia importa:

- ``CONFIRMED``: sus evidencias existen en la auditoría y están validadas por
  código determinista. Confirma la **evidencia**, no la gravedad ni que la
  vulnerabilidad sea explotable.
- ``HYPOTHESIS``: no hay evidencia disponible; se declara la incertidumbre y
  se presenta aparte como pendiente de verificación.

``origin`` distingue si la interpretación vino de una comprobación determinista
o de un modelo de IA. Un hallazgo de origen ``MODEL`` nunca se presenta como
certeza automática, aunque su evidencia esté validada.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from app.domain.evidence import Evidence
from app.domain.identifiers import IDENTIFIER_RULE, is_identifier
from app.domain.roles import FindingProposal, PatchSuggestion
from app.domain.severity import Severity


class FindingError(ValueError):
    """Error de regla de dominio en un hallazgo."""


class MissingEvidenceError(FindingError):
    """El hallazgo referencia evidencias que no existen en la auditoría."""


class FindingStatus(str, Enum):
    CONFIRMED = "confirmed"
    HYPOTHESIS = "hypothesis"


class FindingOrigin(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    title: str
    explanation: str
    severity: Severity
    status: FindingStatus
    origin: FindingOrigin
    evidence_ids: tuple[str, ...] = ()
    recommendation: str | None = None
    patch: PatchSuggestion | None = None
    uncertainty: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_identifier(self.finding_id):
            raise FindingError(f"finding_id {IDENTIFIER_RULE}")
        _validate_text(self.title, "title")
        _validate_text(self.explanation, "explanation")

        if not isinstance(self.severity, Severity):
            raise FindingError("severity debe ser un Severity")
        if not isinstance(self.status, FindingStatus):
            raise FindingError("status debe ser un FindingStatus")
        if not isinstance(self.origin, FindingOrigin):
            raise FindingError("origin debe ser un FindingOrigin")

        _validate_evidence_ids(self.evidence_ids)
        _validate_state(self.status, self.evidence_ids, self.uncertainty)

        if self.recommendation is not None:
            _validate_text(self.recommendation, "recommendation")
        if self.patch is not None and not isinstance(self.patch, PatchSuggestion):
            raise FindingError("patch debe ser un PatchSuggestion")
        if any(not isinstance(item, str) or not item.strip() for item in self.limitations):
            raise FindingError("limitations no admite entradas vacías")

    @property
    def is_confirmed(self) -> bool:
        return self.status is FindingStatus.CONFIRMED

    @classmethod
    def from_proposal(
        cls,
        proposal: FindingProposal,
        *,
        finding_id: str,
        evidence_index: Mapping[str, Evidence],
        origin: FindingOrigin = FindingOrigin.MODEL,
        recommendation: str | None = None,
    ) -> "Finding":
        """Convierte una propuesta de rol en hallazgo, validando la evidencia.

        La propuesta no se cree por sí misma: cada referencia debe existir en
        ``evidence_index``. Si falta alguna, la conversión falla y la propuesta
        no llega al informe como hallazgo.
        """

        missing = tuple(
            evidence_id
            for evidence_id in proposal.evidence_ids
            if evidence_id not in evidence_index
        )
        if missing:
            missing_list = ", ".join(missing)
            raise MissingEvidenceError(
                f"la propuesta cita evidencia inexistente: {missing_list}"
            )

        resolved_status = (
            FindingStatus.CONFIRMED if proposal.evidence_ids else FindingStatus.HYPOTHESIS
        )

        return cls(
            finding_id=finding_id,
            title=proposal.title,
            explanation=proposal.explanation,
            severity=proposal.severity,
            status=resolved_status,
            origin=origin,
            evidence_ids=proposal.evidence_ids,
            recommendation=recommendation,
            patch=proposal.patch,
            uncertainty=proposal.uncertainty,
            limitations=(),
        )


def _validate_state(
    status: FindingStatus,
    evidence_ids: tuple[str, ...],
    uncertainty: str | None,
) -> None:
    if status is FindingStatus.CONFIRMED and not evidence_ids:
        raise FindingError("un hallazgo confirmado exige al menos una evidencia")

    if status is FindingStatus.HYPOTHESIS:
        if evidence_ids:
            raise FindingError(
                "una hipótesis no puede citar evidencia: si la hay, es confirmada"
            )
        if not isinstance(uncertainty, str) or not uncertainty.strip():
            raise FindingError("una hipótesis debe declarar uncertainty")


def _validate_evidence_ids(evidence_ids: tuple[str, ...]) -> None:
    if not isinstance(evidence_ids, tuple):
        raise FindingError("evidence_ids debe ser una tupla")
    for evidence_id in evidence_ids:
        if not is_identifier(evidence_id):
            raise FindingError(f"evidence_id {IDENTIFIER_RULE}")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise FindingError("evidence_ids no admite duplicados")


def _validate_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FindingError(f"{field_name} no puede estar vacío")

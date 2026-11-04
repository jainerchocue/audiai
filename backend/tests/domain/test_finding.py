"""Reglas del Hallazgo: sin evidencia validada no hay confirmación."""

from __future__ import annotations

import pytest

from app.domain.evidence import Evidence
from app.domain.finding import (
    Finding,
    FindingError,
    FindingOrigin,
    FindingStatus,
    MissingEvidenceError,
)
from app.domain.roles import FindingProposal, PatchSuggestion
from app.domain.severity import Severity


def evidence_index() -> dict[str, Evidence]:
    return {
        "ev-001": Evidence(
            evidence_id="ev-001",
            source_path="src/app.py",
            tool="pattern_scan",
            snippet="eval(request.args['q'])",
            line_start=10,
        ),
        "ev-002": Evidence(
            evidence_id="ev-002",
            source_path="src/app.py",
            tool="read_file",
            snippet="def handler():",
            line_start=1,
        ),
    }


def confirmed_finding(**overrides) -> Finding:
    params = {
        "finding_id": "fnd-001",
        "title": "Uso de eval sobre entrada de usuario",
        "explanation": "La entrada llega sin validar a eval.",
        "severity": Severity.HIGH,
        "status": FindingStatus.CONFIRMED,
        "origin": FindingOrigin.DETERMINISTIC,
        "evidence_ids": ("ev-001",),
    }
    params.update(overrides)
    return Finding(**params)


def test_confirmed_finding_requires_evidence() -> None:
    with pytest.raises(FindingError):
        confirmed_finding(evidence_ids=())


def test_hypothesis_requires_uncertainty_and_no_evidence() -> None:
    with pytest.raises(FindingError):
        confirmed_finding(
            status=FindingStatus.HYPOTHESIS,
            evidence_ids=(),
            uncertainty=None,
        )

    with pytest.raises(FindingError):
        confirmed_finding(status=FindingStatus.HYPOTHESIS, uncertainty="pendiente")

    hypothesis = confirmed_finding(
        status=FindingStatus.HYPOTHESIS,
        evidence_ids=(),
        uncertainty="No se ha verificado el flujo de entrada.",
    )

    assert hypothesis.is_confirmed is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"finding_id": "F-001"},
        {"title": "   "},
        {"explanation": ""},
        {"severity": "high"},
        {"status": "confirmed"},
        {"origin": "model"},
        {"evidence_ids": ("ev-001", "ev-001")},
        {"evidence_ids": ("",)},
        {"patch": "diff"},
        {"limitations": ("",)},
        {"recommendation": "  "},
    ],
)
def test_invalid_findings_are_rejected(overrides: dict) -> None:
    with pytest.raises(FindingError):
        confirmed_finding(**overrides)


# -- conversión desde propuestas ----------------------------------------


def proposal(**overrides) -> FindingProposal:
    params = {
        "title": "Uso de eval",
        "explanation": "eval sobre entrada no validada.",
        "severity": Severity.HIGH,
        "evidence_ids": ("ev-001", "ev-002"),
    }
    params.update(overrides)
    return FindingProposal(**params)


def test_proposal_with_existing_evidence_becomes_confirmed() -> None:
    finding = Finding.from_proposal(
        proposal(), finding_id="fnd-010", evidence_index=evidence_index()
    )

    assert finding.status is FindingStatus.CONFIRMED
    assert finding.is_confirmed is True
    assert finding.origin is FindingOrigin.MODEL
    assert finding.evidence_ids == ("ev-001", "ev-002")


def test_proposal_with_missing_evidence_is_rejected() -> None:
    with pytest.raises(MissingEvidenceError) as excinfo:
        Finding.from_proposal(
            proposal(evidence_ids=("ev-001", "ev-999")),
            finding_id="fnd-011",
            evidence_index=evidence_index(),
        )

    assert "ev-999" in str(excinfo.value)


def test_proposal_without_evidence_becomes_a_hypothesis() -> None:
    finding = Finding.from_proposal(
        proposal(evidence_ids=(), uncertainty="Requiere revisar el despliegue."),
        finding_id="fnd-012",
        evidence_index=evidence_index(),
    )

    assert finding.status is FindingStatus.HYPOTHESIS
    assert finding.evidence_ids == ()


def test_patch_and_recommendation_are_carried_but_not_applied() -> None:
    patch = PatchSuggestion(
        description="Sustituir eval por un parser explícito", diff="--- a\n+++ b\n"
    )

    finding = Finding.from_proposal(
        proposal(patch=patch),
        finding_id="fnd-013",
        evidence_index=evidence_index(),
        recommendation="Validar la entrada antes de evaluar.",
    )

    assert finding.patch is patch
    assert finding.recommendation == "Validar la entrada antes de evaluar."
    assert not hasattr(finding, "apply")

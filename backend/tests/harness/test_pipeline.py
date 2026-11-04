"""Fase 6: recorrido fijo del harness con límites, fallos y cancelación.

Todo se prueba con fixtures propias (tmp_path) y ejecutores simulados: ninguna
prueba ejecuta código del "proyecto auditado".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.finding import FindingOrigin, FindingStatus
from app.domain.roles import FindingProposal, RoleName, RoleOutcome
from app.domain.severity import Severity
from app.harness.pipeline import (
    AuditHarness,
    PipelineLimits,
    PipelineStatus,
    StepStatus,
)
from app.providers.mock import MockRoleProvider, MockScript


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def process(value):\n    return eval(value)\n", encoding="utf-8"
    )
    (root / "src" / "settings.py").write_text(
        'API_KEY = "sk-live-9876543210ab"\n', encoding="utf-8"
    )
    (root / "clean.py").write_text("value = int(value)\n", encoding="utf-8")
    return root


def propose_foreign_evidence() -> MockScript:
    return MockScript(
        outcome=RoleOutcome.COMPLETED,
        summary="citando evidencia que no produje",
        proposals=(
            FindingProposal(
                title="Propuesta descartable",
                explanation="cita una evidencia ajena.",
                severity=Severity.HIGH,
                evidence_ids=("ev-fake-000",),
                uncertainty="cita ajena",
            ),
        ),
    )


# -- recorrido completo ---------------------------------------------------


def test_full_pipeline_completes_with_deterministic_findings(
    fixture_project: Path,
) -> None:
    outcome = AuditHarness(fixture_project, MockRoleProvider()).execute()

    assert outcome.status is PipelineStatus.COMPLETED
    assert outcome.reason is None
    assert outcome.findings, "las comprobaciones deterministas deben producir hallazgos"
    assert all(f.status is FindingStatus.CONFIRMED for f in outcome.findings)
    assert all(f.origin is FindingOrigin.DETERMINISTIC for f in outcome.findings)
    ids = [record.step for record in outcome.trace]
    assert ids[0] == "1.lectura"
    assert "4.validation" in ids
    assert all(record.status is StepStatus.OK for record in outcome.trace)


def test_analysis_mock_without_findings_does_not_invent_any(
    fixture_project: Path,
) -> None:
    """El mock genérico no produce propuestas: los hallazgos son solo deterministas."""

    outcome = AuditHarness(fixture_project, MockRoleProvider()).execute()

    assert all("MOCK(" not in finding.title for finding in outcome.findings)


def test_trace_counts_five_steps_by_default(fixture_project: Path) -> None:
    outcome = AuditHarness(fixture_project, MockRoleProvider()).execute()

    assert outcome.steps_used == 5  # 1 lectura + 2 checks + análise + validación


# -- propuestas con evidencia ajena ----------------------------------------


def test_proposal_citing_unknown_evidence_is_discarded_and_traced(
    fixture_project: Path,
) -> None:
    provider = MockRoleProvider({RoleName.CAUSE_ANALYSIS: propose_foreign_evidence()})

    outcome = AuditHarness(fixture_project, provider).execute()

    assert outcome.status is PipelineStatus.COMPLETED
    validation = [record for record in outcome.trace if record.step == "4.validation"]
    assert any(
        record.status is StepStatus.FAILED and "descartada" in record.detail
        for record in validation
    )
    assert tuple(finding.evidence_ids for finding in outcome.findings) != (
        ("ev-fake-000",),
    )


# -- límites ----------------------------------------------------------------


def test_max_steps_limit_fails_honestly(fixture_project: Path) -> None:
    outcome = AuditHarness(
        fixture_project,
        MockRoleProvider(),
        limits=PipelineLimits(max_steps=3),
    ).execute()

    assert outcome.status is PipelineStatus.FAILED
    assert "límite de pasos" in outcome.reason
    assert any(record.status is StepStatus.SKIPPED for record in outcome.trace)


def test_time_limit_fails_before_running_out_of_budget(fixture_project: Path) -> None:
    elapsed: list[float] = []

    def jumpy_clock() -> float:
        elapsed.append(10.0 if elapsed else 0.0)
        return elapsed[-1]

    outcome = AuditHarness(
        fixture_project,
        MockRoleProvider(),
        limits=PipelineLimits(max_seconds=5.0),
        clock=jumpy_clock,
    ).execute()

    assert outcome.status is PipelineStatus.FAILED
    assert "límite de tiempo" in outcome.reason


# -- proveedor que falla -----------------------------------------------------


def test_provider_failure_fails_the_whole_pipeline(fixture_project: Path) -> None:
    provider = MockRoleProvider(
        {
            RoleName.CAUSE_ANALYSIS: MockScript(
                outcome=RoleOutcome.FAILED,
                error="timeout simulado del ejecutor",
            )
        }
    )

    outcome = AuditHarness(fixture_project, provider).execute()

    assert outcome.status is PipelineStatus.FAILED
    assert "3.analysis" in outcome.reason
    assert "timeout simulado del ejecutor" in outcome.reason


def test_provider_refusal_completes_without_findings_from_it(
    fixture_project: Path,
) -> None:
    provider = MockRoleProvider(
        {
            RoleName.CAUSE_ANALYSIS: MockScript(
                outcome=RoleOutcome.REFUSED,
                summary="no tengo candidatos",
            )
        }
    )

    outcome = AuditHarness(fixture_project, provider).execute()

    assert outcome.status is PipelineStatus.COMPLETED
    analysis = [record for record in outcome.trace if record.step == "3.analysis"]
    assert "refused" in analysis[0].detail


# -- cancelación --------------------------------------------------------------


def test_should_stop_cancels_between_steps(fixture_project: Path) -> None:
    outcome = AuditHarness(
        fixture_project,
        MockRoleProvider(),
        should_stop=lambda: True,
    ).execute()

    assert outcome.status is PipelineStatus.CANCELLED
    assert "cancelado" in outcome.reason
    assert any(
        record.status is StepStatus.SKIPPED and "cancelado" in record.detail
        for record in outcome.trace
    )


# -- raíz inválida ------------------------------------------------------------


def test_missing_root_fails_the_reading_step(tmp_path: Path) -> None:
    outcome = AuditHarness(tmp_path / "no-existe", MockRoleProvider()).execute()

    assert outcome.status is PipelineStatus.FAILED
    assert "1.lectura" in outcome.reason

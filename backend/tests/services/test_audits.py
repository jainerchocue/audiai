"""Fase 6: servicio de auditorías, integración dominio + harness + mock."""

from __future__ import annotations

from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from app.domain.audit import Audit, AuditError, AuditStatus, ResultOrigin
from app.domain.roles import FindingProposal, RoleName, RoleOutcome
from app.domain.severity import Severity
from app.providers.mock import MockRoleProvider, MockScript
from app.services.audits import REAL_LIMITATIONS, AuditRun, AuditService


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def process(value):\n    return eval(value)\n", encoding="utf-8"
    )
    (root / "src" / "passwords.py").write_text(
        'ADMIN_PASSWORD = "clave-larga-12345"\n', encoding="utf-8"
    )
    return root


def foreign_evidence_script() -> MockScript:
    return MockScript(
        outcome=RoleOutcome.COMPLETED,
        summary="cita evidencia ajena",
        proposals=(
            FindingProposal(
                title="Propuesta irrecuperable",
                explanation="cita evidencia que no existe en este recorrido.",
                severity=Severity.MEDIUM,
                evidence_ids=("ev-ajena-000",),
                uncertainty="cita ajena",
            ),
        ),
    )


# -- recorrido completo ---------------------------------------------------


def test_service_runs_a_full_mock_audit_and_finishes_terminal(
    fixture_project: Path,
) -> None:
    service = AuditService(MockRoleProvider())

    run = service.execute(audit_id="audit-demo-001", scope_root=fixture_project)

    assert run.audit.status is AuditStatus.COMPLETED
    assert run.audit.origin is ResultOrigin.MOCK
    assert any("simulado" in note for note in run.audit.limitations)
    assert run.findings, "las comprobaciones deterministas deben hallar algo"
    assert all(
        set(f.evidence_ids)
        <= {e.evidence_id for e in run.evidence}
        for f in run.findings
    )
    steps = {record.step for record in run.trace}
    assert {"1.lectura", "2.checks:chk-dynamic-execution", "4.validation"} <= steps
    assert all(
        record.status.value != "failed" for record in run.trace
    )


def test_failed_provider_yields_a_failed_audit_with_reason(
    fixture_project: Path,
) -> None:
    provider = MockRoleProvider(
        {
            RoleName.CAUSE_ANALYSIS: MockScript(
                outcome=RoleOutcome.FAILED,
                error="error simulado del ejecutor",
            )
        }
    )

    run = AuditService(provider).execute(
        audit_id="audit-demo-002", scope_root=fixture_project
    )

    assert run.audit.status is AuditStatus.FAILED
    assert "3.analysis" in run.audit.failure_reason
    assert "error simulado del ejecutor" in run.audit.failure_reason


def test_foreign_evidence_is_discarded_but_the_audit_completes(
    fixture_project: Path,
) -> None:
    provider = MockRoleProvider({RoleName.CAUSE_ANALYSIS: foreign_evidence_script()})

    run = AuditService(provider).execute(
        audit_id="audit-demo-003", scope_root=fixture_project
    )

    assert run.audit.status is AuditStatus.COMPLETED
    discarded = [
        record
        for record in run.trace
        if record.status.value == "failed" and "descartada" in (record.detail or "")
    ]
    assert discarded
    assert all("ev-ajena-000" not in f.evidence_ids for f in run.findings)


# -- invariantes del registro ---------------------------------------------


def test_an_audit_run_rejects_a_non_terminal_audit(fixture_project: Path) -> None:
    pending = Audit.create(audit_id="audit-demo-004", scope_root=str(fixture_project))

    with pytest.raises(AuditError, match="terminadas"):
        AuditRun(
            audit=pending,
            evidence=(),
            findings=(),
            trace=(),
        )


def test_an_audit_run_accepts_real_attribution_with_provider(tmp_path: Path) -> None:
    """Cuando el subagente real corrió, el run se atribuye (provider+model)."""

    root = tmp_path / "demo"
    root.mkdir()

    real_audit = Audit.create(
        audit_id="audit-demo-005",
        scope_root=str(root),
        origin=ResultOrigin.REAL,
        provider="openrouter",
        model="subagente-demo",
        limitations=REAL_LIMITATIONS,
    ).transition(AuditStatus.RUNNING)
    real_audit = real_audit.transition(AuditStatus.COMPLETED)

    run = AuditRun(
        audit=real_audit,
        evidence=(),
        findings=(),
        trace=(),
    )
    assert run.audit.origin is ResultOrigin.REAL


def test_a_real_audit_without_provider_is_rejected(tmp_path: Path) -> None:
    """Un resultado real sin provider/model no puede existir (regla de dominio)."""

    root = tmp_path / "demo"
    root.mkdir()

    with pytest.raises(AuditError, match="provider y model"):
        Audit.create(
            audit_id="audit-demo-006b",
            scope_root=str(root),
            origin=ResultOrigin.REAL,
        )


def test_an_audit_run_rejects_findings_with_foreign_evidence(
    fixture_project: Path,
) -> None:
    provider = MockRoleProvider({RoleName.CAUSE_ANALYSIS: foreign_evidence_script()})

    run = AuditService(provider).execute(
        audit_id="audit-demo-006", scope_root=fixture_project
    )
    assert run.findings  # el recorrido mínimo debe haber hallado algo

    poisoned = tuple(
        dc_replace(finding, evidence_ids=("ev-ajena-000",))
        for finding in run.findings
    )

    with pytest.raises(AuditError, match="no produjo"):
        AuditRun(audit=run.audit, evidence=run.evidence, findings=poisoned, trace=run.trace)

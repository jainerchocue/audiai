"""Fase 4: comprobaciones deterministas con casos positivos y negativos.

Estas pruebas son los "casos esperados" del README: reproducibles, sin IA, con
ubicación verificable y con la prohibición explícita de llamar vulnerable a
una simple coincidencia.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.finding import Finding, FindingOrigin, FindingStatus
from app.domain.severity import Severity
from app.tools.checks import (
    BUNDLED_CHECKS,
    CHECK_UNCERTAINTY,
    CheckRun,
    CheckSpec,
    DeterministicChecks,
    DYNAMIC_EXECUTION_CHECK,
    evidence_index,
)


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    """Un mini proyecto: archivos con y sin hallazgos esperados."""

    root = tmp_path / "demo_project"
    (root / "app").mkdir(parents=True)
    (root / "config").mkdir()

    # positivo esperado: ejecución dinámica (2 líneas)
    (root / "app" / "routes.py").write_text(
        "def process(input):\n"
        "    return eval(input)\n"
        "\n"
        "def run_command(cmd):\n"
        "    exec(cmd)\n",
        encoding="utf-8",
    )

    # positivo esperado: credencial embebida
    (root / "app" / "settings.py").write_text(
        'HOST = "127.0.0.1"\n'
        'API_KEY = "sk-live-9876543210"\n'
        'PASSWORD = "admin-secreta-99"\n',
        encoding="utf-8",
    )

    # negativo: contenido limpio
    (root / "app" / "maths.py").write_text(
        "def twice(value):\n    return value * 2\n", encoding="utf-8"
    )

    # negativo: palabra suelta sin forma de asignación no debe activar la heurística
    (root / "docs.md").write_text(
        "documentación de password: use un gestor de secretos\n", encoding="utf-8"
    )

    # secreto real, pero excluido por política: nunca debe activar la comprobación
    (root / "config" / ".env").write_text(
        'DATABASE_PASSWORD = "super-secreta-01"\n', encoding="utf-8"
    )
    return root


def make_checks(root: Path) -> DeterministicChecks:
    return DeterministicChecks(root)


def test_check_spec_rejects_unknown_severity() -> None:
    with pytest.raises(ValueError, match="UNKNOWN"):
        CheckSpec(
            check_id="chk-demo",
            title="Demo",
            pattern="eval\\(",
            severity=Severity.UNKNOWN,
            explanation="demo",
        )


def test_check_spec_rejects_invalid_pattern() -> None:
    with pytest.raises(ValueError, match="pattern inválido"):
        CheckSpec(
            check_id="chk-demo",
            title="Demo",
            pattern="eval((",
            severity=Severity.MEDIUM,
            explanation="demo",
        )


# -- comprobación de ejecución dinámica ----------------------------------


def test_dynamic_execution_check_finds_only_the_flagged_file(
    fixture_project: Path,
) -> None:
    run = make_checks(fixture_project).run(DYNAMIC_EXECUTION_CHECK)
    flagged = {proposal.title for proposal in run.proposals}

    assert run.result.evidence  # hay línea(s) con match
    assert len(run.proposals) == 1
    assert "app/routes.py" in "".join(flagged)
    assert all("maths.py" not in title for title in flagged)
    assert all("docs.md" not in title for title in flagged)


def test_dynamic_execution_proposal_carries_the_uncertainty_note(
    fixture_project: Path,
) -> None:
    run = make_checks(fixture_project).run(DYNAMIC_EXECUTION_CHECK)

    for proposal in run.proposals:
        assert proposal.uncertainty == CHECK_UNCERTAINTY
        assert proposal.is_hypothesis is False  # sí hay evidencia


def test_dynamic_execution_severity_is_the_documented_one(
    fixture_project: Path,
) -> None:
    run = make_checks(fixture_project).run(DYNAMIC_EXECUTION_CHECK)

    for proposal in run.proposals:
        assert proposal.severity is Severity.MEDIUM


# -- comprobación de credencial embebida ---------------------------------


def test_hardcoded_credential_check_flags_only_real_assignments(
    fixture_project: Path,
) -> None:
    run = make_checks(fixture_project).run(BUNDLED_CHECKS[1])
    flagged = {proposal.title for proposal in run.proposals}

    assert any("app/settings.py" in title for title in flagged)
    # la palabra en doc.md no tiene forma asignación=valor largo, no se marca
    assert all("docs.md" not in title for title in flagged)
    # el secreto en .env está excluido por política: nunca entra a la comprobación
    assert all(".env" not in title for title in flagged)


def test_hardcoded_credential_check_flags_prefixed_names(
    tmp_path: Path,
) -> None:
    """Regresión: ``\\b`` fallaba con prefijos como ``ADMIN_PASSWORD``.

    El guion bajo es carácter de palabra, así que antes no había frontera
    justo antes del keyword y el check dejaba pasar credenciales reales.
    """

    root = tmp_path / "prefix"
    root.mkdir()
    (root / "auth.py").write_text(
        'ADMIN_PASSWORD = "clave-super-larga-001"\n'
        'OAUTH_TOKEN = "yt-9876543210abcdef"\n',
        encoding="utf-8",
    )
    (root / "clean.py").write_text(
        'RETRY_COUNT = "por_defecto"\n', encoding="utf-8"
    )

    run = make_checks(root).run(BUNDLED_CHECKS[1])
    flagged = {proposal.title for proposal in run.proposals}

    assert any("auth.py" in title for title in flagged)
    assert all("clean.py" not in title for title in flagged)


# -- conversion a hallazgo ------------------------------------------------


def test_evidence_index_is_consistent_with_proposals(fixture_project: Path) -> None:
    checker = make_checks(fixture_project)
    runs = checker.run_all()
    combined = evidence_index(runs)

    for run in runs:
        for proposal in run.proposals:
            assert all(evidence_id in combined for evidence_id in proposal.evidence_ids)
            finding = Finding.from_proposal(
                proposal,
                finding_id=f"fnd-{len(combined):04d}",
                evidence_index=combined,
                origin=FindingOrigin.DETERMINISTIC,
            )
            assert finding.status is FindingStatus.CONFIRMED
            assert finding.origin is FindingOrigin.DETERMINISTIC
            assert finding.uncertainty == CHECK_UNCERTAINTY


def test_checks_are_reproducible_between_runs(fixture_project: Path) -> None:
    first = make_checks(fixture_project).run_all()
    second = make_checks(fixture_project).run_all()

    first_signal = [
        (run.check_id, p.title, p.severity, p.evidence_ids) for run in first for p in run.proposals
    ]
    second_signal = [
        (run.check_id, p.title, p.severity, p.evidence_ids) for run in second for p in run.proposals
    ]

    assert first_signal == second_signal
    assert all(
        evidence.snippet == counterpart.snippet
        for run_a, run_b in zip(first, second)
        for evidence, counterpart in zip(run_a.result.evidence, run_b.result.evidence)
    )


# -- límites y honestidad --------------------------------------------------


def test_check_respects_max_matches_limit_and_declares_it(
    fixture_project: Path,
) -> None:
    from app.domain.tools import ToolLimits

    checker = make_checks(fixture_project)
    run = checker.run(
        BUNDLED_CHECKS[1], limits=ToolLimits(max_matches=1)
    )

    assert run.result.outcome.value == "truncated"
    assert len(run.result.evidence) == 1
    assert any("max_matches" in note for note in run.result.limitations)
    # las propuestas solo se formulan con lo que se realmente leyó
    combined = evidence_index((run,))
    assert len(combined) == 1


def test_checkrun_rejects_proposals_with_foreign_evidence() -> None:
    from app.domain.tools import ToolOutcome, ToolName, ToolResult
    from app.domain.evidence import Evidence
    from app.domain.roles import FindingProposal

    evidence = Evidence(
        evidence_id="ev-x000",
        source_path="src/app.py",
        tool="pattern_scan",
        snippet="eval(input)",
    )
    result = ToolResult(
        tool=ToolName.PATTERN_SCAN,
        outcome=ToolOutcome.COMPLETED,
        evidence=(evidence,),
    )
    foreign_proposal = FindingProposal(
        title="No coincide",
        explanation="cita otra evidencia",
        severity=Severity.MEDIUM,
        evidence_ids=("ev-otro",),
        uncertainty="demo",
    )

    with pytest.raises(ValueError, match="no produjo"):
        CheckRun(
            check_id="chk-demo",
            result=result,
            proposals=(foreign_proposal,),
        )

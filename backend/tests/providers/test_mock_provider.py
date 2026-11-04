"""Fase 5: MockRoleProvider determinista, sin llamadas reales a IA.

Cubre los tres requerimientos del README: respuestas válidas, inválidas y
errores simulados; indicador obligatorio de simulación; cero llamadas reales.
"""

from __future__ import annotations

import pytest

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ContextItem,
    FindingProposal,
    RoleName,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.domain.severity import Severity
from app.providers.mock import MockRoleProvider, MockScript

CONTEXT = (ContextItem(evidence_id="ev-001", snippet="eval(input)"),)


def make_request(role: RoleName = RoleName.CAUSE_ANALYSIS) -> RoleRequest:
    return RoleRequest(
        role=role,
        instruction="Explica la causa observada en el fragmento.",
        context=CONTEXT,
    )


def run_scripted(script: MockScript) -> RoleResult:
    provider = MockRoleProvider({RoleName.CAUSE_ANALYSIS: script})
    return provider.run(make_request())


# -- respuesta genérica (sin script pregrabado) --------------------------


def test_without_context_the_mock_refuses_instead_of_inventing() -> None:
    empty_request = RoleRequest(
        role=RoleName.CAUSE_ANALYSIS,
        instruction="Analiza lo que no te entregaron.",
    )
    provider = MockRoleProvider()

    result = provider.run(empty_request)

    assert result.outcome is RoleOutcome.REFUSED
    assert result.proposals == ()
    assert "no fabrica conclusiones" in result.summary


def test_with_context_the_mock_completes_and_labels_itself() -> None:
    result = MockRoleProvider().run(make_request())

    assert result.outcome is RoleOutcome.COMPLETED
    assert result.origin is ResultOrigin.MOCK
    assert result.provider is None and result.model is None
    assert result.summary.startswith("MOCK(mock): ")
    assert result.proposals == ()


def test_the_summary_counts_the_context_honestly() -> None:
    result = MockRoleProvider().run(make_request())

    # "eval(input)" mide 11 bytes en utf-8: el mock reporta la medida real
    assert "recibió 1 fragmento(s) (11 bytes" in result.summary


def test_mock_is_deterministic_between_calls() -> None:
    provider = MockRoleProvider()

    assert provider.run(make_request()) == provider.run(make_request())


# -- scripts pregrabados --------------------------------------------------


def test_preconfigured_script_completes_with_proposals() -> None:
    proposal = FindingProposal(
        title="Uso de eval simulado",
        explanation="El mock marca la causa.",
        severity=Severity.LOW,
        uncertainty="resultado de una simulación",
    )
    script = MockScript(
        outcome=RoleOutcome.COMPLETED,
        summary="causa simulada por script",
        proposals=(proposal,),
    )

    result = run_scripted(script)

    assert result.outcome is RoleOutcome.COMPLETED
    assert result.proposals == (proposal,)
    assert result.summary.startswith("MOCK(mock): causa simulada")
    assert result.origin is ResultOrigin.MOCK
    assert result.provider is None and result.model is None


def test_preconfigured_script_can_simulate_a_failure() -> None:
    script = MockScript(outcome=RoleOutcome.FAILED, error="timeout simulado del mock")

    result = run_scripted(script)

    assert result.outcome is RoleOutcome.FAILED
    assert result.error == "timeout simulado del mock"


def test_preconfigured_script_can_refuse() -> None:
    result = run_scripted(MockScript(outcome=RoleOutcome.REFUSED))

    assert result.outcome is RoleOutcome.REFUSED
    assert result.proposals == ()


# -- esquema inválido gestionado explícitamente ---------------------------


def test_script_claiming_a_real_provider_is_rejected_explicitly() -> None:
    script = MockScript(
        outcome=RoleOutcome.COMPLETED,
        summary="intenta atribuirse un modelo real",
        provider="proveedor-real",
        model="modelo-real",
    )

    result = run_scripted(script)

    assert result.outcome is RoleOutcome.FAILED
    assert result.origin is ResultOrigin.MOCK
    assert "esquema inválido" in result.error
    assert "provider" in result.error or "model" in result.error


def test_completed_script_without_summary_is_rejected_explicitly() -> None:
    result = run_scripted(MockScript(outcome=RoleOutcome.COMPLETED))

    assert result.outcome is RoleOutcome.FAILED
    assert "esquema inválido" in result.error


def test_refused_script_with_proposals_is_rejected_explicitly() -> None:
    proposal = FindingProposal(
        title="No puede proponer",
        explanation="un refused no propone",
        uncertainty="demo",
    )
    result = run_scripted(MockScript(outcome=RoleOutcome.REFUSED, proposals=(proposal,)))

    assert result.outcome is RoleOutcome.FAILED
    assert "esquema inválido" in result.error


# -- trazabilidad ---------------------------------------------------------


def test_custom_label_appears_in_every_summary() -> None:
    result = MockRoleProvider(label="beta").run(make_request())

    assert result.summary.startswith("MOCK(beta): ")


def test_invalid_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="label"):
        MockRoleProvider(label="Este Label NO")

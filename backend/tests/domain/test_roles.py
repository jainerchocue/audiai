"""Contrato de roles (punto de extensión para subagentes).

Estas pruebas fijan la frontera, no la implementación: garantizan que ningún
rol pueda ampliar su alcance, declarar red sin permiso, atribuirse un modelo
real estando simulado, ni presentar una hipótesis como hallazgo.
"""

from __future__ import annotations

import pytest

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ContextItem,
    FindingProposal,
    PatchSuggestion,
    RoleError,
    RoleExecutor,
    RoleLimits,
    RoleName,
    RoleNotConfigured,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.domain.severity import Severity
from app.providers.unavailable import UnavailableRoleExecutor


def ok_request(**overrides) -> RoleRequest:
    params = {
        "role": RoleName.CAUSE_ANALYSIS,
        "instruction": "Explica la causa probable del hallazgo propuesto.",
        "context": (ContextItem(evidence_id="ev-001", snippet="eval(input())"),),
    }
    params.update(overrides)
    return RoleRequest(**params)


# -- petición ------------------------------------------------------------


@pytest.mark.parametrize("role", list(RoleName))
def test_every_role_accepts_a_well_formed_request(role: RoleName) -> None:
    request = ok_request(
        role=role, limits=RoleLimits(allow_network=(role is RoleName.WEB_RESEARCH))
    )

    assert request.role is role
    assert request.context_bytes == len("eval(input())".encode("utf-8"))


@pytest.mark.parametrize("bad_instruction", ["", "   "])
def test_blank_instruction_is_rejected(bad_instruction: str) -> None:
    with pytest.raises(RoleError):
        ok_request(instruction=bad_instruction)


def test_instruction_has_a_length_cap() -> None:
    with pytest.raises(RoleError):
        ok_request(instruction="x" * 4_001)


def test_context_over_the_byte_limit_is_rejected() -> None:
    with pytest.raises(RoleError):
        ok_request(
            context=(ContextItem(evidence_id="ev-001", snippet="a" * 200),),
            limits=RoleLimits(max_context_bytes=100),
        )


def test_context_rejects_non_context_items() -> None:
    with pytest.raises(RoleError):
        ok_request(context=("ev-001",))


def test_network_is_only_allowed_for_web_research() -> None:
    with pytest.raises(RoleError):
        ok_request(limits=RoleLimits(allow_network=True))

    with pytest.raises(RoleError):
        ok_request(role=RoleName.DOC_REVIEW, limits=RoleLimits(allow_network=True))

    request = ok_request(
        role=RoleName.WEB_RESEARCH, limits=RoleLimits(allow_network=True)
    )

    assert request.limits.allow_network is True


def test_context_item_requires_evidence_reference() -> None:
    with pytest.raises(RoleError):
        ContextItem(evidence_id="  ", snippet="x")


def test_context_size_is_measured_in_utf8_bytes() -> None:
    assert ContextItem(evidence_id="ev-001", snippet="ñ").size == 2


# -- presupuesto ---------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_context_bytes": 0},
        {"max_model_calls": 0},
        {"timeout_seconds": 0},
    ],
)
def test_limits_must_be_positive(kwargs: dict) -> None:
    with pytest.raises(RoleError):
        RoleLimits(**kwargs)


# -- parche propuesto ----------------------------------------------------


def test_patch_suggestion_is_text_only_and_bounded() -> None:
    patch = PatchSuggestion(description="Valida la entrada", diff="--- a\n+++ b\n")

    assert not hasattr(patch, "apply")
    assert not hasattr(patch, "write")

    with pytest.raises(RoleError):
        PatchSuggestion(description="   ")
    with pytest.raises(RoleError):
        PatchSuggestion(description="válido", diff="x" * 20_001)


# -- propuestas ----------------------------------------------------------


def test_proposal_without_evidence_must_declare_uncertainty() -> None:
    with pytest.raises(RoleError):
        FindingProposal(
            title="Posible inyección", explanation="El patrón sugiere riesgo."
        )

    proposal = FindingProposal(
        title="Posible inyección",
        explanation="El patrón sugiere riesgo.",
        uncertainty="No se ha verificado el flujo de entrada.",
    )

    assert proposal.is_hypothesis is True
    assert proposal.severity is Severity.UNKNOWN


def test_proposal_with_evidence_is_not_a_hypothesis() -> None:
    proposal = FindingProposal(
        title="Uso de eval",
        explanation="eval sobre entrada no validada.",
        severity=Severity.HIGH,
        evidence_ids=("ev-001",),
    )

    assert proposal.is_hypothesis is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "  ", "explanation": "x"},
        {"title": "t", "explanation": "   "},
        {
            "title": "t",
            "explanation": "x",
            "evidence_ids": ("ev-001", "ev-001"),
        },
        {"title": "t", "explanation": "x", "evidence_ids": ("",)},
        {
            "title": "t",
            "explanation": "x",
            "evidence_ids": ("ev-001",),
            "patch": "no es un parche",
        },
    ],
)
def test_invalid_proposals_are_rejected(kwargs: dict) -> None:
    with pytest.raises(RoleError):
        FindingProposal(**kwargs)


# -- resultado -----------------------------------------------------------


def test_completed_result_requires_summary() -> None:
    with pytest.raises(RoleError):
        RoleResult(role=RoleName.DOCUMENTATION, outcome=RoleOutcome.COMPLETED)

    result = RoleResult(
        role=RoleName.DOCUMENTATION,
        outcome=RoleOutcome.COMPLETED,
        summary="Documento redactado con 2 secciones.",
    )

    assert result.is_simulated is True


def test_failed_result_requires_error() -> None:
    with pytest.raises(RoleError):
        RoleResult(role=RoleName.RISK_RESEARCH, outcome=RoleOutcome.FAILED)

    result = RoleResult(
        role=RoleName.RISK_RESEARCH,
        outcome=RoleOutcome.FAILED,
        error="timeout del ejecutor",
    )

    assert result.error == "timeout del ejecutor"


def test_error_is_forbidden_when_not_failed() -> None:
    with pytest.raises(RoleError):
        RoleResult(
            role=RoleName.RISK_RESEARCH,
            outcome=RoleOutcome.COMPLETED,
            summary="ok",
            error="algo",
        )


@pytest.mark.parametrize("outcome", [RoleOutcome.REFUSED, RoleOutcome.FAILED])
def test_only_completed_results_can_emit_proposals(outcome: RoleOutcome) -> None:
    kwargs = {"error": "sin contexto suficiente"} if outcome is RoleOutcome.FAILED else {}

    with pytest.raises(RoleError):
        RoleResult(
            role=RoleName.PATCH_SUGGESTION,
            outcome=outcome,
            proposals=(
                FindingProposal(
                    title="t", explanation="x", uncertainty="pendiente"
                ),
            ),
            **kwargs,
        )


def test_simulated_result_cannot_claim_a_model() -> None:
    with pytest.raises(RoleError):
        RoleResult(
            role=RoleName.CAUSE_ANALYSIS,
            outcome=RoleOutcome.COMPLETED,
            summary="ok",
            provider="proveedor-x",
        )


@pytest.mark.parametrize(
    ("provider", "model"), [(None, None), ("proveedor-x", None), ("  ", "modelo-y")]
)
def test_real_result_requires_provider_and_model(provider, model) -> None:
    with pytest.raises(RoleError):
        RoleResult(
            role=RoleName.CAUSE_ANALYSIS,
            outcome=RoleOutcome.COMPLETED,
            summary="ok",
            origin=ResultOrigin.REAL,
            provider=provider,
            model=model,
        )


def test_real_result_with_provenance_is_accepted() -> None:
    result = RoleResult(
        role=RoleName.CAUSE_ANALYSIS,
        outcome=RoleOutcome.COMPLETED,
        summary="Causa identificada.",
        origin=ResultOrigin.REAL,
        provider="proveedor-x",
        model="modelo-y",
    )

    assert result.is_simulated is False


# -- conector vacío ------------------------------------------------------


def test_unavailable_executor_fails_closed() -> None:
    executor = UnavailableRoleExecutor()

    assert isinstance(executor, RoleExecutor)

    with pytest.raises(RoleNotConfigured) as excinfo:
        executor.run(ok_request())

    assert "no simula resultados" in str(excinfo.value)

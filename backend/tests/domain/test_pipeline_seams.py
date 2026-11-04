"""Pruebas de integración entre contratos del dominio.

No hacen I/O real: verifican que la cadena completa —herramienta, evidencia,
rol, hallazgo— se compone con los contratos que existen, y que los fallos
fracasan en el punto correcto. Transporte, persistencia e IA siguen fuera.
"""

from __future__ import annotations

import pytest

from app.domain.evidence import Evidence
from app.domain.finding import (
    Finding,
    FindingOrigin,
    FindingStatus,
    MissingEvidenceError,
)
from app.domain.roles import (
    ContextItem,
    FindingProposal,
    RoleOutcome,
    RoleRequest,
    RoleResult,
    RoleName,
)
from app.domain.severity import Severity
from app.domain.tools import ToolOutcome, ToolRequest, ToolResult, ToolName

# -- cadena completa ------------------------------------------------------


def build_tool_result() -> ToolResult:
    """Lo que una herramienta real entregaría tras escanear con límites."""

    request = ToolRequest(
        tool=ToolName.PATTERN_SCAN,
        pattern="eval(",
        relative_path="src/app.py",
    )

    evidence = Evidence(
        evidence_id="ev-001",
        source_path=request.relative_path,
        tool=request.tool.value,
        snippet="eval(user_input)",
        line_start=7,
    )

    return ToolResult(
        tool=request.tool,
        outcome=ToolOutcome.COMPLETED,
        evidence=(evidence,),
    )


def build_role_result(evidence_ids: tuple[str, ...]) -> RoleResult:
    """Lo que un rol entregaría en Fase 5, conectándose solo con contrato."""

    proposal = FindingProposal(
        title="Uso de eval sobre entrada no validada",
        explanation="La entrada llega a eval sin filtrar.",
        severity=Severity.HIGH,
        evidence_ids=evidence_ids,
    )

    return RoleResult(
        role=RoleName.CAUSE_ANALYSIS,
        outcome=RoleOutcome.COMPLETED,
        summary="Causa identificada con respaldo de evidencia.",
        proposals=(proposal,),
    )


def test_tool_evidence_role_and_finding_compose() -> None:
    tool_result = build_tool_result()
    evidence_index = {item.evidence_id: item for item in tool_result.evidence}

    role_result = build_role_result(evidence_ids=("ev-001",))
    finding = Finding.from_proposal(
        role_result.proposals[0],
        finding_id="fnd-001",
        evidence_index=evidence_index,
    )

    assert tool_result.is_complete is True
    assert role_result.is_simulated is True
    assert finding.status is FindingStatus.CONFIRMED
    assert finding.origin is FindingOrigin.MODEL
    assert finding.evidence_ids == tuple(evidence_index)

    evidence = next(iter(evidence_index.values()))
    assert evidence.location == "src/app.py:7"


def test_role_citing_evidence_the_tool_never_produced_is_rejected() -> None:
    tool_result = build_tool_result()
    evidence_index = {item.evidence_id: item for item in tool_result.evidence}

    role_result = build_role_result(evidence_ids=("ev-999",))

    with pytest.raises(MissingEvidenceError):
        Finding.from_proposal(
            role_result.proposals[0],
            finding_id="fnd-002",
            evidence_index=evidence_index,
        )


def test_role_without_evidence_becomes_a_hypothesis() -> None:
    proposal = FindingProposal(
        title="Posible dependencia obsoleta",
        explanation="Se observó en el árbol de dependencias, no se verificó.",
        severity=Severity.MEDIUM,
        evidence_ids=(),
        uncertainty="No se leyó el archivo de lock.",
    )

    role_result = RoleResult(
        role=RoleName.RISK_RESEARCH,
        outcome=RoleOutcome.COMPLETED,
        summary="Riesgo sin evidencia directa.",
        proposals=(proposal,),
    )

    finding = Finding.from_proposal(
        role_result.proposals[0],
        finding_id="fnd-003",
        evidence_index={},
    )

    assert finding.status is FindingStatus.HYPOTHESIS
    assert not finding.is_confirmed


def test_finding_record_knows_its_tool_origin() -> None:
    """La evidencia y el hallazgo comparten vocabulario de herramienta."""

    tool_result = build_tool_result()

    for evidence_item in tool_result.evidence:
        assert evidence_item.tool == ToolName.PATTERN_SCAN.value


def test_context_items_fit_the_role_request_limits() -> None:
    """El harness entrega snippets acotados y la petición sigue siendo válida."""

    request = RoleRequest(
        role=RoleName.CAUSE_ANALYSIS,
        instruction="Explica la causa del patrón observado.",
        context=(ContextItem(evidence_id="ev-001", snippet="eval(user_input)"),),
    )

    assert request.context_bytes == len("eval(user_input)".encode("utf-8"))

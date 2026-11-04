"""Fase 11: chat del agente, flujo completo sin red y con doble mock.

La secuencia de aprobaciones es el mismo recorrido del harness, con puertas
humanas; nada se ejecuta sin ``approve`` y solo la acción pendiente es
aceptada. Los fallos del rol se declaran en el chat, nunca se simulan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ChatResponse,
    FindingProposal,
    RoleName,
    RoleOutcome,
    RoleRequest,
    RoleResult,
)
from app.providers.mock import MockRoleProvider, MockScript
from app.domain.severity import Severity
from app.services.agent_chat import (
    AgentChatError,
    AgentChatService,
    StaleActionError,
    UnknownSessionError,
)


class RecordingRoleProvider:
    def __init__(self) -> None:
        self.requests: list[RoleRequest] = []
        self.delegate = MockRoleProvider()

    def run(self, request: RoleRequest) -> RoleResult:
        self.requests.append(request)
        return self.delegate.run(request)


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def process(value):\n    return eval(value)\n", encoding="utf-8"
    )
    (root / "src" / "secretos.py").write_text(
        'ADMIN_PASSWORD = "clave-larga-12345"\n', encoding="utf-8"
    )
    (root / "clean.py").write_text("value = int(value)\n", encoding="utf-8")
    return root


@pytest.fixture
def service() -> AgentChatService:
    return AgentChatService(MockRoleProvider(), provider_label="mock")


def test_full_flow_produces_findings_docs_and_patches(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    assert state.state == "esperando_ruta"
    assert state.messages[-1].author == "agent"

    state = service.send_message(state.session_id, str(fixture_project))
    assert state.pending.kind == "list"
    assert state.pending.caption  # el agente pregunta antes de ejecutar

    state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "checks"
    source_evidence = next(
        item
        for item in state.evidence
        if item.source_path == "src/app.py" and item.tool == "read_file"
    )
    assert "return eval(value)" in source_evidence.snippet
    context, _omitted = state.context_items()
    assert any("return eval(value)" in item.snippet for item in context)
    listed_authors = [message.author for message in state.messages]
    assert listed_authors.count("user") == 1  # la ruta entró como turno

    state = service.approve(state.session_id, state.pending.action_id)
    assert len(state.evidence) >= 3  # listado + comprobaciones deterministas
    assert len(state.findings) >= 1
    assert any(item.kind == "checks" for item in state.ready_items)
    assert state.pending.kind == "security"

    state = service.approve(state.session_id, state.pending.action_id)
    assert any(item.kind == "security" for item in state.ready_items)
    assert state.pending.kind == "analysis"

    state = service.approve(state.session_id, state.pending.action_id)
    assert state.analysis_origin == "mock"  # el MOCK se declara explícito
    assert state.pending.kind == "sources"

    state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "docs"

    state = service.approve(state.session_id, state.pending.action_id)
    assert len(state.docs) >= 2  # resumen + una sección por hallazgo
    assert all(
        doc.origin in {
            "plantilla local (sin modelo)",
            "origen=mock",
        }
        for doc in state.docs
    )
    assert any(item.kind == "docs" for item in state.ready_items)
    assert state.pending.kind == "patches"

    state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "review"

    state = service.approve(state.session_id, state.pending.action_id)
    assert state.state == "completa"
    assert len(state.patches) == len(state.findings)
    assert all(patch.diff is None for patch in state.patches)
    assert any(item.kind == "review" for item in state.ready_items)
    assert any(doc.title == "Revisión final de calidad" for doc in state.docs)


def test_sensitive_file_requires_approval_before_its_content_is_read(
    service: AgentChatService, fixture_project: Path
) -> None:
    secret = fixture_project / ".env"
    secret.write_text("API_TOKEN=private-value\n", encoding="utf-8")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    state = service.approve(state.session_id, state.pending.action_id)

    assert state.pending.kind == "sensitive"
    assert "private-value" not in "\n".join(message.text for message in state.messages)
    assert all(item.source_path != ".env" for item in state.evidence)

    state = service.approve(state.session_id, state.pending.action_id)

    sensitive_evidence = next(item for item in state.evidence if item.source_path == ".env")
    assert sensitive_evidence.snippet.replace("\r\n", "\n") == "API_TOKEN=private-value\n"
    assert state.sensitive_read_approved is True
    context, _omitted = state.context_items()
    assert "API_TOKEN=private-value" in context[0].snippet
    assert state.pending.kind == "checks"


def test_skipping_sensitive_file_continues_without_reading_it(
    service: AgentChatService, fixture_project: Path
) -> None:
    secret = fixture_project / ".env"
    secret.write_text("API_TOKEN=private-value\n", encoding="utf-8")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    state = service.approve(state.session_id, state.pending.action_id)

    state = service.skip(state.session_id, state.pending.action_id)

    assert state.pending.kind == "checks"
    assert all(item.source_path != ".env" for item in state.evidence)
    assert any("No se leyó su contenido" in message.text for message in state.messages)


def test_sensitive_read_stays_within_the_total_byte_budget(
    service: AgentChatService, fixture_project: Path
) -> None:
    for index in range(33):
        (fixture_project / f".env.{index:02d}").write_text(
            "x" * 16_000, encoding="utf-8"
        )
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "sensitive"

    state = service.approve(state.session_id, state.pending.action_id)

    sensitive_evidence = [
        item for item in state.evidence if item.source_path.startswith(".env.")
    ]
    assert len(sensitive_evidence) == 32
    assert any("Se omitieron 1 archivo(s)" in message.text for message in state.messages)
    assert state.pending.kind == "checks"


def test_external_model_requires_one_session_consent_or_can_be_skipped_locally(
    fixture_project: Path,
) -> None:
    service = AgentChatService(MockRoleProvider(), provider_label="strands")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    state = service.approve(state.session_id, state.pending.action_id)
    state = service.approve(state.session_id, state.pending.action_id)

    assert state.pending.kind == "security"
    local_state = service.skip(state.session_id, state.pending.action_id)

    assert local_state.state == "completa"
    assert local_state.external_data_consent is False
    assert any(patch.origin == "plantilla local (sin modelo)" for patch in local_state.patches)


def test_external_model_consent_is_recorded_for_the_session(
    fixture_project: Path,
) -> None:
    service = AgentChatService(MockRoleProvider(), provider_label="strands")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    state = service.approve(state.session_id, state.pending.action_id)
    state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "security"
    assert state.external_data_consent is False

    state = service.approve(state.session_id, state.pending.action_id)

    assert state.external_data_consent is True
    assert state.pending.kind == "analysis"


def test_final_review_checks_evidence_and_reviews_prior_artifacts(
    fixture_project: Path,
) -> None:
    class ReviewRoles:
        def __init__(self) -> None:
            self.requests: list[RoleRequest] = []
            self.mock = MockRoleProvider()

        def run(self, request: RoleRequest) -> RoleResult:
            self.requests.append(request)
            if request.role is not RoleName.DOC_REVIEW:
                return self.mock.run(request)
            evidence = next(
                item for item in request.context if "eval(" in item.snippet
            )
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.COMPLETED,
                summary="Se contrastaron informe y mitigaciones con el código.",
                origin=ResultOrigin.MOCK,
                proposals=(
                    FindingProposal(
                        title="Observación respaldada",
                        explanation="El parche debe validar el tipo recibido.",
                        severity=Severity.MEDIUM,
                        evidence_ids=(evidence.evidence_id,),
                    ),
                    FindingProposal(
                        title="Observación sin evidencia",
                        explanation="Esta cita no existe en la sesión.",
                        severity=Severity.HIGH,
                        evidence_ids=("ev-fuera",),
                    ),
                ),
            )

    roles = ReviewRoles()
    service = AgentChatService(roles, provider_label="mock")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(8):
        state = service.approve(state.session_id, state.pending.action_id)

    review_request = next(
        request
        for request in roles.requests
        if request.role is RoleName.DOC_REVIEW
    )
    review_section = next(
        section
        for section in state.docs
        if section.title == "Revisión final de calidad"
    )
    assert review_request.limits.allow_network is False
    assert "Propuestas de parche" in review_request.instruction
    assert len(review_section.evidence_ids) == 1
    assert "Observación respaldada" in review_section.body
    assert "Observación sin evidencia" not in review_section.body


def test_failed_final_review_is_visible_and_not_marked_complete(
    fixture_project: Path,
) -> None:
    class FailingReviewRoles:
        def __init__(self) -> None:
            self.mock = MockRoleProvider()

        def run(self, request: RoleRequest) -> RoleResult:
            if request.role is RoleName.DOC_REVIEW:
                return RoleResult(
                    role=request.role,
                    outcome=RoleOutcome.FAILED,
                    error="revisor no disponible",
                )
            return self.mock.run(request)

    service = AgentChatService(FailingReviewRoles(), provider_label="mock")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(8):
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert any(
        item.title == "Agente revisor no completado"
        for item in state.ready_items
    )
    assert not any(
        section.title == "Revisión final de calidad" for section in state.docs
    )
    assert any("revisión final no se completó" in item.text for item in state.messages)


def test_stale_action_is_rejected_with_explicit_order(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))

    with pytest.raises(StaleActionError, match="no es la pendiente"):
        service.approve(state.session_id, "act-099")


def test_analysis_mode_is_added_to_context_and_can_be_changed(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        "Revisa un posible error de autorización",
        analysis_mode="research",
    )
    assert state.user_context == "Revisa un posible error de autorización"
    assert state.role_context().endswith(
        "Enfoque solicitado: investiga el problema relacionando hipótesis "
        "con evidencia verificable. No realices búsquedas externas."
    )

    state = service.send_message(
        state.session_id,
        str(fixture_project),
        analysis_mode="unspecified",
    )

    assert state.user_context == "Revisa un posible error de autorización"
    assert state.role_context() == state.user_context
    assert state.pending.kind == "list"


def test_single_agent_mode_uses_one_role_call_and_local_outputs(
    fixture_project: Path,
) -> None:
    roles = RecordingRoleProvider()
    service = AgentChatService(roles, provider_label="mock")
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        str(fixture_project),
        agent_mode="agent",
    )
    while state.pending is not None:
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert state.agent_mode == "agent"
    assert [request.role for request in roles.requests] == [RoleName.CAUSE_ANALYSIS]
    assert any(item.kind == "analysis" for item in state.ready_items)
    assert any(item.kind == "docs" for item in state.ready_items)
    assert any(item.kind == "patches" for item in state.ready_items)
    assert not any(item.kind in {"security", "sources", "review"} for item in state.ready_items)


def test_multiagent_mode_runs_each_specialist_role(
    fixture_project: Path,
) -> None:
    roles = RecordingRoleProvider()
    service = AgentChatService(roles, provider_label="strands")
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        str(fixture_project),
        agent_mode="multiagent",
    )
    while state.pending is not None:
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert state.external_data_consent is True
    assert [request.role for request in roles.requests] == [
        RoleName.RISK_RESEARCH,
        RoleName.CAUSE_ANALYSIS,
        RoleName.WEB_RESEARCH,
        RoleName.DOCUMENTATION,
        RoleName.PATCH_SUGGESTION,
        RoleName.DOC_REVIEW,
    ]


def test_lmstudio_multiagent_runs_locally_without_external_consent(
    fixture_project: Path,
) -> None:
    roles = RecordingRoleProvider()
    service = AgentChatService(roles, provider_label="lmstudio")
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        str(fixture_project),
        agent_mode="multiagent",
    )
    while state.pending is not None:
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert state.external_data_consent is False
    assert [request.role for request in roles.requests] == [
        RoleName.RISK_RESEARCH,
        RoleName.CAUSE_ANALYSIS,
        RoleName.WEB_RESEARCH,
        RoleName.DOCUMENTATION,
        RoleName.PATCH_SUGGESTION,
        RoleName.DOC_REVIEW,
    ]


def test_lmstudio_agent_mode_uses_one_role_without_external_consent(
    fixture_project: Path,
) -> None:
    roles = RecordingRoleProvider()
    service = AgentChatService(roles, provider_label="lmstudio")
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        str(fixture_project),
        agent_mode="agent",
    )
    while state.pending is not None:
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert state.agent_mode == "agent"
    assert state.external_data_consent is False
    assert [request.role for request in roles.requests] == [
        RoleName.CAUSE_ANALYSIS
    ]


def test_single_agent_skips_external_consent_without_sending_data(
    fixture_project: Path,
) -> None:
    roles = MockRoleProvider()
    service = AgentChatService(roles, provider_label="strands")
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        str(fixture_project),
        agent_mode="agent",
    )
    state = service.approve(state.session_id, state.pending.action_id)
    state = service.approve(state.session_id, state.pending.action_id)

    assert state.pending.kind == "analysis"
    state = service.skip(state.session_id, state.pending.action_id)

    assert state.state == "completa"
    assert state.external_data_consent is False
    assert state.docs
    assert state.patches


def test_after_completion_there_is_nothing_to_approve(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(8):
        state = service.approve(state.session_id, state.pending.action_id)

    with pytest.raises(StaleActionError, match="no tiene acciones"):
        service.approve(state.session_id, "act-001")


def test_unknown_session_is_explicit(service: AgentChatService) -> None:
    with pytest.raises(UnknownSessionError, match="chat-9999"):
        service.send_message("chat-9999", "C:\\")


def test_invalid_path_is_rejected_in_the_conversation(
    service: AgentChatService, tmp_path: Path
) -> None:
    state = service.create_session()
    missing = str(tmp_path / "no-existe")

    state = service.send_message(state.session_id, missing)

    assert state.state == "esperando_ruta"
    assert state.pending is None
    assert "no es una carpeta existente" in state.messages[-1].text
    assert state.messages[-2].author == "user"


def test_user_context_can_precede_the_project_path(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    state = service.send_message(
        state.session_id,
        "Es una API Python interna; me preocupa la validación de entradas.",
    )
    assert state.user_context.startswith("Es una API Python")
    assert state.pending is None
    assert "no verificado" in state.messages[-1].text

    state = service.send_message(state.session_id, str(fixture_project))
    assert state.pending.kind == "list"
    assert state.scope_root == fixture_project


@pytest.mark.parametrize(
    "greeting",
    ["hola", "¿Cómo estás?", "¡Buenos días!", "Hola, ¿qué puedes hacer?"],
)
def test_greeting_gets_product_guidance_without_becoming_audit_context(
    service: AgentChatService,
    greeting: str,
) -> None:
    state = service.create_session()

    state = service.send_message(state.session_id, greeting)

    assert state.state == "esperando_ruta"
    assert state.pending is None
    assert state.user_context == ""
    assert state.messages[-2].author == "user"
    assert "¡Hola!" in state.messages[-1].text
    assert "comparte la ruta de la carpeta" in state.messages[-1].text


def test_lmstudio_converses_on_greeting_and_records_model_identity() -> None:
    class LocalChat:
        def __init__(self) -> None:
            self.turns = ()

        def respond(self, turns, *, timeout_seconds):
            self.turns = turns
            assert timeout_seconds > 0
            return ChatResponse(
                content="¡Hola! Soy AudiAI y puedo ayudarte a revisar un sistema.",
                provider="lmstudio",
                model="qwen/qwen3-8b",
            )

    responder = LocalChat()
    service = AgentChatService(
        MockRoleProvider(),
        provider_label="lmstudio",
        chat_responder=responder,
    )
    state = service.create_session()

    state = service.send_message(state.session_id, "Hola, ¿qué puedes hacer?")

    assert state.user_context == ""
    assert state.messages[-1].text.startswith("¡Hola! Soy AudiAI")
    assert state.chat_provider == "lmstudio"
    assert state.chat_model == "qwen/qwen3-8b"
    assert responder.turns[0].role == "user"
    assert responder.turns[-1].content == "Hola, ¿qué puedes hacer?"


def test_streaming_chat_delivers_model_deltas_and_keeps_final_turn() -> None:
    class LocalStreamingChat:
        def respond(self, turns, *, timeout_seconds):
            raise AssertionError("el chat debe usar el flujo incremental")

        def stream_respond(self, turns, *, timeout_seconds, on_delta):
            assert timeout_seconds > 0
            assert turns[-1].content == "Hola"
            on_delta("¡Hola! ")
            on_delta("Soy AudiAI.")
            return ChatResponse(
                content="¡Hola! Soy AudiAI.",
                provider="lmstudio",
                model="qwen/qwen3-8b",
            )

    service = AgentChatService(
        MockRoleProvider(),
        provider_label="lmstudio",
        chat_responder=LocalStreamingChat(),
    )
    state = service.create_session()
    deltas: list[str] = []

    state = service.send_message(
        state.session_id,
        "Hola",
        on_chat_delta=deltas.append,
    )

    assert deltas == ["¡Hola! ", "Soy AudiAI."]
    assert state.messages[-2].text == "Hola"
    assert state.messages[-1].text == "¡Hola! Soy AudiAI."
    assert state.chat_model == "qwen/qwen3-8b"


def test_empty_message_is_rejected(
    service: AgentChatService,
) -> None:
    state = service.create_session()
    with pytest.raises(AgentChatError, match="vacío"):
        service.send_message(state.session_id, "   ")


def test_analysis_failure_is_declared_never_simulated(
    fixture_project: Path,
) -> None:
    failing = MockRoleProvider(
        {
            RoleName.CAUSE_ANALYSIS: MockScript(
                outcome=RoleOutcome.FAILED,
                error="la credencial del proveedor fue rechazada",
            )
        }
    )
    service = AgentChatService(failing, provider_label="strands")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(3):
        state = service.approve(state.session_id, state.pending.action_id)

    assert state.pending.kind == "analysis"
    state = service.approve(state.session_id, state.pending.action_id)

    assert state.analysis_origin == "failed"
    assert any("rechazada" in message.text for message in state.messages)
    # a pesar del fallo, la plantilla local sigue disponible: es determinista
    assert state.pending.kind == "docs"


def test_patches_template_never_invents_a_diff(
    service: AgentChatService, fixture_project: Path
) -> None:
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(7):
        state = service.approve(state.session_id, state.pending.action_id)

    for patch in state.patches:
        assert patch.origin == "plantilla local (sin modelo)"
        assert patch.description.strip()
        assert patch.finding_id.startswith("fnd-ch")


def test_patch_role_is_context_only_and_validates_cited_evidence(
    fixture_project: Path,
) -> None:
    class RecordingRoles:
        def __init__(self) -> None:
            self.requests: list[RoleRequest] = []
            self.mock = MockRoleProvider()

        def run(self, request: RoleRequest) -> RoleResult:
            self.requests.append(request)
            if request.role is not RoleName.PATCH_SUGGESTION:
                return self.mock.run(request)
            evidence = next(
                item for item in request.context if "eval(" in item.snippet
            )
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.COMPLETED,
                summary="propuesta de parche",
                origin=ResultOrigin.MOCK,
                proposals=(
                    FindingProposal(
                        title="Sustituir evaluación dinámica",
                        explanation="Usar un parser seguro y validar el dato.",
                        severity=Severity.MEDIUM,
                        evidence_ids=(evidence.evidence_id,),
                    ),
                ),
            )

    roles = RecordingRoles()
    service = AgentChatService(roles, provider_label="mock")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(7):
        state = service.approve(state.session_id, state.pending.action_id)

    patch_request = next(
        request
        for request in roles.requests
        if request.role is RoleName.PATCH_SUGGESTION
    )
    source_request = next(
        request
        for request in roles.requests
        if request.role is RoleName.WEB_RESEARCH
    )
    assert patch_request.limits.allow_network is False
    assert any("src/app.py" in item.snippet for item in patch_request.context)
    assert source_request.limits.allow_network is False
    assert any("src/app.py" in item.snippet for item in source_request.context)
    assert len(state.patches) == 1
    assert state.patches[0].title == "Sustituir evaluación dinámica"
    assert state.patches[0].evidence_ids
    assert state.patches[0].diff is None


def test_documentation_role_output_requires_session_evidence(
    fixture_project: Path,
) -> None:
    class DocumentationRoles:
        def __init__(self) -> None:
            self.requests: list[RoleRequest] = []
            self.mock = MockRoleProvider()

        def run(self, request: RoleRequest) -> RoleResult:
            self.requests.append(request)
            if request.role is not RoleName.DOCUMENTATION:
                return self.mock.run(request)
            valid = next(
                item for item in request.context if "eval(" in item.snippet
            )
            return RoleResult(
                role=request.role,
                outcome=RoleOutcome.COMPLETED,
                summary="Informe derivado de la evidencia revisada.",
                origin=ResultOrigin.REAL,
                provider="test-provider",
                model="doc-model",
                proposals=(
                    FindingProposal(
                        title="Observación documentada",
                        explanation="El fragmento contiene ejecución dinámica.",
                        severity=Severity.MEDIUM,
                        evidence_ids=(valid.evidence_id,),
                    ),
                    FindingProposal(
                        title="Cita inexistente",
                        explanation="No debe aparecer en el informe.",
                        severity=Severity.HIGH,
                        evidence_ids=("ev-ajena",),
                    ),
                ),
            )

    roles = DocumentationRoles()
    service = AgentChatService(roles, provider_label="strands")
    state = service.create_session()
    state = service.send_message(state.session_id, str(fixture_project))
    for _ in range(5):
        state = service.approve(state.session_id, state.pending.action_id)
    assert state.pending.kind == "docs"

    state = service.approve(state.session_id, state.pending.action_id)

    documentation_request = next(
        request
        for request in roles.requests
        if request.role is RoleName.DOCUMENTATION
    )
    model_docs = [
        section
        for section in state.docs
        if section.origin == "origen=real (test-provider/doc-model)"
    ]
    assert documentation_request.limits.allow_network is False
    assert len(model_docs) == 2
    assert all(section.evidence_ids for section in model_docs)
    assert all(section.title != "Cita inexistente" for section in state.docs)

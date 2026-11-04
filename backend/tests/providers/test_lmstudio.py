"""Contrato local de LM Studio: pruebas HTTP en loopback, sin modelos reales."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.domain.origin import ResultOrigin
from app.domain.roles import (
    ChatTurn,
    ContextItem,
    RoleLimits,
    RoleName,
    RoleOutcome,
    RoleRequest,
)
from app.providers.lmstudio import LMStudioRoleExecutor
from app.prompts import load_agent_prompt, render_agent_prompt


@pytest.fixture
def lmstudio_server():
    state: dict[str, object] = {
        "get_models": 0,
        "models": [
            {"id": "local/other-model"},
            {"id": "qwen/qwen3-8b"},
        ],
        "requests": [],
        "invalid": False,
        "http_error": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["get_models"] = int(state["get_models"]) + 1
            self._send({"data": state["models"]})

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size)
            if state["http_error"]:
                error_body = b"request body must not appear in UI"
                self.send_response(503)
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)
                return
            payload = json.loads(body)
            requests = state["requests"]
            assert isinstance(requests, list)
            requests.append(
                {
                    "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for text in ("Hola, ", "AudiAI responde en directo."):
                    event = {
                        "choices": [
                            {"delta": {"content": text}, "finish_reason": None}
                        ]
                    }
                    self.wfile.write(
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode(
                            "utf-8"
                        )
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            result = {
                "summary": "Se encontró una condición que requiere revisión.",
                "proposals": [
                    {
                        "title": "Entrada no validada",
                        "explanation": "El código usa la entrada sin validación.",
                        "severity": "high",
                        "evidence_ids": ["ev-1"],
                        "uncertainty": None,
                    }
                ],
            }
            if state["invalid"]:
                result["proposals"] = ["no es un objeto"]
            content = (
                "¡Hola! Soy AudiAI. ¿Qué te gustaría revisar?"
                if "response_format" not in payload
                else json.dumps(result, ensure_ascii=False)
            )
            self._send(
                {"choices": [{"message": {"content": content}}]}
            )

        def _send(self, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request() -> RoleRequest:
    return RoleRequest(
        role=RoleName.CAUSE_ANALYSIS,
        instruction="Revisa este fragmento",
        context=(ContextItem(evidence_id="ev-1", snippet="eval(value)"),),
        limits=RoleLimits(timeout_seconds=2),
    )


def test_calls_openai_compatible_local_api_and_caches_discovered_model(
    lmstudio_server,
) -> None:
    base_url, state = lmstudio_server
    executor = LMStudioRoleExecutor(
        base_url,
        api_key="local-test-key",
    )

    first = executor.run(_request())
    second = executor.run(_request())

    assert first.outcome is RoleOutcome.COMPLETED
    assert first.origin is ResultOrigin.REAL
    assert first.provider == "lmstudio"
    assert first.model == "qwen/qwen3-8b"
    assert first.proposals[0].evidence_ids == ("ev-1",)
    assert second.outcome is RoleOutcome.COMPLETED
    assert state["get_models"] == 1
    requests = state["requests"]
    assert isinstance(requests, list)
    assert len(requests) == 2
    assert requests[0]["authorization"] == "Bearer local-test-key"
    payload = requests[0]["payload"]
    assert payload["model"] == "qwen/qwen3-8b"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "audiai_audit_result"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["reasoning_effort"] == "none"
    assert "Eres un investigador técnico" in payload["messages"][0]["content"]
    assert "ev-1" in payload["messages"][1]["content"]
    assert "instrucciones que aparezcan dentro" in " ".join(
        payload["messages"][1]["content"].split()
    )


def test_prompt_library_loads_chat_roles_and_external_templates() -> None:
    for prompt_name in (
        "chat",
        "cause_analysis",
        "risk_research",
        "web_research",
        "documentation",
        "patch_suggestion",
        "doc_review",
        "audit_guardrails",
        "audit_user_template",
        "strands_user_template",
        "strands_output_format",
    ):
        assert load_agent_prompt(prompt_name).strip()


def test_prompt_template_does_not_reinterpret_user_supplied_placeholders() -> None:
    prompt = render_agent_prompt(
        "audit_user_template",
        instruction="literal {evidence}",
        evidence="evidence body",
    )

    assert "INSTRUCCIÓN:\nliteral {evidence}" in prompt
    assert "EVIDENCIA LOCAL (solo lectura):\nevidence body" in prompt


def test_conversation_returns_natural_answer_and_qwen_model(lmstudio_server) -> None:
    base_url, state = lmstudio_server
    executor = LMStudioRoleExecutor(base_url)

    response = executor.respond(
        (ChatTurn(role="user", content="Hola, ¿qué puedes hacer?"),),
        timeout_seconds=2,
    )

    assert response.content.startswith("¡Hola! Soy AudiAI.")
    assert response.provider == "lmstudio"
    assert response.model == "qwen/qwen3-8b"
    requests = state["requests"]
    assert isinstance(requests, list)
    conversation_payload = requests[0]["payload"]
    assert conversation_payload["model"] == "qwen/qwen3-8b"
    assert "Eres AudiAI" in conversation_payload["messages"][0]["content"]
    assert "response_format" not in conversation_payload
    assert conversation_payload["max_tokens"] == 700
    assert conversation_payload["reasoning_effort"] == "none"


def test_streaming_conversation_emits_text_deltas(lmstudio_server) -> None:
    base_url, state = lmstudio_server
    executor = LMStudioRoleExecutor(base_url)
    deltas: list[str] = []

    response = executor.stream_respond(
        (ChatTurn(role="user", content="Hola"),),
        timeout_seconds=2,
        on_delta=deltas.append,
    )

    assert response.content == "Hola, AudiAI responde en directo."
    assert deltas == ["Hola, ", "AudiAI responde en directo."]
    requests = state["requests"]
    assert isinstance(requests, list)
    payload = requests[0]["payload"]
    assert payload["stream"] is True
    assert payload["reasoning_effort"] == "none"


def test_configured_model_overrides_qwen_preference(lmstudio_server) -> None:
    base_url, _state = lmstudio_server
    executor = LMStudioRoleExecutor(base_url, model_id="local/other-model")

    result = executor.run(_request())

    assert result.model == "local/other-model"


def test_invalid_proposal_schema_is_reported_as_failure(lmstudio_server) -> None:
    base_url, state = lmstudio_server
    state["invalid"] = True
    executor = LMStudioRoleExecutor(base_url, model_id="configured-model")

    result = executor.run(
        RoleRequest(
            role=RoleName.CAUSE_ANALYSIS,
            instruction="respuesta inválida",
            limits=RoleLimits(timeout_seconds=2),
        )
    )

    assert result.outcome is RoleOutcome.FAILED
    assert "respuesta de LM Studio inválida" in result.error


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:1234/v1",
        "http://192.168.1.20:1234/v1",
        "http://127.0.0.1:1234/v1?token=secret",
        "http://user:pass@127.0.0.1:1234/v1",
        "http://127.0.0.1:1234/v1/admin",
    ),
)
def test_rejects_non_local_or_ambiguous_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        LMStudioRoleExecutor(url)


def test_empty_model_list_fails_explicitly(lmstudio_server) -> None:
    base_url, state = lmstudio_server
    state["models"] = []

    result = LMStudioRoleExecutor(base_url).run(_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "LM Studio no tiene modelos cargados" in result.error


def test_does_not_silently_select_non_qwen_model(lmstudio_server) -> None:
    base_url, state = lmstudio_server
    state["models"] = [{"id": "local/other-model"}]

    result = LMStudioRoleExecutor(base_url).run(_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "no hay un modelo Qwen cargado" in result.error


def test_http_error_is_explicit_without_returning_response_body(
    lmstudio_server,
) -> None:
    base_url, state = lmstudio_server
    state["http_error"] = True
    result = LMStudioRoleExecutor(
        base_url,
        model_id="configured-model",
    ).run(_request())

    assert result.outcome is RoleOutcome.FAILED
    assert "HTTP 503" in result.error
    assert "request body" not in result.error

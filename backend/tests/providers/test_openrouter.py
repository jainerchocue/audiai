"""Pruebas del adaptador OpenRouter sin llamadas de red ni costos."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

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
from app.providers.openrouter import OpenRouterRoleExecutor


def _request() -> RoleRequest:
    return RoleRequest(
        role=RoleName.RISK_RESEARCH,
        instruction="Revisa este fragmento",
        context=(ContextItem(evidence_id="ev-1", snippet="eval(value)"),),
        limits=RoleLimits(timeout_seconds=2),
    )


def test_sends_authorized_context_with_bearer_key_and_parses_result(monkeypatch) -> None:
    result_payload = {
        "summary": "La entrada requiere validación.",
        "proposals": [
            {
                "title": "Ejecución dinámica",
                "explanation": "El valor llega a eval.",
                "severity": "high",
                "evidence_ids": ["ev-1"],
                "uncertainty": None,
            }
        ],
    }
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(result_payload, ensure_ascii=False)
                }
            }
        ]
    }
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int) -> bytes:
            return json.dumps(response).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["referer"] = req.get_header("Http-referer")
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data)
        return FakeResponse()

    monkeypatch.setattr("app.providers.openrouter.urlopen", fake_urlopen)
    executor = OpenRouterRoleExecutor(
        api_key="test-key",
        model_id="provider/test-model",
    )

    result = executor.run(_request())

    assert result.outcome is RoleOutcome.COMPLETED
    assert result.origin is ResultOrigin.REAL
    assert result.provider == "openrouter"
    assert result.model == "provider/test-model"
    assert result.proposals[0].evidence_ids == ("ev-1",)
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["referer"] == "http://localhost:8000"
    payload = captured["payload"]
    assert payload["model"] == "provider/test-model"
    assert payload["max_tokens"] == 1200
    assert payload["response_format"]["type"] == "json_schema"
    assert "ev-1" in payload["messages"][1]["content"]
    assert "reasoning_effort" not in payload


def test_chat_response_uses_configured_model_without_audit_files(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "Todo bien, ¿en qué te ayudo?"}}]}
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data)
        captured["authorization"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.providers.openrouter.urlopen", fake_urlopen)
    executor = OpenRouterRoleExecutor(
        api_key="test-key",
        model_id="provider/test-model",
    )

    response = executor.respond(
        (
            ChatTurn(role="user", content="hola"),
            ChatTurn(role="assistant", content="Hola, ¿qué necesitas?"),
            ChatTurn(role="user", content="¿y cómo estás?"),
        ),
        timeout_seconds=5,
    )

    assert response.content == "Todo bien, ¿en qué te ayudo?"
    assert response.provider == "openrouter"
    assert response.model == "provider/test-model"
    payload = captured["payload"]
    assert payload["model"] == "provider/test-model"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 700
    assert "response_format" not in payload
    assert [m["role"] for m in payload["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert payload["messages"][-1]["content"] == "¿y cómo estás?"
    assert captured["authorization"] == "Bearer test-key"


def test_chat_errors_are_reported_without_secrets(monkeypatch) -> None:
    def fake_urlopen(_req, timeout):
        raise HTTPError(
            "https://openrouter.ai",
            402,
            "payment required",
            {},
            BytesIO(b"private provider response"),
        )

    monkeypatch.setattr("app.providers.openrouter.urlopen", fake_urlopen)
    executor = OpenRouterRoleExecutor(
        api_key="test-key",
        model_id="provider/test-model",
    )

    with pytest.raises(RuntimeError) as error:
        executor.respond(
            (ChatTurn(role="user", content="hola"),),
            timeout_seconds=5,
        )

    assert "HTTP 402" in str(error.value)
    assert "private provider response" not in str(error.value)
    assert "test-key" not in str(error.value)


def test_http_errors_do_not_expose_provider_response_body(monkeypatch) -> None:
    def fake_urlopen(_req, timeout):
        raise HTTPError(
            "https://openrouter.ai",
            401,
            "unauthorized",
            {},
            BytesIO(b"private provider response"),
        )

    monkeypatch.setattr("app.providers.openrouter.urlopen", fake_urlopen)
    result = OpenRouterRoleExecutor(
        api_key="test-key",
        model_id="provider/test-model",
    ).run(_request())

    assert result.role is RoleName.RISK_RESEARCH
    assert result.outcome is RoleOutcome.FAILED
    assert "HTTP 401" in result.error
    assert "private provider response" not in result.error
    assert "test-key" not in result.error


@pytest.mark.parametrize(
    ("api_key", "model_id", "missing"),
    (
        ("", "provider/model", "OPENROUTER_API_KEY"),
        ("key", "", "OPENROUTER_MODEL"),
    ),
)
def test_provider_requires_credentials_and_model(
    api_key: str,
    model_id: str,
    missing: str,
) -> None:
    with pytest.raises(ValueError, match=missing):
        OpenRouterRoleExecutor(api_key=api_key, model_id=model_id)

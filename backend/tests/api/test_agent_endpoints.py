"""Fase 11: integración HTTP del chat (create_app real, cliente en proceso)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def process(value):\n    return eval(value)\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_full_chat_flow_over_http(
    client: TestClient, fixture_project: Path
) -> None:
    created = client.post("/agent/sessions")
    assert created.status_code == 201
    state = created.json()
    session_id = state["session_id"]
    assert state["state"] == "esperando_ruta"
    unavailable_report = client.get(
        f"/agent/sessions/{session_id}/report.md"
    )
    assert unavailable_report.status_code == 409
    assert state["roles_provider"] == "mock"
    assert state["agent_mode"] == "multiagent"
    assert state["external_data_consent"] is False
    assert state["sensitive_read_approved"] is False

    response = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"text": str(fixture_project), "analysis_mode": "research"},
    )
    assert response.status_code == 200
    state = response.json()
    assert state["pending_action"]["kind"] == "list"
    session = client.app.state.agent_chat.get_session(session_id)
    assert session.role_context().endswith(
        "Enfoque solicitado: investiga el problema relacionando hipótesis "
        "con evidencia verificable. No realices búsquedas externas."
    )

    for expected in (
        "list",
        "checks",
        "security",
        "analysis",
        "sources",
        "docs",
        "patches",
        "review",
    ):
        approve = client.post(
            f"/agent/sessions/{session_id}/approve/{state['pending_action']['action_id']}"
        )
        assert approve.status_code == 200
        state = approve.json()
        if expected == "checks":
            assert state["findings_count"] >= 1
        if state["state"] == "completa":
            break

    assert state["state"] == "completa"
    report = client.get(f"/agent/sessions/{session_id}/report.md")
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    assert report.headers["content-disposition"].endswith(
        f'filename="AudiAI-{session_id}-README.md"'
    )
    assert "# Informe de auditoría AudiAI" in report.text
    assert "- **Modo:** Multiagente" in report.text
    assert "## Hallazgos" in report.text
    assert "## Evidencias" in report.text
    assert "~~~~diff" not in report.text


def test_chat_stream_endpoint_emits_deltas_and_final_session_state(
    client: TestClient,
) -> None:
    state = client.post("/agent/sessions").json()

    response = client.post(
        f"/agent/sessions/{state['session_id']}/messages/stream",
        json={"text": "hola"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: delta\ndata: {"text": "¡Hola!' in response.text
    assert "event: state" in response.text
    assert '"session_id": "chat-' in response.text


def test_selected_project_context_is_sent_with_the_prompt_not_as_a_chat_message(
    client: TestClient, fixture_project: Path
) -> None:
    created = client.post("/agent/sessions").json()
    session_id = created["session_id"]
    prompt = "Investiga por qué falla la validación de entradas."

    response = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"text": prompt, "project_path": str(fixture_project)},
    )

    assert response.status_code == 200
    state = response.json()
    assert state["pending_action"]["kind"] == "list"
    assert [message["text"] for message in state["messages"] if message["author"] == "user"] == [
        prompt
    ]
    session = client.app.state.agent_chat.get_session(session_id)
    assert session.scope_root == fixture_project
    assert session.role_context().startswith(prompt)


def test_unavailable_attached_project_does_not_start_audit(
    client: TestClient, tmp_path: Path
) -> None:
    created = client.post("/agent/sessions").json()
    missing_folder = str(tmp_path / "folder-removed-after-selection")

    response = client.post(
        f"/agent/sessions/{created['session_id']}/messages",
        json={"text": "Revisa este proyecto.", "project_path": missing_folder},
    )

    assert response.status_code == 200
    state = response.json()
    assert state["state"] == "esperando_ruta"
    assert state["pending_action"] is None
    assert missing_folder not in [message["text"] for message in state["messages"]]
    assert "ya no está disponible" in state["messages"][-1]["text"]


def test_agent_mode_runs_one_model_role_through_http(
    client: TestClient, fixture_project: Path
) -> None:
    state = client.post("/agent/sessions").json()
    session_id = state["session_id"]
    response = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"text": str(fixture_project), "agent_mode": "agent"},
    )
    assert response.status_code == 200
    state = response.json()
    assert state["agent_mode"] == "agent"

    while state["pending_action"] is not None:
        action_id = state["pending_action"]["action_id"]
        response = client.post(
            f"/agent/sessions/{session_id}/approve/{action_id}"
        )
        assert response.status_code == 200
        state = response.json()

    assert state["state"] == "completa"
    assert any(item["kind"] == "analysis" for item in state["ready_items"])
    assert any(item["kind"] == "docs" for item in state["ready_items"])
    assert any(item["kind"] == "patches" for item in state["ready_items"])
    assert not any(
        item["kind"] in {"security", "sources", "review"}
        for item in state["ready_items"]
    )

    listed = client.get("/agent/sessions").json()
    assert listed[0]["session_id"] == session_id
    assert listed[0]["patches_count"] == listed[0]["findings_count"]

    final = client.get(f"/agent/sessions/{session_id}").json()
    assert final["session_id"] == session_id
    assert final["evidence_count"] > 0
    assert final["external_data_consent"] is False
    assert len(final["evidence"]) == final["evidence_count"]
    assert all(
        {"evidence_id", "source_path", "tool", "snippet"} <= evidence.keys()
        for evidence in final["evidence"]
    )


def test_sensitive_files_can_be_skipped_without_exposing_their_contents(
    client: TestClient, fixture_project: Path
) -> None:
    (fixture_project / ".env").write_text(
        "API_TOKEN=private-value\n", encoding="utf-8"
    )
    state = client.post("/agent/sessions").json()
    session_id = state["session_id"]
    state = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"text": str(fixture_project)},
    ).json()
    state = client.post(
        f"/agent/sessions/{session_id}/approve/{state['pending_action']['action_id']}"
    ).json()

    assert state["pending_action"]["kind"] == "sensitive"
    assert "private-value" not in str(state)

    skipped = client.post(
        f"/agent/sessions/{session_id}/skip/{state['pending_action']['action_id']}"
    )

    assert skipped.status_code == 200
    assert skipped.json()["pending_action"]["kind"] == "checks"
    assert all(item["source_path"] != ".env" for item in skipped.json()["evidence"])


def test_unknown_session_returns_404(client: TestClient) -> None:
    assert client.get("/agent/sessions/chat-9999").status_code == 404
    response = client.post(
        "/agent/sessions/chat-9999/messages", json={"text": "x"}
    )
    assert response.status_code == 404


def test_stale_action_returns_409(
    client: TestClient, fixture_project: Path
) -> None:
    state = client.post("/agent/sessions").json()
    session_id = state["session_id"]
    client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"text": str(fixture_project)},
    )

    response = client.post(
        f"/agent/sessions/{session_id}/approve/act-099"
    )
    assert response.status_code == 409


def test_empty_message_returns_422(client: TestClient) -> None:
    state = client.post("/agent/sessions").json()
    response = client.post(
        f"/agent/sessions/{state['session_id']}/messages", json={"text": "  "}
    )
    assert response.status_code == 422


def test_classic_interface_is_served(client: TestClient) -> None:
    index = client.get("/")
    classic = client.get("/classic")
    assert index.status_code == 200
    assert classic.status_code == 200
    assert "AudiAI" in index.text
    assert "text/html" in classic.headers["content-type"]


def test_lmstudio_provider_is_reported_without_connecting_to_it(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUDIAI_ROLE_PROVIDER", "lmstudio")
    monkeypatch.setenv(
        "AUDIAI_LMSTUDIO_BASE_URL",
        "http://127.0.0.1:1234/v1",
    )
    client = TestClient(create_app())

    state = client.post("/agent/sessions").json()

    assert state["roles_provider"] == "lmstudio"
    assert state["external_data_consent"] is False
    assert state["chat_model"] is None

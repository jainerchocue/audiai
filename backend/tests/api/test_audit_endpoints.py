"""Fase 7: integración del caso de uso (crear → ejecutar → leer → informar).

Sin código del proyecto auditado en ejecución; las peticiones contra el
 servicio no dependen de red real (TestClient in-proceso).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.audits import DuplicateAuditError


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def process(value):\n    return eval(value)\n", encoding="utf-8"
    )
    (root / "src" / "settings.py").write_text(
        'API_PASSWORD = "clave-larga-12345"\n', encoding="utf-8"
    )
    (root / "clean.py").write_text("value = int(value)\n", encoding="utf-8")
    return root


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


# -- recorrido ------------------------------------------------------------


def test_created_audit_exposes_state_and_report(
    client: TestClient, fixture_project: Path
) -> None:
    payload = {"audit_id": "demo-x", "scope_root": str(fixture_project)}
    response = client.post("/audits", json=payload)
    assert response.status_code == 201
    created = response.json()
    assert created["audit"]["audit_id"] == "demo-x"
    assert created["audit"]["status"] == "completed"
    assert created["audit"]["findings_count"] > 0
    assert created["audit"]["evidence_count"] > 0

    listed = client.get("/audits").json()
    assert listed == [created["audit"]]

    report = client.get("/audits/demo-x/report").json()
    assert report["audit"]["audit_id"] == "demo-x"
    assert report["findings"]
    assert report["evidence"]
    assert report["trace"]
    for finding in report["findings"]:
        in_evidence = {
            evidence["evidence_id"] for evidence in report["evidence"]
        }
        assert set(finding["evidence_ids"]) <= in_evidence


def test_unknown_audit_returns_404(client: TestClient) -> None:
    response = client.get("/audits/no-existe")
    assert response.status_code == 404
    response = client.get("/audits/no-existe/report")
    assert response.status_code == 404


def test_invalid_root_is_rejected_before_creating(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/audits", json={"scope_root": str(tmp_path / "no-existe")}
    )
    assert response.status_code == 422


def test_duplicate_audit_id_conflicts(
    client: TestClient, fixture_project: Path
) -> None:
    payload = {"audit_id": "demo-dup", "scope_root": str(fixture_project)}
    first = client.post("/audits", json=payload)
    assert first.status_code == 201

    second = client.post("/audits", json=payload)
    assert second.status_code == 409


def test_auto_generated_id_is_deterministic_and_free(
    client: TestClient, fixture_project: Path
) -> None:
    """Sin audit_id el servidor elige un id libre y determinista."""

    first = client.post("/audits", json={"scope_root": str(fixture_project)})
    assert first.status_code == 201
    assert first.json()["audit"]["audit_id"] == "audit-demo-001"

    second = client.post("/audits", json={"scope_root": str(fixture_project)})
    assert second.status_code == 201
    assert second.json()["audit"]["audit_id"] == "audit-demo-002"


def test_duplicate_error_is_explicit_with_id() -> None:
    error = DuplicateAuditError("ya existe una auditoría con id demo-a")
    assert "demo-a" in str(error)

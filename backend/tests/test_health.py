from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_leak_environment_details() -> None:
    client = TestClient(create_app())

    payload = client.get("/health").json()

    assert set(payload.keys()) == {"status"}

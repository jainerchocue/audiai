"""Configuración del proceso: valores por defecto y efecto en la app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import build_roles, create_app
from app.providers.lmstudio import LMStudioRoleExecutor
from app.providers.openrouter import OpenRouterRoleExecutor


def test_defaults_are_conservative(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.load_dotenv", lambda **_kwargs: None)
    for variable in (
        "AUDIAI_ENV",
        "AUDIAI_HOST",
        "AUDIAI_PORT",
        "AUDIAI_LOG_LEVEL",
        "AUDIAI_ROLE_PROVIDER",
        "AUDIAI_LMSTUDIO_BASE_URL",
        "AUDIAI_LMSTUDIO_MODEL",
        "AUDIAI_LMSTUDIO_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings.from_env()

    assert settings.environment == "development"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.log_level == "info"
    assert settings.role_provider == "mock"
    assert settings.lmstudio_base_url == "http://127.0.0.1:1234/v1"
    assert settings.is_development is True


def test_lmstudio_settings_are_loaded_without_exposing_api_key(monkeypatch) -> None:
    monkeypatch.setenv("AUDIAI_ROLE_PROVIDER", "lmstudio")
    monkeypatch.setenv(
        "AUDIAI_LMSTUDIO_BASE_URL",
        "http://127.0.0.1:9876/v1",
    )
    monkeypatch.setenv("AUDIAI_LMSTUDIO_MODEL", "qwen-local")
    monkeypatch.setenv("AUDIAI_LMSTUDIO_API_KEY", "local-secret")

    settings = Settings.from_env()
    executor = build_roles(settings)

    assert settings.role_provider == "lmstudio"
    assert isinstance(executor, LMStudioRoleExecutor)
    assert "local-secret" not in repr(settings)


def test_openrouter_settings_build_direct_provider_without_exposing_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUDIAI_ROLE_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_MODEL", "provider/test-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "remote-secret")

    settings = Settings.from_env()
    executor = build_roles(settings)

    assert settings.role_provider == "openrouter"
    assert isinstance(executor, OpenRouterRoleExecutor)
    assert "remote-secret" not in repr(settings)


def test_openrouter_provider_requires_key_and_model(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.load_dotenv", lambda **_kwargs: None)
    monkeypatch.setenv("AUDIAI_ROLE_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_roles(Settings.from_env())


def test_openrouter_chat_uses_configured_model_without_exposing_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUDIAI_ROLE_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_MODEL", "provider/test-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "remote-secret")

    client = TestClient(create_app())
    created = client.post("/agent/sessions")

    assert created.status_code == 201
    body = created.json()
    assert body["roles_provider"] == "openrouter"
    assert body["configured_model"] == "provider/test-model"
    assert "remote-secret" not in created.text


def test_environment_overrides_are_read(monkeypatch) -> None:
    monkeypatch.setenv("AUDIAI_ENV", "production")
    monkeypatch.setenv("AUDIAI_HOST", "0.0.0.0")
    monkeypatch.setenv("AUDIAI_PORT", "9001")

    settings = Settings.from_env()

    assert settings.environment == "production"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9001
    assert settings.is_development is False


def test_blank_values_fall_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("AUDIAI_HOST", "   ")

    assert Settings.from_env().host == "127.0.0.1"


def test_docs_are_published_only_in_development() -> None:
    client = TestClient(create_app())

    assert client.get("/docs").status_code == 200


def test_docs_are_disabled_outside_development(monkeypatch) -> None:
    monkeypatch.setenv("AUDIAI_ENV", "production")

    client = TestClient(create_app())

    assert client.get("/docs").status_code == 404

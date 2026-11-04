"""Configuración del backend, cargada desde variables de entorno.

Contiene únicamente valores de transporte y ejecución. No lee ni registra
secretos: las credenciales de proveedores se añadirán en la fase del
proveedor real, nunca aquí por defecto.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_ENVIRONMENT_DEVELOPMENT = "development"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


@dataclass(frozen=True)
class Settings:
    """Ajustes mínimos para arrancar el servicio."""

    app_name: str = "AudiAI"
    environment: str = _ENVIRONMENT_DEVELOPMENT
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    role_provider: str = "mock"
    agent_root: str = ""
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = ""
    lmstudio_api_key: str = field(default="", repr=False)

    @property
    def is_development(self) -> bool:
        """En desarrollo se publica la documentación interactiva de la API."""

        return self.environment.lower() == _ENVIRONMENT_DEVELOPMENT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=_env_str("AUDIAI_ENV", _ENVIRONMENT_DEVELOPMENT),
            host=_env_str("AUDIAI_HOST", "127.0.0.1"),
            port=int(_env_str("AUDIAI_PORT", "8000")),
            log_level=_env_str("AUDIAI_LOG_LEVEL", "info"),
            role_provider=_env_str("AUDIAI_ROLE_PROVIDER", "mock").lower(),
            agent_root=_env_str("AUDIAI_AGENT_ROOT", ""),
            lmstudio_base_url=_env_str(
                "AUDIAI_LMSTUDIO_BASE_URL",
                "http://127.0.0.1:1234/v1",
            ),
            lmstudio_model=_env_str("AUDIAI_LMSTUDIO_MODEL", ""),
            lmstudio_api_key=_env_str("AUDIAI_LMSTUDIO_API_KEY", ""),
        )


def get_settings() -> Settings:
    """Devuelve la configuración vigente del proceso."""

    return Settings.from_env()

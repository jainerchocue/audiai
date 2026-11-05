"""Punto de entrada del backend de AudiAI.

Fases 1, 7, 8, 10 y 11: servicio HTTP con /health, endpoints de auditorías,
chat de agente guiado con consentimiento por paso, interfaz en / y la clásica
en /classic. Ejecución de roles por proveedor seleccionable (mock por
defecto; strands = subagentes reales vía otro venv). No guarda secretos ni
escribe en el proyecto auditado.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.agent import router as agent_router
from app.api.audits import router as audits_router
from app.api.frontend import router as frontend_router
from app.api.health import router as health_router
from app.config.settings import get_settings
from app.domain.roles import RoleExecutor
from app.harness.pipeline import PipelineLimits
from app.providers.lmstudio import LMStudioRoleExecutor
from app.providers.mock import MockRoleProvider
from app.providers.openrouter import OpenRouterRoleExecutor
from app.providers.strand_bridge import StrandsSubagentExecutor
from app.services.agent_chat import AgentChatService
from app.services.audits import InMemoryAuditStore, ManagedAuditRunner

# Presupuesto distinto por proveedor: una llamada a un modelo real puede
# tardar decenas de segundos; el mock ni se acerca a 30 s.
_REAL_PROVIDER_LIMITS = PipelineLimits(max_seconds=120.0)


def build_roles(settings) -> RoleExecutor:
    """Elige el ejecutor de roles según ``AUDIAI_ROLE_PROVIDER``.

    - ``mock`` (default): ejecutor determinista sin red ni costo. Es el modo
      de demo por defecto y el de todas las pruebas.
    - ``lmstudio``: modelo local vía API compatible de LM Studio en loopback.
    - ``openrouter``: llamada directa al modelo y la clave de la raíz del repo.
    - ``strands``: subagente real del proyecto de subagentes
      (``AUDIAI_AGENT_ROOT``: su .venv y sus prompts por rol). La clave de
      OpenRouter de ese modo vive en el ``.env`` del subagente.

    Configuración errónea aborta el arranque: correr mock sin querer sería
    una simulación presentada como demo real.
    """

    if settings.role_provider == "mock":
        return MockRoleProvider()
    if settings.role_provider == "lmstudio":
        return LMStudioRoleExecutor(
            settings.lmstudio_base_url,
            model_id=settings.lmstudio_model,
            api_key=settings.lmstudio_api_key,
        )
    if settings.role_provider == "openrouter":
        return OpenRouterRoleExecutor(
            api_key=settings.openrouter_api_key,
            model_id=settings.openrouter_model,
        )
    if settings.role_provider == "strands":
        if not settings.agent_root.strip():
            raise RuntimeError(
                "AUDIAI_ROLE_PROVIDER=strands exige AUDIAI_AGENT_ROOT "
                "(raíz del proyecto de subagentes)"
            )
        agent_root = Path(settings.agent_root)
        interpreter = agent_root / ".venv" / "Scripts" / "python.exe"
        return StrandsSubagentExecutor(interpreter, agent_root)
    raise RuntimeError(
        "AUDIAI_ROLE_PROVIDER inválido: use mock, lmstudio, openrouter o strands (recibí '"
        + settings.role_provider
        + "')"
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.4.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url=None,
    )
    roles = build_roles(settings)
    configured_model = (
        settings.openrouter_model
        if settings.role_provider == "openrouter"
        else settings.lmstudio_model
        if settings.role_provider == "lmstudio"
        else ""
    )
    app.state.audit_runner = ManagedAuditRunner(
        roles,
        InMemoryAuditStore(),
        limits=(
            None
            if settings.role_provider == "mock"
            else _REAL_PROVIDER_LIMITS
        ),
    )
    app.state.agent_chat = AgentChatService(
        roles,
        provider_label=settings.role_provider,
        configured_model=configured_model,
        chat_responder=(
            roles
            if settings.role_provider in {"lmstudio", "openrouter"}
            else None
        ),
    )
    app.state.roles_provider = settings.role_provider
    app.include_router(health_router)
    app.include_router(audits_router)
    app.include_router(agent_router)
    app.include_router(frontend_router)
    app.mount(
        "/assets",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="assets",
    )
    return app


app = create_app()

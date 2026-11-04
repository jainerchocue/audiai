"""Endpoints del chat del agente (Fase 11).

El consentimiento explícito vive en la conversación: una acción solo se
ejecuta con ``POST .../approve/{action_id}`` y SOLO si es la pendiente.
Errores de transporte: 404 (sesión desconocida), 409 (acción vencida o
fuera de orden), 422 (texto vacío).
"""

from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.api.agent_schemas import (
    AgentMessageIn,
    AgentSessionSummaryOut,
    AgentStateOut,
    _to_state,
)
from app.services.agent_chat import (
    AgentChatError,
    StaleActionError,
    UnknownSessionError,
)
from app.services.agent_report import build_session_readme

router = APIRouter(prefix="/agent", tags=["agent"])
_logger = logging.getLogger(__name__)


@router.post("/sessions", status_code=201, response_model=AgentStateOut)
def create_session(request: Request) -> AgentStateOut:
    """Abre una conversación; el agente pregunta la ruta a revisar."""

    service = _chat(request)
    session = service.create_session()
    return _to_state(session, _provider(request))


@router.get("/sessions", response_model=list[AgentSessionSummaryOut])
def list_sessions(request: Request) -> list[AgentSessionSummaryOut]:
    """Resumen de las sesiones de este proceso."""

    service = _chat(request)
    return [
        AgentSessionSummaryOut(
            session_id=summary.session_id,
            state=summary.state,
            findings_count=summary.findings_count,
            docs_count=summary.docs_count,
            patches_count=summary.patches_count,
        )
        for summary in service.list_sessions()
    ]


@router.get("/sessions/{session_id}", response_model=AgentStateOut)
def read_session(session_id: str, request: Request) -> AgentStateOut:
    service = _chat(request)
    try:
        session = service.get_session(session_id)
    except UnknownSessionError as error:
        raise HTTPException(404, detail=str(error)) from error
    return _to_state(session, _provider(request))


@router.get("/sessions/{session_id}/report.md")
def download_session_report(session_id: str, request: Request) -> Response:
    """Descarga el informe README de una sesión completada."""

    service = _chat(request)
    try:
        session = service.get_session(session_id)
    except UnknownSessionError as error:
        raise HTTPException(404, detail=str(error)) from error
    if session.state != "completa":
        raise HTTPException(
            409, detail="El informe estará disponible al completar la auditoría."
        )
    return Response(
        content=build_session_readme(session),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="AudiAI-{session_id}-README.md"'
        },
    )


@router.post("/sessions/{session_id}/messages", response_model=AgentStateOut)
def send_message(
    session_id: str, payload: AgentMessageIn, request: Request
) -> AgentStateOut:
    """Un turno del usuario: normalmente la ruta a revisar."""

    service = _chat(request)
    try:
        session = service.send_message(
            session_id,
            payload.text,
            project_path=payload.project_path,
            analysis_mode=payload.analysis_mode,
            agent_mode=payload.agent_mode,
        )
    except UnknownSessionError as error:
        raise HTTPException(404, detail=str(error)) from error
    except AgentChatError as error:
        raise HTTPException(422, detail=str(error)) from error
    return _to_state(session, _provider(request))


@router.post("/sessions/{session_id}/messages/stream")
def stream_message(
    session_id: str, payload: AgentMessageIn, request: Request
) -> StreamingResponse:
    """Entrega fragmentos del chat mientras conserva el estado de sesión."""

    service = _chat(request)
    events: queue.Queue[tuple[str, object] | None] = queue.Queue()

    def produce() -> None:
        try:
            session = service.send_message(
                session_id,
                payload.text,
                project_path=payload.project_path,
                analysis_mode=payload.analysis_mode,
                agent_mode=payload.agent_mode,
                on_chat_delta=lambda text: events.put(("delta", {"text": text})),
            )
            events.put(
                (
                    "state",
                    _to_state(session, _provider(request)).model_dump(mode="json"),
                )
            )
        except UnknownSessionError as error:
            events.put(("error", {"status": 404, "detail": str(error)}))
        except AgentChatError as error:
            events.put(("error", {"status": 422, "detail": str(error)}))
        except Exception:
            _logger.exception("Fallo inesperado al transmitir un turno del chat")
            events.put(
                (
                    "error",
                    {
                        "status": 500,
                        "detail": "No se pudo completar el turno del chat.",
                    },
                )
            )
        finally:
            events.put(None)

    def stream_events():
        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        while True:
            event = events.get()
            if event is None:
                break
            name, data = event
            yield (
                f"event: {name}\n"
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/sessions/{session_id}/approve/{action_id}",
    response_model=AgentStateOut,
)
def approve_action(
    session_id: str, action_id: str, request: Request
) -> AgentStateOut:
    """Aprueba la única acción pendiente; ejecuta y responde el nuevo estado."""

    service = _chat(request)
    try:
        session = service.approve(session_id, action_id)
    except UnknownSessionError as error:
        raise HTTPException(404, detail=str(error)) from error
    except StaleActionError as error:
        raise HTTPException(409, detail=str(error)) from error
    return _to_state(session, _provider(request))


@router.post(
    "/sessions/{session_id}/skip/{action_id}",
    response_model=AgentStateOut,
)
def skip_action(
    session_id: str, action_id: str, request: Request
) -> AgentStateOut:
    """Rechaza lectura sensible o consentimiento IA y continúa localmente."""

    service = _chat(request)
    try:
        session = service.skip(session_id, action_id)
    except UnknownSessionError as error:
        raise HTTPException(404, detail=str(error)) from error
    except StaleActionError as error:
        raise HTTPException(409, detail=str(error)) from error
    return _to_state(session, _provider(request))


def _chat(request: Request):
    return request.app.state.agent_chat


def _provider(request: Request) -> str:
    return request.app.state.roles_provider

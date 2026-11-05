"""Contrato de transporte del chat del agente (Fase 11).

Solo proyección del estado conversacional: el chat no introduce reglas de
dominio ni muta auditorías del motor. Los errores de transporte son coherentes:
404 (sesión desconocida), 409 (acción vencida), 422 (texto vacío).

Nota de dominio: una ruta inexistente NO es 422 cuando viaja dentro de
``POST .../messages``, porque ahí es una respuesta conversacional legítima
(el agente la rechaza en el chat con HTTP 200 y pide contexto/ruta válida).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.evidence import Evidence
from app.services.agent_chat import (
    Activity,
    ChatMessage,
    ChatSession,
    DocSection,
    PatchItem,
    PendingAction,
    ReadyItem,
)


class ChatMessageOut(BaseModel):
    author: str
    text: str

    @classmethod
    def from_model(cls, message: ChatMessage) -> "ChatMessageOut":
        return cls(author=message.author, text=message.text)


class PendingActionOut(BaseModel):
    action_id: str
    kind: str
    caption: str
    hint: str

    @classmethod
    def from_model(cls, action: PendingAction | None) -> "PendingActionOut | None":
        if action is None:
            return None
        return cls(
            action_id=action.action_id,
            kind=action.kind,
            caption=action.caption,
            hint=action.hint,
        )


class ActivityOut(BaseModel):
    activity_id: str
    turn: int = Field(ge=1)
    kind: str
    title: str
    detail: str
    status: str
    origin: str
    duration_ms: int = Field(ge=0)

    @classmethod
    def from_model(cls, activity: Activity) -> "ActivityOut":
        return cls(
            activity_id=activity.activity_id,
            turn=activity.turn,
            kind=activity.kind,
            title=activity.title,
            detail=activity.detail,
            status=activity.status,
            origin=activity.origin,
            duration_ms=activity.duration_ms,
        )


class ReadyItemOut(BaseModel):
    item_id: str
    kind: str
    title: str
    detail: str
    origin: str

    @classmethod
    def from_model(cls, item: ReadyItem) -> "ReadyItemOut":
        return cls(
            item_id=item.item_id,
            kind=item.kind,
            title=item.title,
            detail=item.detail,
            origin=item.origin,
        )


class DocSectionOut(BaseModel):
    title: str
    body: str
    origin: str
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_model(cls, section: DocSection) -> "DocSectionOut":
        return cls(
            title=section.title,
            body=section.body,
            origin=section.origin,
            evidence_ids=section.evidence_ids,
        )


class PatchItemOut(BaseModel):
    patch_id: str
    finding_id: str
    title: str
    description: str
    diff: str | None = None
    origin: str
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def from_model(cls, patch: PatchItem) -> "PatchItemOut":
        return cls(
            patch_id=patch.patch_id,
            finding_id=patch.finding_id,
            title=patch.title,
            description=patch.description,
            diff=patch.diff,
            origin=patch.origin,
            evidence_ids=patch.evidence_ids,
        )


class EvidenceOut(BaseModel):
    evidence_id: str
    source_path: str
    tool: str
    snippet: str
    line_start: int | None = None
    line_end: int | None = None
    limitations: tuple[str, ...] = ()

    @classmethod
    def from_model(cls, evidence: Evidence) -> "EvidenceOut":
        return cls(
            evidence_id=evidence.evidence_id,
            source_path=evidence.source_path,
            tool=evidence.tool,
            snippet=evidence.snippet,
            line_start=evidence.line_start,
            line_end=evidence.line_end,
            limitations=evidence.limitations,
        )


class AgentSessionSummaryOut(BaseModel):
    session_id: str
    state: str
    findings_count: int = Field(ge=0)
    docs_count: int = Field(ge=0)
    patches_count: int = Field(ge=0)


class AgentStateOut(BaseModel):
    session_id: str
    state: str
    roles_provider: str
    configured_model: str | None = None
    agent_mode: Literal["agent", "multiagent"]
    messages: list[ChatMessageOut]
    pending_action: PendingActionOut | None = None
    activities: list[ActivityOut]
    ready_items: list[ReadyItemOut]
    docs: list[DocSectionOut]
    patches: list[PatchItemOut]
    evidence: list[EvidenceOut]
    evidence_count: int = Field(ge=0)
    findings_count: int = Field(ge=0)
    external_data_consent: bool
    sensitive_read_approved: bool
    analysis_origin: str | None = None
    analysis_provider: str | None = None
    analysis_model: str | None = None
    chat_provider: str | None = None
    chat_model: str | None = None


class AgentMessageIn(BaseModel):
    text: str = Field(min_length=1)
    project_path: str | None = Field(default=None, min_length=1)
    analysis_mode: Literal["reading", "research", "unspecified"] = "reading"
    agent_mode: Literal["agent", "multiagent"] = "multiagent"


def _to_state(session: ChatSession, roles_provider: str) -> AgentStateOut:
    return AgentStateOut(
        session_id=session.session_id,
        state=session.state,
        roles_provider=roles_provider,
        configured_model=session.configured_model,
        agent_mode=session.agent_mode,
        messages=[ChatMessageOut.from_model(item) for item in session.messages],
        pending_action=PendingActionOut.from_model(session.pending),
        activities=[ActivityOut.from_model(item) for item in session.activities],
        ready_items=[ReadyItemOut.from_model(i) for i in session.ready_items],
        docs=[DocSectionOut.from_model(section) for section in session.docs],
        patches=[PatchItemOut.from_model(patch) for patch in session.patches],
        evidence=[EvidenceOut.from_model(item) for item in session.evidence],
        evidence_count=len(session.evidence),
        findings_count=len(session.findings),
        external_data_consent=session.external_data_consent,
        sensitive_read_approved=session.sensitive_read_approved,
        analysis_origin=session.analysis_origin,
        analysis_provider=session.analysis_provider,
        analysis_model=session.analysis_model,
        chat_provider=session.chat_provider,
        chat_model=session.chat_model,
    )

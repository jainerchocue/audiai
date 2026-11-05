"""Fase 11: agente conversacional de auditoría (chat guiado).

QUÉ ES
------
Un conversador LOCAL y determinista que guía la revisión paso a paso: el
usuario entrega una ruta y el agente procesa automáticamente archivos
permitidos en solo lectura. Solicita permiso únicamente para archivos
confidenciales o para enviar evidencia a un proveedor externo.

Reutiliza exactamente las piezas del motor (Fases 3-6): lector acotado,
comprobaciones deterministas, contrato de roles y conversión de propuestas a
hallazgos con validación de evidencia. No existe una segunda lógica de
auditoría: la secuencia del chat es el mismo recorrido, con puertas humanas.

DIVISIÓN HONESTA DE TRABAJO
---------------------------
- ``listar`` / ``comprobar``: herramientas deterministas (sin IA), dentro de
  la carpeta que el usuario indicó.
- ``seguridad``, ``causa``, ``fuentes``, ``documentación``, ``parches`` y
  ``revisión final``: roles acotados del ejecutor configurado. En modo
  ``mock`` no hay modelo; con ``lmstudio`` las solicitudes se quedan en el
  servidor local; con ``strands`` y ``openrouter`` se pide permiso antes de
  enviar evidencia al proveedor externo.
- Los fallos de los roles se declaran; el agente nunca simula resultados.

Los paneles "Listo para revisar" y "Parches" se llenan solo con lo que ESTAS
acciones produjeron, no con estimaciones.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from app.domain.evidence import Evidence
from app.domain.finding import Finding, FindingOrigin, MissingEvidenceError
from app.domain.severity import Severity
from app.domain.roles import (
    ChatResponder,
    ChatTurn,
    ContextItem,
    FindingProposal,
    MAX_INSTRUCTION_CHARS,
    RoleError,
    RoleExecutor,
    RoleLimits,
    RoleName,
    RoleNotConfigured,
    RoleOutcome,
    RoleRequest,
    RoleResult,
    StreamingChatResponder,
)

from app.domain.tools import (
    ToolError,
    ToolLimits,
    ToolName,
    ToolOutcome,
    ToolRequest,
    is_sensitive_path,
)
from app.harness.pipeline import ANALYSIS_INSTRUCTION
from app.tools.checks import BUNDLED_CHECKS, CheckRun, DeterministicChecks
from app.tools.deterministic import LocalReadOnlyTool

_MAX_CONTEXT_BYTES = 32_000
_ROLE_TIMEOUT_SECONDS = 90.0
_CHAT_TIMEOUT_SECONDS = 45.0
_MAX_USER_CONTEXT_CHARS = 3_000
_MAX_INITIAL_SOURCE_FILES = 40
_MAX_TOTAL_SOURCE_BYTES = 512_000
_MAX_SOURCE_FILE_BYTES = 16_000
_ANALYSIS_MODE_INSTRUCTIONS = {
    "reading": "Enfoque solicitado: prioriza describir el código y los hechos observables.",
    "research": (
        "Enfoque solicitado: investiga el problema relacionando hipótesis "
        "con evidencia verificable. No realices búsquedas externas."
    ),
    "unspecified": "",
}
_AGENT_MODES = frozenset({"agent", "multiagent"})
_SOURCE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".html",
        ".java", ".js", ".json", ".jsx", ".md", ".php", ".ps1", ".py",
        ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml",
        ".yaml", ".yml",
    }
)
_SOURCE_FILE_NAMES = frozenset(
    {
        "dockerfile", "gemfile", "go.mod", "makefile", "package.json",
        "pyproject.toml", "requirements.txt", "tsconfig.json",
    }
)

_TEMPLATE_ORIGIN = "plantilla local (sin modelo)"

_ACTION_TITLES = {
    "list": "Lectura del proyecto",
    "sensitive": "Archivos confidenciales",
    "checks": "Comprobaciones deterministas",
    "security": "Evaluación de seguridad",
    "analysis": "Investigación de causa",
    "sources": "Revisión de fuentes",
    "docs": "Redacción del informe",
    "patches": "Recomendaciones de parche",
    "review": "Revisión final de calidad",
}


class AgentChatError(RuntimeError):
    """Base de los errores gestionados del chat."""


class UnknownSessionError(AgentChatError):
    """Nadie registró una sesión con ese identificador."""


class StaleActionError(AgentChatError):
    """La acción a aprobar no es la única pendiente de esta sesión."""


# -- datos de la sesión -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatMessage:
    author: str  # "agent" | "user"
    text: str


@dataclass(frozen=True, slots=True)
class PendingAction:
    action_id: str
    kind: str  # list | sensitive | checks | analysis | docs | patches
    caption: str
    hint: str


@dataclass(frozen=True, slots=True)
class Activity:
    """Registro real de una etapa ejecutada, sin razonamiento privado."""

    activity_id: str
    turn: int
    kind: str
    title: str
    detail: str
    status: str  # completada | fallida | omitida
    origin: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ReadyItem:
    item_id: str
    kind: str  # checks | analysis | docs
    title: str
    detail: str
    origin: str


@dataclass(frozen=True, slots=True)
class DocSection:
    title: str
    body: str
    origin: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PatchItem:
    patch_id: str
    finding_id: str
    title: str
    description: str
    diff: str | None
    origin: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    state: str
    findings_count: int
    docs_count: int
    patches_count: int


class ChatSession:
    """Estado conversacional de una revisión; muta solo el servicio."""

    def __init__(
        self, session_id: str, *, configured_model: str | None = None
    ) -> None:
        self.session_id = session_id
        self.configured_model = configured_model
        self.state = "esperando_ruta"
        self.scope_root: Path | None = None
        self.user_context = ""
        self.analysis_mode_instruction = ""
        self.agent_mode = "multiagent"
        self.messages: list[ChatMessage] = []
        self.pending: PendingAction | None = None
        self.evidence: list[Evidence] = []
        self.sensitive_paths: tuple[str, ...] = ()
        self.external_data_consent = False
        self.sensitive_read_approved = False
        self.findings: list[Finding] = []
        self.activities: list[Activity] = []
        self.ready_items: list[ReadyItem] = []
        self.docs: list[DocSection] = []
        self.patches: list[PatchItem] = []
        self.analysis_origin: str | None = None
        self.analysis_provider: str | None = None
        self.analysis_model: str | None = None
        self.chat_provider: str | None = None
        self.chat_model: str | None = None
        self._action_counter = 0
        self._finding_counter = 0
        self._activity_counter = 0

    # -- helpers internos ----------------------------------------------------

    def ask(self, text: str, *, user_text: str | None = None) -> None:
        if user_text is not None:
            self.messages.append(ChatMessage(author="user", text=user_text))
        self.messages.append(ChatMessage(author="agent", text=text))

    def next_action(self, kind: str, caption: str, hint: str) -> PendingAction:
        self._action_counter += 1
        action = PendingAction(
            action_id=f"act-{self._action_counter:03d}",
            kind=kind,
            caption=caption,
            hint=hint,
        )
        self.pending = action
        self.state = "en_curso"
        return action

    def require_pending(self, action_id: str) -> PendingAction:
        pending = self.pending
        if pending is None:
            raise StaleActionError(
                "la sesión no tiene acciones pendientes por aprobar"
            )
        if pending.action_id != action_id:
            raise StaleActionError(
                f"la acción '{action_id}' no es la pendiente "
                f"(esperaba '{pending.action_id}'): el chat avanza en orden"
            )
        return pending

    def record_activity(
        self,
        action: PendingAction,
        *,
        status: str,
        detail: str,
        origin: str,
        duration_ms: int,
    ) -> Activity:
        """Añade una etapa ejecutada al registro visible del turno actual."""

        self._activity_counter += 1
        turn = max(
            1,
            sum(1 for message in self.messages if message.author == "user"),
        )
        activity = Activity(
            activity_id=f"actv-{self._activity_counter:03d}",
            turn=turn,
            kind=action.kind,
            title=_ACTION_TITLES.get(action.kind, action.caption),
            detail=detail,
            status=status,
            origin=origin,
            duration_ms=max(0, duration_ms),
        )
        self.activities.append(activity)
        return activity

    def context_items(self) -> tuple[tuple[ContextItem, ...], int]:
        """Fragmentos de evidencia acotados por el presupuesto de bytes."""

        context: list[ContextItem] = []
        used = 0
        omitted = 0
        ordered_evidence = sorted(
            self.evidence,
            key=lambda item: (
                0
                if self.sensitive_read_approved
                and item.tool == ToolName.READ_FILE.value
                and is_sensitive_path(item.source_path)
                else 1
                if item.tool == ToolName.PATTERN_SCAN.value
                else 2
                if item.tool == ToolName.READ_FILE.value
                else 3
            ),
        )
        for item in ordered_evidence:
            location = item.source_path
            if item.line_start is not None:
                location += f":{item.line_start}"
            candidate = ContextItem(
                evidence_id=item.evidence_id,
                snippet=f"[{location} · {item.tool}] {item.snippet}",
            )
            if used + candidate.size > _MAX_CONTEXT_BYTES:
                omitted += 1
                continue
            used += candidate.size
            context.append(candidate)
        return tuple(context), omitted

    def role_context(self) -> str:
        """Contexto del usuario y preferencia de enfoque, nunca evidencia."""

        return "\n".join(
            item
            for item in (self.user_context, self.analysis_mode_instruction)
            if item
        )

    def confirm_proposals(
        self,
        proposals: tuple[FindingProposal, ...],
        origin: FindingOrigin,
    ) -> tuple[int, int]:
        """Convierte propuestas en hallazgos; descarta citación ajena."""

        index = {item.evidence_id: item for item in self.evidence}
        confirmed = 0
        discarded = 0
        for proposal in proposals:
            self._finding_counter += 1
            try:
                finding = Finding.from_proposal(
                    proposal,
                    finding_id=f"fnd-ch{self._finding_counter:04d}",
                    evidence_index=index,
                    origin=origin,
                )
            except (MissingEvidenceError, ValueError):
                discarded += 1
                continue
            self.findings.append(finding)
            confirmed += 1
        return confirmed, discarded


# -- contexto para el chat ------------------------------------------------------


def _session_brief(session: ChatSession) -> str | None:
    """Resume la auditoría en curso para que el chat hable de lo ya observado.

    Son datos verificados de esta sesión, no instrucciones ni contenido nuevo:
    no se leen archivos ni se envía nada que el usuario no haya autorizado.
    """

    parts: list[str] = []
    if session.scope_root is not None:
        parts.append(f"Carpeta analizada: {session.scope_root}")
    if session.evidence:
        lines = [
            f"- [{item.evidence_id}] {item.location} (herramienta: {item.tool})"
            for item in session.evidence[:25]
        ]
        parts.append("Evidencia observada:\n" + "\n".join(lines))
    if session.findings:
        lines = []
        for finding in session.findings[:25]:
            evidence = ", ".join(finding.evidence_ids) or "sin evidencia citada"
            state = "confirmado" if finding.is_confirmed else "hipótesis"
            lines.append(
                f"- [{finding.severity.value}] {finding.title} ({state}); "
                f"evidencia: {evidence}. {finding.explanation[:280]}"
            )
        parts.append("Hallazgos:\n" + "\n".join(lines))
    if session.docs:
        lines = [
            f"- {section.title}: {section.body[:220]}"
            for section in session.docs[:8]
        ]
        parts.append("Documentación generada:\n" + "\n".join(lines))
    if session.patches:
        lines = [
            f"- {patch.title}: {patch.description[:160]}"
            for patch in session.patches[:8]
        ]
        parts.append("Parches propuestos (solo texto):\n" + "\n".join(lines))
    if not parts:
        return None
    brief = "\n\n".join(parts)
    return brief[:6_000]


def _local_brief_reply(session: ChatSession) -> str | None:
    """Respuesta determinista sobre lo ya auditado cuando no hay modelo."""

    if not session.findings:
        return None
    total = len(session.findings)
    critical = sum(
        1 for finding in session.findings if finding.severity is Severity.CRITICAL
    )
    high = sum(1 for finding in session.findings if finding.severity is Severity.HIGH)
    header = f"Revisé {session.scope_root}: {total} hallazgo(s)"
    if critical or high:
        header += f", {critical} crítico(s) y {high} alto(s)"
    lines = [header + "."]
    for finding in session.findings[:5]:
        lines.append(
            f"· [{finding.severity.value}] {finding.title} "
            f"(evidencia: {', '.join(finding.evidence_ids) or 'sin cita'})"
        )
    if session.patches:
        lines.append(
            f"Hay {len(session.patches)} parche(s) propuesto(s) en el panel."
        )
    return "\n".join(lines)


def _completion_summary(session: ChatSession) -> str:
    """Cierre proactivo: qué se encontró, con qué gravedad y con qué evidencia."""

    if not session.findings:
        return (
            "Revisión completa. No quedaron hallazgos con evidencia válida en "
            "esta sesión; revisa las limitaciones del informe antes de darla "
            "por concluyente. ¿Quieres que explique qué se comprobó?"
        )
    order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
        Severity.UNKNOWN: 5,
    }
    ranked = sorted(session.findings, key=lambda item: order[item.severity])
    critical = sum(
        1 for item in ranked if item.severity is Severity.CRITICAL
    )
    high = sum(1 for item in ranked if item.severity is Severity.HIGH)
    header = f"Revisión completa: {len(ranked)} hallazgo(s)"
    if critical or high:
        header += f", {critical} crítico(s) y {high} de gravedad alta"
    lines = [header + "."]
    for finding in ranked[:4]:
        lines.append(
            f"· [{finding.severity.value}] {finding.title} — evidencia: "
            f"{', '.join(finding.evidence_ids) or 'sin cita'}"
        )
    if len(ranked) > 4:
        lines.append(f"· y {len(ranked) - 4} más en el panel de hallazgos.")
    lines.append(
        "Pregúntame por cualquiera de ellos y te digo qué tan grave es, qué "
        "lo respalda o qué parche propuse."
    )
    return "\n".join(lines)


# -- servicio -------------------------------------------------------------------


class AgentChatService:
    """Gestiona sesiones de chat; una sola acción pendiente por sesión."""

    def __init__(
        self,
        roles: RoleExecutor,
        *,
        provider_label: str,
        chat_responder: ChatResponder | None = None,
        configured_model: str | None = None,
    ) -> None:
        self._roles = roles
        self._provider_label = provider_label
        self._chat_responder = chat_responder
        self._configured_model = (configured_model or "").strip() or None
        self._requires_external_consent = provider_label in {"strands", "openrouter"}
        self._uses_model_provider = provider_label in {
            "lmstudio",
            "strands",
            "openrouter",
        }
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._counter = 0

    # -- API del servicio ----------------------------------------------------

    def create_session(self) -> ChatSession:
        with self._lock:
            self._counter += 1
            session = ChatSession(
                session_id=f"chat-{self._counter:04d}",
                configured_model=self._configured_model,
            )
            session.ask(
                "Hola. Dime la ruta de la carpeta que quieres revisar "
                "(ej: C:\\Users\\tu\\proyecto). Solo leo: no modifico ni "
                "ejecuto nada. Te consultaré antes de abrir archivos "
                "confidenciales o compartir evidencia con un proveedor externo."
            )
            self._sessions[session.session_id] = session
            self._session_locks[session.session_id] = threading.RLock()
            return self._snapshot(session)

    def send_message(
        self,
        session_id: str,
        text: str,
        *,
        project_path: str | None = None,
        analysis_mode: str = "reading",
        agent_mode: str = "multiagent",
        on_chat_delta: Callable[[str], None] | None = None,
    ) -> ChatSession:
        session, session_lock = self._session_record(session_id)
        with session_lock:
            clean = text.strip()
            if not clean:
                raise AgentChatError("el texto no puede estar vacío")
            if analysis_mode not in _ANALYSIS_MODE_INSTRUCTIONS:
                raise AgentChatError("el enfoque de análisis no es válido")
            if agent_mode not in _AGENT_MODES:
                raise AgentChatError("el modo de agentes no es válido")

            if session.state == "esperando_ruta":
                if project_path is not None and len(clean) > _MAX_USER_CONTEXT_CHARS:
                    session.ask(
                        f"La instrucción supera el límite de "
                        f"{_MAX_USER_CONTEXT_CHARS} caracteres. Resúmela y "
                        "envíala de nuevo.",
                        user_text=clean,
                    )
                    return self._snapshot(session)
                session.analysis_mode_instruction = (
                    _ANALYSIS_MODE_INSTRUCTIONS[analysis_mode]
                )
                session.agent_mode = agent_mode
                self._receive_root(
                    session,
                    clean,
                    project_path=project_path,
                    on_chat_delta=on_chat_delta,
                )
            else:
                if _looks_like_path(clean):
                    session.ask(_status_line(session), user_text=clean)
                else:
                    self._reply_to_user(
                        session,
                        clean,
                        on_chat_delta=on_chat_delta,
                        local_fallback=(
                            _local_brief_reply(session) or _status_line(session)
                        ),
                    )
            return self._snapshot(session)

    def approve(self, session_id: str, action_id: str) -> ChatSession:
        session, session_lock = self._session_record(session_id)
        with session_lock:
            action = session.require_pending(action_id)
            session.pending = None
            if action.kind == "sensitive":
                session.sensitive_read_approved = True
            if (
                self._requires_external_consent
                and (
                    action.kind == "security"
                    or (action.kind == "analysis" and session.agent_mode == "agent")
                )
            ):
                session.external_data_consent = True
            before = _SessionCounts.capture(session)
            started = time.perf_counter()
            self._execute(session, action)
            duration_ms = round((time.perf_counter() - started) * 1000)
            status = (
                "fallida"
                if _failure_count(session) > before.failures
                else "completada"
            )
            session.record_activity(
                action,
                status=status,
                detail=_activity_detail(session, before, status),
                origin=_activity_origin(session, before, status),
                duration_ms=duration_ms,
            )
            return self._snapshot(session)

    def skip(self, session_id: str, action_id: str) -> ChatSession:
        session, session_lock = self._session_record(session_id)
        with session_lock:
            action = session.require_pending(action_id)
            is_single_agent_consent = (
                action.kind == "analysis" and session.agent_mode == "agent"
            )
            started = time.perf_counter()
            if (
                self._requires_external_consent
                and (action.kind == "security" or is_single_agent_consent)
            ):
                session.pending = None
                session.ready_items.append(
                    ReadyItem(
                        item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                        kind="agent_failure",
                        title="Análisis con IA omitido",
                        detail=(
                            "No se compartió evidencia con el proveedor "
                            "externo. La auditoría continúa con resultados "
                            "deterministas y plantillas locales."
                        ),
                        origin="decisión del usuario; sin envío externo",
                    )
                )
                session.docs.extend(_docs_from_findings(session))
                session.patches.extend(_patches_from_findings(session))
                session.ask(
                    "Auditoría local completada. No se envió información al "
                    "proveedor de IA; puedes revisar la evidencia y las "
                    "recomendaciones locales."
                )
                session.state = "completa"
                session.record_activity(
                    action,
                    status="omitida",
                    detail="No se compartió evidencia con el proveedor externo.",
                    origin="decisión del usuario; sin envío externo",
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
                return self._snapshot(session)
            if action.kind != "sensitive":
                raise StaleActionError(
                    "solo se puede omitir una solicitud de archivo sensible "
                    "o el consentimiento inicial para IA externa"
                )
            session.pending = None
            session.ask(
                "Archivos sensibles omitidos por decisión del usuario. "
                "No se leyó su contenido; continuaré con el resto del proyecto."
            )
            self._propose_checks(session)
            session.record_activity(
                action,
                status="omitida",
                detail="No se leyó el contenido de los archivos sensibles.",
                origin="decisión del usuario",
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            return self._snapshot(session)

    def get_session(self, session_id: str) -> ChatSession:
        session, session_lock = self._session_record(session_id)
        with session_lock:
            return self._snapshot(session)

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        with self._lock:
            ordered = sorted(self._sessions.values(), key=lambda s: s.session_id)
            return tuple(
                SessionSummary(
                    session_id=session.session_id,
                    state=session.state,
                    findings_count=len(session.findings),
                    docs_count=len(session.docs),
                    patches_count=len(session.patches),
                )
                for session in ordered
            )

    # -- despacho de acciones ------------------------------------------------

    def _execute(self, session: ChatSession, action: PendingAction) -> None:
        executors = {
            "list": self._action_list,
            "sensitive": self._action_sensitive,
            "checks": self._action_checks,
            "security": self._action_security,
            "analysis": self._action_analysis,
            "sources": self._action_sources,
            "docs": self._action_docs,
            "patches": self._action_patches,
            "review": self._action_review,
        }
        executor = executors.get(action.kind)
        if executor is None:  # pragma: no cover — kinds creados internamente
            raise StaleActionError(f"tipo de acción desconocido: {action.kind}")
        if (
            self._requires_external_consent
            and action.kind in {"analysis", "sources", "docs", "patches", "review"}
            and not session.external_data_consent
        ):
            raise StaleActionError(
                "el envío al proveedor externo requiere consentimiento inicial"
            )
        executor(session)

    def _action_list(self, session: ChatSession) -> None:
        try:
            reader = LocalReadOnlyTool(session.scope_root, id_namespace="chat")
            result = reader.run(ToolRequest(tool=ToolName.LIST_FILES))
        except ToolError as error:
            session.ask(
                f"No pude listar la carpeta ({error}). No hay nada que "
                "tocar: empieza otra sesión nueva si quieres otra ruta."
            )
            session.state = "bloqueada"
            return

        if result.outcome is ToolOutcome.FAILED:
            session.ask(
                f"El listado declaró un fallo explícito: "
                f"{result.failure_reason}. Nada fue tocado."
            )
            session.state = "bloqueada"
            return

        session.evidence.extend(result.evidence)
        names = _short_names(result.evidence)
        try:
            session.sensitive_paths = reader.list_sensitive_files()
        except ToolError as error:
            session.ask(
                "No pude completar el inventario de archivos confidenciales "
                f"({error}). No leeré archivos ni continuaré la auditoría con "
                "un inventario incompleto; inicia otra sesión con una carpeta "
                "de alcance menor."
            )
            session.state = "bloqueada"
            return
        session.ask(
            f"Listado hecho: {len(result.evidence)} archivo(s) presentes "
            f"({names}). Ahora leeré código y texto permitido para generar "
            "evidencia; no ejecutaré nada."
        )
        self._read_project_sources(session, result.evidence)
        if session.sensitive_paths:
            paths = ", ".join(session.sensitive_paths)
            session.ask(
                "Detecté archivos confidenciales excluidos de la lectura "
                "normal: "
                f"{paths}. ¿Autorizas leerlos? Si el análisis con IA está "
                "conectado, se te pedirá aparte antes de compartir evidencia "
                "con ese proveedor."
            )
            session.next_action(
                kind="sensitive",
                caption="Autorizar lectura de archivos confidenciales",
                hint=(
                    "Se leerán localmente solo estos archivos: "
                    f"{paths}. Su contenido no se envía a un modelo sin una "
                    "autorización separada. Límite: 16 KB por archivo y "
                    "512 KB en total."
                ),
            )
            return
        self._propose_checks(session)

    def _read_project_sources(
        self, session: ChatSession, listed_evidence: tuple[Evidence, ...]
    ) -> None:
        if session.scope_root is None:
            raise AgentChatError("la sesión no tiene una carpeta autorizada")
        candidates = [
            item.source_path
            for item in listed_evidence
            if _is_readable_source(item.source_path)
        ]
        remaining_bytes = _MAX_TOTAL_SOURCE_BYTES
        read_count = 0
        failures: list[str] = []
        for index, relative_path in enumerate(
            candidates[:_MAX_INITIAL_SOURCE_FILES], start=1
        ):
            byte_limit = min(_MAX_SOURCE_FILE_BYTES, remaining_bytes)
            if byte_limit <= 0:
                break
            reader = LocalReadOnlyTool(
                session.scope_root,
                id_namespace=f"chat-read-{index}",
            )
            result = reader.run(
                ToolRequest(
                    tool=ToolName.READ_FILE,
                    relative_path=relative_path,
                    limits=ToolLimits(
                        max_bytes_per_file=byte_limit,
                        max_total_bytes=byte_limit,
                    ),
                )
            )
            remaining_bytes -= byte_limit
            if result.outcome is ToolOutcome.FAILED:
                failures.append(f"{relative_path}: {result.failure_reason}")
                continue
            session.evidence.extend(result.evidence)
            read_count += 1

        session.ask(
            f"Lectura acotada completada: {read_count} archivo(s) de código "
            "o texto incluidos como evidencia."
        )
        omitted_count = max(0, len(candidates) - read_count - len(failures))
        if omitted_count:
            session.ask(
                f"Se omitieron {omitted_count} archivo(s) por los límites "
                "de cantidad o tamaño; puedes reducir el alcance."
            )
        if failures:
            session.ask(
                "No se pudieron leer algunos archivos permitidos: "
                + "; ".join(failures[:5])
            )

    def _action_sensitive(self, session: ChatSession) -> None:
        if session.scope_root is None:
            raise AgentChatError("la sesión no tiene una carpeta autorizada")
        remaining_bytes = _MAX_TOTAL_SOURCE_BYTES
        read_count = 0
        failed_paths: list[str] = []
        omitted_paths: list[str] = []
        for index, relative_path in enumerate(session.sensitive_paths, start=1):
            byte_limit = min(_MAX_SOURCE_FILE_BYTES, remaining_bytes)
            if byte_limit <= 0:
                omitted_paths.extend(session.sensitive_paths[index - 1 :])
                break
            reader = LocalReadOnlyTool(
                session.scope_root,
                id_namespace=f"chat-sensitive-{index}",
            )
            result = reader.run(
                ToolRequest(
                    tool=ToolName.READ_FILE,
                    relative_path=relative_path,
                    limits=ToolLimits(
                        max_bytes_per_file=byte_limit,
                        max_total_bytes=byte_limit,
                    ),
                    allow_sensitive=True,
                )
            )
            remaining_bytes -= byte_limit
            if result.outcome is ToolOutcome.FAILED:
                failed_paths.append(relative_path)
                continue
            session.evidence.extend(result.evidence)
            read_count += 1
        session.ask(
            f"Lectura confidencial autorizada: {read_count} archivo(s) leídos. "
            "El contenido queda "
            "en esta sesión; antes de compartirlo con un proveedor de IA "
            "externo solicitaré una autorización separada."
        )
        if omitted_paths:
            session.ask(
                f"Se omitieron {len(omitted_paths)} archivo(s) confidenciales "
                "por el límite de lectura de 512 KB por sesión."
            )
        if failed_paths:
            session.ask(
                "No se pudieron leer algunos archivos autorizados: "
                + ", ".join(failed_paths[:5])
            )
        self._propose_checks(session)

    def _propose_checks(self, session: ChatSession) -> None:
        session.ask("Ejecutando comprobaciones deterministas de solo lectura.")
        session.next_action(
            kind="checks",
            caption="Ejecutar comprobaciones deterministas",
            hint="Sin IA: heurística de patrón sobre los archivos listados.",
        )

    def _action_checks(self, session: ChatSession) -> None:
        checks = DeterministicChecks(session.scope_root)
        runs: list[CheckRun] = []
        try:
            for spec in BUNDLED_CHECKS:
                runs.append(checks.run(spec))
        except ToolError as error:
            session.ask(
                f"Las comprobaciones fallaron explícitamente ({error}). "
                "Lo ya producido queda en el registro; no continuaré con "
                "este análisis."
            )
            session.state = "bloqueada"
            return

        new_evidence: list[Evidence] = []
        proposals_count = 0
        for run in runs:
            new_evidence.extend(run.result.evidence)
            proposals_count += len(run.proposals)
        session.evidence.extend(new_evidence)
        confirmed, discarded = session.confirm_proposals(
            tuple(p for run in runs for p in run.proposals),
            FindingOrigin.DETERMINISTIC,
        )
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="checks",
                title="Evidencia y hallazgos deterministas listos",
                detail=(
                    f"{len(new_evidence)} evidencia(s) nuevas; "
                    f"{confirmed} hallazgo(s) confirmado(s); "
                    f"{discarded} propuesta(s) descartada(s)."
                ),
                origin="comprobaciones deterministas (sin IA)",
            )
        )
        session.ask(
            f"Comprobación terminada: {proposals_count} propuesta(s), "
            f"{confirmed} hallazgo(s) confirmado(s) ({discarded} "
            "descartado(s) por citación inválida). El panel "
            "'Listo para revisar' ya tiene la evidencia."
        )
        if session.agent_mode == "agent":
            self._propose_analysis(session)
            return
        session.ask(
            "Ahora el agente especialista de seguridad revisará la evidencia "
            "autorizada."
        )
        session.next_action(
            kind="security",
            caption="Evaluación de seguridad",
            hint=(
                "Analiza solo la evidencia seleccionada. Un proveedor remoto "
                "requiere autorización y puede tener coste."
            ),
        )

    def _action_security(self, session: ChatSession) -> None:
        context, omitted = session.context_items()
        try:
            result = self._roles.run(
                RoleRequest(
                    role=RoleName.RISK_RESEARCH,
                    instruction=(
                        "Actúa como especialista de seguridad. Analiza "
                        "únicamente las evidencias entregadas: identifica "
                        "riesgos y controles relevantes, separa hechos de "
                        "hipótesis y cita solo evidence_ids existentes. No "
                        "ejecutes código ni afirmes explotabilidad. "
                        "Contexto del usuario (no verificado):\n"
                        + (session.role_context() or "(no aportado)")
                    ),
                    context=context,
                    limits=RoleLimits(timeout_seconds=_ROLE_TIMEOUT_SECONDS),
                )
            )
        except (RoleError, RoleNotConfigured) as error:
            self._record_role_failure(session, "Agente de seguridad", str(error))
            self._propose_analysis(session)
            return

        if result.outcome is not RoleOutcome.COMPLETED:
            reason = (
                result.summary
                if result.outcome is RoleOutcome.REFUSED
                else result.error or "motivo no declarado"
            )
            self._record_role_failure(session, "Agente de seguridad", reason)
        else:
            confirmed, discarded = session.confirm_proposals(
                result.proposals, FindingOrigin.MODEL
            )
            attribution = _attribution(
                result.origin.value, result.provider, result.model
            )
            session.ready_items.append(
                ReadyItem(
                    item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                    kind="security",
                    title="Evaluación del agente de seguridad",
                    detail=(
                        f"{result.summary[:240]} "
                        f"{confirmed} hallazgo(s) respaldado(s), "
                        f"{discarded} propuesta(s) descartada(s), "
                        f"{omitted} fragmento(s) omitido(s) por límite."
                    ),
                    origin=attribution,
                )
            )
            session.ask(
                f"Evaluación de seguridad lista ({attribution}). "
                f"Hallazgos aceptados: {confirmed}; propuestas descartadas: "
                f"{discarded}. Ahora puedo analizar la causa probable."
            )

        self._propose_analysis(session)

    def _record_role_failure(
        self, session: ChatSession, role_label: str, reason: str
    ) -> None:
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="agent_failure",
                title=f"{role_label} no completado",
                detail=reason,
                origin="fallo declarado; no se simula resultado",
            )
        )
        session.ask(
            f"{role_label} no completó la tarea: {reason}. "
            "El fallo queda visible y continuaré con el siguiente paso."
        )

    def _propose_analysis(self, session: ChatSession) -> None:
        if session.agent_mode == "agent":
            caption = "Análisis del agente de auditoría"
            hint = (
                "Un agente revisa la evidencia autorizada y propone hallazgos "
                "con respaldo. Los informes y recomendaciones finales se "
                "generan localmente."
            )
            session.ask(
                "El agente de auditoría analizará la evidencia y propondrá "
                "hallazgos; el informe y las recomendaciones se prepararán "
                "localmente."
            )
        else:
            caption = "Investigar la causa del problema"
            hint = (
                "Analiza el contexto acotado. Un proveedor remoto requiere "
                "autorización y puede tener coste."
            )
            session.ask(
                "Siguiente: investigación de la causa probable con la evidencia "
                "ya autorizada."
            )
        session.next_action(
            kind="analysis",
            caption=caption,
            hint=hint,
        )

    def _action_analysis(self, session: ChatSession) -> None:
        context, omitted = session.context_items()
        instruction = (
            "Actúa como único agente generalista de auditoría técnica. Con "
            "las evidencias proporcionadas, analiza riesgos y causas probables, "
            "separa hechos de hipótesis y propone solo hallazgos respaldados "
            "por evidence_ids existentes. No inventes fuentes, no afirmes "
            "haber ejecutado pruebas ni declares explotabilidad. Devuelve un "
            "resumen breve del trabajo realizado y propuestas verificables."
            if session.agent_mode == "agent"
            else ANALYSIS_INSTRUCTION
        )
        try:
            result = self._roles.run(
                RoleRequest(
                    role=RoleName.CAUSE_ANALYSIS,
                    instruction=_role_instruction(
                        instruction, session.role_context()
                    ),
                    context=context,
                    limits=RoleLimits(timeout_seconds=_ROLE_TIMEOUT_SECONDS),
                )
            )
        except (RoleError, RoleNotConfigured) as error:
            session.ask(
                f"El análisis no corrió: {error}. Los resultados disponibles "
                "quedarán identificados y no se simulará una respuesta."
            )
            if session.agent_mode == "agent":
                self._complete_single_agent(session)
                return
            self._record_local_sources(session)
            self._propose_docs(session)
            return

        if result.outcome is not RoleOutcome.COMPLETED or (
            result.origin.value == "mock" and (result.provider or result.model)
        ):
            session.analysis_origin = "failed"
            reason = (
                result.summary
                if result.outcome is RoleOutcome.REFUSED
                else result.error
            ) or (
                "el ejecutor declaró provider/model con un resultado "
                "simulado"
            )
            session.ask(
                f"El análisis declaró un fallo explícito: {reason}. "
                "No voy a fingir éxito; conservaré los resultados locales."
            )
            if session.agent_mode == "agent":
                self._complete_single_agent(session)
                return
            self._record_local_sources(session)
            self._propose_docs(session)
            return

        session.analysis_origin = result.origin.value
        session.analysis_provider = result.provider
        session.analysis_model = result.model
        attribution = _attribution(result.origin.value, result.provider, result.model)
        confirmed, discarded = session.confirm_proposals(
            result.proposals, FindingOrigin.MODEL
        )
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="analysis",
                title=(
                    "Análisis del agente de auditoría"
                    if session.agent_mode == "agent"
                    else "Análisis de causa disponible"
                ),
                detail=(
                    f"{result.summary[:180]}; "
                    f"{confirmed} hallazgo(s) de modelo, "
                    f"{discarded} propuesta(s) descartada(s)."
                ),
                origin=attribution,
            )
        )
        session.ask(
            f"Análisis terminado ({attribution}): {confirmed} hallazgo(s), "
            f"{discarded} descartado(s), {omitted} fragmento(s) omitidos "
            "por límite de contexto. Resumen: "
            + (result.summary or "(sin summary)")
        )
        if session.agent_mode == "agent":
            self._complete_single_agent(session)
            return
        self._propose_sources(session)

    def _complete_single_agent(self, session: ChatSession) -> None:
        docs = _docs_from_findings(session)
        patches = _patches_from_findings(session)
        session.docs.extend(docs)
        session.patches.extend(patches)
        attribution = (
            "plantilla local (sin modelo)"
            if session.analysis_origin in (None, "failed", "mock")
            else _attribution(
                session.analysis_origin,
                session.analysis_provider,
                session.analysis_model,
            )
        )
        session.ready_items.extend(
            (
                ReadyItem(
                    item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                    kind="docs",
                    title="Informe preparado localmente",
                    detail=f"{len(docs)} sección(es) basadas en hallazgos confirmados.",
                    origin="generación local",
                ),
                ReadyItem(
                    item_id=f"rdy-{len(session.ready_items) + 2:03d}",
                    kind="patches",
                    title="Recomendaciones preparadas localmente",
                    detail=f"{len(patches)} recomendación(es); no se modificó ningún archivo.",
                    origin="generación local; no aplicada",
                ),
            )
        )
        session.ask(
            "Auditoría con un agente completada. "
            f"{len(session.findings)} hallazgo(s), {len(docs)} sección(es) "
            f"de informe y {len(patches)} recomendación(es). "
            f"Análisis: {attribution}. Revisa la actividad y la evidencia."
        )
        session.state = "completa"

    def _propose_sources(self, session: ChatSession) -> None:
        session.ask(
            "Siguiente: revisión de procedencia con fuentes locales; no se "
            "realizará búsqueda web."
        )
        session.next_action(
            kind="sources",
            caption="Revisar procedencia de fuentes",
            hint=(
                "Solo evidencias locales ya acotadas; sin navegación web. "
                "El proveedor remoto requiere autorización."
            ),
        )

    def _action_sources(self, session: ChatSession) -> None:
        context, omitted = session.context_items()
        instruction = (
            "Revisa la procedencia de las fuentes representadas en las "
            "evidencias. No hagas búsquedas web, no inventes fuentes ni "
            "declares verificación externa. Distingue lo observado, el origen "
            "local (ruta y herramienta) y lo que queda sin verificar. "
            "Contexto aportado por el usuario (no verificado):\n"
            + (session.role_context() or "(no aportado)")
        )
        try:
            result = self._roles.run(
                RoleRequest(
                    role=RoleName.WEB_RESEARCH,
                    instruction=instruction,
                    context=context,
                    limits=RoleLimits(
                        timeout_seconds=_ROLE_TIMEOUT_SECONDS,
                        allow_network=False,
                    ),
                )
            )
        except (RoleError, RoleNotConfigured) as error:
            self._source_failure(session, str(error))
            return

        if result.outcome is not RoleOutcome.COMPLETED:
            reason = (
                result.summary
                if result.outcome is RoleOutcome.REFUSED
                else result.error or "motivo no declarado"
            )
            self._source_failure(session, reason)
            return

        attribution = _attribution(
            result.origin.value, result.provider, result.model
        )
        source_body = (
            (result.summary or "El rol no devolvió resumen.")
            + f" Fragmentos omitidos por límite: {omitted}. "
            "No se realizó búsqueda web ni verificación externa."
        )
        valid_source_proposals = [
            proposal
            for proposal in result.proposals
            if proposal.evidence_ids
            and set(proposal.evidence_ids).issubset(
                {item.evidence_id for item in session.evidence}
            )
        ]
        if valid_source_proposals:
            source_body += "\n\nObservaciones respaldadas por evidencia:\n" + "\n".join(
                f"• {proposal.title}: {proposal.explanation} "
                f"[evidencia: {', '.join(proposal.evidence_ids)}]"
                for proposal in valid_source_proposals
            )
        session.docs.append(
            DocSection(
                title="Procedencia de fuentes",
                body=source_body,
                origin=attribution,
                evidence_ids=tuple(
                    dict.fromkeys(
                        evidence_id
                        for proposal in valid_source_proposals
                        for evidence_id in proposal.evidence_ids
                    )
                ),
            )
        )
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="sources",
                title="Revisión de fuentes lista",
                detail=(
                    "Procedencia evaluada solo a partir del contexto local; "
                    "sin búsqueda ni verificación web. "
                    + (result.summary[:160] if result.summary else "")
                ),
                origin=attribution,
            )
        )
        session.ask(
            f"Revisión de fuentes lista ({attribution}); sin búsqueda web. "
            "Ya puedes verla junto a la documentación."
        )
        self._propose_docs(session)

    def _source_failure(self, session: ChatSession, reason: str) -> None:
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="sources",
                title="Investigador de fuentes no disponible",
                detail=reason,
                origin="fallo declarado; no se simula investigación",
            )
        )
        session.ask(
            "El investigador de fuentes falló explícitamente: "
            f"{reason}. No declararé la investigación como completada. "
            "¿Continúo con la documentación local de los hallazgos?"
        )
        self._propose_docs(session)

    def _record_local_sources(self, session: ChatSession) -> None:
        """List local evidence provenance without pretending to research it."""

        lines = sorted(
            {
                f"{item.source_path} — generado por {item.tool}"
                for item in session.evidence
            }
        )
        body = (
            "Inventario local de procedencia (sin investigador IA ni "
            "búsqueda web):\n"
            + ("\n".join(lines) if lines else "No hay fuentes locales producidas.")
        )
        session.docs.append(
            DocSection(
                title="Fuentes locales disponibles",
                body=body,
                origin="inventario local (sin IA)",
            )
        )
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="sources",
                title="Inventario local de fuentes disponible",
                detail=(
                    f"{len(lines)} ruta(s) de evidencia; el investigador de "
                    "fuentes no se marcó como completado."
                ),
                origin="inventario local (sin IA)",
            )
        )

    def _propose_docs(self, session: ChatSession) -> None:
        if not session.findings:
            session.ask(
                "No hay hallazgos todavía: la documentación de plantilla "
                "necesita al menos un hallazgo confirmado para tener "
                "contenido real que resumir."
            )
            session.state = "completa"
            return
        session.ask("Siguiente: preparación del informe técnico.")
        session.next_action(
            kind="docs",
            caption="Preparar informe de documentación",
            hint=(
                "Solo lectura y evidencia acotada. Con un modelo configurado "
                "se consulta al rol especialista; sin modelo se usa plantilla."
            ),
        )

    def _action_docs(self, session: ChatSession) -> None:
        sections: list[DocSection] = []
        origin = _TEMPLATE_ORIGIN
        if self._uses_model_provider:
            context, omitted = session.context_items()
            request = RoleRequest(
                role=RoleName.DOCUMENTATION,
                instruction=(
                    "Redacta un informe conciso y profesional, basado solo en "
                    "estas evidencias. Distingue hechos observados, riesgos "
                    "posibles e información faltante. No inventes hechos. "
                    "Cada propuesta debe citar evidence_ids existentes. "
                    "Contexto aportado por el usuario (no verificado):\n"
                    + (session.role_context() or "(no aportado)")
                ),
                context=context,
                limits=RoleLimits(timeout_seconds=_ROLE_TIMEOUT_SECONDS),
            )
            try:
                result = self._roles.run(request)
            except (RoleError, RoleNotConfigured) as error:
                session.ask(
                    "El rol documental no pudo ejecutarse: "
                    f"{error}. Continuaré con la plantilla local identificada."
                )
            else:
                if result.outcome is RoleOutcome.COMPLETED and result.origin.value == "real":
                    sections = _docs_from_role(session, result, omitted)
                    if sections:
                        origin = _attribution(
                            result.origin.value, result.provider, result.model
                        )
                    else:
                        session.ask(
                            "El especialista no devolvió secciones que citen "
                            "evidencia válida. No publicaré ese texto como "
                            "informe; usaré la plantilla local."
                        )
                else:
                    reason = (
                        result.summary
                        if result.outcome is RoleOutcome.REFUSED
                        else result.error or "motivo no declarado"
                    )
                    session.ask(
                        "El rol documental no produjo un informe real: "
                        f"{reason}. La alternativa será la plantilla local."
                    )

        if not sections:
            sections = _docs_from_findings(session)
            if self._uses_model_provider:
                origin = _TEMPLATE_ORIGIN
        session.docs.extend(sections)
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="docs",
                title="Documentación lista para revisar",
                detail=(
                    f"{len(sections)} sección(es): resumen + una por "
                    "hallazgo."
                ),
                origin=origin,
            )
        )
        session.ask(
            "Documentación lista: la ves en el panel 'Listo para revisar' "
            f"({len(sections)} sección(es), cada una con su evidencia)."
        )
        if not session.findings:
            session.ask(
                "No hay hallazgos para proponer parches: la revisión queda "
                "completa sin panel de parches."
            )
            session.ask(_completion_summary(session))
            session.state = "completa"
            return
        session.ask(
            "Siguiente: propuestas de remediación en texto; no se aplicarán "
            "cambios al proyecto."
        )
        session.next_action(
            kind="patches",
            caption="Proponer parches (solo texto)",
            hint=(
                "Solicita una propuesta al rol especializado. Un proveedor "
                "remoto requiere autorización y puede tener coste. Solo texto; "
                "nunca se aplica."
            ),
        )

    def _action_patches(self, session: ChatSession) -> None:
        context, _omitted = session.context_items()
        request = RoleRequest(
            role=RoleName.PATCH_SUGGESTION,
            instruction=(
                "Propón mitigaciones como texto explicativo, una por hallazgo. "
                "Cita únicamente evidence_ids del contexto. No generes diff "
                "ni código no verificado: devuelve explanation con la acción "
                "recomendada. No apliques cambios. Contexto del usuario "
                "(no verificado):\n"
                + (session.role_context() or "(no aportado)")
            ),
            context=context,
            limits=RoleLimits(timeout_seconds=_ROLE_TIMEOUT_SECONDS),
        )
        try:
            result = self._roles.run(request)
        except (RoleError, RoleNotConfigured) as error:
            result = None
            failure = str(error)
        else:
            failure = result.error or "motivo no declarado"

        if result is not None and result.outcome is RoleOutcome.COMPLETED:
            patches = _patches_from_role(session, result)
            if patches:
                session.patches.extend(patches)
            else:
                session.patches.extend(_patches_from_findings(session))
                session.ask(
                    "El rol de parches no devolvió propuestas que citen "
                    "evidencia válida; las recomendaciones visibles son "
                    "plantillas locales, no salida del modelo."
                )
        else:
            if result is not None:
                failure = result.summary if result.outcome is RoleOutcome.REFUSED else failure
            session.ask(
                f"El rol especializado de parches falló o rehusó la tarea: "
                f"{failure}. No lo presentaré como salida de IA; mostraré "
                "recomendaciones deterministas locales, identificadas como tales."
            )
            session.patches.extend(_patches_from_findings(session))

        session.ask(
            f"Hay {len(session.patches)} propuesta(s) en el panel 'Parches': "
            "solo texto, nada se escribe en el proyecto auditado. "
            "Siguiente: revisión independiente de coherencia."
        )
        session.next_action(
            kind="review",
            caption="Revisión final de calidad",
            hint=(
                "Comprueba los resultados contra la evidencia disponible. "
                "Un proveedor remoto requiere autorización y puede tener coste."
            ),
        )

    def _action_review(self, session: ChatSession) -> None:
        context, omitted = session.context_items()
        artifacts = [
            "Hallazgos:\n"
            + "\n".join(
                f"- {item.finding_id}: {item.title}; evidencia "
                f"{', '.join(item.evidence_ids)}; "
                f"{item.explanation[:180]}"
                for item in session.findings
            ),
            "Documentación:\n"
            + "\n".join(
                f"- {item.title} ({item.origin}): {item.body[:180]}"
                for item in session.docs
            ),
            "Propuestas de parche:\n"
            + "\n".join(
                f"- {item.title}: {item.description[:180]}"
                for item in session.patches
            ),
        ]
        artifact_text = "\n".join(artifacts)
        review_prefix = (
            "Actúa como revisor independiente. Contrasta los hallazgos, "
            "documentación y propuestas de parche con las evidencias "
            "entregadas. Señala contradicciones, afirmaciones sin respaldo "
            "y limitaciones; no inventes hechos, no apliques cambios y cita "
            "solo evidence_ids existentes. Devuelve observaciones como "
            "propuestas respaldadas por evidencia.\n"
            "Artefactos que debes revisar:\n"
        )
        context_label = "\nContexto del usuario (no verificado):\n"
        user_context = session.role_context() or "(no aportado)"
        if len(user_context) > 1_000:
            user_context = user_context[:980] + " [contexto truncado]"
        artifact_budget = max(
            0,
            MAX_INSTRUCTION_CHARS
            - len(review_prefix)
            - len(context_label)
            - len(user_context),
        )
        artifact_summary = artifact_text[:artifact_budget]
        instruction = (
            review_prefix
            + artifact_summary
            + context_label
            + user_context
        )
        try:
            result = self._roles.run(
                RoleRequest(
                    role=RoleName.DOC_REVIEW,
                    instruction=instruction,
                    context=context,
                    limits=RoleLimits(timeout_seconds=_ROLE_TIMEOUT_SECONDS),
                )
            )
        except (RoleError, RoleNotConfigured) as error:
            self._review_failure(session, str(error))
            return

        if result.outcome is not RoleOutcome.COMPLETED:
            reason = (
                result.summary
                if result.outcome is RoleOutcome.REFUSED
                else result.error or "motivo no declarado"
            )
            self._review_failure(session, reason)
            return

        evidence_ids = {item.evidence_id for item in session.evidence}
        valid_proposals = [
            proposal
            for proposal in result.proposals
            if proposal.evidence_ids
            and set(proposal.evidence_ids).issubset(evidence_ids)
        ]
        discarded = len(result.proposals) - len(valid_proposals)
        attribution = _attribution(
            result.origin.value, result.provider, result.model
        )
        review_body = (
            (result.summary or "El revisor no devolvió un resumen.")
            + f"\n\nFragmentos omitidos por límite de contexto: {omitted}."
        )
        if valid_proposals:
            review_body += "\n\nObservaciones respaldadas por evidencia:\n" + "\n".join(
                f"- {proposal.title}: {proposal.explanation} "
                f"[evidencia: {', '.join(proposal.evidence_ids)}]"
                for proposal in valid_proposals
            )
        session.docs.append(
            DocSection(
                title="Revisión final de calidad",
                body=review_body,
                origin=attribution,
                evidence_ids=tuple(
                    dict.fromkeys(
                        evidence_id
                        for proposal in valid_proposals
                        for evidence_id in proposal.evidence_ids
                    )
                ),
            )
        )
        session.ready_items.append(
            ReadyItem(
                item_id=f"rdy-{len(session.ready_items) + 1:03d}",
                kind="review",
                title="Revisión final completada",
                detail=(
                    f"{result.summary[:240]} "
                    f"{len(valid_proposals)} observación(es) citada(s); "
                    f"{discarded} propuesta(s) descartada(s) por falta de "
                    "evidencia válida."
                ),
                origin=attribution,
            )
        )
        session.ask(
            f"Revisión final completada ({attribution}). El resultado está "
            "en Documentación y fuentes; solo se muestran observaciones que "
            "citan evidencia de esta sesión."
        )
        session.ask(_completion_summary(session))
        session.state = "completa"

    def _review_failure(self, session: ChatSession, reason: str) -> None:
        self._record_role_failure(session, "Agente revisor", reason)
        session.ask(
            "La revisión final no se completó; los resultados anteriores "
            "siguen identificados por su procedencia y requieren revisión "
            "humana."
        )
        session.state = "completa"

    # -- soporte ----------------------------------------------------------------

    def _receive_root(
        self,
        session: ChatSession,
        text: str,
        *,
        project_path: str | None = None,
        on_chat_delta: Callable[[str], None] | None = None,
    ) -> None:
        if project_path is not None:
            clean_path = project_path.strip().strip("\"'")
            if not clean_path:
                session.ask(
                    "La carpeta de contexto está vacía. Selecciona una "
                    "carpeta válida e inténtalo de nuevo.",
                    user_text=text,
                )
                return
            candidate = Path(clean_path)
            if not candidate.is_dir():
                session.ask(
                    "La carpeta de contexto ya no está disponible en esta "
                    "máquina. Selecciona otra carpeta e inténtalo de nuevo.",
                    user_text=text,
                )
                return
            session.scope_root = candidate
            session.user_context = text
            session.ask(
                "Recibí tu instrucción y la carpeta de contexto. Empezaré la "
                "revisión en modo de solo lectura.",
                user_text=text,
            )
            session.next_action(
                kind="list",
                caption="Listar archivos de la carpeta",
                hint=(
                    "Solo nombres y presencia (sin leer contenido) de los "
                    "archivos permitidos."
                ),
            )
            return

        if _is_greeting(text):
            self._reply_to_user(
                session,
                text,
                on_chat_delta=on_chat_delta,
                local_fallback=(
                    "¡Hola! Soy AudiAI, tu agente de auditoría de sistemas. "
                    "Puedo revisar un proyecto en solo lectura y mostrar "
                    "evidencias. ¿Qué te gustaría investigar? Cuando quieras "
                    "iniciar una auditoría, comparte la ruta de la carpeta."
                ),
            )
            return

        clean_path = text.strip().strip("\"'")
        candidate = Path(clean_path)
        if not candidate.is_dir():
            if _looks_like_path(clean_path):
                session.ask(
                    f"La ruta \"{text[:120]}\" no es una carpeta existente "
                    "en esta máquina. No ejecuté nada. Puedes pegar otra "
                    "ruta, o compartir contexto del proyecto.",
                    user_text=text,
                )
                return
            combined_context = (
                f"{session.user_context}\n{text}"
                if session.user_context
                else text
            )
            if len(combined_context) > _MAX_USER_CONTEXT_CHARS:
                session.ask(
                    f"El contexto supera el límite de "
                    f"{_MAX_USER_CONTEXT_CHARS} caracteres. Resúmelo y "
                    "envíalo de nuevo, o pega la ruta del proyecto.",
                    user_text=text,
                )
                return
            if not _is_greeting(text):
                session.user_context = combined_context
            self._reply_to_user(
                session,
                text,
                on_chat_delta=on_chat_delta,
                local_fallback=(
                    "Entiendo. Puedo ayudarte a investigar eso. Para revisar "
                    "el código y mostrar evidencias concretas, comparte la "
                    "ruta local de la carpeta del proyecto. El contexto "
                    "compartido queda como no verificado; no he leído "
                    "archivos todavía."
                ),
            )
            return
        session.scope_root = candidate
        session.ask(
            f"Carpeta registrada: {candidate}. Empezaré con archivos "
            "permitidos en modo de solo lectura. Los secretos y archivos "
            "confidenciales se omiten hasta que autorices su acceso.",
            user_text=text,
        )
        session.next_action(
            kind="list",
            caption="Listar archivos de la carpeta",
            hint=(
                "Solo nombres y presencia (sin leer contenido) de los "
                "archivos permitidos."
            ),
        )

    def _reply_to_user(
        self,
        session: ChatSession,
        text: str,
        *,
        local_fallback: str,
        on_chat_delta: Callable[[str], None] | None = None,
    ) -> None:
        session.messages.append(ChatMessage(author="user", text=text))
        brief = _session_brief(session)
        if self._chat_responder is None:
            reply = _local_brief_reply(session) or local_fallback
            session.messages.append(ChatMessage(author="agent", text=reply))
            if on_chat_delta is not None:
                on_chat_delta(reply)
            return

        conversation = (
            session.messages[1:]
            if session.messages and session.messages[0].author == "agent"
            else session.messages
        )
        turns = tuple(
            ChatTurn(
                role="user" if message.author == "user" else "assistant",
                content=message.text,
            )
            for message in conversation[-12:]
            if message.author in {"user", "agent"}
        )
        try:
            if on_chat_delta is None:
                response = self._chat_responder.respond(
                    turns,
                    timeout_seconds=_CHAT_TIMEOUT_SECONDS,
                    context=brief,
                )
            elif isinstance(self._chat_responder, StreamingChatResponder):
                response = self._chat_responder.stream_respond(
                    turns,
                    timeout_seconds=_CHAT_TIMEOUT_SECONDS,
                    on_delta=on_chat_delta,
                    context=brief,
                )
            else:
                response = self._chat_responder.respond(
                    turns,
                    timeout_seconds=_CHAT_TIMEOUT_SECONDS,
                    context=brief,
                )
                on_chat_delta(response.content)
        except RuntimeError as error:
            provider_hint = (
                "Revisa tu conexión, la clave y el modelo configurado."
                if self._provider_label == "openrouter"
                else "Verifica que LM Studio esté abierto y tenga un "
                "modelo Qwen cargado."
            )
            session.messages.append(
                ChatMessage(
                    author="agent",
                    text=(
                        f"No pude consultar el modelo ({error}). "
                        f"{provider_hint} Puedes continuar compartiendo la "
                        "ruta del proyecto."
                    ),
                )
            )
            return
        session.chat_provider = response.provider
        session.chat_model = response.model
        session.messages.append(ChatMessage(author="agent", text=response.content))

    def _session_record(
        self, session_id: str
    ) -> tuple[ChatSession, threading.RLock]:
        """Obtiene el estado y su lock sin bloquear otras sesiones durante IA."""

        with self._lock:
            session = self._sessions.get(session_id)
            session_lock = self._session_locks.get(session_id)
        if session is None or session_lock is None:
            raise UnknownSessionError(
                f"no existe una sesión con id {session_id}"
            )
        return session, session_lock

    @staticmethod
    def _snapshot(session: ChatSession) -> ChatSession:
        """Aísla las respuestas del API del estado mutable interno."""

        return deepcopy(session)


# -- funciones de módulo (plantillas locales, sin mutación de sesión) -------


@dataclass(frozen=True, slots=True)
class _SessionCounts:
    evidence: int
    findings: int
    docs: int
    patches: int
    ready: int
    failures: int

    @classmethod
    def capture(cls, session: ChatSession) -> "_SessionCounts":
        return cls(
            evidence=len(session.evidence),
            findings=len(session.findings),
            docs=len(session.docs),
            patches=len(session.patches),
            ready=len(session.ready_items),
            failures=_failure_count(session),
        )


def _failure_count(session: ChatSession) -> int:
    return sum(1 for item in session.ready_items if item.kind == "agent_failure")


def _counted(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _truncate(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _activity_detail(
    session: ChatSession, before: _SessionCounts, status: str
) -> str:
    """Resumen breve y verificable de lo que produjo la etapa."""

    if status == "fallida":
        failures = [
            item
            for item in session.ready_items[before.ready :]
            if item.kind == "agent_failure"
        ]
        reason = failures[-1].detail if failures else "la etapa no se completó"
        return f"No completado: {_truncate(reason, 160)}"

    parts: list[str] = []
    evidence = len(session.evidence) - before.evidence
    findings = len(session.findings) - before.findings
    docs = len(session.docs) - before.docs
    patches = len(session.patches) - before.patches
    if evidence:
        parts.append(_counted(evidence, "evidencia", "evidencias"))
    if findings:
        parts.append(_counted(findings, "hallazgo", "hallazgos"))
    if docs:
        parts.append(_counted(docs, "sección", "secciones"))
    if patches:
        parts.append(_counted(patches, "propuesta", "propuestas"))
    if not parts:
        new_ready = session.ready_items[before.ready :]
        if new_ready:
            parts.append(new_ready[-1].title)
        elif session.state == "bloqueada":
            parts.append("revisión detenida")
        else:
            parts.append("sin resultados nuevos")
    return " · ".join(parts[:3])


def _activity_origin(
    session: ChatSession, before: _SessionCounts, status: str
) -> str:
    new_ready = session.ready_items[before.ready :]
    if new_ready:
        return new_ready[-1].origin
    if status == "fallida":
        return "fallo declarado"
    return "ejecución local (sin IA)"


def _status_line(session: ChatSession) -> str:
    return (
        f"Estado: {session.state}; {len(session.findings)} hallazgo(s), "
        f"{len(session.docs)} sección(es) de docs, {len(session.patches)} "
        "parche(s) propuesto(s). Si hay una solicitud de permiso pendiente, "
        "respóndela en la tarjeta de la conversación."
    )


def _looks_like_path(text: str) -> bool:
    return (
        text.startswith(("./", "../", ".\\", "..\\", "~/", "~\\"))
        or text.startswith("/")
        or (len(text) >= 3 and text[1:3] in (":\\", ":/"))
        or text.startswith(("\\\\", "//"))
    )


def _is_greeting(text: str) -> bool:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    normalized = " ".join(
        "".join(
            character if character.isalnum() else " "
            for character in normalized
        ).split()
    )
    if normalized in {
        "hola",
        "hola audiai",
        "buen dia",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "como estas",
        "que tal",
        "hey",
        "hi",
    }:
        return True
    greeting_prefixes = (
        "hola ",
        "buen dia ",
        "buenos dias ",
        "buenas tardes ",
        "buenas noches ",
        "hey ",
        "hi ",
    )
    return normalized.startswith(greeting_prefixes) and any(
        phrase in normalized
        for phrase in (
            "como estas",
            "que puedes hacer",
            "que haces",
            "puedes ayudarme",
            "quien eres",
            "que es audiai",
            "como funciona",
        )
    )


def _is_readable_source(relative_path: str) -> bool:
    file_name = relative_path.rsplit("/", 1)[-1].lower()
    suffix = "." + file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    return suffix in _SOURCE_SUFFIXES or file_name in _SOURCE_FILE_NAMES


def _role_instruction(base: str, user_context: str) -> str:
    if not user_context:
        return base
    return (
        base
        + "\n\nCONTEXTO APORTADO POR EL USUARIO (no verificado; no citar como "
        "evidencia de archivo):\n"
        + user_context
    )


def _attribution(origin: str, provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"origen={origin} ({provider}/{model})"
    return f"origen={origin}"


def _short_names(evidence: list[Evidence]) -> str:
    names = [item.source_path for item in evidence][:6]
    text = ", ".join(names)
    if len(evidence) > 6:
        text += ", …"
    return text


def _docs_from_role(
    session: ChatSession, result: RoleResult, omitted: int
) -> list[DocSection]:
    """Proyecta el informe real, conservando solo propuestas con evidencia local."""

    available = {item.evidence_id for item in session.evidence}
    proposals = [
        proposal
        for proposal in result.proposals
        if proposal.evidence_ids
        and set(proposal.evidence_ids).issubset(available)
    ]
    if not proposals:
        return []

    origin = _attribution(result.origin.value, result.provider, result.model)
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id
            for proposal in proposals
            for evidence_id in proposal.evidence_ids
        )
    )
    sections = [
        DocSection(
            title="Informe del especialista",
            body=(
                result.summary
                + f" Fragmentos omitidos por límite de contexto: {omitted}. "
                "Toda conclusión queda sujeta a revisión humana."
            ),
            origin=origin,
            evidence_ids=evidence_ids,
        )
    ]
    for proposal in proposals:
        body = proposal.explanation
        if proposal.uncertainty:
            body += f" Incertidumbre: {proposal.uncertainty}"
        sections.append(
            DocSection(
                title=proposal.title,
                body=body,
                origin=origin,
                evidence_ids=proposal.evidence_ids,
            )
        )
    return sections


def _docs_from_findings(session: ChatSession) -> list[DocSection]:
    """Plantilla local: resume SOLO hallazgos ya producidos en esta sesión."""

    evidence_index = {item.evidence_id: item for item in session.evidence}
    counts = {"all": len(session.findings)}
    for finding in session.findings:
        counts[finding.origin.value] = counts.get(finding.origin.value, 0) + 1

    intro = DocSection(
        title="Resumen de la revisión",
        body=(
            f"Carpeta: {session.scope_root}. {counts['all']} hallazgo(s), "
            f"de los cuales {counts.get('model', 0)} provienen del análisis "
            "de rol y el resto de comprobaciones deterministas. "
            "Coincidencia no implica explotabilidad: cada hallazgo pide "
            "revisión humana antes de actuar."
        ),
        origin=_TEMPLATE_ORIGIN,
    )
    sections = [intro]

    for finding in session.findings:
        evidence_lines = []
        for evidence_id in finding.evidence_ids:
            item = evidence_index.get(evidence_id)
            if item is None:
                continue
            location = item.source_path
            if item.line_start:
                location += f":{item.line_start}"
            evidence_lines.append(f"{location} ({item.tool})")
        body = (
            "Qué: " + finding.explanation + " "
            f"Severidad: {finding.severity.value}. "
            "Dónde está la prueba: "
            + (
                "; ".join(evidence_lines)
                if evidence_lines
                else "sin evidencia citada (hipótesis)"
            )
            + ". "
            "Análisis: "
            + ("modelo" if finding.origin.value == "model" else "determinista")
            + "."
        )
        if finding.uncertainty:
            body += f" Incertidumbre: {finding.uncertainty}"
        sections.append(
            DocSection(
                title=finding.title,
                body=body,
                origin=_TEMPLATE_ORIGIN,
                evidence_ids=finding.evidence_ids,
            )
        )
    return sections


_PATCH_RECOMMENDATIONS: tuple[tuple[str, str], ...] = (
    (
        "Ejecución dinámica",
        "Remplaza eval(…) por un parser explícito (por ejemplo "
        "ast.literal_eval) y valida el origen del dato antes de "
        "interpretarlo como código. Si el dato puede venir del exterior, "
        "elimina la ejecución dinámica por completo.",
    ),
    (
        "Credencial embebida",
        "Mueve el valor a una variable de entorno externa al repositorio "
        "(p. ej. os.environ[\"...\"]) y rota la credencial expuesta: no "
        "sirve descomentarla, está en el historial de versiones.",
    ),
)

_PATCH_DEFAULT_RECOMMENDATION = (
    "Plantea una mitigación para este hallazgo tras la revisión humana; "
    "el parche va como texto indicativo, nunca se aplica automáticamente."
)


def _patches_from_findings(session: ChatSession) -> list[PatchItem]:
    """Plantilla local: recomendación por hallazgo; diff no inventado."""

    patches: list[PatchItem] = []
    for index, finding in enumerate(session.findings, start=1):
        text = f"{finding.title} {finding.explanation}".lower()
        description = _PATCH_DEFAULT_RECOMMENDATION
        for needle, recommendation in _PATCH_RECOMMENDATIONS:
            if needle.lower() in text:
                description = recommendation
                break
        patches.append(
            PatchItem(
                patch_id=f"pat-{index:03d}",
                finding_id=finding.finding_id,
                title=f"Parche para: {finding.title}",
                description=description,
                diff=None,  # el diff real exige ver el archivo completo: no se inventa
                origin=_TEMPLATE_ORIGIN,
                evidence_ids=finding.evidence_ids,
            )
        )
    return patches


def _patches_from_role(session: ChatSession, result) -> list[PatchItem]:
    """Acepta sugerencias del rol solo si citan evidencia de esta sesión."""

    available = {item.evidence_id for item in session.evidence}
    patches: list[PatchItem] = []
    for proposal in result.proposals:
        if not proposal.evidence_ids or not set(proposal.evidence_ids) <= available:
            continue
        finding = next(
            (
                item
                for item in session.findings
                if set(item.evidence_ids).intersection(proposal.evidence_ids)
            ),
            None,
        )
        if finding is None:
            continue
        patches.append(
            PatchItem(
                patch_id=f"pat-{len(session.patches) + len(patches) + 1:03d}",
                finding_id=finding.finding_id,
                title=proposal.title,
                description=proposal.explanation,
                diff=None,
                origin=_attribution(
                    result.origin.value, result.provider, result.model
                ),
                evidence_ids=proposal.evidence_ids,
            )
        )
    return patches

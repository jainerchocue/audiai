"""Contrato de roles (punto de extensión para subagentes).

QUÉ ES ESTO
-----------
AudiAI delega tareas acotadas a "roles": investigar causas y riesgos,
investigar en web, redactar documentación, revisar documentación y proponer
parches. Este módulo define **solo el contrato** entre el pipeline y quien
ejecute esos roles. La implementación (un subagente, un script, un modelo
local, un servicio externo) vive fuera del dominio.

NO IMPLEMENTADO AQUÍ
--------------------
Este módulo no ejecuta nada, no llama a ningún modelo, no accede a la red ni
al sistema de archivos. Es una frontera.

REGLAS QUE LA IMPLEMENTACIÓN DEBE RESPETAR
-------------------------------------------
1. Recibe contexto **ya acotado** por el harness (``RoleRequest.context``).
   No abre archivos por su cuenta ni amplía el alcance.
2. No decide permisos, presupuestos ni qué evidencia es válida: eso es del
   harness. Si un rol necesita más contexto, lo declara y falla.
3. No escribe en el proyecto auditado. Un parche se devuelve como texto en
   ``PatchSuggestion`` y jamás se aplica desde el dominio.
4. No puede declarar red por sí mismo: ``allow_network`` solo se autoriza en
   ``RoleName.WEB_RESEARCH`` y debe venir concedido por el harness.
5. Devuelve ``ResultOrigin.MOCK`` cuando el resultado sea simulado. Es
   obligatorio: un rol simulado no puede declarar ``provider`` ni ``model``.
6. Lo que devuelve son **propuestas**, no hallazgos confirmados. Convertir una
   propuesta en hallazgo exige evidencia validada por código determinista.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, Protocol, runtime_checkable

from app.domain.origin import ResultOrigin
from app.domain.severity import Severity

MAX_INSTRUCTION_CHARS = 4_000
MAX_PATCH_DIFF_CHARS = 20_000


class RoleError(ValueError):
    """El contrato de rol se usó de forma inválida."""


class RoleNotConfigured(RuntimeError):
    """No hay ejecutor conectado para el rol solicitado."""


class RoleName(str, Enum):
    CAUSE_ANALYSIS = "cause_analysis"
    RISK_RESEARCH = "risk_research"
    WEB_RESEARCH = "web_research"
    DOCUMENTATION = "documentation"
    DOC_REVIEW = "doc_review"
    PATCH_SUGGESTION = "patch_suggestion"


class RoleOutcome(str, Enum):
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RoleLimits:
    """Presupuesto que el harness concede al rol. El rol no lo amplía."""

    max_context_bytes: int = 32_000
    max_model_calls: int = 1
    timeout_seconds: float = 60.0
    allow_network: bool = False

    def __post_init__(self) -> None:
        if self.max_context_bytes <= 0:
            raise RoleError("max_context_bytes debe ser positivo")
        if self.max_model_calls <= 0:
            raise RoleError("max_model_calls debe ser positivo")
        if self.timeout_seconds <= 0:
            raise RoleError("timeout_seconds debe ser positivo")


@dataclass(frozen=True, slots=True)
class ContextItem:
    """Fragmento ya seleccionado y acotado por el harness.

    El rol no recibe rutas ni acceso al sistema de archivos: recibe texto con
    la referencia de evidencia a la que pertenece.
    """

    evidence_id: str
    snippet: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id.strip():
            raise RoleError("evidence_id no puede estar vacío")
        if not isinstance(self.snippet, str):
            raise RoleError("snippet debe ser texto")

    @property
    def size(self) -> int:
        return len(self.snippet.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class RoleRequest:
    """Petición de trabajo a un rol."""

    role: RoleName
    instruction: str
    context: tuple[ContextItem, ...] = ()
    limits: RoleLimits = field(default_factory=RoleLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.role, RoleName):
            raise RoleError("role debe ser un RoleName")
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise RoleError("instruction no puede estar vacía")
        if len(self.instruction) > MAX_INSTRUCTION_CHARS:
            raise RoleError(
                f"instruction supera {MAX_INSTRUCTION_CHARS} caracteres"
            )
        if not isinstance(self.context, tuple):
            raise RoleError("context debe ser una tupla de ContextItem")
        if any(not isinstance(item, ContextItem) for item in self.context):
            raise RoleError("context solo admite ContextItem")

        total = sum(item.size for item in self.context)
        if total > self.limits.max_context_bytes:
            raise RoleError(
                f"el contexto ocupa {total} bytes y el límite es "
                f"{self.limits.max_context_bytes}"
            )

        if self.limits.allow_network and self.role is not RoleName.WEB_RESEARCH:
            raise RoleError(
                "solo web_research puede ejecutarse con acceso a red; "
                "el resto de roles trabaja con el contexto entregado"
            )

    @property
    def context_bytes(self) -> int:
        return sum(item.size for item in self.context)


@dataclass(frozen=True, slots=True)
class PatchSuggestion:
    """Parche propuesto como texto.

    No tiene método de aplicación a propósito: el dominio no escribe en el
    proyecto auditado. Se muestra al usuario y él decide.
    """

    description: str
    diff: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise RoleError("la descripción del parche no puede estar vacía")
        if self.diff is not None:
            if not isinstance(self.diff, str) or not self.diff.strip():
                raise RoleError("diff, si se incluye, no puede estar vacío")
            if len(self.diff) > MAX_PATCH_DIFF_CHARS:
                raise RoleError(
                    f"diff supera {MAX_PATCH_DIFF_CHARS} caracteres"
                )


@dataclass(frozen=True, slots=True)
class FindingProposal:
    """Propuesta de hallazgo emitida por un rol. No es un hallazgo confirmado.

    Sin ``evidence_ids`` no hay confirmación posible: en ese caso la propuesta
    debe declarar ``uncertainty`` y se presenta aparte como hipótesis.
    """

    title: str
    explanation: str
    severity: Severity = Severity.UNKNOWN
    evidence_ids: tuple[str, ...] = ()
    uncertainty: str | None = None
    patch: PatchSuggestion | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise RoleError("title no puede estar vacío")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise RoleError("explanation no puede estar vacía")
        if not isinstance(self.severity, Severity):
            raise RoleError("severity debe ser un Severity")
        if not isinstance(self.evidence_ids, tuple):
            raise RoleError("evidence_ids debe ser una tupla")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise RoleError("evidence_ids no admite entradas vacías")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise RoleError("evidence_ids no admite duplicados")

        if not self.evidence_ids and not _has_text(self.uncertainty):
            raise RoleError(
                "una propuesta sin evidencia debe declarar uncertainty: "
                "no se presenta una hipótesis como hallazgo"
            )

        if self.patch is not None and not isinstance(self.patch, PatchSuggestion):
            raise RoleError("patch debe ser un PatchSuggestion")

    @property
    def is_hypothesis(self) -> bool:
        return not self.evidence_ids


@dataclass(frozen=True, slots=True)
class RoleResult:
    """Respuesta de un rol."""

    role: RoleName
    outcome: RoleOutcome
    summary: str = ""
    origin: ResultOrigin = ResultOrigin.MOCK
    proposals: tuple[FindingProposal, ...] = ()
    citations: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.role, RoleName):
            raise RoleError("role debe ser un RoleName")
        if not isinstance(self.outcome, RoleOutcome):
            raise RoleError("outcome debe ser un RoleOutcome")

        if self.outcome is RoleOutcome.COMPLETED:
            if not _has_text(self.summary):
                raise RoleError("un rol completado debe incluir summary")
        if self.outcome is RoleOutcome.FAILED and not _has_text(self.error):
            raise RoleError("outcome=failed exige error")
        if self.outcome is not RoleOutcome.FAILED and self.error is not None:
            raise RoleError("error solo se permite cuando outcome=failed")

        if self.outcome is RoleOutcome.REFUSED and self.proposals:
            raise RoleError("un rol que se niega no puede emitir propuestas")
        if self.outcome is not RoleOutcome.COMPLETED and self.proposals:
            raise RoleError("solo un rol completado puede emitir propuestas")

        if any(not isinstance(p, FindingProposal) for p in self.proposals):
            raise RoleError("proposals solo admite FindingProposal")

        if self.origin is ResultOrigin.REAL:
            if not _has_text(self.provider) or not _has_text(self.model):
                raise RoleError("origin=real exige provider y model")
        elif self.provider is not None or self.model is not None:
            raise RoleError(
                "origin=mock no puede declarar provider ni model: "
                "una simulación no se atribuye a un modelo real"
            )

        if any(not _has_text(c) for c in self.citations):
            raise RoleError("citations no admite entradas vacías")
        if any(not _has_text(item) for item in self.limitations):
            raise RoleError("limitations no admite entradas vacías")

    @property
    def is_simulated(self) -> bool:
        return self.origin is ResultOrigin.MOCK


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Un turno acotado de conversación que no implica auditar archivos."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Respuesta conversacional identificada por proveedor y modelo."""

    content: str
    provider: str
    model: str


@runtime_checkable
class ChatResponder(Protocol):
    """Puerto opcional para conversación natural fuera de roles de auditoría."""

    def respond(
        self,
        turns: tuple[ChatTurn, ...],
        *,
        timeout_seconds: float,
    ) -> ChatResponse:
        ...


@runtime_checkable
class StreamingChatResponder(ChatResponder, Protocol):
    """Puerto opcional para entregar texto a medida que lo genera el modelo."""

    def stream_respond(
        self,
        turns: tuple[ChatTurn, ...],
        *,
        timeout_seconds: float,
        on_delta: Callable[[str], None],
    ) -> ChatResponse:
        ...


@runtime_checkable
class RoleExecutor(Protocol):
    """Puerto que debe cumplir cualquier ejecutor de roles.

    Implementaciones fuera del dominio: adaptador de subagente, script local o
    servicio externo. El harness entrega la petición ya acotada y valida la
    respuesta antes de usarla.
    """

    def run(self, request: RoleRequest) -> RoleResult:
        ...


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())

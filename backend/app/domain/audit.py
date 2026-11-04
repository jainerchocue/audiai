"""Entidad Auditoría, sus estados y la procedencia del resultado.

Responsabilidad de la Fase 2: representar una auditoría y las reglas que la
hacen coherente, sin transporte, sin base de datos y sin modelos de IA.

Decisiones de diseño:

- Dataclasses congeladas en lugar de clases mutables: una auditoría es un
  valor; los cambios se expresan creando una nueva versión con ``transition``.
- Sin Pydantic aquí. El contrato de transporte vive en ``api``; el dominio no
  debe depender de una librería de serialización para expresar sus reglas.
- ``scope_root`` se guarda como texto sin resolver. La resolución y validación
  de rutas reales pertenece a la herramienta de lectura (Fase 3), no al dominio.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from app.domain.identifiers import IDENTIFIER_RULE, is_identifier
from app.domain.origin import ResultOrigin

__all__ = [
    "Audit",
    "AuditError",
    "AuditStatus",
    "InvalidTransition",
    "ResultOrigin",
    "utcnow",
]


class AuditError(ValueError):
    """Error de regla de dominio en una auditoría."""


class InvalidTransition(AuditError):
    """El cambio de estado solicitado no está permitido."""


class AuditStatus(str, Enum):
    """Estados del ciclo de vida de una auditoría."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {AuditStatus.COMPLETED, AuditStatus.FAILED, AuditStatus.CANCELLED}
)

_ALLOWED_TRANSITIONS: dict[AuditStatus, frozenset[AuditStatus]] = {
    AuditStatus.PENDING: frozenset({AuditStatus.RUNNING, AuditStatus.CANCELLED}),
    AuditStatus.RUNNING: frozenset(
        {AuditStatus.COMPLETED, AuditStatus.FAILED, AuditStatus.CANCELLED}
    ),
    AuditStatus.COMPLETED: frozenset(),
    AuditStatus.FAILED: frozenset(),
    AuditStatus.CANCELLED: frozenset(),
}


def utcnow() -> datetime:
    """Reloj del dominio; inyectable en pruebas."""

    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Audit:
    """Una auditoría autorizada sobre un proyecto local.

    Invariantes verificados al construir y al cambiar de estado:

    - ``audit_id`` es estable, minúsculo y apto para usar en rutas y URLs.
    - Las marcas de tiempo son conscientes de zona y no retroceden.
    - ``origin=real`` exige proveedor y modelo; ``origin=mock`` los prohíbe,
      para que una simulación nunca pueda atribuirse a un modelo real.
    - ``failed`` exige motivo; cualquier otro estado lo prohíbe.
    """

    audit_id: str
    scope_root: str
    status: AuditStatus = AuditStatus.PENDING
    origin: ResultOrigin = ResultOrigin.MOCK
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    provider: str | None = None
    model: str | None = None
    failure_reason: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.audit_id)
        _validate_scope(self.scope_root)
        _validate_timestamps(self.created_at, self.updated_at)
        _validate_provenance(self.origin, self.provider, self.model)
        _validate_failure_state(self.status, self.failure_reason)
        _validate_limitations(self.limitations)

    # -- constructores ---------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        audit_id: str,
        scope_root: str,
        origin: ResultOrigin = ResultOrigin.MOCK,
        provider: str | None = None,
        model: str | None = None,
        limitations: tuple[str, ...] = (),
        clock: Callable[[], datetime] = utcnow,
    ) -> "Audit":
        """Crea una auditoría en ``pending`` con marcas de tiempo coherentes."""

        moment = clock()
        return cls(
            audit_id=audit_id,
            scope_root=scope_root,
            status=AuditStatus.PENDING,
            origin=origin,
            created_at=moment,
            updated_at=moment,
            provider=provider,
            model=model,
            limitations=limitations,
        )

    # -- transiciones ----------------------------------------------------

    def allowed_transitions(self) -> frozenset[AuditStatus]:
        return _ALLOWED_TRANSITIONS[self.status]

    def transition(
        self,
        new_status: AuditStatus,
        *,
        failure_reason: str | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> "Audit":
        """Devuelve una nueva versión de la auditoría en ``new_status``."""

        if new_status is self.status:
            raise InvalidTransition(
                f"la auditoría ya está en estado {self.status.value}"
            )

        allowed = self.allowed_transitions()
        if new_status not in allowed:
            permitted = ", ".join(sorted(s.value for s in allowed)) or "ninguno"
            raise InvalidTransition(
                f"no se puede pasar de {self.status.value} a {new_status.value}; "
                f"transiciones permitidas: {permitted}"
            )

        moment = clock()
        if moment < self.updated_at:
            raise AuditError(
                "la marca de tiempo de la transición no puede ser anterior a updated_at"
            )

        return replace(
            self,
            status=new_status,
            updated_at=moment,
            failure_reason=failure_reason,
        )


# -- validaciones --------------------------------------------------------


def _validate_id(audit_id: str) -> None:
    if not is_identifier(audit_id):
        raise AuditError(f"audit_id {IDENTIFIER_RULE}")


def _validate_scope(scope_root: str) -> None:
    if not isinstance(scope_root, str) or not scope_root.strip():
        raise AuditError("scope_root no puede estar vacío")


def _validate_timestamps(created_at: datetime, updated_at: datetime) -> None:
    for name, value in (("created_at", created_at), ("updated_at", updated_at)):
        if not isinstance(value, datetime):
            raise AuditError(f"{name} debe ser un datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise AuditError(f"{name} debe incluir zona horaria")
    if updated_at < created_at:
        raise AuditError("updated_at no puede ser anterior a created_at")


def _validate_provenance(
    origin: ResultOrigin, provider: str | None, model: str | None
) -> None:
    if not isinstance(origin, ResultOrigin):
        raise AuditError("origin debe ser un ResultOrigin")

    if origin is ResultOrigin.REAL:
        if not _is_blank_free(provider) or not _is_blank_free(model):
            raise AuditError("origin=real exige provider y model identificables")
    elif provider is not None or model is not None:
        raise AuditError(
            "origin=mock no puede declarar provider ni model: "
            "una simulación no se atribuye a un modelo real"
        )


def _validate_failure_state(status: AuditStatus, failure_reason: str | None) -> None:
    if not isinstance(status, AuditStatus):
        raise AuditError("status debe ser un AuditStatus")

    if status is AuditStatus.FAILED and not _is_blank_free(failure_reason):
        raise AuditError("status=failed exige failure_reason")
    if status is not AuditStatus.FAILED and failure_reason is not None:
        raise AuditError("failure_reason solo se permite cuando status=failed")


def _validate_limitations(limitations: tuple[str, ...]) -> None:
    if not isinstance(limitations, tuple):
        raise AuditError("limitations debe ser una tupla de textos")
    if any(not _is_blank_free(item) for item in limitations):
        raise AuditError("limitations no admite entradas vacías")


def _is_blank_free(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())

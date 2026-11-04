"""Servicio de auditorías (Fases 6 y 7): caso de uso completo sobre el harness.

Coordina el dominio (estados de Auditoría) con el harness (recorrido fijo) y
devuelve un resultado auditable. No implementa análisis propio: solo coordina
lo que ya existe, respetando la dependencia deseada del README:
API → servicios → dominio y contratos.

Fase 7 añade la gestión de runs terminados: almacenamiento en memoria para la
demo (se pierde al reiniciar, cambio de contrato en Fase 9 con SQLite),
creación con identificador único y recuperación de estado/informe.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from app.domain.audit import Audit, AuditError, AuditStatus, ResultOrigin
from app.domain.evidence import Evidence
from app.domain.finding import Finding
from app.domain.roles import RoleExecutor
from app.harness.pipeline import (
    AuditHarness,
    DEFAULT_PIPELINE_LIMITS,
    PipelineLimits,
    PipelineStatus,
    StepRecord,
)

MOCK_LIMITATIONS: tuple[str, ...] = (
    "análisis por proveedor simulado (mock); ninguna secuencia fue evaluada por un modelo real",
    "comprobaciones deterministas de patrón; coincidencia no implica explotabilidad",
)

REAL_LIMITATIONS: tuple[str, ...] = (
    "análisis por subagente real vía Strands/OpenRouter; el contexto entregado se comparte con el proveedor",
    "comprobaciones deterministas de patrón; coincidencia no implica explotabilidad",
)


@dataclass(frozen=True, slots=True)
class AuditRun:
    """Resultado completo y trazable de una auditoría."""

    audit: Audit
    evidence: tuple[Evidence, ...]
    findings: tuple[Finding, ...]
    trace: tuple[StepRecord, ...]

    def __post_init__(self) -> None:
        if not self.audit.status.is_terminal:
            raise AuditError("un AuditRun solo contiene auditorías terminadas")
        available = {item.evidence_id for item in self.evidence}
        for finding in self.findings:
            if not set(finding.evidence_ids).issubset(available):
                raise AuditError(
                    "un hallazgo del run cita evidencias que el run no produjo"
                )


class AuditService:
    """Ejecuta una auditoría completa y devuelve su resultado terminado."""

    def __init__(
        self,
        roles: RoleExecutor,
        *,
        limits: PipelineLimits | None = None,
        timer: Callable[[], float] = time.monotonic,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._roles = roles
        self._limits = limits
        self._timer = timer
        self._should_stop = should_stop

    def execute(self, *, audit_id: str, scope_root: Path) -> AuditRun:
        """Crea la auditoría, la ejecuta y la deja en estado terminal."""

        audit = Audit.create(
            audit_id=audit_id,
            scope_root=str(scope_root),
            limitations=MOCK_LIMITATIONS,
        )
        audit = audit.transition(AuditStatus.RUNNING)

        harness = AuditHarness(
            scope_root,
            self._roles,
            limits=self._limits,
            clock=self._timer,
            should_stop=self._should_stop,
        )
        outcome = harness.execute()

        final = self._finalize(audit, outcome)
        return AuditRun(
            audit=final,
            evidence=outcome.evidence,
            findings=outcome.findings,
            trace=outcome.trace,
        )

    @staticmethod
    def _finalize(audit: Audit, outcome) -> Audit:
        """Atribuye el resultado a su procedencia real o simulada.

        Si el análisis corrió con un proveedor real, la auditoría se
        re-atribuye (origin, provider, model, limitaciones): un resultado
        simulado jamás se presenta como real ni al revés.
        """

        if outcome.analysis_origin is ResultOrigin.REAL:
            audit = replace(
                audit,
                origin=ResultOrigin.REAL,
                provider=outcome.analysis_provider,
                model=outcome.analysis_model,
                limitations=REAL_LIMITATIONS,
            )

        if outcome.status is PipelineStatus.COMPLETED:
            return audit.transition(AuditStatus.COMPLETED)
        if outcome.status is PipelineStatus.CANCELLED:
            return audit.transition(AuditStatus.CANCELLED)
        return audit.transition(
            AuditStatus.FAILED, failure_reason=outcome.reason or "motivo no declarado"
        )


# -- Fase 7: auditorías gestionadas (crear, consultar, recuperar) ----------


class ManagedAuditError(RuntimeError):
    """Error de gestión de auditorías: identificador duplicado o desconocido."""


class UnknownAuditError(ManagedAuditError):
    """Nadie registró una auditoría con ese identificador."""


class DuplicateAuditError(ManagedAuditError):
    """Ya existe una auditoría terminada con ese identificador."""


class AuditStore(Protocol):
    """Contrato mínimo de almacenamiento de runs terminados (Fase 9 lo cambia)."""

    def save(self, run: AuditRun) -> None: ...

    def load(self, audit_id: str) -> AuditRun | None: ...

    def load_all(self) -> tuple[AuditRun, ...]: ...


class InMemoryAuditStore:
    """Almacenamiento en memoria para demo y pruebas.

    Se pierde al reiniciar el proceso: es explícito y deseado en Fase 7. No
    hay hilo de escritura a disco ni escritura dentro del proyecto auditado.
    El lock protege contra ejecuciones concurrentes sobre el mismo store.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, AuditRun] = {}

    def save(self, run: AuditRun) -> None:
        with self._lock:
            existing = self._runs.get(run.audit.audit_id)
            if existing is not None:
                raise DuplicateAuditError(
                    f"ya existe una auditoría con id {run.audit.audit_id}"
                )
            self._runs[run.audit.audit_id] = run

    def load(self, audit_id: str) -> AuditRun | None:
        with self._lock:
            return self._runs.get(audit_id)

    def load_all(self) -> tuple[AuditRun, ...]:
        with self._lock:
            return tuple(self._runs[audit_id] for audit_id in sorted(self._runs))


class ManagedAuditRunner:
    """Caso de uso de Fase 7: crear, consultar estado y recuperar resultado.

    Ejecución **sincrónica y limitada**: una petición POST corre el recorrido
    completo con presupuesto de demo y guarda el run terminado. No hay colas
    ni procesos nuevos: apropiado para la demo, no para producción.
    """

    def __init__(
        self,
        roles: RoleExecutor,
        store: AuditStore,
        *,
        limits: PipelineLimits = DEFAULT_PIPELINE_LIMITS,
        timer: Callable[[], float] = time.monotonic,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._roles = roles
        self._store = store
        self._limits = limits
        self._timer = timer
        self._should_stop = should_stop

    def run_new(self, *, audit_id: str, scope_root: Path) -> AuditRun:
        """Ejecuta una auditoría nueva y la almacena; falla si el id existe."""

        existing = self._store.load(audit_id)
        if existing is not None:
            raise DuplicateAuditError(
                f"ya existe una auditoría con id {audit_id}; "
                "las auditorías terminadas no se reescriben"
            )
        run = AuditService(
            self._roles,
            limits=self._limits,
            timer=self._timer,
            should_stop=self._should_stop,
        ).execute(audit_id=audit_id, scope_root=scope_root)
        self._store.save(run)
        return run

    def status(self, audit_id: str) -> Audit:
        """Estado de una auditoría registrada, o ``UnknownAuditError``."""

        run = self._store.load(audit_id)
        if run is None:
            raise UnknownAuditError(f"no hay auditoría registrada con id {audit_id}")
        return run.audit

    def report(self, audit_id: str) -> AuditRun:
        """El run completo (hallazgos, evidencias, trace) de una auditoría."""

        run = self._store.load(audit_id)
        if run is None:
            raise UnknownAuditError(f"no hay auditoría registrada con id {audit_id}")
        return run

    def list_ids(self) -> tuple[str, ...]:
        return tuple(run.audit.audit_id for run in self._store.load_all())

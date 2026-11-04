"""Reglas de la entidad Auditoría. Sin HTTP, sin BD, sin proveedores."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.audit import (
    Audit,
    AuditError,
    AuditStatus,
    InvalidTransition,
    ResultOrigin,
)

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def fixed_clock(*moments: datetime):
    """Devuelve un reloj que entrega los instantes indicados en orden."""

    remaining = list(moments)

    def clock() -> datetime:
        if not remaining:
            raise AssertionError("el reloj de prueba se quedó sin instantes")
        return remaining.pop(0)

    return clock


def new_audit(**overrides) -> Audit:
    params = {
        "audit_id": "audit-001",
        "scope_root": "fixtures/proyecto-demo",
        "clock": fixed_clock(BASE_TIME),
    }
    params.update(overrides)
    return Audit.create(**params)


# -- construcción --------------------------------------------------------


def test_create_starts_pending_and_mock() -> None:
    audit = new_audit()

    assert audit.status is AuditStatus.PENDING
    assert audit.origin is ResultOrigin.MOCK
    assert audit.created_at == audit.updated_at == BASE_TIME
    assert audit.failure_reason is None
    assert audit.limitations == ()


def test_create_accepts_a_real_origin_with_provider_and_model() -> None:
    audit = new_audit(
        origin=ResultOrigin.REAL, provider="proveedor-x", model="modelo-y"
    )

    assert audit.origin is ResultOrigin.REAL
    assert audit.provider == "proveedor-x"


@pytest.mark.parametrize("bad_id", ["", "ab", "Audit-001", "audit 001", "a" * 65])
def test_invalid_audit_id_is_rejected(bad_id: str) -> None:
    with pytest.raises(AuditError):
        new_audit(audit_id=bad_id)


@pytest.mark.parametrize("bad_scope", ["", "   "])
def test_empty_scope_is_rejected(bad_scope: str) -> None:
    with pytest.raises(AuditError):
        new_audit(scope_root=bad_scope)


def test_naive_timestamps_are_rejected() -> None:
    naive = datetime(2026, 1, 1, 12, 0)

    with pytest.raises(AuditError):
        Audit(
            audit_id="audit-001",
            scope_root="fixtures/proyecto-demo",
            created_at=naive,
            updated_at=naive,
        )


def test_updated_at_cannot_precede_created_at() -> None:
    with pytest.raises(AuditError):
        Audit(
            audit_id="audit-001",
            scope_root="fixtures/proyecto-demo",
            created_at=BASE_TIME,
            updated_at=BASE_TIME - timedelta(seconds=1),
        )


# -- procedencia ---------------------------------------------------------


def test_mock_origin_cannot_claim_a_model() -> None:
    with pytest.raises(AuditError):
        new_audit(provider="proveedor-x")

    with pytest.raises(AuditError):
        new_audit(model="modelo-y")


@pytest.mark.parametrize(
    ("provider", "model"),
    [(None, None), ("proveedor-x", None), (None, "modelo-y"), ("   ", "modelo-y")],
)
def test_real_origin_requires_provider_and_model(provider, model) -> None:
    with pytest.raises(AuditError):
        new_audit(origin=ResultOrigin.REAL, provider=provider, model=model)


# -- estado failed -------------------------------------------------------


def test_failed_requires_a_reason() -> None:
    with pytest.raises(AuditError):
        Audit(
            audit_id="audit-001",
            scope_root="fixtures/proyecto-demo",
            status=AuditStatus.FAILED,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )


def test_failure_reason_is_forbidden_in_other_states() -> None:
    with pytest.raises(AuditError):
        Audit(
            audit_id="audit-001",
            scope_root="fixtures/proyecto-demo",
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
            failure_reason="algo falló",
        )


# -- transiciones --------------------------------------------------------


def test_happy_path_pending_running_completed() -> None:
    audit = new_audit()
    running = audit.transition(
        AuditStatus.RUNNING, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
    )
    completed = running.transition(
        AuditStatus.COMPLETED, clock=fixed_clock(BASE_TIME + timedelta(seconds=2))
    )

    assert completed.status is AuditStatus.COMPLETED
    assert completed.created_at == BASE_TIME
    assert completed.updated_at == BASE_TIME + timedelta(seconds=2)
    assert completed.allowed_transitions() == frozenset()


def test_cancelled_from_pending_is_allowed() -> None:
    audit = new_audit()

    cancelled = audit.transition(
        AuditStatus.CANCELLED, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
    )

    assert cancelled.status is AuditStatus.CANCELLED


def test_failed_transition_keeps_the_reason() -> None:
    running = new_audit().transition(
        AuditStatus.RUNNING, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
    )

    failed = running.transition(
        AuditStatus.FAILED,
        failure_reason="el alcance autorizado no existe",
        clock=fixed_clock(BASE_TIME + timedelta(seconds=2)),
    )

    assert failed.status is AuditStatus.FAILED
    assert failed.failure_reason == "el alcance autorizado no existe"


def test_failed_transition_without_reason_is_rejected() -> None:
    running = new_audit().transition(
        AuditStatus.RUNNING, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
    )

    with pytest.raises(AuditError):
        running.transition(
            AuditStatus.FAILED, clock=fixed_clock(BASE_TIME + timedelta(seconds=2))
        )


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (AuditStatus.PENDING, AuditStatus.COMPLETED),
        (AuditStatus.PENDING, AuditStatus.FAILED),
        (AuditStatus.COMPLETED, AuditStatus.RUNNING),
        (AuditStatus.CANCELLED, AuditStatus.RUNNING),
        (AuditStatus.FAILED, AuditStatus.RUNNING),
    ],
)
def test_illegal_transitions_are_rejected(start, target) -> None:
    audit = Audit(
        audit_id="audit-001",
        scope_root="fixtures/proyecto-demo",
        status=start,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        failure_reason="motivo" if start is AuditStatus.FAILED else None,
    )

    with pytest.raises(InvalidTransition):
        audit.transition(
            target,
            failure_reason="motivo" if target is AuditStatus.FAILED else None,
            clock=fixed_clock(BASE_TIME + timedelta(seconds=1)),
        )


def test_transition_to_the_same_status_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        new_audit().transition(
            AuditStatus.PENDING, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
        )


def test_transition_cannot_move_time_backwards() -> None:
    running = new_audit().transition(
        AuditStatus.RUNNING, clock=fixed_clock(BASE_TIME + timedelta(seconds=5))
    )

    with pytest.raises(AuditError):
        running.transition(
            AuditStatus.COMPLETED, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
        )


def test_transition_does_not_mutate_the_original() -> None:
    audit = new_audit()
    audit.transition(
        AuditStatus.RUNNING, clock=fixed_clock(BASE_TIME + timedelta(seconds=1))
    )

    assert audit.status is AuditStatus.PENDING


# -- límites declarados --------------------------------------------------


def test_limitations_must_be_non_empty_texts() -> None:
    audit = new_audit(limitations=("sin análisis de dependencias",))

    assert audit.limitations == ("sin análisis de dependencias",)

    with pytest.raises(AuditError):
        new_audit(limitations=("   ",))

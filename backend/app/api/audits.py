"""Endpoints de auditorías (Fase 7).

Un POST ejecuta el recorrido completo de forma sincrónica y limitada: no hay
colas ni procesos nuevos. Los errores de transporte no confunden los estados
del dominio: 404 (no existe), 409 (id duplicado), 422 (raíz no válida).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.audit_schemas import (
    AuditInfoOut,
    AuditReportOut,
    CreateAuditRequest,
)
from app.services.audits import (
    DuplicateAuditError,
    ManagedAuditRunner,
    UnknownAuditError,
)

router = APIRouter(prefix="/audits", tags=["audits"])


@router.post("", status_code=201, response_model=AuditReportOut)
def create_audits(payload: CreateAuditRequest, request: Request) -> AuditReportOut:
    """Crea y ejecuta una auditoría demo limitada, sincrónica."""

    runner = _runner(request)

    audit_id = payload.audit_id
    if audit_id is not None:
        audit_id = audit_id.strip()
        if not audit_id:
            raise HTTPException(422, detail="audit_id no puede ser texto vacío")
    existing = {found: True for found in runner.list_ids()}
    if audit_id is None:
        audit_id = _pick_free_id(existing)

    scope_root = Path(payload.scope_root)
    if not scope_root.is_dir():
        raise HTTPException(
            422,
            detail="scope_root debe ser un directorio existente en la máquina "
            "donde corre AudiAI",
        )

    try:
        run = runner.run_new(audit_id=audit_id, scope_root=scope_root)
    except DuplicateAuditError as error:
        raise HTTPException(409, detail=str(error)) from error

    return AuditReportOut.from_run(run)


@router.get("", response_model=list[AuditInfoOut])
def list_audits(request: Request) -> list[AuditInfoOut]:
    """Auditorías terminadas de este proceso, en orden alfabético por id."""

    runner = _runner(request)
    return [AuditInfoOut.from_run(runner.report(audit_id)) for audit_id in runner.list_ids()]


@router.get("/{audit_id}", response_model=AuditInfoOut)
def read_audits(audit_id: str, request: Request) -> AuditInfoOut:
    """Estado, conteos y limitaciones de una auditoría registrada."""

    runner = _runner(request)
    try:
        return AuditInfoOut.from_run(runner.report(audit_id))
    except UnknownAuditError as error:
        raise HTTPException(404, detail=str(error)) from error


@router.get("/{audit_id}/report", response_model=AuditReportOut)
def read_audits_report(audit_id: str, request: Request) -> AuditReportOut:
    """El informe completo: hallazgos, evidencias y trace del recorrido."""

    runner = _runner(request)
    try:
        return AuditReportOut.from_run(runner.report(audit_id))
    except UnknownAuditError as error:
        raise HTTPException(404, detail=str(error)) from error


def _runner(request: Request) -> ManagedAuditRunner:
    """El corredor gestionado que montó ``create_app``."""

    return request.app.state.audit_runner


def _pick_free_id(taken: dict[str, bool]) -> str:
    """Primer ``audit-demo-N`` libre; determinista y legible en la demo."""

    counter = 1
    while f"audit-demo-{counter:03d}" in taken:
        counter += 1
    return f"audit-demo-{counter:03d}"

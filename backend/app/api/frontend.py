"""Rutas de los documentos y recursos locales de la interfaz."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIRECTORY = Path(__file__).resolve().parents[1] / "static"
INDEX_FILE = STATIC_DIRECTORY / "index.html"
CLASSIC_FILE = STATIC_DIRECTORY / "classic.html"

router = APIRouter(include_in_schema=False)


@router.get("/", summary="Interfaz de chat con el agente (Fase 11)")
def read_index() -> FileResponse:
    return FileResponse(
        INDEX_FILE,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/classic", summary="Interfaz clásica de una sola auditoría")
def read_classic() -> FileResponse:
    return FileResponse(
        CLASSIC_FILE,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )

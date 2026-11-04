"""Endpoint de salud.

No expone datos sensibles: ni rutas, ni configuración, ni versiones del
entorno. Solo confirma que el proceso HTTP responde.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
def read_health() -> HealthResponse:
    return HealthResponse(status="ok")

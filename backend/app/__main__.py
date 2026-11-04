"""Arranque del servicio con la configuración del entorno.

Uso: ``python -m app`` desde ``backend/``. Equivale a invocar ``uvicorn`` con
los valores de ``AUDIAI_HOST``, ``AUDIAI_PORT`` y ``AUDIAI_LOG_LEVEL``.
"""

from __future__ import annotations

import uvicorn

from app.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()

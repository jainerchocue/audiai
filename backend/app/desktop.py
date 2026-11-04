"""Arranque de AudiAI en una ventana de escritorio PyWebView."""

from __future__ import annotations

import getpass
import threading
import time
from collections.abc import Sequence
from typing import Protocol

import uvicorn

from app.config.settings import get_settings
from app.main import create_app

_SERVER_START_TIMEOUT_SECONDS = 15.0
_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class FolderDialogWindow(Protocol):
    def create_file_dialog(
        self, dialog_type: int
    ) -> str | Sequence[str] | None: ...


class DesktopBridge:
    """API mínima de escritorio para contexto local e identidad de bienvenida."""

    def __init__(self, folder_dialog: int) -> None:
        self._folder_dialog = folder_dialog
        self._window: FolderDialogWindow | None = None

    def attach_window(self, window: FolderDialogWindow) -> None:
        self._window = window

    def select_project_folder(self) -> str | None:
        if self._window is None:
            raise RuntimeError("la ventana de escritorio aún no está lista")

        selected = self._window.create_file_dialog(self._folder_dialog)
        if not selected:
            return None
        if isinstance(selected, str):
            return selected
        if isinstance(selected, Sequence):
            return str(selected[0]) if selected else None
        raise RuntimeError("el selector de carpetas devolvió un resultado inválido")

    def get_display_name(self) -> str:
        """Devuelve el nombre de cuenta local para el saludo; no se transmite."""

        return getpass.getuser().rsplit("\\", 1)[-1].strip()


def main() -> None:
    try:
        import webview
    except ImportError as error:
        raise RuntimeError(
            "PyWebView no está instalado; instala las dependencias de "
            "backend\\requirements.txt"
        ) from error

    settings = get_settings()
    bridge = DesktopBridge(webview.FileDialog.FOLDER)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=settings.port,
            log_level=settings.log_level,
        )
    )
    server_errors: list[Exception] = []

    def run_server() -> None:
        try:
            server.run()
        except Exception as error:
            server_errors.append(error)

    server_thread = threading.Thread(
        target=run_server, name="audiai-http", daemon=True
    )
    server_thread.start()
    deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
    while (
        server_thread.is_alive()
        and not server.started
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)

    if server_errors:
        raise RuntimeError("no se pudo iniciar el backend de AudiAI") from server_errors[0]
    if not server.started:
        server.should_exit = True
        server_thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        raise RuntimeError(
            f"el backend no inició en {settings.port}; revisa si el puerto está ocupado"
        )

    try:
        window = webview.create_window(
            "AudiAI",
            f"http://127.0.0.1:{settings.port}/",
            js_api=bridge,
            min_size=(960, 680),
        )
        bridge.attach_window(window)
        webview.start()
    finally:
        server.should_exit = True
        server_thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        if server_thread.is_alive():
            raise RuntimeError("el backend de AudiAI no se detuvo correctamente")
    if server_errors:
        raise RuntimeError("el backend de AudiAI terminó con un error") from server_errors[0]


if __name__ == "__main__":
    main()

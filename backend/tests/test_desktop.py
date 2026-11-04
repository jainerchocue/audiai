from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.desktop import DesktopBridge


class FakeWindow:
    def __init__(self, selection: Sequence[str] | None) -> None:
        self.selection = selection
        self.dialog_type: int | None = None

    def create_file_dialog(self, dialog_type: int) -> Sequence[str] | None:
        self.dialog_type = dialog_type
        return self.selection


def test_desktop_bridge_returns_selected_folder() -> None:
    window = FakeWindow([r"C:\projects\sample"])
    bridge = DesktopBridge(folder_dialog=3)
    bridge.attach_window(window)

    assert bridge.select_project_folder() == r"C:\projects\sample"
    assert window.dialog_type == 3


def test_desktop_bridge_returns_none_when_dialog_is_cancelled() -> None:
    bridge = DesktopBridge(folder_dialog=3)
    bridge.attach_window(FakeWindow(None))

    assert bridge.select_project_folder() is None


def test_desktop_bridge_requires_a_ready_window() -> None:
    bridge = DesktopBridge(folder_dialog=3)

    with pytest.raises(RuntimeError, match="ventana.*lista"):
        bridge.select_project_folder()


def test_desktop_bridge_returns_local_account_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.desktop.getpass.getuser", lambda: r"DEVICE\jaine")
    bridge = DesktopBridge(folder_dialog=3)

    assert bridge.get_display_name() == "jaine"

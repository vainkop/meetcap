from __future__ import annotations

import pytest

from meetcap.detect import window


def test_returns_none_on_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")
    assert window.active_window_title() is None


def test_returns_none_without_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert window.active_window_title() is None


def test_returns_title_on_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(window.subprocess, "check_output", lambda *_a, **_kw: b"Zoom Meeting\n")
    assert window.active_window_title() == "Zoom Meeting"


def test_xdotool_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")

    def _boom(*_a, **_kw):
        raise FileNotFoundError

    monkeypatch.setattr(window.subprocess, "check_output", _boom)
    assert window.active_window_title() is None

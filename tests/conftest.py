from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep tests from reading the developer's real OPENAI_API_KEY / configs."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fixture")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    os.environ.pop("XDG_RUNTIME_DIR", None)

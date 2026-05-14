from __future__ import annotations

import pytest

from meetcap.audio import volume


def _patch_node_lookup(monkeypatch: pytest.MonkeyPatch, node_id: int | None) -> None:
    monkeypatch.setattr(volume, "_find_audio_source_id", lambda _name: node_id)


def _patch_get_volume(monkeypatch: pytest.MonkeyPatch, value: float | None) -> None:
    monkeypatch.setattr(volume, "_wpctl_get_volume", lambda _id: value)


def test_boost_lifts_below_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_node_lookup(monkeypatch, 42)
    _patch_get_volume(monkeypatch, 0.6)
    calls: list[tuple[int, float]] = []

    def _set(nid: int, v: float) -> bool:
        calls.append((nid, v))
        return True

    monkeypatch.setattr(volume, "_wpctl_set_volume", _set)
    snap = volume.boost("mic", 1.0)
    assert snap is not None
    assert snap.previous == pytest.approx(0.6)
    assert calls == [(42, 1.0)]


def test_boost_never_lowers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_node_lookup(monkeypatch, 42)
    _patch_get_volume(monkeypatch, 1.5)  # already above target
    monkeypatch.setattr(volume, "_wpctl_set_volume", lambda *_: True)
    assert volume.boost("mic", 1.0) is None


def test_boost_disabled_when_target_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Should not even try to find the node.
    called = {"x": False}

    def _fail(*_a, **_kw):
        called["x"] = True
        return None

    monkeypatch.setattr(volume, "_find_audio_source_id", _fail)
    assert volume.boost("mic", None) is None
    assert volume.boost("mic", 0) is None
    assert called["x"] is False


def test_boost_returns_none_on_missing_node(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_node_lookup(monkeypatch, None)
    assert volume.boost("mic", 1.0) is None


def test_restore_no_op_on_none() -> None:
    volume.restore(None)  # Must not raise.

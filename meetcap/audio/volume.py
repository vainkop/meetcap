from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class VolumeSnapshot:
    """Captured pre-boost state for a PipeWire audio source node."""

    node_id: int
    previous: float


def _find_audio_source_id(source_name: str) -> int | None:
    try:
        raw = subprocess.check_output(["pw-dump"], stderr=subprocess.DEVNULL, timeout=5)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {}) or {}
        if props.get("node.name") != source_name:
            continue
        if not (props.get("media.class") or "").startswith("Audio/Source"):
            continue
        nid = obj.get("id")
        if isinstance(nid, int):
            return nid
    return None


def _wpctl_get_volume(node_id: int) -> float | None:
    try:
        out = subprocess.check_output(
            ["wpctl", "get-volume", str(node_id)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    parts = out.strip().split()
    if len(parts) < 2 or parts[0] != "Volume:":
        return None
    try:
        return float(parts[1])
    except ValueError:
        return None


def _wpctl_set_volume(node_id: int, volume: float) -> bool:
    try:
        subprocess.run(
            ["wpctl", "set-volume", str(node_id), f"{volume:.2f}"],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            check=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def boost(source_name: str, target: float | None) -> VolumeSnapshot | None:
    """Raise mic volume to `target` if currently below it. Never lowers.

    Returns a snapshot only if a change was applied (so callers know whether
    to restore on stop). Returns None when disabled, wpctl is missing, the
    node can't be found, or the current volume already meets the target.
    """
    if target is None or target <= 0:
        return None
    node_id = _find_audio_source_id(source_name)
    if node_id is None:
        return None
    current = _wpctl_get_volume(node_id)
    if current is None:
        return None
    if current >= target:
        return None
    if not _wpctl_set_volume(node_id, target):
        return None
    return VolumeSnapshot(node_id=node_id, previous=current)


def restore(snap: VolumeSnapshot | None) -> None:
    if snap is None:
        return
    _wpctl_set_volume(snap.node_id, snap.previous)

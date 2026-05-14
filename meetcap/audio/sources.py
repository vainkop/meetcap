from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from meetcap.config import AudioConfig


@dataclass(frozen=True)
class AudioSources:
    mic: str
    sink_monitor: str


class SourceResolutionError(RuntimeError):
    """pw-dump did not yield a usable default sink + source pair."""


def _pw_dump() -> list[dict[str, Any]]:
    try:
        raw = subprocess.check_output(["pw-dump"], stderr=subprocess.DEVNULL, timeout=5)
    except FileNotFoundError as e:
        raise SourceResolutionError("pw-dump not on PATH; is PipeWire installed?") from e
    except subprocess.CalledProcessError as e:
        raise SourceResolutionError(f"pw-dump exited {e.returncode}") from e
    parsed: list[dict[str, Any]] = json.loads(raw)
    return parsed


def _read_default_audio() -> tuple[str, str]:
    """Return (default_sink_name, default_source_name) from pw-dump.

    Uses `default.audio.{sink,source}` — the currently active devices —
    not `default.configured.audio.*`, which can point at a device that is
    offline (e.g. a paired-but-disconnected Bluetooth headset).
    """
    sink = source = None
    for obj in _pw_dump():
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if obj.get("props", {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata", []):
            key, value = entry.get("key"), entry.get("value") or {}
            name = value.get("name") if isinstance(value, dict) else None
            if key == "default.audio.sink" and name:
                sink = name
            elif key == "default.audio.source" and name:
                source = name
        break
    if not sink or not source:
        raise SourceResolutionError(
            "pw-dump did not report default.audio.sink and default.audio.source"
        )
    return sink, source


def resolve(audio_cfg: AudioConfig) -> AudioSources:
    """Resolve mic + sink-monitor source names. Honors `[audio]` overrides."""
    if audio_cfg.mic_source and audio_cfg.sink_monitor:
        return AudioSources(mic=audio_cfg.mic_source, sink_monitor=audio_cfg.sink_monitor)
    sink, source = _read_default_audio()
    mic = audio_cfg.mic_source or source
    monitor = audio_cfg.sink_monitor or f"{sink}.monitor"
    return AudioSources(mic=mic, sink_monitor=monitor)

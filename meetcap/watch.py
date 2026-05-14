from __future__ import annotations

import signal
import time
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from meetcap.audio import volume
from meetcap.audio.paths import build_meeting_dir
from meetcap.audio.recorder import Recorder
from meetcap.audio.sources import resolve as resolve_sources
from meetcap.audio.volume import VolumeSnapshot
from meetcap.config import Settings
from meetcap.detect.pipewire_streams import find_meeting_app
from meetcap.detect.window import active_window_title

POLL_SECONDS = 2.0
STOP_GRACE_SECONDS = 10.0


def _default_slug() -> str:
    return active_window_title() or "untitled"


def run_watch(
    settings: Settings,
    console: Console,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-running watcher: spawns a recorder whenever a meeting app captures the mic."""
    stop_requested = {"flag": False}

    def _request_stop(*_: object) -> None:
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    sources = resolve_sources(settings.audio)
    console.print(f"[cyan]watching[/cyan] mic={sources.mic}  sink_monitor={sources.sink_monitor}")

    recorder: Recorder | None = None
    meeting_dir: Path | None = None
    quiet_since: float | None = None
    vol_snap: VolumeSnapshot | None = None

    while not stop_requested["flag"]:
        app = find_meeting_app()
        now = time.monotonic()

        if app and recorder is None:
            slug = _default_slug()
            meeting_dir = build_meeting_dir(slug)
            recorder = Recorder(sources, meeting_dir / "audio.flac")
            vol_snap = volume.boost(sources.mic, settings.audio.mic_boost_to)
            extra = (
                f" mic_boost {vol_snap.previous:.2f}->{settings.audio.mic_boost_to:.2f}"
                if vol_snap is not None
                else ""
            )
            console.print(
                f"[green]start[/green] app={app.name!r} pid={app.pid} dir={meeting_dir}{extra}"
            )
            recorder.start()
            quiet_since = None
        elif app and recorder is not None:
            quiet_since = None
        elif app is None and recorder is not None:
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= STOP_GRACE_SECONDS:
                console.print(f"[yellow]stop[/yellow] dir={meeting_dir}")
                recorder.stop()
                volume.restore(vol_snap)
                recorder = None
                meeting_dir = None
                quiet_since = None
                vol_snap = None

        sleep(POLL_SECONDS)

    if recorder is not None:
        console.print("[yellow]shutdown: stopping in-flight recorder[/yellow]")
        recorder.stop()
        volume.restore(vol_snap)

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console

from meetcap.audio.paths import RECORDINGS_ROOT, build_meeting_dir
from meetcap.audio.recorder import Recorder
from meetcap.audio.sources import resolve as resolve_sources
from meetcap.audio.volume import VolumeSnapshot
from meetcap.audio.volume import boost as boost_volume
from meetcap.audio.volume import restore as restore_volume
from meetcap.config import Settings
from meetcap.detect.pipewire_streams import find_meeting_app
from meetcap.detect.window import active_window_title
from meetcap.merge.plan import MergeError, MergePlan
from meetcap.notify import (
    notify_post_process_failure,
    notify_post_process_success,
    notify_recording_died,
)
from meetcap.tray.indicator import TrayIndicator
from meetcap.tray.retry import (
    RetryQueue,
    delay_for_attempt,
    detect_failed_stage,
    tail_error,
)
from meetcap.tray.state import TrayState

POLL_SECONDS = 2.0
STOP_GRACE_SECONDS = 10.0
RETRY_TICK_SECONDS = 10.0
# Hard timeout for a single transcribe+upload subprocess. Cloud diarize of a
# 60-minute audio finishes in <3 min server-side under normal conditions —
# 10 min is generous enough to absorb upload + summary + Drive convert without
# letting a stalled HTTPS socket pin the post-process lock forever.
POST_PROCESS_TIMEOUT = 600


class DaemonContext:
    """Holds mutable state shared between the GTK main loop and worker threads."""

    def __init__(self, settings: Settings, indicator: TrayIndicator, console: Console) -> None:
        self.settings = settings
        self.indicator = indicator
        self.console = console
        self.stop_event = threading.Event()
        self.auto_detect = True
        self.manual_recorder: Recorder | None = None
        self.manual_vol_snap: VolumeSnapshot | None = None
        self.manual_dir: Path | None = None
        self._lock = threading.Lock()
        # One transcribe/upload subprocess at a time across the whole daemon —
        # avoids racing on the OpenAI key / Drive token and keeps the State
        # menu coherent. Both initial post-process and retries take this lock.
        self._post_process_lock = threading.Lock()
        self._recording_active = False  # True between auto/manual start and stop
        # Set by toggle_recording when the user clicks "Stop recording" during
        # an auto-detected capture. The watch loop reads + clears it.
        self._stop_auto_event = threading.Event()
        self.retry_queue = RetryQueue()
        self.retry_queue.scan(RECORDINGS_ROOT)
        # Hook the indicator up so it can render the queue's live status and
        # so the "Retry pending now" menu item works.
        indicator.set_retry_status_provider(
            provider=self.retry_status_text,
            on_retry_now=self.force_retries_now,
        )
        # Publish the initial state — if scan() found stale failures we go
        # straight into RETRY_PENDING instead of IDLE.
        self._publish_quiescent_state()

    # ---- public surface ----

    def retry_status_text(self) -> str | None:
        active = self.retry_queue.active()
        abandoned = self.retry_queue.abandoned()
        if not active and not abandoned:
            return None
        parts: list[str] = []
        if active:
            soonest = self.retry_queue.next_due_in()
            if soonest is None or soonest <= 0:
                parts.append(f"{len(active)} pending · retrying now")
            else:
                mins, secs = divmod(int(soonest), 60)
                parts.append(f"{len(active)} pending · next in {mins}:{secs:02d}")
        if abandoned:
            parts.append(f"{len(abandoned)} abandoned")
        return " · ".join(parts)

    def force_retries_now(self) -> None:
        self.retry_queue.force_due_now()
        self._publish_quiescent_state()

    def merge_meetings(self, sources: list[Path], slug: str | None) -> None:
        """Run `meetcap merge` in a background thread, then chain post-process
        for the merged dir. Acquires the post-process lock so a fresh
        recording's transcribe can't trample the merge / merged transcribe."""
        threading.Thread(
            target=self._merge_then_post_process,
            args=(sources, slug),
            daemon=True,
        ).start()

    def _merge_then_post_process(self, sources: list[Path], slug: str | None) -> None:
        merged_dir = self._infer_merged_dir(sources, slug)
        if merged_dir is None:
            self.console.print("[red]merge: invalid input dirs[/red]")
            return
        with self._post_process_lock:
            self.indicator.set_state(TrayState.MERGING, merged_dir.name)
            merge_log = merged_dir.parent / f".{merged_dir.name}.merge.log"
            merge_log.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                "-m",
                "meetcap.cli",
                "merge",
                *(str(s) for s in sources),
                "--no-transcribe",
            ]
            if slug:
                cmd += ["--name", slug]
            self.console.print(f"[cyan]merge[/cyan] {len(sources)} sources → {merged_dir.name}")
            try:
                with merge_log.open("w") as log:
                    rc = subprocess.run(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=POST_PROCESS_TIMEOUT,
                    ).returncode
            except subprocess.TimeoutExpired:
                self.console.print(f"[red]merge timed out[/red] {merged_dir.name}")
                rc = 124
            except Exception as e:
                self.console.print(f"[red]merge spawn failed:[/red] {e}")
                rc = 1
            if rc != 0:
                self.console.print(f"[red]merge exited {rc}[/red] (see {merge_log})")
                if self.settings.notifications.enabled and not self._recording_active:
                    notify_post_process_failure(merged_dir, merge_log)
                self._publish_quiescent_state()
                return
            self.indicator.refresh_recent()
        # Chain post-process for the merged dir — re-acquires the lock cleanly.
        self._post_process(merged_dir)

    def _infer_merged_dir(self, sources: list[Path], slug: str | None) -> Path | None:
        """Replicate MergePlan's naming so we know which dir to post-process
        without re-parsing the subprocess output."""
        try:
            plan = MergePlan.build(sources, custom_slug=slug)
        except MergeError:
            return None
        return plan.output_dir

    def toggle_recording(self) -> None:
        """Stop whatever recording is in flight (manual *or* auto), else start
        a manual one. Critical: the "Stop recording" menu item used to only stop
        manual recordings — auto-detected captures got stuck."""
        with self._lock:
            if self.manual_recorder is not None:
                self._stop_manual_locked()
            elif self._recording_active:
                # Watch thread owns the recorder object — signal it to stop.
                self.console.print("[yellow]user requested stop on auto recording[/yellow]")
                self._stop_auto_event.set()
            else:
                self._start_manual_locked()

    # Back-compat alias for the GTK callback wired in cli.py.
    toggle_manual_recording = toggle_recording

    def set_auto_detect(self, enabled: bool) -> None:
        self.auto_detect = enabled
        self.console.print(f"[cyan]auto-detect[/cyan] {'enabled' if enabled else 'disabled'}")

    # ---- manual recording ----

    def _start_manual_locked(self) -> None:
        try:
            sources = resolve_sources(self.settings.audio)
        except Exception as e:
            self.console.print(f"[red]source resolution failed:[/red] {e}")
            self.indicator.set_state(TrayState.ERROR, "audio source")
            return
        slug = active_window_title() or "manual"
        self.manual_dir = build_meeting_dir(slug)
        self.manual_recorder = Recorder(sources, self.manual_dir / "audio.flac")
        self.manual_vol_snap = boost_volume(sources.mic, self.settings.audio.mic_boost_to)
        self.manual_recorder.start()
        self._recording_active = True
        self.console.print(f"[green]manual start[/green] {self.manual_dir}")
        self.indicator.set_state(TrayState.RECORDING, self.manual_dir.name)

    def _stop_manual_locked(self) -> None:
        if self.manual_recorder is None or self.manual_dir is None:
            return
        meeting_dir = self.manual_dir
        self.manual_recorder.stop()
        restore_volume(self.manual_vol_snap)
        self.manual_recorder = None
        self.manual_vol_snap = None
        self.manual_dir = None
        self._recording_active = False
        self.console.print(f"[yellow]manual stop[/yellow] {meeting_dir}")
        self.indicator.refresh_recent()
        self._spawn_post_process(meeting_dir)

    # ---- post-process ----

    def _spawn_post_process(self, meeting_dir: Path) -> None:
        """Run transcribe + upload in a background thread; never blocks the watcher."""
        threading.Thread(
            target=self._post_process,
            args=(meeting_dir,),
            daemon=True,
        ).start()

    def _post_process(self, meeting_dir: Path) -> None:
        """Take the post-process lock, run `meetcap transcribe --upload`, update
        queue + indicator from the outcome. Both initial post-process and retry
        attempts funnel through here."""
        with self._post_process_lock:
            self.indicator.set_state(TrayState.TRANSCRIBING, meeting_dir.name)
            log_path = meeting_dir / ".post-process.log"
            cmd = [
                sys.executable,
                "-m",
                "meetcap.cli",
                "transcribe",
                str(meeting_dir),
                "--upload",
            ]
            try:
                with log_path.open("w") as log:
                    rc = subprocess.run(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=POST_PROCESS_TIMEOUT,
                    ).returncode
            except subprocess.TimeoutExpired:
                # The child has been SIGKILLed by subprocess.run. Append a
                # marker so .post-process.log captures why we bailed.
                with log_path.open("a") as log:
                    log.write(
                        f"\npost-process timed out after {POST_PROCESS_TIMEOUT}s; "
                        "killed and queued for retry\n"
                    )
                self._record_failure(
                    meeting_dir,
                    f"timeout after {POST_PROCESS_TIMEOUT}s",
                    detect_failed_stage(meeting_dir),
                    log_path,
                )
                return
            except Exception as e:
                self.console.print(f"[red]post-process spawn failed:[/red] {e}")
                self._record_failure(meeting_dir, f"spawn: {e}", "transcribe", log_path)
                return

            if rc == 0:
                self.retry_queue.mark_success(meeting_dir)
                if self.settings.notifications.enabled and not self._recording_active:
                    notify_post_process_success(meeting_dir)
                self.indicator.refresh_recent()
            else:
                stage = detect_failed_stage(meeting_dir)
                last_err = tail_error(log_path)
                self._record_failure(meeting_dir, last_err, stage, log_path)
                return
        # Lock released — recompute state. Done outside the lock so a queued
        # retry can grab it next without waiting on us.
        self._publish_quiescent_state()

    def _record_failure(
        self, meeting_dir: Path, last_error: str, stage: str, log_path: Path
    ) -> None:
        entry = self.retry_queue.add(meeting_dir, last_error, stage)
        if entry.abandoned:
            self.console.print(
                f"[red]{stage} failed[/red] {meeting_dir.name}: {last_error}\n"
                f"[red]abandoned[/red] after {entry.attempts} attempt(s) "
                "(permanent error or max retries hit) — "
                "use 'Retry pending now' to force one more try."
            )
        else:
            delay = delay_for_attempt(entry.attempts - 1)
            mins, secs = divmod(delay, 60)
            self.console.print(
                f"[red]{stage} failed[/red] {meeting_dir.name}: {last_error}\n"
                f"[yellow]retry #{entry.attempts + 1} in {mins}:{secs:02d}[/yellow]"
            )
        # Suppress libnotify while a recording is in flight — popping an error
        # toast mid-meeting is alarming and the tray icon (amber dot) already
        # shows the queue state passively. Surface it once we're back to idle.
        if self.settings.notifications.enabled and not self._recording_active:
            notify_post_process_failure(meeting_dir, log_path)
        # Caller still holds the post-process lock; publish state once it's free.
        self._publish_quiescent_state()

    def _publish_quiescent_state(self) -> None:
        """Show RETRY_PENDING (queue non-empty) or IDLE — unless a recording is
        active, in which case the watcher / manual path owns the state and we
        leave it alone."""
        if self._recording_active:
            return
        text = self.retry_status_text()
        if text:
            self.indicator.set_state(TrayState.RETRY_PENDING, text)
        else:
            self.indicator.set_state(TrayState.IDLE)


# ---- watch loop ----


def run_watch_thread(ctx: DaemonContext) -> None:
    """Long-running detection loop. Auto-records when a meeting app captures the mic."""
    try:
        sources = resolve_sources(ctx.settings.audio)
    except Exception as e:
        ctx.console.print(f"[red]source resolution failed:[/red] {e}")
        ctx.indicator.set_state(TrayState.ERROR, "audio source")
        return

    ctx.console.print(
        f"[cyan]watching[/cyan] mic={sources.mic}  sink_monitor={sources.sink_monitor}"
    )
    recorder: Recorder | None = None
    meeting_dir: Path | None = None
    quiet_since: float | None = None
    vol_snap: VolumeSnapshot | None = None

    while not ctx.stop_event.is_set():
        # Manual recording active -> don't fight it; let the manual path own everything.
        if ctx.manual_recorder is not None:
            time.sleep(POLL_SECONDS)
            continue

        # ffmpeg died on its own (crash, PulseAudio reset, OOM kill, ...).
        # The watch loop used to assume "once started, recorder stays alive
        # until I stop it" and silently held a dead recorder for the rest
        # of the call. Detect and recover: queue what we captured for
        # post-process, notify the user, reset state so the *next* tick can
        # spin a fresh recorder up if the meeting app is still streaming.
        if recorder is not None and not recorder.is_alive():
            rc = recorder.returncode() or -1
            ctx.console.print(f"[red]ffmpeg exited unexpectedly[/red] rc={rc} dir={meeting_dir}")
            restore_volume(vol_snap)
            ctx._recording_active = False
            died_dir = meeting_dir
            recorder = None
            meeting_dir = None
            quiet_since = None
            vol_snap = None
            if died_dir is not None:
                ctx._spawn_post_process(died_dir)
                ctx.indicator.refresh_recent()
                if ctx.settings.notifications.enabled:
                    notify_recording_died(died_dir, rc)
            # Don't sleep — fall through to detection so a fresh recording
            # can start on this tick if Zoom is still capturing.

        # User clicked "Stop recording" on an auto-detected capture.
        if ctx._stop_auto_event.is_set() and recorder is not None:
            ctx.console.print(f"[yellow]user stop[/yellow] dir={meeting_dir}")
            recorder.stop()
            restore_volume(vol_snap)
            ctx._recording_active = False
            if meeting_dir is not None:
                ctx._spawn_post_process(meeting_dir)
            recorder = None
            meeting_dir = None
            quiet_since = None
            vol_snap = None
            ctx._stop_auto_event.clear()
            ctx.indicator.refresh_recent()
            time.sleep(POLL_SECONDS)
            continue
        elif ctx._stop_auto_event.is_set():
            # No recording was actually active; clear the spurious flag.
            ctx._stop_auto_event.clear()

        if not ctx.auto_detect:
            if recorder is not None:
                # Auto-detect was disabled mid-recording; stop cleanly.
                ctx.console.print("[yellow]auto stop (auto-detect disabled)[/yellow]")
                recorder.stop()
                restore_volume(vol_snap)
                ctx._recording_active = False
                if meeting_dir is not None:
                    ctx._spawn_post_process(meeting_dir)
                recorder = None
                meeting_dir = None
                quiet_since = None
                vol_snap = None
                ctx.indicator.refresh_recent()
            time.sleep(POLL_SECONDS)
            continue

        app = find_meeting_app()
        now = time.monotonic()

        if app and recorder is None:
            slug = active_window_title() or "meeting"
            meeting_dir = build_meeting_dir(slug)
            recorder = Recorder(sources, meeting_dir / "audio.flac")
            vol_snap = boost_volume(sources.mic, ctx.settings.audio.mic_boost_to)
            ctx.console.print(
                f"[green]auto start[/green] app={app.name!r} pid={app.pid} dir={meeting_dir}"
            )
            recorder.start()
            ctx._recording_active = True
            ctx.indicator.set_state(TrayState.RECORDING, meeting_dir.name)
            quiet_since = None
        elif app and recorder is not None:
            quiet_since = None
        elif app is None and recorder is not None:
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= STOP_GRACE_SECONDS:
                ctx.console.print(f"[yellow]auto stop[/yellow] dir={meeting_dir}")
                recorder.stop()
                restore_volume(vol_snap)
                ctx._recording_active = False
                if meeting_dir is not None:
                    ctx._spawn_post_process(meeting_dir)
                recorder = None
                meeting_dir = None
                quiet_since = None
                vol_snap = None
                ctx.indicator.refresh_recent()

        time.sleep(POLL_SECONDS)

    # Shutdown: stop any in-flight recorder cleanly (no post-process — user is quitting).
    if recorder is not None:
        ctx.console.print("[yellow]shutdown: stopping in-flight recorder[/yellow]")
        recorder.stop()
        restore_volume(vol_snap)
        ctx._recording_active = False


# ---- retry loop ----


def run_retry_thread(ctx: DaemonContext) -> None:
    """Wake every RETRY_TICK_SECONDS, fire any due retries through `_post_process`.
    Serialized via `_post_process_lock` so retries can't trample a fresh recording's
    transcribe."""
    while not ctx.stop_event.is_set():
        # Refresh the menu countdown opportunistically (cheap, idle-add into GTK).
        ctx._publish_quiescent_state()
        for entry in ctx.retry_queue.due():
            if ctx.stop_event.is_set():
                return
            if ctx._recording_active:
                # Don't start a transcribe while a meeting is being recorded —
                # wait for the next tick when capture is done.
                break
            ctx.console.print(
                f"[cyan]retry[/cyan] {entry.meeting_dir.name} "
                f"stage={entry.stage} attempt={entry.attempts + 1}"
            )
            ctx._post_process(entry.meeting_dir)
        if ctx.stop_event.wait(RETRY_TICK_SECONDS):
            return

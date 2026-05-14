from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from openai import OpenAI
from rich.console import Console

from meetcap import __version__
from meetcap.audio import volume
from meetcap.audio.paths import build_meeting_dir
from meetcap.audio.recorder import OK_EXIT_CODES, Recorder
from meetcap.audio.sources import SourceResolutionError
from meetcap.audio.sources import resolve as resolve_sources
from meetcap.config import load_settings
from meetcap.detect.window import active_window_title
from meetcap.drive import auth as drive_auth
from meetcap.drive.upload import upload_meeting
from meetcap.merge.plan import MergeError, MergePlan
from meetcap.merge.splice import run_concat, write_merge_markers
from meetcap.transcribe.mixdown import make_opus_mixdown
from meetcap.transcribe.openai_diarize import transcribe_diarized
from meetcap.transcribe.render import render_all
from meetcap.transcribe.summary import append_summary, summarize_transcript
from meetcap.voice.enroll import list_enrolled_speakers, run_enroll
from meetcap.watch import run_watch

REPO_ROOT = Path(__file__).resolve().parent.parent

app = typer.Typer(
    name="meetcap",
    help="Personal Linux meeting capture, OpenAI diarize, Google Drive upload.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(name="auth", help="Auth bootstrap subcommands.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"meetcap {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    pass


def _stub(phase: str) -> None:
    console.print(f"[yellow]TODO[/yellow]: implementation lands in {phase}.")


def _flac_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


@app.command()
def record(
    slug: str = typer.Option(
        "",
        "--slug",
        help="Slug for the meeting folder. Defaults to the active window title.",
    ),
) -> None:
    """Record a meeting manually until Ctrl-C."""
    settings = load_settings()
    try:
        sources = resolve_sources(settings.audio)
    except SourceResolutionError as e:
        console.print(f"[red]audio source resolution failed:[/red] {e}")
        raise typer.Exit(2) from e

    chosen_slug = slug or active_window_title() or "untitled"
    meeting_dir = build_meeting_dir(chosen_slug)
    output = meeting_dir / "audio.flac"
    console.print(
        f"[cyan]recording[/cyan] mic={sources.mic}\n"
        f"[cyan]         [/cyan] sink_monitor={sources.sink_monitor}\n"
        f"[cyan]         [/cyan] output={output}\n"
        "[dim]Ctrl-C to stop.[/dim]"
    )
    snap = volume.boost(sources.mic, settings.audio.mic_boost_to)
    if snap is not None:
        console.print(
            f"[cyan]         [/cyan] mic_boost: {snap.previous:.2f} → "
            f"{settings.audio.mic_boost_to:.2f}"
        )
    recorder = Recorder(sources, output)
    try:
        recorder.start()
        code = recorder.wait()
    finally:
        volume.restore(snap)
    if code in OK_EXIT_CODES:
        console.print(f"[green]saved[/green] {output}")
    else:
        console.print(f"[red]ffmpeg exited {code}[/red]")
        raise typer.Exit(code)


@app.command()
def watch() -> None:
    """Auto-detect Zoom / Google Meet / Slack Huddle and record on its own (headless)."""
    settings = load_settings()
    run_watch(settings, console)


@app.command()
def tray() -> None:
    """Tray icon + menu daemon (replaces `watch` for desktop sessions)."""
    import threading

    try:
        from meetcap.tray import indicator as ind
        from meetcap.tray.daemon import DaemonContext, run_retry_thread, run_watch_thread
    except (ImportError, ValueError) as e:
        console.print(
            f"[red]tray UI unavailable:[/red] {e}\n"
            "Install system PyGObject + AyatanaAppIndicator3 (`apt install "
            "python3-gi gir1.2-ayatanaappindicator3-0.1`) or run `meetcap watch` instead."
        )
        raise typer.Exit(2) from e

    settings = load_settings()

    indicator: ind.TrayIndicator | None = None
    ctx: DaemonContext | None = None

    def _on_quit() -> None:
        if ctx is not None:
            ctx.stop_event.set()
        ind.quit_gtk_main_loop()

    def _on_manual_toggle() -> None:
        if ctx is not None:
            ctx.toggle_manual_recording()

    def _on_auto_toggle(enabled: bool) -> None:
        if ctx is not None:
            ctx.set_auto_detect(enabled)

    def _on_enroll() -> None:
        # Spawn a terminal that runs `meetcap enroll` so the playback+confirm
        # prompts have somewhere to render. KDE-first; fall back to xterm.
        import shutil

        for term, args in (
            ("konsole", ["-e", "bash", "-lc"]),
            ("xterm", ["-e", "bash", "-lc"]),
            ("gnome-terminal", ["--", "bash", "-lc"]),
        ):
            if shutil.which(term):
                cmd = [
                    term,
                    *args,
                    f"cd {REPO_ROOT} && uv run meetcap enroll && "
                    "echo && read -p 'done. press Enter to close.'",
                ]
                import subprocess

                subprocess.Popen(cmd, start_new_session=True)
                return
        console.print("[yellow]no terminal emulator found; run `meetcap enroll` manually[/yellow]")

    indicator = ind.TrayIndicator(
        on_manual_toggle=_on_manual_toggle,
        on_auto_toggle=_on_auto_toggle,
        on_enroll=_on_enroll,
        on_quit=_on_quit,
        settings=settings,
    )

    def _open_merge_dialog() -> None:
        # Imported lazily so headless `meetcap tray` startup doesn't pull in
        # the dialog module before we know AppIndicator + GTK loaded cleanly.
        from meetcap.tray.merge_dialog import MergeDialog

        def _on_submit(sources: list[Path], slug: str | None) -> None:
            if ctx is not None:
                ctx.merge_meetings(sources, slug)

        MergeDialog(on_submit=_on_submit)

    indicator.set_merge_handler(_open_merge_dialog)

    ctx = DaemonContext(settings, indicator, console)
    t_watch = threading.Thread(target=run_watch_thread, args=(ctx,), daemon=True)
    t_retry = threading.Thread(target=run_retry_thread, args=(ctx,), daemon=True)
    t_watch.start()
    t_retry.start()
    ind.run_gtk_main_loop()
    ctx.stop_event.set()
    t_watch.join(timeout=5)
    t_retry.join(timeout=5)


@app.command(name="install-service")
def install_service() -> None:
    """Install the user systemd unit (runs `meetcap tray` on login)."""
    src = Path(__file__).resolve().parent.parent / "systemd" / "meetcap.service"
    if not src.exists():
        console.print(f"[red]systemd unit not found at {src}[/red]")
        raise typer.Exit(2)
    uv_path = shutil.which("uv")
    if not uv_path:
        console.print("[red]uv not on PATH[/red]")
        raise typer.Exit(2)
    template = src.read_text()
    rendered = template.replace("{REPO_ROOT}", str(REPO_ROOT)).replace("{UV}", uv_path)
    dst_dir = Path.home() / ".config" / "systemd" / "user"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "meetcap.service"
    dst.write_text(rendered)
    console.print(f"[green]installed[/green] {dst}")
    console.print(
        "[dim]Reload + enable:[/dim]\n"
        "  systemctl --user daemon-reload\n"
        "  systemctl --user enable --now meetcap.service\n"
        "  journalctl --user -fu meetcap"
    )


@app.command(name="install-desktop")
def install_desktop() -> None:
    """Install a .desktop launcher (KMenu / KRunner / panel pin)."""
    import subprocess

    uv_path = shutil.which("uv")
    if not uv_path:
        console.print("[red]uv not on PATH[/red]")
        raise typer.Exit(2)

    icon_src = REPO_ROOT / "meetcap" / "assets" / "meetcap-idle.svg"
    icon_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_dst = icon_dir / "meetcap.svg"
    shutil.copy(icon_src, icon_dst)

    apps_dir = Path.home() / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    desktop_dst = apps_dir / "meetcap.desktop"
    desktop_dst.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=meetcap\n"
        "GenericName=Meeting Recorder\n"
        "Comment=Record, transcribe, and upload meetings to Google Drive\n"
        f"Exec={uv_path} run --project {REPO_ROOT} meetcap tray\n"
        "Icon=meetcap\n"
        "Categories=AudioVideo;Audio;Recorder;\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "StartupWMClass=meetcap\n"
        "Keywords=meeting;record;zoom;transcribe;\n"
    )

    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "--quiet", str(icon_dir.parent.parent.parent)],
        ["kbuildsycoca6"],  # KDE Plasma 6 — refresh menu cache
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    console.print(f"[green]installed[/green] {desktop_dst}")
    console.print(f"[green]icon[/green]      {icon_dst}")
    console.print(
        "[dim]Type 'meetcap' in KRunner / KMenu to launch. "
        "Right-click the result → Pin to Taskbar to add it to the panel.[/dim]"
    )


@app.command()
def enroll(
    name: str = typer.Option("me", "--name", help="Speaker name for the voice sample."),
) -> None:
    """Record a 7s voice sample for diarization speaker matching."""
    settings = load_settings()
    try:
        sources = resolve_sources(settings.audio)
    except SourceResolutionError as e:
        console.print(f"[red]audio source resolution failed:[/red] {e}")
        raise typer.Exit(2) from e
    run_enroll(
        name=name, sources=sources, console=console, mic_boost_to=settings.audio.mic_boost_to
    )


# When the OpenAI backend fails with one of these patterns, the same call will
# *always* fail on the cloud — but the local backend (no duration cap, no
# quota) usually handles it fine. _run_transcribe auto-falls-back in that
# case, provided the local extras are installed.
FALLBACK_TRIGGER_PATTERNS: tuple[str, ...] = (
    "is longer than",  # OpenAI: "audio duration X is longer than Y"
    "insufficient_quota",
    "insufficient credits",
    "model_not_found",
    "model_deprecated",
)


def _local_backend_available() -> bool:
    try:
        import meetcap.transcribe.local_pipeline  # noqa: F401

        return True
    except ImportError:
        return False


def _should_fallback_to_local(backend: str, error_message: str) -> bool:
    """True if the failure is a kind that local backend can recover from."""
    if backend != "openai":
        return False
    if not _local_backend_available():
        return False
    lowered = error_message.lower()
    return any(p in lowered for p in FALLBACK_TRIGGER_PATTERNS)


@app.command()
def transcribe(
    directory: str = typer.Argument(..., help="Path to a meeting folder."),
    force: bool = typer.Option(False, "--force", help="Re-transcribe even if outputs exist."),
    summary: bool = typer.Option(
        True, "--summary/--no-summary", help="Append a Summary section to transcript.md."
    ),
    upload: bool = typer.Option(False, "--upload", help="Chain into Drive upload on success."),
    backend_override: str | None = typer.Option(
        None,
        "--backend",
        help=(
            "Override transcription backend for this call only: 'openai' or 'local'. "
            "Defaults to transcription.backend in config.toml."
        ),
    ),
) -> None:
    """Run the OpenAI diarize pipeline on a recorded meeting.

    Auto-falls-back from openai to local when the cloud rejects the request
    for an unrecoverable reason (duration cap, insufficient quota, retired
    model) AND the local extras are installed. Override the choice per call
    with --backend."""
    settings = load_settings()
    meeting_dir = Path(directory).expanduser().resolve()
    # Sub-segments of a merged meeting must not run their own transcribe / upload —
    # their content is in the merged dir already. Exit 0 so the retry queue marks
    # them as resolved and stops cycling.
    if (meeting_dir / ".merged-into.json").is_file():
        console.print(f"[yellow]skip[/yellow] {meeting_dir.name}: merged into another meeting")
        raise typer.Exit(0)
    flac = meeting_dir / "audio.flac"
    if not flac.is_file():
        console.print(f"[red]no audio.flac under[/red] {meeting_dir}")
        raise typer.Exit(2)

    transcript_json = meeting_dir / "transcript.json"
    transcribed_already = transcript_json.exists() and not force
    if transcribed_already:
        console.print(
            f"[yellow]already transcribed[/yellow] {transcript_json} (use --force to redo)"
        )

    if not transcribed_already:
        opus = make_opus_mixdown(flac)
        size_mb = opus.stat().st_size / (1024 * 1024)
        console.print(f"[cyan]mixdown[/cyan] {flac.name} → {opus.name} ({size_mb:.2f} MB)")

        speakers = list_enrolled_speakers()
        if speakers:
            console.print(f"[cyan]speakers[/cyan] enrolled: {', '.join(n for n, _ in speakers)}")
        else:
            console.print(
                "[yellow]no enrolled voice samples — speakers will be labeled A, B, C[/yellow]"
            )

        started_iso = _flac_mtime_iso(flac)
        backend = (backend_override or settings.transcription.backend).lower()
        if backend not in ("openai", "local"):
            console.print(f"[red]unknown backend:[/red] {backend!r} (expected 'openai' or 'local')")
            raise typer.Exit(2)
        resp, model_label = _run_transcribe(backend, settings, meeting_dir, opus, speakers)
        # model_label embeds the actually-used backend ("local/..." vs cloud
        # model name), so an auto-fallback shows up clearly in metadata.json.
        actually_local = model_label.startswith("local/")
        cost_rate = None if actually_local else settings.transcription.cost_per_minute_usd
        json_path, md_path, meta_path = render_all(
            resp,
            meeting_dir=meeting_dir,
            model=model_label,
            started_iso=started_iso,
            cost_per_minute_usd=cost_rate,
        )
        console.print(f"[green]wrote[/green] {json_path.name}, {md_path.name}, {meta_path.name}")

        if summary and settings.transcription.summary_enabled:
            _run_summary(settings, md_path)

    if upload:
        console.print(f"[cyan]upload[/cyan] {meeting_dir}")
        try:
            url = upload_meeting(meeting_dir, settings, console)
            console.print(f"[green]drive:[/green] {url}")
        except drive_auth.TokenMissingError as e:
            console.print(f"[red]not authenticated:[/red] run `meetcap auth google` first ({e})")
            raise typer.Exit(2) from e

    # If a tray retry was tracking this meeting, the .retry-state.json is now
    # stale (success won't propagate back to the running tray's in-memory queue).
    # Drop the marker so a future tray restart doesn't re-queue a completed
    # meeting. The tray's own mark_success() path handles this for retries it
    # ran itself; this covers the manual `meetcap transcribe` case.
    if (meeting_dir / "transcript.json").is_file():
        (meeting_dir / ".retry-state.json").unlink(missing_ok=True)


def _run_transcribe(
    backend: str,
    settings: Any,
    meeting_dir: Path,
    opus: Path,
    speakers: list[tuple[str, Path]],
) -> tuple[Any, str]:
    """Returns (response, model_label). Writes .transcribe-error.log on failure."""
    if backend == "local":
        try:
            from meetcap.transcribe.local_pipeline import transcribe_local
        except ImportError as e:
            console.print(
                f"[red]local backend unavailable:[/red] {e}\n"
                "Run `uv sync --extra local` to install torch + faster-whisper + "
                "pyannote + speechbrain."
            )
            raise typer.Exit(2) from e
        model_label = f"local/{settings.transcription.local_model}"
        console.print(f"[cyan]diarize[/cyan] backend=local model={model_label}")
        try:
            resp = transcribe_local(opus, settings)
        except Exception as e:
            err_log = meeting_dir / ".transcribe-error.log"
            err_log.write_text(f"{type(e).__name__}: {e}\n")
            console.print(f"[red]local transcribe failed:[/red] {type(e).__name__}: {e}")
            console.print(f"[dim]see {err_log}[/dim]")
            raise typer.Exit(1) from e
    else:
        model_label = settings.transcription.model
        console.print(f"[cyan]diarize[/cyan] backend=openai model={model_label}")
        client = OpenAI(api_key=settings.openai_api_key)
        try:
            resp = transcribe_diarized(
                client,
                audio_path=opus,
                model=settings.transcription.model,
                speaker_samples=speakers,
            )
        except Exception as e:
            err_text = f"{type(e).__name__}: {e}"
            # If the cloud rejected with a "this will never work" error and the
            # local backend is installed, transparently retry on local. The
            # user keeps a working transcript instead of an abandoned meeting.
            if _should_fallback_to_local("openai", err_text):
                console.print(
                    f"[yellow]openai rejected ({type(e).__name__})[/yellow] — "
                    "[yellow]falling back to local backend[/yellow]"
                )
                # Clear the openai error log so a clean one lands if local also fails.
                (meeting_dir / ".transcribe-error.log").unlink(missing_ok=True)
                return _run_transcribe("local", settings, meeting_dir, opus, speakers)
            err_log = meeting_dir / ".transcribe-error.log"
            err_log.write_text(err_text + "\n")
            console.print(f"[red]transcribe failed:[/red] {err_text}")
            console.print(f"[dim]see {err_log}[/dim]")
            raise typer.Exit(1) from e
    return resp, model_label


def _run_summary(settings: Any, md_path: Path) -> None:
    """Append a ## Summary section to transcript.md. Failures are non-fatal."""
    provider = settings.transcription.summary_provider.lower()
    sm = settings.transcription.summary_model
    console.print(f"[cyan]summary[/cyan] provider={provider} model={sm}")
    try:
        md_text = md_path.read_text()
        if provider == "ollama":
            from meetcap.transcribe.summary_ollama import summarize_ollama

            summary_md = summarize_ollama(md_text, model=sm)
        else:
            summary_client = OpenAI(api_key=settings.openai_api_key)
            summary_md = summarize_transcript(summary_client, md_text, model=sm)
        append_summary(md_path, summary_md)
        console.print("[green]appended[/green] ## Summary section")
    except Exception as e:
        console.print(f"[yellow]summary skipped:[/yellow] {type(e).__name__}: {e}")


@app.command()
def merge(
    dirs: list[str] = typer.Argument(..., help="Two or more meeting dirs to concat."),  # noqa: B008
    name: str | None = typer.Option(
        None, "--name", help="Slug for the merged dir (default: <first>-merged)."
    ),
    transcribe_after: bool = typer.Option(
        True,
        "--transcribe/--no-transcribe",
        help="Chain into transcribe+upload after concat.",
    ),
    upload_after: bool = typer.Option(
        True, "--upload/--no-upload", help="Drive upload after transcribe (if enabled)."
    ),
) -> None:
    """Concat audio.flac across meetings into one merged meeting dir."""
    paths = [Path(d) for d in dirs]
    try:
        plan = MergePlan.build(paths, custom_slug=name)
    except MergeError as e:
        console.print(f"[red]merge rejected:[/red] {e}")
        raise typer.Exit(2) from e

    total_min = plan.total_duration_sec / 60
    total_mb = plan.total_bytes / (1024 * 1024)
    console.print(
        f"[cyan]merging[/cyan] {len(plan.sources)} segments "
        f"(~{total_min:.1f} min, {total_mb:.1f} MB) → {plan.output_dir.name}"
    )
    for s in plan.sources:
        secs = s.duration_sec
        console.print(
            f"  + {s.name}  ({int(secs // 60)}:{int(secs % 60):02d}, {s.size_bytes // 1024} KB)"
        )

    # Markers first — if concat fails, sources still skip individual transcribe.
    write_merge_markers(plan)
    merged_flac = run_concat(plan, console)
    size_mb = merged_flac.stat().st_size / (1024 * 1024)
    console.print(f"[green]merged[/green] {merged_flac} ({size_mb:.2f} MB)")

    if not transcribe_after:
        return

    # Chain into the transcribe command in a subprocess so its exit code (which
    # the retry queue depends on) is preserved cleanly. Same path the tray uses.
    import subprocess as _sp

    cmd = [sys.executable, "-m", "meetcap.cli", "transcribe", str(plan.output_dir)]
    cmd.append("--upload" if upload_after else "--no-upload")
    rc = _sp.run(cmd).returncode
    if rc != 0:
        raise typer.Exit(rc)


@app.command()
def upload(
    directory: str = typer.Argument(..., help="Path to a meeting folder."),
) -> None:
    """Upload a meeting folder to Google Drive."""
    settings = load_settings()
    meeting_dir = Path(directory).expanduser().resolve()
    if not meeting_dir.is_dir():
        console.print(f"[red]not a directory:[/red] {meeting_dir}")
        raise typer.Exit(2)
    try:
        url = upload_meeting(meeting_dir, settings, console)
    except drive_auth.TokenMissingError as e:
        console.print(f"[red]not authenticated:[/red] run `meetcap auth google` first ({e})")
        raise typer.Exit(2) from e
    console.print(f"[green]drive:[/green] {url}")


@auth_app.command("google")
def auth_google() -> None:
    """Complete Google Drive OAuth bootstrap (or refresh the token)."""
    if not drive_auth.CLIENT_SECRET_PATH.exists():
        misplaced = drive_auth.find_misplaced_secret()
        if misplaced is not None:
            console.print(
                "[yellow]Client secret found in ~/.config/ but not at the canonical path.[/yellow]"
            )
            console.print("Run these three commands, then re-invoke `meetcap auth google`:\n")
            console.print(
                "  mkdir -p ~/.config/meetcap\n"
                "  mv ~/.config/client_secret_*.apps.googleusercontent.com.json"
                " ~/.config/meetcap/google-client-secret.json\n"
                "  chmod 600 ~/.config/meetcap/google-client-secret.json",
                soft_wrap=True,
            )
        else:
            console.print(
                f"[red]no client secret at[/red] {drive_auth.CLIENT_SECRET_PATH}\n"
                "Download an OAuth desktop client JSON from Google Cloud Console "
                "and place it at that path (chmod 600)."
            )
        raise typer.Exit(2)

    console.print("[cyan]opening browser for OAuth consent[/cyan] (scope: drive.file)")
    try:
        drive_auth.run_oauth_bootstrap()
    except drive_auth.ClientSecretMissingError as e:
        console.print(f"[red]client secret missing:[/red] {e}")
        raise typer.Exit(2) from e
    console.print(f"[green]authenticated.[/green] token saved to {drive_auth.TOKEN_PATH}")


if __name__ == "__main__":
    app()

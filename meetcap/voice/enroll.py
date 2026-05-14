from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from meetcap.audio.sources import AudioSources
from meetcap.audio.volume import boost as boost_volume
from meetcap.audio.volume import restore as restore_volume

ENROLL_SECONDS = 7
USER_CONFIG_DIR = Path.home() / ".config" / "meetcap"


def voice_sample_path(name: str) -> Path:
    return USER_CONFIG_DIR / f"{name}-voice-sample.wav"


def list_enrolled_speakers() -> list[tuple[str, Path]]:
    """Return [(name, path), ...] of enrolled voice samples found on disk."""
    if not USER_CONFIG_DIR.is_dir():
        return []
    results: list[tuple[str, Path]] = []
    for path in sorted(USER_CONFIG_DIR.glob("*-voice-sample.wav")):
        name = path.stem.removesuffix("-voice-sample")
        if name:
            results.append((name, path))
    return results


def _record_sample(mic_source: str, dest: Path, mic_boost_to: float | None) -> None:
    snap = boost_volume(mic_source, mic_boost_to)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-thread_queue_size",
                "1024",
                "-f",
                "pulse",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-i",
                mic_source,
                "-t",
                str(ENROLL_SECONDS),
                "-af",
                "aformat=channel_layouts=mono",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(dest),
            ],
            check=True,
        )
    finally:
        restore_volume(snap)


def _play_back(path: Path) -> None:
    subprocess.run(
        ["ffplay", "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit", str(path)],
        check=False,
    )


def run_enroll(
    name: str,
    sources: AudioSources,
    console: Console,
    mic_boost_to: float | None,
    prompt: bool = True,
) -> Path:
    """Record a voice sample for `name`. Returns the saved path."""
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    dest = voice_sample_path(name)

    while True:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        console.print(
            f"[cyan]recording {ENROLL_SECONDS}s sample for[/cyan] [bold]{name}[/bold]"
            f" from mic={sources.mic}"
        )
        console.print("[dim]speak naturally now...[/dim]")
        _record_sample(sources.mic, tmp_path, mic_boost_to)
        console.print("[cyan]playing back[/cyan]")
        _play_back(tmp_path)

        if not prompt:
            tmp_path.replace(dest)
            console.print(f"[green]saved[/green] {dest}")
            return dest

        choice = console.input("[bold]keep this sample? (y/N/r=retry)[/bold] ").strip().lower()
        if choice in ("y", "yes"):
            tmp_path.replace(dest)
            dest.chmod(0o600)
            console.print(f"[green]saved[/green] {dest}")
            return dest
        tmp_path.unlink(missing_ok=True)
        if choice not in ("r", "retry"):
            raise SystemExit("enrollment aborted")

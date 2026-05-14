from __future__ import annotations

import signal
import subprocess
from pathlib import Path

from meetcap.audio.sources import AudioSources

OK_EXIT_CODES = (0, 130, 255)


def _build_ffmpeg_cmd(sources: AudioSources, output: Path) -> list[str]:
    return [
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
        "48000",
        "-i",
        sources.mic,
        "-thread_queue_size",
        "1024",
        "-f",
        "pulse",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-i",
        sources.sink_monitor,
        "-filter_complex",
        # Pulse delivers stereo even with -ac 1; downmix each input to mono
        # explicitly so amerge produces an unambiguous L=mic, R=system stereo.
        (
            "[0:a]aformat=channel_layouts=mono[mic];"
            "[1:a]aformat=channel_layouts=mono[sys];"
            "[mic][sys]amerge=inputs=2,aformat=channel_layouts=stereo"
        ),
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-compression_level",
        "5",
        "-y",
        str(output),
    ]


class Recorder:
    """Wraps a long-running ffmpeg pulse-capture process."""

    def __init__(self, sources: AudioSources, output: Path) -> None:
        self.sources = sources
        self.output = output
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("Recorder already started")
        cmd = _build_ffmpeg_cmd(self.sources, self.output)
        self._proc = subprocess.Popen(cmd)

    def wait(self) -> int:
        if self._proc is None:
            raise RuntimeError("Recorder not started")
        try:
            return self._proc.wait()
        except KeyboardInterrupt:
            # Terminal SIGINT already went to the whole process group, so ffmpeg
            # is already flushing its FLAC trailer — just wait for it to finish.
            return self._proc.wait()

    def stop(self, timeout: float = 15.0) -> int:
        """Programmatic stop (for the watch daemon)."""
        if self._proc is None:
            return 0
        if self._proc.poll() is not None:
            return self._proc.returncode
        self._proc.send_signal(signal.SIGINT)
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait()

    def is_alive(self) -> bool:
        """True iff ffmpeg has been started and is still running."""
        return self._proc is not None and self._proc.poll() is None

    def returncode(self) -> int | None:
        """Exit code if the process has finished; None while still running."""
        if self._proc is None:
            return None
        return self._proc.returncode

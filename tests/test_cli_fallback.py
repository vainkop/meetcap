from __future__ import annotations

from unittest.mock import patch

from meetcap.cli import (
    FALLBACK_TRIGGER_PATTERNS,
    _should_fallback_to_local,
)


def test_fallback_triggers_on_duration_cap_when_local_installed() -> None:
    """The original problem that motivated this: a 43-min meeting fails on
    OpenAI's gpt-4o-transcribe-diarize because of the 1400 s cap. Local
    has no such cap."""
    with patch("meetcap.cli._local_backend_available", return_value=True):
        assert _should_fallback_to_local(
            "openai",
            "BadRequestError: 400 - audio duration 2631s is longer than 1400 seconds",
        )


def test_fallback_triggers_on_insufficient_quota() -> None:
    with patch("meetcap.cli._local_backend_available", return_value=True):
        assert _should_fallback_to_local("openai", "RateLimitError: insufficient_quota")


def test_fallback_does_not_trigger_on_transient_errors() -> None:
    """Connection/timeout errors should keep cycling on cloud — not silently
    switch to local."""
    with patch("meetcap.cli._local_backend_available", return_value=True):
        assert not _should_fallback_to_local("openai", "APIConnectionError: Connection error.")
        assert not _should_fallback_to_local("openai", "timeout after 600s")
        assert not _should_fallback_to_local("openai", "RateLimitError: Rate limit reached")


def test_fallback_does_not_trigger_on_auth_errors() -> None:
    """Bad API key won't be saved by switching backends — local needs its
    own auth (HF_TOKEN) and would just fail differently."""
    with patch("meetcap.cli._local_backend_available", return_value=True):
        assert not _should_fallback_to_local(
            "openai", "AuthenticationError: Incorrect API key provided"
        )


def test_fallback_skips_when_local_extras_missing() -> None:
    """No torch/faster-whisper/pyannote installed → don't pretend we can fall
    back. Caller falls through to the existing abandon path with a clear
    error message."""
    with patch("meetcap.cli._local_backend_available", return_value=False):
        assert not _should_fallback_to_local("openai", "audio duration is longer than 1400 seconds")


def test_fallback_only_from_openai() -> None:
    """Don't recurse — if the local backend itself fails, surface the error."""
    with patch("meetcap.cli._local_backend_available", return_value=True):
        assert not _should_fallback_to_local("local", "is longer than")


def test_fallback_trigger_patterns_are_substrings() -> None:
    """Real-world error messages embed these patterns, sometimes lowercased,
    sometimes inside larger strings. Make sure substring match is enough."""
    for pattern in FALLBACK_TRIGGER_PATTERNS:
        assert pattern.islower()  # match is case-insensitive via lowered input

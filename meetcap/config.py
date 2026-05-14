from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_CONFIG_DIR = Path.home() / ".config" / "meetcap"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.toml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.toml.example"


class AudioConfig(BaseModel):
    mic_source: str | None = None
    sink_monitor: str | None = None
    # On record start, raise the mic volume to this PipeWire level if it is
    # currently below it. Never lowers. None or 0 disables. 1.0 = unity gain;
    # values >1.0 amplify (laptops can need this for Zoom/Teams which interpret
    # the system mic level as the upper bound and don't apply their own boost).
    # Default 2.0 (200%) — above the Plasma UI cap of 150% but well below the
    # PipeWire hard ceiling of ~3.0.
    mic_boost_to: float | None = 2.0


class TranscriptionConfig(BaseModel):
    # "local"  = on-device faster-whisper + pyannote (default; requires
    #            `uv sync --extra local` + HF_TOKEN and the pyannote EULAs).
    # "openai" = cloud gpt-4o-transcribe-diarize (fallback / faster than local
    #            on machines without a CUDA GPU).
    backend: str = "local"
    model: str = "gpt-4o-transcribe-diarize"
    # Local-backend options (ignored when backend="openai")
    local_model: str = "large-v3"  # also: large-v3-turbo, medium, small
    local_compute_type: str = "float16"  # also: int8_float16, int8
    # None = auto-detect language; set e.g. "he" or "en" to pin it.
    language: str | None = None
    summary_enabled: bool = True
    # "ollama" = local Ollama HTTP API at http://localhost:11434 (default;
    #            requires `ollama serve` + `ollama pull <model>`).
    # "openai" = cloud Chat Completions.
    summary_provider: str = "ollama"
    # llama3.1:8b fits in 5 GB VRAM and is fast for 5-bullet summaries. For
    # higher-quality picks see `ollama list`; for summary_provider="openai" use
    # a chat-model tag like "gpt-5.4-mini" (~7× cheaper than gpt-5.5).
    summary_model: str = "llama3.1:8b"
    # $/min estimate baked into cost_estimate_usd in metadata.json. Local is
    # free; flip to ~0.006 for the cloud diarize backend. Set to null to omit.
    cost_per_minute_usd: float | None = 0.0


class DriveConfig(BaseModel):
    parent_folder_id: str = "10RTchUbN_ESFsgfA5tlTZxyF-yBbXM6r"
    convert_markdown_to_doc: bool = True


class RetentionConfig(BaseModel):
    delete_local_after_upload: bool = False


class SpeakersConfig(BaseModel):
    # Label used for your own segments in diarized transcripts once you've
    # enrolled. Change in config.toml to your first name or any tag.
    self_name: str = "me"


class NotificationsConfig(BaseModel):
    enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openai_api_key: str
    # HuggingFace token, required for the local backend's pyannote models.
    # Read from HF_TOKEN env var (in <repo>/.env). Optional unless backend="local".
    hf_token: str | None = None

    audio: AudioConfig = AudioConfig()
    transcription: TranscriptionConfig = TranscriptionConfig()
    drive: DriveConfig = DriveConfig()
    retention: RetentionConfig = RetentionConfig()
    speakers: SpeakersConfig = SpeakersConfig()
    notifications: NotificationsConfig = NotificationsConfig()


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_settings() -> Settings:
    """Compose Settings from <repo>/.env (secrets) + ~/.config/meetcap/config.toml (overrides)."""
    overrides = _read_toml(USER_CONFIG_PATH) or _read_toml(EXAMPLE_CONFIG_PATH)
    return Settings(**overrides)


def update_user_config(section: str, key: str, value: Any) -> None:
    """Write a single setting to ~/.config/meetcap/config.toml, seeding from the
    example file on first call. Comments in the example are lost on first write
    (tomli_w doesn't preserve them); subsequent writes preserve structure."""
    import tomli_w

    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if USER_CONFIG_PATH.exists():
        data = _read_toml(USER_CONFIG_PATH)
    else:
        data = _read_toml(EXAMPLE_CONFIG_PATH)
    if section not in data or not isinstance(data[section], dict):
        data[section] = {}
    if value is None:
        data[section].pop(key, None)
    else:
        data[section][key] = value
    with USER_CONFIG_PATH.open("wb") as fh:
        tomli_w.dump(data, fh)

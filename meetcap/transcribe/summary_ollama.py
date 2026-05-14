from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from meetcap.transcribe.summary import SYSTEM_PROMPT

OLLAMA_BASE = "http://localhost:11434"


class OllamaUnreachableError(RuntimeError):
    """Ollama isn't running at localhost:11434 or doesn't have the model pulled."""


def summarize_ollama(transcript_md_text: str, model: str, timeout: int = 600) -> str:
    """Local summary via Ollama's `/api/chat` endpoint. Same system prompt as cloud."""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript_md_text},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }
    ).encode("utf-8")
    req = Request(
        f"{OLLAMA_BASE}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except URLError as e:
        raise OllamaUnreachableError(
            f"Ollama unreachable at {OLLAMA_BASE} — is `ollama serve` running and "
            f"`ollama pull {model}` done?"
        ) from e
    return str(payload.get("message", {}).get("content", "")).strip()

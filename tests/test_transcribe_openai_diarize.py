from __future__ import annotations

import base64
from pathlib import Path

from meetcap.transcribe.openai_diarize import _data_url_for


def test_data_url_for_wav(tmp_path: Path) -> None:
    p = tmp_path / "bob.wav"
    p.write_bytes(b"RIFFsample")
    url = _data_url_for(p)
    assert url.startswith("data:audio/")
    assert ";base64," in url
    payload = url.split(",", 1)[1]
    assert base64.b64decode(payload) == b"RIFFsample"


def test_data_url_for_unknown_extension_defaults_to_wav(tmp_path: Path) -> None:
    p = tmp_path / "no-ext"
    p.write_bytes(b"abc")
    url = _data_url_for(p)
    assert url.startswith("data:audio/wav;base64,")

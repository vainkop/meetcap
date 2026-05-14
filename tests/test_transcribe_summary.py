from __future__ import annotations

from pathlib import Path

from meetcap.transcribe.summary import append_summary


def test_append_summary_appends_section(tmp_path: Path) -> None:
    md = tmp_path / "transcript.md"
    md.write_text("## [00:00:00] bob\nHi.\n")
    append_summary(md, "### 5-bullet summary\n- item 1\n\n### Action items\n- do thing")
    content = md.read_text()
    assert "## Summary" in content
    assert "### 5-bullet summary" in content
    assert content.count("## Summary") == 1


def test_append_summary_replaces_existing(tmp_path: Path) -> None:
    md = tmp_path / "transcript.md"
    md.write_text("## [00:00:00] bob\nHi.\n\n## Summary\n\nold summary content\n")
    append_summary(md, "new content")
    content = md.read_text()
    assert content.count("## Summary") == 1
    assert "old summary content" not in content
    assert "new content" in content

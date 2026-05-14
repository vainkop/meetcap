from __future__ import annotations

from pathlib import Path

from openai import OpenAI

SYSTEM_PROMPT = (
    "You summarize meeting transcripts produced by an automated diarization "
    "pipeline. The transcript may mix English, Russian, and Hebrew within a "
    "single meeting. Write the summary in English regardless of the spoken "
    "languages. Produce exactly two sections in Markdown:\n"
    "1. `### 5-bullet summary` — at most five concise bullet points covering "
    "the key decisions, topics, and outcomes.\n"
    "2. `### Action items` — a bulleted list, each item attributed to a "
    "speaker label when possible (use the labels exactly as they appear in "
    "the transcript). If there are none, write the single line `None.`.\n"
    "Do not invent content that is not in the transcript. Do not include any "
    "preamble or commentary outside the two sections."
)


def summarize_transcript(client: OpenAI, transcript_md_text: str, model: str) -> str:
    """Return the Summary section markdown for the given transcript."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript_md_text},
        ],
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


def append_summary(transcript_md: Path, summary_md: str) -> None:
    existing = transcript_md.read_text()
    marker = "\n## Summary\n"
    if marker in existing:
        head, _ = existing.split(marker, 1)
        existing = head.rstrip() + "\n"
    transcript_md.write_text(existing.rstrip() + f"\n\n## Summary\n\n{summary_md}\n")

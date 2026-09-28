"""Pure rendering: transcripts and notes to Markdown / WebVTT / compact LLM text."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import Phrase, Transcript


def fmt_ts(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def fmt_vtt(ms: int) -> str:
    ms = max(0, int(ms))
    return f"{ms // 3_600_000:02d}:{(ms % 3_600_000) // 60_000:02d}:{(ms % 60_000) // 1000:02d}.{ms % 1000:03d}"


def speaker_label(speaker: int | None) -> str:
    return f"S{speaker}" if speaker is not None else "Speaker"  # "Speaker" = transcriber gave no speaker labels


@dataclass
class Turn:
    speaker: int | None
    start_ms: int
    end_ms: int
    text: str


def turns(phrases: list[Phrase], max_gap_ms: int = 2000, max_turn_ms: int = 60_000) -> list[Turn]:
    """Merge consecutive phrases from the same speaker into readable turns (at most ~1 min each, so every
    minute keeps a timestamp even when the transcriber gives no speaker labels)."""
    out: list[Turn] = []
    for p in sorted(phrases, key=lambda x: x.start_ms):
        if (out and out[-1].speaker == p.speaker and p.start_ms - out[-1].end_ms <= max_gap_ms
                and p.end_ms - out[-1].start_ms <= max_turn_ms):
            out[-1].text += " " + p.text
            out[-1].end_ms = max(out[-1].end_ms, p.end_ms)
        else:
            out.append(Turn(p.speaker, p.start_ms, p.end_ms, p.text))
    return out


def speaker_names(notes: dict[str, Any] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    for s in (notes or {}).get("speakers", []) or []:
        if isinstance(s, dict) and s.get("label") and s.get("name"):
            names[str(s["label"])] = str(s["name"])
    return names


def _who(speaker: int | None, names: dict[str, str]) -> str:
    label = speaker_label(speaker)
    return f"{names[label]} ({label})" if label in names else label


def transcript_for_llm(t: Transcript) -> str:
    return "\n".join(f"[{fmt_ts(x.start_ms)}] {speaker_label(x.speaker)}: {x.text}" for x in turns(t.phrases))


def transcript_markdown(t: Transcript, meta: dict[str, Any], names: dict[str, str] | None = None) -> str:
    names = names or {}
    lines = [f"# Transcript: {meta.get('title') or meta.get('id', '')}", "",
             f"{_when(meta)} · {meta.get('app', '')} · {fmt_ts(t.duration_ms)}", ""]
    for x in turns(t.phrases):
        lines.append(f"**[{fmt_ts(x.start_ms)}] {_who(x.speaker, names)}:** {x.text}")
        lines.append("")
    if not t.phrases:
        lines.append("_No speech detected._")
    return "\n".join(lines).rstrip() + "\n"


def transcript_vtt(t: Transcript, names: dict[str, str] | None = None) -> str:
    names = names or {}
    out = ["WEBVTT", ""]
    for i, p in enumerate(sorted(t.phrases, key=lambda x: x.start_ms), 1):
        out += [str(i), f"{fmt_vtt(p.start_ms)} --> {fmt_vtt(p.end_ms)}",
                f"<v {_who(p.speaker, names)}>{p.text}", ""]
    return "\n".join(out)


def _when(meta: dict[str, Any]) -> str:
    ts = meta.get("started_at")
    return datetime.fromtimestamp(ts).strftime("%a %d %b %Y, %H:%M") if ts else ""


def _bullets(items: list[Any]) -> list[str]:
    return [f"- {i}" for i in items if str(i).strip()]


def notes_markdown(notes: dict[str, Any], meta: dict[str, Any], files: dict[str, str]) -> str:
    n = notes
    lines = [f"# {n.get('title') or 'Meeting notes'}", "",
             f"{_when(meta)} · {meta.get('app', '')} · {fmt_ts(int(meta.get('duration_s', 0) * 1000))}", ""]
    if n.get("summary"):
        lines += ["## Summary", "", n["summary"], ""]

    actions = [a for a in n.get("action_items", []) if isinstance(a, dict)]
    if actions:
        lines += ["## Action items", "", "| Owner | Task | Due | At |", "|---|---|---|---|"]
        for a in actions:
            cells = [a.get("owner") or "Unassigned", a.get("task") or "", a.get("due") or "", a.get("timestamp") or ""]
            lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
        lines.append("")

    for key, heading in (("decisions", "Decisions"), ("key_points", "Key points"),
                         ("open_questions", "Open questions"), ("risks", "Risks")):
        body = _bullets(n.get(key, []))
        if body:
            lines += [f"## {heading}", "", *body, ""]

    for key, heading in (("on_screen", "Shown on screen"), ("key_moments", "Key moments")):
        rows = [r for r in n.get(key, []) if isinstance(r, dict)]
        if rows:
            lines += [f"## {heading}", "", *[f"- `{r.get('timestamp', '')}` {r.get('what', '')}" for r in rows], ""]

    speakers = [s for s in n.get("speakers", []) if isinstance(s, dict)]
    if speakers:
        lines += ["## Speakers", ""]
        lines += [f"- {s.get('label')}: {s.get('name') or 'unknown'}" for s in speakers]
        lines.append("")

    if files:
        lines += ["## Files", "", *[f"- {label}: `{path}`" for label, path in files.items()], ""]
    lines += ["---", "_Generated by meetcap from an automatic recording. Check against the recording before sharing._"]
    return "\n".join(lines) + "\n"

"""Meeting notes library: a local, searchable copy of every meeting's notes, key moments and transcript.

Source of truth stays in Azure Blob. The library keeps only the small JSON files locally
(meta, notes, transcript text, a few screenshots) so browsing and search are instant and work offline.
Audio is fetched from Blob the first time you press play, converted to AAC (plays in every browser)
and kept in a size-capped cache. New meetings are added straight from the pipeline; `sync()` pulls
anything recorded elsewhere or before the library existed.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import STATE_DIR

log = logging.getLogger(__name__)

LIBRARY_DIR = STATE_DIR / "library"
NOTE_LIST_KEYS = ("key_points", "decisions", "open_questions", "risks")


def _slug(prefix: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix.strip("/")) or "meeting"


def _ts_to_seconds(ts: str | None) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    parts = ts.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _phrases(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep what the reader needs: start/end (s), speaker, text."""
    out = []
    for p in transcript.get("phrases", []):
        start = int(p.get("start_ms", 0))
        out.append({"t": round(start / 1000, 2), "end": round((start + int(p.get("duration_ms", 0))) / 1000, 2),
                    "speaker": p.get("speaker"), "text": p.get("text", "")})
    return out


def _search_text(notes: dict[str, Any]) -> str:
    bits = [notes.get("title", ""), notes.get("summary", "")]
    for k in NOTE_LIST_KEYS:
        bits += [str(x) for x in notes.get(k, []) or []]
    for a in notes.get("action_items", []) or []:
        if isinstance(a, dict):
            bits += [str(a.get("owner", "")), str(a.get("task", ""))]
    for k in ("key_moments", "on_screen"):
        for m in notes.get(k, []) or []:
            if isinstance(m, dict):
                bits.append(str(m.get("what", "")))
    return " ".join(bits).lower()


class MeetingLibrary:
    def __init__(self, root: Path = LIBRARY_DIR, blob_factory: Callable[[], Any] | None = None,
                 blob_prefix: str = "", audio_cache_mb: int = 1024, jobs_fn: Callable[[], list] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._blob_factory = blob_factory
        self._blob = None
        self.blob_prefix = blob_prefix.strip("/")
        self.audio_cache_bytes = audio_cache_mb * 1024 * 1024
        self._jobs_fn = jobs_fn
        self._lock = threading.RLock()
        self._transcript_text: dict[str, str] = {}
        self.last_sync: float | None = None
        self.last_sync_error = ""

    # -- storage -----------------------------------------------------------------------
    @property
    def blob(self):
        if self._blob is None:
            if self._blob_factory is None:
                raise RuntimeError("Azure storage isn't configured yet (run: meetcap azure-setup)")
            self._blob = self._blob_factory()
        return self._blob

    def _dir(self, meeting_id: str) -> Path | None:
        for d in self.root.iterdir() if self.root.exists() else []:
            meta = d / "meta.json"
            if meta.exists():
                try:
                    if json.loads(meta.read_text()).get("id") == meeting_id:
                        return d
                except ValueError:
                    continue
        return None

    def _write(self, prefix: str, meta: dict, notes: dict, transcript: dict | None) -> Path:
        d = self.root / _slug(prefix)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        (d / "notes.json").write_text(json.dumps(notes, indent=2, ensure_ascii=False))
        if transcript is not None:
            (d / "transcript.json").write_text(json.dumps({"phrases": _phrases(transcript)}, ensure_ascii=False))
        self._transcript_text.pop(meta.get("id", ""), None)
        return d

    def store_local(self, meta: dict, notes: dict, transcript: dict | None = None,
                    frames: list[tuple[Path, str]] | None = None, audio: Path | None = None) -> Path:
        """Called by the pipeline when a meeting's notes are ready (files are still on disk then)."""
        with self._lock:
            d = self._write(meta.get("blob_prefix") or meta.get("id", "meeting"), meta, notes, transcript)
            if frames:
                (d / "frames").mkdir(exist_ok=True)
                for src, name in frames:
                    if Path(src).exists():
                        shutil.copyfile(src, d / "frames" / name)
            if audio and Path(audio).exists():
                try:
                    self._to_m4a(Path(audio), d / "audio.m4a")
                    self._trim_audio_cache()
                except Exception as exc:  # noqa: BLE001 - playback falls back to fetching from Blob
                    log.warning("library audio copy failed: %s", exc)
            return d

    # -- sync from Blob ------------------------------------------------------------------
    def sync(self) -> int:
        """Pull meetings that are in Blob but not here yet. Returns how many were added."""
        added = 0
        with self._lock:
            try:
                names = self.blob.list_names(self.blob_prefix)
                have = {json.loads((d / "meta.json").read_text()).get("blob_prefix")
                        for d in self.root.iterdir() if (d / "meta.json").exists()}
                for name in names:
                    if not name.endswith("/meta.json"):
                        continue
                    prefix = name[: -len("/meta.json")]
                    if prefix in have or f"{prefix}/notes.json" not in names:
                        continue
                    meta = json.loads(self.blob.download_bytes(name))
                    meta.setdefault("blob_prefix", prefix)
                    notes = json.loads(self.blob.download_bytes(f"{prefix}/notes.json"))
                    transcript = None
                    if f"{prefix}/transcript.json" in names:
                        transcript = json.loads(self.blob.download_bytes(f"{prefix}/transcript.json"))
                    self._write(prefix, meta, notes, transcript)
                    added += 1
                self.last_sync, self.last_sync_error = time.time(), ""
            except Exception as exc:  # noqa: BLE001 - shown in the UI; local notes still work
                self.last_sync_error = str(exc)[:300]
                log.warning("library sync failed: %s", exc)
        if added:
            log.info("library: added %d meeting(s) from Azure", added)
        return added

    # -- reading -------------------------------------------------------------------------
    def _entries(self) -> list[tuple[Path, dict, dict]]:
        out = []
        for d in self.root.iterdir() if self.root.exists() else []:
            try:
                meta = json.loads((d / "meta.json").read_text())
                notes = json.loads((d / "notes.json").read_text())
            except (OSError, ValueError):
                continue
            out.append((d, meta, notes))
        out.sort(key=lambda e: e[1].get("started_at", 0), reverse=True)
        return out

    def _transcript_blob(self, d: Path, meeting_id: str) -> str:
        if meeting_id not in self._transcript_text:
            try:
                ph = json.loads((d / "transcript.json").read_text()).get("phrases", [])
                self._transcript_text[meeting_id] = " ".join(p.get("text", "") for p in ph).lower()
            except (OSError, ValueError):
                self._transcript_text[meeting_id] = ""
        return self._transcript_text[meeting_id]

    def list(self, q: str = "") -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"\s+", q.lower().strip()) if t]
        rows = []
        for d, meta, notes in self._entries():
            mid = meta.get("id", d.name)
            score, where = 0, []
            if terms:
                notes_text = _search_text(notes)
                tx = self._transcript_blob(d, mid)
                if not all(t in notes_text or t in tx for t in terms):
                    continue
                score = sum(notes_text.count(t) * 3 + tx.count(t) for t in terms)
                where = ["notes"] if any(t in notes_text for t in terms) else []
                if any(t in tx for t in terms):
                    where.append("transcript")
            rows.append({
                "id": mid, "title": notes.get("title") or meta.get("title") or "Untitled meeting",
                "summary": notes.get("summary", ""), "started_at": meta.get("started_at"),
                "duration_s": meta.get("duration_s", 0), "app": meta.get("app", ""),
                "actions": len(notes.get("action_items", []) or []),
                "moments": len(notes.get("key_moments", []) or []),
                "has_video": bool(meta.get("has_video")), "score": score, "matched_in": where, "status": "ready",
            })
        if terms:
            rows.sort(key=lambda r: (-r["score"], -(r["started_at"] or 0)))
        return rows

    def processing(self) -> list[dict[str, Any]]:
        """Meetings still on their way (recorded, not yet transcribed/summarised)."""
        if not self._jobs_fn:
            return []
        out = []
        for j in self._jobs_fn():
            status = getattr(j.status, "value", str(j.status))
            if status in ("pending", "failed"):
                out.append({"id": j.id, "title": j.data.get("title") or "Processing…", "started_at": j.started_at,
                            "duration_s": j.data.get("duration_s", 0), "app": j.app, "status": status,
                            "stage": getattr(j.stage, "value", str(j.stage)), "error": j.last_error[:200]})
        return out

    def get(self, meeting_id: str) -> dict[str, Any] | None:
        d = self._dir(meeting_id)
        if d is None:
            return None
        meta = json.loads((d / "meta.json").read_text())
        notes = json.loads((d / "notes.json").read_text())
        try:
            phrases = json.loads((d / "transcript.json").read_text()).get("phrases", [])
        except (OSError, ValueError):
            phrases = []
        names = {str(s.get("label")): s.get("name") for s in notes.get("speakers", []) or []
                 if isinstance(s, dict) and s.get("name")}
        for p in phrases:
            label = f"S{p['speaker']}" if p.get("speaker") is not None else "Speaker"
            p["who"] = names.get(label) or label
        for key in ("key_moments", "on_screen", "action_items"):
            for item in notes.get(key, []) or []:
                if isinstance(item, dict):
                    item["seconds"] = _ts_to_seconds(item.get("timestamp"))
        frames = sorted(p.name for p in (d / "frames").glob("*.jpg")) if (d / "frames").exists() else []
        return {"meta": meta, "notes": notes, "transcript": phrases, "frames": frames,
                "audio_cached": (d / "audio.m4a").exists()}

    # -- media ---------------------------------------------------------------------------
    @staticmethod
    def _to_m4a(src: Path, dst: Path) -> Path:
        from . import media

        tmp = dst.with_suffix(".partial.m4a")
        media._run([media._tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn",
                    "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(tmp)])
        tmp.replace(dst)
        return dst

    def audio_file(self, meeting_id: str) -> Path | None:
        d = self._dir(meeting_id)
        if d is None:
            return None
        out = d / "audio.m4a"
        if out.exists():
            out.touch()  # most recently used
            return out
        prefix = json.loads((d / "meta.json").read_text()).get("blob_prefix", "")
        raw = d / "audio.ogg"
        raw.write_bytes(self.blob.download_bytes(f"{prefix}/audio.ogg"))
        try:
            self._to_m4a(raw, out)
        finally:
            raw.unlink(missing_ok=True)
        self._trim_audio_cache(keep=out)
        return out

    def frame_file(self, meeting_id: str, name: str) -> Path | None:
        if not re.fullmatch(r"[0-9A-Za-z_-]+\.jpg", name):
            return None
        d = self._dir(meeting_id)
        if d is None:
            return None
        f = d / "frames" / name
        if not f.exists():
            prefix = json.loads((d / "meta.json").read_text()).get("blob_prefix", "")
            f.parent.mkdir(exist_ok=True)
            f.write_bytes(self.blob.download_bytes(f"{prefix}/frames/{name}"))
        return f

    def video_blob(self, meeting_id: str) -> tuple[str, int] | None:
        """(blob name, size) of the screen recording, streamed straight from Blob (never cached)."""
        d = self._dir(meeting_id)
        if d is None:
            return None
        meta = json.loads((d / "meta.json").read_text())
        if not meta.get("has_video"):
            return None
        prefix = meta.get("blob_prefix", "")
        for name in (f"{prefix}/recording.mp4", f"{prefix}/recording.mov"):
            size = self.blob.size(name)
            if size:
                return name, size
        return None

    def _trim_audio_cache(self, keep: Path | None = None) -> None:
        files = sorted(self.root.glob("*/audio.m4a"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= self.audio_cache_bytes:
                break
            if keep is not None and p == keep:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

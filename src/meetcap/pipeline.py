"""Processing pipeline: one handler per Stage, each idempotent and checkpointed to the JobStore.

finalize -> extract_audio -> upload_media -> transcribe -> publish_transcript
         -> extract_frames -> summarize -> publish_notes -> cleanup -> done

A failing stage is retried with exponential backoff (30 s .. 1 h). After `max_attempts` the job is
dead-lettered (status=failed) and the user is notified; `meetcap retry <id>` resumes from that stage.
Local media is deleted only in `cleanup`, after every uploaded blob is re-checked by size.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__, media, render
from .config import WORK_DIR, Config
from .jobs import JobStore
from .models import Frame, Job, Stage, Status, Transcript, next_stage
from .notify import notify as default_notify
from .storage import BlobStore, StorageError
from .summarize import Summarizer, normalize_notes
from .transcribe import Transcriber, merge_speaker_tracks

log = logging.getLogger(__name__)

APP_SHORT = {"com.microsoft.teams2": "teams", "com.microsoft.teams": "teams", "us.zoom.xos": "zoom",
             "Cisco-Systems.Spark": "webex", "com.cisco.webexmeetingsapp": "webex",
             "com.google.Chrome": "chrome", "com.microsoft.edgemac": "edge"}


SILENT_DB = -80.0  # AAC digital silence decodes to about -91 dBFS; any real room noise is far louder


class PermanentError(RuntimeError):
    """Retrying cannot help (corrupt file, no audio). The job is dead-lettered immediately."""


def app_short(app: str) -> str:
    return APP_SHORT.get(app) or re.sub(r"[^a-z0-9]+", "-", app.split(".")[-1].lower()).strip("-") or "meeting"


def app_label(app: str) -> str:
    return {"teams": "Teams", "zoom": "Zoom", "webex": "Webex", "chrome": "Chrome", "edge": "Edge",
            "manual": "Recorded"}.get(app_short(app), app_short(app).capitalize())


def safe_filename(s: str, limit: int = 80) -> str:
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)[:limit].strip() or "Meeting"


class Pipeline:
    def __init__(self, cfg: Config, store: JobStore, blob: BlobStore, transcriber: Transcriber,
                 summarizer: Summarizer, notifier: Callable[[str, str], None] = default_notify,
                 work_root: Path = WORK_DIR, clock: Callable[[], float] = time.time, library=None,
                 on_ready: Callable[[Job], None] | None = None):
        self.cfg, self.store, self.blob = cfg, store, blob
        self.transcriber, self.summarizer = transcriber, summarizer
        self.notify, self.work_root, self.clock = notifier, work_root, clock
        self.library = library  # MeetingLibrary: gets each finished meeting straight away
        self.on_ready = on_ready  # e.g. open the notes reader

    # -- driver -------------------------------------------------------------------
    def run(self, job: Job, should_yield: Callable[[], bool] | None = None) -> Job:
        """Run stages until done, failed, or `should_yield()` asks us to pause (a call went live)."""
        if job.status == Status.PENDING and job.stage == Stage.DONE:  # never let a finished job block the queue
            job.status = Status.DONE
            return self.store.save(job)
        while job.status == Status.PENDING and job.stage != Stage.DONE:
            if should_yield and should_yield():
                log.info("job %s paused before %s", job.id, job.stage.value)
                return job
            stage = job.stage
            started = time.monotonic()
            try:
                nxt = getattr(self, f"_{stage.value}")(job)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
                log.exception("job %s failed at %s", job.id, stage.value)
                attempts = 1 if isinstance(exc, PermanentError) else self.cfg.processing.max_attempts
                job = self.store.fail(job, f"{stage.value}: {type(exc).__name__}: {exc}", attempts, self.clock())
                if job.status == Status.FAILED:
                    self.notify("Meeting processing failed", f"{stage.value}: {exc}. Run: meetcap retry {job.id}")
                return job
            log.info("job %s: %s done in %.1fs", job.id, stage.value, time.monotonic() - started)
            job = self.store.advance(job, nxt)
        return job

    # -- helpers ------------------------------------------------------------------
    def work_dir(self, job: Job) -> Path:
        d = self.work_root / job.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _prefix(self, job: Job) -> str:
        dt = datetime.fromtimestamp(job.started_at)
        parts = [self.cfg.blob_prefix, dt.strftime("%Y/%m/%d"),
                 f"{dt:%H%M}_{app_short(job.app)}_{job.id[:6]}"]
        return "/".join(p for p in parts if p)

    def _meta(self, job: Job) -> dict[str, Any]:
        return {
            "id": job.id, "app": job.app, "title": job.data.get("title", ""),
            "started_at": job.started_at, "ended_at": job.ended_at,
            "started_at_iso": datetime.fromtimestamp(job.started_at).astimezone().isoformat(timespec="seconds"),
            "duration_s": job.data.get("duration_s", 0), "blob_prefix": job.blob_prefix,
            "has_video": job.data.get("has_video", False), "host": socket.gethostname(),
            "meetcap_version": __version__,
        }

    def _put_text(self, job: Job, name: str, text: str) -> None:
        r = self.blob.upload_text(text, f"{job.blob_prefix}/{name}")
        job.data.setdefault("uploads", {})[name] = {"blob": r.blob, "size": r.size, "md5": r.md5}

    def _put_file(self, job: Job, name: str, path: Path) -> None:
        uploads = job.data.setdefault("uploads", {})
        blob = f"{job.blob_prefix}/{name}"
        prev = uploads.get(name)
        if prev and prev.get("size") == path.stat().st_size and self.blob.size(blob) == prev["size"]:
            return  # already uploaded by an earlier attempt
        r = self.blob.upload_file(path, blob, metadata={"meetcap_job": job.id, "app": app_short(job.app)})
        uploads[name] = {"blob": r.blob, "size": r.size, "md5": r.md5}
        self.store.save(job)  # checkpoint between large uploads

    def _transcript(self, job: Job) -> Transcript:
        return Transcript.from_dict(json.loads(Path(job.data["transcript_path"]).read_text()))

    # -- stages -------------------------------------------------------------------
    def _finalize(self, job: Job) -> Stage:
        src = Path(job.source_path)
        if not src.exists():
            raise FileNotFoundError(f"recording not found: {src}")
        media.wait_until_stable(src)
        duration = media.duration_seconds(src)  # 0.0 = unknown (e.g. crash-truncated file), never "short"
        job.data["duration_s"] = duration
        auto = job.owned and job.app not in ("manual", "import", "recovered")
        if auto and 0 < duration < self.cfg.detection.min_meeting_seconds:
            if self.cfg.processing.delete_local_after_upload:
                src.unlink(missing_ok=True)
            job.status = Status.SKIPPED
            job.last_error = f"only {duration:.0f}s long; discarded"
            return Stage.DONE
        if not media.has_stream(src, "audio"):
            raise PermanentError("file has no audio stream")
        loudest = media.max_volume_db(src, stream=0 if media.audio_stream_count(src) > 1 else None)
        job.data["max_volume_db"] = loudest
        if loudest <= SILENT_DB:
            msg = ("The recording has no sound at all, so OBS isn't capturing audio. Allow OBS in System Settings > "
                   "Privacy & Security > Microphone and Screen & System Audio Recording, quit OBS, then run "
                   "`meetcap setup-obs`.")
            self.notify("Recording was silent", "OBS captured no sound. Run: meetcap setup-obs", sound=True)
            raise PermanentError(msg)
        # Video intent is captured when the recording is made, so a later config change can't cause the
        # recording to be deleted without having been uploaded.
        wanted = job.data.get("video", True) if job.owned else True
        job.data["has_video"] = bool(wanted) and media.has_stream(src, "video")
        if job.data["has_video"] and media.mostly_black(src):
            job.data["has_video"] = False  # nothing to see; don't upload or send screenshots
            job.data["video_black"] = True
            self.notify("Screen wasn't captured",
                        "The video is black. Allow OBS under Screen & System Audio Recording. Audio is fine.")
        job.blob_prefix = job.blob_prefix or self._prefix(job)
        return next_stage(Stage.FINALIZE)

    def _extract_audio(self, job: Job) -> Stage:
        src, wd, rate = Path(job.source_path), self.work_dir(job), self.cfg.processing.audio_bitrate
        audio = wd / "audio.ogg"
        if not audio.exists():
            media.extract_audio(src, audio, rate, stream=0 if media.audio_stream_count(src) > 1 else None)
        job.data["audio_path"] = str(audio)
        # OBS track 2 = your mic, track 3 = meeting audio (set up by `meetcap setup-obs`).
        if self.cfg.split_speakers and job.owned and media.audio_stream_count(src) >= 3:
            mine, others = wd / "you.ogg", wd / "others.ogg"
            if not mine.exists():
                media.extract_audio(src, mine, rate, stream=1)
            if not others.exists():
                media.extract_audio(src, others, rate, stream=2)
            job.data["tracks"] = {"you": str(mine), "others": str(others)}
        return next_stage(Stage.EXTRACT_AUDIO)

    def _upload_media(self, job: Job) -> Stage:
        src = Path(job.source_path)
        if job.data.get("has_video"):
            self._put_file(job, f"recording{src.suffix.lower()}", src)
        self._put_file(job, "audio.ogg", Path(job.data["audio_path"]))
        return next_stage(Stage.UPLOAD_MEDIA)

    def _transcribe(self, job: Job) -> Stage:
        out = self.work_dir(job) / "transcript.json"
        if not out.exists():
            tracks = job.data.get("tracks")
            if self.transcriber is None:   # transcription is off: keep the audio, no transcript
                t = Transcript(int(job.data.get("duration_s", 0) * 1000), [], [{"skipped": "transcription is off"}])
            elif tracks:
                t = merge_speaker_tracks(self.transcriber.transcribe(Path(tracks["you"])),
                                         self.transcriber.transcribe(Path(tracks["others"])),
                                         int(job.data.get("duration_s", 0) * 1000) or None)
            else:
                t = self.transcriber.transcribe(Path(job.data["audio_path"]))
            tmp = out.with_suffix(".partial")
            tmp.write_text(json.dumps(t.to_dict()))
            tmp.replace(out)
        job.data["transcript_path"] = str(out)
        return next_stage(Stage.TRANSCRIBE)

    def _publish_transcript(self, job: Job) -> Stage:
        t = self._transcript(job)
        meta = self._meta(job)
        names = self._track_names(job, t)
        self._put_text(job, "transcript.md", render.transcript_markdown(t, meta, names))
        self._put_text(job, "transcript.vtt", render.transcript_vtt(t, names))
        self._put_file(job, "transcript.json", Path(job.data["transcript_path"]))
        return next_stage(Stage.PUBLISH_TRANSCRIPT)

    def _track_names(self, job: Job, t: Transcript) -> dict[str, str]:
        """With split tracks, S1 is certainly the recorder; S2 is 'Others' unless the engine told them apart."""
        if not job.data.get("tracks"):
            return {}
        names = {"S1": self.cfg.processing.user_name or "You"}
        if {p.speaker for p in t.phrases if p.speaker and p.speaker >= 2} <= {2}:
            names["S2"] = "Others"
        return names

    def _extract_frames(self, job: Job) -> Stage:
        frames: list[Frame] = []
        if self.cfg.processing.screen_insights and job.data.get("has_video"):
            try:
                frames = media.extract_scene_frames(Path(job.source_path), self.work_dir(job) / "frames",
                                                    self.cfg.processing.max_frames)
            except Exception as exc:  # noqa: BLE001 - screenshots are a nice-to-have
                log.warning("frame extraction failed for %s: %s", job.id, exc)
        job.data["frames"] = [{"path": f.path, "t_ms": f.t_ms} for f in frames]
        return next_stage(Stage.EXTRACT_FRAMES)

    def _summarize(self, job: Job) -> Stage:
        out = self.work_dir(job) / "notes.json"
        if not out.exists():
            t = self._transcript(job)
            text = render.transcript_for_llm(t)
            when = datetime.fromtimestamp(job.started_at)
            plain_title = f"{app_label(job.app)} meeting, {when:%a %-d %b %H:%M}"
            if self.transcriber is None:
                notes = normalize_notes({"title": plain_title, "summary": "Transcription is off, so there's no "
                                         "transcript or notes. The audio is saved; turn transcription on with "
                                         "`meetcap setup`."})
            elif not text.strip():
                notes = normalize_notes({"title": "No speech detected",
                                         "summary": "The recording contains no transcribable speech."})
            elif self.summarizer is None:   # notes are off: transcript only
                notes = normalize_notes({"title": plain_title, "summary": ""})
            else:
                frames = [Frame(**f) for f in job.data.get("frames", []) if Path(f["path"]).exists()]
                meta = {"date": datetime.fromtimestamp(job.started_at).strftime("%Y-%m-%d %H:%M"),
                        "app": app_short(job.app), "duration": render.fmt_ts(t.duration_ms),
                        "recorded_by": self.cfg.processing.user_name or "the user"}
                if job.data.get("tracks"):
                    meta["speaker_hint"] = ("S1 is the recorder's own microphone, so S1 is "
                                            f"{self.cfg.processing.user_name or 'the recorder'}. S2 and higher are "
                                            "the other participants (S2 may be several people).")
                notes = self.summarizer.summarize(text, meta, frames)
            out.write_text(json.dumps(notes, indent=2, ensure_ascii=False))
        notes = json.loads(out.read_text())
        job.data["notes_path"] = str(out)
        job.data["title"] = notes.get("title", "")
        return next_stage(Stage.SUMMARIZE)

    def _publish_notes(self, job: Job) -> Stage:
        notes = json.loads(Path(job.data["notes_path"]).read_text())
        names = {**self._track_names(job, self._transcript(job)), **render.speaker_names(notes)}
        meta = self._meta(job)
        p = job.blob_prefix
        files = {}
        if job.data.get("has_video"):
            files["Recording"] = f"{p}/recording{Path(job.source_path).suffix.lower()}"
        files.update({"Audio": f"{p}/audio.ogg", "Transcript": f"{p}/transcript.md"})
        md = render.notes_markdown(notes, meta, files)

        self._put_text(job, "notes.md", md)
        self._put_text(job, "notes.json", json.dumps(notes, indent=2, ensure_ascii=False))
        # Re-render the transcript now that the title and any names the model could justify are known.
        t = self._transcript(job)
        self._put_text(job, "transcript.md", render.transcript_markdown(t, meta, names))
        self._put_text(job, "transcript.vtt", render.transcript_vtt(t, names))
        for f in job.data.get("frames", []):
            fp = Path(f["path"])
            if fp.exists():
                self._put_file(job, f"frames/{render.fmt_ts(f['t_ms']).replace(':', '-')}.jpg", fp)
        self._put_text(job, "meta.json", json.dumps(meta, indent=2))

        local = None
        if self.cfg.notes_dir:
            self.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
            dt = datetime.fromtimestamp(job.started_at)
            local = self.cfg.notes_dir / f"{dt:%Y-%m-%d %H%M} {safe_filename(notes.get('title', ''))}.md"
            local.write_text(md, encoding="utf-8")
            job.data["local_notes"] = str(local)
        if self.library is not None:
            try:
                frames = [(Path(f["path"]), f"{render.fmt_ts(f['t_ms']).replace(':', '-')}.jpg")
                          for f in job.data.get("frames", [])]
                audio = Path(job.data["audio_path"]) if job.data.get("audio_path") else None
                self.library.store_local(meta, notes, self._transcript(job).to_dict(), frames,
                                         None if self.cfg.storage.backend == "local" else audio)
            except Exception as exc:  # noqa: BLE001 - the notes are safe in Blob; sync will pick them up
                log.warning("could not add %s to the notes library: %s", job.id, exc)
        if self.cfg.storage.backend == "local":
            try:
                from . import reader
                reader.publish_static(Path(self.blob.root), Path(self.blob.root) / p, audio=job.data.get("audio_path"))
            except Exception as exc:  # noqa: BLE001 - the meeting is saved; `meetcap reader` rebuilds the page
                log.warning("could not update the notes reader for %s: %s", job.id, exc)
        self.notify("Meeting notes ready", notes.get("title") or "Untitled meeting")
        if local and self.cfg.processing.open_notes_when_ready:
            subprocess.Popen(["open", str(local)])
        if self.on_ready is not None:
            try:
                self.on_ready(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_ready hook failed: %s", exc)
        return next_stage(Stage.PUBLISH_NOTES)

    def _cleanup(self, job: Job) -> Stage:
        if not self.cfg.processing.delete_local_after_upload:
            return Stage.DONE
        for name, u in job.data.get("uploads", {}).items():
            remote = self.blob.size(u["blob"])
            if remote != u["size"]:
                raise StorageError(f"{name} not verified in Blob (remote={remote}, local={u['size']}); keeping local files")
        src = Path(job.source_path)
        if job.owned and src.exists():
            src.unlink()
        shutil.rmtree(self.work_root / job.id, ignore_errors=True)
        return Stage.DONE

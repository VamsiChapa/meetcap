"""MeetCapAgent: the SDK facade. Detection loop + recording control + background processing worker.

UIs (menu bar, CLI) call only this class. It never blocks the caller for long: recording start/stop
talk to OBS, and all cloud work happens on the worker thread.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import DB_PATH, STATE_DIR, Config, ensure_dirs, problems
from .detect import MeetingDetector, MicSource, MicWatch, match_app
from .jobs import JobStore
from .models import Job, Status
from .notify import notify as default_notify
from . import media
from .pipeline import Pipeline, app_short
from .recorder import FILENAME_PREFIX, ObsRecorder, Recorder
from .vault import get_secret, set_secret

log = logging.getLogger(__name__)

MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv"}
STATUS_PATH = STATE_DIR / "status.json"    # written by the agent every poll; read by `meetcap status`
CONTROL_PATH = STATE_DIR / "control.json"  # written by `meetcap start|stop|auto`; consumed by the agent


@dataclass
class Session:
    app: str
    started_at: float
    manual: bool


@dataclass
class AgentStatus:
    recording: bool
    app: str | None
    since: float | None
    manual: bool
    auto: bool
    detection_available: bool
    counts: dict[str, int] = field(default_factory=dict)
    last_error: str = ""
    processing: bool = False


class MeetCapAgent:
    def __init__(self, cfg: Config, *, store: JobStore | None = None, recorder: Recorder | None = None,
                 mic: MicSource | None = None, pipeline_factory: Callable[[], Pipeline] | None = None,
                 notifier: Callable[..., None] = default_notify, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store or JobStore(DB_PATH)
        self.recorder = recorder or ObsRecorder(cfg, password=lambda: get_secret("obs-password"))
        self.mic = mic or MicWatch(cfg.micwatch_path)
        self.detector = MeetingDetector(cfg.detection.start_after_seconds, cfg.detection.stop_after_seconds)
        self.notify = notifier
        self.clock = clock
        self.auto = cfg.detection.enabled
        self.session: Session | None = None
        self.last_error = ""
        self._pipeline_factory = pipeline_factory or self._default_pipeline
        self._pipeline: Pipeline | None = None
        self._suppressed = False          # user stopped mid-meeting: don't auto-restart until the mic frees up
        self._last_start_attempt = 0.0
        self._last_cloud_warning = 0.0
        self._last_orphan_scan = 0.0
        self._processing = False
        self.pet_request: bool | None = None
        self._last_library_sync = 0.0
        self.web = None
        from .library import MeetingLibrary
        self.library = MeetingLibrary(blob_factory=lambda: self.pipeline().blob,
                                      blob_prefix=cfg.blob_prefix,
                                      audio_cache_mb=cfg.ui.library_audio_cache_mb,
                                      jobs_fn=lambda: self.store.list(limit=50))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- wiring -------------------------------------------------------------------
    def _default_pipeline(self) -> Pipeline:
        issues = problems(self.cfg)
        if issues:
            raise RuntimeError("; ".join(issues))
        from . import providers

        cred = providers._azure_credential(self.cfg)
        return Pipeline(self.cfg, self.store,
                        providers.make_store(self.cfg, get_secret, cred),
                        providers.make_transcriber(self.cfg, get_secret, cred),
                        providers.make_summarizer(self.cfg, get_secret, cred),
                        notifier=self.notify, library=self.library, on_ready=self.open_reader)

    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            self._pipeline = self._pipeline_factory()
        return self._pipeline

    @property
    def detection_available(self) -> bool:
        available = getattr(self.mic, "available", None)
        return bool(available()) if callable(available) else True

    # -- lifecycle ----------------------------------------------------------------
    def start(self) -> None:
        ensure_dirs(self.cfg)
        if not self.detection_available:
            self.last_error = "micwatch helper missing: auto-detect is off (run install.sh)"
            self.notify("meetcap", self.last_error)
        self.recover()
        for target, name in ((self._detect_loop, "detect"), (self._worker_loop, "worker")):
            t = threading.Thread(target=target, name=f"meetcap-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        if self.auto and self.cfg.recording.keep_obs_running:
            threading.Thread(target=self._warm_obs, name="meetcap-obs-warmup", daemon=True).start()
        if self.cfg.ui.library:
            self.start_library()
        if self.cfg.storage.backend == "local":
            threading.Thread(target=self._refresh_reader, name="meetcap-reader", daemon=True).start()

    @property
    def library_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.ui.library_port}"

    def _refresh_reader(self) -> None:
        try:
            from .reader import publish_static
            publish_static(self.cfg.local_root, species=self.cfg.ui.pet_species)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not refresh the notes reader: %s", exc)

    def reader_target(self) -> str:
        """The reader page if it has been written (works without meetcap), else the local library."""
        return str(self.cfg.reader_path) if self.cfg.reader_path.exists() else self.library_url

    def open_reader(self, job: Job | None = None) -> None:
        """Called when a meeting's notes are ready. Backlog processed hours later doesn't pop a tab."""
        if not self.cfg.ui.open_notes_after_meeting:
            return
        if job is not None and job.ended_at and self.clock() - job.ended_at > 3600:
            return
        try:
            subprocess.Popen(["open", self.reader_target()])
        except OSError as exc:
            log.warning("could not open the notes reader: %s", exc)

    def start_library(self) -> None:
        """Serve the notes library on 127.0.0.1 and pull older meetings from Azure in the background."""
        from .webui import serve

        try:
            self.web = serve(self.library, self.cfg.ui.library_port, status_fn=self.status_dict,
                             species_fn=lambda: self.cfg.ui.pet_species)
        except OSError as exc:
            self.last_error = f"notes library: port {self.cfg.ui.library_port} is busy ({exc})"
            log.warning(self.last_error)
            return
        threading.Thread(target=self.library.sync, name="meetcap-library-sync", daemon=True).start()
        self._last_library_sync = self.clock()

    def status_dict(self) -> dict:
        s = self.status()
        return {"recording": s.recording, "since": s.since, "app": s.app, "manual": s.manual, "auto": s.auto,
                "processing": s.processing, "last_error": s.last_error, "counts": s.counts}

    def _warm_obs(self) -> None:
        try:
            self.recorder.ensure_ready()  # OBS idle in the tray, so a meeting starts recording within seconds
        except Exception as exc:  # noqa: BLE001
            log.warning("OBS warm-up failed: %s", exc)

    def _close_obs_later(self, path: Path | None) -> None:
        """Quit OBS after a recording so the mic/screen indicators and headset call mode go away."""
        if self.cfg.recording.keep_obs_running:
            return
        quit_fn = getattr(self.recorder, "close_after_recording", None) or getattr(self.recorder, "quit_obs", None)
        if not callable(quit_fn):
            return

        def run() -> None:
            try:
                if path and path.exists():
                    media.wait_until_stable(path)  # let OBS finish writing the hybrid MP4 first
                with self._lock:
                    if self.session:               # a new meeting started meanwhile; keep OBS
                        return
                    quit_fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not close OBS: %s", exc)

        threading.Thread(target=run, name="meetcap-obs-close", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        if self.web is not None:
            self.web.shutdown()
        if self.session:
            self.stop_recording(user_initiated=False)
        for t in self._threads:
            t.join(timeout=5)

    def set_auto(self, enabled: bool) -> None:
        self.auto = enabled
        if not enabled:
            self.detector.reset()

    # -- recording control ----------------------------------------------------------
    def start_recording(self, app: str = "manual", manual: bool = True) -> bool:
        with self._lock:
            if self.session:
                return False
            self._last_start_attempt = self.clock()
            try:
                self.recorder.start()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"recording did not start: {exc}"
                log.error(self.last_error)
                self.notify("Recording did NOT start", str(exc), sound=True)
                return False
            self.session = Session(app, self.clock(), manual)
            self.last_error = ""
            what = "audio + screen" if self.cfg.recording.video else "audio"
            self.notify("Recording meeting", f"{app_short(app)} · {what}")
            threading.Thread(target=self._check_capture, name="meetcap-capture-check", daemon=True).start()
            return True

    def _check_capture(self, delay: float = 4.0) -> list[str]:
        """A few seconds in, make sure OBS really sees the screen and hears the mic; warn at once if not."""
        check = getattr(self.recorder, "check_capture", None)
        if not callable(check):
            return []
        time.sleep(delay)
        try:
            problems = check(3.0)
        except Exception as exc:  # noqa: BLE001 - a failed check must never stop the recording
            log.debug("capture check failed: %s", exc)
            return []
        if problems and self.session:
            self.last_error = problems[0]
            self.notify("Recording has a problem", problems[0], sound=True)
        return problems

    def stop_recording(self, user_initiated: bool = True) -> Job | None:
        with self._lock:
            s = self.session
            if not s:
                return None
            self.session = None
            if user_initiated and self.detector.active_app:
                self._suppressed = True
            self.detector.reset()
            job = self._finish_file(s)
            self._close_obs_later(Path(job.source_path) if job else None)
            if job:
                later = self.cfg.processing.pause_while_recording
                self.notify("Recording saved",
                            "Processing starts now" if not later else "Uploads resume when no call is live")
            return job

    def _finish_file(self, s: Session) -> Job | None:
        """Stop OBS recording and queue the file. Caller holds the lock."""
        try:
            path = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"stop failed: {exc}"
            self.notify("Recording stop failed", str(exc), sound=True)
            path = None
        if not path:
            return None
        return self.store.add(Job(source_path=str(path), started_at=s.started_at, ended_at=self.clock(),
                                  app=s.app, data={"owned": True, "manual": s.manual,
                                                   "video": self.cfg.recording.video
                                                   and bool(getattr(self.recorder, "video_available", True))}))

    def _rollover(self) -> None:
        """Close the current file and keep recording into a new one. Detector state is untouched."""
        with self._lock:
            s = self.session
            if not s:
                return
            log.info("max recording length reached; rolling over to a new file")
            self._finish_file(s)
            try:
                self.recorder.start()
                self.session = Session(s.app, self.clock(), s.manual)
            except Exception as exc:  # noqa: BLE001
                self.session = None
                self.last_error = f"rollover restart failed: {exc}"
                self.notify("Recording did NOT restart", str(exc), sound=True)

    # -- detection ------------------------------------------------------------------
    def _detect_loop(self) -> None:
        while not self._stop.wait(min(self.cfg.detection.poll_seconds, 2.0)):
            try:
                self.handle_control()
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("detection tick failed")
            finally:
                self.write_status()

    # -- control from Terminal (no menu bar needed) -----------------------------------
    def handle_control(self, path: Path | None = None) -> str | None:
        path = path or CONTROL_PATH
        try:
            cmd = json.loads(path.read_text()).get("cmd")
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return None
        log.info("control command: %s", cmd)
        if cmd == "start":
            self.start_recording("manual", manual=True)
        elif cmd == "stop":
            self.stop_recording()
        elif cmd in ("auto-on", "auto-off"):
            self.set_auto(cmd == "auto-on")
        elif cmd in ("pet-on", "pet-off"):
            self.pet_request = cmd == "pet-on"   # picked up by the menu bar (the pet lives on its thread)
        return cmd

    def write_status(self, path: Path | None = None) -> None:
        path = path or STATUS_PATH
        s = self.status()
        data = {"updated": self.clock(), "pid": os.getpid(), "recording": s.recording, "app": s.app,
                "since": s.since, "manual": s.manual, "auto": s.auto, "detection": s.detection_available,
                "counts": s.counts, "last_error": s.last_error, "processing": s.processing}
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not write status: %s", exc)

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        s = self.session
        if s and now - s.started_at > self.cfg.detection.max_meeting_hours * 3600:
            self._rollover()
            return
        if not self.detection_available or not (self.auto or (s and not s.manual)):
            return
        try:
            bundles = self.mic.active_bundles()
        except Exception as exc:  # noqa: BLE001
            log.warning("micwatch failed: %s", exc)
            return
        app = match_app(bundles, [*self.cfg.detection.apps, *self.cfg.detection.browsers])
        if self._suppressed:
            if app is None:
                self._suppressed = False
            return
        event = self.detector.update(app, now)
        if event and event.kind == "start" and self.auto and not self.session:
            self.start_recording(event.app, manual=False)
        elif event and event.kind == "stop" and self.session and not self.session.manual:
            self.stop_recording(user_initiated=False)
        elif (not event and self.detector.active_app and not self.session and self.auto
              and now - self._last_start_attempt > 30):
            self.start_recording(self.detector.active_app, manual=False)  # retry a failed start

    # -- processing -----------------------------------------------------------------
    def _busy(self) -> bool:
        return bool(self.cfg.processing.pause_while_recording and self.session)

    def _worker_loop(self) -> None:
        while not self._stop.wait(5):
            try:
                if not self.session and self.clock() - self._last_orphan_scan > 600:
                    self._last_orphan_scan = self.clock()
                    self.scan_orphans()
                if self.web is not None and not self.session and self.clock() - self._last_library_sync > 600:
                    self._last_library_sync = self.clock()
                    self.library.sync()
                self.process_due()
            except Exception:  # noqa: BLE001
                log.exception("worker iteration failed")

    def process_due(self) -> Job | None:
        if self._busy() or self.store.next_due(self.clock()) is None:
            return None
        try:
            pipeline = self.pipeline()
        except Exception as exc:  # noqa: BLE001 - recording keeps working without cloud config
            self.last_error = f"cloud processing not configured: {exc}"
            if self.clock() - self._last_cloud_warning > 600:
                log.warning(self.last_error)
                self._last_cloud_warning = self.clock()
            return None
        job = self.store.claim_next(self.clock())
        if not job:
            return None
        self._processing = True
        try:
            return pipeline.run(job, should_yield=self._busy)
        finally:
            self._processing = False
            self.store.release(job)

    def process_file(self, path: Path, app: str = "import") -> Job:
        """Queue any existing recording (a Teams download, a voice memo). The source file is never deleted."""
        path = Path(path).expanduser().resolve()
        st = path.stat()
        started = getattr(st, "st_birthtime", st.st_mtime)
        return self.store.add(Job(source_path=str(path), started_at=started, ended_at=st.st_mtime, app=app,
                                  data={"owned": False}))

    def retry_failed(self) -> int:
        failed = self.store.list(Status.FAILED, limit=1000)
        for j in failed:
            self.store.retry(j)
        return len(failed)

    def recover(self) -> int:
        """Adopt a recording left running by a previous meetcap process, then queue orphaned files."""
        n = 0
        try:
            if not self.session and self.recorder.is_recording():
                path = self.recorder.stop()
                if path:
                    log.info("recovered in-flight recording %s", path)
                    now = self.clock()
                    self.store.add(Job(source_path=str(path), started_at=now, ended_at=now, app="recovered",
                                       data={"owned": True}))
                    n += 1
                self._close_obs_later(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("recovery check failed: %s", exc)
        return n + self.scan_orphans()

    def scan_orphans(self, min_age: float = 120.0) -> int:
        """Queue meetcap_* files in output_dir that no job knows about (OBS or meetcap crashed mid-call).

        Only files carrying OBS's meetcap_ filename prefix are adopted, so pointing output_dir at a folder
        with personal videos can never get them uploaded and deleted.
        """
        if self.session or not self.cfg.output_dir.exists():
            return 0
        n = 0
        now = self.clock()
        for f in sorted(self.cfg.output_dir.iterdir()):
            if (f.suffix.lower() not in MEDIA_SUFFIXES or not f.is_file()
                    or not f.name.startswith(FILENAME_PREFIX)):
                continue
            st = f.stat()
            if now - st.st_mtime < min_age or self.store.by_source(str(f)):
                continue
            self.store.add(Job(source_path=str(f), started_at=getattr(st, "st_birthtime", st.st_mtime),
                               ended_at=st.st_mtime, app="recovered", data={"owned": True}))
            n += 1
        if n:
            log.info("queued %d orphaned recording(s)", n)
        return n

    # -- status ---------------------------------------------------------------------
    def status(self) -> AgentStatus:
        s = self.session
        return AgentStatus(recording=bool(s), app=s.app if s else None, since=s.started_at if s else None,
                           manual=bool(s and s.manual), auto=self.auto,
                           detection_available=self.detection_available,
                           counts=self.store.counts(), last_error=self.last_error, processing=self._processing)


def setup_obs(cfg: Config, on_step: Callable[[str], None] | None = None) -> list[str]:
    """One-time OBS configuration (profile, scene, sources, websocket). Used by `meetcap setup-obs`."""
    ensure_dirs(cfg)
    rec = ObsRecorder(cfg, password=lambda: get_secret("obs-password"))
    return rec.setup(lambda pw: set_secret("obs-password", pw), on_step=on_step)

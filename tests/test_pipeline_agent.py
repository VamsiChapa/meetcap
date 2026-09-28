import json
import time
from pathlib import Path

import pytest

from meetcap import media
from meetcap.agent import MeetCapAgent
from meetcap.config import Config
from meetcap.jobs import JobStore
from meetcap.models import Job, Stage, Status, Transcript
from meetcap.pipeline import Pipeline
from meetcap.storage import UploadResult
from meetcap.transcribe import parse_phrases
from tests.test_render_transcribe import RAW


class FakeBlob:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_next = 0

    def upload_file(self, local, blob, metadata=None):
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("network down")
        data = Path(local).read_bytes()
        self.objects[blob] = data
        return UploadResult(blob, len(data), "md5")

    def upload_text(self, text, blob):
        self.objects[blob] = text.encode()
        return UploadResult(blob, len(self.objects[blob]), "md5")

    def size(self, blob):
        return len(self.objects[blob]) if blob in self.objects else None

    def url(self, blob):
        return f"https://acct/{blob}"


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        assert audio.exists() and audio.suffix == ".ogg"
        return Transcript(12000, parse_phrases(RAW), [RAW])


class FakeSummarizer:
    def __init__(self):
        self.frames = None

    def summarize(self, text, meta, frames):
        self.frames = frames
        assert "S1:" in text and meta["recorded_by"] == "Alex"
        return {"title": "Loan / pilot: next steps", "summary": "Short.", "speakers": [{"label": "S2", "name": "Mary"}],
                "key_points": [], "decisions": ["Proceed"], "action_items": [], "open_questions": [], "risks": [],
                "on_screen": [], "key_moments": []}


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.recording.output_dir = str(tmp_path / "rec")
    cfg.processing.user_name = "Alex"
    cfg.processing.notes_dir = str(tmp_path / "notes")
    cfg.storage.backend = "azure"
    cfg.azure.storage.prefix = "alex"
    cfg.detection.min_meeting_seconds = 5
    return cfg


def _pipeline(tmp_path, cfg=None):
    cfg = cfg or _cfg(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3")
    blob, tr, sm, notes = FakeBlob(), FakeTranscriber(), FakeSummarizer(), []
    p = Pipeline(cfg, store, blob, tr, sm, notifier=lambda *a, **k: notes.append(a), work_root=tmp_path / "work")
    return p, store, blob, tr, sm, notes


def test_media_helpers(sample_video, tmp_path):
    assert 11 <= media.duration_seconds(sample_video) <= 13
    assert media.has_stream(sample_video, "audio") and media.has_stream(sample_video, "video")
    ogg = media.extract_audio(sample_video, tmp_path / "a.ogg")
    assert ogg.exists() and not media.has_stream(ogg, "video")
    frames = media.extract_scene_frames(sample_video, tmp_path / "frames", max_frames=5)
    assert 1 <= len(frames) <= 5
    assert any(4500 <= f.t_ms <= 7500 for f in frames), [f.t_ms for f in frames]  # the cut at 6 s
    parts = media.split_audio(ogg, tmp_path / "parts", 5)
    assert [round(o) for _, o in parts] == [0, 5, 10]


def test_full_pipeline_happy_path(sample_video, tmp_path):
    p, store, blob, tr, sm, notes = _pipeline(tmp_path)
    job = store.add(Job(source_path=str(sample_video), started_at=time.time() - 60, ended_at=time.time(),
                        app="com.microsoft.teams2", data={"owned": True}))
    job = p.run(job)
    assert job.status == Status.DONE, job.last_error
    prefix = job.blob_prefix
    assert prefix.startswith("alex/") and "_teams_" in prefix
    for name in ("recording.mp4", "audio.ogg", "transcript.md", "transcript.vtt", "transcript.json",
                 "notes.md", "notes.json", "meta.json"):
        assert f"{prefix}/{name}" in blob.objects, name
    assert any(k.startswith(f"{prefix}/frames/") for k in blob.objects)
    assert sm.frames  # screenshots reached the summarizer
    assert "Mary (S2)" in blob.objects[f"{prefix}/transcript.md"].decode()
    assert "# Loan / pilot: next steps" in blob.objects[f"{prefix}/notes.md"].decode()
    assert json.loads(blob.objects[f"{prefix}/meta.json"])["title"] == "Loan / pilot: next steps"
    local = list((tmp_path / "notes").glob("*.md"))
    assert len(local) == 1 and "Loan pilot next steps" in local[0].name  # slash made filename-safe
    assert not sample_video.exists() and not (tmp_path / "work" / job.id).exists()  # cleaned up
    assert notes[-1][0] == "Meeting notes ready"


def test_imported_file_is_never_deleted(sample_video, tmp_path):
    p, store, *_ = _pipeline(tmp_path)
    job = store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="import", data={"owned": False}))
    assert p.run(job).status == Status.DONE
    assert sample_video.exists()


def test_short_auto_recording_is_skipped(sample_video, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.detection.min_meeting_seconds = 60
    p, store, blob, *_ = _pipeline(tmp_path, cfg)
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="us.zoom.xos")))
    assert job.status == Status.SKIPPED and not blob.objects and not sample_video.exists()


def test_upload_failure_retries_from_same_stage(sample_video, tmp_path):
    p, store, blob, tr, *_ = _pipeline(tmp_path)
    blob.fail_next = 1
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="manual")))
    assert job.status == Status.PENDING and job.stage == Stage.UPLOAD_MEDIA and job.attempts == 1
    assert "network down" in job.last_error and sample_video.exists()
    job.next_attempt_at = 0
    job = p.run(store.save(job))
    assert job.status == Status.DONE and tr.calls == 1


def test_pipeline_yields_when_call_goes_live(sample_video, tmp_path):
    p, store, *_ = _pipeline(tmp_path)
    job = store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="manual"))
    job = p.run(job, should_yield=lambda: True)
    assert job.status == Status.PENDING and job.stage == Stage.FINALIZE


# -- agent ------------------------------------------------------------------------
class FakeRecorder:
    def __init__(self, out: Path):
        self.out, self.recording, self.starts, self.fail = out, False, 0, False

    def ensure_ready(self):
        pass

    def start(self):
        if self.fail:
            raise RuntimeError("OBS permission denied")
        self.recording, self.starts = True, self.starts + 1

    def stop(self):
        self.recording = False
        path = self.out / f"r{self.starts}.mp4"
        path.write_bytes(b"x")
        return path

    def is_recording(self):
        return self.recording


class FakeMic:
    def __init__(self):
        self.bundles: set[str] = set()

    def available(self):
        return True

    def active_bundles(self):
        return set(self.bundles)


@pytest.fixture
def agent(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.detection.start_after_seconds, cfg.detection.stop_after_seconds = 3, 20
    (tmp_path / "rec").mkdir()
    events = []
    a = MeetCapAgent(cfg, store=JobStore(tmp_path / "j.sqlite3"), recorder=FakeRecorder(tmp_path / "rec"),
                     mic=FakeMic(), pipeline_factory=lambda: None, notifier=lambda *x, **k: events.append(x),
                     clock=lambda: 0)
    a.events = events
    return a


def test_agent_auto_records_teams_meeting(agent):
    agent.mic.bundles = {"com.microsoft.teams2.helper"}
    agent.tick(0)
    agent.tick(3)
    assert agent.session and agent.session.app == "com.microsoft.teams2"
    agent.mic.bundles = set()
    agent.tick(10)
    assert agent.session  # still inside the grace period
    agent.tick(31)
    assert agent.session is None
    jobs = agent.store.list()
    assert len(jobs) == 1 and jobs[0].app == "com.microsoft.teams2" and jobs[0].owned


def test_manual_stop_mid_meeting_does_not_restart(agent):
    agent.mic.bundles = {"us.zoom.xos"}
    agent.tick(0)
    agent.tick(3)
    agent.stop_recording()          # user clicked Stop while the call continues
    for t in range(4, 200, 2):
        agent.tick(t)
    assert agent.session is None and agent.recorder.starts == 1
    agent.mic.bundles = set()
    agent.tick(201)                 # call ended; suppression lifts
    agent.mic.bundles = {"us.zoom.xos"}
    agent.tick(300)
    agent.tick(303)
    assert agent.session and agent.recorder.starts == 2


def test_failed_start_notifies_and_retries(agent):
    agent.recorder.fail = True
    agent.mic.bundles = {"com.microsoft.teams2"}
    agent.clock = lambda: 3
    agent.tick(0)
    agent.tick(3)
    assert agent.session is None and any("did NOT start" in e[0] for e in agent.events)
    agent.recorder.fail = False
    agent.tick(20)
    assert agent.session is None     # waits 30 s between attempts
    agent.tick(40)
    assert agent.session is not None


def test_worker_pauses_during_live_call(agent):
    agent.start_recording()
    agent.store.add(Job(source_path="/tmp/whatever.mp4", started_at=1, ended_at=2))
    assert agent.process_due() is None


def test_orphan_scan_queues_only_meetcap_files(agent, tmp_path):
    (tmp_path / "rec" / "holiday.mp4").write_bytes(b"x")          # personal video: never adopted
    (tmp_path / "rec" / "meetcap_2026-09-25_10-00-00.mp4").write_bytes(b"x")
    agent.clock = lambda: time.time() + 3600
    assert agent.scan_orphans() == 1
    assert agent.scan_orphans() == 0
    jobs = agent.store.list()
    assert len(jobs) == 1 and jobs[0].app == "recovered" and jobs[0].source_path.endswith("meetcap_2026-09-25_10-00-00.mp4")


def test_recover_queues_in_flight_recording(agent):
    agent.recorder.recording = True   # left running by a crashed meetcap
    assert agent.recover() == 1
    assert agent.store.list()[0].app == "recovered" and not agent.recorder.recording


def test_rollover_keeps_detector_and_records_video_intent(agent):
    agent.cfg.detection.max_meeting_hours = 1
    agent.mic.bundles = {"com.microsoft.teams2"}
    agent.tick(0)
    agent.tick(3)
    agent.clock = lambda: 3700
    agent.tick(3700)
    assert agent.session and agent.recorder.starts == 2 and agent.detector.active_app == "com.microsoft.teams2"
    job = agent.store.list()[0]
    assert job.data["video"] is True and job.data["owned"] is True
    agent.mic.bundles = set()
    agent.tick(3701)
    agent.tick(3722)
    assert agent.session is None  # the meeting still auto-stops after a rollover


def test_obs_closed_after_meeting_unless_kept(agent, monkeypatch):
    calls = []
    agent.recorder.quit_obs = lambda: calls.append("quit")
    monkeypatch.setattr("meetcap.agent.media.wait_until_stable", lambda p: None)
    agent.start_recording()
    agent.stop_recording()
    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == ["quit"]
    agent.cfg.recording.keep_obs_running = True
    agent.start_recording()
    agent.stop_recording()
    time.sleep(0.3)
    assert calls == ["quit"]


def test_unknown_duration_is_never_discarded(sample_video, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.detection.min_meeting_seconds = 600
    p, store, blob, *_ = _pipeline(tmp_path, cfg)
    real = media.duration_seconds
    monkeypatch.setattr("meetcap.pipeline.media.duration_seconds",
                        lambda path: 0.0 if str(path).endswith(".mp4") else real(path))
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="com.microsoft.teams2")))
    assert job.status == Status.DONE and any(k.endswith("recording.mp4") for k in blob.objects)


def test_video_intent_comes_from_recording_time(sample_video, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.recording.video = False       # changed after a video meeting was recorded
    p, store, blob, *_ = _pipeline(tmp_path, cfg)
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="manual",
                              data={"owned": True, "video": True})))
    assert job.status == Status.DONE and any(k.endswith("recording.mp4") for k in blob.objects)


def test_finished_job_cannot_block_queue(tmp_path):
    p, store, *_ = _pipeline(tmp_path)
    j = store.add(Job(source_path="/x/done.mp4", started_at=1, ended_at=2))
    j.stage = Stage.DONE
    store.save(j)
    assert p.run(j).status == Status.DONE and store.next_due(now=10**10) is None
    with pytest.raises(ValueError):
        store.retry(store.get(j.id))


def test_lease_prevents_double_processing(tmp_path):
    store = JobStore(tmp_path / "j.sqlite3")
    j = store.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2))
    assert store.claim(j, now=100)
    assert not store.claim(j, now=101)          # a second process loses
    assert store.next_due(now=101) is None       # and the worker skips it
    store.release(j)
    assert store.claim_next(now=102).id == j.id


def test_terminal_control_and_status(agent, tmp_path):
    import json as _json
    ctl, st = tmp_path / "control.json", tmp_path / "status.json"
    ctl.write_text(_json.dumps({"cmd": "start"}))
    assert agent.handle_control(ctl) == "start" and agent.session and not ctl.exists()
    agent.write_status(st)
    data = _json.loads(st.read_text())
    assert data["recording"] is True and data["manual"] is True and "counts" in data
    ctl.write_text(_json.dumps({"cmd": "stop"}))
    agent.handle_control(ctl)
    assert agent.session is None
    ctl.write_text(_json.dumps({"cmd": "auto-off"}))
    agent.handle_control(ctl)
    assert agent.auto is False
    ctl.write_text("not json")
    assert agent.handle_control(ctl) is None and not ctl.exists()


def test_capture_problem_is_reported_during_recording(agent):
    agent.recorder.check_capture = lambda seconds: ["OBS hears nothing from your microphone."]
    agent.start_recording()
    problems = agent._check_capture(delay=0)
    assert problems and agent.last_error.startswith("OBS hears nothing")
    assert any(e[0] == "Recording has a problem" for e in agent.events)


def test_obs_closed_without_its_crashing_quit_when_possible(agent, monkeypatch):
    calls = []
    agent.recorder.quit_obs = lambda: calls.append("quit")
    agent.recorder.close_after_recording = lambda: calls.append("close")
    monkeypatch.setattr("meetcap.agent.media.wait_until_stable", lambda p: None)
    agent.start_recording()
    agent.stop_recording()
    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == ["close"]

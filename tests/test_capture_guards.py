import json
import struct
import subprocess
import time
from pathlib import Path

import pytest

from meetcap import media
from meetcap.jobs import JobStore
from meetcap.models import Job, Stage, Status
from meetcap.obs_client import ObsClient
from meetcap.pipeline import Pipeline
from meetcap.recorder import bmp_is_black
from tests.conftest import HAS_FFMPEG
from tests.test_pipeline_agent import FakeBlob, FakeSummarizer, FakeTranscriber, _cfg

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def _make(path: Path, video: str, audio: str, seconds: int = 6) -> Path:
    vsrc = f"{video}:size=320x240:rate=5:duration={seconds}"
    asrc = f"{audio}:duration={seconds}" if "sine" in audio else audio
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", vsrc, "-f", "lavfi", "-i", asrc,
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)
    return path


@pytest.fixture
def silent_black(tmp_path):   # exactly what an OBS scene without sources produces
    return _make(tmp_path / "meetcap_silent.mov", "color=c=black", "anullsrc=r=48000:cl=stereo")


@pytest.fixture
def black_with_sound(tmp_path):
    return _make(tmp_path / "meetcap_black.mp4", "color=c=black", "sine=frequency=440")


def test_media_detectors(silent_black, black_with_sound, sample_video):
    assert media.max_volume_db(silent_black) <= -80
    assert media.max_volume_db(black_with_sound) > -40
    assert media.mostly_black(silent_black) and media.mostly_black(black_with_sound)
    assert not media.mostly_black(sample_video)
    quiet = media.silences(silent_black)
    assert quiet and quiet[0][0] == 0.0 and quiet[-1][1] >= 5.5
    assert media.silences(black_with_sound) == []


def _pipe(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.detection.min_meeting_seconds = 1
    notes = []
    store, blob, tx = JobStore(tmp_path / "j.sqlite3"), FakeBlob(), FakeTranscriber()
    p = Pipeline(cfg, store, blob, tx, FakeSummarizer(), notifier=lambda *a, **k: notes.append(a),
                 work_root=tmp_path / "work")
    return p, store, blob, tx, notes


def test_silent_recording_is_rejected_not_transcribed(silent_black, tmp_path):
    p, store, blob, tx, notes = _pipe(tmp_path)
    job = p.run(store.add(Job(source_path=str(silent_black), started_at=time.time(), ended_at=time.time(),
                              app="manual", data={"owned": True, "video": True})))
    assert job.status == Status.FAILED and job.stage == Stage.FINALIZE
    assert "no sound" in job.last_error and "setup-obs" in job.last_error
    assert tx.calls == 0 and not blob.objects
    assert any(n[0] == "Recording was silent" for n in notes)
    assert silent_black.exists()          # kept for inspection


def test_black_video_is_not_uploaded_but_audio_is_processed(black_with_sound, tmp_path):
    p, store, blob, tx, notes = _pipe(tmp_path)
    job = p.run(store.add(Job(source_path=str(black_with_sound), started_at=time.time(), ended_at=time.time(),
                              app="manual", data={"owned": True, "video": True})))
    assert job.status == Status.DONE, job.last_error
    assert job.data["video_black"] and not job.data["has_video"]
    assert not any(k.endswith("recording.mp4") for k in blob.objects) and any(k.endswith("audio.ogg") for k in blob.objects)
    assert any(n[0] == "Screen wasn't captured" for n in notes)


def _bmp(pixels: bytes, bpp: int) -> bytes:
    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, bpp, 0, len(pixels), 0, 0, 0, 0)
    return header + info + pixels


def test_bmp_black_detection():
    assert bmp_is_black(_bmp(b"\x00\x00\x00\xff" * 2, 32))        # alpha 255 must not count as bright
    assert not bmp_is_black(_bmp(b"\x00\x80\x00\xff" * 2, 32))
    assert bmp_is_black(_bmp(b"\x03\x02\x01\x00\x00\x00\x00\x00", 24))


class EventWS:
    def __init__(self):
        self.outbox = [{"op": 0, "d": {"rpcVersion": 1}}, {"op": 2, "d": {}}]
        for _ in range(3):
            self.outbox.append({"op": 5, "d": {"eventType": "InputVolumeMeters", "eventData": {"inputs": [
                {"inputName": "MeetCap Mic", "inputLevelsMul": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]},
                {"inputName": "MeetCap System Audio", "inputLevelsMul": [[0.02, 0.05, 0.05]]}]}}})
        self.sent = []

    def recv(self):
        if not self.outbox:
            import websocket
            raise websocket.WebSocketTimeoutException("timeout")
        return json.dumps(self.outbox.pop(0))

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def settimeout(self, t):
        pass

    def close(self):
        pass


def test_read_events_collects_volume_meters():
    ws = EventWS()
    c = ObsClient(connect_fn=lambda *a, **k: ws, event_subscriptions=1 << 16)
    events = c.read_events(0.3, "InputVolumeMeters")
    assert len(events) == 3 and ws.sent[0]["d"]["eventSubscriptions"] == 65536

import subprocess
import time
from pathlib import Path

import pytest

from meetcap import local_whisper, media
from meetcap.jobs import JobStore
from meetcap.models import Job, Phrase, Status, Transcript
from meetcap.pipeline import Pipeline
from meetcap.transcribe import LocalWhisperTranscriber, merge_speaker_tracks
from tests.conftest import HAS_FFMPEG
from tests.test_pipeline_agent import FakeBlob, _cfg


def test_merge_drops_speaker_echo_and_labels_tracks():
    mine = Transcript(10_000, [Phrase(2100, 1500, "Can you send the SOW"), Phrase(5000, 1000, "Yes, by Friday.")])
    others = Transcript(10_000, [Phrase(2000, 1800, "Can you send the SOW?")])
    t = merge_speaker_tracks(mine, others)
    assert [(p.speaker, p.text) for p in t.phrases] == [(2, "Can you send the SOW?"), (1, "Yes, by Friday.")]


def test_merge_keeps_diarized_others_apart():
    others = Transcript(5000, [Phrase(0, 500, "hi", 1), Phrase(1000, 500, "hello", 2)])
    t = merge_speaker_tracks(Transcript(5000, []), others)
    assert [p.speaker for p in t.phrases] == [2, 3]


def test_local_transcriber_filters_silence(monkeypatch, tmp_path):
    monkeypatch.setattr(local_whisper, "supported", lambda: (True, ""))
    monkeypatch.setattr(media, "duration_seconds", lambda p: 12.0)
    monkeypatch.setattr(media, "silences", lambda p: [(6.0, 12.0)])   # talking only in the first half
    captured = {}

    def fake_run(audio, out, model, language, prompt):
        captured.update(model=model, language=language, prompt=prompt)
        return {"segments": [{"start": 0.5, "end": 2.0, "text": " Hello team ", "no_speech_prob": 0.05},
                             {"start": 3.0, "end": 4.0, "text": "Hmm.", "no_speech_prob": 0.92},
                             {"start": 7.0, "end": 9.0, "text": "Home Kinder Instagram", "no_speech_prob": 0.0},
                             {"start": 12.0, "end": 30.0, "text": "Thank you.", "no_speech_prob": 0.0}]}

    monkeypatch.setattr(local_whisper, "run", fake_run)
    t = LocalWhisperTranscriber("mlx-community/whisper-large-v3-turbo", "en", ["Acme"]).transcribe(tmp_path / "a.ogg")
    assert [(p.start_ms, p.text) for p in t.phrases] == [(500, "Hello team")]
    assert captured == {"model": "mlx-community/whisper-large-v3-turbo", "language": "en",
                        "prompt": "Vocabulary: Acme."}


def test_local_unsupported_platform_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(local_whisper, "supported", lambda: (False, "on-Mac transcription needs an Apple Silicon Mac"))
    with pytest.raises(Exception, match="Apple Silicon"):
        LocalWhisperTranscriber("m").transcribe(tmp_path / "a.ogg")


@pytest.fixture
def three_track_video(tmp_path: Path) -> Path:
    """Like an OBS recording with tracks 1 = mix, 2 = mic, 3 = meeting audio."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path / "meetcap_rec.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=500:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=700:duration=8",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
    return out


class TrackTranscriber:
    def __init__(self):
        self.files = []

    def transcribe(self, audio):
        self.files.append(audio.name)
        if audio.name == "you.ogg":
            return Transcript(8000, [Phrase(2100, 1500, "Can you send the SOW"), Phrase(5000, 1000, "Yes, by Friday.")])
        return Transcript(8000, [Phrase(2000, 1800, "Can you send the SOW?")])


class HintSummarizer:
    def __init__(self):
        self.meta = None

    def summarize(self, text, meta, frames):
        self.meta = meta
        return {"title": "SOW follow-up", "summary": "", "speakers": [], "key_points": [], "decisions": [],
                "action_items": [{"owner": "Alex", "task": "Send SOW", "due": "Friday", "timestamp": "00:00:05"}],
                "open_questions": [], "risks": [], "on_screen": [], "key_moments": []}


def test_pipeline_splits_you_and_others(three_track_video, tmp_path):
    assert media.audio_stream_count(three_track_video) == 3
    cfg = _cfg(tmp_path)
    cfg.transcription.engine = "local"  # split_speakers auto -> on
    cfg.detection.min_meeting_seconds = 1
    store, blob, tx, sm = JobStore(tmp_path / "j.sqlite3"), FakeBlob(), TrackTranscriber(), HintSummarizer()
    p = Pipeline(cfg, store, blob, tx, sm, notifier=lambda *a, **k: None, work_root=tmp_path / "work")
    job = p.run(store.add(Job(source_path=str(three_track_video), started_at=time.time(), ended_at=time.time(),
                              app="com.microsoft.teams2", data={"owned": True, "video": True})))
    assert job.status == Status.DONE, job.last_error
    assert tx.files == ["you.ogg", "others.ogg"]
    md = blob.objects[f"{job.blob_prefix}/transcript.md"].decode()
    assert "**[00:00:02] Others (S2):** Can you send the SOW?" in md
    assert "**[00:00:05] Alex (S1):** Yes, by Friday." in md
    assert "Can you send the SOW\n" not in md               # the echo on your mic was dropped
    assert "S1 is the recorder" in sm.meta["speaker_hint"]


def test_single_track_recordings_still_work(sample_video, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.transcription.engine = "local"
    tx = TrackTranscriber()
    store = JobStore(tmp_path / "j.sqlite3")
    p = Pipeline(cfg, store, FakeBlob(), tx, HintSummarizer(), notifier=lambda *a, **k: None,
                 work_root=tmp_path / "work")
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=1, ended_at=2, app="manual")))
    assert job.status == Status.DONE and tx.files == ["audio.ogg"]


def test_local_transcriber_skips_fully_silent_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(local_whisper, "supported", lambda: (True, ""))
    monkeypatch.setattr(media, "duration_seconds", lambda p: 31.6)
    monkeypatch.setattr(media, "silences", lambda p: [(0.0, 31.55)])
    monkeypatch.setattr(local_whisper, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    t = LocalWhisperTranscriber("m").transcribe(tmp_path / "a.ogg")
    assert t.phrases == [] and t.raw == [{"skipped": "silent"}]

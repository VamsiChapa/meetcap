import http.client
import json
import subprocess
import time

import pytest

from meetcap.library import MeetingLibrary, _ts_to_seconds
from meetcap.webui import parse_range, serve
from tests.conftest import HAS_FFMPEG

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def _ogg(tmp_path, seconds=3):
    out = tmp_path / "a.ogg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    "-ac", "1", "-c:a", "libopus", str(out)], check=True)
    return out.read_bytes()


class Blob:
    def __init__(self):
        self.objects = {}

    def list_names(self, prefix=""):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def download_bytes(self, name):
        return self.objects[name]

    def download_range(self, name, offset, length):
        return self.objects[name][offset:offset + length]

    def size(self, name):
        return len(self.objects[name]) if name in self.objects else None


def _meeting(blob, prefix, mid, started, title, summary, transcript_text, audio, point="Pilot scope agreed",
             task="Send the SOW"):
    meta = {"id": mid, "app": "com.microsoft.teams2", "title": title, "started_at": started, "ended_at": started + 1800,
            "duration_s": 1800, "blob_prefix": prefix, "has_video": False}
    notes = {"title": title, "summary": summary,
             "speakers": [{"label": "S1", "name": "Alex"}, {"label": "S2", "name": None}],
             "key_points": [point], "decisions": ["Use Azure"], "open_questions": [], "risks": [],
             "action_items": [{"owner": "Alex", "task": task, "due": "Friday", "timestamp": "00:12:05"}],
             "key_moments": [{"timestamp": "00:03:10", "what": "Budget discussed"}],
             "on_screen": [{"timestamp": "01:02:03", "what": "Architecture slide"}]}
    transcript = {"phrases": [{"start_ms": 1000, "duration_ms": 2000, "text": transcript_text, "speaker": 1},
                              {"start_ms": 4000, "duration_ms": 1500, "text": "Sounds good.", "speaker": 2}]}
    blob.objects[f"{prefix}/meta.json"] = json.dumps(meta).encode()
    blob.objects[f"{prefix}/notes.json"] = json.dumps(notes).encode()
    blob.objects[f"{prefix}/transcript.json"] = json.dumps(transcript).encode()
    blob.objects[f"{prefix}/audio.ogg"] = audio
    blob.objects[f"{prefix}/notes.md"] = b"# notes"


@pytest.fixture
def lib(tmp_path):
    blob = Blob()
    audio = _ogg(tmp_path)
    _meeting(blob, "alex/2026/09/27/1000_teams_aaaaaa", "aaaaaa111111", 1790500000, "Apple pilot scope",
             "Agreed the pilot scope and the SOW timeline.", "We need the statement of work by Friday.", audio)
    _meeting(blob, "alex/2026/09/28/1500_teams_bbbbbb", "bbbbbb222222", 1790600000, "Hiring sync",
             "Discussed two open roles.", "The kubernetes migration can wait.", audio,
             point="Two roles open", task="Post the job ads")
    blob.objects["other/unrelated.txt"] = b"x"
    library = MeetingLibrary(root=tmp_path / "library", blob_factory=lambda: blob, blob_prefix="alex")
    return library, blob


def test_sync_list_and_search(lib):
    library, blob = lib
    assert library.sync() == 2 and library.sync() == 0            # second sync adds nothing
    rows = library.list()
    assert [r["title"] for r in rows] == ["Hiring sync", "Apple pilot scope"]   # newest first
    assert rows[1]["actions"] == 1 and rows[1]["moments"] == 1
    hit = library.list("sow")
    assert [r["id"] for r in hit] == ["aaaaaa111111"] and "notes" in hit[0]["matched_in"]
    tx_only = library.list("kubernetes")
    assert [r["id"] for r in tx_only] == ["bbbbbb222222"] and tx_only[0]["matched_in"] == ["transcript"]
    assert library.list("pilot friday") and not library.list("pilot kubernetes")   # all terms must match


def test_get_adds_seconds_and_speaker_names(lib):
    library, _ = lib
    library.sync()
    m = library.get("aaaaaa111111")
    assert m["notes"]["action_items"][0]["seconds"] == 725
    assert m["notes"]["key_moments"][0]["seconds"] == 190
    assert m["notes"]["on_screen"][0]["seconds"] == 3723
    assert [p["who"] for p in m["transcript"]] == ["Alex", "S2"]
    assert m["transcript"][0]["t"] == 1.0
    assert library.get("ffffffffffff") is None


def test_audio_is_fetched_once_converted_and_capped(lib):
    library, blob = lib
    library.sync()
    a = library.audio_file("aaaaaa111111")
    assert a.suffix == ".m4a" and a.stat().st_size > 1000
    blob.objects["alex/2026/09/27/1000_teams_aaaaaa/audio.ogg"] = b"gone"      # cached: no second download
    assert library.audio_file("aaaaaa111111") == a
    library.audio_cache_bytes = 1                                              # tiny cap: older audio dropped
    b = library.audio_file("bbbbbb222222")
    assert b.exists() and not a.exists()


def test_store_local_from_pipeline(tmp_path):
    library = MeetingLibrary(root=tmp_path / "lib")
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    meta = {"id": "cccccc333333", "blob_prefix": "p/2026/1", "started_at": 1.0, "duration_s": 60}
    library.store_local(meta, {"title": "Local one", "summary": "s"}, {"phrases": []}, [(frame, "00-00-05.jpg")])
    assert library.list()[0]["title"] == "Local one"
    assert library.get("cccccc333333")["frames"] == ["00-00-05.jpg"]
    assert library.frame_file("cccccc333333", "../../etc/passwd") is None       # no path tricks


def test_ts_parsing():
    assert _ts_to_seconds("01:02:03") == 3723 and _ts_to_seconds("02:05") == 125
    assert _ts_to_seconds(None) is None and _ts_to_seconds("soon") is None


def test_parse_range():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    with pytest.raises(ValueError):
        parse_range("bytes=500-", 100)


def _get(port, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_web_app_end_to_end(lib):
    library, _ = lib
    library.sync()
    httpd = serve(library, port=0, status_fn=lambda: {"recording": False, "auto": True})
    port = httpd.server_address[1]
    try:
        code, hdrs, body = _get(port, "/")
        assert code == 200 and b"Meeting notes" in body and "frame-ancestors 'none'" in hdrs["Content-Security-Policy"]
        code, _, body = _get(port, "/api/meetings?q=sow")
        assert code == 200 and [m["id"] for m in json.loads(body)["meetings"]] == ["aaaaaa111111"]
        code, _, body = _get(port, "/api/meetings/aaaaaa111111")
        assert json.loads(body)["notes"]["title"] == "Apple pilot scope"
        code, hdrs, body = _get(port, "/api/meetings/aaaaaa111111/audio", {"Range": "bytes=0-99"})
        assert code == 206 and len(body) == 100 and hdrs["Content-Range"].startswith("bytes 0-99/")
        assert _get(port, "/api/meetings/aaaaaa111111/video")[0] == 404        # audio-only meeting
        assert _get(port, "/api/status")[0] == 200
        assert _get(port, "/favicon.png")[0] == 200 and _get(port, "/pet/sit_r_0.png")[0] == 200
        assert _get(port, "/api/meetings/..%2f..%2fetc")[0] == 400
        # security: other Host headers (DNS rebinding) and header-less POSTs are refused
        assert _get(port, "/api/meetings", {"Host": "evil.example:80"})[0] == 403
        assert _get(port, "/api/sync", method="POST")[0] == 403
        code, _, body = _get(port, "/api/sync", {"X-Meetcap": "1"}, method="POST")
        assert code == 200 and json.loads(body)["added"] == 0
    finally:
        httpd.shutdown()


def test_pipeline_feeds_the_library(sample_video, tmp_path):
    from meetcap.jobs import JobStore
    from meetcap.models import Job, Status
    from meetcap.pipeline import Pipeline
    from tests.test_pipeline_agent import FakeBlob, FakeSummarizer, FakeTranscriber, _cfg

    library = MeetingLibrary(root=tmp_path / "lib")
    store = JobStore(tmp_path / "j.sqlite3")
    p = Pipeline(_cfg(tmp_path), store, FakeBlob(), FakeTranscriber(), FakeSummarizer(),
                 notifier=lambda *a, **k: None, work_root=tmp_path / "work", library=library)
    job = p.run(store.add(Job(source_path=str(sample_video), started_at=time.time(), ended_at=time.time(),
                              app="manual", data={"owned": True, "video": True})))
    assert job.status == Status.DONE
    rows = library.list()
    assert rows and rows[0]["title"] == "Loan / pilot: next steps"
    m = library.get(rows[0]["id"])
    assert m["audio_cached"] and m["frames"] and m["transcript"]

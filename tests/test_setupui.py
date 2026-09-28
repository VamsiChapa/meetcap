import http.client
import json
import time

import pytest

from meetcap.config import load_config
from meetcap.setupui import SetupSession, serve


@pytest.fixture
def setup(tmp_path):
    vault = {}
    steps_ran = []

    def fake_obs(cfg, on_step):
        on_step("OBS 32.2.2, obs-websocket 5.6")
        on_step("WARNING: macOS asked for permission")
        steps_ran.append(cfg)

    session = SetupSession(config_path=tmp_path / "config.yaml", get=vault.get, put=vault.__setitem__,
                           remove=lambda n: vault.pop(n, None), obs_setup=fake_obs, restart=lambda: "restarted meetcap")
    httpd, url = serve(session)
    port = httpd.server_address[1]
    yield session, vault, port, url, tmp_path
    httpd.shutdown()


def _req(port, path, body=None, token=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"}
    if token:
        headers["X-Meetcap-Token"] = token
    c.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw), dict(r.getheaders())
    except ValueError:
        return r.status, raw, dict(r.getheaders())


def test_page_and_token(setup):
    session, vault, port, url, _ = setup
    assert url.endswith(f"/setup#t={session.token}")
    code, body, headers = _req(port, "/setup")
    assert code == 200 and b"Set up meetcap" in body and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert _req(port, "/api/state")[0] == 403                                  # no token
    assert _req(port, "/api/state", token="guess")[0] == 403
    assert _req(port, "/api/state", token=session.token, host="evil.example")[0] == 403
    assert _req(port, "/pet/kitten/sit_r_0.png")[0] == 200 and _req(port, "/pet/../x")[0] == 404
    code, state, _ = _req(port, "/api/state", token=session.token)
    assert code == 200 and state["config"]["storage.backend"] == "local" and state["species"][0]["id"] == "puppy"
    assert state["presets"]["groq"]["signup"].startswith("https://")


def test_save_writes_config_and_keychain_but_never_returns_keys(setup):
    session, vault, port, _, tmp_path = setup
    folder = str(tmp_path / "Meetings")
    code, state, _ = _req(port, "/api/save", {"config": {
        "storage.local_path": folder, "transcription.engine": "groq", "summary.provider": "anthropic",
        "azure.speech.phrase_list": ["Priya", " ", "Kubernetes"], "ui.pet_species": "fox", "ui.pet": True, "ui.pet_size": 80},
        "secrets": {"groq-key": "gsk_live_123", "anthropic-key": "sk-ant-456"}}, token=session.token)
    assert code == 200, state
    assert vault == {"groq-key": "gsk_live_123", "anthropic-key": "sk-ant-456"}
    assert "gsk_live_123" not in json.dumps(state) and state["secrets"]["groq-key"] is True
    cfg = load_config(tmp_path / "config.yaml")
    assert (cfg.transcription.engine, cfg.summary.provider, cfg.ui.pet_species, cfg.ui.pet_size) == ("groq", "anthropic", "fox", 80)
    assert cfg.storage.local_path == folder and cfg.azure.speech.phrase_list == ["Priya", "Kubernetes"]
    assert state["needed"] == ["groq-key", "anthropic-key"] and state["problems"] == []
    _req(port, "/api/save", {"config": {}, "secrets": {"groq-key": "", "anthropic-key": False}}, token=session.token)
    assert vault == {"groq-key": "gsk_live_123"}                              # empty = keep, False = remove


@pytest.mark.parametrize("config, secrets", [
    ({"recording.output_dir": "/tmp"}, {}),            # not editable here
    ({"summary.provider": "gemini"}, {}),              # not a choice
    ({"ui.pet_size": True}, {}),                       # wrong type
    ({}, {"obs-password": "x"}),                       # meetcap manages this one itself
])
def test_bad_saves_are_refused(setup, config, secrets):
    session, vault, port, _, _ = setup
    code, body, _ = _req(port, "/api/save", {"config": config, "secrets": secrets}, token=session.token)
    assert code == 400 and body["error"] and vault == {}


def test_tests_obs_and_finish(setup):
    session, vault, port, _, tmp_path = setup
    _req(port, "/api/save", {"config": {"storage.local_path": str(tmp_path / "M")}}, token=session.token)
    code, r, _ = _req(port, "/api/test", {"what": "storage"}, token=session.token)
    assert code == 200 and r["ok"] is True and str(tmp_path / "M") in r["message"]
    code, r, _ = _req(port, "/api/test", {"what": "summary"}, token=session.token)
    assert r["ok"] is None and "notes are off" in r["message"]
    _req(port, "/api/save", {"config": {"transcription.engine": "groq"}}, token=session.token)
    code, r, _ = _req(port, "/api/test", {"what": "transcription"}, token=session.token)
    assert r["ok"] is False and "groq-key" in r["message"]                     # says which key is missing
    assert _req(port, "/api/obs", {}, token=session.token)[0] == 200
    for _ in range(50):
        state = _req(port, "/api/obs", token=session.token)[1]
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["done"] and state["steps"][1].startswith("WARNING:")
    code, r, _ = _req(port, "/api/finish", {}, token=session.token)
    assert r["restarted"] == "restarted meetcap" and r["reader"].endswith("Meeting Notes.html")
    assert (tmp_path / "M" / "Meeting Notes.html").exists() and session.finished.is_set()

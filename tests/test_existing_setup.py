from datetime import timezone

import pytest

from meetcap.azure_setup import (DATED_API_VERSION, Prompter, explain, parse_openai_endpoint, parse_speech_location,
                                 parse_storage, run_existing, with_container)
from meetcap.config import StorageConfig, load_config
from meetcap.storage import AzureBlobStore, sas_expiry

CONN = ("DefaultEndpointsProtocol=https;AccountName=stacmerec;AccountKey=abc==;"
        "EndpointSuffix=core.windows.net")
SAS_ACCOUNT = "https://stacmerec.blob.core.windows.net/?sv=2022-11-02&ss=b&se=2027-03-01T00:00:00Z&sig=xyz"
SAS_CONTAINER = "https://stacmerec.blob.core.windows.net/recordings?sp=racwl&se=2027-03-01T00:00:00Z&sig=xyz"


def test_parse_storage_variants():
    t = parse_storage(CONN)
    assert (t.kind, t.container, t.secret) == ("key", None, CONN)
    assert t.account_url == "https://stacmerec.blob.core.windows.net"
    t = parse_storage(SAS_CONTAINER)
    assert (t.kind, t.container) == ("sas", "recordings")
    t = with_container(parse_storage(SAS_ACCOUNT), "meetings")
    assert t.secret == "https://stacmerec.blob.core.windows.net/meetings?sv=2022-11-02&ss=b&se=2027-03-01T00:00:00Z&sig=xyz"
    assert parse_storage("stacmerec").kind == "aad"
    assert parse_storage(f'"{CONN}"').kind == "key"  # quotes from copy-paste are tolerated
    with pytest.raises(ValueError):
        parse_storage("hello world")


def test_parse_speech_location():
    assert parse_speech_location("eastus") == "https://eastus.api.cognitive.microsoft.com"
    assert parse_speech_location("East US 2") == "https://eastus2.api.cognitive.microsoft.com"
    assert parse_speech_location("https://eastus.api.cognitive.microsoft.com/") == "https://eastus.api.cognitive.microsoft.com"
    assert parse_speech_location("https://my-ai.cognitiveservices.azure.com/") == "https://my-ai.cognitiveservices.azure.com"
    with pytest.raises(ValueError):
        parse_speech_location("??")


def test_parse_openai_endpoint_variants():
    assert parse_openai_endpoint("https://aoai-x.openai.azure.com/") == ("https://aoai-x.openai.azure.com", None)
    assert parse_openai_endpoint("https://ai-x.cognitiveservices.azure.com/")[0] == "https://ai-x.openai.azure.com"
    assert parse_openai_endpoint("https://ai-x.services.ai.azure.com/api/projects/p1")[0] == "https://ai-x.openai.azure.com"
    uri = "https://aoai-x.openai.azure.com/openai/deployments/gpt-4.1-prod/chat/completions?api-version=2025-01-01-preview"
    assert parse_openai_endpoint(uri) == ("https://aoai-x.openai.azure.com", "gpt-4.1-prod")
    with pytest.raises(ValueError):
        parse_openai_endpoint("https://eastus.api.cognitive.microsoft.com/")
    with pytest.raises(ValueError):
        parse_openai_endpoint("aoai-x")


def test_explain_is_plain():
    assert "SAS URL" in explain(RuntimeError("KeyBasedAuthenticationNotPermitted"))
    assert "Deployments" in explain(RuntimeError("Error code: 404 - DeploymentNotFound"))
    assert "key was rejected" in explain(RuntimeError("Speech returned 401: Access denied due to invalid subscription key"))
    assert "reach that address" in explain(RuntimeError("Failed to resolve 'eastuss.api.cognitive.microsoft.com'"))


def test_sas_store_never_leaks_token_and_expiry_parses():
    store = AzureBlobStore(StorageConfig(auth="sas"), lambda n: SAS_CONTAINER if n == "storage-sas-url" else None)
    assert store.url("a/b.md") == "https://stacmerec.blob.core.windows.net/recordings/a/b.md"
    exp = sas_expiry(SAS_CONTAINER)
    assert exp.year == 2027 and exp.tzinfo == timezone.utc


class FakeChecker:
    def __init__(self, fail_storage=0, fail_speech=0, v1_ok=True, speech_on=(), tx_fail=0, tx_format="diarized_json"):
        self.fail_storage, self.fail_speech, self.v1_ok = fail_storage, fail_speech, v1_ok
        self.speech_on, self.tx_fail, self.tx_format, self.calls = set(speech_on), tx_fail, tx_format, []

    def storage(self, t):
        self.calls.append(("storage", t.kind, t.container))
        if self.fail_storage:
            self.fail_storage -= 1
            raise RuntimeError("AuthenticationFailed")

    def speech(self, endpoint, key):
        self.calls.append(("speech", endpoint, key))
        if endpoint.endswith(".cognitiveservices.azure.com"):   # "same resource" auto-check
            if endpoint in self.speech_on:
                return
            raise RuntimeError("Speech returned 404: Resource not found")
        if self.fail_speech:
            self.fail_speech -= 1
            raise RuntimeError("Speech returned 401: invalid subscription key")

    def openai(self, endpoint, deployment, key, api_version=""):
        self.calls.append(("openai", endpoint, deployment, key, api_version))
        if not api_version and not self.v1_ok:
            raise RuntimeError("Error code: 404 - Resource not found")

    def local(self, model, out=print):
        self.calls.append(("local", model))

    def openai_transcribe(self, endpoint, deployment, key):
        self.calls.append(("transcribe", endpoint, deployment, key))
        if self.tx_fail:
            self.tx_fail -= 1
            raise RuntimeError("deployment 'gpt-4.1' rejected every format")
        return {"format": self.tx_format, "api_version": ""}


def _run(tmp_path, answers, secrets, checker):
    answers, secrets, out, stored = list(answers), list(secrets), [], {}
    p = Prompter(ask=lambda q: answers.pop(0) if answers else "", ask_secret=lambda q: secrets.pop(0),
                 out=out.append)
    result = run_existing(p=p, config_path=tmp_path / "config.yaml", state_path=tmp_path / "azure.json",
                          store_secret=lambda k, v: stored.__setitem__(k, v), checker=checker, verify=False)
    return result, stored, out


def test_plain_azure_openai_defaults_to_on_mac_transcription(tmp_path):
    checker = FakeChecker()
    # container, endpoint, chat deployment, transcription choice (Enter = on this Mac), language
    answers = ["", "https://my-aoai-1.openai.azure.com/openai/deployments/gpt-4.1/chat/completions?x=1", "", "", "2"]
    result, stored, out = _run(tmp_path, answers, [CONN, "AOAIKEY"], checker)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.transcription.engine == "local" and cfg.split_speakers
    assert cfg.azure.openai.endpoint == "https://my-aoai-1.openai.azure.com" and cfg.azure.openai.deployment == "gpt-4.1"
    assert cfg.azure.storage.auth == "key" and cfg.azure.speech.locales == ["en-IN"]
    assert stored == {"openai-key": "AOAIKEY", "storage-connection-string": CONN}
    assert ("local", "mlx-community/whisper-large-v3-turbo") in checker.calls


def test_azure_openai_transcription_deployment(tmp_path):
    checker = FakeChecker()
    answers = ["", "https://aoai-x.openai.azure.com/", "gpt-4.1", "2", "", "2"]
    result, stored, out = _run(tmp_path, answers, [CONN, "AOAIKEY"], checker)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.transcription.engine == "azure-openai" and not cfg.split_speakers
    assert cfg.azure.openai.transcribe_deployment == "gpt-4o-transcribe-diarize"
    assert cfg.azure.openai.transcribe_format == "diarized_json"
    assert any("with speaker labels" in line for line in out)


def test_foundry_resource_with_speech_is_detected(tmp_path):
    checker = FakeChecker(v1_ok=False, speech_on={"https://ai-x.cognitiveservices.azure.com"})
    uri = "https://ai-x.cognitiveservices.azure.com/openai/deployments/gpt41/chat/completions?api-version=x"
    answers = [uri, "", ""]          # container from SAS URL; deployment from the URI; language default
    result, stored, out = _run(tmp_path, answers, [SAS_CONTAINER, "ONEKEY"], checker)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.transcription.engine == "azure-speech"
    assert cfg.azure.speech.endpoint == "https://ai-x.cognitiveservices.azure.com"
    assert cfg.azure.openai.endpoint == "https://ai-x.openai.azure.com" and cfg.azure.openai.deployment == "gpt41"
    assert cfg.azure.openai.api_version == DATED_API_VERSION
    assert stored["speech-key"] == "ONEKEY" == stored["openai-key"] and stored["storage-sas-url"] == SAS_CONTAINER
    assert any("stops working on 01 Mar 2027" in line for line in out)


def test_separate_speech_resource_with_retries(tmp_path):
    checker = FakeChecker(fail_storage=1, fail_speech=1)
    answers = ["", "y", "", "https://aoai-x.openai.azure.com", "gpt-4.1", "3", "eastus", "y", "eastus", ""]
    secrets = ["badconnectionstringAccountKey=;AccountName=x", CONN, "K", "BAD", "GOOD"]
    result, stored, out = _run(tmp_path, answers, secrets, checker)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.transcription.engine == "azure-speech"
    assert cfg.azure.speech.endpoint == "https://eastus.api.cognitive.microsoft.com"
    assert stored["speech-key"] == "GOOD" and stored["storage-connection-string"] == CONN
    assert any("rejected" in line for line in out)


def test_wrong_transcription_deployment_then_whisper(tmp_path):
    checker = FakeChecker(tx_fail=1, tx_format="verbose_json")
    answers = ["", "https://aoai-x.openai.azure.com", "gpt-4.1", "2", "gpt-4.1", "y", "whisper", ""]
    result, stored, out = _run(tmp_path, answers, [CONN, "K"], checker)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.azure.openai.transcribe_deployment == "whisper" and cfg.azure.openai.transcribe_format == "verbose_json"
    assert any("isn't a transcription model" in line for line in out)


def test_openai_url_in_speech_field_maps_to_same_resource():
    uri = "https://my-aoai-1.openai.azure.com/openai/deployments/gpt-4.1/chat/completions?api-version=2025-01-01"
    assert parse_speech_location(uri) == "https://my-aoai-1.cognitiveservices.azure.com"

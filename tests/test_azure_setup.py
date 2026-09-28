import json

import pytest

from meetcap.azure_setup import AzError, Prompter, run_setup, update_config
from meetcap.config import ConfigError, load_config


class FakeAz:
    """Records every az call and answers from a small in-memory Azure."""

    def __init__(self, can_grant_roles=True, openai=True, key_readable=True):
        self.calls, self.created = [], set()
        self.can_grant_roles, self.openai, self.key_readable = can_grant_roles, openai, key_readable
        self.exe = "/usr/bin/true"

    def _key(self, args):
        return " ".join(a for a in args if not a.startswith("["))

    def __call__(self, *args):
        self.calls.append(args)
        a = args
        if a[:2] == ("account", "show"):
            return {"user": {"name": "alex@example.com"}}
        if a[:2] == ("account", "list"):
            return [{"name": "Sandbox", "id": "sub-1", "isDefault": False},
                    {"name": "Acme-AI-Dev", "id": "sub-2", "isDefault": True}]
        if a[:3] == ("storage", "container-rm", "exists"):
            return {"exists": "container" in self.created}
        if a[:3] == ("cognitiveservices", "account", "list"):
            return [{"name": "aoai-shared", "rg": "rg-ai", "location": "eastus2", "subdomain": "aoai-shared",
                     "endpoint": "https://aoai-shared.cognitiveservices.azure.com/"}] if self.openai else []
        if a[:4] == ("cognitiveservices", "account", "deployment", "list"):
            return [{"name": "embed", "model": "text-embedding-3-large", "version": "1"},
                    {"name": "chat41", "model": "gpt-4.1", "version": "2025-04-14"}]
        if a[:3] in (("storage", "account", "show"), ("cognitiveservices", "account", "show")):
            name = a[a.index("-n") + 1]
            if name in self.created:
                return {"name": name}
            raise AzError("ResourceNotFound")
        raise AssertionError(f"unexpected json call {a}")

    def try_(self, *args):
        try:
            return self(*args)
        except AzError:
            return None

    def do(self, *args):
        self.calls.append(args)
        if args[:3] == ("storage", "account", "create"):
            self.created.add(args[args.index("-n") + 1])
        if args[:3] == ("cognitiveservices", "account", "create"):
            self.created.add(args[args.index("-n") + 1])
        if args[:3] == ("storage", "container-rm", "create"):
            self.created.add("container")
        if args[:3] == ("role", "assignment", "create") and not self.can_grant_roles:
            raise AzError("AuthorizationFailed")

    def tsv(self, *args):
        self.calls.append(args)
        a = args
        if a[:3] == ("ad", "signed-in-user", "show"):
            return "oid-1"
        if a[:3] == ("storage", "account", "show"):
            return "/subscriptions/sub-2/.../stmeetcap"
        if a[:3] == ("role", "assignment", "list"):
            return "0"
        if a[:3] == ("cognitiveservices", "account", "show"):
            return f"https://{a[a.index('-n') + 1]}.cognitiveservices.azure.com/"
        if a[:4] == ("cognitiveservices", "account", "keys", "list"):
            if "aoai-shared" in a and not self.key_readable:
                raise AzError("AuthorizationFailed: listKeys")
            return "KEY-" + a[a.index("-n") + 1]
        if a[:3] == ("storage", "account", "show-connection-string"):
            return "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y"
        raise AssertionError(f"unexpected tsv call {a}")

    def login(self):
        raise AssertionError("already signed in")


def _prompter(answers, secrets=()):
    answers, secrets, out = list(answers), list(secrets), []
    return Prompter(ask=lambda q: answers.pop(0) if answers else "", ask_secret=lambda q: secrets.pop(0),
                    out=out.append), out


def _run(tmp_path, az, answers, secrets=()):
    stored = {}
    p, out = _prompter(answers, secrets)
    result = run_setup(az=az, p=p, config_path=tmp_path / "config.yaml", state_path=tmp_path / "azure.json",
                       store_secret=lambda k, v: stored.__setitem__(k, v), verify=False)
    return result, stored, out


def test_defaults_create_everything_and_write_config(tmp_path):
    az = FakeAz()
    # Enter for subscription (default = isDefault), region, short name "alex", language 2, OpenAI 1, deployment 1
    result, stored, out = _run(tmp_path, az, ["", "", "alex", "2", "", ""])
    names = result["names"]
    assert names["rg"] == "rg-meetcap-alex" and names["storage"].startswith("stmeetcapalex")
    assert len(names["storage"]) <= 24 and names["storage"].isalnum()
    assert ("account", "set", "--subscription", "sub-2") in az.calls

    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.azure.storage.account_url == f"https://{names['storage']}.blob.core.windows.net"
    assert cfg.azure.storage.auth == "aad" and cfg.azure.storage.prefix == "alex"
    assert cfg.azure.speech.endpoint == f"https://{names['speech']}.cognitiveservices.azure.com"
    assert cfg.azure.speech.locales == ["en-IN"]
    assert cfg.azure.openai.endpoint == "https://aoai-shared.openai.azure.com"
    assert cfg.azure.openai.deployment == "chat41"          # gpt-4.1 ranked above the embedding model
    assert cfg.azure.openai.auth == "key"
    assert stored == {"speech-key": f"KEY-{names['speech']}", "openai-key": "KEY-aoai-shared"}
    assert "# meetcap settings" in (tmp_path / "config.yaml").read_text()  # comments survive
    assert json.loads((tmp_path / "azure.json").read_text())["names"] == names


def test_rerun_reuses_resources(tmp_path):
    az = FakeAz()
    first, *_ = _run(tmp_path, az, ["", "", "alex", "", "", ""])
    az.calls.clear()
    second, *_ = _run(tmp_path, az, ["", "", "", "", "", ""])
    assert second["names"] == first["names"]
    assert not any(c[:3] in (("storage", "account", "create"), ("cognitiveservices", "account", "create"),
                             ("storage", "container-rm", "create")) for c in az.calls)


def test_no_role_rights_falls_back_to_storage_key(tmp_path):
    result, stored, out = _run(tmp_path, FakeAz(can_grant_roles=False), ["", "", "alex", "", "", ""])
    assert result["storage_auth"] == "key" and "storage-connection-string" in stored
    assert load_config(tmp_path / "config.yaml").azure.storage.auth == "key"
    assert any("storage key will be used" in line for line in out)


def test_manual_openai_entry_and_aad_when_no_key(tmp_path):
    az = FakeAz(openai=False)
    answers = ["", "", "alex", "", "1", "https://acme-aoai.openai.azure.com/", "gpt-4.1"]
    result, stored, _ = _run(tmp_path, az, answers, secrets=[""])
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.azure.openai.endpoint == "https://acme-aoai.openai.azure.com"
    assert cfg.azure.openai.deployment == "gpt-4.1" and cfg.azure.openai.auth == "aad"
    assert "openai-key" not in stored


def test_unreadable_key_prompts_for_paste(tmp_path):
    result, stored, _ = _run(tmp_path, FakeAz(key_readable=False), ["", "", "alex", "", "", ""],
                             secrets=["PASTED"])
    assert stored["openai-key"] == "PASTED" and result["openai_auth"] == "key"


def test_bad_answers_are_reasked(tmp_path):
    result, _, out = _run(tmp_path, FakeAz(), ["9", "", "mars-1", "", "Alex!", "alex", "", "", ""])
    assert any("type a number" in line for line in out)
    assert any("pick one of" in line for line in out)
    assert any("lowercase" in line for line in out)
    assert result["names"]["rg"] == "rg-meetcap-alex"


def test_update_config_rejects_invalid_result(tmp_path):
    with pytest.raises(ConfigError):
        update_config(tmp_path / "c.yaml", {"azure.storage.bogus": 1})

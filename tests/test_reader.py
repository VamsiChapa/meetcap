import base64
import json
import re
import stat
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from azure.storage.blob import AccessPolicy, CorsRule

from meetcap.reader import ReaderLinkError, build_reader, check_link
from meetcap.storage import READER_POLICY, AzureBlobStore, StorageError

KEY = base64.b64encode(b"k" * 32).decode()


class FakeContainer:
    def __init__(self, policies=None, public=None, url="https://acct1.blob.core.windows.net/meetings"):
        self.url, self.container_name = url, "meetings"
        self.credential = SimpleNamespace(account_name="acct1", account_key=KEY)
        self.policies, self.public, self.sets = dict(policies or {}), public, []

    def get_container_access_policy(self):
        return {"public_access": self.public,
                "signed_identifiers": [SimpleNamespace(id=k, access_policy=v) for k, v in self.policies.items()]}

    def set_container_access_policy(self, signed_identifiers, public_access=None):
        self.sets.append(public_access)
        self.policies = dict(signed_identifiers)


class FakeSvc:
    def __init__(self, cors=None):
        self.cors, self.sets = list(cors or []), 0

    def get_service_properties(self):
        return {"cors": self.cors, "hour_metrics": object()}

    def set_service_properties(self, cors):
        self.cors, self.sets = cors, self.sets + 1


def _store(auth="key", container=None, svc=None):
    s = AzureBlobStore.__new__(AzureBlobStore)
    s.auth, s._container, s._svc = auth, container or FakeContainer(), svc
    return s


def test_reader_link_uses_a_revocable_policy_and_keeps_other_policies():
    c = FakeContainer({"backup": AccessPolicy(permission="r", expiry="2030-01-01T00:00:00Z")}, public="blob")
    link, expires = _store(container=c).reader_link("alexr", days=30)
    assert link.startswith("https://acct1.blob.core.windows.net/meetings/alexr?")
    assert "si=meetcap-reader" in link and "spr=https" in link and "sig=" in link and "se=" not in link
    assert set(c.policies) == {"backup", READER_POLICY} and str(c.policies[READER_POLICY].permission) == "rl"
    assert c.sets == ["blob"]                                   # public-access level passed back unchanged
    assert 29 <= (expires - datetime.now(timezone.utc)).days <= 30
    assert check_link(link)["prefix"] == "alexr"
    again, _ = _store(container=c).reader_link("alexr", days=60)
    assert again == link                                        # renewing keeps the bookmark working


def test_reader_link_policy_limit_and_revoke():
    full = FakeContainer({f"p{i}": AccessPolicy(permission="r") for i in range(5)})
    with pytest.raises(StorageError, match="5 access policies"):
        _store(container=full).reader_link("x")
    c = FakeContainer({"backup": AccessPolicy(permission="r")})
    s = _store(container=c)
    s.reader_link("x")
    assert s.revoke_reader_link() is True and set(c.policies) == {"backup"}
    assert s.revoke_reader_link() is False
    assert _store(auth="aad", container=c).revoke_reader_link() is False


def test_reader_link_with_a_sas_connection():
    ok = FakeContainer(url="https://acct1.blob.core.windows.net/meetings?sv=2024&sp=racwl&se=2027-01-01T00%3A00%3A00Z&sig=x")
    link, expires = _store(auth="sas", container=ok).reader_link("alex")
    assert link.startswith("https://acct1.blob.core.windows.net/meetings/alex?sv=2024&sp=racwl") and expires.year == 2027
    write_only = FakeContainer(url="https://acct1.blob.core.windows.net/meetings?sv=2024&sp=cw&sig=x")
    with pytest.raises(StorageError, match="Read and List"):
        _store(auth="sas", container=write_only).reader_link("alex")


def test_cors_rule_added_once_and_merged():
    svc = FakeSvc([CorsRule(["https://portal.example"], ["PUT"])])
    s = _store(svc=svc)
    assert s.reader_cors(apply=False) == "needed" and svc.sets == 0
    assert s.reader_cors() == "added" and len(svc.cors) == 2 and svc.cors[0].allowed_origins == "https://portal.example"
    assert svc.cors[1].allowed_origins == "*" and svc.cors[1].allowed_methods == "GET,HEAD"
    assert s.reader_cors() == "present" and svc.sets == 1
    assert _store(svc=FakeSvc([CorsRule(["*"], ["GET", "HEAD", "PUT"])])).reader_cors() == "present"
    with pytest.raises(StorageError, match="5 CORS rules"):
        _store(svc=FakeSvc([CorsRule([f"https://{i}.example"], ["GET"]) for i in range(5)])).reader_cors()
    assert _store(auth="sas").reader_cors() == "unavailable"


@pytest.mark.parametrize("bad, why", [
    ("http://acct1.blob.core.windows.net/meetings?sig=x", "starts with https"),
    ("https://evil.example.com/meetings?sig=x", "starts with https"),
    ("https://acct1.blob.core.windows.net/meetings?sv=1", "signature"),
    ("https://acct1.blob.core.windows.net/?sig=x", "container"),
])
def test_check_link_rejects(bad, why):
    with pytest.raises(ReaderLinkError, match=why):
        check_link(bad)


def test_build_reader_embeds_link_csp_and_sprites(tmp_path):
    link = "https://acct1.blob.core.windows.net/meetings/alexr?sv=2024&si=meetcap-reader&sr=c&sig=a%2Bb</script><b>"
    out = build_reader(link, out=tmp_path / "Meeting Notes.html")
    html = out.read_text()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert 'http-equiv="Content-Security-Policy"' in html and "connect-src https://*.blob.core.windows.net" in html
    assert "/favicon.png" not in html and 'rel="icon" href="data:image/png;base64,' in html
    assert "</script><b>" not in html                              # the link can't close the script element
    cfg = json.loads(re.search(r"window\.MEETCAP_READER = (\{.*?\});</script>", html).group(1))
    assert cfg["link"] == link and cfg["pets"]["sit_r_0"].startswith("data:image/png;base64,")

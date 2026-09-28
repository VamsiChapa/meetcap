import base64
import hashlib
import json

import pytest

from meetcap.obs_client import ObsClient, ObsConnectionError, ObsRequestError, auth_string


def test_auth_string_matches_protocol_steps():
    pw, salt, challenge = "supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", \
        "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="
    secret = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
    expected = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()
    assert auth_string(pw, salt, challenge) == expected


class FakeWS:
    """Scripted OBS: sends Hello with auth, accepts Identify, answers requests."""

    def __init__(self, password, responses):
        self.password, self.responses, self.sent = password, responses, []
        self.outbox = [{"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": "s", "challenge": "c"}}}]

    def recv(self):
        return json.dumps(self.outbox.pop(0))

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["op"] == 1:
            ok = msg["d"].get("authentication") == auth_string(self.password, "s", "c")
            self.outbox.append({"op": 2, "d": {"negotiatedRpcVersion": 1}} if ok else {"op": 9, "d": {}})
        elif msg["op"] == 6:
            rid, rtype = msg["d"]["requestId"], msg["d"]["requestType"]
            self.outbox.append({"op": 5, "d": {"eventType": "Noise"}})  # events must be skipped
            status, data = self.responses[rtype]
            self.outbox.append({"op": 7, "d": {"requestType": rtype, "requestId": rid,
                                               "requestStatus": status, "responseData": data}})

    def close(self):
        pass


def _client(password, responses, server_password="pw"):
    ws = FakeWS(server_password, responses)
    return ObsClient(password=password, connect_fn=lambda *a, **k: ws), ws


def test_call_success_and_error():
    c, ws = _client("pw", {
        "StopRecord": ({"result": True, "code": 100}, {"outputPath": "/tmp/x.mp4"}),
        "StartRecord": ({"result": False, "code": 500, "comment": "already"}, None),
    })
    assert c.call("StopRecord") == {"outputPath": "/tmp/x.mp4"}
    with pytest.raises(ObsRequestError) as e:
        c.call("StartRecord")
    assert e.value.code == 500
    assert ws.sent[0]["d"]["eventSubscriptions"] == 0


def test_wrong_password_rejected():
    c, _ = _client("nope", {})
    with pytest.raises(ObsConnectionError):
        c.call("GetVersion")


def test_missing_password_is_explained():
    c, _ = _client(None, {})
    with pytest.raises(ObsConnectionError, match="setup-obs"):
        c.call("GetVersion")

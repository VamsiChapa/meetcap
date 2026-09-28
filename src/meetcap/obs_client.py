"""Minimal, thread-safe obs-websocket v5 client (request/response only, no event subscriptions).

Protocol: https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
Hello(op 0) -> Identify(op 1) -> Identified(op 2); Request(op 6) -> RequestResponse(op 7).
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable

import websocket  # websocket-client


class ObsConnectionError(ConnectionError):
    pass


class ObsRequestError(RuntimeError):
    def __init__(self, request_type: str, code: int, comment: str):
        super().__init__(f"{request_type} failed ({code}): {comment}")
        self.request_type, self.code, self.comment = request_type, code, comment


# obs-websocket RequestStatus codes we branch on
OUTPUT_RUNNING = 500
OUTPUT_NOT_RUNNING = 501
RESOURCE_ALREADY_EXISTS = 601
RESOURCE_NOT_FOUND = 600
EVENT_INPUT_VOLUME_METERS = 1 << 16  # high-volume event: live audio levels per input


def _recv_json(ws: Any) -> dict[str, Any]:
    """websocket-client returns '' on a close frame (OBS quitting, auth rejected); surface that as a closed socket."""
    raw = ws.recv()
    if not raw:
        raise websocket.WebSocketConnectionClosedException("connection closed by OBS")
    return json.loads(raw)


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4455,
        password: str | None = None,
        timeout: float = 10.0,
        connect_fn: Callable[..., Any] | None = None,
        event_subscriptions: int = 0,
    ):
        self.url = f"ws://{host}:{port}"
        self.password = password
        self.timeout = timeout
        self._connect_fn = connect_fn or websocket.create_connection
        self.event_subscriptions = event_subscriptions
        self._ws: Any = None
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------------------
    def connect(self) -> None:
        try:
            ws = self._connect_fn(self.url, timeout=self.timeout, subprotocols=["obswebsocket.json"])
            hello = _recv_json(ws)
            if hello.get("op") != 0:
                raise ObsConnectionError(f"Unexpected handshake frame: {hello}")
            identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": self.event_subscriptions}
            auth = hello.get("d", {}).get("authentication")
            if auth:
                if not self.password:
                    raise ObsConnectionError("OBS requires a websocket password. Run: meetcap setup-obs")
                identify["authentication"] = auth_string(self.password, auth["salt"], auth["challenge"])
            ws.send(json.dumps({"op": 1, "d": identify}))
            identified = _recv_json(ws)
            if identified.get("op") != 2:
                raise ObsConnectionError(f"Identify rejected: {identified}")
        except ObsConnectionError:
            raise
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            raise ObsConnectionError(f"Cannot reach OBS at {self.url} ({exc}). Wrong password or OBS not running?") from exc
        self._ws = ws

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._ws = None

    def ping(self) -> bool:
        try:
            self.call("GetVersion")
            return True
        except (ObsConnectionError, ObsRequestError):
            return False

    # -- requests ---------------------------------------------------------------
    def call(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            for attempt in (1, 2):  # one transparent reconnect for a stale socket
                if self._ws is None:
                    self.connect()
                rid = uuid.uuid4().hex
                d: dict[str, Any] = {"requestType": request_type, "requestId": rid}
                if data is not None:
                    d["requestData"] = data
                try:
                    self._ws.send(json.dumps({"op": 6, "d": d}))
                    while True:
                        frame = _recv_json(self._ws)
                        if frame.get("op") == 7 and frame.get("d", {}).get("requestId") == rid:
                            break
                    break
                except (websocket.WebSocketException, OSError, ValueError) as exc:
                    self.close()
                    if attempt == 2:
                        raise ObsConnectionError(f"Lost connection to OBS during {request_type}: {exc}") from exc
            status = frame["d"].get("requestStatus", {})
            if not status.get("result"):
                raise ObsRequestError(request_type, int(status.get("code", -1)), status.get("comment", ""))
            return frame["d"].get("responseData") or {}

    def read_events(self, seconds: float, event_type: str | None = None) -> list[dict[str, Any]]:
        """Collect event payloads for `seconds` (needs a client created with event_subscriptions)."""
        out: list[dict[str, Any]] = []
        with self._lock:
            if self._ws is None:
                self.connect()
            deadline = time.monotonic() + seconds
            try:
                self._ws.settimeout(0.5)
            except AttributeError:
                pass
            while time.monotonic() < deadline:
                try:
                    frame = _recv_json(self._ws)
                except websocket.WebSocketTimeoutException:
                    continue
                except (websocket.WebSocketException, OSError, ValueError) as exc:
                    self.close()
                    raise ObsConnectionError(f"OBS event stream closed: {exc}") from exc
                d = frame.get("d", {})
                if frame.get("op") == 5 and (event_type is None or d.get("eventType") == event_type):
                    out.append(d.get("eventData") or {})
            try:
                self._ws.settimeout(self.timeout)
            except AttributeError:
                pass
        return out

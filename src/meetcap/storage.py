"""Where meetings are kept: a folder on this Mac (LocalStore) or Azure Blob (AzureBlobStore). Same interface."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .config import StorageConfig
from .vault import SecretGetter, require

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".ogg": "audio/ogg", ".md": "text/markdown; charset=utf-8", ".json": "application/json",
    ".vtt": "text/vtt; charset=utf-8", ".jpg": "image/jpeg", ".txt": "text/plain; charset=utf-8",
}


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadResult:
    blob: str
    size: int
    md5: str


class BlobStore(Protocol):
    def upload_file(self, local: Path, blob: str, metadata: dict[str, str] | None = None) -> UploadResult: ...
    def upload_text(self, text: str, blob: str) -> UploadResult: ...
    def size(self, blob: str) -> int | None: ...
    def url(self, blob: str) -> str: ...


def file_md5(path: Path, chunk: int = 8 * 1024 * 1024) -> bytes:
    h = hashlib.md5()  # integrity check only
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.digest()


def content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return CONTENT_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


SYNCED_FOLDER_MARKERS = ("/Library/CloudStorage/", "/Mobile Documents/", "/Dropbox/", "/Google Drive/")


class LocalStore:
    """Meetings in a folder: ~/MeetCap by default, or a OneDrive / Google Drive / Dropbox / iCloud folder
    so that app backs them up. Blob names become relative paths under `root`."""
    auth = "local"

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    def _path(self, blob: str) -> Path:
        p = (self.root / blob).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise StorageError(f"refusing to write outside {self.root}: {blob}")
        return p

    def ensure_container(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def upload_file(self, local: Path, blob: str, metadata: dict[str, str] | None = None) -> UploadResult:
        dst = self._path(blob)
        dst.parent.mkdir(parents=True, exist_ok=True)
        size = local.stat().st_size
        tmp = dst.with_name(dst.name + ".partial")
        tmp.unlink(missing_ok=True)
        synced = any(m in str(dst) for m in SYNCED_FOLDER_MARKERS)
        try:
            if synced:
                raise OSError("synced folder: copy, don't link")
            os.link(local, tmp)                    # same disk: instant, and no second copy once the original goes
        except OSError:
            shutil.copyfile(local, tmp)
        os.replace(tmp, dst)
        if dst.stat().st_size != size:
            raise StorageError(f"size mismatch for {blob}: local {size}, stored {dst.stat().st_size}")
        return UploadResult(blob, size, file_md5(dst).hex())

    def upload_text(self, text: str, blob: str) -> UploadResult:
        dst = self._path(blob)
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        tmp = dst.with_name(dst.name + ".partial")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        return UploadResult(blob, len(data), hashlib.md5(data).hexdigest())

    def size(self, blob: str) -> int | None:
        p = self._path(blob)
        return p.stat().st_size if p.exists() else None

    def url(self, blob: str) -> str:
        return self._path(blob).as_uri()

    def list_names(self, prefix: str = "") -> list[str]:
        if not self.root.exists():
            return []
        names = (f.relative_to(self.root).as_posix() for f in self.root.rglob("*") if f.is_file())
        return sorted(n for n in names if n.startswith(prefix) and not n.endswith(".partial"))

    def download_bytes(self, blob: str) -> bytes:
        return self._path(blob).read_bytes()

    def download_range(self, blob: str, offset: int, length: int) -> bytes:
        with open(self._path(blob), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def check_access(self) -> str:
        self.ensure_container()
        probe = self.root / ".meetcap_probe"
        probe.write_text("ok")
        probe.unlink()
        return str(self.root)

    def reader_cors(self, apply: bool = True) -> str:
        return "unavailable"


class AzureBlobStore:
    def __init__(self, cfg: StorageConfig, secrets: SecretGetter, credential=None):
        from azure.storage.blob import BlobServiceClient, ContainerClient

        self.auth = cfg.auth
        self._svc = None
        if cfg.auth == "sas":  # container SAS URL handed out by the storage owner
            self._container = ContainerClient.from_container_url(require(secrets, "storage-sas-url"))
            return
        if cfg.auth == "key":
            svc = BlobServiceClient.from_connection_string(require(secrets, "storage-connection-string"))
        else:
            if credential is None:
                from azure.identity import DefaultAzureCredential
                credential = DefaultAzureCredential()
            svc = BlobServiceClient(account_url=cfg.account_url, credential=credential)
        self._svc = svc
        self._container = svc.get_container_client(cfg.container)

    def ensure_container(self) -> None:
        """Create the container if missing (not possible, and not needed, with a container SAS)."""
        from azure.core.exceptions import ResourceExistsError

        if self.auth == "sas":
            return
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass

    def upload_file(self, local: Path, blob: str, metadata: dict[str, str] | None = None) -> UploadResult:
        from azure.storage.blob import ContentSettings

        size = local.stat().st_size
        md5 = file_md5(local)
        client = self._container.get_blob_client(blob)
        with open(local, "rb") as fh:
            client.upload_blob(
                fh, length=size, overwrite=True, max_concurrency=4, validate_content=True,
                metadata=metadata or {},
                content_settings=ContentSettings(content_type=content_type(blob), content_md5=bytearray(md5)),
            )
        remote = client.get_blob_properties().size
        if remote != size:
            raise StorageError(f"size mismatch for {blob}: local {size}, remote {remote}")
        log.info("uploaded %s (%d bytes)", blob, size)
        return UploadResult(blob, size, md5.hex())

    def upload_text(self, text: str, blob: str) -> UploadResult:
        from azure.storage.blob import ContentSettings

        data = text.encode("utf-8")
        md5 = hashlib.md5(data).digest()
        client = self._container.get_blob_client(blob)
        client.upload_blob(data, overwrite=True,
                           content_settings=ContentSettings(content_type=content_type(blob), content_md5=bytearray(md5)))
        return UploadResult(blob, len(data), md5.hex())

    def size(self, blob: str) -> int | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._container.get_blob_client(blob).get_blob_properties().size
        except ResourceNotFoundError:
            return None

    def url(self, blob: str) -> str:
        return strip_query(self._container.get_blob_client(blob).url)  # never leak a SAS token

    # -- reading back (the notes library) ---------------------------------------------
    def list_names(self, prefix: str = "") -> list[str]:
        return [b.name for b in self._container.list_blobs(name_starts_with=prefix or None)]

    def download_bytes(self, blob: str) -> bytes:
        return self._container.get_blob_client(blob).download_blob().readall()

    def download_range(self, blob: str, offset: int, length: int) -> bytes:
        return self._container.get_blob_client(blob).download_blob(offset=offset, length=length).readall()

    def check_access(self) -> str:
        """Write, read and delete a probe blob. Used by `meetcap doctor` and setup."""
        from azure.core.exceptions import HttpResponseError

        probe = "_meetcap_probe.txt"
        self.upload_text("ok", probe)
        try:
            self._container.get_blob_client(probe).delete_blob()
        except HttpResponseError as exc:
            if exc.status_code != 403:  # a SAS without Delete can still upload; that's enough
                raise
        return strip_query(self._container.url)


    # -- the standalone notes reader ----------------------------------------------------
    def reader_link(self, prefix: str, days: int = 365) -> tuple[str, datetime | None]:
        """A read + list link to `<container>/<prefix>` for the standalone reader, and when it expires.

        key auth: SAS bound to the container's stored access policy `meetcap-reader`, so
        `revoke_reader_link()` kills every copy at once. aad auth: user-delegation SAS (Azure caps it
        at 7 days). sas auth: the configured SAS, if it allows read and list.
        """
        from datetime import timedelta, timezone

        from azure.storage.blob import AccessPolicy, ContainerSasPermissions, generate_container_sas

        base = strip_query(self._container.url).rstrip("/")
        path = f"{base}/{prefix.strip('/')}" if prefix.strip("/") else base
        now = datetime.now(timezone.utc)
        if self.auth == "sas":
            query = self._container.url.split("?", 1)[1] if "?" in self._container.url else ""
            perms = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("sp", "")
            if "r" not in perms or "l" not in perms:
                raise StorageError("your storage link can't read and list (it needs sp=rl or more). "
                                   "Ask the storage owner for a SAS with Read and List.")
            return f"{path}?{query}", sas_expiry(self._container.url)
        name = self._container.container_name
        if self.auth == "key":
            policies = self._policies()
            policies[READER_POLICY] = AccessPolicy(permission=ContainerSasPermissions(read=True, list=True),
                                                   start=now - timedelta(minutes=5), expiry=now + timedelta(days=days))
            if len(policies) > 5:
                raise StorageError("the container already has 5 access policies (Azure's limit); remove one first")
            self._set_policies(policies)
            cred = self._container.credential
            sas = generate_container_sas(cred.account_name, name, account_key=cred.account_key,
                                         policy_id=READER_POLICY, protocol="https")
            return f"{path}?{sas}", now + timedelta(days=days)
        expiry = now + timedelta(days=min(days, 7))
        key = self._svc.get_user_delegation_key(now - timedelta(minutes=5), expiry)
        sas = generate_container_sas(self._svc.account_name, name, user_delegation_key=key,
                                     permission=ContainerSasPermissions(read=True, list=True),
                                     start=now - timedelta(minutes=5), expiry=expiry, protocol="https")
        return f"{path}?{sas}", expiry

    def revoke_reader_link(self) -> bool:
        """Delete the `meetcap-reader` policy: every link made from it stops working within ~30 s."""
        if self.auth != "key":
            return False
        policies = self._policies()
        if policies.pop(READER_POLICY, None) is None:
            return False
        self._set_policies(policies)
        return True

    def _policies(self) -> dict:
        current = self._container.get_container_access_policy()
        self._public_access = current.get("public_access")
        return {s.id: s.access_policy for s in current.get("signed_identifiers") or []}

    def _set_policies(self, policies: dict) -> None:
        # Set Container ACL replaces the whole list and the public-access level, so pass both back.
        self._container.set_container_access_policy(signed_identifiers=policies, public_access=self._public_access)

    def reader_cors(self, apply: bool = True) -> str:
        """Make sure a page opened from a file can read blobs (browsers need a CORS rule for that).

        Adds one GET/HEAD rule for any origin. Reads still need the link's signature, so the rule
        exposes nothing by itself. Returns "present", "added", "needed" (apply=False) or "unavailable".
        """
        if self._svc is None:
            return "unavailable"
        from azure.storage.blob import CorsRule

        rules = list(self._svc.get_service_properties().get("cors") or [])
        if any(_covers_reader(r) for r in rules):
            return "present"
        if not apply:
            return "needed"
        if len(rules) >= 5:
            raise StorageError("the storage account already has 5 CORS rules (Azure's limit)")
        rules.append(CorsRule(["*"], ["GET", "HEAD"], allowed_headers=["*"],
                              exposed_headers=["content-length", "content-range", "content-type", "etag", "last-modified"],
                              max_age_in_seconds=3600))
        self._svc.set_service_properties(cors=rules)   # other service settings are left as they are
        return "added"


READER_POLICY = "meetcap-reader"


def _split(v) -> list[str]:
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v or []]


def _covers_reader(rule) -> bool:
    origins, methods = _split(rule.allowed_origins), [m.upper() for m in _split(rule.allowed_methods)]
    return "*" in origins and "GET" in methods and "HEAD" in methods


def strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def sas_expiry(sas_url: str):
    """Expiry (`se=`) of a SAS URL as an aware datetime, or None."""
    from urllib.parse import parse_qs, urlparse

    se = parse_qs(urlparse(sas_url).query).get("se", [None])[0]
    if not se:
        return None
    try:
        return datetime.fromisoformat(se.replace("Z", "+00:00"))
    except ValueError:
        return None

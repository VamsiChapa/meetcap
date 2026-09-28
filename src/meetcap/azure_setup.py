"""Guided Azure setup (`meetcap azure-setup`).

Asks a handful of questions (every one has a default), then with the az CLI:
  - creates a resource group, a private storage account + `meetings` container (soft delete, lifecycle tiering)
    and a Speech resource in a fast-transcription region,
  - gives your account data access to the storage account (falls back to the storage key if it can't),
  - lets you pick an existing Azure OpenAI resource + deployment (or type one in),
  - writes config.yaml, stores keys in the Keychain, restarts the agent and runs `doctor --deep`.

Safe to re-run: resource names are remembered in azure.json and existing resources are reused.
For team-managed infrastructure use infra/terraform instead.
"""
from __future__ import annotations

import getpass
import json
import re
import secrets as pyrandom
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import CONFIG_PATH, STATE_DIR, load_config

# Regions where Azure Speech fast transcription is available (Microsoft Learn, Speech regions table).
FAST_TRANSCRIPTION_REGIONS = [
    "centralindia", "southeastasia", "eastus", "eastus2", "westus", "westus2", "westus3", "northcentralus",
    "southcentralus", "canadacentral", "brazilsouth", "westeurope", "northeurope", "uksouth", "francecentral",
    "germanywestcentral", "italynorth", "swedencentral", "australiaeast", "japaneast", "japanwest", "koreacentral",
]
LANGUAGES = [
    ("English (US)", ["en-US"]),
    ("English (India)", ["en-IN"]),
    ("English + Hindi (mixed calls)", ["en-IN", "hi-IN"]),
    ("English (UK)", ["en-GB"]),
]
PREFERRED_MODELS = ("gpt-4.1", "gpt-5", "gpt-4o")  # chat models that accept screenshots
CONTAINER = "meetings"
STATE_FILE = STATE_DIR / "azure.json"
BLOB_ROLE = "Storage Blob Data Contributor"

LIFECYCLE_POLICY = {"rules": [{
    "enabled": True, "name": "meetings-tiering", "type": "Lifecycle",
    "definition": {
        "actions": {"baseBlob": {
            "tierToCool": {"daysAfterModificationGreaterThan": 30},
            "tierToCold": {"daysAfterModificationGreaterThan": 90},
            "delete": {"daysAfterModificationGreaterThan": 365},
        }},
        "filters": {"blobTypes": ["blockBlob"], "prefixMatch": [f"{CONTAINER}/"]},
    },
}]}


class AzError(RuntimeError):
    pass


class Az:
    """Thin runner for the az CLI. `az(...)` returns parsed JSON; `az.tsv(...)` returns a string."""

    def __init__(self, exe: str | None = None):
        self.exe = exe or shutil.which("az") or "/opt/homebrew/bin/az"

    def _run(self, args: list[str]) -> str:
        proc = subprocess.run([self.exe, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise AzError(msg[-600:] or f"az {' '.join(args[:3])} failed")
        return proc.stdout.strip()

    def __call__(self, *args: str) -> Any:
        out = self._run([*args, "-o", "json"])
        return json.loads(out) if out else None

    def tsv(self, *args: str) -> str:
        return self._run([*args, "-o", "tsv"])

    def do(self, *args: str) -> None:
        self._run([*args, "-o", "none"])

    def try_(self, *args: str) -> Any:
        try:
            return self(*args)
        except AzError:
            return None

    def login(self) -> None:
        subprocess.run([self.exe, "login", "-o", "none"], check=True)  # opens the browser


class Prompter:
    def __init__(self, ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] = getpass.getpass,
                 out: Callable[[str], None] = print):
        self.ask, self.ask_secret, self.out = ask, ask_secret, out

    def text(self, question: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str:
        while True:
            answer = self.ask(f"  {question}" + (f" [{default}]" if default else "") + ": ").strip() or default
            problem = validate(answer) if validate else (None if answer else "an answer is required")
            if not problem:
                return answer
            self.out(f"    ! {problem}")

    def choose(self, question: str, options: list[str], default: int = 0) -> int:
        self.out(f"  {question}")
        for i, opt in enumerate(options, 1):
            self.out(f"    {i}) {opt}")
        while True:
            raw = self.ask(f"  Enter a number [{default + 1}]: ").strip()
            if not raw:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.out(f"    ! type a number from 1 to {len(options)}")

    def secret(self, question: str) -> str:
        return self.ask_secret(f"  {question}: ").strip()

    def yes(self, question: str, default: bool = True) -> bool:
        raw = self.ask(f"  {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw.startswith("y")


# -- config writing (keeps the comments in config.yaml) ----------------------------------------
def update_config(path: Path, updates: dict[str, Any]) -> None:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedSeq

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resources.files("meetcap").joinpath("config.example.yaml").read_text())
    data = yaml.load(path.read_text()) or {}
    for dotted, value in updates.items():
        node = data
        *parents, leaf = dotted.split(".")
        for key in parents:
            if node.get(key) is None:
                node[key] = {}
            node = node[key]
        if isinstance(value, list):
            seq = CommentedSeq(value)
            seq.fa.set_flow_style()
            value = seq
        node[leaf] = value
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    load_config(path)  # fail loudly if the result is not a valid config


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _label() -> str:
    from .cli import LAUNCH_LABEL
    return LAUNCH_LABEL


def _first_name() -> str:
    try:
        full = subprocess.run(["id", "-F"], capture_output=True, text=True).stdout.strip()
        return full.split()[0] if full else ""
    except OSError:
        return ""


def _valid_region(r: str) -> str | None:
    if r in FAST_TRANSCRIPTION_REGIONS:
        return None
    return "pick one of: " + ", ".join(FAST_TRANSCRIPTION_REGIONS)


def _valid_short(s: str) -> str | None:
    return None if re.fullmatch(r"[a-z][a-z0-9]{2,9}", s) else "3-10 lowercase letters or digits, starting with a letter"


def _valid_https(s: str) -> str | None:
    return None if re.fullmatch(r"https://[A-Za-z0-9.-]+/?", s) else "looks like https://<name>.openai.azure.com"


# -- the wizard ------------------------------------------------------------------------------
def run_setup(az: Az | None = None, p: Prompter | None = None, config_path: Path = CONFIG_PATH,
              state_path: Path = STATE_FILE, store_secret: Callable[[str, str], None] | None = None,
              verify: bool = True, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    az = az or Az()
    p = p or Prompter()
    if store_secret is None:
        from .vault import set_secret as store_secret
    state = _load_state(state_path)

    def step(n: int, title: str) -> None:
        p.out(f"\nStep {n}/6  {title}")

    # 1. sign in ---------------------------------------------------------------------------
    step(1, "Sign in to Azure")
    acct = az.try_("account", "show")
    if not acct:
        p.out("  A browser window will open. Sign in with your work account, then come back here.")
        az.login()
        acct = az("account", "show")
    user = acct.get("user", {}).get("name", "")
    p.out(f"  ✓ signed in as {user}")

    # 2. subscription ----------------------------------------------------------------------
    step(2, "Choose the Azure subscription")
    subs = az("account", "list", "--query", "[?state=='Enabled'].{name:name, id:id, isDefault:isDefault}") or []
    if not subs:
        raise AzError("Your account has no enabled subscriptions. Ask your Azure admin for access to one.")
    default = next((i for i, s in enumerate(subs) if s["id"] == state.get("subscription")),
                   next((i for i, s in enumerate(subs) if s.get("isDefault")), 0))
    sub = subs[p.choose("Which subscription should hold your recordings? (pick the one your team uses for "
                        "internal tools; ask your manager if unsure)",
                        [f"{s['name']}  ({s['id']})" for s in subs], default)]
    az.do("account", "set", "--subscription", sub["id"])
    p.out(f"  ✓ using {sub['name']}")

    # 3. region, names, language -------------------------------------------------------------
    step(3, "Where and how")
    region = p.text("Azure region for your data (press Enter to accept)", state.get("region", "centralindia"),
                    _valid_region)
    short = p.text("Short name used in resource names", state.get("short") or
                   (re.sub(r"[^a-z0-9]", "", _first_name().lower()) or "meetcap")[:10], _valid_short)
    lang = p.choose("Main language spoken in your meetings", [name for name, _ in LANGUAGES],
                    int(state.get("language", 0)))
    reuse = state.get("subscription") == sub["id"] and state.get("short") == short and state.get("names")
    if reuse:
        names = state["names"]
    else:
        suffix = pyrandom.token_hex(2)  # storage and speech names must be globally unique
        names = {"rg": f"rg-meetcap-{short}", "storage": f"stmeetcap{short}{suffix}"[:24],
                 "speech": f"speech-meetcap-{short}-{suffix}"}
    rg, sa, speech = names["rg"], names["storage"], names["speech"]

    # 4. storage + speech ------------------------------------------------------------------
    step(4, f"Create storage and speech in {region} (1-3 minutes)")
    az.do("group", "create", "-n", rg, "-l", region, "--tags", "app=meetcap", f"owner={user}")
    p.out(f"  ✓ resource group {rg}")
    if not az.try_("storage", "account", "show", "-n", sa, "-g", rg):
        az.do("storage", "account", "create", "-n", sa, "-g", rg, "-l", region, "--sku", "Standard_LRS",
              "--kind", "StorageV2", "--min-tls-version", "TLS1_2", "--allow-blob-public-access", "false",
              "--https-only", "true", "--tags", "app=meetcap")
    az.do("storage", "account", "blob-service-properties", "update", "--account-name", sa, "-g", rg,
          "--enable-delete-retention", "true", "--delete-retention-days", "14",
          "--enable-container-delete-retention", "true", "--container-delete-retention-days", "14")
    exists = az("storage", "container-rm", "exists", "--storage-account", sa, "-g", rg, "-n", CONTAINER) or {}
    if not exists.get("exists"):
        az.do("storage", "container-rm", "create", "--storage-account", sa, "-g", rg, "-n", CONTAINER,
              "--public-access", "off")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(LIFECYCLE_POLICY, fh)
    try:
        az.do("storage", "account", "management-policy", "create", "--account-name", sa, "-g", rg,
              "--policy", f"@{fh.name}")
    finally:
        Path(fh.name).unlink(missing_ok=True)
    p.out(f"  ✓ storage account {sa}, private container '{CONTAINER}' "
          "(soft delete 14 d; Cool after 30 d, Cold after 90 d, deleted after 365 d)")

    if not az.try_("cognitiveservices", "account", "show", "-n", speech, "-g", rg):
        az.do("cognitiveservices", "account", "create", "-n", speech, "-g", rg, "-l", region,
              "--kind", "SpeechServices", "--sku", "S0", "--custom-domain", speech, "--yes")
    speech_endpoint = az.tsv("cognitiveservices", "account", "show", "-n", speech, "-g", rg,
                             "--query", "properties.endpoint").rstrip("/")
    speech_key = az.tsv("cognitiveservices", "account", "keys", "list", "-n", speech, "-g", rg, "--query", "key1")
    p.out(f"  ✓ speech resource {speech}")

    storage_auth, conn_str, fresh_role = "aad", "", False
    try:
        oid = az.tsv("ad", "signed-in-user", "show", "--query", "id")
        sa_id = az.tsv("storage", "account", "show", "-n", sa, "-g", rg, "--query", "id")
        have = az.tsv("role", "assignment", "list", "--assignee", oid, "--scope", sa_id, "--role", BLOB_ROLE,
                      "--query", "length(@)")
        if have.strip() in ("", "0"):
            az.do("role", "assignment", "create", "--assignee-object-id", oid, "--assignee-principal-type", "User",
                  "--role", BLOB_ROLE, "--scope", sa_id)
            fresh_role = True
        p.out(f"  ✓ your account can read/write the container ({BLOB_ROLE})")
    except AzError:
        storage_auth = "key"
        conn_str = az.tsv("storage", "account", "show-connection-string", "-n", sa, "-g", rg,
                          "--query", "connectionString")
        p.out("  ! your account can't grant roles in this subscription, so the storage key will be used instead "
              "(kept in your Keychain)")

    # 5. azure openai ----------------------------------------------------------------------
    step(5, "Choose the Azure OpenAI model that writes the notes")
    found = az.try_("cognitiveservices", "account", "list", "--query",
                    "[?kind=='OpenAI' || kind=='AIServices'].{name:name, rg:resourceGroup, location:location, "
                    "subdomain:properties.customSubDomainName, endpoint:properties.endpoint}") or []
    options = [f"{r['name']}  ({r['location']}, group {r['rg']})" for r in found]
    options.append("It's not listed: I'll type the endpoint, deployment name and key")
    if found:
        p.out("  These Azure OpenAI resources are visible in this subscription:")
    else:
        p.out("  No Azure OpenAI resource is visible in this subscription. Get the endpoint, deployment name and")
        p.out("  key from whoever manages your team's Azure OpenAI (Portal -> resource -> Keys and Endpoint).")
    pick = p.choose("Which one should meetcap use?", options, 0)

    aoai_endpoint = aoai_deployment = aoai_key = ""
    if pick < len(found):
        res = found[pick]
        aoai_endpoint = (f"https://{res['subdomain']}.openai.azure.com" if res.get("subdomain")
                         else (res.get("endpoint") or "").rstrip("/"))
        deps = az.try_("cognitiveservices", "account", "deployment", "list", "-n", res["name"], "-g", res["rg"],
                       "--query", "[].{name:name, model:properties.model.name, version:properties.model.version}") or []
        if deps:
            ranked = sorted(deps, key=lambda d: next((i for i, m in enumerate(PREFERRED_MODELS)
                                                      if (d.get("model") or "").startswith(m)), 9))
            di = p.choose("Which deployment? (gpt-4.1, gpt-5 or gpt-4o work best; they can read screenshots)",
                          [f"{d['name']}  (model {d.get('model')} {d.get('version') or ''})".rstrip() for d in ranked], 0)
            aoai_deployment = ranked[di]["name"]
        else:
            aoai_deployment = p.text("No deployments were listed. Deployment name (Foundry -> Deployments)")
        try:
            aoai_key = az.tsv("cognitiveservices", "account", "keys", "list", "-n", res["name"], "-g", res["rg"],
                              "--query", "key1")
        except AzError:
            aoai_key = p.secret("Couldn't read this resource's key. Paste Key 1 from the portal, "
                                "or press Enter to use your az login instead")
    else:
        aoai_endpoint = p.text("Endpoint (e.g. https://my-aoai.openai.azure.com)", "", _valid_https).rstrip("/")
        aoai_deployment = p.text("Deployment name (e.g. gpt-4.1)")
        aoai_key = p.secret("Key 1 (input is hidden; press Enter to use your az login instead)")
    openai_auth = "key" if aoai_key else "aad"
    p.out(f"  ✓ notes will be written by '{aoai_deployment}' at {aoai_endpoint}")

    # 6. save + verify ---------------------------------------------------------------------
    step(6, "Save settings and check everything")
    updates: dict[str, Any] = {
        "storage.backend": "azure",
        "transcription.engine": "azure-speech",
        "summary.provider": "azure-openai",
        "azure.storage.account_url": f"https://{sa}.blob.core.windows.net",
        "azure.storage.container": CONTAINER,
        "azure.storage.prefix": short,
        "azure.storage.auth": storage_auth,
        "azure.speech.endpoint": speech_endpoint,
        "azure.speech.locales": LANGUAGES[lang][1],
        "azure.speech.auth": "key",
        "azure.openai.endpoint": aoai_endpoint,
        "azure.openai.deployment": aoai_deployment,
        "azure.openai.auth": openai_auth,
    }
    current = load_config(config_path) if config_path.exists() else None
    if not (current and current.processing.user_name) and _first_name():
        updates["processing.user_name"] = _first_name()
    update_config(config_path, updates)
    p.out(f"  ✓ saved {config_path}")

    store_secret("speech-key", speech_key)
    if aoai_key:
        store_secret("openai-key", aoai_key)
    if conn_str:
        store_secret("storage-connection-string", conn_str)
    p.out("  ✓ keys stored in your macOS Keychain (service 'meetcap')")

    state = {"subscription": sub["id"], "region": region, "short": short, "language": lang, "names": names}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))

    if verify:
        _verify(config_path, storage_auth, fresh_role, p, sleep)
    return {"updates": updates, "names": names, "storage_auth": storage_auth, "openai_auth": openai_auth}


def _verify(config_path: Path, storage_auth: str, fresh_role: bool, p: Prompter,
            sleep: Callable[[float], None]) -> None:
    from .storage import AzureBlobStore
    from .vault import get_secret

    cfg = load_config(config_path)
    if storage_auth == "aad" and fresh_role:
        p.out("  Waiting for Azure to activate your new storage access (usually 1-5 minutes)")
        store = AzureBlobStore(cfg.azure.storage, get_secret)
        for attempt in range(20):
            try:
                store.check_access()
                p.out("  ✓ storage access is active")
                break
            except Exception:  # noqa: BLE001 - propagation delay shows up as 403
                if attempt == 19:
                    p.out("  ! still not active; run `meetcap doctor --deep` again in 10 minutes")
                sleep(30)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{_label()}"], capture_output=True)
    p.out("  ✓ restarted the menu bar agent with the new settings\n")
    subprocess.run([sys.executable, "-m", "meetcap.cli", "doctor", "--deep"])


def _uid() -> int:
    import os
    return os.getuid()


# =============================================================================================
# "I already have the keys" mode: nothing is created in Azure, every value is tested on entry.
# =============================================================================================
@dataclass
class StorageTarget:
    kind: str            # key | sas | aad
    account_url: str
    container: str | None
    secret: str = ""     # connection string or SAS URL


def parse_storage(raw: str) -> StorageTarget:
    s = raw.strip().strip('"').strip("'")
    if "AccountKey=" in s or "SharedAccessSignature=" in s or s.startswith("DefaultEndpointsProtocol"):
        parts = dict(p.split("=", 1) for p in s.split(";") if "=" in p)
        if parts.get("BlobEndpoint"):
            url = parts["BlobEndpoint"].rstrip("/")
        elif parts.get("AccountName"):
            url = f"https://{parts['AccountName']}.blob.{parts.get('EndpointSuffix', 'core.windows.net')}"
        else:
            raise ValueError("that connection string has no AccountName; copy the whole string again")
        return StorageTarget("key", url, None, s)
    if s.startswith("https://"):
        u = urlparse(s)
        base = f"https://{u.netloc}"
        container = u.path.strip("/").split("/")[0] or None
        if "sig=" in u.query:
            return StorageTarget("sas", base, container, s)
        return StorageTarget("aad", base, container)
    if re.fullmatch(r"[a-z0-9]{3,24}", s):
        return StorageTarget("aad", f"https://{s}.blob.core.windows.net", None)
    raise ValueError("paste the storage connection string, a container SAS URL, or type the storage account name")


def with_container(t: StorageTarget, container: str) -> StorageTarget:
    t.container = container
    if t.kind == "sas":
        u = urlparse(t.secret)
        t.secret = f"https://{u.netloc}/{container}?{u.query}"
    return t


def parse_speech_location(raw: str) -> str:
    s = raw.strip()
    if s.lower().startswith("http"):
        u = urlparse(s)
        host = u.netloc.lower()
        if not host:
            raise ValueError("that doesn't look like an endpoint URL")
        if host.endswith((".openai.azure.com", ".services.ai.azure.com")):
            host = host.split(".")[0] + ".cognitiveservices.azure.com"  # Speech lives on this host of the resource
        return f"https://{host}"
    region = re.sub(r"[^a-z0-9]", "", s.lower())  # "East US 2" -> "eastus2"
    if not re.fullmatch(r"[a-z]{4,}[0-9]?", region):
        raise ValueError("type the region (e.g. eastus, centralindia) or paste the endpoint URL")
    return f"https://{region}.api.cognitive.microsoft.com"


def parse_openai_endpoint(raw: str) -> tuple[str, str | None]:
    """Accepts the resource endpoint or a full Foundry 'Target URI'. Returns (endpoint, deployment hint)."""
    u = urlparse(raw.strip())
    host = u.netloc.lower()
    if u.scheme != "https" or not host:
        raise ValueError("paste the https://... endpoint from Keys and Endpoint (or the Target URI)")
    if host.endswith(".api.cognitive.microsoft.com"):
        raise ValueError("that's a regional Speech endpoint; the Azure OpenAI one looks like https://<name>.openai.azure.com")
    name = host.split(".")[0]
    if host.endswith((".cognitiveservices.azure.com", ".services.ai.azure.com")):
        endpoint = f"https://{name}.openai.azure.com"  # AI Services / Foundry resources also serve this host
    else:
        endpoint = f"https://{host}"
    m = re.search(r"/deployments/([^/?]+)", u.path)
    return endpoint, (m.group(1) if m else None)


_EXPLAIN = [
    (r"KeyBasedAuthenticationNotPermitted", "This storage account doesn't allow key access. Ask its owner for a container "
     "SAS URL instead (Containers -> your container -> Shared access tokens)."),
    (r"DeploymentNotFound", "No deployment with that name. Copy the name exactly from Foundry -> Deployments."),
    (r"rejected every format|OperationNotSupported|does not work with the specified model|not supported.*audio",
     "That deployment isn't a transcription model. Use one whose model is gpt-4o-transcribe-diarize, "
     "gpt-4o-transcribe or whisper."),
    (r"AuthenticationFailed|Server failed to authenticate", "The storage key or SAS was rejected (wrong or expired). "
     "Copy it again from the portal."),
    (r"AuthorizationPermissionMismatch|AuthorizationFailure|not authorized to perform",
     "Access denied. A SAS URL needs Read, Add, Create, Write and List permissions; an Azure login needs the "
     "'Storage Blob Data Contributor' role."),
    (r"ContainerNotFound", "That container doesn't exist. Check the name, or ask the owner to create it."),
    (r"\b401\b|invalid subscription key|Unauthorized|AuthenticationError|invalid.*api key",
     "The key was rejected. Use KEY 1 or KEY 2 of this same resource, and the region/endpoint of that resource."),
    (r"nodename nor servname|Name or service not known|getaddrinfo|NameResolution|Failed to resolve|"
     r"ConnectError|APIConnectionError|Connection error",
     "Couldn't reach that address. Check the spelling of the region or endpoint, and your VPN/internet."),
    (r"ProxyError|Tunnel connection failed", "A network proxy blocked the connection. Check your VPN/proxy, or try "
     "another network."),
    (r"\b403\b|Forbidden|firewall|Public access is disabled|public network access",
     "Blocked by the resource's network rules. Connect to the VPN, or ask the owner to allow your network."),
    (r"\b404\b|Resource not found|NotFound", "Nothing at that address. Check the region or endpoint."),
    (r"ffmpeg not found", "ffmpeg is missing. Run: brew install ffmpeg"),
]


def explain(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for pattern, message in _EXPLAIN:
        if re.search(pattern, text, re.IGNORECASE):
            return message
    return text[:300]


class Checker:
    """Live checks used while pasting keys. Each raises on failure."""

    def storage(self, t: StorageTarget) -> None:
        from .config import StorageConfig
        from .storage import AzureBlobStore

        name = {"key": "storage-connection-string", "sas": "storage-sas-url"}.get(t.kind, "")
        store = AzureBlobStore(StorageConfig(account_url=t.account_url, container=t.container or CONTAINER,
                                             auth=t.kind), lambda n: t.secret if n == name else None)
        store.ensure_container()
        store.check_access()

    def speech(self, endpoint: str, key: str) -> None:
        from .config import SpeechConfig
        from .transcribe import AzureFastTranscriber, TranscriptionError

        with tempfile.TemporaryDirectory() as tmp:
            probe = _probe_audio(Path(tmp))
            tr = AzureFastTranscriber(SpeechConfig(endpoint=endpoint, auth="key", max_speakers=0, timeout_seconds=60),
                                      lambda n: key)
            try:
                tr._post(probe)
            except TranscriptionError as exc:
                if "returned 400" in str(exc):
                    return  # key and endpoint were accepted; the tone just isn't speech
                raise

    def openai_transcribe(self, endpoint: str, deployment: str, key: str) -> dict[str, str]:
        """Check a transcription deployment; returns the response format and API flavour that work."""
        from .config import OpenAIConfig
        from .transcribe import TRANSCRIBE_DATED_API, AzureOpenAITranscriber

        with tempfile.TemporaryDirectory() as tmp:
            probe = _probe_audio(Path(tmp), seconds=3)
            last: Exception | None = None
            for api_version in ("", TRANSCRIBE_DATED_API):
                cfg = OpenAIConfig(endpoint=endpoint, auth="key", transcribe_deployment=deployment,
                                   transcribe_api_version=api_version, timeout_seconds=90)
                try:
                    fmt = AzureOpenAITranscriber(cfg, lambda n: key).detect_format(probe)
                    return {"format": fmt, "api_version": api_version}
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    if getattr(exc, "status_code", None) != 404 and "404" not in str(exc):
                        raise
            assert last is not None
            raise last

    def local(self, model: str, out: Callable[[str], None] = print) -> None:
        """Make sure on-Mac Whisper is installed and its model is downloaded (first run ~1.6 GB)."""
        from . import local_whisper

        ok, why = local_whisper.supported()
        if not ok:
            raise RuntimeError(why)
        if not local_whisper.installed():
            out("  Installing the Whisper runtime for Apple Silicon (about a minute)...")
            local_whisper.install()
        out("  Downloading the speech model the first time (about 1.6 GB; a few minutes on office Wi-Fi)...")
        with tempfile.TemporaryDirectory() as tmp:
            local_whisper.run(_probe_audio(Path(tmp), seconds=3), Path(tmp) / "out.json", model, timeout=3600)

    def openai(self, endpoint: str, deployment: str, key: str, api_version: str = "") -> None:
        from .summarize import make_client

        client = make_client(endpoint, key, api_version, timeout=60, max_retries=1)
        r = client.chat.completions.create(model=deployment, response_format={"type": "json_object"},
                                           messages=[{"role": "user", "content": 'Reply with the JSON {"ok": true}'}])
        if "ok" not in (r.choices[0].message.content or ""):
            raise RuntimeError("the model answered, but not in JSON mode; pick a gpt-4.1 / gpt-4o / gpt-5 deployment")


DATED_API_VERSION = "2024-10-21"  # GA dated API; fallback for resources/gateways without the v1 route
DEFAULT_LOCAL_MODEL = "mlx-community/whisper-large-v3-turbo"


def _probe_audio(folder: Path, seconds: int = 1) -> Path:
    from . import media

    probe = folder / "probe.ogg"
    media._run([media._tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=16000:duration={seconds}", "-ac", "1", "-c:a", "libopus", str(probe)])
    return probe


def run_existing(p: Prompter | None = None, config_path: Path = CONFIG_PATH, state_path: Path = STATE_FILE,
                 store_secret: Callable[[str, str], None] | None = None, checker: Checker | None = None,
                 verify: bool = True) -> dict[str, Any]:
    p = p or Prompter()
    checker = checker or Checker()
    if store_secret is None:
        from .vault import set_secret as store_secret
    state = _load_state(state_path)

    def section(n: int, title: str) -> None:
        p.out(f"\nPart {n}/4  {title}")

    def check(label: str, fn: Callable[[], None]) -> bool:
        p.out(f"  Testing {label}...")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - shown to the user in plain words
            p.out(f"  ✗ {explain(exc)}")
            return False
        p.out(f"  ✓ {label} works")
        return True

    p.out("You'll paste the Azure details you already have. Nothing is created in Azure; each value is tested "
          "right away.\nPasted keys are hidden while you type or paste. That's normal: paste, then press Enter.")

    # 1. storage ---------------------------------------------------------------------------
    section(1, "Storage: where recordings, transcripts and notes are uploaded")
    p.out("  Where to find it (use ONE of these):\n"
          "   - Connection string: Portal -> your Storage account -> Security + networking -> Access keys ->\n"
          "     key1 -> Connection string -> Show -> copy\n"
          "   - SAS URL: Portal -> Storage account -> Containers -> your container -> Shared access tokens ->\n"
          "     tick Read, Add, Create, Write, List -> set an expiry -> Generate -> copy 'Blob SAS URL'")
    while True:
        try:
            target = parse_storage(p.secret("Paste the connection string or SAS URL (or type the account name)"))
        except ValueError as exc:
            p.out(f"    ! {exc}")
            continue
        if not target.container:
            with_container(target, p.text("Container name (created if it doesn't exist)",
                                          state.get("container", CONTAINER),
                                          lambda c: None if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", c)
                                          else "3-63 lowercase letters, digits or hyphens"))
        if check("upload to storage", lambda t=target: checker.storage(t)) or not p.yes("Try the storage details again?"):
            break
    if target.kind == "sas":
        from .storage import sas_expiry
        exp = sas_expiry(target.secret)
        if exp:
            p.out(f"  Note: this SAS URL stops working on {exp:%d %b %Y}. Run `meetcap azure-setup --existing` "
                  "with a new one before then.")

    # 2. azure openai (notes) ---------------------------------------------------------------
    section(2, "Azure OpenAI: writes the notes")
    p.out("  Where to find it: Foundry (ai.azure.com) -> your resource -> Deployments -> click the chat model\n"
          "  (e.g. gpt-4.1) -> copy 'Target URI' and 'Key'. Or Portal -> Azure OpenAI resource -> Keys and Endpoint.\n"
          "  Pasting the Target URI fills in the deployment name for you.")
    api_version = ""
    while True:
        raw = p.text("Azure OpenAI endpoint or Target URI", state.get("openai_endpoint", ""),
                     lambda s: _try(parse_openai_endpoint, s))
        aoai_endpoint, hint = parse_openai_endpoint(raw)
        deployment = p.text("Chat deployment name (gpt-4.1 is best; gpt-4o / gpt-4o-mini also work)",
                            hint or state.get("openai_deployment", "gpt-4.1"))
        aoai_key = p.secret("Azure OpenAI Key (KEY 1)")
        api_version = ""
        ok = check(f"Azure OpenAI deployment '{deployment}'",
                   lambda e=aoai_endpoint, d=deployment, k=aoai_key: checker.openai(e, d, k))
        if not ok:
            p.out("  Trying the older dated API on the same endpoint...")
            ok = check(f"Azure OpenAI deployment '{deployment}' (dated API)",
                       lambda e=aoai_endpoint, d=deployment, k=aoai_key: checker.openai(e, d, k, DATED_API_VERSION))
            api_version = DATED_API_VERSION if ok else ""
        if ok or not p.yes("Try the Azure OpenAI details again?"):
            break

    # 3. transcription ---------------------------------------------------------------------
    section(3, "Transcription: turns the audio into text")
    transcriber, speech_endpoint, speech_key, tx = "speech", "", "", {"format": "", "api_version": ""}
    tx_deployment = ""
    local_model = DEFAULT_LOCAL_MODEL
    same_resource = f"https://{urlparse(aoai_endpoint).netloc.split('.')[0]}.cognitiveservices.azure.com"
    p.out("  Checking whether your Azure OpenAI resource also includes Azure Speech "
          "(AI Services / Foundry resources do)...")
    try:
        checker.speech(same_resource, aoai_key)
        speech_endpoint, speech_key = same_resource, aoai_key
        p.out("  ✓ it does: transcripts will use Azure Speech on the same resource, with speaker labels")
    except Exception:  # noqa: BLE001 - expected for a plain Azure OpenAI resource
        p.out("  It doesn't (it's a plain Azure OpenAI resource). Choose how to transcribe:")
        order = ["local", "openai", "speech"]
        choice = p.choose("How should meetcap transcribe?", [
            "On this Mac (free and private; no extra Azure resource; your voice and the others are labelled apart). "
            "Recommended",
            "A transcription model in this Azure OpenAI resource (only if its region offers one: eastus2, centralus, "
            "canadacentral, swedencentral, francecentral, southindia)",
            "A separate Azure Speech resource (best speaker labels; you need its KEY 1 and region)",
        ], order.index(state.get("transcriber", "local")) if state.get("transcriber") in order else 0)
        transcriber = order[choice]
        if transcriber == "local":
            while True:
                if (check("transcription on this Mac", lambda: checker.local(local_model, p.out))
                        or not p.yes("Try again?")):
                    break
        elif transcriber == "openai":
            p.out("  To add one: Foundry (ai.azure.com) -> your resource -> Deployments -> + Deploy model ->\n"
                  "  Deploy base model -> search 'transcribe' -> gpt-4o-transcribe-diarize (or gpt-4o-transcribe /\n"
                  "  whisper) -> Confirm -> Deploy. If it says 'region not supported', choose option 1 instead.")
            while True:
                tx_deployment = p.text("Transcription deployment name",
                                       state.get("transcribe_deployment", "gpt-4o-transcribe-diarize"))
                ok = check(f"transcription deployment '{tx_deployment}'",
                           lambda d=tx_deployment: tx.update(checker.openai_transcribe(aoai_endpoint, d, aoai_key)))
                if ok:
                    labels = {"diarized_json": "with speaker labels", "verbose_json": "with timestamps",
                              "json": "text only; timestamps are approximate"}
                    p.out(f"  ✓ transcripts will be {labels.get(tx['format'], tx['format'])}")
                if ok or not p.yes("Try another transcription deployment?"):
                    break
        else:
            p.out("  Where to find it: Portal -> your Speech resource -> Keys and Endpoint -> KEY 1 and Location/Region")
            while True:
                speech_key = p.secret("Speech KEY 1")
                speech_endpoint = parse_speech_location(p.text(
                    "Speech region or endpoint", state.get("speech_location", "eastus"),
                    lambda s: _try(parse_speech_location, s)))
                if (check("Speech", lambda e=speech_endpoint, k=speech_key: checker.speech(e, k))
                        or not p.yes("Try the Speech details again?")):
                    break

    # 4. language + save -------------------------------------------------------------------
    section(4, "Meeting language, then save")
    lang = p.choose("Main language spoken in your meetings", [name for name, _ in LANGUAGES],
                    int(state.get("language", 0)))
    short = state.get("short") or re.sub(r"[^a-z0-9]", "", _first_name().lower())[:10] or "meetings"
    updates: dict[str, Any] = {
        "storage.backend": "azure",
        "summary.provider": "azure-openai" if aoai_endpoint else "none",
        "transcription.engine": {"speech": "azure-speech", "openai": "azure-openai"}.get(transcriber, transcriber),
        "azure.storage.account_url": target.account_url,
        "azure.storage.container": target.container or CONTAINER,
        "azure.storage.prefix": short,
        "azure.storage.auth": target.kind,
        "azure.speech.locales": LANGUAGES[lang][1],
        "azure.openai.endpoint": aoai_endpoint,
        "azure.openai.deployment": deployment,
        "azure.openai.auth": "key",
        "azure.openai.api_version": api_version,
    }
    if transcriber == "speech":
        updates.update({"azure.speech.endpoint": speech_endpoint, "azure.speech.auth": "key"})
    elif transcriber == "local":
        updates["transcription.local_model"] = local_model
    else:
        updates.update({"azure.openai.transcribe_deployment": tx_deployment,
                        "azure.openai.transcribe_format": tx["format"],
                        "azure.openai.transcribe_api_version": tx["api_version"]})
    current = load_config(config_path) if config_path.exists() else None
    if not (current and current.processing.user_name) and _first_name():
        updates["processing.user_name"] = _first_name()
    update_config(config_path, updates)
    p.out(f"  ✓ saved {config_path}")

    store_secret("openai-key", aoai_key)
    if transcriber == "speech":
        store_secret("speech-key", speech_key)
    if target.kind == "key":
        store_secret("storage-connection-string", target.secret)
    elif target.kind == "sas":
        store_secret("storage-sas-url", target.secret)
    p.out("  ✓ keys stored in your macOS Keychain (service 'meetcap')")

    state.update(mode="existing", container=target.container, transcriber=transcriber,
                 speech_location=speech_endpoint or state.get("speech_location", "eastus"),
                 transcribe_deployment=tx_deployment or state.get("transcribe_deployment", ""),
                 openai_endpoint=aoai_endpoint, openai_deployment=deployment, language=lang, short=short)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))

    if verify:
        if load_config(config_path).split_speakers and p.yes(
                "Update OBS to record your voice and the meeting on separate tracks (labels You vs Others)? "
                "OBS closes and reopens once"):
            try:
                from .agent import setup_obs
                setup_obs(load_config(config_path), on_step=lambda line_: p.out(
                    f"  ✗ {line_[9:]}" if line_.startswith("PROBLEM: ") else
                    f"  ! {line_[9:]}" if line_.startswith("WARNING: ") else f"  ✓ {line_}"))
            except Exception as exc:  # noqa: BLE001
                p.out(f"  ! OBS update didn't finish ({explain(exc)}). Run later: meetcap setup-obs")
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{_label()}"], capture_output=True)
        p.out("  ✓ restarted the menu bar agent with the new settings\n")
        subprocess.run([sys.executable, "-m", "meetcap.cli", "doctor", "--deep"])
    return {"updates": updates, "storage_kind": target.kind, "transcriber": transcriber}


def _try(fn: Callable[[str], Any], value: str) -> str | None:
    try:
        fn(value)
        return None
    except ValueError as exc:
        return str(exc)

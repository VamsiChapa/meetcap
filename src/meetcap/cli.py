"""Command-line interface. A thin consumer of the SDK (agent, pipeline, jobs)."""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, DB_PATH, Config, ConfigError, ensure_dirs, load_config, problems
from .logs import setup_logging
from .models import Status
from .vault import KNOWN_SECRETS, delete_secret, get_secret, set_secret

OK, BAD, WARN = "✓", "✗", "!"
PET_NAMES = ["puppy", "kitten", "lion_cub", "fox", "panda", "bunny", "bear_cub", "tiger_cub", "raccoon", "koala"]


def _cfg() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        sys.exit(f"{BAD} {CONFIG_PATH}: {exc}")


# -- commands ----------------------------------------------------------------------
def cmd_init(args) -> None:
    if CONFIG_PATH.exists() and not args.force:
        print(f"{OK} config already exists: {CONFIG_PATH}")
        return
    import platform

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(resources.files("meetcap").joinpath("config.example.yaml").read_text())
    if platform.machine() != "arm64":     # Whisper on the Mac needs Apple Silicon; pick an engine in setup
        from .azure_setup import update_config
        update_config(CONFIG_PATH, {"transcription.engine": "none"})
    print(f"{OK} wrote {CONFIG_PATH}\n  Next: meetcap setup")


LAUNCH_LABEL = "com.meetcap.agent"


def cmd_config(args) -> None:
    if not CONFIG_PATH.exists():
        cmd_init(argparse.Namespace(force=False))
    subprocess.run(["open", "-e", str(CONFIG_PATH)], check=False)
    print(f"{OK} opened {CONFIG_PATH}\n  After saving, run: meetcap restart")


def cmd_restart(args) -> None:
    import os

    target = f"gui/{os.getuid()}/{LAUNCH_LABEL}"
    r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{BAD} could not restart {target}: {(r.stderr or r.stdout).strip()} (was install.sh run?)")
    print(f"{OK} restarted the menu bar agent; new config is live")


def cmd_azure_setup(args) -> None:
    from .azure_setup import Az, AzError, Prompter, run_existing, run_setup

    p = Prompter()
    print("meetcap Azure setup. Questions show a default in [brackets]; press Enter to accept it.\n")
    try:
        if args.existing:
            mode = 0
        elif args.create:
            mode = 1
        else:
            mode = p.choose("How should meetcap connect to Azure?", [
                "I already have the keys (Storage, Speech, Azure OpenAI): I'll paste them. No Azure permissions needed.",
                "Create new Azure resources for me (needs permission to create resource groups).",
            ], 0)
        if mode == 0:
            run_existing(p=p, verify=not args.no_verify)
            return
        if not Path(Az().exe).exists():
            sys.exit(f"{BAD} Azure CLI not found. Install it with: brew install azure-cli")
        try:
            run_setup(p=p, verify=not args.no_verify)
        except AzError as exc:
            if "AuthorizationFailed" not in str(exc):
                raise
            print(f"\n{WARN} Your account can't create resources in that subscription (Azure said AuthorizationFailed).")
            if p.yes("Use the keys you already have instead?"):
                run_existing(p=p, verify=not args.no_verify)
            else:
                sys.exit("Ask your Azure admin for Contributor access, then run: meetcap azure-setup --create")
    except AzError as exc:
        sys.exit(f"\n{BAD} Azure refused a step:\n  {exc}\n\nFix the cause above and run `meetcap azure-setup` again. "
                 "It reuses what it already created.")
    except KeyboardInterrupt:
        sys.exit("\ncancelled; run `meetcap azure-setup` again any time")


def _read_status() -> dict | None:
    import time

    from .agent import STATUS_PATH
    try:
        st = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return None
    return st if time.time() - float(st.get("updated", 0)) < 15 else None


def _fmt_elapsed(since: float | None) -> str:
    import time

    secs = int(time.time() - (since or time.time()))
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def cmd_status(args) -> None:
    st = _read_status()
    if not st:
        sys.exit(f"{BAD} the meetcap agent isn't running. Start it with: meetcap restart")
    if st["recording"]:
        from .pipeline import app_short
        print(f"● RECORDING {_fmt_elapsed(st.get('since'))}  ({app_short(st.get('app') or 'manual')}, "
              f"{'manual' if st.get('manual') else 'auto'})")
    else:
        print("◌ idle: " + ("waiting for a Teams/Zoom call" if st.get("auto") else "auto-record is OFF"))
    c = st.get("counts", {})
    print(f"  queue: {c.get('pending', 0)} pending · {c.get('done', 0)} done · {c.get('failed', 0)} failed"
          f"{' · ' + str(c.get('skipped')) + ' skipped' if c.get('skipped') else ''}")
    if st.get("last_error"):
        print(f"  {WARN} {st['last_error']}")


def _send(cmd: str, done, wait: float = 75.0) -> dict | None:
    import time

    from .agent import CONTROL_PATH
    if not _read_status():
        sys.exit(f"{BAD} the meetcap agent isn't running. Start it with: meetcap restart")
    CONTROL_PATH.write_text(json.dumps({"cmd": cmd, "at": time.time()}))
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        st = _read_status()
        if st and not CONTROL_PATH.exists() and done(st):
            return st
    return _read_status()


def cmd_start(args) -> None:
    print("Starting recording (OBS may take up to a minute to open)...")
    st = _send("start", lambda s: s["recording"] or bool(s.get("last_error")))
    if st and st["recording"]:
        print(f"{OK} recording. Stop with: meetcap stop")
    else:
        sys.exit(f"{BAD} recording did not start: {(st or {}).get('last_error') or 'no answer from the agent'}")


def cmd_stop(args) -> None:
    st = _send("stop", lambda s: not s["recording"])
    if st and not st["recording"]:
        print(f"{OK} stopped. The file is queued; watch it with: meetcap jobs")
    else:
        sys.exit(f"{BAD} still recording; try again in a few seconds")


def cmd_auto(args) -> None:
    on = args.state == "on"
    st = _send("auto-on" if on else "auto-off", lambda s: s["auto"] == on, wait=15)
    print(f"{OK} auto-record is {'ON' if on else 'OFF'}" if st and st["auto"] == on else f"{BAD} no answer")


def cmd_notes(args) -> None:
    cfg = _cfg()
    url = f"http://127.0.0.1:{cfg.ui.library_port}"
    if args.serve:
        import signal
        import threading

        from .agent import MeetCapAgent
        from .webui import serve
        agent = MeetCapAgent(cfg)
        serve(agent.library, cfg.ui.library_port, status_fn=agent.status_dict)
        threading.Thread(target=agent.library.sync, daemon=True).start()
        print(f"{OK} notes library at {url} (Ctrl+C to stop)")
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        stop.wait()
        return
    if cfg.reader_path.exists() and not args.local:
        subprocess.run(["open", str(cfg.reader_path)], check=False)
        print(f"{OK} opened {cfg.reader_path}  (bookmark it)")
        return
    if not _read_status():
        sys.exit(f"{BAD} meetcap isn't running, so the local library isn't either. Either start it (meetcap restart), "
                 "or make the reader that works without meetcap: meetcap reader")
    subprocess.run(["open", url], check=False)
    print(f"{OK} opened {url}  (bookmark it)")


def cmd_reader(args) -> None:
    """Write the standalone reader: an HTML file that reads meetings straight from Azure."""
    from .reader import build_reader
    from .storage import AzureBlobStore, StorageError, strip_query

    cfg = _cfg()
    if cfg.storage.backend == "local":
        from .reader import publish_static
        path = publish_static(cfg.local_root, species=cfg.ui.pet_species)
        print(f"{OK} wrote {path}")
        print("  It reads your meetings folder directly, so it works whether or not meetcap is running.")
        if not args.no_open:
            subprocess.run(["open", str(path)], check=False)
            print("  Opened it in your browser. Bookmark that page.")
        return
    try:
        blob = AzureBlobStore(cfg.azure.storage, get_secret)
        if args.revoke:
            if blob.revoke_reader_link():
                print(f"{OK} revoked. Every reader link stops working within about 30 seconds.")
                print("  Run `meetcap reader` when you want a new one.")
            elif blob.auth == "key":
                print(f"{WARN} there was no reader link to revoke.")
            else:
                print(f"{WARN} links made with az login expire by themselves within 7 days; "
                      "a storage SAS link is revoked by whoever issued it.")
            return
        account = strip_query(blob._container.url).split("//", 1)[-1].split(".", 1)[0]
        cors = blob.reader_cors(apply=False)
        if cors == "needed":
            print(f"A page opened from a file on your Mac can only read Azure if the storage account allows it.\n"
                  f"meetcap will add one rule to storage account '{account}': browsers may send read requests (GET, HEAD).\n"
                  "Each read still needs your reader link, so the rule opens nothing by itself.")
            if args.yes or input("Add the rule now? [Y/n] ").strip().lower() in ("", "y", "yes"):
                blob.reader_cors(apply=True)
                print(f"{OK} added the read rule to '{account}'")
            else:
                print(f"{WARN} skipped. The reader will show \"Couldn't reach Azure\" until the rule exists.")
        elif cors == "unavailable":
            print(f"{WARN} your storage is connected with a SAS link, so meetcap can't add the browser rule itself.\n"
                  "  Ask the storage owner to add a CORS rule on the Blob service: origin *, methods GET and HEAD.")
        link, expires = blob.reader_link(cfg.azure.storage.prefix, days=args.days)
    except StorageError as exc:
        sys.exit(f"{BAD} {exc}")
    except Exception as exc:  # noqa: BLE001 - Azure errors are long; show the first line
        sys.exit(f"{BAD} Azure said: {str(exc).splitlines()[0][:300]}")
    path = build_reader(link, out=cfg.reader_path, expires=expires, species=cfg.ui.pet_species)
    when = expires.strftime("%-d %B %Y") if expires else "when your storage link expires"
    print(f"{OK} wrote {path}")
    print(f"  It reads straight from Azure, so it works whether or not meetcap is running. The link lasts until {when}.")
    if args.print:
        print("\nReader link (anyone who has it can read your meetings; paste it only into the reader):\n" + link + "\n")
    if not args.no_open:
        subprocess.run(["open", str(path)], check=False)
        print("  Opened it in your browser. Bookmark that page.")
    print("  New link or lost access: run `meetcap reader` again. Stop every link: `meetcap reader --revoke`.")


def cmd_setup(args) -> None:
    """The setup screen in your browser: storage, transcription, notes, OBS and the desktop pet."""
    from .setupui import run
    run(open_browser=not args.no_open)


def cmd_pet(args) -> None:
    from .azure_setup import update_config

    from .pet import SPECIES, available_species

    cfg = _cfg()
    if args.state == "list":
        for name in available_species():
            print(f"{'*' if name == cfg.ui.pet_species else ' '} {name:<10} {SPECIES[name]}")
        print("\nPick one: meetcap pet <name>   (or in meetcap setup)")
        return
    if args.state == "test":
        print("Opening the pet on its own for 30 seconds...")
        r = subprocess.run([sys.executable, "-m", "meetcap.pet", "30", cfg.ui.pet_species])
        if r.returncode != 0:
            sys.exit(f"{BAD} the pet couldn't be shown (see the error above).")
        return
    if args.state in SPECIES:
        update_config(CONFIG_PATH, {"ui.pet_species": args.state, "ui.pet": True})
        print(f"{OK} your pet is now the {SPECIES[args.state].lower()}.")
        cmd_restart(args)
        return
    on = args.state == "on"
    update_config(CONFIG_PATH, {"ui.pet": on})
    if _read_status():
        _send("pet-on" if on else "pet-off", lambda s: True, wait=5)
    print(f"{OK} the pet is {'on your screen now' if on else 'back in the menu bar only'}"
          + ("" if _read_status() else " (takes effect when meetcap starts: meetcap restart)"))


def cmd_secret(args) -> None:
    if args.action == "list":
        for name, desc in KNOWN_SECRETS.items():
            print(f"{OK if get_secret(name) else ' '} {name:28} {desc}")
        return
    if not args.name:
        sys.exit("secret name required")
    if args.action == "set":
        value = getpass.getpass(f"{args.name}: ").strip()
        if not value:
            sys.exit("empty value, nothing stored")
        set_secret(args.name, value)
        print(f"{OK} stored {args.name} in the login Keychain (service 'meetcap')")
    elif args.action == "delete":
        delete_secret(args.name)
        print(f"{OK} deleted {args.name}")


def cmd_setup_obs(args) -> None:
    from .agent import setup_obs
    from .obs_client import ObsConnectionError, ObsRequestError
    from .recorder import RecorderError

    print("Configuring OBS. It quits and reopens a few times; that's expected.\n"
          "If macOS asks about Screen & System Audio Recording or Microphone, click Allow.\n"
          "If it says OBS must Quit & Reopen, click that too; meetcap waits and continues.\n")
    problems = 0

    def show(step: str) -> None:
        nonlocal problems
        if step.startswith("PROBLEM: "):
            problems += 1
            print(f"{BAD} {step[9:]}", flush=True)
        elif step.startswith("WARNING: "):
            print(f"{WARN} {step[9:]}", flush=True)
        else:
            print(f"{OK} {step}", flush=True)

    try:
        setup_obs(_cfg(), on_step=show)
    except (RecorderError, ObsConnectionError, ObsRequestError) as exc:
        sys.exit(f"\n{BAD} OBS setup stopped: {exc}")
    if problems:
        sys.exit(f"\nFix the {problems} problem(s) above, then run `meetcap setup-obs` again.")
    print(f"\n{OK} OBS is ready. Next: meetcap doctor --deep")


def cmd_obs_log(args) -> None:
    from .recorder import QUARANTINE_PATH, crash_summary, latest_obs_log, obs_crashes

    log_path = latest_obs_log()
    if log_path:
        lines = log_path.read_text(errors="replace").splitlines()
        print(f"== last {args.lines} lines of {log_path} ==")
        print("\n".join(lines[-args.lines:]))
    else:
        print("no OBS log found")
    crashes = obs_crashes(days=args.days, limit=args.crashes)
    for crash in crashes:
        print(f"\n== crash report {crash.name} ==")
        print(crash_summary(crash))
    if not crashes:
        print(f"\nno OBS crash reports in the last {args.days:g} days")
    if QUARANTINE_PATH.exists():
        print(f"\n== sources left out because they crashed OBS ==\n{QUARANTINE_PATH.read_text()}")


def cmd_mics(args) -> None:
    from .detect import MicWatch, match_app

    cfg = _cfg()
    mw = MicWatch(cfg.micwatch_path)
    if not mw.available():
        sys.exit(f"{BAD} micwatch not found at {mw.path}. Run install.sh")
    procs = mw.processes(include_all=args.all)
    for p in procs:
        flag = "MIC" if p.get("input") else "   "
        print(f"{flag} pid={p.get('pid'):<7} {p.get('bundle') or '(no bundle id)'}")
    app = match_app({p["bundle"] for p in procs if p.get("input") and p.get("bundle")},
                    [*cfg.detection.apps, *cfg.detection.browsers])
    print(f"\nmeeting app holding the mic: {app or 'none'}")


def cmd_doctor(args) -> None:
    cfg = _cfg()
    ensure_dirs(cfg)
    bad = 0

    def line(ok: bool | None, msg: str) -> None:
        nonlocal bad
        bad += ok is False
        print(f"{OK if ok else (WARN if ok is None else BAD)} {msg}")

    line(CONFIG_PATH.exists(), f"config {CONFIG_PATH}" + ("" if CONFIG_PATH.exists() else " (run: meetcap init)"))
    for p in problems(cfg):
        line(False, p)

    ffmpeg = shutil.which("ffmpeg") or next((str(x) for x in (Path("/opt/homebrew/bin/ffmpeg"),) if x.exists()), None)
    line(bool(ffmpeg), f"ffmpeg {ffmpeg or 'missing (brew install ffmpeg)'}")
    if ffmpeg:
        enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        line("libopus" in enc, "ffmpeg has libopus")

    from .detect import MicWatch
    mw = MicWatch(cfg.micwatch_path)
    if mw.available():
        try:
            users = sorted({p.get("bundle") or "?" for p in mw.processes()})
            line(True, f"micwatch works; mic in use by: {', '.join(users) or 'nobody'}")
        except Exception as exc:  # noqa: BLE001
            line(False, f"micwatch failed: {exc}")
    else:
        line(False, f"micwatch missing at {mw.path} (run install.sh)")

    from .obs_client import ObsRequestError
    from .recorder import ObsRecorder, obs_running
    obs = cfg.recording.obs
    line(Path(obs.app_path).exists(), f"OBS installed at {obs.app_path}")
    line(bool(get_secret("obs-password")), "OBS websocket password in Keychain" +
         ("" if get_secret("obs-password") else " (run: meetcap setup-obs)"))
    if obs_running():
        rec = ObsRecorder(cfg, password=lambda: get_secret("obs-password"))
        try:
            v = rec.client.call("GetVersion")
            line(True, f"OBS {v.get('obsVersion')} reachable on :{obs.port}")
            scenes = [s["sceneName"] for s in rec.client.call("GetSceneList").get("scenes", [])]
            line(obs.scene in scenes, f"scene '{obs.scene}' present")
            fmt = rec.client.call("GetProfileParameter", {"parameterCategory": "SimpleOutput",
                                                          "parameterName": "RecFormat2"}).get("parameterValue")
            line(fmt in ("hybrid_mp4", "hybrid_mov", "fragmented_mp4", "fragmented_mov"),
                 f"recording format {fmt} (crash-safe formats: hybrid_*/fragmented_*)")
            rd = rec.client.call("GetRecordDirectory").get("recordDirectory")
            line(Path(rd or "").resolve() == cfg.output_dir.resolve(), f"record directory {rd}")
            fn = rec.client.call("GetProfileParameter", {"parameterCategory": "Output",
                                                         "parameterName": "FilenameFormatting"}).get("parameterValue")
            line(str(fn or "").startswith("meetcap_"), f"file name pattern {fn} (needs meetcap_ prefix for recovery)")
            if cfg.split_speakers:
                tracks = rec.client.call("GetProfileParameter", {"parameterCategory": "SimpleOutput",
                                                                 "parameterName": "RecTracks"}).get("parameterValue")
                line(str(tracks) == "7" or None, "separate You / Others audio tracks" +
                     ("" if str(tracks) == "7" else " not set up yet; run: meetcap setup-obs"))
        except (ObsRequestError, ConnectionError) as exc:
            line(False, f"OBS websocket: {exc}")
    else:
        line(None, "OBS not running (meetcap starts it when needed)")

    from . import health, providers
    for role, text in providers.describe(cfg).items():
        print(f"  {role}: {text}")
    for secret in health.secrets_needed(cfg):
        have = bool(get_secret(secret))
        fix = f"missing: run meetcap setup (or meetcap secret set {secret})"
        line(have, f"Keychain '{secret}' " + ("set" if have else fix))
    if cfg.storage.backend == "azure" and cfg.azure.storage.auth == "sas" and get_secret("storage-sas-url"):
        from datetime import datetime, timezone

        from .storage import sas_expiry
        exp = sas_expiry(get_secret("storage-sas-url") or "")
        if exp:
            days = (exp - datetime.now(timezone.utc)).days
            line(days > 14 or (None if days > 0 else False),
                 f"storage SAS URL expires {exp:%d %b %Y} ({days} days)" +
                 ("" if days > 14 else "; get a new one and paste it in: meetcap setup"))
    if cfg.transcription.engine == "local":
        line(*health.transcription(cfg, get_secret))

    if args.deep:
        _obs_capture_check(cfg, line)
    if args.deep and not problems(cfg):
        line(*health.storage(cfg, get_secret))
        if cfg.transcription.engine != "local":
            line(*health.transcription(cfg, get_secret))
        line(*health.summary(cfg, get_secret))
    elif not args.deep:
        print("  (add --deep to test storage, transcription and notes with real calls)")
    print(f"\n{'All good.' if not bad else f'{bad} problem(s).'}")
    sys.exit(1 if bad else 0)


def _obs_capture_check(cfg: Config, line) -> None:
    """Open OBS if needed, repair the MeetCap scene, and check it sees the screen and hears the mic."""
    from .recorder import ObsRecorder, RecorderError, obs_running

    if not get_secret("obs-password"):
        return
    was_running = obs_running()
    rec = ObsRecorder(cfg, password=lambda: get_secret("obs-password"))
    try:
        rec.ensure_ready()
        import time
        time.sleep(2)
        found = rec.check_capture(4)
        line(not found, "OBS sees your screen and hears your microphone" if not found else found[0])
        for extra in found[1:]:
            line(False, extra)
    except (RecorderError, ConnectionError) as exc:
        line(False, f"OBS capture check: {exc}")
    finally:
        if not was_running and not cfg.recording.keep_obs_running:
            try:
                rec.quit_obs()
            except RecorderError:
                pass


def cmd_run(args) -> None:
    import signal
    import threading

    from .agent import MeetCapAgent

    agent = MeetCapAgent(_cfg())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    agent.start()
    print("meetcap running headless. Ctrl+C to stop.")
    stop.wait()
    agent.shutdown()


def cmd_menubar(args) -> None:
    from .menubar import run_menubar

    run_menubar(_cfg())


def cmd_process(args) -> None:
    from .agent import MeetCapAgent

    cfg = _cfg()
    ensure_dirs(cfg)
    agent = MeetCapAgent(cfg)
    job = agent.process_file(Path(args.file), app=args.app)
    print(f"{OK} queued job {job.id} for {job.source_path}")
    if args.now:
        pipeline = agent.pipeline()
        if not agent.store.claim(job):
            sys.exit(f"{WARN} the running agent is already processing {job.id}; watch it with: meetcap jobs")
        try:
            job = pipeline.run(job)
        finally:
            agent.store.release(job)
        print(f"{OK if job.status == Status.DONE else BAD} {job.status.value} at {job.stage.value} "
              f"{job.last_error or ''}\n  blob folder: {job.blob_prefix}")


def cmd_jobs(args) -> None:
    from .jobs import JobStore

    store = JobStore(DB_PATH)
    status = Status(args.status) if args.status else None
    for j in store.list(status, args.limit):
        when = datetime.fromtimestamp(j.started_at).strftime("%Y-%m-%d %H:%M")
        title = j.data.get("title") or Path(j.source_path).name
        err = f"  <- {j.last_error[:80]}" if j.last_error and j.status != Status.DONE else ""
        print(f"{j.id}  {when}  {j.status.value:8} {j.stage.value:18} {title}{err}")
    print(json.dumps(store.counts()))


def cmd_retry(args) -> None:
    from .jobs import JobStore

    store = JobStore(DB_PATH)
    if args.id:
        job = store.get(args.id)
        if not job:
            sys.exit(f"no unique job matches '{args.id}'")
        if job.status != Status.FAILED:
            sys.exit(f"{WARN} {job.id} is {job.status.value}; only failed jobs can be retried")
        store.retry(job)
        print(f"{OK} re-queued {job.id} at stage {job.stage.value}")
    else:
        failed = store.list(Status.FAILED, 1000)
        for j in failed:
            store.retry(j)
        print(f"{OK} re-queued {len(failed)} failed job(s)")


def cmd_show(args) -> None:
    from .jobs import JobStore

    job = JobStore(DB_PATH).get(args.id)
    if not job:
        sys.exit(f"no unique job matches '{args.id}'")
    print(json.dumps({**job.__dict__, "stage": job.stage.value, "status": job.status.value}, indent=2, default=str))


# -- parser ------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meetcap", description="Auto-record meetings, push to Azure, get notes.")
    p.add_argument("--version", action="version", version=f"meetcap {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write the config template")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("setup", help="open the setup screen in your browser (storage, transcripts, notes, OBS, pet)")
    s.add_argument("--no-open", action="store_true", help="print the address instead of opening the browser")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("azure-setup", help="guided setup: create Azure storage + speech, pick Azure OpenAI, save config")
    s.add_argument("--no-verify", action="store_true", help="skip the checks at the end")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--existing", action="store_true", help="paste keys you already have (creates nothing)")
    g.add_argument("--create", action="store_true", help="create new resources with az CLI")
    s.set_defaults(fn=cmd_azure_setup)

    sub.add_parser("status", help="is it recording? queue and last error").set_defaults(fn=cmd_status)
    sub.add_parser("start", help="start recording now (like the menu bar button)").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the current recording").set_defaults(fn=cmd_stop)
    s = sub.add_parser("notes", help="open the meeting notes library in your browser")
    s.add_argument("--serve", action="store_true", help="run the library in this window (if the agent isn't)")
    s.add_argument("--local", action="store_true", help="open meetcap's local library instead of the Azure reader")
    s.set_defaults(fn=cmd_notes)

    s = sub.add_parser("reader", help="make the notes reader file that reads straight from Azure (works without meetcap)")
    s.add_argument("--days", type=int, default=365, help="how long the reader link lasts (default 365)")
    s.add_argument("--print", action="store_true", help="also print the reader link, to paste on another Mac")
    s.add_argument("--revoke", action="store_true", help="stop every reader link at once")
    s.add_argument("--yes", action="store_true", help="don't ask before adding the browser read rule")
    s.add_argument("--no-open", action="store_true", help="don't open the file afterwards")
    s.set_defaults(fn=cmd_reader)

    s = sub.add_parser("pet", help="show / hide the puppy on your screen, or test it on its own")
    s.add_argument("state", choices=["on", "off", "test", "list", *PET_NAMES], metavar="on|off|test|list|<pet>")
    s.set_defaults(fn=cmd_pet)

    s = sub.add_parser("auto", help="turn auto-record on or off")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_auto)

    sub.add_parser("config", help="open config.yaml in TextEdit").set_defaults(fn=cmd_config)
    sub.add_parser("restart", help="restart the menu bar agent (after config changes)").set_defaults(fn=cmd_restart)

    s = sub.add_parser("secret", help="manage Keychain secrets")
    s.add_argument("action", choices=["set", "delete", "list"])
    s.add_argument("name", nargs="?", choices=list(KNOWN_SECRETS))
    s.set_defaults(fn=cmd_secret)

    sub.add_parser("setup-obs", help="configure OBS profile, scene, sources and websocket").set_defaults(fn=cmd_setup_obs)

    s = sub.add_parser("doctor", help="check everything end to end")
    s.add_argument("--deep", action="store_true", help="make real Azure calls")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("obs-log", help="show OBS's latest log and crash report (for troubleshooting)")
    s.add_argument("--lines", type=int, default=30, help="how much of the log to show")
    s.add_argument("--crashes", type=int, default=4, help="how many recent crash reports to summarise")
    s.add_argument("--days", type=float, default=7, help="look back this many days for crash reports")
    s.set_defaults(fn=cmd_obs_log)

    s = sub.add_parser("mics", help="show which processes hold the microphone")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_mics)

    sub.add_parser("run", help="run the agent without UI").set_defaults(fn=cmd_run)
    sub.add_parser("menubar", help="run the agent with the menu bar UI").set_defaults(fn=cmd_menubar)

    s = sub.add_parser("process", help="queue an existing recording (never deleted)")
    s.add_argument("file")
    s.add_argument("--app", default="import")
    s.add_argument("--now", action="store_true", help="process in this terminal instead of the agent")
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("jobs", help="list jobs")
    s.add_argument("--status", choices=[x.value for x in Status])
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("retry", help="re-queue a failed job, or all failed jobs")
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_retry)

    s = sub.add_parser("show", help="print one job as JSON")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, to_file=args.cmd in ("run", "menubar", "process"))
    args.fn(args)


if __name__ == "__main__":
    main()

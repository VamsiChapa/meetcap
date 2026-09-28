"""Menu bar UI (rumps). A thin consumer of MeetCapAgent: no logic lives here.

The icon is an animated puppy (frames drawn by tools/make_icons.py):
  idle    - sits and blinks now and then      recording - headphones on, tilts its head, red dot
  working - writing notes on a notepad        auto off  - asleep
  error   - orange "!" badge (details in the menu)
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from importlib import resources
from pathlib import Path

from .agent import AgentStatus, MeetCapAgent
from .config import CONFIG_PATH, LOG_DIR, Config
from .pipeline import app_short

log = logging.getLogger(__name__)

DOT_IDLE, DOT_REC = "◌", "●"  # ui.icon = dot: tiny text, for very crowded menu bars
TICK = 0.4                    # animation step in seconds
PET_TICK = 0.1                # desktop pet: 10 frames per second is smooth and costs almost nothing

SEQUENCES = {
    "idle": ["idle_0"] * 12 + ["idle_1"],               # a blink every ~5 s
    "rec": ["rec_0", "rec_1", "rec_2", "rec_3"],        # listening head tilt
    "work": ["work_0", "work_0", "work_1", "work_1"],   # scribbling
    "sleep": ["sleep_0"] * 3 + ["sleep_1"] * 3,         # floating z's
    "error": ["error_0"],
}


def _elapsed(since: float | None) -> str:
    s = int(time.time() - (since or time.time()))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def mood(s: AgentStatus) -> str:
    if s.recording:
        return "rec"
    if not s.auto:
        return "sleep"
    if s.last_error:
        return "error"
    if s.processing:
        return "work"
    return "idle"


def frame_for(s: AgentStatus, tick: int, animate: bool = True) -> str:
    seq = SEQUENCES[mood(s)]
    return seq[tick % len(seq)] if animate else seq[0]


def icon_dir() -> Path:
    return Path(str(resources.files("meetcap").joinpath("icons")))


def run_menubar(cfg: Config) -> None:
    import rumps

    try:  # no Dock icon: behave like a menu bar utility
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    except Exception:  # noqa: BLE001
        pass

    agent = MeetCapAgent(cfg)
    icons = icon_dir()
    use_puppy = cfg.ui.icon == "puppy" and (icons / "idle_0.png").exists()

    class App(rumps.App):
        def __init__(self) -> None:
            if use_puppy:
                super().__init__("MeetCap", title=None, icon=str(icons / "idle_0.png"), template=False,
                                 quit_button=None)
            else:
                super().__init__("MeetCap", title=DOT_IDLE, quit_button=None)
            self._tick = 0
            self._frame = "idle_0"
            self._status = agent.status()
            self.status_item = rumps.MenuItem("Starting…")
            self.error_item = rumps.MenuItem("No errors")
            self.record_item = rumps.MenuItem("Start recording now", callback=self.toggle_record)
            self.auto_item = rumps.MenuItem("Auto-record meetings", callback=self.toggle_auto)
            self.auto_item.state = int(agent.auto)
            self.queue_item = rumps.MenuItem("Queue: –")
            self.pet_item = rumps.MenuItem("Show puppy on screen", callback=self.toggle_pet)
            self.pet_item.state = int(cfg.ui.pet)
            self.pet = None
            self.pet_timer = rumps.Timer(self.pet_tick, PET_TICK)
            self.menu = [
                self.status_item, self.error_item, None,
                self.record_item, self.auto_item, self.pet_item, None,
                self.queue_item,
                rumps.MenuItem("Retry failed jobs", callback=self.retry),
                rumps.MenuItem("Open meeting notes", callback=lambda _: self._open(self.notes_target())),
                rumps.MenuItem("Settings…", callback=lambda _: subprocess.Popen(
                    [sys.executable, "-m", "meetcap.cli", "setup"])),
                rumps.MenuItem("Open recordings folder", callback=lambda _: self._open(cfg.output_dir)),
                rumps.MenuItem("Open notes folder", callback=lambda _: self._open(cfg.notes_dir or cfg.output_dir)),
                rumps.MenuItem("Open log", callback=lambda _: self._open(LOG_DIR / "meetcap.log")),
                None,
                rumps.MenuItem("Quit MeetCap", callback=self.quit),
            ]
            self.timer = rumps.Timer(self.refresh, 1)
            self.timer.start()
            if use_puppy:
                self.anim = rumps.Timer(self.animate, TICK)
                self.anim.start()
            if cfg.ui.pet:
                self.pet_timer.start()

        @staticmethod
        def notes_target():
            return agent.reader_target()

        @staticmethod
        def _open(path) -> None:
            subprocess.Popen(["open", str(path)])

        def animate(self, _=None) -> None:
            self._tick += 1
            frame = frame_for(self._status, self._tick, cfg.ui.animate)
            if frame != self._frame:
                self._frame = frame
                self.icon = str(icons / f"{frame}.png")

        # -- desktop pet --------------------------------------------------------------
        def set_pet(self, on: bool, save: bool = True) -> None:
            self.pet_item.state = int(on)
            if on:
                self.pet_timer.start()
            else:
                self.pet_timer.stop()
                if self.pet:
                    self.pet.hide()
            if save:
                try:
                    from .azure_setup import update_config
                    update_config(CONFIG_PATH, {"ui.pet": on})
                except Exception:  # noqa: BLE001 - the toggle still works for this session
                    pass

        def toggle_pet(self, sender) -> None:
            self.set_pet(not bool(sender.state))

        def pet_tick(self, _=None) -> None:
            try:
                if self.pet is None:
                    from .pet import DesktopPet
                    from .pet import sprite_dir
                    self.pet = DesktopPet(size=cfg.ui.pet_size, draggable=cfg.ui.pet_draggable,
                                          folder=sprite_dir(cfg.ui.pet_species),
                                          on_double_click=lambda: self.toggle_record(None))
                    log.info("desktop puppy shown (size %s, draggable %s)", cfg.ui.pet_size, cfg.ui.pet_draggable)
                s = self._status
                if cfg.ui.pet_hide_in_meetings and s.recording:
                    if self.pet.visible:
                        self.pet.hide()
                    return
                if not self.pet.visible:
                    self.pet.show()
                self.pet.tick(mood(s), PET_TICK)
            except Exception as exc:  # noqa: BLE001 - a broken pet must never take the recorder down
                log.exception("desktop puppy failed")
                self.pet_timer.stop()
                self.pet_item.state = 0
                agent.notify("Couldn't show the puppy", f"{type(exc).__name__}: {exc}. Try: meetcap pet test")

        def refresh(self, _=None) -> None:
            s = self._status = agent.status()
            if agent.pet_request is not None:   # `meetcap pet on|off` from Terminal
                self.set_pet(agent.pet_request, save=False)
                agent.pet_request = None
            if s.recording:
                self.title = f" {_elapsed(s.since)}" if use_puppy else f"{DOT_REC} {_elapsed(s.since)}"
                self.status_item.title = f"Recording {app_short(s.app or 'manual')} ({'manual' if s.manual else 'auto'})"
                self.record_item.title = "Stop recording"
            else:
                self.title = None if use_puppy else (DOT_IDLE if s.auto else "◌ off")
                if not s.auto:
                    self.status_item.title = "Asleep: auto-record is off"
                elif s.processing:
                    self.status_item.title = "Writing notes for your last meeting…"
                elif s.detection_available:
                    self.status_item.title = "Waiting for a meeting"
                else:
                    self.status_item.title = "Idle: meeting detection unavailable"
                self.record_item.title = "Start recording now"
            c = s.counts
            self.queue_item.title = (f"Queue: {c.get('pending', 0)} pending · {c.get('done', 0)} done · "
                                     f"{c.get('failed', 0)} failed")
            self.error_item.title = f"⚠ {s.last_error[:70]}" if s.last_error else "No errors"

        def toggle_record(self, _) -> None:
            # OBS launch/stop can take tens of seconds; never block the menu bar's main thread.
            if agent.status().recording:
                self.record_item.title = "Stopping…"
                threading.Thread(target=agent.stop_recording, daemon=True).start()
            else:
                self.record_item.title = "Starting…"
                threading.Thread(target=agent.start_recording, args=("manual", True), daemon=True).start()

        def toggle_auto(self, sender) -> None:
            agent.set_auto(not agent.auto)
            sender.state = int(agent.auto)
            self.refresh()

        def retry(self, _) -> None:
            n = agent.retry_failed()
            agent.notify("MeetCap", f"Re-queued {n} job(s)")

        def quit(self, _) -> None:
            agent.shutdown()
            rumps.quit_application()

    agent.start()
    App().run()

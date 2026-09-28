"""Desktop pet: the meetcap puppy lives on your screen and acts out what meetcap is doing.

  waiting   - wanders (walks, sometimes dashes), sits and wags, now and then naps
  recording - sits with headphones on and a red dot, bobbing its head
  notes     - sits scribbling on a notepad
  auto off  - asleep
  problem   - sits with an orange "!"

Double-click it to start recording (double-click again to stop). Drag it anywhere: it dangles while
carried, then stays at the height you drop it and walks around from there. A single click makes it wag.
The spot is remembered across restarts. It lives in a non-activating panel, so the first click goes
straight to the puppy and it never takes keyboard focus away from Teams or whatever you're typing in.

The window is transparent and always on top, shows on every Space, and asks macOS to keep it out of
screen sharing and recordings. Only the puppy itself catches the mouse; clicks beside it reach your apps.
PetBrain holds all behaviour and is plain Python (tested); DesktopPet is the thin AppKit layer.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config import STATE_DIR

log = logging.getLogger(__name__)

POSE_FOR_MOOD = {"rec": "listen", "work": "write", "sleep": "sleep", "error": "alert"}
FRAMES = {"walk": 8, "sit": 2, "listen": 2, "write": 2, "sleep": 2, "alert": 1, "held": 2}
FRAME_SECONDS = {"walk": 0.09, "sit": 0.5, "listen": 0.45, "write": 0.4, "sleep": 0.8, "alert": 1.0, "held": 0.35}
RUN_SPEEDUP = 2.3  # a run is the same stride, faster
POSITION_PATH = STATE_DIR / "pet.json"


SPECIES = {  # name -> label. The puppy's sprites sit directly in pet/, every other species in pet/<name>/
    "puppy": "Puppy", "kitten": "Kitten", "lion_cub": "Lion cub", "fox": "Fox kit", "panda": "Panda",
    "bunny": "Bunny", "bear_cub": "Bear cub", "tiger_cub": "Tiger cub", "raccoon": "Raccoon", "koala": "Koala",
}


def sprite_dir(species: str = "puppy") -> Path:
    base = Path(str(resources.files("meetcap").joinpath("pet")))
    if species != "puppy" and (base / species).is_dir():
        return base / species
    return base


def available_species() -> list[str]:
    return [s for s in SPECIES if s == "puppy" or sprite_dir(s) != sprite_dir("puppy")]


def sprite_resource(species: str, name: str) -> str:
    """Package-relative path of one sprite, e.g. pet/kitten/sit_r_0.png (the puppy if the species is missing)."""
    return "pet/" + (f"{species}/" if sprite_dir(species) != sprite_dir("puppy") else "") + f"{name}.png"


@dataclass(frozen=True)
class PetFrame:
    sprite: str   # e.g. "walk_r_2"
    x: float      # left edge in screen points
    facing: str   # "r" or "l"


class PetBrain:
    SPEED = 70.0          # points per second
    MIN_TRIP = 80.0       # don't bother walking less than this
    RUN_CHANCE = 0.35     # share of trips it runs instead of walks

    def __init__(self, rng: random.Random | None = None, x: float = 200.0):
        self.rng = rng or random.Random()
        self.x, self.facing = x, "r"
        self.state, self.timer, self.target = "sit", 2.0, x
        self.t = 0.0
        self.running = False
        self.happy = 0.0      # seconds of extra wagging after a click

    def _frame(self, pose: str) -> PetFrame:
        secs = FRAME_SECONDS[pose] / (RUN_SPEEDUP if pose == "walk" and self.running else 1.0)
        if pose == "sit" and self.happy > 0:
            secs = 0.18       # excited wagging
        idx = int(self.t / secs) % FRAMES[pose]
        return PetFrame(f"{pose}_{self.facing}_{idx}", self.x, self.facing)

    # -- mouse -----------------------------------------------------------------------
    def hold(self) -> None:
        self.state = "held"

    def drop(self, x: float) -> None:
        self.x = x
        self.state, self.timer = "sit", self.rng.uniform(2.5, 5.0)

    def poke(self) -> None:
        """A click without dragging: stop, look at you and wag."""
        self.state, self.timer, self.happy = "sit", 3.0, 2.0

    # -- behaviour -------------------------------------------------------------------
    def step(self, mood: str, dt: float, left: float, right: float) -> PetFrame:
        self.t += dt
        self.happy = max(0.0, self.happy - dt)
        if self.state == "held":
            return self._frame("held")
        self.x = min(max(self.x, left), max(left, right))
        if mood in POSE_FOR_MOOD:            # meetcap is busy: stay put and show it
            self.state, self.timer = "sit", 1.5
            return self._frame(POSE_FOR_MOOD[mood])

        if self.state == "walk":
            stride = self.SPEED * (RUN_SPEEDUP if self.running else 1.0) * dt
            if abs(self.target - self.x) <= stride:
                self.x = self.target
                self.state, self.timer = "sit", self.rng.uniform(3.0, 9.0)
                return self._frame("sit")
            self.x += stride if self.target > self.x else -stride
            return self._frame("walk")

        self.timer -= dt
        if self.timer <= 0:
            if self.state == "nap" or self.rng.random() < 0.8:
                self._start_walk(left, right)
                return self._frame("walk")
            self.state, self.timer = "nap", self.rng.uniform(12.0, 25.0)
        return self._frame("sleep" if self.state == "nap" else "sit")

    def _start_walk(self, left: float, right: float) -> None:
        span = max(0.0, right - left)
        target = self.rng.uniform(left, left + span)
        if abs(target - self.x) < self.MIN_TRIP:  # pick the far side instead of a tiny shuffle
            target = left + span if self.x - left < span / 2 else left
        self.target = target
        self.facing = "r" if target > self.x else "l"
        self.state = "walk"
        self.running = self.rng.random() < self.RUN_CHANCE


def load_position(path: Path | None = None) -> dict | None:
    try:
        data = json.loads((path or POSITION_PATH).read_text())
        return {"x": float(data["x"]), "y": float(data["y"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_position(x: float, y: float, path: Path | None = None) -> None:
    try:
        p = path or POSITION_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"x": round(x, 1), "y": round(y, 1)}))
    except OSError as exc:
        log.debug("could not remember pet position: %s", exc)


def clamp_drop(x: float, y: float, size: float, frame: tuple[float, float, float, float]) -> tuple[float, float]:
    """Keep a dropped puppy fully on the visible screen area (ox, oy, width, height)."""
    ox, oy, w, h = frame
    return min(max(x, ox), ox + w - size), min(max(y, oy), oy + h - size)


# -- AppKit layer ---------------------------------------------------------------------------------------
_VIEW_CLASS = None
_CURRENT: "DesktopPet | None" = None   # one pet per process; fallback if the view can't hold a Python attribute


def _owner(view):
    return getattr(view, "pet_owner", None) or _CURRENT


def _view_class():
    """NSImageView subclass that forwards mouse drags to its DesktopPet (defined once per process)."""
    global _VIEW_CLASS
    if _VIEW_CLASS is None:
        from AppKit import NSImageView

        class MeetcapPetView(NSImageView):
            def acceptsFirstMouse_(self, event):   # react on the first click, without activating meetcap
                return True

            def mouseDown_(self, event):
                owner = _owner(self)
                if owner is None:
                    return
                if event is not None and event.clickCount() >= 2:
                    owner.double_click()
                else:
                    owner.begin_drag()

            def mouseDragged_(self, event):
                owner = _owner(self)
                if owner is not None:
                    owner.drag()

            def mouseUp_(self, event):
                owner = _owner(self)
                if owner is not None:
                    owner.end_drag()

        _VIEW_CLASS = MeetcapPetView
    return _VIEW_CLASS


def _screen_frame(screen=None) -> tuple[float, float, float, float]:
    from AppKit import NSScreen

    s = screen or NSScreen.mainScreen() or NSScreen.screens()[0]
    vf = s.visibleFrame()
    return float(vf.origin.x), float(vf.origin.y), float(vf.size.width), float(vf.size.height)


class DesktopPet:
    """Transparent always-on-top window that shows PetBrain's frames. Main thread only."""

    def __init__(self, size: int = 64, brain: PetBrain | None = None, folder: Path | None = None,
                 draggable: bool = True, on_double_click=None):
        from AppKit import NSColor, NSImage, NSMakeRect, NSPanel

        self.size = size
        ox, oy, w, h = _screen_frame()
        saved = load_position()
        start_x, self.floor_y = (saved["x"], saved["y"]) if saved else (ox + w * 0.7, oy)
        start_x, self.floor_y = clamp_drop(start_x, self.floor_y, size, (ox, oy, w, h))
        self.brain = brain or PetBrain(x=start_x)
        self.on_double_click = on_double_click
        # borderless + non-activating panel: clicks reach the puppy at once and never steal focus
        self.win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(self.brain.x, self.floor_y, size, size), 0 | (1 << 7), 2, False)
        self.win.setFloatingPanel_(True)
        self.win.setBecomesKeyOnlyIfNeeded_(True)
        self.win.setHidesOnDeactivate_(False)            # panels hide when their app deactivates by default
        self.win.setOpaque_(False)
        self.win.setBackgroundColor_(NSColor.clearColor())
        self.win.setHasShadow_(False)
        self.win.setIgnoresMouseEvents_(not draggable)
        self.win.setLevel_(25)                           # NSStatusWindowLevel: above normal windows
        self.win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))
        # canJoinAllSpaces | stationary | ignoresCycle | fullScreenAuxiliary
        self.win.setSharingType_(0)                      # NSWindowSharingNone: keep out of screen shares
        self.win.setReleasedWhenClosed_(False)
        self.view = _view_class().alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
        self.view.setImageScaling_(3)                    # proportionally up or down
        global _CURRENT
        _CURRENT = self
        try:
            self.view.pet_owner = self
        except (AttributeError, TypeError):
            pass
        self.win.setContentView_(self.view)
        self.images = {}
        for p in (folder or sprite_dir()).glob("*.png"):
            img = NSImage.alloc().initWithContentsOfFile_(str(p))
            img.setSize_((size, size))
            self.images[p.stem] = img
        if not self.images:
            raise RuntimeError(f"no pet pictures found in {folder or sprite_dir()}")
        self._sprite = ""
        self._grab: tuple[float, float] | None = None
        self._moved = False
        self.visible = False

    # -- visibility -------------------------------------------------------------------
    def show(self) -> None:
        self.win.orderFrontRegardless()
        self.visible = True

    def hide(self) -> None:
        self.win.orderOut_(None)
        self.visible = False

    # -- dragging -----------------------------------------------------------------------
    def begin_drag(self) -> None:
        from AppKit import NSEvent

        m = NSEvent.mouseLocation()
        o = self.win.frame().origin
        self._grab, self._moved = (m.x - o.x, m.y - o.y), False
        self.brain.hold()
        self._show_sprite(self.brain.step("idle", 0.0, 0, 0).sprite)

    def drag(self) -> None:
        from AppKit import NSEvent

        if self._grab is None:
            return
        m = NSEvent.mouseLocation()
        x, y = m.x - self._grab[0], m.y - self._grab[1]
        self.win.setFrameOrigin_((x, y))
        self.brain.x = x
        self._moved = True

    def double_click(self) -> None:
        """Start or stop recording (the owner decides); the puppy reacts straight away."""
        self._grab = None
        self.brain.poke()
        if self.on_double_click:
            self.on_double_click()

    def end_drag(self) -> None:
        if self._grab is None:
            return
        o = self.win.frame().origin
        self._grab = None
        if not self._moved:
            self.brain.poke()
            return
        x, y = clamp_drop(o.x, o.y, self.size, _screen_frame(self.win.screen()))
        self.floor_y = y
        self.brain.drop(x)
        self.win.setFrameOrigin_((x, y))
        save_position(x, y)

    # -- animation ----------------------------------------------------------------------
    def _show_sprite(self, name: str) -> None:
        if name != self._sprite and name in self.images:
            self.view.setImage_(self.images[name])
            self._sprite = name

    def tick(self, mood: str, dt: float) -> None:
        if self._grab is not None and self._moved:        # being carried: the mouse moves the window
            self._show_sprite(self.brain.step(mood, dt, 0, 0).sprite)
            return
        ox, oy, w, h = _screen_frame(self.win.screen())   # re-read: displays and the Dock can change
        self.floor_y = min(max(self.floor_y, oy), oy + h - self.size)
        frame = self.brain.step(mood, dt, ox, ox + w - self.size)
        self._show_sprite(frame.sprite)
        self.win.setFrameOrigin_((frame.x, self.floor_y))


def demo(seconds: float = 30.0, size: int = 64, species: str = "puppy") -> None:
    """`meetcap pet test`: show the pet on its own for a while, cycling through every mood."""
    from AppKit import NSApplication, NSTimer
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)                         # accessory: no Dock icon
    pet = DesktopPet(size=size, folder=sprite_dir(species), on_double_click=lambda: print("Double-click works: in the menu bar app this "
                                                                "starts / stops recording."))
    pet.show()
    moods = ["idle", "idle", "rec", "work", "sleep", "error"]
    state = {"t": 0.0}
    print(f"Your {SPECIES.get(species, species).lower()} should now be on your screen (bottom, or where you left it).")
    print("Try dragging it and double-clicking it. It shows each mood for 5 seconds: waiting, recording, notes, "
          "asleep, problem.")

    def tick(_timer) -> None:
        state["t"] += 0.1
        mood = moods[int(state["t"] / 5) % len(moods)]
        pet.tick(mood, 0.1)
        if state["t"] >= seconds:
            pet.hide()
            AppHelper.stopEventLoop()

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.1, True, tick)
    AppHelper.runEventLoop()
    print("Done. If you saw it, the menu bar app can show it too: meetcap pet on")


if __name__ == "__main__":
    import sys
    demo(float(sys.argv[1]) if len(sys.argv) > 1 else 30.0, species=sys.argv[2] if len(sys.argv) > 2 else "puppy")

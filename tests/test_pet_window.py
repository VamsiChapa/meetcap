"""Exercise DesktopPet end to end against a stand-in for AppKit (the real one only exists on macOS)."""
import sys
import types

import pytest

from meetcap import pet as pet_mod


class Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Rect:
    def __init__(self, x, y, w, h):
        self.origin, self.size = Pt(x, y), types.SimpleNamespace(width=w, height=h)


class FakeScreen:
    def visibleFrame(self):
        return Rect(0, 25, 1512, 920)        # 14" MacBook Pro above the Dock


class FakeWindow:
    instances = []

    @classmethod
    def alloc(cls):
        return cls()

    def initWithContentRect_styleMask_backing_defer_(self, rect, style, backing, defer):
        self.origin = Pt(rect.origin.x, rect.origin.y)
        self.calls = {"style": style}
        FakeWindow.instances.append(self)
        return self

    def __getattr__(self, name):             # setOpaque_, setLevel_, ... just record the value
        if name.startswith("set"):
            return lambda *a: self.calls.__setitem__(name, a)
        raise AttributeError(name)

    def setFrameOrigin_(self, p):
        self.origin = Pt(*p)

    def frame(self):
        return Rect(self.origin.x, self.origin.y, 64, 64)

    def screen(self):
        return FakeScreen()

    def orderFrontRegardless(self):
        self.calls["front"] = True

    def orderOut_(self, _):
        self.calls["front"] = False


class FakeImage:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithContentsOfFile_(self, path):
        self.path = path
        return self

    def setSize_(self, s):
        self.size = s


class FakeImageView:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithFrame_(self, rect):
        self.image = None
        return self

    def setImageScaling_(self, v):
        self.scaling = v

    def setImage_(self, img):
        self.image = img


MOUSE = Pt(0, 0)


@pytest.fixture
def appkit(monkeypatch, tmp_path):
    fake = types.ModuleType("AppKit")
    fake.NSPanel, fake.NSImage, fake.NSImageView = FakeWindow, FakeImage, FakeImageView
    fake.NSColor = types.SimpleNamespace(clearColor=lambda: None)
    fake.NSMakeRect = Rect
    fake.NSScreen = types.SimpleNamespace(mainScreen=lambda: FakeScreen(), screens=lambda: [FakeScreen()])
    fake.NSEvent = types.SimpleNamespace(mouseLocation=lambda: MOUSE)
    monkeypatch.setitem(sys.modules, "AppKit", fake)
    monkeypatch.setattr(pet_mod, "_VIEW_CLASS", None)
    monkeypatch.setattr(pet_mod, "POSITION_PATH", tmp_path / "pet.json")
    return fake


def test_window_is_transparent_on_top_and_out_of_screen_shares(appkit):
    p = pet_mod.DesktopPet(size=64)
    w = p.win
    assert w.calls["setOpaque_"] == (False,) and w.calls["setHasShadow_"] == (False,)
    assert w.calls["setLevel_"] == (25,) and w.calls["setSharingType_"] == (0,)
    assert w.calls["setIgnoresMouseEvents_"] == (False,)          # draggable by default
    assert len(p.images) == 2 * sum(pet_mod.FRAMES.values()) and p.view.scaling == 3
    p.show()
    assert w.calls["front"] is True
    for _ in range(30):
        p.tick("idle", 0.1)
    assert p.view.image is not None and w.origin.y == 25          # standing on the bottom edge
    p.tick("rec", 0.1)
    assert p._sprite.startswith("listen_")


def test_click_through_mode(appkit):
    p = pet_mod.DesktopPet(draggable=False)
    assert p.win.calls["setIgnoresMouseEvents_"] == (True,)


def test_drag_moves_it_and_it_stays_there(appkit):
    p = pet_mod.DesktopPet(size=64)
    p.show()
    start = (p.win.origin.x, p.win.origin.y)
    MOUSE.x, MOUSE.y = start[0] + 20, start[1] + 30                # grab 20/30 pt inside the puppy
    p.view.mouseDown_(None)
    assert p._sprite.startswith("held_")
    MOUSE.x, MOUSE.y = 520, 630
    p.view.mouseDragged_(None)
    assert (p.win.origin.x, p.win.origin.y) == (500, 600)          # follows the mouse, keeps the grab offset
    p.tick("idle", 0.1)
    assert (p.win.origin.x, p.win.origin.y) == (500, 600)          # animation doesn't fight the drag
    p.view.mouseUp_(None)
    assert p.floor_y == 600 and pet_mod.load_position() == {"x": 500.0, "y": 600.0}
    for _ in range(20):
        p.tick("idle", 0.1)
    assert p.win.origin.y == 600                                    # keeps walking at the height you chose


def test_drop_off_screen_is_pulled_back(appkit):
    p = pet_mod.DesktopPet(size=64)
    MOUSE.x, MOUSE.y = p.win.origin.x + 10, p.win.origin.y + 10
    p.view.mouseDown_(None)
    MOUSE.x, MOUSE.y = 5000, 5000
    p.view.mouseDragged_(None)
    p.view.mouseUp_(None)
    assert (p.win.origin.x, p.win.origin.y) == (1512 - 64, 25 + 920 - 64)


def test_click_without_drag_wags(appkit):
    p = pet_mod.DesktopPet(size=64)
    p.view.mouseDown_(None)
    p.view.mouseUp_(None)
    assert p.brain.happy > 0 and p.brain.state == "sit"


def test_remembered_spot_is_used_on_start(appkit):
    pet_mod.save_position(300, 450)
    p = pet_mod.DesktopPet(size=64)
    assert (p.win.origin.x, p.win.origin.y) == (300, 450)


class Click:
    def __init__(self, n):
        self.n = n

    def clickCount(self):
        return self.n


def test_panel_never_steals_focus(appkit):
    p = pet_mod.DesktopPet(size=64)
    w = p.win
    assert w.calls["style"] & (1 << 7)                               # non-activating panel
    assert w.calls["setHidesOnDeactivate_"] == (False,)             # stays visible when you use other apps
    assert w.calls["setBecomesKeyOnlyIfNeeded_"] == (True,)


def test_double_click_toggles_recording(appkit):
    toggles = []
    p = pet_mod.DesktopPet(size=64, on_double_click=lambda: toggles.append(1))
    MOUSE.x, MOUSE.y = p.win.origin.x + 5, p.win.origin.y + 5
    p.view.mouseDown_(Click(1))                                      # first click of the pair
    p.view.mouseUp_(Click(1))
    p.view.mouseDown_(Click(2))                                      # second click -> double-click
    p.view.mouseUp_(Click(2))
    assert toggles == [1]
    assert p._grab is None and p.brain.state == "sit"                # no accidental drag

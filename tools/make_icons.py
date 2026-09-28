"""Draws meetcap's menu bar puppy (an original character) and writes the animation frames.

    python tools/make_icons.py            # writes src/meetcap/icons/*.png and tools/icon_preview.png

Frames are drawn at 4x on a 160 px canvas and downsampled to 40 px (a 20 pt icon on Retina).
States: idle (blinks now and then), rec (ears up, head tilts while listening, red dot),
work (holding a notepad), sleep (auto-record off), error (orange "!").
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

S = 160          # drawing canvas
OUT = 40         # final size in pixels
ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "src" / "meetcap" / "icons"

INK = (58, 34, 18, 255)        # outline
FUR = (226, 166, 92, 255)      # tan
EAR = (140, 88, 42, 255)       # darker brown
PATCH = (190, 122, 56, 255)    # eye patch
MUZZLE = (248, 230, 204, 255)
TONGUE = (240, 118, 138, 255)
RED = (255, 59, 48, 255)
ORANGE = (255, 159, 10, 255)
WHITE = (255, 255, 255, 255)
PAPER = (252, 252, 248, 255)


def _ear_floppy(d: ImageDraw.ImageDraw, side: int) -> None:
    pts = [(52, 48), (32, 54), (16, 80), (18, 112), (32, 122), (46, 108), (50, 80)]
    if side > 0:
        pts = [(S - x, y) for x, y in pts]
    d.polygon(pts, fill=EAR, outline=INK, width=6)


def _ear_up(d: ImageDraw.ImageDraw, side: int) -> None:
    pts = [(44, 62), (30, 14), (46, 18), (72, 50)]
    if side > 0:
        pts = [(S - x, y) for x, y in pts]
    d.polygon(pts, fill=EAR, outline=INK, width=6)


def _eyes(d: ImageDraw.ImageDraw, mode: str) -> None:
    for cx in (62, 98):
        if mode == "open":
            d.ellipse((cx - 8, 72, cx + 8, 90), fill=INK)
            d.ellipse((cx - 5, 75, cx, 80), fill=WHITE)
        elif mode == "blink":
            d.line((cx - 9, 82, cx + 9, 82), fill=INK, width=6)
        elif mode == "sleep":
            d.arc((cx - 10, 72, cx + 10, 90), start=20, end=160, fill=INK, width=6)
        elif mode == "down":  # looking at the notepad
            d.ellipse((cx - 7, 78, cx + 7, 92), fill=INK)
            d.ellipse((cx - 4, 81, cx, 85), fill=WHITE)


PHONES = (70, 72, 86, 255)
PHONES_HI = (120, 124, 140, 255)


def _headphones(d: ImageDraw.ImageDraw) -> None:
    d.arc((24, 22, 136, 128), start=195, end=345, fill=INK, width=16)       # band outline
    d.arc((27, 25, 133, 125), start=197, end=343, fill=PHONES, width=9)     # band
    for x0 in (14, 118):
        d.rounded_rectangle((x0, 66, x0 + 28, 108), radius=10, fill=PHONES, outline=INK, width=5)
        d.rounded_rectangle((x0 + 7, 72, x0 + 14, 100), radius=3, fill=PHONES_HI)


def puppy(ears: str = "floppy", eyes: str = "open", tongue: bool = False, tilt: float = 0.0,
          phones: bool = False) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if ears == "up":
        _ear_up(d, -1)
        _ear_up(d, 1)
    d.ellipse((32, 42, 128, 134), fill=FUR, outline=INK, width=6)          # head
    d.ellipse((84, 60, 118, 96), fill=PATCH)                              # patch over one eye
    if ears == "floppy":
        _ear_floppy(d, -1)
        _ear_floppy(d, 1)
    d.ellipse((54, 94, 106, 130), fill=MUZZLE)                            # muzzle
    _eyes(d, eyes)
    d.ellipse((70, 94, 90, 108), fill=INK)                                # nose
    d.ellipse((74, 96, 80, 100), fill=(120, 120, 120, 255))
    d.line((80, 107, 80, 114), fill=INK, width=4)                         # mouth
    d.arc((66, 106, 81, 120), start=20, end=160, fill=INK, width=4)
    d.arc((79, 106, 94, 120), start=20, end=160, fill=INK, width=4)
    if tongue:
        d.ellipse((72, 114, 88, 132), fill=TONGUE, outline=INK, width=3)
        d.line((80, 117, 80, 126), fill=(200, 80, 100, 255), width=2)
    if phones:
        _headphones(d)
    if tilt:
        img = img.rotate(tilt, resample=Image.BICUBIC, center=(80, 90))
    return img


def badge_dot(img: Image.Image, color=RED) -> Image.Image:
    d = ImageDraw.Draw(img)
    d.ellipse((114, 8, 154, 48), fill=color, outline=WHITE, width=6)
    return img


def badge_alert(img: Image.Image) -> Image.Image:
    badge_dot(img, ORANGE)
    d = ImageDraw.Draw(img)
    d.polygon([(127, 14), (141, 14), (137, 33), (131, 33)], fill=WHITE)   # tapered bar
    d.ellipse((129, 36, 139, 45), fill=WHITE)
    return img


def badge_notepad(img: Image.Image, pencil_x: int) -> Image.Image:
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((104, 96, 154, 154), radius=6, fill=PAPER, outline=INK, width=5)
    for y in (112, 124, 136):
        d.line((114, y, 144, y), fill=(120, 140, 170, 255), width=4)
    d.line((pencil_x, 150, pencil_x + 22, 118), fill=ORANGE, width=9)          # pencil
    d.line((pencil_x, 150, pencil_x + 5, 143), fill=INK, width=5)
    return img


def badge_z(img: Image.Image, lift: int) -> Image.Image:
    d = ImageDraw.Draw(img)
    for (x, y, s) in ((112, 40 - lift, 18), (134, 14 - lift, 22)):
        d.line([(x, y), (x + s, y), (x, y + s), (x + s, y + s)], fill=(90, 110, 160, 255), width=6)
    return img


def small(img: Image.Image) -> Image.Image:
    return img.resize((OUT, OUT), Image.LANCZOS)


def frames() -> dict[str, Image.Image]:
    f: dict[str, Image.Image] = {}
    f["idle_0"] = puppy()
    f["idle_1"] = puppy(eyes="blink")
    for i, tilt in enumerate((0, 8, 0, -8)):
        f[f"rec_{i}"] = badge_dot(puppy(phones=True, tongue=(i % 2 == 1), tilt=tilt))
    f["work_0"] = badge_notepad(puppy(eyes="down", tongue=True), 112)
    f["work_1"] = badge_notepad(puppy(eyes="down", tongue=True, tilt=4), 124)
    f["sleep_0"] = badge_z(puppy(eyes="sleep"), 0)
    f["sleep_1"] = badge_z(puppy(eyes="sleep"), 6)
    f["error_0"] = badge_alert(puppy(eyes="open"))
    return f


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    fr = frames()
    for name, img in fr.items():
        small(img).save(ICONS / f"{name}.png")
    # Preview: every frame at 1x (as in the menu bar) and enlarged, on light and dark bars.
    names = list(fr)
    cell = 120
    sheet = Image.new("RGBA", (cell * len(names), cell * 2 + 80), (0, 0, 0, 0))
    for row, bg in enumerate(((236, 236, 236, 255), (40, 40, 44, 255))):
        ImageDraw.Draw(sheet).rectangle((0, row * (cell + 40), sheet.width, row * (cell + 40) + cell + 40), fill=bg)
        for i, n in enumerate(names):
            big = fr[n].resize((96, 96), Image.LANCZOS)
            sheet.alpha_composite(big, (i * cell + 12, row * (cell + 40) + 4))
            sheet.alpha_composite(small(fr[n]).resize((20, 20), Image.LANCZOS), (i * cell + 50, row * (cell + 40) + 110))
    sheet.save(ROOT / "tools" / "icon_preview.png")
    print(f"wrote {len(fr)} frames to {ICONS}")


if __name__ == "__main__":
    main()

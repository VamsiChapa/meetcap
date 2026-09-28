"""3D renderer for meetcap's desktop pets: ten original characters (puppy, kitten, lion cub, fox kit, panda,
bunny, bear cub, tiger cub, raccoon, koala). Sculpted from signed-distance shapes and ray-marched with soft
shadows, ambient occlusion, wrap lighting for a soft-fur look, glossy eyes/nose and a rim light. Every species
shares one body rig and set of poses; a Species entry sets colours, ears, snout, build, tail and markings.

    python tools/render_pet3d.py                     # all species -> src/meetcap/pet/ (+ pets_sheet.png)
    python tools/render_pet3d.py --species fox panda # just these
    python tools/render_pet3d.py --quick             # low-res test render

Poses: walk (8-frame trot), sit (tail wag), listen (3D headphones + red dot), write (notepad),
sleep (curled up, z's), alert (orange "!"), held (picked up). Rendered facing right; left frames are mirrored.
"""
from __future__ import annotations

import argparse
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src" / "meetcap" / "pet"
OUT = 128

# -- materials: fixed ones here, coat colours come from the species ------------------------------------------
#            albedo (linear-ish RGB)      spec   shininess
MATS = {
    "fur":    ((0.78, 0.44, 0.17), 0.06, 14.0),
    "cream":  ((0.96, 0.84, 0.66), 0.05, 14.0),
    "ear":    ((0.40, 0.20, 0.08), 0.06, 14.0),
    "patch":  ((0.55, 0.30, 0.11), 0.06, 14.0),
    "black":  ((0.03, 0.03, 0.04), 0.90, 90.0),
    "pink":   ((0.93, 0.45, 0.52), 0.35, 40.0),
    "collar": ((0.16, 0.42, 0.88), 0.35, 40.0),
    "gold":   ((0.98, 0.76, 0.22), 0.90, 70.0),
    "phones": ((0.20, 0.21, 0.26), 0.55, 50.0),
    "cushion": ((0.10, 0.10, 0.12), 0.20, 20.0),
    "white":  ((1.00, 1.00, 1.00), 0.60, 60.0),
    "nose":   ((0.03, 0.03, 0.04), 0.90, 90.0),
    "inner":  ((0.93, 0.62, 0.62), 0.08, 16.0),
    "tail":   ((0.78, 0.44, 0.17), 0.06, 14.0),
}
MAT_IDS = {k: i for i, k in enumerate(MATS)}
FURRY = ("fur", "cream", "ear", "patch", "inner", "tail")


@dataclass(frozen=True)
class Species:
    label: str
    fur: tuple = (0.78, 0.44, 0.17)
    cream: tuple = (0.96, 0.84, 0.66)
    ear: tuple = (0.40, 0.20, 0.08)
    patch: tuple = (0.55, 0.30, 0.11)
    nose: tuple = (0.03, 0.03, 0.04)
    inner: tuple = (0.93, 0.62, 0.62)
    collar: tuple | None = None           # pets wear a collar; wild ones don't
    ears: str = "floppy"                  # floppy | pointed | round | long | koala
    ear_size: float = 1.0
    snout: tuple = (0.29, -0.08, 0.21, 0.145, 0.18)   # head-local x, y and radii
    snout_mat: str = "cream"
    nose_r: tuple = (0.06, 0.055, 0.075)
    head_r: float = 0.34
    body: tuple = (1.0, 1.0, 1.0)         # torso radii scale
    leg: float = 1.0                      # leg thickness scale
    leg_mat: str = "fur"
    paw_mat: str = "cream"
    tail: str = "dog"                     # dog | thin | bushy | tuft | stub | puff
    tail_tip: str | None = None
    marks: tuple = ()                     # eye_patch panda stripes faint_stripes mask rings
    tongue: bool = True                   # sticks it out now and then


SPECIES = {
    "puppy": Species("Puppy", collar=(0.16, 0.42, 0.88), marks=("eye_patch",)),
    "kitten": Species("Kitten", fur=(0.58, 0.60, 0.64), cream=(0.95, 0.94, 0.93), ear=(0.58, 0.60, 0.64),
                      patch=(0.30, 0.31, 0.35), nose=(0.93, 0.50, 0.56), collar=(0.86, 0.20, 0.22), ears="pointed",
                      snout=(0.25, -0.10, 0.13, 0.10, 0.15), nose_r=(0.045, 0.04, 0.055), head_r=0.33,
                      body=(0.95, 0.92, 0.9), leg=0.85, tail="thin", marks=("faint_stripes",)),
    "lion_cub": Species("Lion cub", fur=(0.82, 0.60, 0.30), cream=(0.97, 0.89, 0.72), ear=(0.82, 0.60, 0.30),
                        patch=(0.36, 0.22, 0.11), nose=(0.55, 0.32, 0.28), inner=(0.40, 0.25, 0.13), ears="round",
                        snout=(0.27, -0.09, 0.16, 0.125, 0.16), head_r=0.35, body=(1.0, 1.02, 1.02), leg=1.05,
                        tail="tuft", tail_tip="patch"),
    "fox": Species(tongue=False, label="Fox kit", fur=(0.88, 0.40, 0.12), cream=(0.98, 0.96, 0.92), ear=(0.88, 0.40, 0.12),
                   patch=(0.16, 0.10, 0.08), inner=(0.20, 0.12, 0.09), ears="pointed", ear_size=1.15,
                   snout=(0.33, -0.09, 0.24, 0.11, 0.12), nose_r=(0.05, 0.045, 0.06), head_r=0.33, body=(1.0, 0.95, 0.92),
                   leg=0.9, paw_mat="patch", tail="bushy", tail_tip="cream"),
    "panda": Species(tongue=False, label="Panda", fur=(0.95, 0.95, 0.93), cream=(0.97, 0.97, 0.95), ear=(0.07, 0.07, 0.08),
                     patch=(0.07, 0.07, 0.08), inner=(0.07, 0.07, 0.08), ears="round", ear_size=1.1,
                     snout=(0.27, -0.10, 0.15, 0.11, 0.15), head_r=0.37, body=(1.05, 1.1, 1.12), leg=1.2,
                     leg_mat="patch", paw_mat="patch", tail="stub", marks=("panda",)),
    "bunny": Species(tongue=False, label="Bunny", fur=(0.80, 0.68, 0.54), cream=(0.98, 0.96, 0.93), ear=(0.80, 0.68, 0.54),
                     patch=(0.55, 0.44, 0.33), nose=(0.93, 0.50, 0.56), ears="long",
                     snout=(0.25, -0.10, 0.12, 0.105, 0.14), nose_r=(0.04, 0.035, 0.05), head_r=0.33,
                     body=(0.85, 1.05, 1.05), leg=0.9, tail="puff", tail_tip="cream"),
    "bear_cub": Species("Bear cub", fur=(0.46, 0.29, 0.16), cream=(0.74, 0.57, 0.40), ear=(0.46, 0.29, 0.16),
                        patch=(0.30, 0.18, 0.10), inner=(0.74, 0.57, 0.40), ears="round",
                        snout=(0.28, -0.09, 0.17, 0.13, 0.15), head_r=0.36, body=(1.05, 1.08, 1.1), leg=1.2,
                        paw_mat="fur", tail="stub"),
    "tiger_cub": Species("Tiger cub", fur=(0.92, 0.50, 0.14), cream=(0.98, 0.96, 0.92), ear=(0.92, 0.50, 0.14),
                         patch=(0.10, 0.07, 0.06), nose=(0.80, 0.45, 0.45), inner=(0.98, 0.96, 0.92), ears="round",
                         ear_size=0.85, snout=(0.27, -0.09, 0.16, 0.125, 0.16), head_r=0.35, leg=1.05, tail="thin",
                         marks=("stripes",)),
    "raccoon": Species(tongue=False, label="Raccoon", fur=(0.56, 0.55, 0.53), cream=(0.93, 0.92, 0.90), ear=(0.56, 0.55, 0.53),
                       patch=(0.12, 0.11, 0.11), inner=(0.93, 0.92, 0.90), ears="pointed", ear_size=0.8,
                       snout=(0.30, -0.09, 0.18, 0.10, 0.12), nose_r=(0.05, 0.045, 0.06), head_r=0.34,
                       paw_mat="patch", tail="bushy", marks=("mask", "rings")),
    "koala": Species(tongue=False, label="Koala", fur=(0.62, 0.63, 0.66), cream=(0.94, 0.94, 0.94), ear=(0.62, 0.63, 0.66),
                     ears="koala", snout=(0.24, -0.08, 0.08, 0.08, 0.10), snout_mat="fur", nose_r=(0.07, 0.10, 0.085),
                     head_r=0.38, body=(0.95, 1.1, 1.1), leg=1.15, paw_mat="fur", tail="stub"),
}


def palette(sp: Species):
    colours = {"fur": sp.fur, "cream": sp.cream, "ear": sp.ear, "patch": sp.patch, "nose": sp.nose, "inner": sp.inner,
               "tail": sp.fur, "collar": sp.collar or MATS["collar"][0]}
    albedo = np.array([colours.get(k, m[0]) for k, m in MATS.items()], dtype=np.float32)
    return albedo, np.array([m[1] for m in MATS.values()], np.float32), np.array([m[2] for m in MATS.values()], np.float32)


# -- SDF primitives (p: (N,3) float32) --------------------------------------------------------------------
def rot(yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0) -> np.ndarray:
    """World->local rotation (apply as p @ R.T after translating)."""
    cy, sy, cp, sp, cr, sr = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch), math.cos(roll), math.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cp, -sp, 0], [sp, cp, 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    return (ry @ rz @ rx).T.astype(np.float32)


def sphere(c, r):
    c = np.asarray(c, np.float32)
    return lambda p: np.linalg.norm(p - c, axis=1) - r


def ellipsoid(c, radii, R=None):
    c, rr = np.asarray(c, np.float32), np.asarray(radii, np.float32)

    def f(p):
        q = p - c
        if R is not None:
            q = q @ R.T
        k0 = np.linalg.norm(q / rr, axis=1)
        k1 = np.linalg.norm(q / (rr * rr), axis=1)
        return k0 * (k0 - 1.0) / np.maximum(k1, 1e-6)
    return f


def capsule(a, b, ra, rb=None):
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    rb = ra if rb is None else rb
    ba = b - a
    bb = float(ba @ ba)

    def f(p):
        pa = p - a
        h = np.clip((pa @ ba) / bb, 0.0, 1.0)
        return np.linalg.norm(pa - h[:, None] * ba, axis=1) - (ra + (rb - ra) * h)
    return f


def torus(c, major, minor, R=None):
    c = np.asarray(c, np.float32)

    def f(p):
        q = p - c
        if R is not None:
            q = q @ R.T
        xz = np.hypot(q[:, 0], q[:, 2]) - major
        return np.hypot(xz, q[:, 1]) - minor
    return f


def arc_band(c, major, minor, R=None, keep_y_above=0.0):
    """Upper half of a torus standing in the x-y plane (a headphone band)."""
    c = np.asarray(c, np.float32)

    def f(p):
        q = p - c
        if R is not None:
            q = q @ R.T
        xy = np.hypot(q[:, 0], q[:, 1]) - major
        d = np.hypot(xy, q[:, 2]) - minor
        return np.where(q[:, 1] > keep_y_above, d, d + np.maximum(keep_y_above - q[:, 1], 0) * 2 + 0.02)
    return f


def cylinder(c, radius, half_h, R=None):
    c = np.asarray(c, np.float32)

    def f(p):
        q = p - c
        if R is not None:
            q = q @ R.T
        dx = np.hypot(q[:, 0], q[:, 1]) - radius
        dy = np.abs(q[:, 2]) - half_h
        return np.minimum(np.maximum(dx, dy), 0) + np.hypot(np.maximum(dx, 0), np.maximum(dy, 0)) - 0.01
    return f


def smin(a, b, k):
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


# -- the puppy -----------------------------------------------------------------------------------------
class Pose:
    """Everything that animates. Units: the dog is ~1.9 long, ~1.35 tall; +x is forward, +y up, +z toward us."""

    # look: head yaw, turn: whole-body yaw. Negative = toward the viewer (the camera sits at +z).
    DEFAULT_LOOK = {"walk": -0.95, "sit": -0.30, "sleep": -0.45, "held": -0.35}
    DEFAULT_TURN = {"walk": 0.0, "sit": -0.95, "sleep": -0.55, "held": -0.95}

    def __init__(self, kind="walk", phase=0.0, wag=0.0, tilt=0.0, eyes="open", tongue=False, phones=False,
                 breath=0.0, look=None, turn=None):
        self.kind, self.phase, self.wag, self.tilt = kind, phase, wag, tilt
        self.eyes, self.tongue, self.phones, self.breath = eyes, tongue, phones, breath
        self.look = self.DEFAULT_LOOK[kind] if look is None else look
        self.turn = self.DEFAULT_TURN[kind] if turn is None else turn


def build(pose: Pose, sp: Species = SPECIES["puppy"]):
    """Returns a list of (sdf, material, smooth_group) parts."""
    parts = []
    add = parts.append
    k = pose.kind
    bx, by, bz = sp.body
    lg = sp.leg

    if k == "walk":
        bob = 0.03 * abs(math.sin(pose.phase * 2))
        body_c, body_r, body_R = (0.0, 0.58 + bob, 0.0), (0.56 * bx, 0.30 * by, 0.29 * bz), rot(pitch=0.04)
        neck_a, head_c = (0.34, 0.68 + bob, 0.0), (0.66, 1.06 + bob, 0.0)
        head_R = rot(yaw=pose.look, pitch=-0.10 + 0.03 * math.sin(pose.phase * 2), roll=0.0)
    elif k == "sleep":
        body_c, body_r, body_R = (-0.05, 0.28 + pose.breath, 0.0), (0.62 * bx, (0.25 + pose.breath) * by, 0.34 * bz), rot()
        neck_a, head_c = (0.35, 0.32, 0.05), (0.66, 0.30, 0.10)
        head_R = rot(yaw=pose.look, pitch=0.18, roll=0.10)
    elif k == "held":  # picked up by the mouse: hanging, legs dangling
        body_c, body_r, body_R = (-0.02, 0.66, 0.0), (0.29 * bz, 0.43 * by, 0.27 * bz), rot(pitch=-0.12)
        neck_a, head_c = (0.05, 0.98, 0.0), (0.18, 1.30, 0.0)
        head_R = rot(yaw=pose.look, pitch=0.10 + pose.tilt * 0.3, roll=pose.tilt)
    else:  # sitting poses
        body_c, body_r, body_R = (-0.05, 0.62, 0.0), (0.34 * bz, 0.46 * by, 0.30 * bz), rot(pitch=-0.35)
        neck_a, head_c = (0.08, 0.95, 0.0), (0.28, 1.30, 0.0)
        head_R = rot(yaw=pose.look, pitch=-0.05 + pose.tilt * 0.4, roll=pose.tilt)

    # torso + belly + neck + head in one smooth group (organic blend)
    add((ellipsoid(body_c, body_r, body_R), "fur", "body"))
    belly_c = (body_c[0] + (0.14 if k == "walk" else 0.10), body_c[1] - (0.08 if k != "sleep" else 0.05), 0.02)
    add((ellipsoid(belly_c, (body_r[0] * 0.62, body_r[1] * 0.72, body_r[2] * 0.9), body_R), "cream", "body"))
    add((capsule(neck_a, head_c, 0.17 * bz, 0.15 * bz), "fur", "body"))

    hc = np.asarray(head_c, np.float32)
    hs = sp.head_r / 0.34        # features scale with the head

    def hp(local):
        """Head-local offset -> world point (head rotation applied)."""
        return tuple((hc + np.asarray(local, np.float32) * hs @ head_R).tolist())

    add((sphere(head_c, sp.head_r), "fur", "body"))
    sx, sy, rx, ry, rz = sp.snout
    add((ellipsoid(hp((sx, sy, 0.0)), (rx * hs, ry * hs, rz * hs), head_R), sp.snout_mat, "body"))       # snout
    nx, ny, nz = sp.nose_r
    add((ellipsoid(hp((sx + rx - 0.015, sy + ry * 0.4, 0.0)), (nx * hs, ny * hs, nz * hs), head_R), "nose", "face"))
    if pose.tongue and sp.tongue:
        add((ellipsoid(hp((sx + 0.07, sy - 0.12, 0.0)), (0.06, 0.08, 0.05), head_R), "pink", "face"))
    es = sp.ear_size
    for zs in (1, -1):
        if pose.eyes == "open":
            add((ellipsoid(hp((0.22, 0.09, 0.165 * zs)), (0.07, 0.08, 0.06), head_R), "black", "face"))
            add((sphere(hp((0.265, 0.125, 0.205 * zs)), 0.019), "white", "face"))            # catchlight
        else:  # closed: a thin dark lid line
            add((ellipsoid(hp((0.23, 0.08, 0.17 * zs)), (0.065, 0.013, 0.035), head_R), "black", "face"))
        if sp.ears == "floppy":   # attached on the side of the head, hanging past the cheeks, tips flared out
            mid = hp((-0.06, -0.10 + (0.06 if k == "sleep" else 0.0), 0.34 * zs))
            ear_R = rot(yaw=0.0, pitch=-0.12, roll=EAR_ROLL * zs) @ head_R
            add((ellipsoid(mid, (0.12, 0.25, 0.06), ear_R), "ear", "ears"))
        elif sp.ears == "pointed":
            add((capsule(hp((-0.07, 0.22, 0.19 * zs)), hp((-0.05, 0.22 + 0.30 * es, (0.19 + 0.08 * es) * zs)),
                         0.11 * es * hs, 0.02), "ear", "ears"))
            add((capsule(hp((-0.02, 0.26, 0.19 * zs)), hp((-0.01, 0.22 + 0.24 * es, (0.19 + 0.06 * es) * zs)),
                         0.06 * es * hs, 0.012), "inner", "earin"))
        elif sp.ears == "round":
            add((ellipsoid(hp((-0.06, 0.25, 0.24 * zs)), (0.05 * hs, 0.11 * es * hs, 0.10 * es * hs), head_R), "ear", "ears"))
            add((ellipsoid(hp((-0.025, 0.25, 0.24 * zs)), (0.03 * hs, 0.065 * es * hs, 0.06 * es * hs), head_R), "inner", "earin"))
        elif sp.ears == "long":
            base, tip = hp((-0.08, 0.24, 0.12 * zs)), hp((-0.17, 0.74, 0.20 * zs))
            add((capsule(base, tip, 0.075 * hs, 0.062 * hs), "ear", "ears"))
            add((capsule(hp((-0.035, 0.30, 0.12 * zs)), hp((-0.12, 0.68, 0.20 * zs)), 0.042 * hs, 0.032 * hs), "inner", "earin"))
        elif sp.ears == "koala":
            add((ellipsoid(hp((-0.05, 0.22, 0.30 * zs)), (0.06 * hs, 0.17 * hs, 0.17 * hs), head_R), "ear", "ears"))
            add((ellipsoid(hp((-0.01, 0.21, 0.29 * zs)), (0.035 * hs, 0.11 * hs, 0.11 * hs), head_R), "cream", "earin"))

    # collar + tag (pets only)
    nd = np.asarray(head_c, np.float32) - np.asarray(neck_a, np.float32)
    nd = nd / np.linalg.norm(nd)
    if sp.collar is not None:
        collar_c = tuple((np.asarray(neck_a) + nd * 0.20).tolist())
        ang = math.atan2(nd[0], nd[1])
        add((torus(collar_c, 0.17 * bz, 0.045, rot(pitch=ang)), "collar", "acc"))
        tag_c = tuple((np.asarray(collar_c) + np.array([0.21 * math.cos(ang), -0.21 * math.sin(ang) - 0.04, 0.0])).tolist())
        add((sphere(tag_c, 0.05), "gold", "acc"))

    # headphones: band over the head + two cups on the ears
    if pose.phones:
        add((arc_band(hp((-0.02, 0.02, 0.0)), 0.37 * hs, 0.05, rot(yaw=math.pi / 2) @ head_R, keep_y_above=-0.02),
             "phones", "acc"))
        for zs in (1, -1):
            add((cylinder(hp((-0.02, -0.02, 0.36 * zs)), 0.14, 0.055, head_R), "phones", "acc"))
            add((cylinder(hp((-0.02, -0.02, 0.32 * zs)), 0.11, 0.035, head_R), "cushion", "acc"))

    # legs
    L, P = sp.leg_mat, sp.paw_mat
    if k == "walk":
        amp = 0.42
        for (hx, zs, off) in ((0.36, 1, 0.0), (-0.36, -1, 0.0), (0.36, -1, math.pi), (-0.36, 1, math.pi)):
            th = amp * math.sin(pose.phase + off)
            hip = (hx * bx, 0.52 + bob, 0.17 * zs * bz)
            lift = max(0.0, math.sin(pose.phase + off + math.pi / 2)) * 0.07
            paw = (hx * bx + 0.46 * math.sin(th), 0.07 + lift, 0.17 * zs * bz)
            add((capsule(hip, paw, 0.11 * lg, 0.085 * lg), L, "legs"))
            add((ellipsoid((paw[0] + 0.03, paw[1] - 0.01, paw[2]), (0.11 * lg, 0.07, 0.09 * lg)), P, "legs"))
    elif k == "sleep":
        for zs in (1, -1):
            add((capsule((0.35, 0.18, 0.18 * zs), (0.78, 0.08, 0.22 * zs), 0.09 * lg, 0.08 * lg), L, "legs"))
            add((ellipsoid((0.82, 0.07, 0.22 * zs), (0.11 * lg, 0.06, 0.09 * lg)), P, "legs"))
        add((ellipsoid((-0.35, 0.22, 0.24), (0.26, 0.17, 0.12)), L, "legs"))              # haunch
    elif k == "held":
        sway = 0.05 * pose.wag
        for zs in (1, -1):
            add((capsule((0.10, 0.74, 0.15 * zs), (0.16 + sway, 0.36, 0.17 * zs), 0.095 * lg, 0.08 * lg), L, "legs"))
            add((ellipsoid((0.18 + sway, 0.32, 0.17 * zs), (0.09 * lg, 0.065, 0.085 * lg)), P, "legs"))
            add((capsule((-0.12, 0.46, 0.17 * zs), (-0.10 - sway, 0.10, 0.19 * zs), 0.11 * lg, 0.085 * lg), L, "legs"))
            add((ellipsoid((-0.08 - sway, 0.06, 0.19 * zs), (0.11 * lg, 0.065, 0.09 * lg)), P, "legs"))
    else:
        for zs in (1, -1):
            add((capsule((0.12, 0.62, 0.15 * zs), (0.22, 0.08, 0.16 * zs), 0.10 * lg, 0.085 * lg), L, "legs"))
            add((ellipsoid((0.26, 0.06, 0.16 * zs), (0.12 * lg, 0.065, 0.09 * lg)), P, "legs"))
            add((ellipsoid((-0.22, 0.26, 0.20 * zs * bz), (0.26 * bx, 0.22 * by, 0.13 * bz)), L if L != "patch" else "patch", "legs"))
            add((ellipsoid((-0.02, 0.05, 0.22 * zs * bz), (0.14 * lg, 0.06, 0.09 * lg)), P, "legs"))    # back paws

    # tail
    if k == "walk":
        base, mid, tip = (-0.52 * bx, 0.70, 0.0), (-0.72 * bx, 0.92, 0.06 * pose.wag), (-0.80 * bx, 1.12, 0.16 * pose.wag)
        if sp.tail == "thin":      # cats carry it high with a curl
            base, mid, tip = (-0.52 * bx, 0.70, 0.0), (-0.76 * bx, 1.00, 0.05 * pose.wag), (-0.70 * bx, 1.30, 0.12 * pose.wag)
        elif sp.tail == "bushy":   # held low and long
            base, mid, tip = (-0.50 * bx, 0.66, 0.0), (-0.86 * bx, 0.62, 0.05 * pose.wag), (-1.10 * bx, 0.72, 0.12 * pose.wag)
    elif k == "sleep":
        base, mid, tip = (-0.62, 0.28, 0.1), (-0.72, 0.16, 0.30), (-0.52, 0.10, 0.45)
    elif k == "held":
        base, mid, tip = (-0.28, 0.40, 0.0), (-0.40, 0.24, 0.10 * pose.wag), (-0.44, 0.06, 0.22 * pose.wag)
    else:
        base, mid, tip = (-0.36, 0.20, 0.0), (-0.62, 0.10, 0.12 * pose.wag), (-0.82, 0.14, 0.28 * pose.wag)
    if sp.tail in ("stub", "puff"):
        r = 0.08 if sp.tail == "stub" else 0.11
        at = (body_c[0] - body_r[0] * (0.95 if k == "walk" else 0.75), body_c[1] + (0.05 if k == "walk" else -0.10), 0.0)
        add((sphere(at, r), sp.tail_tip or "tail", "tail"))
    else:
        r0, r1, r2 = {"dog": (0.075, 0.06, 0.045), "thin": (0.05, 0.042, 0.035), "tuft": (0.045, 0.04, 0.035),
                      "bushy": (0.10, 0.13, 0.10)}[sp.tail]
        add((capsule(base, mid, r0, r1), "tail", "tail"))
        add((capsule(mid, tip, r1, r2), "tail", "tail"))
        if sp.tail_tip:
            add((sphere(tip, r2 * (1.9 if sp.tail == "tuft" else 1.15)), sp.tail_tip, "tail"))
    return parts, head_c, head_R


EAR_ROLL = -0.30  # tips flare outward

GROUP_K = {"body": 0.08, "legs": 0.05, "tail": 0.04, "ears": 0.02}


def scene_fn(parts):
    def f(p, want_mat=False):
        groups: dict[str, np.ndarray] = {}
        best = np.full(len(p), 1e9, np.float32)
        mat = np.zeros(len(p), np.int32)
        for sdf, m, g in parts:
            d = sdf(p).astype(np.float32)
            if want_mat:
                closer = d < best
                best = np.where(closer, d, best)
                mat = np.where(closer, MAT_IDS[m], mat)
            if g in GROUP_K:
                groups[g] = d if g not in groups else smin(groups[g], d, GROUP_K[g])
            else:
                groups[g] = d if g not in groups else np.minimum(groups[g], d)
        dist = None
        for g, d in groups.items():
            if dist is None:
                dist = d
            elif g in ("legs", "tail"):
                dist = smin(dist, d, 0.05)
            else:
                dist = np.minimum(dist, d)
        return (dist, mat) if want_mat else dist
    return f


# -- camera + shading ----------------------------------------------------------------------------------------
def render(pose: Pose, size: int = 384, sp: Species = SPECIES["puppy"]) -> Image.Image:
    parts, head_c, head_R = build(pose, sp)
    ALB, SPC, SHN = palette(sp)
    model_sdf = scene_fn(parts)
    pivot = np.array([0.05, 0.0, 0.0], np.float32)
    G = rot(yaw=pose.turn)                         # world -> model: p_model = (p - pivot) @ G.T + pivot

    def to_model(p):
        return (p - pivot) @ G.T + pivot if pose.turn else p

    def sdf(p, want_mat=False):
        return model_sdf(to_model(p), want_mat)
    eye = np.array([1.75, 1.62, 3.75], np.float32)   # three-quarter view: you see the face as it walks
    target = np.array([0.06, 0.92, 0.0], np.float32)  # high enough for bunny and fox ears
    fwd = target - eye
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 1, 0]).astype(np.float32)
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    fov = math.radians(36)
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    u = (xs + 0.5) / size * 2 - 1
    v = 1 - (ys + 0.5) / size * 2
    dirs = (fwd[None, :] + (u.reshape(-1, 1) * right + v.reshape(-1, 1) * up) * math.tan(fov / 2))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    n = len(dirs)
    t = np.full(n, 1.5, np.float32)
    hit = np.zeros(n, bool)
    alive = np.ones(n, bool)
    for _ in range(96):
        idx = np.nonzero(alive)[0]
        if len(idx) == 0:
            break
        p = eye + dirs[idx] * t[idx, None]
        d = sdf(p)
        t[idx] += d * 0.9
        done = d < 0.0015
        hit[idx[done]] = True
        gone = t[idx] > 7.0
        alive[idx[done | gone]] = False

    rgba = np.zeros((n, 4), np.float32)
    hi = np.nonzero(hit)[0]
    if len(hi):
        p = eye + dirs[hi] * t[hi, None]
        _, mat = sdf(p, want_mat=True)
        e = 0.0015
        ks = np.array([[1, -1, -1], [-1, -1, 1], [-1, 1, -1], [1, 1, 1]], np.float32)
        nrm = sum(ks[i][None, :] * sdf(p + ks[i] * e)[:, None] for i in range(4))
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9

        # ambient occlusion
        ao = np.ones(len(hi), np.float32)
        for i, h in enumerate((0.02, 0.05, 0.09, 0.14, 0.2)):
            ao -= (h - sdf(p + nrm * h)) * (0.9 / (i + 1))
        ao = np.clip(ao, 0.35, 1.0)

        key = np.array([-0.35, 0.85, 0.55], np.float32)
        key /= np.linalg.norm(key)
        # soft shadow toward the key light
        sh = np.ones(len(hi), np.float32)
        tt = np.full(len(hi), 0.02, np.float32)
        for _ in range(28):
            dd = sdf(p + nrm * 0.003 + key * tt[:, None])
            sh = np.minimum(sh, np.clip(10 * dd / tt, 0, 1))
            tt += np.clip(dd, 0.02, 0.15)
        sh = 0.45 + 0.55 * sh

        alb = ALB[mat].copy()
        pm = to_model(p)
        mark(alb, mat, pm, np.asarray(head_c, np.float32), head_R, sp, ALB[MAT_IDS["patch"]])
        furry = np.isin(mat, [MAT_IDS[m] for m in FURRY])
        noise = 0.97 + 0.03 * np.sin(pm[:, 0] * 57) * np.sin(pm[:, 1] * 63) * np.sin(pm[:, 2] * 71)
        alb[furry] *= noise[furry, None]

        wrap = 0.35
        diff = np.clip((nrm @ key + wrap) / (1 + wrap), 0, 1) * sh
        fill_dir = np.array([0.6, 0.2, 0.7], np.float32)
        fill_dir /= np.linalg.norm(fill_dir)
        fill = np.clip(nrm @ fill_dir, 0, 1) * 0.28
        sky = 0.16 + 0.12 * nrm[:, 1]
        view = -dirs[hi]
        rim = np.clip(1 - np.sum(nrm * view, axis=1), 0, 1) ** 3 * 0.45
        half = key + view
        half /= np.linalg.norm(half, axis=1, keepdims=True)
        spec = SPC[mat] * np.clip(np.sum(nrm * half, axis=1), 0, 1) ** SHN[mat] * sh

        col = alb * (diff[:, None] * 1.25 * np.array([1.0, 0.95, 0.88]) + fill[:, None] * np.array([0.75, 0.85, 1.0])
                     + sky[:, None] * ao[:, None] * np.array([0.55, 0.62, 0.75]))
        col += spec[:, None] + rim[:, None] * np.array([1.0, 0.92, 0.8]) * furry[:, None] * 0.6
        col = col * ao[:, None] ** 0.5
        col = col / (1 + col * 0.18)                   # soft tone map
        lum = col @ np.array([0.3, 0.59, 0.11], np.float32)
        col = lum[:, None] + (col - lum[:, None]) * 1.18   # a touch more colour
        col = np.clip(col, 0, 1) ** (1 / 2.2)          # gamma
        rgba[hi, :3] = col
        rgba[hi, 3] = 1.0

    img = Image.fromarray((rgba.reshape(size, size, 4) * 255).astype(np.uint8), "RGBA")
    tan = math.tan(fov / 2)

    def frac(P):
        d = np.asarray(P, np.float32) - eye
        z = d @ fwd
        return (float(d @ right) / z / tan + 1) / 2, (1 - float(d @ up) / z / tan) / 2
    gx = 0.03 if pose.kind == "walk" else -0.03
    (cx, cy), (ex, _) = frac((gx, 0.0, 0.0)), frac((gx + 1.0, 0.0, 0.0))
    return with_ground_shadow(img, pose, cx, cy, abs(ex - cx))


def mark(alb, mat, pm, head_c, head_R, sp: Species, dark) -> None:
    """Coat markings, painted in the animal's own coordinates so they move with it."""
    hs = sp.head_r / 0.34
    local = ((pm - head_c) @ head_R.T) / hs
    on_head = np.linalg.norm(pm - head_c, axis=1) < sp.head_r * 1.08
    fur, cream, tail = (mat == MAT_IDS["fur"]), (mat == MAT_IDS["cream"]), (mat == MAT_IDS["tail"])
    marks = sp.marks
    if "eye_patch" in marks:       # the puppy's patch on the near side of the face
        alb[(np.linalg.norm(local - np.array([0.16, 0.10, 0.16], np.float32), axis=1) < 0.13) & fur] = dark
    if "panda" in marks:
        for zs in (1, -1):
            d = np.linalg.norm((local - np.array([0.20, 0.075, 0.165 * zs], np.float32)) / np.array([1.0, 0.75, 1.0]), axis=1)
            alb[(d < 0.115) & (fur | cream) & on_head] = dark
        r = np.linalg.norm(pm - head_c, axis=1)
        alb[fur & ~on_head & (r > sp.head_r * 1.05) & (r < sp.head_r * 1.75) & (pm[:, 1] < head_c[1] - 0.12)] = dark
    if "stripes" in marks or "faint_stripes" in marks:
        wave = np.sin(pm[:, 0] * 22 + 2.2 * np.sin(pm[:, 1] * 8) + 1.5 * np.sin(pm[:, 2] * 6))
        body = (fur | tail) & ~on_head & (wave > 0.62)
        head = fur & on_head & (local[:, 1] > 0.12) & (np.sin(local[:, 2] * 30) > 0.72)
        sel = body | head
        alb[sel] = dark if "stripes" in marks else alb[sel] * 0.55 + dark * 0.45
    if "mask" in marks:            # raccoon: dark band across the eyes, white brows and muzzle
        alb[(fur | cream) & on_head & (np.abs(local[:, 1] - 0.085) < 0.065) & (local[:, 0] > 0.05) & (local[:, 0] < 0.30)] = dark
    if "rings" in marks:
        alb[tail & (np.sin(pm[:, 0] * 26 + pm[:, 1] * 8) > 0.15)] = dark


def with_ground_shadow(img: Image.Image, pose: Pose, cx: float, cy: float, unit: float) -> Image.Image:
    """Soft contact shadow under the animal; cx, cy = ground point and unit = one world unit, as image fractions."""
    size = img.width
    shadow = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(shadow)
    w = size * unit * {"sit": 0.60, "held": 0.37}.get(pose.kind, 0.83)
    h = size * unit * 0.069
    x, y = size * cx, size * cy
    d.ellipse((x - w, y - h, x + w, y + h), fill=55 if pose.kind == "held" else 120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(size / 40))
    base = Image.new("RGBA", (size, size), (20, 16, 12, 0))
    base.putalpha(shadow)
    base.alpha_composite(img)
    return base


# -- 2D badges (added after mirroring so symbols read correctly) ----------------------------------------------
def badge_dot(img, color=(255, 59, 48, 255)):
    s = img.width / 256
    d = ImageDraw.Draw(img)
    d.ellipse((196 * s, 14 * s, 242 * s, 60 * s), fill=color, outline=(255, 255, 255, 255), width=int(7 * s))
    return img


def badge_alert(img):
    s = img.width / 256
    badge_dot(img, (255, 159, 10, 255))
    d = ImageDraw.Draw(img)
    d.polygon([(212 * s, 22 * s), (226 * s, 22 * s), (222 * s, 43 * s), (216 * s, 43 * s)], fill=(255, 255, 255, 255))
    d.ellipse((214 * s, 46 * s, 224 * s, 56 * s), fill=(255, 255, 255, 255))
    return img


def badge_notepad(img, pencil):
    s = img.width / 256
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((176 * s, 144 * s, 236 * s, 214 * s), radius=int(8 * s), fill=(250, 250, 246, 255),
                        outline=(70, 60, 50, 255), width=int(5 * s))
    for y in (162, 178, 194):
        d.line((186 * s, y * s, 226 * s, y * s), fill=(120, 140, 175, 255), width=int(5 * s))
    d.line(((186 + pencil) * s, 208 * s, (212 + pencil) * s, 168 * s), fill=(255, 159, 10, 255), width=int(11 * s))
    d.line(((186 + pencil) * s, 208 * s, (191 + pencil) * s, 200 * s), fill=(60, 40, 30, 255), width=int(6 * s))
    return img


def badge_z(img, lift):
    s = img.width / 256
    d = ImageDraw.Draw(img)
    for x, y, z in ((150, 104 - lift, 20), (178, 72 - lift, 26), (208, 36 - lift, 20)):
        d.line([(x * s, y * s), ((x + z) * s, y * s), (x * s, (y + z) * s), ((x + z) * s, (y + z) * s)],
               fill=(90, 110, 170, 255), width=int(7 * s))
    return img


def poses() -> dict[str, tuple[Pose, object]]:
    out: dict[str, tuple[Pose, object]] = {}
    for i in range(8):
        ph = i / 8 * 2 * math.pi
        out[f"walk_{i}"] = (Pose("walk", phase=ph, wag=math.sin(ph * 2)), None)
    out["sit_0"] = (Pose("sit", wag=-1.0), None)
    out["sit_1"] = (Pose("sit", wag=1.0, tongue=True), None)
    out["listen_0"] = (Pose("sit", phones=True, tilt=0.12, wag=0.4), badge_dot)
    out["listen_1"] = (Pose("sit", phones=True, tilt=-0.12, tongue=True, wag=-0.4), badge_dot)
    out["write_0"] = (Pose("sit", eyes="open", wag=0.2), lambda im: badge_notepad(im, 0))
    out["write_1"] = (Pose("sit", eyes="open", tongue=True, wag=-0.2), lambda im: badge_notepad(im, 14))
    out["sleep_0"] = (Pose("sleep", eyes="closed", breath=0.0), lambda im: badge_z(im, 0))
    out["sleep_1"] = (Pose("sleep", eyes="closed", breath=0.02), lambda im: badge_z(im, 10))
    out["alert_0"] = (Pose("sit", wag=0.0), badge_alert)
    out["held_0"] = (Pose("held", tilt=0.14, wag=1.0, tongue=True), None)
    out["held_1"] = (Pose("held", tilt=-0.14, wag=-1.0, tongue=True), None)
    return out


def species_dir(name: str) -> Path:
    return DEST if name == "puppy" else DEST / name


def render_one(job):
    name, species, size, quick = job
    sp = SPECIES[species]
    pose, badge = poses()[name]
    img = render(pose, size, sp)
    pose_name, n = name.rsplit("_", 1)
    right, left = img.copy(), ImageOps.mirror(img)
    if badge:
        right, left = badge(right), badge(left)
    out = {}
    for facing, im in (("r", right), ("l", left)):
        key = f"{pose_name}_{facing}_{n}"
        out[key] = im.resize((OUT, OUT), Image.LANCZOS)
        if not quick:
            d = species_dir(species)
            d.mkdir(parents=True, exist_ok=True)
            out[key].save(d / f"{key}.png", optimize=True)
    return species, out


def sheet(rendered: dict[str, dict[str, Image.Image]], path: Path) -> None:
    """All pets sitting in a row, with their names (for the README)."""
    names = [s for s in SPECIES if s in rendered and "sit_r_1" in rendered[s]]
    cols = 5
    rows = (len(names) + cols - 1) // cols
    cell = OUT + 34
    img = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 15)
    except OSError:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
    for i, s in enumerate(names):
        x, y = (i % cols) * cell, (i // cols) * cell
        img.alpha_composite(rendered[s]["sit_r_1"], (x + 17, y))
        label = SPECIES[s].label
        w = d.textlength(label, font=font)
        d.text((x + (cell - w) / 2, y + OUT + 4), label, fill=(90, 96, 108, 255), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", nargs="*", help="pose names, e.g. sit_1 walk_0")
    ap.add_argument("--species", nargs="*", default=list(SPECIES))
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--sheet-only", action="store_true", help="rebuild docs/images/pets.png from the saved sprites")
    a = ap.parse_args(argv)
    if a.sheet_only:
        saved = {sp: {"sit_r_1": Image.open(species_dir(sp) / "sit_r_1.png").convert("RGBA")} for sp in SPECIES
                 if (species_dir(sp) / "sit_r_1.png").exists()}
        sheet(saved, ROOT / "docs" / "images" / "pets.png")
        return
    size = 160 if a.quick else 384
    jobs = [(name, sp, size, a.quick) for sp in a.species for name in poses() if not a.only or name in a.only]
    rendered: dict[str, dict[str, Image.Image]] = {}
    with ProcessPoolExecutor(max_workers=a.jobs or None) as pool:
        for species, out in pool.map(render_one, jobs):
            rendered.setdefault(species, {}).update(out)
            print("rendered", species, next(iter(out))[:-6], file=sys.stderr, flush=True)
    if a.quick:
        sheet(rendered, ROOT / "tools" / "pets_quick.png")


if __name__ == "__main__":
    main()

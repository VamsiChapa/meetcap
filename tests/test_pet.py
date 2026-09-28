import random

from meetcap.pet import FRAMES, PetBrain, clamp_drop, load_position, save_position, sprite_dir


def _run(brain, mood, seconds, left=0.0, right=1000.0, dt=0.1):
    out = []
    for _ in range(int(seconds / dt)):
        out.append(brain.step(mood, dt, left, right))
    return out


def test_every_sprite_exists():
    for pose, n in FRAMES.items():
        for facing in "rl":
            for i in range(n):
                assert (sprite_dir() / f"{pose}_{facing}_{i}.png").exists(), (pose, facing, i)


def test_wanders_within_the_screen_and_faces_where_it_goes():
    brain = PetBrain(rng=random.Random(7), x=500)
    frames = _run(brain, "idle", 120)
    assert all(0.0 <= f.x <= 1000.0 for f in frames)
    walks = [f for f in frames if f.sprite.startswith("walk_")]
    assert walks, "it should walk around"
    assert {f.sprite.split("_")[0] for f in frames} >= {"walk", "sit"}
    for a, b in zip(frames, frames[1:], strict=False):          # moving right means facing right, and vice versa
        if b.sprite.startswith("walk_") and b.x != a.x:
            assert (b.x > a.x) == (b.facing == "r")


def test_busy_moods_stop_it_and_show_the_right_pose():
    brain = PetBrain(rng=random.Random(1), x=300)
    brain.state, brain.target = "walk", 900
    rec = _run(brain, "rec", 2)
    assert all(f.sprite.startswith("listen_") for f in rec) and len({f.x for f in rec}) == 1
    assert _run(brain, "work", 1)[-1].sprite.startswith("write_")
    assert _run(brain, "sleep", 1)[-1].sprite.startswith("sleep_")
    assert _run(brain, "error", 1)[-1].sprite.startswith("alert_")


def test_clamps_when_the_screen_shrinks():
    brain = PetBrain(rng=random.Random(3), x=1800)
    f = brain.step("idle", 0.1, 0.0, 1200.0)
    assert f.x <= 1200.0


def test_sometimes_runs():
    brain = PetBrain(rng=random.Random(11), x=100)
    speeds = []
    last = brain.x
    for f in _run(brain, "idle", 300):
        if f.sprite.startswith("walk_"):
            speeds.append(abs(f.x - last) / 0.1)
        last = f.x
    assert max(speeds) > PetBrain.SPEED * 1.5        # dashed at least once
    assert min(s for s in speeds if s > 0) <= PetBrain.SPEED + 1e-6


def test_carry_and_drop():
    brain = PetBrain(rng=random.Random(5), x=100)
    brain.state, brain.target = "walk", 900
    brain.hold()
    held = _run(brain, "rec", 1)                    # carried: dangles even while recording
    assert all(f.sprite.startswith("held_") for f in held)
    brain.drop(640)
    f = brain.step("idle", 0.1, 0, 1000)
    assert f.x == 640 and f.sprite.startswith("sit_")


def test_click_makes_it_wag_fast():
    brain = PetBrain(rng=random.Random(2), x=300)
    brain.state, brain.target = "walk", 900
    brain.poke()
    sprites = [f.sprite for f in _run(brain, "idle", 1.0)]
    assert all(s.startswith("sit_") for s in sprites)
    assert len({s for s in sprites}) == 2 and sum(a != b for a, b in zip(sprites, sprites[1:], strict=False)) >= 3


def test_drop_is_kept_on_screen():
    frame = (0.0, 25.0, 1512.0, 920.0)               # a 14" MacBook visible area above the Dock
    assert clamp_drop(-50, 2000, 64, frame) == (0.0, 25.0 + 920.0 - 64)
    assert clamp_drop(700, 400, 64, frame) == (700, 400)


def test_position_is_remembered(tmp_path):
    p = tmp_path / "pet.json"
    assert load_position(p) is None
    save_position(812.34, 403.2, p)
    assert load_position(p) == {"x": 812.3, "y": 403.2}


def test_every_species_has_every_frame():
    from meetcap.pet import FRAMES, SPECIES, available_species, sprite_dir, sprite_resource
    assert available_species() == list(SPECIES)
    want = {f"{pose}_{side}_{i}.png" for pose, n in FRAMES.items() for side in "rl" for i in range(n)}
    for sp in SPECIES:
        have = {p.name for p in sprite_dir(sp).glob("*.png")}
        assert want <= have, (sp, sorted(want - have)[:3])
    assert sprite_resource("fox", "sit_r_0") == "pet/fox/sit_r_0.png"
    assert sprite_resource("puppy", "sit_r_0") == "pet/sit_r_0.png"
    assert sprite_resource("dragon", "sit_r_0") == "pet/sit_r_0.png"     # unknown: the puppy

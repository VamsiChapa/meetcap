from meetcap.agent import AgentStatus
from meetcap.menubar import SEQUENCES, frame_for, icon_dir, mood


def _s(**kw):
    base = dict(recording=False, app=None, since=None, manual=False, auto=True, detection_available=True)
    base.update(kw)
    return AgentStatus(**base)


def test_mood_priority():
    assert mood(_s()) == "idle"
    assert mood(_s(recording=True, auto=False, last_error="x")) == "rec"
    assert mood(_s(auto=False)) == "sleep"
    assert mood(_s(last_error="boom", processing=True)) == "error"
    assert mood(_s(processing=True)) == "work"


def test_frames_cycle_and_exist():
    assert [frame_for(_s(recording=True), t) for t in range(5)] == ["rec_0", "rec_1", "rec_2", "rec_3", "rec_0"]
    assert frame_for(_s(recording=True), 3, animate=False) == "rec_0"
    names = {f for seq in SEQUENCES.values() for f in seq}
    for n in names:
        assert (icon_dir() / f"{n}.png").exists(), n

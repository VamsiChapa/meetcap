from meetcap.detect import MeetingDetector, match_app

APPS = ["com.microsoft.teams2", "com.microsoft.teams", "us.zoom.xos"]


def test_match_app_prefix_rules():
    assert match_app({"com.microsoft.teams2.helper"}, APPS) == "com.microsoft.teams2"
    assert match_app({"com.microsoft.teams2"}, APPS) == "com.microsoft.teams2"
    assert match_app({"com.microsoft.teams"}, APPS) == "com.microsoft.teams"
    assert match_app({"com.microsoft.teamsfoo"}, APPS) is None
    assert match_app({"com.obsproject.obs-studio", "com.apple.Siri"}, APPS) is None
    assert match_app(set(), APPS) is None


def test_start_is_debounced():
    d = MeetingDetector(start_after=3, stop_after=20)
    assert d.update("teams", 0) is None
    assert d.update("teams", 2) is None
    ev = d.update("teams", 3)
    assert ev and ev.kind == "start" and ev.app == "teams" and ev.at == 0


def test_blip_does_not_start():
    d = MeetingDetector(start_after=3, stop_after=20)
    d.update("teams", 0)
    d.update(None, 1)          # mic released before debounce
    assert d.update("teams", 2) is None
    assert d.update("teams", 4) is None  # restarted the clock at 2
    assert d.update("teams", 5).kind == "start"


def test_stop_needs_grace_period_and_mute_toggle_survives():
    d = MeetingDetector(start_after=0, stop_after=20)
    assert d.update("teams", 0).kind == "start"
    assert d.update(None, 10) is None     # brief release (mute toggle)
    assert d.update("teams", 15) is None  # back: grace timer cleared
    assert d.update(None, 30) is None
    ev = d.update(None, 50)
    assert ev and ev.kind == "stop" and ev.at == 30
    assert d.active_app is None


def test_switching_apps_keeps_meeting_alive():
    d = MeetingDetector(start_after=0, stop_after=5)
    d.update("teams", 0)
    assert d.update("zoom", 3) is None
    assert d.update("zoom", 30) is None

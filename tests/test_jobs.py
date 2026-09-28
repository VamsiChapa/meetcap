from meetcap.jobs import JobStore, backoff_seconds
from meetcap.models import Job, Stage, Status


def _store(tmp_path):
    return JobStore(tmp_path / "jobs.sqlite3")


def test_add_is_idempotent_on_source(tmp_path):
    s = _store(tmp_path)
    a = s.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2))
    b = s.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2))
    assert a.id == b.id
    assert s.counts() == {"pending": 1}


def test_advance_and_done(tmp_path):
    s = _store(tmp_path)
    j = s.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2, data={"k": 1}))
    j = s.advance(j, Stage.UPLOAD_MEDIA)
    again = s.get(j.id)
    assert again.stage == Stage.UPLOAD_MEDIA and again.data == {"k": 1}
    j = s.advance(j, Stage.DONE)
    assert s.get(j.id).status == Status.DONE


def test_fail_backs_off_then_dead_letters(tmp_path):
    s = _store(tmp_path)
    j = s.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2))
    j = s.fail(j, "boom", max_attempts=3, now=1000)
    assert j.status == Status.PENDING and 1030 <= j.next_attempt_at <= 1033
    assert s.next_due(now=1001) is None
    assert s.next_due(now=2000).id == j.id
    j = s.fail(j, "boom", max_attempts=3, now=2000)
    j = s.fail(j, "boom", max_attempts=3, now=3000)
    assert j.status == Status.FAILED
    assert s.next_due(now=10**10) is None
    j = s.retry(j)
    assert j.status == Status.PENDING and j.attempts == 0 and s.next_due(now=0).id == j.id


def test_backoff_is_capped():
    assert 30 <= backoff_seconds(1) <= 33
    assert 60 <= backoff_seconds(2) <= 66
    assert 3600 <= backoff_seconds(20) <= 3960


def test_prefix_lookup(tmp_path):
    s = _store(tmp_path)
    j = s.add(Job(source_path="/x/a.mp4", started_at=1, ended_at=2))
    assert s.get(j.id[:5]).id == j.id

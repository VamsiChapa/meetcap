"""Durable job state in SQLite. Survives crashes, sleep and reboots; the worker resumes from the stored stage."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from pathlib import Path

from .models import Job, Stage, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    app TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    blob_prefix TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    lease_until REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_attempt_at);
"""
LEASE_SECONDS = 4 * 3600  # longer than any single stage (a multi-GB upload on hotel Wi-Fi)

COLUMNS = ("id", "source_path", "app", "started_at", "ended_at", "stage", "status", "attempts",
           "next_attempt_at", "last_error", "blob_prefix", "data", "created_at")


def backoff_seconds(attempt: int, base: float = 30.0, cap: float = 3600.0, jitter: float = 0.1) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay + random.uniform(0, delay * jitter)


class JobStore:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "lease_until" not in cols:  # databases created by 0.1.0 pre-release
                self._conn.execute("ALTER TABLE jobs ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")

    # -- mapping ------------------------------------------------------------------
    @staticmethod
    def _row(job: Job) -> tuple:
        return (job.id, job.source_path, job.app, job.started_at, job.ended_at, job.stage.value,
                job.status.value, job.attempts, job.next_attempt_at, job.last_error, job.blob_prefix,
                json.dumps(job.data), job.created_at)

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        d = dict(row)
        return Job(source_path=d["source_path"], started_at=d["started_at"], ended_at=d["ended_at"],
                   app=d["app"], id=d["id"], stage=Stage(d["stage"]), status=Status(d["status"]),
                   attempts=d["attempts"], next_attempt_at=d["next_attempt_at"], last_error=d["last_error"],
                   blob_prefix=d["blob_prefix"], data=json.loads(d["data"] or "{}"), created_at=d["created_at"])

    def _query(self, sql: str, args: tuple = ()) -> list[Job]:
        with self._lock:
            return [self._job(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- API ----------------------------------------------------------------------
    def add(self, job: Job) -> Job:
        """Idempotent on source_path: enqueueing the same file twice returns the existing job."""
        with self._lock:
            self._conn.execute(
                f"INSERT OR IGNORE INTO jobs ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                self._row(job))
        existing = self.by_source(job.source_path)
        assert existing is not None
        return existing

    def save(self, job: Job) -> Job:
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in COLUMNS[1:])} WHERE id = ?",
                self._row(job)[1:] + (job.id,))
        return job

    def get(self, job_id: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE id = ? OR id LIKE ?", (job_id, f"{job_id}%"))
        return rows[0] if len(rows) == 1 else None

    def by_source(self, path: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE source_path = ?", (path,))
        return rows[0] if rows else None

    def list(self, status: Status | None = None, limit: int = 50) -> list[Job]:
        if status:
            return self._query("SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                               (status.value, limit))
        return self._query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))

    def next_due(self, now: float | None = None) -> Job | None:
        now = time.time() if now is None else now
        rows = self._query("SELECT * FROM jobs WHERE status = ? AND next_attempt_at <= ? AND lease_until <= ? "
                           "ORDER BY created_at LIMIT 1", (Status.PENDING.value, now, now))
        return rows[0] if rows else None

    def claim(self, job: Job, now: float | None = None, ttl: float = LEASE_SECONDS) -> bool:
        """Atomically lease a job so two processes (agent worker, `process --now`) never run it at once."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ? AND lease_until <= ?",
                                     (now + ttl, job.id, now))
            return cur.rowcount == 1

    def release(self, job: Job) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET lease_until = 0 WHERE id = ?", (job.id,))

    def claim_next(self, now: float | None = None) -> Job | None:
        now = time.time() if now is None else now
        for _ in range(3):  # lost a race with another process: try the next one
            job = self.next_due(now)
            if job is None:
                return None
            if self.claim(job, now):
                return job
        return None

    def advance(self, job: Job, stage: Stage) -> Job:
        job.stage, job.attempts, job.next_attempt_at, job.last_error = stage, 0, 0.0, ""
        if stage == Stage.DONE and job.status == Status.PENDING:
            job.status = Status.DONE
        return self.save(job)

    def fail(self, job: Job, error: str, max_attempts: int, now: float | None = None) -> Job:
        now = time.time() if now is None else now
        job.attempts += 1
        job.last_error = error[:2000]
        if job.attempts >= max_attempts:
            job.status = Status.FAILED  # dead letter
        else:
            job.next_attempt_at = now + backoff_seconds(job.attempts)
        return self.save(job)

    def retry(self, job: Job) -> Job:
        """Re-queue a dead-lettered job at the stage where it stopped. Only FAILED jobs are eligible."""
        if job.status != Status.FAILED:
            raise ValueError(f"job {job.id} is {job.status.value}; only failed jobs can be retried")
        job.status, job.attempts, job.next_attempt_at = Status.PENDING, 0, 0.0
        return self.save(job)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

"""Domain types shared by every layer."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """Pipeline stages. Each one is idempotent and retried independently."""
    FINALIZE = "finalize"                  # wait for OBS to close the file, validate, drop too-short clips
    EXTRACT_AUDIO = "extract_audio"        # mono 16 kHz Opus for transcription
    UPLOAD_MEDIA = "upload_media"          # video + audio to Blob, size-verified
    TRANSCRIBE = "transcribe"              # Azure AI Speech fast transcription with diarization
    PUBLISH_TRANSCRIPT = "publish_transcript"
    EXTRACT_FRAMES = "extract_frames"      # scene-change screenshots (non-fatal)
    SUMMARIZE = "summarize"                # Azure OpenAI -> structured notes
    PUBLISH_NOTES = "publish_notes"        # notes.md, meta.json, frames, local copy, notification
    CLEANUP = "cleanup"                    # delete local media once every blob is verified
    DONE = "done"


STAGE_ORDER: list[Stage] = list(Stage)


def next_stage(stage: Stage) -> Stage:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[min(i + 1, len(STAGE_ORDER) - 1)]


class Status(str, Enum):
    PENDING = "pending"   # waiting for its next attempt
    DONE = "done"
    SKIPPED = "skipped"   # too short / empty; nothing uploaded
    FAILED = "failed"     # dead-lettered after max attempts; `meetcap retry` re-queues


@dataclass
class Job:
    source_path: str
    started_at: float
    ended_at: float
    app: str = "manual"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: Stage = Stage.FINALIZE
    status: Status = Status.PENDING
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    blob_prefix: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def owned(self) -> bool:
        """True when meetcap created the source file and may delete it."""
        return bool(self.data.get("owned", True))


@dataclass
class Phrase:
    start_ms: int
    duration_ms: int
    text: str
    speaker: int | None = None
    locale: str | None = None

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


@dataclass
class Transcript:
    duration_ms: int
    phrases: list[Phrase]
    raw: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "phrases": [p.__dict__ for p in self.phrases],
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Transcript":
        return cls(
            duration_ms=int(d.get("duration_ms", 0)),
            phrases=[Phrase(**p) for p in d.get("phrases", [])],
            raw=d.get("raw", []),
        )


@dataclass
class Frame:
    path: str
    t_ms: int

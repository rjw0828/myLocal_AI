"""Bounded in-memory job state. No conversation or approval persistence."""
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock, RLock
from uuid import uuid4


class ToolChatError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class JobState(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNCERTAIN = "UNCERTAIN"


TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.UNCERTAIN}


@dataclass(frozen=True)
class Limits:
    tool_calls: int = 8
    ollama_requests: int = 10
    active_seconds: float = 300
    approval_seconds: float = 120
    lifetime_seconds: float = 900
    max_jobs: int = 128
    result_bytes: int = 64 * 1024

    def __post_init__(self):
        import math
        for value in self.__dict__.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Limits must be positive finite numbers")
        for key in ("tool_calls", "ollama_requests", "max_jobs", "result_bytes"):
            if not isinstance(getattr(self, key), int):
                raise ValueError("Count limits must be integers")


@dataclass
class Job:
    session_id: str
    revision: int
    browser_generation: int
    created: float
    updated: float
    catalog: dict
    job_id: str = field(default_factory=lambda: uuid4().hex)
    state: JobState = JobState.CREATED
    cancelled: bool = False
    ended: float | None = None
    active_seconds: float = 0
    waiting_since: float | None = None
    tool_count: int = 0
    ollama_count: int = 0
    operation_id: str | None = None
    operation_server: str | None = None
    in_flight: bool = False
    ollama_in_flight: bool = False
    unsettled: bool = False
    reservation_owned: bool = True
    grants: list = field(default_factory=list)
    queue: deque = field(default_factory=deque)
    pending: dict | None = None
    error_code: str | None = None
    lock: object = field(default_factory=RLock, repr=False)
    runner: object = field(default_factory=Lock, repr=False)

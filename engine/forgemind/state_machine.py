"""The deterministic task state machine.

This module is the single source of truth for what states exist, which
transitions between them are legal, and how a task's state is persisted.
No AI agent, and no other module, may mutate a task's state except through
`TaskStateMachine.transition()`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from forgemind import locking


class TaskState(str, Enum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    DESIGNING = "DESIGNING"
    DESIGNED = "DESIGNED"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    PLAN_APPROVED = "PLAN_APPROVED"
    PLAN_REJECTED = "PLAN_REJECTED"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    TESTING = "TESTING"
    TESTS_PASSED = "TESTS_PASSED"
    TESTS_FAILED = "TESTS_FAILED"
    REVIEWING = "REVIEWING"
    REVIEWED = "REVIEWED"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_REJECTED = "FINAL_REJECTED"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


TERMINAL_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.PLAN_REJECTED,
        TaskState.FINAL_REJECTED,
    }
)

# Explicit legal-transition table: every (from -> {to, ...}) pair is data,
# not inferred at runtime. Nothing is legal unless it is listed here.
#
# Notes on states that don't appear in the graph as their own named
# outcome (e.g. "review changes requested"): per the agreed V1 state list,
# that outcome is represented as REVIEWING -> IMPLEMENTING directly, with
# the *reason* recorded in transition history and revision_retry_count
# tracking how many times it has happened -- not as a separate state.
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.ANALYZING, TaskState.CANCELLED}),
    TaskState.ANALYZING: frozenset(
        {TaskState.ANALYZED, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.ANALYZED: frozenset({TaskState.DESIGNING, TaskState.CANCELLED}),
    TaskState.DESIGNING: frozenset(
        {TaskState.DESIGNED, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.DESIGNED: frozenset({TaskState.AWAITING_PLAN_APPROVAL, TaskState.CANCELLED}),
    TaskState.AWAITING_PLAN_APPROVAL: frozenset(
        {TaskState.PLAN_APPROVED, TaskState.PLAN_REJECTED, TaskState.CANCELLED}
    ),
    TaskState.PLAN_APPROVED: frozenset({TaskState.IMPLEMENTING, TaskState.CANCELLED}),
    TaskState.PLAN_REJECTED: frozenset(),
    TaskState.IMPLEMENTING: frozenset(
        {TaskState.IMPLEMENTED, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.IMPLEMENTED: frozenset({TaskState.TESTING, TaskState.CANCELLED}),
    TaskState.TESTING: frozenset(
        {
            TaskState.TESTS_PASSED,
            TaskState.TESTS_FAILED,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.TESTS_PASSED: frozenset({TaskState.REVIEWING, TaskState.CANCELLED}),
    TaskState.TESTS_FAILED: frozenset(
        {TaskState.IMPLEMENTING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.REVIEWING: frozenset(
        {
            TaskState.REVIEWED,
            TaskState.IMPLEMENTING,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.REVIEWED: frozenset({TaskState.AWAITING_FINAL_APPROVAL, TaskState.CANCELLED}),
    TaskState.AWAITING_FINAL_APPROVAL: frozenset(
        {TaskState.FINAL_APPROVED, TaskState.FINAL_REJECTED, TaskState.CANCELLED}
    ),
    TaskState.FINAL_APPROVED: frozenset({TaskState.FINALIZING, TaskState.CANCELLED}),
    TaskState.FINAL_REJECTED: frozenset(),
    # BLOCKED is included for symmetry with every other in-progress role
    # state (ANALYZING/DESIGNING/IMPLEMENTING/TESTING/REVIEWING all allow
    # it) -- added in Phase 9 when FINALIZING became a real runner-driven
    # stage instead of the always-synchronous Python-only step it was
    # designed as in Phase 1. Without it, a PENDING AgentResult from this
    # stage would crash start_stage() with an IllegalTransitionError.
    TaskState.FINALIZING: frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.BLOCKED: frozenset(
        {
            TaskState.ANALYZING,
            TaskState.DESIGNING,
            TaskState.IMPLEMENTING,
            TaskState.TESTING,
            TaskState.REVIEWING,
            TaskState.FINALIZING,
            TaskState.CANCELLED,
        }
    ),
}

# Every state must have an entry in TRANSITIONS (even if empty). This is
# checked at import time so a missing entry fails loudly, immediately.
assert set(TRANSITIONS.keys()) == set(TaskState), "TRANSITIONS table is missing a state"


class IllegalTransitionError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic replace, including on Windows


@dataclass
class TaskStateMachine:
    tasks_root: Path
    task_id: str
    state: TaskState
    blocked_from: Optional[TaskState] = None
    implementation_retry_count: int = 0
    revision_retry_count: int = 0
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    history: list = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.tasks_root / self.task_id / "state.json"

    @classmethod
    def create(cls, tasks_root: Path, task_id: str) -> "TaskStateMachine":
        sm = cls(tasks_root=tasks_root, task_id=task_id, state=TaskState.CREATED)
        sm.save()
        return sm

    @classmethod
    def load(cls, tasks_root: Path, task_id: str) -> "TaskStateMachine":
        path = tasks_root / task_id / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            tasks_root=tasks_root,
            task_id=task_id,
            state=TaskState(data["state"]),
            blocked_from=TaskState(data["blocked_from"]) if data.get("blocked_from") else None,
            implementation_retry_count=data.get("implementation_retry_count", 0),
            revision_retry_count=data.get("revision_retry_count", 0),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            history=data.get("history", []),
        )

    def save(self) -> None:
        data = {
            "task_id": self.task_id,
            "state": self.state.value,
            "blocked_from": self.blocked_from.value if self.blocked_from else None,
            "implementation_retry_count": self.implementation_retry_count,
            "revision_retry_count": self.revision_retry_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": self.history,
        }
        _atomic_write_json(self.state_path, data)

    def can_transition(self, to_state: TaskState) -> bool:
        return to_state in TRANSITIONS.get(self.state, frozenset())

    def transition(self, to_state: TaskState, reason: Optional[str] = None) -> None:
        if self.state in TERMINAL_STATES:
            raise IllegalTransitionError(
                f"Task {self.task_id} is in terminal state {self.state.value}; "
                f"no further transitions are allowed."
            )
        if not self.can_transition(to_state):
            raise IllegalTransitionError(
                f"Illegal transition for task {self.task_id}: "
                f"{self.state.value} -> {to_state.value}"
            )
        from_state = self.state
        if to_state == TaskState.BLOCKED:
            self.blocked_from = from_state
        elif from_state == TaskState.BLOCKED:
            self.blocked_from = None

        self.state = to_state
        self.updated_at = _now_iso()
        self.history.append(
            {"from": from_state.value, "to": to_state.value, "at": self.updated_at, "reason": reason}
        )
        self.save()

        if to_state in TERMINAL_STATES:
            locking.release(self.tasks_root, self.task_id)

    def increment_implementation_retry(self) -> int:
        self.implementation_retry_count += 1
        self.save()
        return self.implementation_retry_count

    def increment_revision_retry(self) -> int:
        self.revision_retry_count += 1
        self.save()
        return self.revision_retry_count

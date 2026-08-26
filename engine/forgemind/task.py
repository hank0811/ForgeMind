"""Task creation and the task.md request record.

Creating a task is the point where the single active-task lock is acquired
(forgemind.locking) and the initial state machine (state = CREATED) is
written. No AI call happens here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from forgemind import artifacts, locking
from forgemind.state_machine import TaskStateMachine


@dataclass
class Task:
    task_id: str
    tasks_root: Path
    artifacts_root: Path

    @property
    def task_dir(self) -> Path:
        return self.tasks_root / self.task_id

    @property
    def task_md_path(self) -> Path:
        return self.task_dir / "task.md"

    @property
    def artifact_dir(self) -> Path:
        return self.artifacts_root / self.task_id


def generate_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{stamp}-{suffix}"


def create_task(tasks_root: Path, artifacts_root: Path, request: str) -> Task:
    """Create a new task. Raises TaskAlreadyActiveError if another task is active."""
    task_id = generate_task_id()
    locking.acquire(tasks_root, task_id)  # raises if another task is already active

    task = Task(task_id=task_id, tasks_root=tasks_root, artifacts_root=artifacts_root)
    task.task_dir.mkdir(parents=True, exist_ok=True)
    task.artifact_dir.mkdir(parents=True, exist_ok=True)

    artifacts.write_markdown(
        task.task_md_path,
        {
            "id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "CREATED",
        },
        request,
    )
    TaskStateMachine.create(tasks_root, task_id)
    return task


def read_task_request(tasks_root: Path, task_id: str) -> str:
    path = tasks_root / task_id / "task.md"
    _frontmatter, body = artifacts.read_markdown(path)
    return body

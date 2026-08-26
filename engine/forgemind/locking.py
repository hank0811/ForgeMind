"""Single active-task locking.

Only one task may be active at a time in Version 1. The lock is a single
file at ``tasks/.active_task`` containing the active task_id. Python is the
only code that ever writes it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class TaskAlreadyActiveError(Exception):
    pass


def _lock_path(tasks_root: Path) -> Path:
    return tasks_root / ".active_task"


def get_active(tasks_root: Path) -> Optional[str]:
    path = _lock_path(tasks_root)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def is_locked(tasks_root: Path) -> bool:
    return get_active(tasks_root) is not None


def acquire(tasks_root: Path, task_id: str) -> None:
    tasks_root.mkdir(parents=True, exist_ok=True)
    active = get_active(tasks_root)
    if active is not None and active != task_id:
        raise TaskAlreadyActiveError(
            f"Task {active} is already active; cannot activate {task_id} "
            f"until it reaches a terminal state."
        )
    path = _lock_path(tasks_root)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(task_id, encoding="utf-8")
    os.replace(tmp_path, path)


def release(tasks_root: Path, task_id: str) -> None:
    """Release the lock only if it is currently held by task_id (safe no-op otherwise)."""
    path = _lock_path(tasks_root)
    if not path.exists():
        return
    if get_active(tasks_root) == task_id:
        path.unlink()

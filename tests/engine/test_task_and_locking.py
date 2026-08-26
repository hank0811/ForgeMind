from __future__ import annotations

import pytest

from forgemind import locking
from forgemind import task as task_module
from forgemind.state_machine import TaskState, TaskStateMachine


def test_create_task_creates_expected_files(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add dark mode toggle")
    assert task.task_md_path.exists()
    assert task.artifact_dir.exists()
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    assert sm.state == TaskState.CREATED


def test_task_ids_are_unique(repo):
    t1 = task_module.create_task(repo / "tasks", repo / "artifacts", "Task one")
    sm1 = TaskStateMachine.load(repo / "tasks", t1.task_id)
    sm1.transition(TaskState.CANCELLED)  # release lock so a second task can be created
    t2 = task_module.create_task(repo / "tasks", repo / "artifacts", "Task two")
    assert t1.task_id != t2.task_id


def test_second_task_blocked_while_first_active(repo):
    task_module.create_task(repo / "tasks", repo / "artifacts", "First task")
    with pytest.raises(locking.TaskAlreadyActiveError):
        task_module.create_task(repo / "tasks", repo / "artifacts", "Second task")


def test_creating_second_task_does_not_disturb_first(repo):
    t1 = task_module.create_task(repo / "tasks", repo / "artifacts", "First task")
    with pytest.raises(locking.TaskAlreadyActiveError):
        task_module.create_task(repo / "tasks", repo / "artifacts", "Second task")
    assert locking.get_active(repo / "tasks") == t1.task_id


def test_lock_released_after_terminal_allows_new_task(repo):
    t1 = task_module.create_task(repo / "tasks", repo / "artifacts", "First task")
    sm1 = TaskStateMachine.load(repo / "tasks", t1.task_id)
    sm1.transition(TaskState.CANCELLED)
    t2 = task_module.create_task(repo / "tasks", repo / "artifacts", "Second task")
    assert locking.get_active(repo / "tasks") == t2.task_id


def test_read_task_request_roundtrip(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a retry button")
    assert task_module.read_task_request(repo / "tasks", task.task_id) == "Add a retry button"


def test_generate_task_id_is_filesystem_safe():
    task_id = task_module.generate_task_id()
    assert "/" not in task_id
    assert "\\" not in task_id
    assert " " not in task_id

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgemind import locking
from forgemind.state_machine import (
    TERMINAL_STATES,
    IllegalTransitionError,
    TaskState,
    TaskStateMachine,
)

HAPPY_PATH = [
    TaskState.ANALYZING,
    TaskState.ANALYZED,
    TaskState.DESIGNING,
    TaskState.DESIGNED,
    TaskState.AWAITING_PLAN_APPROVAL,
    TaskState.PLAN_APPROVED,
    TaskState.IMPLEMENTING,
    TaskState.IMPLEMENTED,
    TaskState.TESTING,
    TaskState.TESTS_PASSED,
    TaskState.REVIEWING,
    TaskState.REVIEWED,
    TaskState.AWAITING_FINAL_APPROVAL,
    TaskState.FINAL_APPROVED,
    TaskState.FINALIZING,
    TaskState.COMPLETED,
]


def _sm(repo: Path, task_id: str = "t1") -> TaskStateMachine:
    locking.acquire(repo / "tasks", task_id)
    return TaskStateMachine.create(repo / "tasks", task_id)


# --- legal transitions -------------------------------------------------


def test_legal_transition_created_to_analyzing(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    assert sm.state == TaskState.ANALYZING


def test_full_happy_path_is_legal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH:
        sm.transition(state)
    assert sm.state == TaskState.COMPLETED


def test_retry_loop_transitions_are_legal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH[: HAPPY_PATH.index(TaskState.TESTING) + 1]:
        sm.transition(state)
    sm.transition(TaskState.TESTS_FAILED)
    sm.transition(TaskState.IMPLEMENTING)  # test-failure retry loop-back
    assert sm.state == TaskState.IMPLEMENTING


def test_review_revision_loop_transition_is_legal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH[: HAPPY_PATH.index(TaskState.REVIEWING) + 1]:
        sm.transition(state)
    sm.transition(TaskState.IMPLEMENTING)  # review-changes-requested loop-back
    assert sm.state == TaskState.IMPLEMENTING


def test_blocked_and_resume(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.BLOCKED)
    assert sm.blocked_from == TaskState.ANALYZING
    sm.transition(TaskState.ANALYZING)
    assert sm.state == TaskState.ANALYZING
    assert sm.blocked_from is None


# --- illegal transitions ------------------------------------------------


def test_illegal_transition_raises(repo):
    sm = _sm(repo)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.COMPLETED)


def test_illegal_transition_skipping_stages_raises(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.IMPLEMENTING)


def test_illegal_transition_does_not_mutate_state(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.COMPLETED)
    assert sm.state == TaskState.ANALYZING


# --- terminal states ------------------------------------------------------


def test_cancelled_is_terminal(repo):
    sm = _sm(repo)
    sm.transition(TaskState.CANCELLED)
    assert sm.state in TERMINAL_STATES
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.ANALYZING)


def test_failed_is_terminal(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.FAILED)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.ANALYZED)


def test_plan_rejected_is_terminal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH[: HAPPY_PATH.index(TaskState.AWAITING_PLAN_APPROVAL) + 1]:
        sm.transition(state)
    sm.transition(TaskState.PLAN_REJECTED)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.PLAN_APPROVED)


def test_final_rejected_is_terminal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH[: HAPPY_PATH.index(TaskState.AWAITING_FINAL_APPROVAL) + 1]:
        sm.transition(state)
    sm.transition(TaskState.FINAL_REJECTED)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.FINAL_APPROVED)


def test_completed_is_terminal(repo):
    sm = _sm(repo)
    for state in HAPPY_PATH:
        sm.transition(state)
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.FAILED)


def test_lock_released_on_terminal_state(repo):
    sm = _sm(repo, task_id="lockme")
    assert locking.get_active(repo / "tasks") == "lockme"
    sm.transition(TaskState.CANCELLED)
    assert locking.get_active(repo / "tasks") is None


def test_lock_not_released_on_non_terminal_state(repo):
    sm = _sm(repo, task_id="lockme2")
    sm.transition(TaskState.ANALYZING)
    assert locking.get_active(repo / "tasks") == "lockme2"


# --- atomic state writes ----------------------------------------------


def test_no_leftover_tmp_file_after_save(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    tmp_path = sm.state_path.with_name(sm.state_path.name + ".tmp")
    assert not tmp_path.exists()
    assert sm.state_path.exists()


def test_state_json_is_valid_and_current_after_each_transition(repo):
    sm = _sm(repo)
    for state in [TaskState.ANALYZING, TaskState.ANALYZED]:
        sm.transition(state)
        data = json.loads(sm.state_path.read_text(encoding="utf-8"))
        assert data["state"] == state.value


def test_reload_from_disk_matches_in_memory_state(repo):
    sm = _sm(repo, task_id="reload-me")
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.ANALYZED)
    reloaded = TaskStateMachine.load(repo / "tasks", "reload-me")
    assert reloaded.state == TaskState.ANALYZED
    assert reloaded.history == sm.history

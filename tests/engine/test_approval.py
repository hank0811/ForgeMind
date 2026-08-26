from __future__ import annotations

from pathlib import Path

import pytest

from forgemind import locking, orchestrator
from forgemind.state_machine import IllegalTransitionError, TaskState, TaskStateMachine


def _sm(repo: Path, task_id: str = "t1") -> TaskStateMachine:
    locking.acquire(repo / "tasks", task_id)
    return TaskStateMachine.create(repo / "tasks", task_id)


def _at_awaiting_plan_approval(repo: Path, task_id: str = "t1") -> TaskStateMachine:
    sm = _sm(repo, task_id)
    for state in [
        TaskState.ANALYZING,
        TaskState.ANALYZED,
        TaskState.DESIGNING,
        TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL,
    ]:
        sm.transition(state)
    return sm


def _at_awaiting_final_approval(repo: Path, task_id: str = "t1") -> TaskStateMachine:
    sm = _at_awaiting_plan_approval(repo, task_id)
    for state in [
        TaskState.PLAN_APPROVED,
        TaskState.IMPLEMENTING,
        TaskState.IMPLEMENTED,
        TaskState.TESTING,
        TaskState.TESTS_PASSED,
        TaskState.REVIEWING,
        TaskState.REVIEWED,
        TaskState.AWAITING_FINAL_APPROVAL,
    ]:
        sm.transition(state)
    return sm


# --- plan approval / rejection --------------------------------------------


def test_approve_plan_transitions_to_plan_approved(repo):
    sm = _at_awaiting_plan_approval(repo)
    errors = orchestrator.approve_task(sm)
    assert errors == []
    assert sm.state == TaskState.PLAN_APPROVED


def test_reject_plan_requires_reason(repo):
    sm = _at_awaiting_plan_approval(repo)
    errors = orchestrator.reject_task(sm, "")
    assert errors
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL


def test_reject_plan_with_reason_transitions_to_plan_rejected(repo):
    sm = _at_awaiting_plan_approval(repo)
    errors = orchestrator.reject_task(sm, "Plan touches the auth module unnecessarily")
    assert errors == []
    assert sm.state == TaskState.PLAN_REJECTED


def test_plan_rejected_is_terminal_and_releases_lock(repo):
    sm = _at_awaiting_plan_approval(repo, task_id="lockme")
    orchestrator.reject_task(sm, "not aligned with requirements")
    assert locking.get_active(repo / "tasks") is None
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.PLAN_APPROVED)


# --- final approval / rejection -------------------------------------------


def test_approve_final_transitions_to_final_approved(repo):
    sm = _at_awaiting_final_approval(repo)
    errors = orchestrator.approve_task(sm)
    assert errors == []
    assert sm.state == TaskState.FINAL_APPROVED


def test_reject_final_requires_reason(repo):
    sm = _at_awaiting_final_approval(repo)
    errors = orchestrator.reject_task(sm, "   ")
    assert errors
    assert sm.state == TaskState.AWAITING_FINAL_APPROVAL


def test_reject_final_with_reason_transitions_to_final_rejected(repo):
    sm = _at_awaiting_final_approval(repo)
    errors = orchestrator.reject_task(sm, "Diff includes an unreviewed dependency bump")
    assert errors == []
    assert sm.state == TaskState.FINAL_REJECTED


def test_final_rejected_is_terminal_and_releases_lock(repo):
    sm = _at_awaiting_final_approval(repo, task_id="lockme2")
    orchestrator.reject_task(sm, "needs more work")
    assert locking.get_active(repo / "tasks") is None
    with pytest.raises(IllegalTransitionError):
        sm.transition(TaskState.FINAL_APPROVED)


# --- invalid states ---------------------------------------------------


def test_approve_in_invalid_state_returns_error(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    errors = orchestrator.approve_task(sm)
    assert errors
    assert sm.state == TaskState.ANALYZING


def test_reject_in_invalid_state_returns_error(repo):
    sm = _sm(repo)
    sm.transition(TaskState.ANALYZING)
    errors = orchestrator.reject_task(sm, "some reason")
    assert errors
    assert sm.state == TaskState.ANALYZING


def test_approve_after_already_approved_returns_error(repo):
    sm = _at_awaiting_plan_approval(repo)
    orchestrator.approve_task(sm)
    errors = orchestrator.approve_task(sm)
    assert errors
    assert sm.state == TaskState.PLAN_APPROVED

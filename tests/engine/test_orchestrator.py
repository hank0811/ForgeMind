from __future__ import annotations

from forgemind import artifacts, orchestrator
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.base import AgentResultStatus
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine


def _drive_to_testing(sm: TaskStateMachine) -> None:
    for state in [
        TaskState.ANALYZING,
        TaskState.ANALYZED,
        TaskState.DESIGNING,
        TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.PLAN_APPROVED,
        TaskState.IMPLEMENTING,
        TaskState.IMPLEMENTED,
        TaskState.TESTING,
    ]:
        sm.transition(state)


# --- implementation retry counting -------------------------------------


def test_implementation_retry_counting_and_exhaustion(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Fix a bug")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    config = ForgeMindConfig(max_implementation_retries=2, max_revision_retries=2)
    _drive_to_testing(sm)

    sm.transition(TaskState.TESTS_FAILED)
    result = orchestrator.retry_after_test_failure(sm, config)
    assert result == TaskState.IMPLEMENTING
    assert sm.implementation_retry_count == 1

    sm.transition(TaskState.IMPLEMENTED)
    sm.transition(TaskState.TESTING)
    sm.transition(TaskState.TESTS_FAILED)
    result = orchestrator.retry_after_test_failure(sm, config)
    assert result == TaskState.IMPLEMENTING
    assert sm.implementation_retry_count == 2

    sm.transition(TaskState.IMPLEMENTED)
    sm.transition(TaskState.TESTING)
    sm.transition(TaskState.TESTS_FAILED)
    result = orchestrator.retry_after_test_failure(sm, config)
    assert result == TaskState.FAILED
    assert sm.state == TaskState.FAILED
    assert sm.implementation_retry_count == 2  # not incremented on the exhausting call


# --- revision retry counting --------------------------------------------


def test_revision_retry_counting_and_exhaustion(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    config = ForgeMindConfig(max_implementation_retries=2, max_revision_retries=1)
    _drive_to_testing(sm)
    sm.transition(TaskState.TESTS_PASSED)
    sm.transition(TaskState.REVIEWING)

    result = orchestrator.retry_after_review_revision(sm, config)
    assert result == TaskState.IMPLEMENTING
    assert sm.revision_retry_count == 1

    sm.transition(TaskState.IMPLEMENTED)
    sm.transition(TaskState.TESTING)
    sm.transition(TaskState.TESTS_PASSED)
    sm.transition(TaskState.REVIEWING)
    result = orchestrator.retry_after_review_revision(sm, config)
    assert result == TaskState.FAILED
    assert sm.state == TaskState.FAILED


def test_implementation_and_revision_retries_are_independent_counters(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    config = ForgeMindConfig(max_implementation_retries=1, max_revision_retries=1)
    _drive_to_testing(sm)
    sm.transition(TaskState.TESTS_FAILED)
    orchestrator.retry_after_test_failure(sm, config)  # implementation_retry_count -> 1
    sm.transition(TaskState.IMPLEMENTED)
    sm.transition(TaskState.TESTING)
    sm.transition(TaskState.TESTS_PASSED)
    sm.transition(TaskState.REVIEWING)

    # Revision retries should still be available even though implementation retries are exhausted.
    result = orchestrator.retry_after_review_revision(sm, config)
    assert result == TaskState.IMPLEMENTING
    assert sm.implementation_retry_count == 1
    assert sm.revision_retry_count == 1


# --- full ManualRunner stage cycle via the orchestrator -----------------


def test_manual_runner_full_stage_cycle(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a health check endpoint")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = ManualRunner(repo / "tasks")

    result = orchestrator.start_stage(sm, "analyst", runner, input_artifacts=[])
    assert result.status == AgentResultStatus.PENDING
    assert sm.state == TaskState.BLOCKED
    pending_path = repo / "tasks" / task.task_id / "pending_prompt.md"
    assert pending_path.exists()

    # Human runs the stage manually and produces the artifact.
    output_path = repo / "artifacts" / task.task_id / "01_analysis.md"
    artifacts.write_markdown(output_path, {"status": "ok", "task_id": task.task_id}, "Analysis body.")
    complete_result = runner.complete_pending(task.task_id, output_path)
    assert complete_result.status == AgentResultStatus.OK
    assert not pending_path.exists()

    orchestrator.resume_blocked_stage(sm)
    assert sm.state == TaskState.ANALYZING

    errors = orchestrator.complete_stage(
        sm, "analyst", output_path, required_fields=["status", "task_id"]
    )
    assert errors == []
    assert sm.state == TaskState.ANALYZED


def test_complete_stage_rejects_invalid_artifact_without_transitioning(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Fix a bug")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    sm.transition(TaskState.ANALYZING)

    bad_path = repo / "artifacts" / task.task_id / "01_analysis.md"
    artifacts.write_markdown(bad_path, {"task_id": task.task_id}, "")  # missing 'status', empty body

    errors = orchestrator.complete_stage(
        sm, "analyst", bad_path, required_fields=["status", "task_id"]
    )
    assert errors
    assert sm.state == TaskState.ANALYZING  # unchanged


def test_complete_stage_supports_explicit_target_state_for_tester(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Fix a bug")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    _drive_to_testing(sm)

    report_path = repo / "artifacts" / task.task_id / "04_test_report.md"
    artifacts.write_markdown(report_path, {"status": "failed"}, "2 tests failed.")

    errors = orchestrator.complete_stage(
        sm,
        "tester",
        report_path,
        required_fields=["status"],
        target_state=TaskState.TESTS_FAILED,
    )
    assert errors == []
    assert sm.state == TaskState.TESTS_FAILED

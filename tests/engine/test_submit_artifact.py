from __future__ import annotations

from pathlib import Path

from forgemind import artifacts, orchestrator
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine

CONFIG = ForgeMindConfig(max_implementation_retries=2, max_revision_retries=2)


def _blocked_on_analyst(repo: Path):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = ManualRunner(repo / "tasks")
    orchestrator.start_stage(sm, "analyst", runner, input_artifacts=[])
    assert sm.state == TaskState.BLOCKED
    return task, sm, runner


def _write_scratch_artifact(repo: Path, name: str, frontmatter: dict, body: str) -> Path:
    path = repo / "scratch" / name
    artifacts.write_markdown(path, frontmatter, body)
    return path


# --- valid / invalid submission ------------------------------------------


def test_submit_valid_artifact_advances_stage(repo):
    task, sm, runner = _blocked_on_analyst(repo)
    source = _write_scratch_artifact(repo, "analysis.md", {"status": "ok"}, "Findings here.")

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "analyst", source, runner, CONFIG)

    assert errors == []
    assert sm.state == TaskState.ANALYZED
    canonical = repo / "artifacts" / task.task_id / "01_analysis.md"
    assert canonical.exists()


def test_submit_invalid_artifact_returns_errors_and_stays_blocked(repo):
    task, sm, runner = _blocked_on_analyst(repo)
    source = _write_scratch_artifact(repo, "bad.md", {}, "Findings here.")  # missing 'status'

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "analyst", source, runner, CONFIG)

    assert errors
    assert sm.state == TaskState.BLOCKED
    pending_path = repo / "tasks" / task.task_id / "pending_prompt.md"
    assert pending_path.exists()  # untouched -- nothing was consumed


def test_submit_artifact_wrong_stage_rejected(repo):
    task, sm, runner = _blocked_on_analyst(repo)
    source = _write_scratch_artifact(repo, "analysis.md", {"status": "ok"}, "Findings here.")

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "tester", source, runner, CONFIG)

    assert errors
    assert "awaiting a 'analyst'" in errors[0]
    assert sm.state == TaskState.BLOCKED


def test_submit_artifact_when_task_not_expecting_one(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)  # still CREATED, never started
    runner = ManualRunner(repo / "tasks")
    source = _write_scratch_artifact(repo, "analysis.md", {"status": "ok"}, "Findings here.")

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "analyst", source, runner, CONFIG)

    assert errors
    assert "not awaiting a manual submission" in errors[0]
    assert sm.state == TaskState.CREATED


def test_submit_artifact_missing_file_rejected(repo):
    task, sm, runner = _blocked_on_analyst(repo)
    missing = repo / "scratch" / "missing.md"
    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "analyst", missing, runner, CONFIG)
    assert errors
    assert sm.state == TaskState.BLOCKED


def test_submit_artifact_unknown_stage_rejected(repo):
    task, sm, runner = _blocked_on_analyst(repo)
    source = _write_scratch_artifact(repo, "analysis.md", {"status": "ok"}, "Findings here.")
    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "bogus", source, runner, CONFIG)
    assert errors
    assert "unknown stage" in errors[0]


# --- tester / reviewer status-driven branching ----------------------------


def _drive_to_testing_blocked(repo: Path):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Fix a bug")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = ManualRunner(repo / "tasks")

    orchestrator.start_stage(sm, "analyst", runner, input_artifacts=[])
    orchestrator.submit_artifact(
        sm,
        repo / "artifacts",
        "analyst",
        _write_scratch_artifact(repo, "a.md", {"status": "ok"}, "Analysis."),
        runner,
        CONFIG,
    )
    orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    orchestrator.submit_artifact(
        sm,
        repo / "artifacts",
        "architect_planner",
        _write_scratch_artifact(repo, "p.md", {"status": "ok"}, "Read the file and summarize it."),
        runner,
        CONFIG,
    )
    # No governance_config passed above, so the plan checkpoint can't decide
    # on its own and correctly waits at AWAITING_PLAN_APPROVAL for a human.
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL
    orchestrator.approve_task(sm)
    assert sm.state == TaskState.PLAN_APPROVED
    orchestrator.start_stage(sm, "implementer", runner, input_artifacts=[])
    orchestrator.submit_artifact(
        sm,
        repo / "artifacts",
        "implementer",
        _write_scratch_artifact(repo, "i.md", {"status": "ok"}, "Implemented the fix."),
        runner,
        CONFIG,
    )
    orchestrator.start_stage(sm, "tester", runner, input_artifacts=[])
    assert sm.state == TaskState.BLOCKED
    return task, sm, runner


def test_submit_tester_failed_triggers_implementation_retry(repo):
    task, sm, runner = _drive_to_testing_blocked(repo)
    source = _write_scratch_artifact(repo, "t.md", {"status": "failed"}, "2 tests failed.")

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "tester", source, runner, CONFIG)

    assert errors == []
    assert sm.state == TaskState.IMPLEMENTING
    assert sm.implementation_retry_count == 1


def test_submit_tester_passed_advances_to_tests_passed(repo):
    task, sm, runner = _drive_to_testing_blocked(repo)
    source = _write_scratch_artifact(repo, "t.md", {"status": "ok"}, "All tests passed.")

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "tester", source, runner, CONFIG)

    assert errors == []
    assert sm.state == TaskState.TESTS_PASSED


def test_submit_reviewer_changes_requested_triggers_revision_retry(repo):
    task, sm, runner = _drive_to_testing_blocked(repo)
    orchestrator.submit_artifact(
        sm,
        repo / "artifacts",
        "tester",
        _write_scratch_artifact(repo, "t.md", {"status": "ok"}, "All tests passed."),
        runner,
        CONFIG,
    )
    orchestrator.start_stage(sm, "reviewer", runner, input_artifacts=[])
    source = _write_scratch_artifact(
        repo, "r.md", {"status": "changes_requested"}, "Needs a rename."
    )

    errors = orchestrator.submit_artifact(sm, repo / "artifacts", "reviewer", source, runner, CONFIG)

    assert errors == []
    assert sm.state == TaskState.IMPLEMENTING
    assert sm.revision_retry_count == 1

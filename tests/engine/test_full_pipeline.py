"""Phase 8: end-to-end automatic progression through all five core roles,
extended in Phase 9 to include the Finalizer (FINAL_APPROVED -> FINALIZING
-> COMPLETED).

These tests use a small scripted fake AgentRunner instead of mocking
subprocess.run repeatedly -- it exercises the exact same AgentRunner
interface ManualRunner and ClaudeCodeCLIRunner already implement, which is
the whole point of the abstraction: run_full_pipeline() cannot tell the
difference, and neither does this test need to know about Claude CLI
internals to prove the driver's control flow is correct.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from forgemind import artifacts, governance, orchestrator
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner
from forgemind.state_machine import TaskState, TaskStateMachine


class ScriptedRunner(AgentRunner):
    """Returns pre-scripted outcomes per role, one per call, in order."""

    def __init__(self, artifacts_root: Path, script: dict[str, list[dict]]):
        self.artifacts_root = artifacts_root
        self.script = {role: list(entries) for role, entries in script.items()}
        self.calls: list[dict] = []

    def run_agent(
        self,
        role: str,
        task_context: dict,
        input_artifacts: list[Path],
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> AgentResult:
        self.calls.append(
            {
                "role": role,
                "input_artifacts": list(input_artifacts),
                "workspace_path": workspace_path,
                "allowed_capabilities": list(allowed_capabilities),
            }
        )
        entries = self.script.get(role)
        if not entries:
            raise AssertionError(f"ScriptedRunner: no scripted response left for role '{role}'")
        entry = entries.pop(0)
        task_id = task_context["task_id"]
        output_path = self.artifacts_root / task_id / orchestrator.ARTIFACT_FILENAMES[role]
        artifacts.write_markdown(output_path, {"status": entry["status"]}, entry.get("body", "..."))
        return AgentResult(status=AgentResultStatus.OK, output_artifact_path=output_path, runner_name="scripted")


@pytest.fixture
def gov_config() -> governance.GovernanceConfig:
    return governance.GovernanceConfig(
        patterns=[
            governance.SensitivePattern(name="git_push", regex=r"git\s+push", reason="pushes commits")
        ],
        approval_required_stages=["plan", "final"],
    )


def _new_task(repo: Path, request: str = "Add a feature") -> TaskStateMachine:
    task = task_module.create_task(repo / "tasks", repo / "artifacts", request)
    return TaskStateMachine.load(repo / "tasks", task.task_id)


HAPPY_SCRIPT = {
    "analyst": [{"status": "ok", "body": "Found the relevant files."}],
    "architect_planner": [{"status": "ok", "body": "Add a health check route."}],
    "implementer": [{"status": "ok", "body": "Added the route."}],
    "tester": [{"status": "ok", "body": "Ran pytest: 3 passed."}],
    "reviewer": [{"status": "ok", "body": "Matches the plan."}],
    "finalizer": [{"status": "ok", "body": "Summary of the completed work."}],
}


# --- successful automatic progression ------------------------------------


def test_full_pipeline_happy_path_reaches_completed(repo, gov_config):
    sm = _new_task(repo)
    runner = ScriptedRunner(repo / "artifacts", HAPPY_SCRIPT)
    config = ForgeMindConfig()

    outcome = orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", repo / "workspace" / "demo", config, gov_config
    )

    assert outcome.stopped_state == TaskState.COMPLETED
    assert outcome.reason == "terminal"
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer"
    ]
    # The workspace was actually threaded through every stage that needs it.
    for call in runner.calls:
        assert call["workspace_path"] == repo / "workspace" / "demo"


def test_full_pipeline_passes_prior_artifacts_generically_to_each_stage(repo, gov_config):
    sm = _new_task(repo)
    runner = ScriptedRunner(repo / "artifacts", HAPPY_SCRIPT)
    config = ForgeMindConfig()

    orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    calls_by_role = {c["role"]: c for c in runner.calls}
    assert calls_by_role["analyst"]["input_artifacts"] == []
    assert calls_by_role["architect_planner"]["input_artifacts"] == [
        repo / "artifacts" / sm.task_id / "01_analysis.md"
    ]
    assert calls_by_role["implementer"]["input_artifacts"] == [
        repo / "artifacts" / sm.task_id / "01_analysis.md",
        repo / "artifacts" / sm.task_id / "02_design_plan.md",
    ]
    assert calls_by_role["tester"]["input_artifacts"] == [
        repo / "artifacts" / sm.task_id / "01_analysis.md",
        repo / "artifacts" / sm.task_id / "02_design_plan.md",
        repo / "artifacts" / sm.task_id / "03_implementation.md",
    ]
    assert calls_by_role["reviewer"]["input_artifacts"] == [
        repo / "artifacts" / sm.task_id / "01_analysis.md",
        repo / "artifacts" / sm.task_id / "02_design_plan.md",
        repo / "artifacts" / sm.task_id / "03_implementation.md",
        repo / "artifacts" / sm.task_id / "04_test_report.md",
    ]
    assert calls_by_role["finalizer"]["input_artifacts"] == [
        repo / "artifacts" / sm.task_id / "01_analysis.md",
        repo / "artifacts" / sm.task_id / "02_design_plan.md",
        repo / "artifacts" / sm.task_id / "03_implementation.md",
        repo / "artifacts" / sm.task_id / "04_test_report.md",
        repo / "artifacts" / sm.task_id / "05_review.md",
    ]


# --- governance checkpoints stop the pipeline cleanly ---------------------


def test_full_pipeline_stops_at_awaiting_plan_approval_when_plan_sensitive(repo, gov_config):
    sm = _new_task(repo)
    script = dict(HAPPY_SCRIPT)
    script["architect_planner"] = [{"status": "ok", "body": "Step 2: git push the branch."}]
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig()

    outcome = orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", None, config, gov_config
    )

    assert outcome.stopped_state == TaskState.AWAITING_PLAN_APPROVAL
    assert outcome.reason == "awaiting_human_action"
    # Implementer must never run before a human approves a sensitive plan.
    assert [c["role"] for c in runner.calls] == ["analyst", "architect_planner"]


def test_full_pipeline_resumes_after_manual_plan_approval(repo, gov_config):
    sm = _new_task(repo)
    script = dict(HAPPY_SCRIPT)
    script["architect_planner"] = [{"status": "ok", "body": "Step 2: git push the branch."}]
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig()

    first = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)
    assert first.stopped_state == TaskState.AWAITING_PLAN_APPROVAL

    approve_errors = orchestrator.approve_task(sm)
    assert approve_errors == []

    second = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    assert second.stopped_state == TaskState.COMPLETED
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer"
    ]


def test_full_pipeline_stops_at_awaiting_final_approval_when_implementation_sensitive(repo, gov_config):
    sm = _new_task(repo)
    script = dict(HAPPY_SCRIPT)
    script["implementer"] = [{"status": "ok", "body": "Ran git push to publish the branch."}]
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig()

    outcome = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    assert outcome.stopped_state == TaskState.AWAITING_FINAL_APPROVAL
    assert outcome.reason == "awaiting_human_action"
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner", "implementer", "tester", "reviewer"
    ]


# --- existing retry/revision paths, reused unmodified ---------------------


def test_full_pipeline_retries_test_failure_then_passes(repo, gov_config):
    sm = _new_task(repo)
    script = {
        "analyst": [{"status": "ok"}],
        "architect_planner": [{"status": "ok", "body": "Add a route."}],
        "implementer": [{"status": "ok"}, {"status": "ok"}],
        "tester": [{"status": "failed", "body": "1 test failed."}, {"status": "ok"}],
        "reviewer": [{"status": "ok"}],
        "finalizer": [{"status": "ok"}],
    }
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig(max_implementation_retries=2)

    outcome = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    assert outcome.stopped_state == TaskState.COMPLETED
    assert sm.implementation_retry_count == 1
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner",
        "implementer", "tester",  # first attempt: fails
        "implementer", "tester",  # retry: passes
        "reviewer", "finalizer",
    ]


def test_full_pipeline_exhausts_test_retries_and_fails(repo, gov_config):
    sm = _new_task(repo)
    script = {
        "analyst": [{"status": "ok"}],
        "architect_planner": [{"status": "ok"}],
        "implementer": [{"status": "ok"}, {"status": "ok"}, {"status": "ok"}],
        "tester": [{"status": "failed"}, {"status": "failed"}, {"status": "failed"}],
        "reviewer": [],
    }
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig(max_implementation_retries=2)

    outcome = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    assert outcome.stopped_state == TaskState.FAILED
    assert outcome.reason == "terminal"
    assert sm.implementation_retry_count == 2
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner",
        "implementer", "tester",
        "implementer", "tester",
        "implementer", "tester",
    ]
    assert "reviewer" not in [c["role"] for c in runner.calls]


def test_full_pipeline_reviewer_changes_requested_then_approved(repo, gov_config):
    sm = _new_task(repo)
    script = {
        "analyst": [{"status": "ok"}],
        "architect_planner": [{"status": "ok"}],
        "implementer": [{"status": "ok"}, {"status": "ok"}],
        "tester": [{"status": "ok"}, {"status": "ok"}],
        "reviewer": [{"status": "changes_requested", "body": "Missing error handling."}, {"status": "ok"}],
        "finalizer": [{"status": "ok"}],
    }
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig(max_revision_retries=2)

    outcome = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, config, gov_config)

    assert outcome.stopped_state == TaskState.COMPLETED
    assert sm.revision_retry_count == 1
    assert [c["role"] for c in runner.calls] == [
        "analyst", "architect_planner",
        "implementer", "tester", "reviewer",  # first pass: changes requested
        "implementer", "tester", "reviewer",  # revision: approved
        "finalizer",
    ]


# --- failure handling ------------------------------------------------------


def test_full_pipeline_stops_cleanly_on_runner_error(repo, gov_config):
    class FailingRunner(AgentRunner):
        def run_agent(self, role, task_context, input_artifacts, workspace_path, allowed_capabilities):
            return AgentResult(status=AgentResultStatus.ERROR, runner_name="failing", notes="boom")

    sm = _new_task(repo)
    outcome = orchestrator.run_full_pipeline(
        sm, FailingRunner(), repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.FAILED
    assert outcome.reason == "terminal"


def test_full_pipeline_stops_on_invalid_artifact_content(repo, gov_config):
    """A runner that reports OK but writes an artifact missing the
    required 'status' field -- the driver must not crash or loop, and must
    not silently treat it as success."""

    class BadArtifactRunner(AgentRunner):
        def run_agent(self, role, task_context, input_artifacts, workspace_path, allowed_capabilities):
            task_id = task_context["task_id"]
            output_path = repo / "artifacts" / task_id / orchestrator.ARTIFACT_FILENAMES[role]
            artifacts.write_markdown(output_path, {}, "No status field.")
            return AgentResult(status=AgentResultStatus.OK, output_artifact_path=output_path, runner_name="bad")

    sm = _new_task(repo)
    outcome = orchestrator.run_full_pipeline(
        sm, BadArtifactRunner(), repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.FAILED
    assert outcome.reason == "terminal"
    assert "validation" in sm.history[-1]["reason"]


def test_full_pipeline_stops_when_blocked_by_pending_manual_submission(repo, gov_config):
    """A PENDING-returning runner (like ManualRunner) must stop the driver
    at BLOCKED rather than looping or crashing."""

    class PendingOnceRunner(AgentRunner):
        def run_agent(self, role, task_context, input_artifacts, workspace_path, allowed_capabilities):
            return AgentResult(status=AgentResultStatus.PENDING, runner_name="pending")

    sm = _new_task(repo)
    outcome = orchestrator.run_full_pipeline(
        sm, PendingOnceRunner(), repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.BLOCKED
    assert outcome.reason == "awaiting_human_action"


# --- no invalid auto-transition / no infinite loop ------------------------


def test_full_pipeline_never_exceeds_max_iterations_and_does_not_hang(repo, gov_config):
    sm = _new_task(repo)
    for state in [
        TaskState.ANALYZING, TaskState.ANALYZED,
        TaskState.DESIGNING, TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL, TaskState.PLAN_APPROVED,
    ]:
        sm.transition(state)

    script = {
        "implementer": [{"status": "ok"}] * 10,
        "tester": [{"status": "failed"}] * 10,
    }
    runner = ScriptedRunner(repo / "artifacts", script)
    config = ForgeMindConfig(max_implementation_retries=10)  # would need >>3 iterations to fail naturally

    outcome = orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", None, config, gov_config, max_iterations=3
    )

    assert outcome.reason == "max_iterations_reached"
    assert outcome.iterations == 3
    assert sm.state != TaskState.FAILED  # safety net tripped before the real retry limit did
    assert sm.state not in (TaskState.COMPLETED, TaskState.CANCELLED)


# --- Phase 9: finalization, resume, and idempotency ----------------------


def test_full_pipeline_continues_from_final_approved(repo, gov_config):
    """A task that already reached FINAL_APPROVED in a prior run (e.g. a
    human approving sensitive final content) must have run_full_pipeline
    invoke the Finalizer and reach COMPLETED on the very next call."""
    sm = _new_task(repo)
    for state in [
        TaskState.ANALYZING, TaskState.ANALYZED,
        TaskState.DESIGNING, TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL, TaskState.PLAN_APPROVED,
        TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
        TaskState.TESTING, TaskState.TESTS_PASSED,
        TaskState.REVIEWING, TaskState.REVIEWED,
        TaskState.AWAITING_FINAL_APPROVAL, TaskState.FINAL_APPROVED,
    ]:
        sm.transition(state)

    runner = ScriptedRunner(repo / "artifacts", {"finalizer": [{"status": "ok", "body": "Done."}]})
    outcome = orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.COMPLETED
    assert outcome.reason == "terminal"
    assert [c["role"] for c in runner.calls] == ["finalizer"]


def test_full_pipeline_rerun_on_completed_task_is_a_safe_noop(repo, gov_config):
    sm = _new_task(repo)
    runner = ScriptedRunner(repo / "artifacts", HAPPY_SCRIPT)
    orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config)
    assert sm.state == TaskState.COMPLETED

    outcome = orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.COMPLETED
    assert outcome.reason == "terminal"
    assert outcome.iterations == 0
    # No further runner calls were made -- COMPLETED is checked before any dispatch.
    assert len(runner.calls) == 6


def test_full_pipeline_finalizer_pending_then_resumed_reaches_completed(repo, gov_config):
    """Exercises the exact bug Phase 9 found and fixed: FINALIZING must be
    able to reach BLOCKED (a PENDING finalizer result) and, once something
    outside this driver resumes it back to FINALIZING, a fresh call to
    run_full_pipeline must re-invoke the finalizer rather than raising an
    illegal-transition error or getting stuck."""

    class OnceThenOkRunner(AgentRunner):
        def __init__(self, artifacts_root: Path):
            self.artifacts_root = artifacts_root
            self.pending_used = False
            self.calls: list[str] = []

        def run_agent(self, role, task_context, input_artifacts, workspace_path, allowed_capabilities):
            self.calls.append(role)
            if role == "finalizer" and not self.pending_used:
                self.pending_used = True
                return AgentResult(status=AgentResultStatus.PENDING, runner_name="once")
            task_id = task_context["task_id"]
            output_path = self.artifacts_root / task_id / orchestrator.ARTIFACT_FILENAMES[role]
            artifacts.write_markdown(output_path, {"status": "ok"}, "...")
            return AgentResult(status=AgentResultStatus.OK, output_artifact_path=output_path, runner_name="once")

    sm = _new_task(repo)
    for state in [
        TaskState.ANALYZING, TaskState.ANALYZED,
        TaskState.DESIGNING, TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL, TaskState.PLAN_APPROVED,
        TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
        TaskState.TESTING, TaskState.TESTS_PASSED,
        TaskState.REVIEWING, TaskState.REVIEWED,
        TaskState.AWAITING_FINAL_APPROVAL, TaskState.FINAL_APPROVED,
    ]:
        sm.transition(state)

    runner = OnceThenOkRunner(repo / "artifacts")

    first = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config)
    assert first.stopped_state == TaskState.BLOCKED
    assert first.reason == "awaiting_human_action"
    assert sm.state == TaskState.BLOCKED
    assert sm.blocked_from == TaskState.FINALIZING

    # Something outside this driver (e.g. a human via `forgemind
    # submit-artifact`) resolves the pending manual step and resumes.
    orchestrator.resume_blocked_stage(sm)
    assert sm.state == TaskState.FINALIZING

    second = orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config)

    assert second.stopped_state == TaskState.COMPLETED
    assert runner.calls == ["finalizer", "finalizer"]


def test_full_pipeline_stops_on_unrecognized_midflight_state(repo, gov_config):
    """If the task is left in a state this driver has no defined action
    for (e.g. DESIGNING, observed mid-flight rather than via a retry
    loop-back), it must stop cleanly rather than guess a transition."""
    sm = _new_task(repo)
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.ANALYZED)
    sm.transition(TaskState.DESIGNING)

    outcome = orchestrator.run_full_pipeline(
        sm, ScriptedRunner(repo / "artifacts", {}), repo / "artifacts", None, ForgeMindConfig(), gov_config
    )

    assert outcome.stopped_state == TaskState.DESIGNING
    assert "no automatic action defined" in outcome.reason
    assert sm.state == TaskState.DESIGNING  # unchanged -- no invalid transition was taken

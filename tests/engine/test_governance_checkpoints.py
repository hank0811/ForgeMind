from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgemind import artifacts, governance, orchestrator
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine

CONFIG = ForgeMindConfig()


@pytest.fixture
def gov_config(tmp_path: Path) -> governance.GovernanceConfig:
    data = {
        "sensitive_patterns": [
            {"name": "git_push", "regex": r"git\s+push", "reason": "Pushes commits to a remote"},
        ],
        "approval_required_stages": ["plan", "final"],
    }
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return governance.load_governance(path)


def _sm_at_designed(repo: Path) -> TaskStateMachine:
    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    for state in [TaskState.ANALYZING, TaskState.ANALYZED, TaskState.DESIGNING, TaskState.DESIGNED]:
        sm.transition(state)
    return sm


def _sm_at_reviewed(repo: Path) -> TaskStateMachine:
    sm = _sm_at_designed(repo)
    for state in [
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.PLAN_APPROVED,
        TaskState.IMPLEMENTING,
        TaskState.IMPLEMENTED,
        TaskState.TESTING,
        TaskState.TESTS_PASSED,
        TaskState.REVIEWING,
        TaskState.REVIEWED,
    ]:
        sm.transition(state)
    return sm


# --- direct checkpoint tests -----------------------------------------


def test_plan_checkpoint_auto_approves_when_not_sensitive(repo, gov_config):
    sm = _sm_at_designed(repo)
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    artifacts.write_markdown(plan_path, {"status": "ok"}, "Read the config and update a comment.")

    result = orchestrator.apply_plan_governance_checkpoint(sm, plan_path, gov_config)

    assert result.sensitive is False
    assert sm.state == TaskState.PLAN_APPROVED


def test_plan_checkpoint_waits_for_human_when_sensitive(repo, gov_config):
    sm = _sm_at_designed(repo)
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    artifacts.write_markdown(plan_path, {"status": "ok"}, "Step 3: git push the release branch.")

    result = orchestrator.apply_plan_governance_checkpoint(sm, plan_path, gov_config)

    assert result.sensitive is True
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL


def test_plan_checkpoint_without_governance_config_waits_for_human(repo):
    sm = _sm_at_designed(repo)
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    artifacts.write_markdown(plan_path, {"status": "ok"}, "Nothing sensitive here.")

    result = orchestrator.apply_plan_governance_checkpoint(sm, plan_path, None)

    assert result is None
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL


def test_final_checkpoint_auto_approves_when_not_sensitive(repo, gov_config):
    sm = _sm_at_reviewed(repo)
    impl_path = repo / "artifacts" / "t1" / "03_implementation.md"
    artifacts.write_markdown(impl_path, {"status": "ok"}, "Renamed a variable.")

    result = orchestrator.apply_final_governance_checkpoint(sm, impl_path, gov_config)

    assert result.sensitive is False
    assert sm.state == TaskState.FINAL_APPROVED


def test_final_checkpoint_waits_for_human_when_sensitive(repo, gov_config):
    sm = _sm_at_reviewed(repo)
    impl_path = repo / "artifacts" / "t1" / "03_implementation.md"
    artifacts.write_markdown(impl_path, {"status": "ok"}, "Ran git push to sync the branch.")

    result = orchestrator.apply_final_governance_checkpoint(sm, impl_path, gov_config)

    assert result.sensitive is True
    assert sm.state == TaskState.AWAITING_FINAL_APPROVAL


# --- full submit_artifact integration, both governance paths --------------


def _blocked_on_architect_planner(repo: Path):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = ManualRunner(repo / "tasks")
    orchestrator.start_stage(sm, "analyst", runner, input_artifacts=[])
    orchestrator.submit_artifact(
        sm,
        repo / "artifacts",
        "analyst",
        _scratch(repo, "a.md", {"status": "ok"}, "Analysis."),
        runner,
        CONFIG,
    )
    orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    return task, sm, runner


def _scratch(repo: Path, name: str, frontmatter: dict, body: str) -> Path:
    path = repo / "scratch" / name
    artifacts.write_markdown(path, frontmatter, body)
    return path


def test_submit_nonsensitive_plan_auto_approves_end_to_end(repo, gov_config):
    task, sm, runner = _blocked_on_architect_planner(repo)
    source = _scratch(repo, "plan.md", {"status": "ok"}, "Add a health check route and a test.")

    errors = orchestrator.submit_artifact(
        sm, repo / "artifacts", "architect_planner", source, runner, CONFIG, gov_config
    )

    assert errors == []
    assert sm.state == TaskState.PLAN_APPROVED  # non-gated path: no approve_task() call needed


def test_submit_sensitive_plan_requires_explicit_approval(repo, gov_config):
    task, sm, runner = _blocked_on_architect_planner(repo)
    source = _scratch(
        repo, "plan.md", {"status": "ok"}, "Step 2: git push the branch to trigger deploy."
    )

    errors = orchestrator.submit_artifact(
        sm, repo / "artifacts", "architect_planner", source, runner, CONFIG, gov_config
    )

    assert errors == []
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL  # gated: waiting on a human

    approve_errors = orchestrator.approve_task(sm)
    assert approve_errors == []
    assert sm.state == TaskState.PLAN_APPROVED

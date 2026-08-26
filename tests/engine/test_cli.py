from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from forgemind import artifacts, cli, orchestrator
from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine

_FORGEMIND_YAML = "runner: manual\nretry_limits:\n  max_implementation_retries: 2\n  max_revision_retries: 2\n"
_GOVERNANCE_YAML = (
    "sensitive_patterns:\n"
    "  - name: git_push\n"
    "    regex: \"git\\\\s+push\"\n"
    "    reason: \"Pushes commits to a remote\"\n"
    "approval_required_stages:\n"
    "  - plan\n"
    "  - final\n"
)


def _init_fake_repo(tmp_path: Path) -> None:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "artifacts").mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "forgemind.yaml").write_text(_FORGEMIND_YAML, encoding="utf-8")
    (config_dir / "governance.yaml").write_text(_GOVERNANCE_YAML, encoding="utf-8")


def test_cli_new_and_status(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)

    exit_code = cli.main(["new", "Add a login page"])
    assert exit_code == 0
    task_id = capsys.readouterr().out.strip()
    assert task_id

    exit_code = cli.main(["status", task_id])
    assert exit_code == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["task_id"] == task_id
    assert data["state"] == "CREATED"


def test_cli_status_unknown_task_returns_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)

    exit_code = cli.main(["status", "does-not-exist"])
    assert exit_code == 1
    assert "no such task" in capsys.readouterr().err


def test_cli_new_twice_without_terminal_transition_fails(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)

    assert cli.main(["new", "First request"]) == 0
    capsys.readouterr()
    exit_code = cli.main(["new", "Second request"])
    assert exit_code == 1
    assert "already active" in capsys.readouterr().err


# --- Phase 2: submit-artifact / approve / reject --------------------------


def _new_task_blocked_on_analyst(tmp_path: Path) -> str:
    """Create a task and drive it to BLOCKED-on-analyst directly through the
    engine (there is no CLI command yet to start a stage -- that remains
    part of the not-yet-built pipeline automation)."""
    from forgemind import task as task_module

    task = task_module.create_task(tmp_path / "tasks", tmp_path / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(tmp_path / "tasks", task.task_id)
    runner = ManualRunner(tmp_path / "tasks")
    orchestrator.start_stage(sm, "analyst", runner, input_artifacts=[])
    assert sm.state == TaskState.BLOCKED
    return task.task_id


def test_cli_submit_artifact_unknown_task_rejected(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)

    exit_code = cli.main(["submit-artifact", "does-not-exist", "analyst", "somefile.md"])
    assert exit_code == 1
    assert "no such task" in capsys.readouterr().err


def test_cli_submit_artifact_valid(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)

    source = tmp_path / "scratch" / "analysis.md"
    artifacts.write_markdown(source, {"status": "ok"}, "Findings here.")

    exit_code = cli.main(["submit-artifact", task_id, "analyst", str(source)])
    assert exit_code == 0
    assert "submitted" in capsys.readouterr().out

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.ANALYZED


def test_cli_submit_artifact_invalid_reports_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)

    source = tmp_path / "scratch" / "bad.md"
    artifacts.write_markdown(source, {}, "Findings here.")  # missing 'status'

    exit_code = cli.main(["submit-artifact", task_id, "analyst", str(source)])
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_cli_submit_artifact_wrong_stage_choice_rejected_by_argparse(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)

    import pytest

    with pytest.raises(SystemExit):
        cli.main(["submit-artifact", task_id, "not-a-real-stage", "somefile.md"])


def test_cli_approve_and_reject_require_awaiting_approval(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)  # currently BLOCKED, not awaiting approval

    exit_code = cli.main(["approve", task_id])
    assert exit_code == 1
    assert "not awaiting approval" in capsys.readouterr().err

    exit_code = cli.main(["reject", task_id, "--reason", "no"])
    assert exit_code == 1
    assert "not awaiting approval" in capsys.readouterr().err


def test_cli_approve_plan_end_to_end(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)

    source = tmp_path / "scratch" / "analysis.md"
    artifacts.write_markdown(source, {"status": "ok"}, "Findings here.")
    assert cli.main(["submit-artifact", task_id, "analyst", str(source)]) == 0
    capsys.readouterr()

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    runner = ManualRunner(tmp_path / "tasks")
    orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    plan_source = tmp_path / "scratch" / "plan.md"
    # Sensitive content so the gate actually waits for an explicit approval.
    artifacts.write_markdown(plan_source, {"status": "ok"}, "Step 2: git push the branch.")
    assert cli.main(["submit-artifact", task_id, "architect_planner", str(plan_source)]) == 0
    capsys.readouterr()

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL

    exit_code = cli.main(["approve", task_id])
    assert exit_code == 0
    assert "approved" in capsys.readouterr().out
    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.PLAN_APPROVED


def test_cli_reject_plan_end_to_end(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_blocked_on_analyst(tmp_path)

    source = tmp_path / "scratch" / "analysis.md"
    artifacts.write_markdown(source, {"status": "ok"}, "Findings here.")
    assert cli.main(["submit-artifact", task_id, "analyst", str(source)]) == 0
    capsys.readouterr()

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    runner = ManualRunner(tmp_path / "tasks")
    orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    plan_source = tmp_path / "scratch" / "plan.md"
    artifacts.write_markdown(plan_source, {"status": "ok"}, "Step 2: git push the branch.")
    assert cli.main(["submit-artifact", task_id, "architect_planner", str(plan_source)]) == 0
    capsys.readouterr()

    exit_code = cli.main(["reject", task_id, "--reason", "Do not push automatically"])
    assert exit_code == 0
    assert "rejected" in capsys.readouterr().out
    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.PLAN_REJECTED


# --- Phase 10: `forgemind run` ---------------------------------------------


class _ScriptedFakeRunner(AgentRunner):
    """Minimal fake AgentRunner for `run` tests: writes a scripted artifact
    per role and returns OK, exactly matching what a real runner reports --
    no subprocess, no Claude call required for these unit tests."""

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
        self.calls.append({"role": role, "workspace_path": workspace_path})
        entries = self.script.get(role)
        if not entries:
            raise AssertionError(f"_ScriptedFakeRunner: no scripted response left for role '{role}'")
        entry = entries.pop(0)
        task_id = task_context["task_id"]
        output_path = self.artifacts_root / task_id / orchestrator.ARTIFACT_FILENAMES[role]
        artifacts.write_markdown(output_path, {"status": entry["status"]}, entry.get("body", "..."))
        return AgentResult(status=AgentResultStatus.OK, output_artifact_path=output_path, runner_name="fake")


_FULL_HAPPY_SCRIPT = {
    "analyst": [{"status": "ok", "body": "Found the relevant files."}],
    "architect_planner": [{"status": "ok", "body": "Add a health check route."}],
    "implementer": [{"status": "ok", "body": "Added the route."}],
    "tester": [{"status": "ok", "body": "Ran pytest: 3 passed."}],
    "reviewer": [{"status": "ok", "body": "Matches the plan."}],
    "finalizer": [{"status": "ok", "body": "Summary of the completed work."}],
}


def _new_task_via_cli(tmp_path: Path, capsys, request: str = "Add a feature") -> str:
    assert cli.main(["new", request]) == 0
    task_id = capsys.readouterr().out.strip()
    assert task_id
    return task_id


def test_cli_run_unknown_task_rejected(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)

    exit_code = cli.main(["run", "does-not-exist"])
    assert exit_code == 1
    assert "no such task" in capsys.readouterr().err


def test_cli_run_unknown_runner_in_config_reports_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    (tmp_path / "config" / "forgemind.yaml").write_text("runner: bogus\n", encoding="utf-8")
    task_id = _new_task_via_cli(tmp_path, capsys)

    exit_code = cli.main(["run", task_id])
    assert exit_code == 1
    assert "unknown runner" in capsys.readouterr().err


def test_cli_run_reaches_pipeline_driver_with_manual_runner(tmp_path: Path, monkeypatch, capsys):
    """Default config (runner: manual) -- proves `run` really reaches
    run_full_pipeline() with zero mocking: ManualRunner immediately returns
    PENDING for the analyst stage, so the driver correctly stops at
    BLOCKED. That's the real, existing, safe behavior -- not a crash."""
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    exit_code = cli.main(["run", task_id])

    assert exit_code == 0  # awaiting_human_action is not a CLI failure
    output = json.loads(capsys.readouterr().out)
    assert output["task_id"] == task_id
    assert output["stopped_state"] == "BLOCKED"
    assert output["reason"] == "awaiting_human_action"

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.BLOCKED


def test_cli_run_threads_workspace_argument_through(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    fake_runner = _ScriptedFakeRunner(tmp_path / "artifacts", dict(_FULL_HAPPY_SCRIPT))
    monkeypatch.setattr(cli, "_build_runner", lambda *a, **k: fake_runner)

    workspace = tmp_path / "my-project"
    exit_code = cli.main(["run", task_id, "--workspace", str(workspace)])

    assert exit_code == 0
    assert fake_runner.calls[0]["workspace_path"] == workspace


def test_cli_run_without_workspace_flag_passes_none(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    fake_runner = _ScriptedFakeRunner(tmp_path / "artifacts", dict(_FULL_HAPPY_SCRIPT))
    monkeypatch.setattr(cli, "_build_runner", lambda *a, **k: fake_runner)

    cli.main(["run", task_id])

    assert fake_runner.calls[0]["workspace_path"] is None


def test_cli_run_completes_full_pipeline_with_scripted_runner(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    fake_runner = _ScriptedFakeRunner(tmp_path / "artifacts", dict(_FULL_HAPPY_SCRIPT))
    monkeypatch.setattr(cli, "_build_runner", lambda *a, **k: fake_runner)

    exit_code = cli.main(["run", task_id])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["stopped_state"] == "COMPLETED"
    assert output["reason"] == "terminal"
    assert [c["role"] for c in fake_runner.calls] == [
        "analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer"
    ]

    sm = TaskStateMachine.load(tmp_path / "tasks", task_id)
    assert sm.state == TaskState.COMPLETED


def test_cli_run_on_already_completed_task_is_a_safe_noop(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    fake_runner = _ScriptedFakeRunner(tmp_path / "artifacts", dict(_FULL_HAPPY_SCRIPT))
    monkeypatch.setattr(cli, "_build_runner", lambda *a, **k: fake_runner)
    assert cli.main(["run", task_id]) == 0
    capsys.readouterr()

    exit_code = cli.main(["run", task_id])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["stopped_state"] == "COMPLETED"
    assert output["iterations"] == 0
    # No further runner calls beyond the original six -- idempotency,
    # reused unmodified from Phase 9, not restarting completed work.
    assert len(fake_runner.calls) == 6


def test_cli_run_stops_at_awaiting_plan_approval_and_resumes_after_approve(
    tmp_path: Path, monkeypatch, capsys
):
    """Proves governance is not bypassed by the CLI entry point: a
    sensitive plan must stop at AWAITING_PLAN_APPROVAL, and only an
    explicit `forgemind approve` (existing Phase 2 behavior) lets a
    second `run` continue."""
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    _init_fake_repo(tmp_path)
    task_id = _new_task_via_cli(tmp_path, capsys)

    script = dict(_FULL_HAPPY_SCRIPT)
    script["architect_planner"] = [{"status": "ok", "body": "Step 2: git push the branch."}]
    fake_runner = _ScriptedFakeRunner(tmp_path / "artifacts", script)
    monkeypatch.setattr(cli, "_build_runner", lambda *a, **k: fake_runner)

    exit_code = cli.main(["run", task_id])
    assert exit_code == 0  # awaiting a human is not a CLI failure
    output = json.loads(capsys.readouterr().out)
    assert output["stopped_state"] == "AWAITING_PLAN_APPROVAL"
    assert [c["role"] for c in fake_runner.calls] == ["analyst", "architect_planner"]

    assert cli.main(["approve", task_id]) == 0
    capsys.readouterr()

    exit_code = cli.main(["run", task_id])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["stopped_state"] == "COMPLETED"
    assert [c["role"] for c in fake_runner.calls] == [
        "analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer"
    ]

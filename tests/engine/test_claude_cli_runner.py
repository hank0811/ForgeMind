"""ClaudeCodeCLIRunner tests.

None of these require Claude Code to be installed, authenticated, or
funded -- the actual `claude` process is always mocked or stood in for by
a real, harmless local script. Most tests mock `_launch` (the module-level
subprocess wrapper) to prove the runner constructs a bounded,
non-interactive command and translates process outcomes into AgentResult
correctly. A few (search "process-tree" below) deliberately run real OS
processes to prove the timeout/kill behavior itself, since that can't be
proven by mocking the thing it's testing.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from forgemind import artifacts, orchestrator
from forgemind.runners.base import AgentResultStatus
from forgemind.runners.claude_cli_runner import ClaudeCodeCLIRunner
from forgemind.state_machine import TaskState, TaskStateMachine


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    (directory / "analyst.md").write_text(
        "You are the Analyst. Write status: ok to the given path.", encoding="utf-8"
    )
    (directory / "architect_planner.md").write_text(
        "You are the Architect-Planner. Read the analysis artifact, then write "
        "status: ok and a plan to the given path.",
        encoding="utf-8",
    )
    (directory / "implementer.md").write_text(
        "You are the Implementer. Read the analysis and plan artifacts, make "
        "changes in the workspace, then write status: ok and a report to the "
        "given path.",
        encoding="utf-8",
    )
    (directory / "tester.md").write_text(
        "You are the Tester. Read the analysis, plan, and implementation "
        "artifacts, run tests in the workspace, then write status: ok or "
        "status: failed and a report to the given path.",
        encoding="utf-8",
    )
    (directory / "reviewer.md").write_text(
        "You are the Reviewer. Read the analysis, plan, implementation, and "
        "test report artifacts, inspect the workspace, then write "
        "status: ok or status: changes_requested and a review to the given path.",
        encoding="utf-8",
    )
    (directory / "finalizer.md").write_text(
        "You are the Finalizer. Read the analysis, plan, implementation, test "
        "report, and review artifacts, then write status: ok and a final "
        "report to the given path.",
        encoding="utf-8",
    )
    return directory


def _fake_completed(returncode: int = 0, stdout: str = "{}", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _task_context(task_id: str = "t1") -> dict:
    return {"task_id": task_id, "request": "Investigate the login bug"}


# --- command construction -------------------------------------------


def test_builds_expected_bounded_noninteractive_command(repo, agents_dir, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        # Simulate Claude having written the artifact before exiting.
        output_path = repo / "artifacts" / "t1" / "01_analysis.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Findings.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "C:\\fake\\claude.CMD")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=42)
    runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo",
        allowed_capabilities=["Read", "Grep", "Glob"],
    )

    command = captured["command"]
    assert command[0] == "C:\\fake\\claude.CMD"
    assert "-p" in command
    assert "--output-format" in command and "json" in command
    assert "--append-system-prompt-file" in command
    assert "--no-session-persistence" in command
    assert "--allowedTools" in command
    allowed_index = command.index("--allowedTools")
    assert command[allowed_index + 1] == "Read Grep Glob"
    assert "--add-dir" in command
    # Bounded and non-interactive: no resume/continue flags, and a real timeout is enforced.
    assert "--resume" not in command
    assert "--continue" not in command
    assert "-c" not in command
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 42
    # The prompt is never an argv element -- see _build_command()'s
    # docstring: multi-line argv elements get silently truncated by
    # cmd.exe's batch-argument handling on Windows (the Phase 11 bug).
    # It must instead be piped over stdin.
    assert all("\n" not in part for part in command)
    assert captured["kwargs"]["input"] is not None
    assert "\n" in captured["kwargs"]["input"]  # the real prompt is multi-line
    # The actual task request text -- exactly what a real Claude run never
    # received before this fix -- must survive into the delivered prompt.
    assert "Investigate the login bug" in captured["kwargs"]["input"]


def test_no_argv_element_ever_contains_a_newline(repo, agents_dir, monkeypatch):
    """The exact Phase 11 regression test: on Windows, `claude` is a .CMD
    shim, so subprocess.run(..., shell=False) launches it through cmd.exe.
    A batch-argument containing an embedded newline is silently truncated
    at the first line break -- this is the real, reproduced root cause of
    the Phase 10 failure (Claude only ever saw "Task ID: <id>" and nothing
    else). No command element may contain "\\n", regardless of how long
    or multi-paragraph the prompt or role definition is."""
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "...")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    # architect_planner's real role file is long, multi-paragraph markdown
    # -- exactly the shape that would have broken the old
    # --append-system-prompt <text> approach.
    repo_root = Path(__file__).resolve().parents[2]
    real_agents_dir = repo_root / "agents"
    assert "\n" in (real_agents_dir / "architect_planner.md").read_text(encoding="utf-8")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", real_agents_dir)
    runner.run_agent(
        role="architect_planner",
        task_context={"task_id": "t1", "request": "A fairly long request.\nWith an explicit newline in it too."},
        input_artifacts=[repo / "artifacts" / "t1" / "01_analysis.md"],
        workspace_path=repo / "workspace" / "demo",
        allowed_capabilities=["Read", "Grep", "Glob"],
    )

    for element in captured["command"]:
        assert "\n" not in element, f"argv element contains a newline and would be truncated by cmd.exe: {element!r}"
    # The role definition is referenced by file path, never inlined --
    # avoiding the exact same truncation risk for the system prompt.
    assert "--append-system-prompt" not in captured["command"]  # old, broken flag must be gone
    assert "--append-system-prompt-file" in captured["command"]


def test_command_omits_allowedTools_when_none_given(repo, agents_dir, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        output_path = repo / "artifacts" / "t1" / "01_analysis.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Findings.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert "--allowedTools" not in captured["command"]
    assert "--add-dir" not in captured["command"]


# --- process outcome translation ----------------------------------------


def test_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "01_analysis.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "The bug is in auth.py.")
        return _fake_completed(returncode=0, stdout='{"type":"result","is_error":false}')

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=["Read"],
    )

    assert result.status == AgentResultStatus.OK
    assert result.runner_name == "claude_cli"
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "01_analysis.md"
    assert result.raw_log_path is not None and result.raw_log_path.exists()

    # Python, not Claude, is the one deciding this is acceptable -- prove the
    # artifact independently satisfies the existing validation contract.
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_nonzero_exit_returns_error_result(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        return _fake_completed(returncode=1, stderr="boom")

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 1" in result.notes
    assert result.raw_log_path is not None and result.raw_log_path.exists()


def test_timeout_is_handled_and_does_not_hang(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=5)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "5s" in result.notes


def test_missing_expected_artifact_returns_error(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        # Exits successfully but never writes the expected file.
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_missing_claude_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []  # never even tried to launch a process


def test_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    empty_agents_dir = tmp_path / "no_agents_here"
    empty_agents_dir.mkdir()
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", empty_agents_dir)
    result = runner.run_agent(
        role="analyst",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def test_unsupported_role_returns_error_without_launching(repo, agents_dir, monkeypatch):
    # "reviewer" is now a genuinely supported role (Phase 7), so this uses a
    # still-unsupported, deferred V2 role name instead. Give it a role
    # definition file so the failure genuinely comes from the
    # unsupported-role check, not from a missing role file.
    (agents_dir / "memory_keeper.md").write_text("You are the Memory Keeper.", encoding="utf-8")

    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="memory_keeper",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "does not implement role" in result.notes
    assert calls == []


# --- orchestrator only ever sees AgentResult -----------------------------


def test_orchestrator_end_to_end_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """ANALYZING -> run_agent -> (mocked) claude process -> AgentResult ->
    Python validates -> ANALYZED, exactly the Phase 3 required path, with
    the orchestrator never touching subprocess/CLI details directly."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "01_analysis.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "The bug is in auth.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Investigate the login bug")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm, "analyst", runner, input_artifacts=[], allowed_capabilities=["Read", "Grep", "Glob"]
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.ANALYZING  # start_stage doesn't auto-complete on OK

    errors = orchestrator.complete_stage(sm, "analyst", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.ANALYZED


def test_real_analyst_role_file_loads(repo):
    """Sanity check against the actual agents/analyst.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("analyst")
    assert "Analyst" in content
    assert "status: ok" in content


# ---------------------------------------------------------------------------
# Phase 4: architect_planner through the same runner, same abstraction.
# ---------------------------------------------------------------------------


def _analysis_artifact(repo: Path, task_id: str = "t1") -> Path:
    path = repo / "artifacts" / task_id / "01_analysis.md"
    artifacts.write_markdown(path, {"status": "ok"}, "The bug is in auth.py.")
    return path


def test_architect_planner_command_includes_role_prompt_and_input_artifact(repo, agents_dir, monkeypatch):
    captured = {}
    analysis_path = _analysis_artifact(repo)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Step 1: ...")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=99)
    runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[analysis_path],
        workspace_path=None,
        allowed_capabilities=["Read", "Grep", "Glob"],
    )

    command = captured["command"]
    assert "-p" in command
    # The prompt is piped via stdin, never an argv element -- see
    # _build_command()'s docstring for why (the Phase 11 newline bug).
    prompt = captured["kwargs"]["input"]
    # The plan stage must actually be pointed at the analysis artifact --
    # this is how it "consumes" the prior artifact, via the existing,
    # generic input_artifacts mechanism, not a new one.
    assert str(analysis_path) in prompt
    assert "02_design_plan.md" in prompt

    assert "--append-system-prompt-file" in command
    role_file_arg = command[command.index("--append-system-prompt-file") + 1]
    assert role_file_arg == str(agents_dir / "architect_planner.md")
    assert "Architect-Planner" in Path(role_file_arg).read_text(encoding="utf-8")
    assert "--no-session-persistence" in command
    assert captured["kwargs"]["timeout"] == 99


def test_architect_planner_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    analysis_path = _analysis_artifact(repo)

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Step 1: update auth.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[analysis_path],
        workspace_path=None,
        allowed_capabilities=["Read"],
    )

    assert result.status == AgentResultStatus.OK
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "02_design_plan.md"
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_architect_planner_nonzero_exit_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=2, stderr="boom"),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[_analysis_artifact(repo)],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 2" in result.notes


def test_architect_planner_timeout_is_handled(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=7)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "7s" in result.notes


def test_architect_planner_missing_output_artifact_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=0),  # never writes the file
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_architect_planner_invalid_artifact_fails_orchestrator_validation(repo, agents_dir, monkeypatch):
    """Claude exits 0 and writes a file, but it's missing the required
    'status' field -- the runner reports OK (a process fact), and it is
    Python's existing artifact validation, not the runner, that catches
    the content problem."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {}, "A plan with no status field.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.OK
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors  # Python rejects it despite the runner reporting OK


def test_architect_planner_missing_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []


def test_architect_planner_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    agents_dir_without_planner = tmp_path / "agents_missing_planner"
    agents_dir_without_planner.mkdir()
    (agents_dir_without_planner / "analyst.md").write_text("Analyst only.", encoding="utf-8")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir_without_planner)
    result = runner.run_agent(
        role="architect_planner",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def test_orchestrator_reaches_designed_with_mocked_claude_cli_architect_planner(repo, agents_dir, monkeypatch):
    """ANALYZED -> run_agent("architect_planner") -> (mocked) claude process
    -> AgentResult -> Python validates -> DESIGNED, using only the existing
    orchestrator mechanics, exactly as analyst already does for ANALYZED."""
    analysis_path = _analysis_artifact(repo)

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Step 1: update auth.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Fix the login bug")
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.ANALYZED)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "architect_planner",
        runner,
        input_artifacts=[analysis_path],
        allowed_capabilities=["Read", "Grep", "Glob"],
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.DESIGNING  # start_stage doesn't auto-complete on OK

    errors = orchestrator.complete_stage(sm, "architect_planner", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.DESIGNED


def test_governance_checkpoint_after_claude_cli_plan_auto_approves_when_not_sensitive(
    repo, agents_dir, monkeypatch
):
    """Preserving Phase 2 governance behavior: the checkpoint doesn't care
    which runner produced the plan artifact, only what it says."""
    import yaml as _yaml

    from forgemind import governance

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Read a config value and log it.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Log a config value")
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.ANALYZED)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    orchestrator.complete_stage(sm, "architect_planner", result.output_artifact_path, ["status"])

    gov_path = repo / "governance.yaml"
    gov_path.write_text(
        _yaml.safe_dump(
            {
                "sensitive_patterns": [
                    {"name": "git_push", "regex": r"git\s+push", "reason": "pushes commits"}
                ],
                "approval_required_stages": ["plan", "final"],
            }
        ),
        encoding="utf-8",
    )
    gov_config = governance.load_governance(gov_path)

    gov_result = orchestrator.apply_plan_governance_checkpoint(sm, result.output_artifact_path, gov_config)

    assert gov_result.sensitive is False
    assert sm.state == TaskState.PLAN_APPROVED  # non-gated path, no approve_task() needed


def test_governance_checkpoint_after_claude_cli_plan_waits_when_sensitive(repo, agents_dir, monkeypatch):
    import yaml as _yaml

    from forgemind import governance

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "02_design_plan.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Step 3: git push the release branch.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Ship the release")
    sm.transition(TaskState.ANALYZING)
    sm.transition(TaskState.ANALYZED)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(sm, "architect_planner", runner, input_artifacts=[])
    orchestrator.complete_stage(sm, "architect_planner", result.output_artifact_path, ["status"])

    gov_path = repo / "governance.yaml"
    gov_path.write_text(
        _yaml.safe_dump(
            {
                "sensitive_patterns": [
                    {"name": "git_push", "regex": r"git\s+push", "reason": "pushes commits"}
                ],
                "approval_required_stages": ["plan", "final"],
            }
        ),
        encoding="utf-8",
    )
    gov_config = governance.load_governance(gov_path)

    gov_result = orchestrator.apply_plan_governance_checkpoint(sm, result.output_artifact_path, gov_config)

    assert gov_result.sensitive is True
    assert sm.state == TaskState.AWAITING_PLAN_APPROVAL  # gated: waits for a human

    approve_errors = orchestrator.approve_task(sm)
    assert approve_errors == []
    assert sm.state == TaskState.PLAN_APPROVED


def test_real_architect_planner_role_file_loads(repo):
    """Sanity check against the actual agents/architect_planner.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("architect_planner")
    assert "Architect-Planner" in content
    assert "status: ok" in content


# ---------------------------------------------------------------------------
# Phase 5: implementer through the same runner, same abstraction.
# ---------------------------------------------------------------------------


def _plan_artifact(repo: Path, task_id: str = "t1") -> Path:
    path = repo / "artifacts" / task_id / "02_design_plan.md"
    artifacts.write_markdown(path, {"status": "ok"}, "Step 1: update auth.py.")
    return path


def test_implementer_command_includes_role_prompt_analysis_and_plan_inputs(repo, agents_dir, monkeypatch):
    captured = {}
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    workspace_path = repo / "workspace" / "demo-project"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "03_implementation.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Implemented the fix.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=88)
    runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path],
        workspace_path=workspace_path,
        allowed_capabilities=["Read", "Edit", "Write", "Bash"],
    )

    command = captured["command"]
    # The prompt is piped via stdin, never an argv element -- see
    # _build_command()'s docstring for why (the Phase 11 newline bug).
    prompt = captured["kwargs"]["input"]

    # Both prior artifacts must actually be consumed via the existing,
    # generic input_artifacts mechanism -- no separate wiring for the plan.
    assert str(analysis_path) in prompt
    assert str(plan_path) in prompt
    assert "03_implementation.md" in prompt

    # The workspace must be reflected both in the prompt text and in the
    # actual command/cwd Claude is launched with -- not simulated.
    assert str(workspace_path) in prompt
    assert "--add-dir" in command
    add_dir_index = command.index("--add-dir")
    assert command[add_dir_index + 1] == str(workspace_path)
    assert captured["kwargs"]["cwd"] == str(workspace_path)

    assert "--append-system-prompt-file" in command
    role_file_arg = command[command.index("--append-system-prompt-file") + 1]
    assert role_file_arg == str(agents_dir / "implementer.md")
    assert "Implementer" in Path(role_file_arg).read_text(encoding="utf-8")
    assert "--no-session-persistence" in command
    assert captured["kwargs"]["timeout"] == 88


def test_implementer_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "03_implementation.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Implemented the fix in auth.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Edit", "Write"],
    )

    assert result.status == AgentResultStatus.OK
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "03_implementation.md"
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_implementer_nonzero_exit_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=3, stderr="boom"),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[_analysis_artifact(repo), _plan_artifact(repo)],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 3" in result.notes


def test_implementer_timeout_is_handled(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=11)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "11s" in result.notes


def test_implementer_missing_output_artifact_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=0),  # never writes the file
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_implementer_invalid_artifact_fails_orchestrator_validation(repo, agents_dir, monkeypatch):
    """Claude exits 0 and writes a file, but it's missing the required
    'status' field -- the runner reports OK (a process fact), and it is
    Python's existing artifact validation, not the runner, that catches
    the content problem."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "03_implementation.md"
        artifacts.write_markdown(output_path, {}, "A report with no status field.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.OK
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors  # Python rejects it despite the runner reporting OK


def test_implementer_missing_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []


def test_implementer_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    agents_dir_without_implementer = tmp_path / "agents_missing_implementer"
    agents_dir_without_implementer.mkdir()
    (agents_dir_without_implementer / "analyst.md").write_text("Analyst only.", encoding="utf-8")
    (agents_dir_without_implementer / "architect_planner.md").write_text("Planner only.", encoding="utf-8")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir_without_implementer)
    result = runner.run_agent(
        role="implementer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def test_orchestrator_reaches_implemented_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """PLAN_APPROVED -> run_agent("implementer") -> (mocked) claude process
    -> AgentResult -> Python validates -> IMPLEMENTED, using only the
    existing orchestrator mechanics, exactly as analyst/architect_planner
    already do for their own success states."""
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    workspace_path = repo / "workspace" / "demo-project"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "03_implementation.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Implemented the fix in auth.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Fix the login bug")
    for state in [
        TaskState.ANALYZING,
        TaskState.ANALYZED,
        TaskState.DESIGNING,
        TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.PLAN_APPROVED,
    ]:
        sm.transition(state)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "implementer",
        runner,
        input_artifacts=[analysis_path, plan_path],
        workspace_path=workspace_path,
        allowed_capabilities=["Read", "Edit", "Write"],
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.IMPLEMENTING  # start_stage doesn't auto-complete on OK

    errors = orchestrator.complete_stage(sm, "implementer", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.IMPLEMENTED


def test_real_implementer_role_file_loads(repo):
    """Sanity check against the actual agents/implementer.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("implementer")
    assert "Implementer" in content
    assert "status: ok" in content


# ---------------------------------------------------------------------------
# Phase 6: tester through the same runner, same abstraction.
# ---------------------------------------------------------------------------


def _implementation_artifact(repo: Path, task_id: str = "t1") -> Path:
    path = repo / "artifacts" / task_id / "03_implementation.md"
    artifacts.write_markdown(path, {"status": "ok"}, "Implemented the fix in auth.py.")
    return path


def test_tester_command_includes_role_prompt_and_all_three_prior_inputs(repo, agents_dir, monkeypatch):
    captured = {}
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    implementation_path = _implementation_artifact(repo)
    workspace_path = repo / "workspace" / "demo-project"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "All tests passed.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=77)
    runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path, implementation_path],
        workspace_path=workspace_path,
        allowed_capabilities=["Read", "Bash"],
    )

    command = captured["command"]
    # The prompt is piped via stdin, never an argv element -- see
    # _build_command()'s docstring for why (the Phase 11 newline bug).
    prompt = captured["kwargs"]["input"]

    # All three prior artifacts must actually be consumed via the existing,
    # generic input_artifacts mechanism -- no separate wiring per artifact.
    assert str(analysis_path) in prompt
    assert str(plan_path) in prompt
    assert str(implementation_path) in prompt
    assert "04_test_report.md" in prompt

    # The workspace must be reflected in the prompt text, the command, and
    # the actual subprocess cwd -- the same generic mechanism already
    # proven for the Implementer, reused without any Tester-specific fork.
    assert str(workspace_path) in prompt
    assert "--add-dir" in command
    add_dir_index = command.index("--add-dir")
    assert command[add_dir_index + 1] == str(workspace_path)
    assert captured["kwargs"]["cwd"] == str(workspace_path)

    assert "--append-system-prompt-file" in command
    role_file_arg = command[command.index("--append-system-prompt-file") + 1]
    assert role_file_arg == str(agents_dir / "tester.md")
    assert "Tester" in Path(role_file_arg).read_text(encoding="utf-8")
    assert "--no-session-persistence" in command
    assert "--resume" not in command
    assert "--continue" not in command
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 77


def test_tester_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    implementation_path = _implementation_artifact(repo)

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Ran pytest: 12 passed.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path, implementation_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Bash"],
    )

    assert result.status == AgentResultStatus.OK
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "04_test_report.md"
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_tester_nonzero_exit_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=4, stderr="boom"),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[_analysis_artifact(repo), _plan_artifact(repo), _implementation_artifact(repo)],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 4" in result.notes


def test_tester_timeout_is_handled(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=13)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "13s" in result.notes


def test_tester_missing_output_artifact_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=0),  # never writes the file
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_tester_invalid_artifact_fails_orchestrator_validation(repo, agents_dir, monkeypatch):
    """Claude exits 0 and writes a file, but it's missing the required
    'status' field -- the runner reports OK (a process fact), and it is
    Python's existing artifact validation, not the runner, that catches
    the content problem."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {}, "A report with no status field.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.OK
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors  # Python rejects it despite the runner reporting OK


def test_tester_missing_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []


def test_tester_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    agents_dir_without_tester = tmp_path / "agents_missing_tester"
    agents_dir_without_tester.mkdir()
    (agents_dir_without_tester / "analyst.md").write_text("Analyst only.", encoding="utf-8")
    (agents_dir_without_tester / "architect_planner.md").write_text("Planner only.", encoding="utf-8")
    (agents_dir_without_tester / "implementer.md").write_text("Implementer only.", encoding="utf-8")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir_without_tester)
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def _drive_to_implemented(repo: Path, agents_dir: Path, monkeypatch) -> TaskStateMachine:
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "03_implementation.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Implemented the fix.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    from forgemind import locking

    locking.acquire(repo / "tasks", "t1")
    sm = TaskStateMachine.create(repo / "tasks", "t1")
    (repo / "tasks" / "t1").mkdir(exist_ok=True)
    artifacts.write_markdown(repo / "tasks" / "t1" / "task.md", {"id": "t1"}, "Fix the login bug")
    for state in [
        TaskState.ANALYZING,
        TaskState.ANALYZED,
        TaskState.DESIGNING,
        TaskState.DESIGNED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.PLAN_APPROVED,
    ]:
        sm.transition(state)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "implementer",
        runner,
        input_artifacts=[analysis_path, plan_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Edit", "Write"],
    )
    orchestrator.complete_stage(sm, "implementer", result.output_artifact_path, ["status"])
    assert sm.state == TaskState.IMPLEMENTED
    return sm


def test_orchestrator_reaches_tests_passed_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """IMPLEMENTED -> run_agent("tester") -> (mocked) claude process ->
    AgentResult -> Python validates -> TESTS_PASSED. There is no "TESTED"
    state in the existing state machine -- TESTS_PASSED/TESTS_FAILED are
    the real, existing legal outcomes of the TESTING stage, unchanged
    since Phase 2."""
    sm = _drive_to_implemented(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Ran pytest: 12 passed.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "tester",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Bash"],
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.TESTING  # start_stage doesn't auto-complete on OK

    # The Tester's own reported status decides which existing target_state
    # applies -- Python reads the fact, the existing complete_stage() picks
    # the transition. No new orchestrator logic for this phase.
    frontmatter, _body = artifacts.read_markdown(result.output_artifact_path)
    target_state = TaskState.TESTS_FAILED if frontmatter.get("status") == "failed" else None
    errors = orchestrator.complete_stage(
        sm, "tester", result.output_artifact_path, ["status"], target_state=target_state
    )
    assert errors == []
    assert sm.state == TaskState.TESTS_PASSED


def test_orchestrator_reaches_tests_failed_and_retries_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """Same path, but the Tester reports a real defect: TESTS_FAILED, then
    the existing retry_after_test_failure() loop-back to IMPLEMENTING --
    proving the pre-existing retry mechanism from Phase 2 needs no changes
    to work with a Claude-produced test report."""
    from forgemind.config import ForgeMindConfig

    sm = _drive_to_implemented(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {"status": "failed"}, "2 tests failed in auth_test.py.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "tester",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Bash"],
    )

    assert result.status == AgentResultStatus.OK
    frontmatter, _body = artifacts.read_markdown(result.output_artifact_path)
    assert frontmatter["status"] == "failed"

    errors = orchestrator.complete_stage(
        sm, "tester", result.output_artifact_path, ["status"], target_state=TaskState.TESTS_FAILED
    )
    assert errors == []
    assert sm.state == TaskState.TESTS_FAILED

    config = ForgeMindConfig(max_implementation_retries=2)
    next_state = orchestrator.retry_after_test_failure(sm, config)
    assert next_state == TaskState.IMPLEMENTING
    assert sm.implementation_retry_count == 1


def test_real_tester_role_file_loads(repo):
    """Sanity check against the actual agents/tester.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("tester")
    assert "Tester" in content
    assert "status: ok" in content


# ---------------------------------------------------------------------------
# Regression: a real Tester run on a static HTML/CSS project (no test
# framework) stayed stuck for ~16 minutes past its configured 600s timeout,
# and left orphaned `claude` processes running afterward. Root cause:
# subprocess.run()'s own timeout handling only kills the ONE process it
# directly launched (cmd.exe, since claude is a .CMD shim on Windows) --
# not any child processes that process itself spawned (e.g. a shell
# command Claude's Tester agent started). These tests use real OS
# processes (no mocking of _launch/subprocess) to prove _launch actually
# terminates the whole tree, not just the top of it.
# ---------------------------------------------------------------------------


from forgemind.runners.claude_cli_runner import _launch


@pytest.mark.skipif(os.name != "nt", reason="the real Tester hang was Windows-specific (.CMD shim + cmd.exe)")
def test_launch_kills_entire_process_tree_on_timeout(tmp_path):
    """Simulates exactly the real failure: a script that itself spawns a
    detached background process (like a Claude tool call starting a shell
    command) and then blocks. _launch(timeout=...) must return promptly
    -- not hang for the full inner delay -- and the detached grandchild
    must never be allowed to finish, proving the whole tree was killed,
    not just the immediate child."""
    marker = tmp_path / "grandchild_finished.marker"
    script = tmp_path / "hang.bat"
    # The grandchild ('start /B ...') would write the marker ~2s in if left
    # alone; the parent itself blocks for far longer than our timeout.
    script.write_text(
        "@echo off\r\n"
        f'start /B cmd /c "ping -n 3 127.0.0.1 >nul & echo done > \\"{marker}\\""\r\n'
        "ping -n 30 127.0.0.1 >nul\r\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _launch(
            [str(script)],
            shell=False,
            input="",
            capture_output=True,
            text=True,
            timeout=1,
            cwd=None,
        )
    elapsed = time.monotonic() - started

    # Bounded: proves subprocess.run() cannot leave the pipeline blocked --
    # this must return in roughly `timeout` + kill overhead, never anywhere
    # near the script's real ~30s inner delay.
    assert elapsed < 15

    # Give the (correctly killed) grandchild the time it *would* have
    # needed to write the marker if it had survived, then confirm it
    # never did -- proof the whole tree was terminated, not just the
    # script's own top-level process.
    time.sleep(4)
    assert not marker.exists(), (
        "grandchild process survived _launch()'s timeout kill -- only the "
        "top-level process was terminated, exactly the real Tester hang"
    )


@pytest.mark.skipif(os.name != "nt", reason="the real Tester hang was Windows-specific (.CMD shim + cmd.exe)")
def test_tester_run_agent_does_not_hang_and_leaves_no_orphans(repo, agents_dir, tmp_path):
    """End-to-end through the real ClaudeCodeCLIRunner.run_agent() (no
    monkeypatching _launch at all) with `executable` pointed at a real
    hanging script standing in for `claude` -- proves the fix works
    through the actual role this bug was found on, not just _launch()
    in isolation."""
    marker = tmp_path / "orphan.marker"
    script = tmp_path / "claude.bat"
    script.write_text(
        "@echo off\r\n"
        f'start /B cmd /c "ping -n 3 127.0.0.1 >nul & echo done > \\"{marker}\\""\r\n'
        "ping -n 30 127.0.0.1 >nul\r\n",
        encoding="utf-8",
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, executable=str(script), timeout_seconds=1)

    started = time.monotonic()
    result = runner.run_agent(
        role="tester",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=["Read", "Bash"],
    )
    elapsed = time.monotonic() - started

    assert result.status == AgentResultStatus.TIMEOUT
    assert "1s" in result.notes
    assert elapsed < 15  # the pipeline stage genuinely returns, not blocked forever

    time.sleep(4)
    assert not marker.exists(), "a real Tester timeout still left an orphaned process running"


# ---------------------------------------------------------------------------
# Phase 7: reviewer through the same runner, same abstraction. This is the
# fifth and final V1 role.
# ---------------------------------------------------------------------------


def _test_report_artifact(repo: Path, task_id: str = "t1") -> Path:
    path = repo / "artifacts" / task_id / "04_test_report.md"
    artifacts.write_markdown(path, {"status": "ok"}, "All tests passed.")
    return path


def test_reviewer_command_includes_role_prompt_and_all_four_prior_inputs(repo, agents_dir, monkeypatch):
    captured = {}
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    implementation_path = _implementation_artifact(repo)
    test_report_path = _test_report_artifact(repo)
    workspace_path = repo / "workspace" / "demo-project"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Looks correct.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=66)
    runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path],
        workspace_path=workspace_path,
        allowed_capabilities=["Read", "Grep"],
    )

    command = captured["command"]
    # The prompt is piped via stdin, never an argv element -- see
    # _build_command()'s docstring for why (the Phase 11 newline bug).
    prompt = captured["kwargs"]["input"]

    # All four prior artifacts must actually be consumed via the existing,
    # generic input_artifacts mechanism -- no separate wiring per artifact.
    assert str(analysis_path) in prompt
    assert str(plan_path) in prompt
    assert str(implementation_path) in prompt
    assert str(test_report_path) in prompt
    assert "05_review.md" in prompt

    # The workspace must be reflected in the prompt text, the command, and
    # the actual subprocess cwd -- the same generic mechanism already
    # proven for Implementer and Tester, reused without any Reviewer fork.
    assert str(workspace_path) in prompt
    assert "--add-dir" in command
    add_dir_index = command.index("--add-dir")
    assert command[add_dir_index + 1] == str(workspace_path)
    assert captured["kwargs"]["cwd"] == str(workspace_path)

    assert "--append-system-prompt-file" in command
    role_file_arg = command[command.index("--append-system-prompt-file") + 1]
    assert role_file_arg == str(agents_dir / "reviewer.md")
    assert "Reviewer" in Path(role_file_arg).read_text(encoding="utf-8")
    assert "--no-session-persistence" in command
    assert "--resume" not in command
    assert "--continue" not in command
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 66


def test_reviewer_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    inputs = [
        _analysis_artifact(repo),
        _plan_artifact(repo),
        _implementation_artifact(repo),
        _test_report_artifact(repo),
    ]

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Matches the plan, no issues found.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=inputs,
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Grep"],
    )

    assert result.status == AgentResultStatus.OK
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "05_review.md"
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_reviewer_nonzero_exit_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=5, stderr="boom"),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 5" in result.notes


def test_reviewer_timeout_is_handled(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=17)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "17s" in result.notes


def test_reviewer_missing_output_artifact_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=0),  # never writes the file
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_reviewer_invalid_artifact_fails_orchestrator_validation(repo, agents_dir, monkeypatch):
    """Claude exits 0 and writes a file, but it's missing the required
    'status' field -- the runner reports OK (a process fact), and it is
    Python's existing artifact validation, not the runner, that catches
    the content problem."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(output_path, {}, "A review with no status field.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.OK
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors  # Python rejects it despite the runner reporting OK


def test_reviewer_missing_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []


def test_reviewer_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    agents_dir_without_reviewer = tmp_path / "agents_missing_reviewer"
    agents_dir_without_reviewer.mkdir()
    (agents_dir_without_reviewer / "analyst.md").write_text("Analyst only.", encoding="utf-8")
    (agents_dir_without_reviewer / "architect_planner.md").write_text("Planner only.", encoding="utf-8")
    (agents_dir_without_reviewer / "implementer.md").write_text("Implementer only.", encoding="utf-8")
    (agents_dir_without_reviewer / "tester.md").write_text("Tester only.", encoding="utf-8")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir_without_reviewer)
    result = runner.run_agent(
        role="reviewer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def _drive_to_tests_passed(repo: Path, agents_dir: Path, monkeypatch) -> TaskStateMachine:
    sm = _drive_to_implemented(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "04_test_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Ran pytest: 12 passed.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "tester",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Bash"],
    )
    errors = orchestrator.complete_stage(sm, "tester", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.TESTS_PASSED
    return sm


def test_orchestrator_reviewer_approved_reaches_final_approved_with_mocked_claude_cli(
    repo, agents_dir, monkeypatch
):
    """TESTS_PASSED -> run_agent("reviewer") -> (mocked) claude process ->
    AgentResult -> Python validates -> REVIEWED -> the existing, unmodified
    apply_final_governance_checkpoint() -> FINAL_APPROVED (non-sensitive
    implementation content, so the existing non-gated auto-approve path
    applies -- no approve_task() call needed, exactly as Phase 2 already
    proved for the plan checkpoint)."""
    sm = _drive_to_tests_passed(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"
    test_report_path = repo / "artifacts" / "t1" / "04_test_report.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Matches the plan, no issues found.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "reviewer",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Grep"],
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.REVIEWING  # start_stage doesn't auto-complete on OK

    errors = orchestrator.complete_stage(sm, "reviewer", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.REVIEWED

    from forgemind import governance

    gov_path = repo / "governance.yaml"
    gov_path.write_text(
        yaml_dump_no_sensitive_patterns(),
        encoding="utf-8",
    )
    gov_config = governance.load_governance(gov_path)
    gov_result = orchestrator.apply_final_governance_checkpoint(sm, implementation_path, gov_config)

    assert gov_result.sensitive is False
    assert sm.state == TaskState.FINAL_APPROVED


def yaml_dump_no_sensitive_patterns() -> str:
    import yaml as _yaml

    return _yaml.safe_dump(
        {
            "sensitive_patterns": [
                {"name": "git_push", "regex": r"git\s+push", "reason": "pushes commits"}
            ],
            "approval_required_stages": ["plan", "final"],
        }
    )


def test_orchestrator_reviewer_changes_requested_retries_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """Same path, but the Reviewer requests changes: REVIEWING ->
    retry_after_review_revision() -> IMPLEMENTING, using the pre-existing
    Phase 2 revision-retry mechanism unmodified. There is no intermediate
    landing state for "changes requested" -- exactly matching how
    orchestrator.submit_artifact() already routes a human-submitted
    reviewer artifact with this status."""
    from forgemind.config import ForgeMindConfig

    sm = _drive_to_tests_passed(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"
    test_report_path = repo / "artifacts" / "t1" / "04_test_report.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(
            output_path, {"status": "changes_requested"}, "The error handling branch is missing."
        )
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "reviewer",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path],
        workspace_path=repo / "workspace" / "demo-project",
        allowed_capabilities=["Read", "Grep"],
    )

    assert result.status == AgentResultStatus.OK
    frontmatter, _body = artifacts.read_markdown(result.output_artifact_path)
    assert frontmatter["status"] == "changes_requested"

    config = ForgeMindConfig(max_revision_retries=2)
    next_state = orchestrator.retry_after_review_revision(sm, config)
    assert next_state == TaskState.IMPLEMENTING
    assert sm.revision_retry_count == 1


def test_real_reviewer_role_file_loads(repo):
    """Sanity check against the actual agents/reviewer.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("reviewer")
    assert "Reviewer" in content
    assert "status: ok" in content
    assert "changes_requested" in content


# ---------------------------------------------------------------------------
# Phase 9: finalizer through the same runner, same abstraction. This is the
# sixth and final V1 role -- FINAL_APPROVED -> FINALIZING -> COMPLETED.
# ---------------------------------------------------------------------------


def _review_artifact(repo: Path, task_id: str = "t1") -> Path:
    path = repo / "artifacts" / task_id / "05_review.md"
    artifacts.write_markdown(path, {"status": "ok"}, "Matches the plan, no issues found.")
    return path


def test_finalizer_command_includes_role_prompt_and_all_five_prior_inputs(repo, agents_dir, monkeypatch):
    captured = {}
    analysis_path = _analysis_artifact(repo)
    plan_path = _plan_artifact(repo)
    implementation_path = _implementation_artifact(repo)
    test_report_path = _test_report_artifact(repo)
    review_path = _review_artifact(repo)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = repo / "artifacts" / "t1" / "06_final_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Summary.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=55)
    runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path, review_path],
        workspace_path=None,
        allowed_capabilities=["Read"],
    )

    command = captured["command"]
    # The prompt is piped via stdin, never an argv element -- see
    # _build_command()'s docstring for why (the Phase 11 newline bug).
    prompt = captured["kwargs"]["input"]

    # All five prior artifacts must actually be consumed via the existing,
    # generic input_artifacts mechanism -- no separate wiring per artifact.
    assert str(analysis_path) in prompt
    assert str(plan_path) in prompt
    assert str(implementation_path) in prompt
    assert str(test_report_path) in prompt
    assert str(review_path) in prompt
    assert "06_final_report.md" in prompt

    assert "--append-system-prompt-file" in command
    role_file_arg = command[command.index("--append-system-prompt-file") + 1]
    assert role_file_arg == str(agents_dir / "finalizer.md")
    assert "Finalizer" in Path(role_file_arg).read_text(encoding="utf-8")
    assert "--no-session-persistence" in command
    assert "--resume" not in command
    assert "--continue" not in command
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 55


def test_finalizer_successful_execution_returns_ok_result(repo, agents_dir, monkeypatch):
    inputs = [
        _analysis_artifact(repo),
        _plan_artifact(repo),
        _implementation_artifact(repo),
        _test_report_artifact(repo),
        _review_artifact(repo),
    ]

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "06_final_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "The feature was added and verified.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=inputs,
        workspace_path=None,
        allowed_capabilities=["Read"],
    )

    assert result.status == AgentResultStatus.OK
    # The canonical, pre-existing artifact-naming source of truth.
    assert result.output_artifact_path == repo / "artifacts" / "t1" / "06_final_report.md"
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors == []


def test_finalizer_nonzero_exit_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=6, stderr="boom"),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "exited with code 6" in result.notes


def test_finalizer_timeout_is_handled(repo, agents_dir, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir, timeout_seconds=21)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.TIMEOUT
    assert "21s" in result.notes


def test_finalizer_missing_output_artifact_returns_error(repo, agents_dir, monkeypatch):
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda command, **kwargs: _fake_completed(returncode=0),  # never writes the file
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "was not created" in result.notes


def test_finalizer_invalid_artifact_fails_orchestrator_validation(repo, agents_dir, monkeypatch):
    """Claude exits 0 and writes a file, but it's missing the required
    'status' field -- the runner reports OK (a process fact), and it is
    Python's existing artifact validation, not the runner, that catches
    the content problem."""

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "06_final_report.md"
        artifacts.write_markdown(output_path, {}, "A report with no status field.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.OK
    errors = artifacts.validate_artifact_file(result.output_artifact_path, ["status"])
    assert errors  # Python rejects it despite the runner reporting OK


def test_finalizer_missing_executable_returns_error_without_launching(repo, agents_dir, monkeypatch):
    calls = []
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "forgemind.runners.claude_cli_runner._launch",
        lambda *a, **k: calls.append(1) or _fake_completed(),
    )

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "not found on PATH" in result.notes
    assert calls == []


def test_finalizer_missing_role_definition_returns_error(repo, tmp_path, monkeypatch):
    agents_dir_without_finalizer = tmp_path / "agents_missing_finalizer"
    agents_dir_without_finalizer.mkdir()
    (agents_dir_without_finalizer / "analyst.md").write_text("Analyst only.", encoding="utf-8")
    (agents_dir_without_finalizer / "architect_planner.md").write_text("Planner only.", encoding="utf-8")
    (agents_dir_without_finalizer / "implementer.md").write_text("Implementer only.", encoding="utf-8")
    (agents_dir_without_finalizer / "tester.md").write_text("Tester only.", encoding="utf-8")
    (agents_dir_without_finalizer / "reviewer.md").write_text("Reviewer only.", encoding="utf-8")
    monkeypatch.setattr("forgemind.runners.claude_cli_runner.shutil.which", lambda name: "claude")

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir_without_finalizer)
    result = runner.run_agent(
        role="finalizer",
        task_context=_task_context(),
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )

    assert result.status == AgentResultStatus.ERROR
    assert "No role definition found" in result.notes


def _drive_to_reviewed(repo: Path, agents_dir: Path, monkeypatch) -> TaskStateMachine:
    sm = _drive_to_tests_passed(repo, agents_dir, monkeypatch)
    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"
    test_report_path = repo / "artifacts" / "t1" / "04_test_report.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "05_review.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "Matches the plan.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "reviewer",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path],
        allowed_capabilities=["Read", "Grep"],
    )
    errors = orchestrator.complete_stage(sm, "reviewer", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.REVIEWED
    return sm


def test_orchestrator_reaches_completed_with_mocked_claude_cli(repo, agents_dir, monkeypatch):
    """AWAITING_FINAL_APPROVAL/FINAL_APPROVED -> run_agent("finalizer") ->
    (mocked) claude process -> AgentResult -> Python validates ->
    COMPLETED, using only the existing orchestrator mechanics, exactly as
    every other role already does for its own success state."""
    sm = _drive_to_reviewed(repo, agents_dir, monkeypatch)
    from forgemind import governance

    # Non-sensitive implementation content -> the existing non-gated path
    # auto-approves straight to FINAL_APPROVED, no approve_task() needed.
    gov_config = governance.GovernanceConfig(patterns=[], approval_required_stages=["plan", "final"])
    implementation_path = repo / "artifacts" / "t1" / "03_implementation.md"
    gov_result = orchestrator.apply_final_governance_checkpoint(sm, implementation_path, gov_config)
    assert gov_result.sensitive is False
    assert sm.state == TaskState.FINAL_APPROVED

    analysis_path = repo / "artifacts" / "t1" / "01_analysis.md"
    plan_path = repo / "artifacts" / "t1" / "02_design_plan.md"
    test_report_path = repo / "artifacts" / "t1" / "04_test_report.md"
    review_path = repo / "artifacts" / "t1" / "05_review.md"

    def fake_run(command, **kwargs):
        output_path = repo / "artifacts" / "t1" / "06_final_report.md"
        artifacts.write_markdown(output_path, {"status": "ok"}, "The login bug was fixed and verified.")
        return _fake_completed(returncode=0)

    monkeypatch.setattr("forgemind.runners.claude_cli_runner._launch", fake_run)

    runner = ClaudeCodeCLIRunner(repo / "artifacts", agents_dir)
    result = orchestrator.start_stage(
        sm,
        "finalizer",
        runner,
        input_artifacts=[analysis_path, plan_path, implementation_path, test_report_path, review_path],
        allowed_capabilities=["Read"],
    )

    assert result.status == AgentResultStatus.OK
    assert sm.state == TaskState.FINALIZING  # start_stage doesn't auto-complete on OK

    errors = orchestrator.complete_stage(sm, "finalizer", result.output_artifact_path, ["status"])
    assert errors == []
    assert sm.state == TaskState.COMPLETED


def test_real_finalizer_role_file_loads(repo):
    """Sanity check against the actual agents/finalizer.md shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = ClaudeCodeCLIRunner(repo / "artifacts", repo_root / "agents")
    content = runner._load_role_definition("finalizer")
    assert "Finalizer" in content
    assert "status: ok" in content

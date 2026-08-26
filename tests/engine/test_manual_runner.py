from __future__ import annotations

import pytest

from forgemind import artifacts
from forgemind.runners.base import AgentResultStatus
from forgemind.runners.manual_runner import ManualRunner


def test_run_agent_writes_pending_prompt_and_returns_pending(repo):
    runner = ManualRunner(repo / "tasks")
    (repo / "tasks" / "t1").mkdir()
    result = runner.run_agent(
        role="analyst",
        task_context={"task_id": "t1", "request": "Do the thing"},
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=["Read", "Grep"],
    )
    assert result.status == AgentResultStatus.PENDING
    assert result.runner_name == "manual"
    pending_path = repo / "tasks" / "t1" / "pending_prompt.md"
    assert pending_path.exists()
    frontmatter, _body = artifacts.read_markdown(pending_path)
    assert frontmatter["role"] == "analyst"
    assert frontmatter["task_id"] == "t1"
    assert frontmatter["allowed_capabilities"] == ["Read", "Grep"]


def test_complete_pending_returns_ok_and_removes_prompt(repo):
    runner = ManualRunner(repo / "tasks")
    (repo / "tasks" / "t1").mkdir()
    runner.run_agent(
        role="analyst",
        task_context={"task_id": "t1", "request": "Do the thing"},
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )
    output_path = repo / "artifacts" / "t1_result.md"
    artifacts.write_markdown(output_path, {"status": "ok"}, "Result body.")

    result = runner.complete_pending("t1", output_path)

    assert result.status == AgentResultStatus.OK
    assert result.output_artifact_path == output_path
    assert result.runner_name == "manual"
    pending_path = repo / "tasks" / "t1" / "pending_prompt.md"
    assert not pending_path.exists()


def test_complete_pending_without_run_agent_raises(repo):
    runner = ManualRunner(repo / "tasks")
    (repo / "tasks" / "t1").mkdir()
    with pytest.raises(FileNotFoundError):
        runner.complete_pending("t1", repo / "artifacts" / "nope.md")


def test_complete_pending_missing_output_raises(repo):
    runner = ManualRunner(repo / "tasks")
    (repo / "tasks" / "t1").mkdir()
    runner.run_agent(
        role="analyst",
        task_context={"task_id": "t1", "request": "x"},
        input_artifacts=[],
        workspace_path=None,
        allowed_capabilities=[],
    )
    with pytest.raises(FileNotFoundError):
        runner.complete_pending("t1", repo / "artifacts" / "missing.md")

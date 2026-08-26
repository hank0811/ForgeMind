"""Phase 12: Implementer and Tester must actually be able to run commands.

Phase 11's real (non-mocked) smoke test showed the Implementer and Tester
repeatedly attempting to run `python`/test commands and being denied every
time -- their tool name for shell execution on Windows is "PowerShell",
not "Bash" (confirmed via the real permission_denials log), and the
Implementer had no shell tool granted at all. This file guards against
that regressing, and proves the fix is actually wired through
run_full_pipeline(), not just present in the dict.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from forgemind import artifacts, governance, orchestrator
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner
from forgemind.state_machine import TaskStateMachine


def test_implementer_and_tester_can_invoke_a_shell():
    for role in ("implementer", "tester"):
        caps = orchestrator.DEFAULT_CAPABILITIES[role]
        assert "Bash" in caps, f"{role} must be able to run commands on POSIX (Bash)"
        assert "PowerShell" in caps, f"{role} must be able to run commands on this Windows machine"


def test_read_only_roles_are_not_granted_shell_access():
    """Analyst, Architect-Planner, Reviewer, and Finalizer explicitly
    forbid running commands in their own agents/*.md -- the capability
    grant must not silently contradict that."""
    for role in ("analyst", "architect_planner", "reviewer", "finalizer"):
        caps = orchestrator.DEFAULT_CAPABILITIES[role]
        assert "Bash" not in caps, f"{role} is documented read-only; must not gain shell access"
        assert "PowerShell" not in caps, f"{role} is documented read-only; must not gain shell access"


class _CapabilityCapturingRunner(AgentRunner):
    """Fake runner that records exactly what allowed_capabilities each
    role call actually received, and otherwise behaves like a normal
    successful runner -- no subprocess, no Claude call."""

    def __init__(self, artifacts_root: Path):
        self.artifacts_root = artifacts_root
        self.received: dict[str, list[str]] = {}

    def run_agent(
        self,
        role: str,
        task_context: dict,
        input_artifacts: list[Path],
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> AgentResult:
        self.received[role] = list(allowed_capabilities)
        task_id = task_context["task_id"]
        output_path = self.artifacts_root / task_id / orchestrator.ARTIFACT_FILENAMES[role]
        artifacts.write_markdown(output_path, {"status": "ok"}, "...")
        return AgentResult(status=AgentResultStatus.OK, output_artifact_path=output_path, runner_name="fake")


def test_run_full_pipeline_actually_passes_powershell_through_to_implementer_and_tester(repo):
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = _CapabilityCapturingRunner(repo / "artifacts")
    gov_config = governance.GovernanceConfig(patterns=[], approval_required_stages=["plan", "final"])

    orchestrator.run_full_pipeline(sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config)

    assert "PowerShell" in runner.received["implementer"]
    assert "Bash" in runner.received["implementer"]
    assert "PowerShell" in runner.received["tester"]
    assert "Bash" in runner.received["tester"]
    # Confirms the read-only roles genuinely received no shell access
    # through the real call path, not just in the static dict.
    for role in ("analyst", "architect_planner", "reviewer", "finalizer"):
        assert "PowerShell" not in runner.received[role]
        assert "Bash" not in runner.received[role]


def test_explicit_capabilities_override_still_wins(repo):
    """A caller-supplied override must still take priority over the
    default -- unchanged behavior, just confirming the fix didn't
    accidentally make the default unconditional."""
    task = task_module.create_task(repo / "tasks", repo / "artifacts", "Add a feature")
    sm = TaskStateMachine.load(repo / "tasks", task.task_id)
    runner = _CapabilityCapturingRunner(repo / "artifacts")
    gov_config = governance.GovernanceConfig(patterns=[], approval_required_stages=["plan", "final"])

    orchestrator.run_full_pipeline(
        sm, runner, repo / "artifacts", None, ForgeMindConfig(), gov_config,
        capabilities={"tester": ["Read"]},
    )

    assert runner.received["tester"] == ["Read"]

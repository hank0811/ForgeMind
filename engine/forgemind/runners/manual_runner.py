"""Human-in-the-loop AgentRunner.

For Phase 1 this is the only working runner. It never calls Claude Code,
never shells out, and never makes an AI call of any kind. It writes a
self-contained "prompt packet" describing the stage, returns PENDING, and
waits for a human to run the stage manually elsewhere and submit the
resulting artifact via `complete_pending()`.

This lets the entire deterministic engine be developed and tested without
spending any AI budget.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from forgemind import artifacts
from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner


class ManualRunner(AgentRunner):
    def __init__(self, tasks_root: Path):
        self.tasks_root = tasks_root

    def _pending_prompt_path(self, task_id: str) -> Path:
        return self.tasks_root / task_id / "pending_prompt.md"

    def run_agent(
        self,
        role: str,
        task_context: dict,
        input_artifacts: list[Path],
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> AgentResult:
        task_id = task_context["task_id"]
        frontmatter = {
            "role": role,
            "task_id": task_id,
            "input_artifacts": [str(p) for p in input_artifacts],
            "workspace_path": str(workspace_path) if workspace_path else None,
            "allowed_capabilities": list(allowed_capabilities),
        }
        body_lines = [
            f"# Manual stage: {role}",
            "",
            "Run this stage yourself (e.g. in an interactive Claude Code session, "
            "using the matching agents/<role>.md definition), then submit the "
            "resulting artifact by calling ManualRunner.complete_pending(...).",
            "",
            "## Task context",
            f"- task_id: {task_id}",
            f"- request: {task_context.get('request', '')}",
        ]
        path = self._pending_prompt_path(task_id)
        artifacts.write_markdown(path, frontmatter, "\n".join(body_lines))
        return AgentResult(
            status=AgentResultStatus.PENDING,
            runner_name="manual",
            notes=f"awaiting manual submission at {path}",
        )

    def complete_pending(self, task_id: str, output_artifact_path: Path) -> AgentResult:
        """Called once a human has produced the stage's artifact by hand."""
        start = time.monotonic()
        pending_path = self._pending_prompt_path(task_id)
        if not pending_path.exists():
            raise FileNotFoundError(f"No pending manual stage for task {task_id}")
        if not output_artifact_path.exists():
            raise FileNotFoundError(f"Submitted artifact does not exist: {output_artifact_path}")
        pending_path.unlink()
        return AgentResult(
            status=AgentResultStatus.OK,
            output_artifact_path=output_artifact_path,
            runner_name="manual",
            duration_seconds=time.monotonic() - start,
        )

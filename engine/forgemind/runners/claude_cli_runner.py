"""Real, non-interactive Claude Code CLI adapter -- analyst,
architect_planner, implementer, tester, reviewer, and finalizer roles.

One `run_agent()` call launches exactly one short-lived, bounded `claude -p`
process and returns. Claude never decides what happens next: this class
only ever reports a fact (ok/error/timeout) about that one process, exactly
like ManualRunner reports a fact about a human's submission. The
orchestrator interprets the result identically either way -- including
which governance checkpoint (if any) applies, which remains entirely
orchestrator.py's concern, not this runner's.

All Claude-specific behavior (command construction, subprocess handling,
locating the resulting artifact) lives in this file only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner


def _kill_process_tree(pid: int) -> None:
    """Best-effort termination of a process and everything it spawned.

    subprocess.run()'s own timeout handling only kills the ONE process it
    directly launched -- on Windows that's cmd.exe (claude is a .CMD
    shim), which itself launched node.exe running the real Claude process,
    which may itself have started a shell command (a dev server, a test
    runner, ...). Killing just the top process leaves everything below it
    running and orphaned, which is exactly what left a real Tester run
    stuck: the Claude process it launched a long-running command that
    never returned, subprocess.run()'s kill()-on-timeout only reaped the
    shim, and the actual work kept running forever underneath it.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True)
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _launch(
    command: list[str],
    *,
    shell: bool,
    input: Optional[str],
    capture_output: bool,
    text: bool,
    timeout: float,
    cwd: Optional[str],
) -> subprocess.CompletedProcess:
    """Drop-in replacement for subprocess.run(command, shell=..., input=...,
    capture_output=..., text=..., timeout=..., cwd=...) with one crucial
    difference: on timeout, it kills the entire process tree it spawned
    (see _kill_process_tree) instead of only the immediate child, so a
    hung Claude invocation can never block the pipeline past
    `timeout` or leave orphaned processes running after it.
    """
    assert shell is False, "claude_cli_runner must never launch with shell=True"
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        cwd=cwd,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=(os.name != "nt"),
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        # Reap the now-dead tree so we don't leak a zombie/handle; the
        # process is already gone at this point, so this should return
        # almost immediately -- the short timeout is only a last-resort
        # safety net, not something callers wait on.
        try:
            proc.communicate(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


# Only the roles ClaudeCodeCLIRunner actually supports so far. Deliberately
# not imported from orchestrator.ARTIFACT_FILENAMES -- runners must not
# depend on the orchestrator; the dependency direction is orchestrator ->
# runner, never the reverse.
_SUPPORTED_ROLE_ARTIFACT_FILENAMES: dict[str, str] = {
    "analyst": "01_analysis.md",
    "architect_planner": "02_design_plan.md",
    "implementer": "03_implementation.md",
    "tester": "04_test_report.md",
    "reviewer": "05_review.md",
    "finalizer": "06_final_report.md",
}


class ClaudeCLIUnavailableError(Exception):
    """Raised internally when the CLI executable or role file can't be found."""


class ClaudeCodeCLIRunner(AgentRunner):
    def __init__(
        self,
        artifacts_root: Path,
        agents_dir: Path,
        executable: str = "claude",
        timeout_seconds: int = 600,
    ):
        self.artifacts_root = artifacts_root
        self.agents_dir = agents_dir
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def _resolve_executable(self) -> str:
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise ClaudeCLIUnavailableError(
                f"Claude CLI executable '{self.executable}' was not found on PATH."
            )
        return resolved

    def _load_role_definition(self, role: str) -> str:
        role_path = self.agents_dir / f"{role}.md"
        if not role_path.exists():
            raise ClaudeCLIUnavailableError(f"No role definition found for '{role}' at {role_path}")
        return role_path.read_text(encoding="utf-8")

    def _expected_output_path(self, task_id: str, role: str) -> Path:
        if role not in _SUPPORTED_ROLE_ARTIFACT_FILENAMES:
            raise ClaudeCLIUnavailableError(
                f"ClaudeCodeCLIRunner does not implement role '{role}' yet "
                f"(supported: {sorted(_SUPPORTED_ROLE_ARTIFACT_FILENAMES)})"
            )
        return self.artifacts_root / task_id / _SUPPORTED_ROLE_ARTIFACT_FILENAMES[role]

    @staticmethod
    def _build_prompt(
        task_context: dict,
        output_path: Path,
        input_artifacts: list[Path],
        workspace_path: Optional[Path] = None,
    ) -> str:
        lines = [
            f"Task ID: {task_context.get('task_id')}",
            f"Request: {task_context.get('request')}",
            "",
        ]
        if workspace_path is not None:
            lines.append(f"Workspace directory (make any file changes here): {workspace_path}")
            lines.append("")
        if input_artifacts:
            lines.append("Input artifacts you may read:")
            lines.extend(f"- {p}" for p in input_artifacts)
        else:
            lines.append("No prior artifacts are available yet.")
        lines += [
            "",
            f"Write your complete output to exactly this file path: {output_path}",
            "The file must be Markdown with YAML frontmatter containing at least a "
            "'status' field (e.g. status: ok), then a blank line, then your output "
            "as the body.",
        ]
        return "\n".join(lines)

    def _build_command(
        self,
        executable: str,
        role_definition_path: Path,
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> list[str]:
        """Build the argv list only -- the prompt itself is never one of
        these elements. See run_agent() for why: on Windows, `claude` is a
        `.CMD` shim, so subprocess.run(..., shell=False) launches it via
        cmd.exe's batch-argument handling, which silently truncates any
        single argument at its first embedded newline. Our prompts are
        always multi-line, so passing one as an argv element (as earlier
        phases did) corrupts it -- Claude would only ever see the text
        before the first line break. Piping the prompt over stdin instead
        sidesteps that argument path entirely, on every platform.

        The same problem applies to --append-system-prompt with inline
        text (our role definitions are multi-paragraph markdown), so this
        uses --append-system-prompt-file with the role file's own path
        instead of its loaded content.
        """
        command = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--append-system-prompt-file",
            str(role_definition_path),
            "--no-session-persistence",
        ]
        if allowed_capabilities:
            command += ["--allowedTools", " ".join(allowed_capabilities)]
        if workspace_path is not None:
            command += ["--add-dir", str(workspace_path)]
        return command

    def run_agent(
        self,
        role: str,
        task_context: dict,
        input_artifacts: list[Path],
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> AgentResult:
        start = time.monotonic()
        task_id = task_context["task_id"]

        try:
            executable = self._resolve_executable()
            self._load_role_definition(role)  # existence check; raises if missing
            role_definition_path = self.agents_dir / f"{role}.md"
            output_path = self._expected_output_path(task_id, role)
        except ClaudeCLIUnavailableError as exc:
            return AgentResult(
                status=AgentResultStatus.ERROR,
                runner_name="claude_cli",
                duration_seconds=time.monotonic() - start,
                notes=str(exc),
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = self._build_prompt(task_context, output_path, input_artifacts, workspace_path)
        command = self._build_command(
            executable, role_definition_path, workspace_path, allowed_capabilities
        )

        try:
            completed = _launch(
                command,
                shell=False,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd=str(workspace_path) if workspace_path is not None else None,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                status=AgentResultStatus.TIMEOUT,
                runner_name="claude_cli",
                duration_seconds=time.monotonic() - start,
                notes=f"claude CLI did not finish within {self.timeout_seconds}s",
            )
        except OSError as exc:
            return AgentResult(
                status=AgentResultStatus.ERROR,
                runner_name="claude_cli",
                duration_seconds=time.monotonic() - start,
                notes=f"failed to launch claude CLI: {exc}",
            )

        log_path = self.artifacts_root / task_id / f"_claude_cli_{role}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"command={command}\n\n"
            f"--- stdin (prompt) ---\n{prompt}\n\n"
            f"exit_code={completed.returncode}\n\n"
            f"--- stdout ---\n{completed.stdout}\n\n"
            f"--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )

        duration = time.monotonic() - start

        if completed.returncode != 0:
            return AgentResult(
                status=AgentResultStatus.ERROR,
                raw_log_path=log_path,
                duration_seconds=duration,
                runner_name="claude_cli",
                notes=f"claude CLI exited with code {completed.returncode}",
            )

        if not output_path.exists():
            return AgentResult(
                status=AgentResultStatus.ERROR,
                raw_log_path=log_path,
                duration_seconds=duration,
                runner_name="claude_cli",
                notes=f"expected artifact was not created: {output_path}",
            )

        return AgentResult(
            status=AgentResultStatus.OK,
            output_artifact_path=output_path,
            raw_log_path=log_path,
            duration_seconds=duration,
            runner_name="claude_cli",
        )

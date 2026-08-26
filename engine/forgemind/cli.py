"""CLI foundation.

Phase 1 added:
  forgemind new "<request>"
  forgemind status <task_id>

Phase 2 adds the human-driven workflow commands:
  forgemind submit-artifact <task_id> <stage> <path>
  forgemind approve <task_id>
  forgemind reject <task_id> --reason "<reason>"

Phase 10 adds the automatic pipeline entry point:
  forgemind run <task_id> [--workspace <path>]

`new`, `status`, `submit-artifact`, `approve`, and `reject` never make an
AI call -- they only touch the deterministic engine. `run` does: it drives
orchestrator.run_full_pipeline() using whichever runner config/forgemind.yaml
selects.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from forgemind import governance, locking, orchestrator
from forgemind import task as task_module
from forgemind.config import load_config
from forgemind.runners.claude_cli_runner import ClaudeCodeCLIRunner
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine


def _repo_root() -> Path:
    # engine/forgemind/cli.py -> engine/forgemind -> engine -> repo root
    return Path(__file__).resolve().parents[2]


def cmd_new(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    artifacts_root = repo_root / "artifacts"
    try:
        task = task_module.create_task(tasks_root, artifacts_root, args.request)
    except locking.TaskAlreadyActiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(task.task_id)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    try:
        sm = TaskStateMachine.load(tasks_root, args.task_id)
    except FileNotFoundError:
        print(f"error: no such task: {args.task_id}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "task_id": sm.task_id,
                "state": sm.state.value,
                "blocked_from": sm.blocked_from.value if sm.blocked_from else None,
                "implementation_retry_count": sm.implementation_retry_count,
                "revision_retry_count": sm.revision_retry_count,
                "created_at": sm.created_at,
                "updated_at": sm.updated_at,
            },
            indent=2,
        )
    )
    return 0


def cmd_submit_artifact(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    artifacts_root = repo_root / "artifacts"
    config = load_config(repo_root / "config" / "forgemind.yaml")
    governance_config = governance.load_governance(repo_root / "config" / "governance.yaml")
    runner = ManualRunner(tasks_root)

    try:
        sm = TaskStateMachine.load(tasks_root, args.task_id)
    except FileNotFoundError:
        print(f"error: no such task: {args.task_id}", file=sys.stderr)
        return 1

    errors = orchestrator.submit_artifact(
        sm,
        artifacts_root,
        args.stage,
        Path(args.path),
        runner,
        config,
        governance_config,
    )
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"submitted '{args.stage}' artifact for task {args.task_id}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    try:
        sm = TaskStateMachine.load(tasks_root, args.task_id)
    except FileNotFoundError:
        print(f"error: no such task: {args.task_id}", file=sys.stderr)
        return 1
    errors = orchestrator.approve_task(sm)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"task {args.task_id} approved -> {sm.state.value}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    try:
        sm = TaskStateMachine.load(tasks_root, args.task_id)
    except FileNotFoundError:
        print(f"error: no such task: {args.task_id}", file=sys.stderr)
        return 1
    errors = orchestrator.reject_task(sm, args.reason)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"task {args.task_id} rejected -> {sm.state.value}")
    return 0


def _build_runner(config, tasks_root: Path, artifacts_root: Path, agents_dir: Path):
    """Construct whichever AgentRunner config/forgemind.yaml's `runner`
    field selects. This is the first CLI command where that field actually
    matters -- submit-artifact always uses ManualRunner directly, since
    that command *is* the ManualRunner workflow."""
    if config.runner == "claude_cli":
        return ClaudeCodeCLIRunner(
            artifacts_root,
            agents_dir,
            executable=config.claude_cli_executable,
            timeout_seconds=config.stage_timeout_seconds,
        )
    if config.runner == "manual":
        return ManualRunner(tasks_root)
    raise ValueError(f"unknown runner in config: {config.runner!r} (expected 'claude_cli' or 'manual')")


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    tasks_root = repo_root / "tasks"
    artifacts_root = repo_root / "artifacts"
    config = load_config(repo_root / "config" / "forgemind.yaml")
    governance_config = governance.load_governance(repo_root / "config" / "governance.yaml")

    try:
        sm = TaskStateMachine.load(tasks_root, args.task_id)
    except FileNotFoundError:
        print(f"error: no such task: {args.task_id}", file=sys.stderr)
        return 1

    try:
        runner = _build_runner(config, tasks_root, artifacts_root, repo_root / "agents")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    workspace_path = Path(args.workspace) if args.workspace else None

    outcome = orchestrator.run_full_pipeline(
        sm, runner, artifacts_root, workspace_path, config, governance_config
    )

    print(
        json.dumps(
            {
                "task_id": sm.task_id,
                "stopped_state": outcome.stopped_state.value,
                "reason": outcome.reason,
                "iterations": outcome.iterations,
            },
            indent=2,
        )
    )

    if outcome.stopped_state == TaskState.COMPLETED or outcome.reason == "awaiting_human_action":
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgemind")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="Create a new task")
    new_parser.add_argument("request", help="The feature/bug/task request text")
    new_parser.set_defaults(func=cmd_new)

    status_parser = subparsers.add_parser("status", help="Show a task's current state")
    status_parser.add_argument("task_id")
    status_parser.set_defaults(func=cmd_status)

    submit_parser = subparsers.add_parser(
        "submit-artifact", help="Submit a manually produced artifact for the currently pending stage"
    )
    submit_parser.add_argument("task_id")
    submit_parser.add_argument("stage", choices=sorted(orchestrator.STAGE_MAP))
    submit_parser.add_argument("path")
    submit_parser.set_defaults(func=cmd_submit_artifact)

    approve_parser = subparsers.add_parser(
        "approve", help="Approve a task awaiting plan or final approval"
    )
    approve_parser.add_argument("task_id")
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser(
        "reject", help="Reject a task awaiting plan or final approval"
    )
    reject_parser.add_argument("task_id")
    reject_parser.add_argument("--reason", required=True)
    reject_parser.set_defaults(func=cmd_reject)

    run_parser = subparsers.add_parser(
        "run", help="Run the automatic pipeline for an existing task until it stops"
    )
    run_parser.add_argument("task_id")
    run_parser.add_argument(
        "--workspace",
        default=None,
        help="Path to the project/workspace directory the pipeline should operate on",
    )
    run_parser.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

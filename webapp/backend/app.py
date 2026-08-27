"""ForgeMind website interface -- thin local API bridge.

Phase 15. This module contains no pipeline/business logic of its own: every
route is a direct call into the existing, already-tested
forgemind.orchestrator / forgemind.task / forgemind.governance /
forgemind.state_machine functions, using the exact same
forgemind.cli._build_runner() the CLI itself uses to pick a runner. The
CLI (engine/forgemind/cli.py) is untouched and continues to work exactly
as before -- this is a second, independent consumer of the same library,
not a replacement.

Why a background thread per run: orchestrator.run_full_pipeline() is a
single, synchronous, potentially long-running call (each ClaudeCodeCLIRunner
stage can block for up to stage_timeout_seconds). Running it inline in a
Flask request would block the browser's "Start" click for the whole
pipeline. Instead each run happens on a daemon thread, and the browser
polls GET /api/tasks/<id> to observe progress -- which works because
TaskStateMachine.transition() already persists state.json to disk
synchronously on every single transition (see state_machine.py), including
the "in progress" transition BEFORE the runner is even invoked. No new
progress/event mechanism was added to the orchestrator; polling the
already-real, already-persisted state is sufficient and genuine.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory

from forgemind import artifacts, cli, governance, locking, orchestrator
from forgemind import task as task_module
from forgemind.config import load_config
from forgemind.locking import TaskAlreadyActiveError
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TaskState, TaskStateMachine, TERMINAL_STATES

import storage

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def _repo_root() -> Path:
    # webapp/backend/app.py -> webapp/backend -> webapp -> repo root
    return Path(__file__).resolve().parents[2]


# Guards against starting a second run_full_pipeline() thread for a task
# that already has one in flight. run_full_pipeline() is explicitly safe to
# call again on a task that previously *stopped* (see its own docstring),
# but never safe to call twice *concurrently* against the same state.json.
_running_lock = threading.Lock()
_running_tasks: set[str] = set()


def create_app(repo_root: Optional[Path] = None) -> Flask:
    root = repo_root or _repo_root()
    tasks_root = root / "tasks"
    artifacts_root = root / "artifacts"
    agents_dir = root / "agents"
    config_path = root / "config" / "forgemind.yaml"
    governance_path = root / "config" / "governance.yaml"

    app = Flask(__name__, static_folder=None)
    app.config.update(
        TASKS_ROOT=tasks_root,
        ARTIFACTS_ROOT=artifacts_root,
        AGENTS_DIR=agents_dir,
        CONFIG_PATH=config_path,
        GOVERNANCE_PATH=governance_path,
    )

    def _cfg():
        return load_config(app.config["CONFIG_PATH"])

    def _gov():
        return governance.load_governance(app.config["GOVERNANCE_PATH"])

    def _workspace_file(task_id: str) -> Path:
        return app.config["TASKS_ROOT"] / task_id / "web_workspace.txt"

    def _saved_workspace(task_id: str) -> Optional[Path]:
        p = _workspace_file(task_id)
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8").strip()
        return Path(text) if text else None

    def _error_log_path(task_id: str) -> Path:
        return app.config["TASKS_ROOT"] / task_id / "web_run_error.log"

    def _run_pipeline_background(task_id: str) -> None:
        tasks_root_ = app.config["TASKS_ROOT"]
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        agents_dir_ = app.config["AGENTS_DIR"]
        try:
            config = _cfg()
            gconfig = _gov()
            sm = TaskStateMachine.load(tasks_root_, task_id)
            runner = cli._build_runner(config, tasks_root_, artifacts_root_, agents_dir_)
            workspace_path = _saved_workspace(task_id)
            orchestrator.run_full_pipeline(sm, runner, artifacts_root_, workspace_path, config, gconfig)
            # A clean stop (terminal or awaiting-human) is not an error --
            # clear any earlier error record so the UI doesn't show a stale
            # failure banner after a successful retry.
            err_path = _error_log_path(task_id)
            if err_path.exists():
                err_path.unlink()
        except Exception:
            # Never let a background-thread crash disappear silently -- a
            # task the UI thinks is "still running" but whose thread died
            # would be a false-success-by-omission. Record the real
            # traceback so /api/tasks/<id> can surface it honestly.
            _error_log_path(task_id).write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            with _running_lock:
                _running_tasks.discard(task_id)
            storage.sync_task(tasks_root_, artifacts_root_, task_id)

    def _start_background_run(task_id: str) -> bool:
        """Returns False (does nothing) if a run is already in flight for
        this task -- the caller should treat that as success-no-op, not an
        error, since the existing run will reach the same result."""
        with _running_lock:
            if task_id in _running_tasks:
                return False
            _running_tasks.add(task_id)
        thread = threading.Thread(target=_run_pipeline_background, args=(task_id,), daemon=True)
        thread.start()
        return True

    def _sync(task_id: str) -> None:
        storage.sync_task(app.config["TASKS_ROOT"], app.config["ARTIFACTS_ROOT"], task_id)

    def _load_sm(task_id: str) -> TaskStateMachine:
        tasks_root_ = app.config["TASKS_ROOT"]
        if not (tasks_root_ / task_id / "state.json").exists():
            storage.restore_task(tasks_root_, app.config["ARTIFACTS_ROOT"], task_id)
        return TaskStateMachine.load(tasks_root_, task_id)

    def _status_payload(task_id: str) -> dict:
        sm = _load_sm(task_id)
        with _running_lock:
            running = task_id in _running_tasks
        err_path = _error_log_path(task_id)
        try:
            request_text = task_module.read_task_request(app.config["TASKS_ROOT"], task_id)
        except FileNotFoundError:
            request_text = ""
        return {
            "task_id": sm.task_id,
            "request": request_text,
            "state": sm.state.value,
            "blocked_from": sm.blocked_from.value if sm.blocked_from else None,
            "implementation_retry_count": sm.implementation_retry_count,
            "revision_retry_count": sm.revision_retry_count,
            "created_at": sm.created_at,
            "updated_at": sm.updated_at,
            "history": sm.history,
            "running": running,
            "is_terminal": sm.state in TERMINAL_STATES,
            "error": err_path.read_text(encoding="utf-8") if err_path.exists() else None,
        }

    # -- Static frontend -----------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(_FRONTEND_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(_FRONTEND_DIR, filename)

    # -- Config -----------------------------------------------------------
    @app.get("/api/config")
    def get_config():
        config = _cfg()
        return jsonify(
            {
                "runner": config.runner,
                "claude_cli_available": shutil.which(config.claude_cli_executable) is not None,
                "claude_cli_executable": config.claude_cli_executable,
                "max_implementation_retries": config.max_implementation_retries,
                "max_revision_retries": config.max_revision_retries,
            }
        )

    # -- Task list ----------------------------------------------------------
    @app.get("/api/tasks")
    def list_tasks():
        tasks_root_ = app.config["TASKS_ROOT"]
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        tasks_root_.mkdir(parents=True, exist_ok=True)

        task_ids = {entry.name for entry in tasks_root_.iterdir() if entry.is_dir()}
        task_ids.update(storage.list_known_task_ids())  # tasks that survived a restart in DB only

        out = []
        for task_id in task_ids:
            if not (tasks_root_ / task_id / "state.json").exists():
                if not storage.restore_task(tasks_root_, artifacts_root_, task_id):
                    continue
            try:
                sm = TaskStateMachine.load(tasks_root_, task_id)
                request_text = task_module.read_task_request(tasks_root_, task_id)
            except (FileNotFoundError, OSError):
                continue
            out.append(
                {
                    "task_id": sm.task_id,
                    "request": request_text,
                    "state": sm.state.value,
                    "created_at": sm.created_at,
                    "updated_at": sm.updated_at,
                    "is_terminal": sm.state in TERMINAL_STATES,
                }
            )
        out.sort(key=lambda t: t["created_at"], reverse=True)
        return jsonify(out)

    # -- Create + start a task ----------------------------------------------
    @app.post("/api/tasks")
    def create_task_route():
        body = request.get_json(silent=True) or {}
        request_text = (body.get("request") or "").strip()
        if not request_text:
            return jsonify({"error": "request text is required"}), 400

        workspace_raw = (body.get("workspace_path") or "").strip()
        workspace_path: Optional[Path] = None
        if workspace_raw:
            workspace_path = Path(workspace_raw)
            if not workspace_path.is_dir():
                return jsonify({"error": f"workspace_path does not exist or is not a directory: {workspace_raw}"}), 400

        tasks_root_ = app.config["TASKS_ROOT"]
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        try:
            task = task_module.create_task(tasks_root_, artifacts_root_, request_text)
        except TaskAlreadyActiveError as exc:
            return jsonify({"error": str(exc)}), 409

        if workspace_path is not None:
            wf = _workspace_file(task.task_id)
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(str(workspace_path), encoding="utf-8")

        _sync(task.task_id)
        _start_background_run(task.task_id)
        return jsonify({"task_id": task.task_id}), 201

    # -- Status ---------------------------------------------------------
    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str):
        try:
            return jsonify(_status_payload(task_id))
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404

    # -- Resume a stopped run (after approve, or to retry a failed launch) --
    @app.post("/api/tasks/<task_id>/run")
    def run_task(task_id: str):
        try:
            _load_sm(task_id)  # 404s before touching the thread registry
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404
        started = _start_background_run(task_id)
        return jsonify({"started": started, **_status_payload(task_id)})

    # -- Governance: approve / reject ---------------------------------------
    @app.post("/api/tasks/<task_id>/approve")
    def approve_task_route(task_id: str):
        try:
            sm = _load_sm(task_id)
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404
        errors = orchestrator.approve_task(sm)
        if errors:
            return jsonify({"errors": errors}), 400
        _sync(task_id)
        _start_background_run(task_id)
        return jsonify(_status_payload(task_id))

    @app.post("/api/tasks/<task_id>/reject")
    def reject_task_route(task_id: str):
        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "").strip()
        try:
            sm = _load_sm(task_id)
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404
        errors = orchestrator.reject_task(sm, reason)
        if errors:
            return jsonify({"errors": errors}), 400
        _sync(task_id)
        return jsonify(_status_payload(task_id))

    # -- Manual-runner artifact submission -----------------------------------
    @app.post("/api/tasks/<task_id>/submit-artifact")
    def submit_artifact_route(task_id: str):
        body = request.get_json(silent=True) or {}
        stage = (body.get("stage") or "").strip()
        status = (body.get("status") or "").strip()
        artifact_body = (body.get("body") or "").strip()

        if stage not in orchestrator.STAGE_MAP:
            return jsonify({"error": f"unknown stage: {stage} (expected one of {sorted(orchestrator.STAGE_MAP)})"}), 400
        if not status:
            return jsonify({"error": "status is required"}), 400
        if not artifact_body:
            return jsonify({"error": "body is required"}), 400

        try:
            sm = _load_sm(task_id)
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404

        tasks_root_ = app.config["TASKS_ROOT"]
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        config = _cfg()
        gconfig = _gov()
        runner = ManualRunner(tasks_root_)

        fd, tmp_name = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            artifacts.write_markdown(tmp_path, {"status": status}, artifact_body)
            errors = orchestrator.submit_artifact(
                sm, artifacts_root_, stage, tmp_path, runner, config, gconfig
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        if errors:
            return jsonify({"errors": errors}), 400
        _sync(task_id)
        _start_background_run(task_id)
        return jsonify(_status_payload(task_id))

    # -- Delete ------------------------------------------------------------
    @app.delete("/api/tasks/<task_id>")
    def delete_task_route(task_id: str):
        tasks_root_ = app.config["TASKS_ROOT"]
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        try:
            _load_sm(task_id)  # 404s if truly unknown anywhere (disk or DB)
        except FileNotFoundError:
            return jsonify({"error": f"no such task: {task_id}"}), 404

        with _running_lock:
            if task_id in _running_tasks:
                return jsonify({"error": "task is currently running; wait for it to stop before deleting"}), 409

        locking.release(tasks_root_, task_id)  # no-op unless this task held the lock
        shutil.rmtree(tasks_root_ / task_id, ignore_errors=True)
        shutil.rmtree(artifacts_root_ / task_id, ignore_errors=True)
        storage.delete_task(task_id)
        return jsonify({"deleted": task_id})

    # -- Artifacts / logs -----------------------------------------------
    @app.get("/api/tasks/<task_id>/artifacts")
    def get_artifacts(task_id: str):
        artifacts_root_ = app.config["ARTIFACTS_ROOT"]
        task_artifacts_dir = artifacts_root_ / task_id
        produced = []
        for role, filename in orchestrator.ARTIFACT_FILENAMES.items():
            path = task_artifacts_dir / filename
            if not path.exists():
                continue
            try:
                frontmatter, body = artifacts.read_markdown(path)
            except artifacts.ArtifactValidationError as exc:
                produced.append({"role": role, "filename": filename, "error": str(exc)})
                continue
            produced.append(
                {"role": role, "filename": filename, "frontmatter": frontmatter, "body": body}
            )

        logs = []
        if task_artifacts_dir.exists():
            for log_path in sorted(task_artifacts_dir.glob("_claude_cli_*.log")):
                logs.append({"filename": log_path.name, "content": log_path.read_text(encoding="utf-8")})

        return jsonify({"artifacts": produced, "logs": logs})

    return app


# WSGI entry point for gunicorn (`gunicorn app:app`), e.g. on Render.
app = create_app()


def main() -> None:
    # Local default stays 127.0.0.1-only: this process can execute local
    # shell commands and read/write the filesystem on behalf of the
    # pipeline, so a plain `python app.py` run must never be reachable from
    # the network. PORT is set by hosts like Render, never by a local dev
    # run, so its presence is what opts into a public bind -- gunicorn is
    # the actual production server there, this branch is only a fallback.
    port_env = os.environ.get("PORT")
    if port_env:
        app.run(host="0.0.0.0", port=int(port_env), debug=False)
    else:
        app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()

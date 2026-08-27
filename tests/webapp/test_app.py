"""Integration tests for the Phase 15 website API bridge.

These deliberately use the *real* forgemind package (orchestrator, task,
governance, state_machine) and the real ManualRunner -- exactly what a
user gets with runner: manual -- so a passing test here means the bridge
genuinely drives the real pipeline, not a mock of it. Only the repo root
(tasks/artifacts/config dirs) is faked, via tmp_path, exactly like
tests/engine/test_cli.py does for the CLI.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import app as web_app  # from tests/webapp/conftest.py's sys.path insert

_FORGEMIND_YAML = (
    "runner: manual\n"
    "retry_limits:\n"
    "  max_implementation_retries: 2\n"
    "  max_revision_retries: 2\n"
)
_GOVERNANCE_YAML = (
    "sensitive_patterns:\n"
    "  - name: git_push\n"
    '    regex: "git\\\\s+push"\n'
    '    reason: "Pushes commits to a remote"\n'
    "approval_required_stages:\n"
    "  - plan\n"
    "  - final\n"
)


def _init_fake_repo(root: Path) -> None:
    (root / "tasks").mkdir()
    (root / "artifacts").mkdir()
    (root / "agents").mkdir()
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "forgemind.yaml").write_text(_FORGEMIND_YAML, encoding="utf-8")
    (config_dir / "governance.yaml").write_text(_GOVERNANCE_YAML, encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path):
    _init_fake_repo(tmp_path)
    app = web_app.create_app(tmp_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _wait_until_idle(client, task_id: str, timeout: float = 5.0) -> dict:
    """Poll status until the background run_full_pipeline() thread has
    finished, exactly like a real browser would poll -- run_full_pipeline
    itself is synchronous, but it happens on a background thread here."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/tasks/{task_id}")
        last = resp.get_json()
        if not last["running"]:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} still running after {timeout}s: {last}")


def _submit(client, task_id: str, stage: str, status: str = "ok", body: str = "content"):
    resp = client.post(
        f"/api/tasks/{task_id}/submit-artifact",
        json={"stage": stage, "status": status, "body": body},
    )
    assert resp.status_code == 200, resp.get_json()
    return _wait_until_idle(client, task_id)


def test_config_reflects_real_forgemind_yaml(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["runner"] == "manual"
    assert data["max_implementation_retries"] == 2


def test_create_task_reaches_blocked_awaiting_analyst(client):
    resp = client.post("/api/tasks", json={"request": "Add a login page"})
    assert resp.status_code == 201
    task_id = resp.get_json()["task_id"]

    status = _wait_until_idle(client, task_id)
    assert status["state"] == "BLOCKED"
    assert status["blocked_from"] == "ANALYZING"
    assert status["request"] == "Add a login page"
    assert status["error"] is None


def test_create_task_requires_request_text(client):
    resp = client.post("/api/tasks", json={"request": "  "})
    assert resp.status_code == 400


def test_create_task_rejects_nonexistent_workspace(client):
    resp = client.post("/api/tasks", json={"request": "x", "workspace_path": "Z:/does/not/exist"})
    assert resp.status_code == 400


def test_second_task_while_one_active_is_rejected(client):
    r1 = client.post("/api/tasks", json={"request": "first"})
    assert r1.status_code == 201
    _wait_until_idle(client, r1.get_json()["task_id"])

    r2 = client.post("/api/tasks", json={"request": "second"})
    assert r2.status_code == 409
    assert "already active" in r2.get_json()["error"]


def test_unknown_task_returns_404_everywhere(client):
    assert client.get("/api/tasks/does-not-exist").status_code == 404
    assert client.post("/api/tasks/does-not-exist/run").status_code == 404
    assert client.post("/api/tasks/does-not-exist/approve").status_code == 404
    assert client.post("/api/tasks/does-not-exist/reject", json={"reason": "x"}).status_code == 404


def test_full_manual_pipeline_via_api_reaches_completed(client):
    """The real, non-mocked end-to-end walkthrough: create -> submit each
    stage's artifact through the API -> non-sensitive plan/result auto-clear
    governance -> COMPLETED, with all six artifacts on disk."""
    task_id = client.post("/api/tasks", json={"request": "Non-sensitive task"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)

    status = _submit(client, task_id, "analyst")
    assert status["state"] == "BLOCKED"
    assert status["blocked_from"] == "DESIGNING"

    # Non-sensitive plan text auto-clears governance (no pause), and the
    # submit-artifact endpoint auto-continues the pipeline in the
    # background, so by the time it's idle again it has already advanced
    # all the way through PLAN_APPROVED into the next BLOCKED stage.
    status = _submit(client, task_id, "architect_planner", body="a trivial, non-sensitive plan")
    assert status["state"] == "BLOCKED"
    assert status["blocked_from"] == "IMPLEMENTING"

    status = _submit(client, task_id, "implementer")
    assert status["blocked_from"] == "TESTING"

    status = _submit(client, task_id, "tester")
    assert status["blocked_from"] == "REVIEWING"

    # Same auto-clear + auto-continue pattern as the plan checkpoint above.
    status = _submit(client, task_id, "reviewer")
    assert status["state"] == "BLOCKED"
    assert status["blocked_from"] == "FINALIZING"

    status = _submit(client, task_id, "finalizer")
    assert status["state"] == "COMPLETED"
    assert status["is_terminal"] is True

    artifacts_resp = client.get(f"/api/tasks/{task_id}/artifacts")
    produced = artifacts_resp.get_json()["artifacts"]
    assert {a["role"] for a in produced} == {
        "analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer",
    }
    for a in produced:
        assert a["frontmatter"]["status"] == "ok"


def test_sensitive_plan_pauses_then_approve_resumes(client):
    task_id = client.post("/api/tasks", json={"request": "Sensitive task"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)
    _submit(client, task_id, "analyst")

    status = _submit(client, task_id, "architect_planner", body="this plan requires a git push to deploy")
    assert status["state"] == "AWAITING_PLAN_APPROVAL"

    # run must not silently bypass an unapproved governance gate
    run_resp = client.post(f"/api/tasks/{task_id}/run")
    status = run_resp.get_json()
    assert status["state"] == "AWAITING_PLAN_APPROVAL"

    approve_resp = client.post(f"/api/tasks/{task_id}/approve")
    assert approve_resp.status_code == 200
    status = _wait_until_idle(client, task_id)
    assert status["blocked_from"] == "IMPLEMENTING"


def test_reject_lands_terminal_and_run_does_not_fake_progress(client):
    task_id = client.post("/api/tasks", json={"request": "Reject me"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)
    _submit(client, task_id, "analyst")
    _submit(client, task_id, "architect_planner", body="git push required here")

    reject_resp = client.post(f"/api/tasks/{task_id}/reject", json={"reason": "not needed"})
    assert reject_resp.status_code == 200
    status = reject_resp.get_json()
    assert status["state"] == "PLAN_REJECTED"
    assert status["is_terminal"] is True

    run_resp = client.post(f"/api/tasks/{task_id}/run")
    status = _wait_until_idle(client, task_id)
    assert status["state"] == "PLAN_REJECTED"  # unchanged -- reject is final, run is a safe no-op


def test_reject_without_reason_is_rejected(client):
    task_id = client.post("/api/tasks", json={"request": "x"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)
    _submit(client, task_id, "analyst")
    _submit(client, task_id, "architect_planner", body="git push required")

    resp = client.post(f"/api/tasks/{task_id}/reject", json={"reason": ""})
    assert resp.status_code == 400


def test_submit_artifact_rejects_unknown_stage(client):
    task_id = client.post("/api/tasks", json={"request": "x"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)
    resp = client.post(
        f"/api/tasks/{task_id}/submit-artifact",
        json={"stage": "not_a_real_stage", "status": "ok", "body": "x"},
    )
    assert resp.status_code == 400


def test_submit_artifact_rejects_when_task_not_blocked(client):
    task_id = client.post("/api/tasks", json={"request": "x"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)
    _submit(client, task_id, "analyst")  # now BLOCKED on architect_planner, not analyst

    resp = client.post(
        f"/api/tasks/{task_id}/submit-artifact",
        json={"stage": "analyst", "status": "ok", "body": "x"},
    )
    assert resp.status_code == 400


def test_list_tasks_reflects_real_state(client):
    task_id = client.post("/api/tasks", json={"request": "listed task"}).get_json()["task_id"]
    _wait_until_idle(client, task_id)

    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.get_json()
    assert any(t["task_id"] == task_id and t["state"] == "BLOCKED" for t in tasks)


def test_index_serves_frontend(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"ForgeMind" in resp.data

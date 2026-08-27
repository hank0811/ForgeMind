"""Optional Postgres-backed persistence for the web app.

Render's filesystem is ephemeral -- tasks/ and artifacts/ (both plain
files, exactly like the CLI writes them) disappear on every restart or
redeploy. This module is a thin sync layer on top of that, not a
replacement for it: the engine, CLI, and tests keep reading/writing the
same local files exactly as before. When DATABASE_URL is set, every
state-changing web request also mirrors that task's files into one row
of a `forgemind_tasks` table, and any read for a task missing locally
first restores it from that row. With DATABASE_URL unset (local dev,
the existing test suite), every function here is a no-op -- behavior is
byte-for-byte the same as before this module existed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_TABLE = "forgemind_tasks"


def is_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _connect():
    import psycopg  # imported lazily: only ever needed when DATABASE_URL is set

    return psycopg.connect(os.environ["DATABASE_URL"])


def ensure_schema() -> None:
    if not is_enabled():
        return
    with _connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                task_id TEXT PRIMARY KEY,
                files JSONB NOT NULL,
                deleted BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()


def _collect_files(tasks_root: Path, artifacts_root: Path, task_id: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for root, prefix in ((tasks_root, "tasks"), (artifacts_root, "artifacts")):
        task_dir = root / task_id
        if not task_dir.is_dir():
            continue
        for path in task_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # never persist non-text/unreadable files
            rel = path.relative_to(task_dir).as_posix()
            files[f"{prefix}/{rel}"] = text
    return files


def sync_task(tasks_root: Path, artifacts_root: Path, task_id: str) -> None:
    """Mirror a task's current on-disk files into the database. Call this
    right after any request that changes a task's state or artifacts."""
    if not is_enabled():
        return
    files = _collect_files(tasks_root, artifacts_root, task_id)
    if not files:
        return
    ensure_schema()
    with _connect() as conn:
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (task_id, files, deleted, updated_at)
            VALUES (%s, %s, false, now())
            ON CONFLICT (task_id) DO UPDATE
                SET files = EXCLUDED.files, deleted = false, updated_at = now()
            """,
            (task_id, json.dumps(files)),
        )
        conn.commit()


def restore_task(tasks_root: Path, artifacts_root: Path, task_id: str) -> bool:
    """If this task exists (and isn't deleted) in the database but is
    missing locally, write its files back to disk. Returns True if a
    restore happened or the task was already present locally."""
    local_state = tasks_root / task_id / "state.json"
    if local_state.exists():
        return True
    if not is_enabled():
        return False
    ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            f"SELECT files FROM {_TABLE} WHERE task_id = %s AND deleted = false", (task_id,)
        ).fetchone()
    if row is None:
        return False
    files = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    for rel_path, content in files.items():
        prefix, _, rel = rel_path.partition("/")
        base = tasks_root if prefix == "tasks" else artifacts_root
        target = base / task_id / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return True


def list_known_task_ids() -> list[str]:
    """All non-deleted task_ids the database knows about, including ones
    not currently present on local disk."""
    if not is_enabled():
        return []
    ensure_schema()
    with _connect() as conn:
        rows = conn.execute(f"SELECT task_id FROM {_TABLE} WHERE deleted = false").fetchall()
    return [r[0] for r in rows]


def delete_task(task_id: str) -> None:
    if not is_enabled():
        return
    ensure_schema()
    with _connect() as conn:
        conn.execute(f"DELETE FROM {_TABLE} WHERE task_id = %s", (task_id,))
        conn.commit()

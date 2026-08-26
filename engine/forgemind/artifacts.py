"""Markdown-with-YAML-frontmatter file I/O.

Used for real artifacts (artifacts/<task_id>/*.md), task requests
(tasks/<task_id>/task.md), and manual-runner prompt packets alike -- one
small, generic module instead of duplicating frontmatter parsing per
subsystem. No Pydantic, no JSON artifact contracts, no database.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

FRONTMATTER_DELIMITER = "---"


class ArtifactValidationError(Exception):
    pass


def write_markdown(path: Path, frontmatter: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    content = f"{FRONTMATTER_DELIMITER}\n{fm_text}\n{FRONTMATTER_DELIMITER}\n\n{body.strip()}\n"
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)  # atomic on Windows and POSIX alike


def read_markdown(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(FRONTMATTER_DELIMITER):
        raise ArtifactValidationError(f"{path}: missing YAML frontmatter delimiter")
    parts = text.split(FRONTMATTER_DELIMITER, 2)
    if len(parts) < 3:
        raise ArtifactValidationError(f"{path}: malformed frontmatter block")
    _, fm_text, body = parts
    try:
        frontmatter = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise ArtifactValidationError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ArtifactValidationError(f"{path}: frontmatter must be a mapping")
    return frontmatter, body.strip()


def validate_frontmatter(frontmatter: dict, required_fields: list[str]) -> list[str]:
    errors = []
    for field_name in required_fields:
        if field_name not in frontmatter or frontmatter[field_name] in (None, ""):
            errors.append(f"missing required field: {field_name}")
    return errors


def validate_artifact_file(path: Path, required_fields: list[str]) -> list[str]:
    if not path.exists():
        return [f"artifact file does not exist: {path}"]
    try:
        frontmatter, body = read_markdown(path)
    except ArtifactValidationError as exc:
        return [str(exc)]
    errors = validate_frontmatter(frontmatter, required_fields)
    if not body:
        errors.append("artifact body is empty")
    return errors

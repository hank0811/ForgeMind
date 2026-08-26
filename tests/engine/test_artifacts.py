from __future__ import annotations

import pytest

from forgemind import artifacts


def test_write_and_read_roundtrip(repo):
    path = repo / "artifacts" / "sample.md"
    artifacts.write_markdown(path, {"status": "ok", "task_id": "t1"}, "Body text here.")
    frontmatter, body = artifacts.read_markdown(path)
    assert frontmatter == {"status": "ok", "task_id": "t1"}
    assert body == "Body text here."


def test_no_tmp_file_left_after_write(repo):
    path = repo / "artifacts" / "sample2.md"
    artifacts.write_markdown(path, {"status": "ok"}, "Body.")
    tmp_path = path.with_name(path.name + ".tmp")
    assert not tmp_path.exists()


def test_validate_artifact_file_missing_field(repo):
    path = repo / "artifacts" / "bad.md"
    artifacts.write_markdown(path, {"task_id": "t1"}, "Body.")
    errors = artifacts.validate_artifact_file(path, required_fields=["status", "task_id"])
    assert any("status" in e for e in errors)


def test_validate_artifact_file_empty_body(repo):
    path = repo / "artifacts" / "empty.md"
    artifacts.write_markdown(path, {"status": "ok"}, "")
    errors = artifacts.validate_artifact_file(path, required_fields=["status"])
    assert any("empty" in e for e in errors)


def test_validate_artifact_file_valid_returns_no_errors(repo):
    path = repo / "artifacts" / "good.md"
    artifacts.write_markdown(path, {"status": "ok", "task_id": "t1"}, "Real content.")
    errors = artifacts.validate_artifact_file(path, required_fields=["status", "task_id"])
    assert errors == []


def test_validate_artifact_file_missing_file(repo):
    path = repo / "artifacts" / "doesnotexist.md"
    errors = artifacts.validate_artifact_file(path, required_fields=["status"])
    assert errors


def test_read_markdown_rejects_missing_frontmatter(repo):
    path = repo / "artifacts" / "plain.md"
    path.write_text("Just a plain markdown file, no frontmatter.", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactValidationError):
        artifacts.read_markdown(path)


def test_read_markdown_rejects_invalid_yaml(repo):
    path = repo / "artifacts" / "badyaml.md"
    path.write_text("---\nstatus: [unclosed\n---\n\nBody.\n", encoding="utf-8")
    with pytest.raises(artifacts.ArtifactValidationError):
        artifacts.read_markdown(path)

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import EXAMPLES, SCHEMA_FIXTURES


def test_examples_directory_is_valid(rlp):
    result = rlp("validate", str(EXAMPLES))
    assert result.code == 0, result.stdout + result.stderr
    assert "rlp.yaml: valid" in result.stdout
    assert "rlp-template.yaml: valid" in result.stdout


def test_defaults_to_current_directory(rlp):
    assert rlp("validate", cwd=EXAMPLES).code == 0


@pytest.mark.parametrize("path", sorted((SCHEMA_FIXTURES / "repo" / "invalid").glob("*.yaml")), ids=lambda p: p.stem)
def test_invalid_repo_fixtures_fail(rlp, path: Path):
    result = rlp("validate", str(path), "-o", "json")
    assert result.code == 1
    assert result.json["ok"] is False
    assert result.json["problems"]


def test_invalid_template_fixture_fails_with_kind_from_document(rlp):
    result = rlp("validate", str(SCHEMA_FIXTURES / "template" / "invalid" / "bad-version.yaml"), "-o", "json")
    assert result.code == 1
    assert [p["path"] for p in result.json["problems"]] == ["metadata.version"]


def test_error_names_the_offending_path(rlp):
    result = rlp("validate", str(SCHEMA_FIXTURES / "repo" / "invalid" / "unknown-artifact-kind.yaml"))
    assert result.code == 1
    assert "components[0].artifact.kind: 'wheel' is not one of" in result.stdout


def test_root_level_errors_say_root(rlp):
    result = rlp("validate", str(SCHEMA_FIXTURES / "repo" / "invalid" / "missing-components.yaml"), "-o", "json")
    assert result.json["problems"][0]["path"] == "(root)"


def test_missing_contract_is_a_usage_error(rlp, tmp_path):
    result = rlp("validate", str(tmp_path))
    assert result.code == 2
    assert "no rlp.yaml or rlp-template.yaml" in result.stderr


def test_unparseable_yaml_is_reported(rlp, tmp_path):
    (tmp_path / "rlp.yaml").write_text("apiVersion: [unclosed\n", encoding="utf-8")
    result = rlp("validate", str(tmp_path), "-o", "json")
    assert result.code == 2
    assert "not valid YAML" in result.json["error"]


def test_windows_line_endings_validate(rlp, tmp_path):
    text = (EXAMPLES / "rlp.yaml").read_text(encoding="utf-8")
    (tmp_path / "rlp.yaml").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert rlp("validate", str(tmp_path)).code == 0

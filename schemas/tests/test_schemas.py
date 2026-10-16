"""Tests for the v1alpha1 contract schemas.

Valid fixtures and the examples must validate. Every invalid fixture declares,
in an ``# expect-error: <JSON pointer>`` header comment, the one path its
errors must point at (``(root)`` for the document itself); the test fails if
it validates or if any error points elsewhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas" / "v1alpha1"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = ROOT / "examples"

PUBLISH_BASE = "https://raw.githubusercontent.com/rdlucas2/rlp/main/schemas/v1alpha1/"
KINDS = {"repo": "repo.schema.json", "template": "template.schema.json"}
EXPECT = re.compile(r"^# expect-error: (\S+)\s*$", re.MULTILINE)
ROOT_POINTER = "(root)"
LANGUAGE_SERVER = re.compile(r"^# yaml-language-server: \$schema=(\S+)\s*$", re.MULTILINE)


def load_schema(kind: str) -> dict:
    return json.loads((SCHEMAS / KINDS[kind]).read_text(encoding="utf-8"))


def validator(kind: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(kind))


def pointer(error) -> str:
    return "".join(f"/{part}" for part in error.absolute_path)


def cases(kind: str, group: str) -> list[Path]:
    return sorted((FIXTURES / kind / group).glob("*.yaml"))


def case_id(path: Path) -> str:
    return path.stem


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_schema_is_valid_draft_2020_12(kind: str) -> None:
    Draft202012Validator.check_schema(load_schema(kind))


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_schema_id_is_published_url(kind: str) -> None:
    assert load_schema(kind)["$id"] == PUBLISH_BASE + KINDS[kind]


@pytest.mark.parametrize(
    ("kind", "example"),
    [("repo", "rlp.yaml"), ("template", "rlp-template.yaml")],
)
def test_example_validates_and_points_editors_at_schema(kind: str, example: str) -> None:
    text = (EXAMPLES / example).read_text(encoding="utf-8")
    errors = [f"{pointer(e)}: {e.message}" for e in validator(kind).iter_errors(yaml.safe_load(text))]
    assert errors == []
    match = LANGUAGE_SERVER.search(text)
    assert match, f"{example} lacks a yaml-language-server $schema comment"
    assert match.group(1) == load_schema(kind)["$id"]


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_fixture_groups_are_not_empty(kind: str) -> None:
    assert cases(kind, "valid")
    assert cases(kind, "invalid")


@pytest.mark.parametrize(
    ("kind", "path"),
    [(k, p) for k in sorted(KINDS) for p in cases(k, "valid")],
    ids=lambda v: v if isinstance(v, str) else case_id(v),
)
def test_valid_fixture(kind: str, path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = [f"{pointer(e)}: {e.message}" for e in validator(kind).iter_errors(doc)]
    assert errors == []


@pytest.mark.parametrize(
    ("kind", "path"),
    [(k, p) for k in sorted(KINDS) for p in cases(k, "invalid")],
    ids=lambda v: v if isinstance(v, str) else case_id(v),
)
def test_invalid_fixture_fails_at_expected_path(kind: str, path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    match = EXPECT.search(text)
    assert match, f"{path.name} lacks an '# expect-error: <pointer>' header"
    expected = "" if match.group(1) == ROOT_POINTER else match.group(1)
    errors = list(validator(kind).iter_errors(yaml.safe_load(text)))
    assert errors, f"{path.name} validated but should fail at '{match.group(1)}'"
    assert {pointer(e) for e in errors} == {expected}, [f"{pointer(e)}: {e.message}" for e in errors]


def test_verbs_default_to_task_name() -> None:
    verbs = load_schema("repo")["$defs"]["verbs"]["properties"]
    assert set(verbs) == {"setup", "lint", "test", "build", "package", "run", "smoke"}
    for name, spec in verbs.items():
        assert spec["default"] == f"task {name}"

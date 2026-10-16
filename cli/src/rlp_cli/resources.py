"""Locate the contract schemas and the policy bundle.

An installed wheel and a PyInstaller binary carry them in ``rlp_cli/_data``.
A source checkout (editable install, ``python -m rlp_cli``) reads them from
the repo root instead.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

PACKAGE_DATA = Path(__file__).resolve().parent / "_data"
REPO_ROOT = Path(__file__).resolve().parents[3]

SCHEMA_FILES = {"Repo": "repo.schema.json", "Template": "template.schema.json"}


def data_root() -> Path:
    if (PACKAGE_DATA / "policy").is_dir():
        return PACKAGE_DATA
    if (REPO_ROOT / "policy").is_dir() and (REPO_ROOT / "schemas" / "v1alpha1").is_dir():
        return REPO_ROOT
    raise FileNotFoundError("rlp data files (schemas, policy bundle) are missing from this installation")


def policy_dir() -> Path:
    return data_root() / "policy"


def schema_path(kind: str) -> Path:
    return data_root() / "schemas" / "v1alpha1" / SCHEMA_FILES[kind]


@cache
def schema(kind: str) -> dict:
    return json.loads(schema_path(kind).read_text(encoding="utf-8"))

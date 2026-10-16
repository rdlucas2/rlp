"""Find, load and validate rlp contract files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from jsonschema import Draft202012Validator

from rlp_cli import resources

CONTRACT_FILES = {"rlp.yaml": "Repo", "rlp-template.yaml": "Template"}


class ContractError(Exception):
    """A contract file is missing or unreadable."""


@dataclass(frozen=True)
class Problem:
    file: str
    path: str
    message: str

    def as_dict(self) -> dict:
        return {"file": self.file, "path": self.path, "message": self.message}

    def __str__(self) -> str:
        return f"{self.file} {self.path}: {self.message}"


def dotted(parts) -> str:
    """Render a JSON path as components[0].artifact.kind; the document itself is (root)."""
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(root)"


def find_contracts(path: Path) -> list[Path]:
    """Contract files at path: the file itself, or every contract file in the directory."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ContractError(f"{path} does not exist")
    found = [path / name for name in CONTRACT_FILES if (path / name).is_file()]
    if not found:
        raise ContractError(f"no rlp.yaml or rlp-template.yaml in {path}")
    return found


def load_yaml(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as err:
        raise ContractError(f"cannot read {path}: {err}") from err
    except yaml.YAMLError as err:
        raise ContractError(f"{path} is not valid YAML: {err}") from err


def kind_for(path: Path, doc) -> str:
    """Pick the schema: by file name, else by the document's kind, else Repo."""
    if path.name in CONTRACT_FILES:
        return CONTRACT_FILES[path.name]
    if isinstance(doc, dict) and doc.get("kind") in resources.SCHEMA_FILES:
        return doc["kind"]
    return "Repo"


def validate_file(path: Path, display: str | None = None) -> list[Problem]:
    doc = load_yaml(path)
    kind = kind_for(path, doc)
    validator = Draft202012Validator(resources.schema(kind))
    name = display or path.name
    errors = sorted(validator.iter_errors(doc), key=lambda e: (list(map(str, e.absolute_path)), e.message))
    return [Problem(name, dotted(e.absolute_path), e.message) for e in errors]


def load_repo(root: Path) -> dict:
    """The parsed, schema-valid rlp.yaml at root."""
    path = root / "rlp.yaml"
    if not path.is_file():
        raise ContractError(f"no rlp.yaml in {root}")
    problems = validate_file(path)
    if problems:
        details = "\n".join(f"  {p}" for p in problems)
        raise ContractError(f"rlp.yaml is invalid; run `rlp validate` for details:\n{details}")
    return load_yaml(path)


def component_dir(root: Path, component: dict) -> Path:
    """The directory a component's verbs run in. Contract paths always use forward slashes."""
    rel = PurePosixPath(component.get("path", "."))
    return root.joinpath(*rel.parts) if rel.parts else root

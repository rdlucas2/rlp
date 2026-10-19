"""The change under review, as input for the rlp.agent policy package.

The anti-cheat rules (policy/agent/) judge an agent-authored pull request by
what it changed since the baseline commit (see baseline.py). `rlp policy check`
builds this document and hands it to conftest next to the repo's own files:

- who: the PR author and labels, from $RLP_PR_AUTHOR and $RLP_PR_LABELS
  (comma-separated) when set, otherwise from the pull_request event at
  $GITHUB_EVENT_PATH;
- what: every file under root changed since the baseline, with its added lines
  and the removed lines' text, plus untracked files as added. Paths are relative
  to root, so a project in a subdirectory is judged on its own files;
- contracts: rlp.yaml and Taskfiles changed in the diff, parsed before and after;
- results: test counts and line coverage per component from a base and a head
  .rlp/pipeline/results.json, when both are given.

Without a baseline there is no diff, so the diff rules have nothing to judge.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

from rlp_cli import baseline

KEY = "rlp_change"
BASE_RESULTS_ENV = "RLP_BASE_RESULTS"
CONTRACT_NAMES = {"rlp.yaml", "Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"}
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
# Untracked files bigger than this are listed without their lines.
MAX_UNTRACKED_BYTES = 1_000_000


class ChangeError(Exception):
    """A results file or the PR event could not be read."""


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _name_status(root: Path, base: str) -> dict[str, str]:
    out = _git(root, "diff", "--relative", "--no-renames", "--name-status", "-z", base) or ""
    parts = [p for p in out.split("\0") if p]
    return {parts[i + 1]: parts[i][0] for i in range(0, len(parts) - 1, 2)}


def _hunks(root: Path, base: str) -> dict[str, dict]:
    """path -> {"added": [{"line", "text"}], "removed": [text]} from a zero-context diff."""
    out = _git(root, "diff", "--relative", "--no-renames", "--no-color", "--no-ext-diff", "--unified=0", base) or ""
    files: dict[str, dict] = {}
    old = new = None
    current: dict | None = None
    line = 0
    for raw in out.splitlines():
        if raw.startswith("diff --git "):
            old = new = current = None
        elif raw.startswith("--- ") and current is None:
            old = raw[4:]
        elif raw.startswith("+++ ") and current is None:
            new = raw[4:]
            path = new[2:] if new != "/dev/null" else (old or "")[2:]
            current = files.setdefault(path, {"added": [], "removed": []})
        elif current is not None and (m := HUNK.match(raw)):
            line = int(m["start"])
        elif current is not None and raw.startswith("+"):
            current["added"].append({"line": line, "text": raw[1:]})
            line += 1
        elif current is not None and raw.startswith("-"):
            current["removed"].append(raw[1:])
    return files


def _untracked(root: Path) -> list[str]:
    out = _git(root, "ls-files", "--others", "--exclude-standard", "-z") or ""
    return [p for p in out.split("\0") if p]


def _read_lines(path: Path) -> list[dict]:
    try:
        if path.stat().st_size > MAX_UNTRACKED_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [{"line": i, "text": t} for i, t in enumerate(text.splitlines(), start=1)]


def _parse_yaml(text: str | None):
    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _contract(root: Path, base: str, path: str, status: str) -> dict:
    before = None if status == "A" else _parse_yaml(_git(root, "show", f"{base}:./{path}"))
    after = None
    if status != "D":
        try:
            after = _parse_yaml((root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            after = None
    return {"before": before, "after": after}


def files(root: Path, base: str | None) -> tuple[list[dict], dict[str, dict]]:
    """Changed files since base, and the before/after documents of changed contract files."""
    if base is None:
        return [], {}
    statuses = _name_status(root, base)
    hunks = _hunks(root, base)
    changed = []
    for path in sorted(statuses):
        h = hunks.get(path, {"added": [], "removed": []})
        changed.append({"path": path, "status": statuses[path], "added": h["added"], "removed": h["removed"]})
    for path in sorted(set(_untracked(root)) - set(statuses)):
        changed.append({"path": path, "status": "A", "added": _read_lines(root / path), "removed": []})
    contracts = {
        f["path"]: _contract(root, base, f["path"], f["status"])
        for f in changed
        if f["path"].rsplit("/", 1)[-1] in CONTRACT_NAMES
    }
    return changed, contracts


def author_and_labels(env: dict | None = None) -> tuple[str, list[str]]:
    env = os.environ if env is None else env
    author, labels = "", []
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise ChangeError(f"can't read the GitHub event at {event_path}: {err}") from err
        pr = event.get("pull_request") or {}
        author = (pr.get("user") or {}).get("login") or ""
        labels = [lab.get("name", "") for lab in pr.get("labels") or [] if isinstance(lab, dict)]
    if "RLP_PR_AUTHOR" in env:
        author = env["RLP_PR_AUTHOR"].strip()
    if "RLP_PR_LABELS" in env:
        labels = [x.strip() for x in env["RLP_PR_LABELS"].split(",")]
    return author, sorted({x for x in labels if x})


def metrics(path: Path | None) -> dict | None:
    """Per-component test counts and line coverage from a results.json, or None when there is none."""
    if path is None or not path.is_file():
        return None
    try:
        stages = {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as err:
        raise ChangeError(f"can't read pipeline results at {path}: {err}") from err
    tests = (stages.get("test") or {}).get("data", {}).get("tests") or {}
    coverage = {
        r["component"]: r["percent"]
        for r in (stages.get("coverage") or {}).get("data", {}).get("results") or []
        if isinstance(r.get("percent"), (int, float))
    }
    return {"tests": tests, "coverage": coverage}


def build(root: Path, base_results: Path | None = None, head_results: Path | None = None) -> dict:
    """The rlp.agent input document for the repo at root."""
    base = baseline.resolve(root)
    changed, contracts = files(root, base)
    author, labels = author_and_labels()
    if base_results is None and os.environ.get(BASE_RESULTS_ENV):
        base_results = Path(os.environ[BASE_RESULTS_ENV])
    if head_results is None:
        head_results = root / ".rlp" / "pipeline" / "results.json"
    return {
        KEY: {
            "baseline": base,
            "author": author,
            "labels": labels,
            "files": changed,
            "contracts": contracts,
            "results": {"base": metrics(base_results), "head": metrics(head_results)},
        }
    }

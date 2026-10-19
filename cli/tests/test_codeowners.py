""".github/CODEOWNERS covers every path the RLP-A001 rule protects (#73).

The protected prefixes, paths and file names are read from
policy/agent/agent.rego, so a path added to the rule without a CODEOWNERS
line fails here. Each is checked at the repo root and inside a project in a
subdirectory, as golden-selftest and platform/ are.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CODEOWNERS = REPO / ".github" / "CODEOWNERS"
AGENT_REGO = REPO / "policy" / "agent" / "agent.rego"
OWNER = "@rdlucas2"


def rules() -> list[tuple[str, list[str]]]:
    out = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            out.append((pattern, owners))
    return out


def regex(pattern: str) -> re.Pattern:
    """A CODEOWNERS (gitignore-style) pattern as a regex over repo-relative paths."""
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in body
    parts = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif body[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(body[i]))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if directory else "(?:/.*)?"
    return re.compile(f"^{prefix}{''.join(parts)}{suffix}$")


def owners_of(path: str) -> list[str]:
    """The owners of path: the last matching rule wins, as on GitHub."""
    found: list[str] = []
    for pattern, owners in rules():
        if regex(pattern).match(path):
            found = owners
    return found


def rego_strings(name: str) -> list[str]:
    """The string literals of a `name := [...]` or `name := {...}` in agent.rego."""
    text = AGENT_REGO.read_text(encoding="utf-8")
    m = re.search(rf"^{name} := [\[{{](.*?)[\]}}]", text, flags=re.S | re.M)
    assert m, f"{name} not found in {AGENT_REGO}"
    return re.findall(r'"([^"]+)"', m[1])


def protected_samples() -> list[str]:
    samples = [f"{prefix}example.yml" for prefix in rego_strings("protected_prefixes")]
    samples += rego_strings("protected_paths")
    samples += rego_strings("protected_names")
    samples += [f"tools/{name}" for name in rego_strings("protected_names")]
    samples.append(".semgrep/rules.yml")  # the rule's /.semgrep/ directory check
    assert len(samples) > 10
    return samples


@pytest.mark.parametrize("path", protected_samples())
@pytest.mark.parametrize("project", ["", "platform/", "tests/pipeline/fixtures/hello/"])
def test_every_rlp_a001_path_has_ryan_as_code_owner(path: str, project: str):
    assert owners_of(project + path) == [OWNER], f"{project + path} has no CODEOWNERS rule"


@pytest.mark.parametrize("path", ["rlp.yaml", "Taskfile.yml", "platform/rlp.yaml", "platform/Taskfile.yml", ".github/CODEOWNERS"])
def test_contracts_and_codeowners_itself_are_owned(path: str):
    assert owners_of(path) == [OWNER]


@pytest.mark.parametrize("path", ["README.md", "cli/src/rlp_cli/cli.py", "docs/policy.md", ".github/actions/agent-identity/action.yml"])
def test_ordinary_files_have_no_owner(path: str):
    assert owners_of(path) == []


def test_every_rule_names_an_owner():
    assert rules()
    for pattern, owners in rules():
        assert owners == [OWNER], pattern

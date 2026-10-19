"""The quality remediation loop (#48): `rlp loop quality`.

It fixes what the golden pipeline's code-quality gates found: `lint`, `test`,
`coverage` (overall and on new code) and `sast` (Semgrep). Those four stages
are also what it verifies with, so a run ends `fixed` only when all of them
pass on the loop branch.

Discovery reads the gates' last results, so run them first (the loop workflow
does): `rlp pipeline run -s setup -s lint -s test -s coverage -s sast`.

- `.rlp/pipeline/results.json`: which verify stages failed, the coverage
  results and the uncovered changed lines, and the Semgrep findings the sast
  stage recorded (else `.rlp/pipeline/reports/semgrep.json`).
- For a failed `lint` or `test` verb, the verb is run again per failing
  component to capture its output, since the agent can't run commands.
- When SONAR_TOKEN is set and the optional `sonar` stage failed, the open
  issues from the Sonar issues API.

The agent may change only the tracked, non-hidden top-level directories of
each component (for example `src/` and `tests/`), never files at a
component's root (Taskfile.yml, pyproject.toml, rlp.yaml, coverage and lint
config) nor `.github/`, `.rlp/`, `policy/` or `infra/`. The anti-cheat rules
(rlp.agent) judge what it does change.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from rlp_cli import loop, pipeline, sonar, verbs
from rlp_cli.contract import component_dir

NAME = "quality"
VERIFY_STAGES = ("lint", "test", "coverage", "sast")
OUTPUT_LIMIT = 4000  # characters of a failing verb's output kept in a finding (its tail)
# Top-level directories that hold platform or gate configuration, not code, even without a leading dot.
PROTECTED_DIRS = frozenset({"policy", "infra", "reports"})
SONAR_PAGE = 100


# --- allowed paths ---


def _tracked(root: Path) -> list[str] | None:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.split("\0") if p]


def allowed_paths(root: Path, doc: dict) -> list[str]:
    """Each component's tracked top-level directories, minus hidden and protected ones."""
    tracked = _tracked(root)
    if tracked is None:
        raise loop.LoopError(f"{root} is not in a git work tree; loops work on a branch")
    allowed: list[str] = []
    rels = {c["name"]: component_dir(root, c).resolve().relative_to(root.resolve()).as_posix() for c in doc.get("components") or []}
    for c in doc.get("components") or []:
        rel = rels[c["name"]]
        prefix = "" if rel == "." else rel + "/"
        # Another component nested inside this one is that component's to change.
        nested = {r for n, r in rels.items() if n != c["name"] and r != rel and r.startswith(prefix)}
        found = []
        for path in tracked:
            if not path.startswith(prefix):
                continue
            parts = PurePosixPath(path[len(prefix):]).parts
            if len(parts) < 2:
                continue  # a file at the component's root: config, not code
            top = parts[0]
            if top.startswith(".") or top in PROTECTED_DIRS or any(n == prefix + top or n.startswith(prefix + top + "/") for n in nested):
                continue
            found.append(prefix + top)
        if not found:
            raise loop.LoopError(f"component {c['name']} has no tracked source directories the quality loop may change (for example src/ or tests/)")
        allowed += found
    return list(dict.fromkeys(allowed))


# --- discovery ---


def _tail(text: str, limit: int = OUTPUT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"[... {len(text) - limit} characters cut ...]\n{text[-limit:]}"


def _verb_output(root: Path, doc: dict, verb: str, component: str) -> tuple[int | None, str]:
    """Run one component's verb again, capturing what it prints."""
    [step] = verbs.plan(root, doc, verb, (component,))
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out:
        verbs.execute(step, stdout=out, stderr=subprocess.STDOUT)
        out.seek(0)
        text = out.read()
    if step.error:
        text = f"{text}\n{step.error}"
    return step.exit_code, _tail(text)


def verb_findings(root: Path, doc: dict, result: dict) -> list[dict]:
    verb = result["stage"]
    failed = [c for c, status in (result.get("components") or {}).items() if status == "failed"]
    if not failed:
        return [{"id": verb, "stage": verb, "message": f"the {verb} stage failed: {result.get('detail', '')}"}]
    findings = []
    for name in failed:
        code, output = _verb_output(root, doc, verb, name)
        findings.append({
            "id": f"{verb}-{name}",
            "stage": verb,
            "component": name,
            "message": f"the {verb} verb fails for component {name} (exit {code})",
            "output": output,
        })
    return findings


def coverage_findings(root: Path, doc: dict, result: dict) -> list[dict]:
    data = result.get("data") or {}
    findings = []
    for r in data.get("results") or []:
        if r.get("ok"):
            continue
        if r.get("error"):
            message = f"coverage for component {r['component']}: {r['error']}"
        else:
            message = f"line coverage of component {r['component']} is {r['percent']:.1f}%, below the minimum of {r['minimum']:g}%"
        findings.append({"id": f"coverage-{r['component']}", "stage": "coverage", "component": r["component"], "path": r.get("path", ""), "rule": "coverage-minimum", "message": message})
    components = {c["name"]: c for c in doc.get("components") or []}
    for n in data.get("new_code") or []:
        if n.get("ok", True):
            continue
        comp = components.get(n["component"])
        cdir = component_dir(root, comp).resolve().relative_to(root.resolve()).as_posix() if comp else "."
        missing = n.get("missing") or {}
        if not missing:
            findings.append({"id": f"coverage-new-{n['component']}", "stage": "coverage", "component": n["component"], "rule": "new-code-coverage",
                             "message": f"new code in component {n['component']} is {n['percent']:.1f}% covered, below the minimum of {n['minimum']:g}%"})
        for path, lines in sorted(missing.items()):
            shown = path if cdir == "." or path.startswith(cdir + "/") else f"{cdir}/{path}"
            findings.append({
                "id": f"coverage-new-{shown}",
                "stage": "coverage",
                "component": n["component"],
                "path": shown,
                "line": int(lines[0]),
                "lines": [int(x) for x in lines],
                "rule": "new-code-coverage",
                "message": (f"changed lines not covered by any test: {', '.join(str(x) for x in lines)} "
                            f"(new code in {n['component']} is {n['percent']:.1f}% covered, minimum {n['minimum']:g}%)"),
            })
    if not findings:
        findings.append({"id": "coverage", "stage": "coverage", "message": f"the coverage stage failed: {result.get('detail', '')}"})
    return findings


def _semgrep_report(root: Path) -> list[dict]:
    path = root / pipeline.REPORTS / "semgrep.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        {
            "rule": r.get("check_id", ""),
            "path": r.get("path", ""),
            "line": (r.get("start") or {}).get("line", 0),
            "severity": (r.get("extra") or {}).get("severity", ""),
            "message": ((r.get("extra") or {}).get("message", "").strip().splitlines() or [""])[0],
        }
        for r in report.get("results") or []
    ]


def sast_findings(root: Path, doc: dict, result: dict) -> list[dict]:
    data = result.get("data") or {}
    raw = data["findings"] if "findings" in data else _semgrep_report(root)
    findings = [
        {
            "id": f"sast-{f['rule']}-{f['path']}-{f['line']}",
            "stage": "sast",
            "path": f["path"],
            "line": int(f["line"]),
            "rule": f["rule"],
            "severity": f.get("severity", ""),
            "message": f.get("message") or f["rule"],
        }
        for f in raw
    ]
    if not findings:
        findings.append({"id": "sast", "stage": "sast", "message": f"the sast stage failed: {result.get('detail', '')}"})
    return findings


Fetch = Callable[[str, Mapping[str, str]], dict]


def _fetch(url: str, headers: Mapping[str, str]) -> dict:
    req = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(req, timeout=30) as resp:  # the Sonar host is the user's own setting
        return json.loads(resp.read() or b"{}")


def sonar_findings(root: Path, doc: dict, env: Mapping[str, str] | None = None, fetch: Fetch = _fetch) -> list[dict]:
    """Open issues from the Sonar issues API, for the project the sonar stage analysed."""
    env = os.environ if env is None else env
    props = sonar.properties(root, doc, dict(env))
    query = {"componentKeys": props["sonar.projectKey"], "resolved": "false", "ps": str(SONAR_PAGE)}
    if "sonar.organization" in props:
        query["organization"] = props["sonar.organization"]
    url = props["sonar.host.url"].rstrip("/") + "/api/issues/search?" + urllib.parse.urlencode(query)
    try:
        doc_ = fetch(url, {"Authorization": f"Bearer {env.get('SONAR_TOKEN', '')}", "Accept": "application/json"})
    except (urllib.error.URLError, OSError, ValueError) as err:
        return [{"id": "sonar", "stage": "sonar", "message": f"the sonar quality gate failed, and its issues could not be read from {props['sonar.host.url']}: {err}"}]
    findings = []
    for issue in doc_.get("issues") or []:
        _, _, path = str(issue.get("component", "")).partition(":")
        f = {"id": f"sonar-{issue.get('key', len(findings))}", "stage": "sonar", "path": path, "rule": issue.get("rule", ""),
             "severity": issue.get("severity", ""), "message": issue.get("message", "")}
        if issue.get("line"):
            f["line"] = int(issue["line"])
        findings.append(f)
    return findings


FINDERS = {"lint": verb_findings, "test": verb_findings, "coverage": coverage_findings, "sast": sast_findings}


def discover(root: Path, doc: dict, env: Mapping[str, str] | None = None) -> list[dict]:
    """The findings of every failed verify stage in the last gate results, plus Sonar's when it ran."""
    env = os.environ if env is None else env
    results = pipeline.load_results(root)
    findings: list[dict] = []
    for stage in VERIFY_STAGES:
        r = results.get(stage)
        if r and r.get("status") == "failed":
            findings += FINDERS[stage](root, doc, r)
    s = results.get("sonar")
    if s and s.get("status") == "failed" and sonar.configured(dict(env)):
        findings += sonar_findings(root, doc, env)
    return findings


# --- the prompt ---

STAGE_TITLES = {
    "lint": "Lint (the lint verb must exit 0)",
    "test": "Tests (the test verb must exit 0)",
    "coverage": "Coverage (overall, and on lines changed since the baseline commit)",
    "sast": "Semgrep (no new ERROR or WARNING findings)",
    "sonar": "Sonar (open issues)",
}


NO_WEAKENING = (
    "- Fix the code. Never weaken a gate: no suppression comments (such as nosemgrep, noqa or pragma: no cover), "
    "no lowered thresholds, no skipped or deleted tests, no exception files."
)


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def build_prompt(p: loop.PromptInput) -> str:
    lines = [
        f"You are fixing findings from rlp's code-quality gates ({', '.join(VERIFY_STAGES)}) in this repository (attempt {p.iteration} of {p.max_iterations}).",
        "",
        "Rules:",
        f"- Change only files under: {', '.join(p.allowed_paths)}. Changes anywhere else are thrown away.",
        NO_WEAKENING,
        "- For uncovered lines, add or extend tests that exercise them.",
        "- You can't run commands. When you finish, the loop reruns lint, test, coverage and sast itself.",
        "- When you are done, say in one short paragraph what you changed.",
    ]
    by_stage: dict[str, list[dict]] = {}
    for f in p.findings:
        by_stage.setdefault(f.get("stage", "other"), []).append(f)
    for stage, group in by_stage.items():
        lines += ["", f"## {STAGE_TITLES.get(stage, stage)}", ""]
        for f in group:
            where = f.get("path", "")
            if where and f.get("line"):
                where += f":{f['line']}"
            rule = f" [{f['rule']}]" if f.get("rule") else ""
            lines.append(f"- {where + ': ' if where else ''}{f.get('message', '')}{rule}")
            if f.get("output"):
                lines += ["", "  Output:", "", *("  " + x for x in _fence(f["output"]).splitlines()), ""]
    if p.rejected:
        lines += ["", f"The previous attempt was thrown away: {p.rejected}"]
    failed = [g for g in p.gates if g.get("status") == "failed"]
    if failed:
        lines += ["", "The gates still fail after the previous attempt:"]
        lines += [f"- {g['stage']}: {g.get('detail', '')}" for g in failed]
    return "\n".join(lines) + "\n"


KIND = loop.register(loop.LoopKind(
    name=NAME,
    description="Fix lint, test, coverage and Semgrep findings, and verify with those gates.",
    discover=discover,
    verify_stages=VERIFY_STAGES,
    allowed_paths=allowed_paths,
    build_prompt=build_prompt,
))

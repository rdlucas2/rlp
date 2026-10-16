"""The golden pipeline's gates, in order, as CLI logic.

The golden workflow (.github/workflows/golden.yml) is a thin wrapper: each
step runs `rlp pipeline run --stage <name>`. Because the logic lives here, a
laptop, `act` and GitHub Actions produce the same gate results.

Results accumulate in .rlp/pipeline/results.json, one entry per stage, so
separate workflow steps build one report; `rlp pipeline summary` renders it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rlp_cli import baseline, contract, coverage, policy, sast, secrets, sonar, trivy, verbs, vex

# (stage, what it gates). Order is the pipeline order.
STAGES = [
    ("precommit", "pre-commit hooks and a full-history gitleaks scan"),
    ("validate", "contract files match the JSON Schemas"),
    ("policy", "the OPA policy bundle"),
    ("setup", "the setup verb, per component (installs what lint and test need)"),
    ("lint", "the lint verb, per component"),
    ("test", "the test verb, per component"),
    ("coverage", "line coverage, overall and on new code, against the declared minimum"),
    ("sast", "Semgrep static analysis: no new findings"),
    ("sonar", "optional local SonarQube analysis; skipped without SONAR_TOKEN"),
    ("package", "the package verb, per component"),
    ("trivy", "Trivy scan of dependencies and built images"),
    ("terraform", "Terraform plan checked by OPA (#16)"),
    ("deploy", "deploy to the target environment (#16)"),
]
STAGE_NAMES = [name for name, _ in STAGES]
RESULTS = Path(".rlp") / "pipeline" / "results.json"
# SARIF and JSON reports from the scanners, uploaded to code scanning by golden.yml.
REPORTS = Path(".rlp") / "pipeline" / "reports"


@dataclass
class StageResult:
    stage: str
    status: str  # passed | failed | skipped
    detail: str = ""
    seconds: float = 0.0
    components: dict[str, str] = field(default_factory=dict)  # component -> passed | failed | skipped
    data: dict = field(default_factory=dict)


@dataclass
class Context:
    root: Path
    components: tuple[str, ...] = ()
    deploy_env: str = ""
    deploy_local: bool = False
    stream: object = None  # where verbs' output goes; None inherits stdout

    def doc(self) -> dict:
        return contract.load_repo(self.root)


# --- stages ---


def stage_precommit(ctx: Context) -> StageResult:
    notes = []
    config = ctx.root / ".pre-commit-config.yaml"
    if config.is_file():
        exe = shutil.which("pre-commit")
        if exe is None:
            return StageResult("precommit", "failed", "the repo has .pre-commit-config.yaml but pre-commit is not installed")
        proc = subprocess.run([exe, "run", "--all-files", "--show-diff-on-failure"], cwd=ctx.root, stdout=ctx.stream, stderr=ctx.stream, check=False)
        if proc.returncode != 0:
            return StageResult("precommit", "failed", f"pre-commit hooks failed (exit {proc.returncode})")
        notes.append("pre-commit hooks passed")
    else:
        notes.append("no .pre-commit-config.yaml")
    sarif = REPORTS / "gitleaks.sarif"
    findings = secrets.scan(ctx.root, sarif=ctx.root / sarif)
    if findings:
        where = ", ".join(sorted({f"{f['file']}:{f['line']}" for f in findings}))
        return StageResult("precommit", "failed", f"gitleaks found {len(findings)} secret(s): {where}", data={"findings": findings, "sarif": sarif.as_posix()})
    notes.append("gitleaks: no secrets")
    return StageResult("precommit", "passed", "; ".join(notes))


def stage_validate(ctx: Context) -> StageResult:
    files = contract.find_contracts(ctx.root)
    problems = [p for f in files for p in contract.validate_file(f)]
    if problems:
        return StageResult("validate", "failed", "; ".join(str(p) for p in problems), data={"problems": [p.as_dict() for p in problems]})
    return StageResult("validate", "passed", ", ".join(f.name for f in files) + " valid")


def stage_policy(ctx: Context) -> StageResult:
    violations = policy.check(ctx.root)
    if violations:
        rules = ", ".join(sorted({v.rule for v in violations}))
        return StageResult("policy", "failed", f"{len(violations)} violation(s): {rules}", data={"violations": [v.as_dict() for v in violations]})
    return StageResult("policy", "passed", "no violations")


def run_verb(ctx: Context, verb: str) -> StageResult:
    steps = verbs.plan(ctx.root, ctx.doc(), verb, ctx.components)
    for step in steps:
        print(f"==> {verb} {step.component}: {' '.join(step.command)}", file=sys.stderr)
        verbs.execute(step, stdout=ctx.stream, stderr=ctx.stream)
        if step.error:
            print(f"error: {step.component}: {step.error}", file=sys.stderr)
    per = {s.component: "passed" if s.ok else "failed" for s in steps}
    failed = [s for s in steps if not s.ok]
    data = {"steps": [s.as_dict(ctx.root) for s in steps]}
    if failed:
        detail = "; ".join(f"{s.component} exited {s.exit_code}" + (f" ({s.error})" if s.error else "") for s in failed)
        return StageResult(verb, "failed", detail, components=per, data=data)
    return StageResult(verb, "passed", f"{len(steps)} component(s)", components=per, data=data)


def stage_coverage(ctx: Context) -> StageResult:
    doc = ctx.doc()
    results = coverage.check(ctx.root, doc, ctx.components)
    if not results:
        return StageResult("coverage", "skipped", "no component declares a coverage report")
    base = baseline.resolve(ctx.root)
    fresh = coverage.new_code(ctx.root, doc, base, ctx.components)
    new_ok = {n.component: n.ok for n in fresh}
    per = {r.component: "passed" if r.ok and new_ok.get(r.component, True) else "failed" for r in results}
    data = {"results": [r.as_dict() for r in results], "baseline": base, "new_code": [n.as_dict() for n in fresh]}
    parts = [
        f"{r.component} {r.error}" if r.error else f"{r.component} {r.percent:.1f}% (minimum {r.minimum:g}%)"
        for r in results
    ]
    if fresh:
        parts.append("new code: " + ", ".join(n.text() for n in fresh))
    status = "passed" if all(v == "passed" for v in per.values()) else "failed"
    return StageResult("coverage", status, "; ".join(parts), components=per, data=data)


def stage_sast(ctx: Context) -> StageResult:
    base = baseline.resolve(ctx.root)
    report = sast.scan(ctx.root, ctx.doc(), base, ctx.components)
    since = f"since {base[:12]}" if base else "in the whole repo (no baseline commit)"
    if report["semgrep"] != report["pinned"]:
        since += f"; semgrep {report['semgrep']}, rlp pins {report['pinned']}"
    if report["findings"]:
        shown = "; ".join(report["lines"][:5]) + ("; ..." if len(report["lines"]) > 5 else "")
        return StageResult("sast", "failed", f"{len(report['findings'])} new finding(s) {since}: {shown}", data=report)
    return StageResult("sast", "passed", f"no new findings {since} ({report['rules']} rules: {', '.join(report['rule_dirs'])})", data=report)


def stage_sonar(ctx: Context) -> StageResult:
    if not sonar.configured():
        return StageResult("sonar", "skipped", "optional; SONAR_TOKEN is not set (see docs/quality-gates.md)")
    props = sonar.properties(ctx.root, ctx.doc())
    code = sonar.analyse(ctx.root, ctx.doc(), stream=ctx.stream)
    data = {"properties": {k: v for k, v in props.items() if k != "sonar.projectBaseDir"}}
    where = f"{props['sonar.host.url']} project {props['sonar.projectKey']}"
    if code != 0:
        return StageResult("sonar", "failed", f"Sonar quality gate failed or analysis errored (exit {code}); see {where}", data=data)
    return StageResult("sonar", "passed", f"quality gate passed on {where}", data=data)


def stage_trivy(ctx: Context) -> StageResult:
    report = trivy.check(ctx.root, ctx.doc(), ctx.components)
    excepted = len(report["findings"]) - report["blocking"]
    targets = ", ".join(s["target"] for s in report["scans"])
    note = f"; {excepted} covered by .rlp/vex.yaml" if excepted else ""
    if report["blocking"]:
        shown = "; ".join(report["lines"][:5]) + ("; ..." if len(report["lines"]) > 5 else "")
        return StageResult("trivy", "failed", f"{report['blocking']} fixable HIGH or CRITICAL finding(s){note}: {shown}", data=report)
    return StageResult("trivy", "passed", f"no fixable HIGH or CRITICAL vulnerabilities in {targets}{note}", data=report)


def stage_terraform(ctx: Context) -> StageResult:
    return StageResult("terraform", "skipped", "no Terraform rendered for this repo yet (#16)")


def stage_deploy(ctx: Context) -> StageResult:
    if not ctx.deploy_env and not ctx.deploy_local:
        return StageResult("deploy", "skipped", "no deploy-env given")
    target = "LocalStack" if ctx.deploy_local else ctx.deploy_env
    return StageResult("deploy", "skipped", f"deploy to {target} is not implemented yet (#16)")


RUNNERS = {
    "precommit": stage_precommit,
    "validate": stage_validate,
    "policy": stage_policy,
    "setup": lambda ctx: run_verb(ctx, "setup"),
    "lint": lambda ctx: run_verb(ctx, "lint"),
    "test": lambda ctx: run_verb(ctx, "test"),
    "coverage": stage_coverage,
    "sast": stage_sast,
    "sonar": stage_sonar,
    "package": lambda ctx: run_verb(ctx, "package"),
    "trivy": stage_trivy,
    "terraform": stage_terraform,
    "deploy": stage_deploy,
}

# --- running and recording ---


def results_path(root: Path) -> Path:
    return root / RESULTS


def load_results(root: Path) -> dict[str, dict]:
    path = results_path(root)
    if not path.is_file():
        return {}
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))}


def save_results(root: Path, results: dict[str, dict]) -> None:
    path = results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [results[s] for s in STAGE_NAMES if s in results]
    path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


def run(ctx: Context, stages: list[str], reset: bool = False) -> list[StageResult]:
    """Run stages in pipeline order, stopping at the first failure. Results merge into the results file."""
    recorded = {} if reset else load_results(ctx.root)
    ran = []
    for name in [s for s in STAGE_NAMES if s in stages]:
        start = time.monotonic()
        try:
            result = RUNNERS[name](ctx)
        except (contract.ContractError, verbs.VerbError, policy.PolicyError, secrets.ScanError, coverage.CoverageError, sonar.SonarError, trivy.ScanError, vex.VexError, sast.SastError, baseline.BaselineError) as err:
            result = StageResult(name, "failed", str(err))
        result.seconds = round(time.monotonic() - start, 3)
        ran.append(result)
        recorded[name] = asdict(result)
        save_results(ctx.root, recorded)
        if result.status == "failed":
            break
    return ran


ICONS = {"passed": "passed", "failed": "**failed**", "skipped": "skipped"}


def summary(results: dict[str, dict]) -> str:
    """Markdown for $GITHUB_STEP_SUMMARY: one row per stage, then per-component verb results."""
    lines = ["## rlp gates", "", "| Stage | Result | Detail |", "| --- | --- | --- |"]
    for name, what in STAGES:
        r = results.get(name)
        if r is None:
            lines.append(f"| {name} | not run | {what} |")
        else:
            detail = r["detail"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {name} | {ICONS[r['status']]} | {detail} |")
    per_component = {name: r["components"] for name, r in results.items() if r.get("components")}
    if per_component:
        components = sorted({c for v in per_component.values() for c in v})
        stage_cols = [s for s in STAGE_NAMES if s in per_component]
        lines += ["", "### Per component", "", "| Component | " + " | ".join(stage_cols) + " |", "| --- |" + " --- |" * len(stage_cols)]
        for comp in components:
            cells = [ICONS.get(per_component[s].get(comp, ""), "–") for s in stage_cols]
            lines.append(f"| {comp} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"

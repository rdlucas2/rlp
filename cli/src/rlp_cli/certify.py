"""Template conformance: certify that a Copier template renders a repo that passes every gate.

`rlp template certify` and the template-conformance workflow run the same
sequence:

1. ``template``: rlp-template.yaml is schema-valid and its conformance.answers file exists.
2. ``render``: ``copier copy`` with those answers into a fresh directory, committed as a new git repo.
3. The golden pipeline's gates on the rendered repo, deploy excluded:
   precommit, validate, policy, setup, lint, test, coverage, sast, sonar, package, trivy.
4. Any verb in conformance.verbs the pipeline does not run (``build``), per component.
5. ``run``, ``smoke`` and ``down``: start the repo with the local runtime, smoke-test it, stop it.
   They run when conformance.verbs lists run and smoke.

The sequence stops at the first failure; ``down`` still runs when ``run`` started
anything. A template is certified when no gate failed and none was skipped by
the caller (``--skip``). Gates the pipeline itself skips (sonar without
SONAR_TOKEN) do not block certification.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rlp_cli import __version__, baseline, contract, pipeline, runtime, scaffold, verbs

TEMPLATE_FILE = "rlp-template.yaml"
# The golden gates certification runs: everything before the deploy job.
PIPELINE_GATES = pipeline.STAGE_NAMES[: pipeline.STAGE_NAMES.index("trivy") + 1]
PIPELINE_VERBS = {"setup", "lint", "test", "package"}
RUNTIME_GATES = ["run", "smoke", "down"]
GATES = ["template", "render", *PIPELINE_GATES, "build", *RUNTIME_GATES]
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "rlp-conformance",
    "GIT_AUTHOR_EMAIL": "rlp-conformance@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-conformance",
    "GIT_COMMITTER_EMAIL": "rlp-conformance@example.invalid",
}


class CertifyError(Exception):
    """Certification could not start (bad arguments, not a template)."""


@dataclass
class Gate:
    gate: str
    status: str  # passed | failed | skipped | not run
    detail: str = ""
    seconds: float = 0.0
    data: dict = field(default_factory=dict)


@dataclass
class Options:
    template: Path
    dest: Path | None = None
    vcs_ref: str | None = "HEAD"
    trust: bool = False
    skip: tuple[str, ...] = ()
    smoke_timeout: float = 180.0
    stream: object = None  # where gate output goes; None inherits stdout


@dataclass
class Report:
    template: str
    version: str | None
    contract: str | None
    rlp: str
    source: str
    ref: str | None
    commit: str | None
    rendered: str | None
    certified: bool = False
    started: str = ""
    gates: list[Gate] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["gates"] = [asdict(g) for g in self.gates]
        return out

    def failed(self) -> list[Gate]:
        return [g for g in self.gates if g.status == "failed"]

    def skipped_by_caller(self, skip: tuple[str, ...]) -> list[Gate]:
        return [g for g in self.gates if g.gate in skip and g.status == "skipped"]


def git_output(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def load_template(path: Path) -> tuple[dict, list[contract.Problem]]:
    file = path / TEMPLATE_FILE
    if not file.is_file():
        raise CertifyError(f"no {TEMPLATE_FILE} in {path}; certify runs on a template repo")
    return contract.load_yaml(file), contract.validate_file(file)


def load_answers(path: Path, rel: str) -> dict:
    """The conformance answers, minus Copier's own _-prefixed keys (from a copied .copier-answers.yml)."""
    data = contract.load_yaml(path.joinpath(*rel.split("/")))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise contract.ContractError(f"{rel} must be a mapping of question to answer")
    return {str(k): v for k, v in data.items() if not str(k).startswith("_")}


def commit_rendered(dest: Path) -> None:
    """Make the rendered directory a one-commit git repo: pre-commit and gitleaks need a work tree."""
    env = {**os.environ, **GIT_IDENTITY}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", "Rendered by rlp template certify"]):
        proc = subprocess.run(["git", *args], cwd=dest, env=env, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise scaffold.ScaffoldError(f"git {' '.join(args[:1])} in the rendered repo failed: {proc.stderr.strip()}")


class Run:
    """One certification run: gates in order, stopping at the first failure."""

    def __init__(self, opts: Options, report: Report) -> None:
        self.opts = opts
        self.report = report
        self.stopped = False

    def record(self, gate: Gate) -> Gate:
        self.report.gates.append(gate)
        if gate.status == "failed":
            self.stopped = True
        return gate

    def gate(self, name: str, fn, always: bool = False) -> Gate:
        """Run fn() -> Gate unless an earlier gate failed (and not always) or the caller skipped this one."""
        if self.stopped and not always:
            return self.record(Gate(name, "not run", "an earlier gate failed"))
        if name in self.opts.skip:
            return self.record(Gate(name, "skipped", "skipped with --skip; the template is not certified"))
        start = time.monotonic()
        try:
            result = fn()
        except (contract.ContractError, scaffold.ScaffoldError, verbs.VerbError, runtime.RuntimeFailure, baseline.BaselineError) as err:
            result = Gate(name, "failed", str(err))
        result.seconds = round(time.monotonic() - start, 3)
        return self.record(result)


def certify(opts: Options) -> Report:
    template = opts.template.resolve()
    doc, problems = load_template(template)
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    conformance = doc.get("conformance") if isinstance(doc, dict) else None
    conformance = conformance if isinstance(conformance, dict) else {}
    unknown = [s for s in opts.skip if s not in GATES]
    if unknown:
        raise CertifyError(f"unknown gate(s) for --skip: {', '.join(unknown)}; gates are: {', '.join(GATES)}")
    is_git = git_output(template, "rev-parse", "--is-inside-work-tree") == "true"
    report = Report(
        template=str(meta.get("name") or template.name),
        version=meta.get("version"),
        contract=doc.get("apiVersion") if isinstance(doc, dict) else None,
        rlp=__version__,
        source=str(template),
        ref=opts.vcs_ref if is_git else None,
        commit=git_output(template, "rev-parse", opts.vcs_ref or "HEAD") if is_git else None,
        rendered=None,
        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    run = Run(opts, report)

    # A baseline commit from the caller's repo means nothing in the freshly rendered one:
    # the rendered repo is all new code.
    os.environ.pop(baseline.ENV, None)

    answers: dict = {}

    def gate_template() -> Gate:
        nonlocal answers
        if problems:
            return Gate("template", "failed", "; ".join(str(p) for p in problems), data={"problems": [p.as_dict() for p in problems]})
        rel = conformance["answers"]
        if not template.joinpath(*rel.split("/")).is_file():
            return Gate("template", "failed", f"conformance.answers points at {rel}, which does not exist")
        answers = load_answers(template, rel)
        return Gate("template", "passed", f"{TEMPLATE_FILE} valid; answers from {rel}", data={"answers": answers})

    run.gate("template", gate_template)

    if opts.dest:
        dest = opts.dest.resolve()
    else:
        dest = Path(tempfile.mkdtemp(prefix=f"rlp-certify-{report.template}-"))
        report.rendered = str(dest)  # so cleanup removes it even if render never runs

    def gate_render() -> Gate:
        if dest.exists() and any(dest.iterdir()):
            return Gate("render", "failed", f"{dest} is not empty; certify renders into a fresh directory")
        report.rendered = str(dest)
        scaffold.new(str(template), dest, answers, defaults=True, vcs_ref=opts.vcs_ref if is_git else None, trust=opts.trust)
        commit_rendered(dest)
        ref = f" at {opts.vcs_ref}" if is_git and opts.vcs_ref else ""
        return Gate("render", "passed", f"rendered {report.template}{ref} into {dest}")

    run.gate("render", gate_render)

    ctx = pipeline.Context(root=dest, stream=opts.stream)
    for name in PIPELINE_GATES:
        run.gate(name, lambda name=name: pipeline_gate(ctx, name))

    declared = conformance.get("verbs") or list(verbs.VERBS)
    extra = [v for v in declared if v not in PIPELINE_VERBS and v not in ("run", "smoke")]

    def gate_build() -> Gate:
        if not extra:
            return Gate("build", "skipped", "conformance.verbs lists no verb beyond the pipeline's")
        return run_extra_verbs(dest, extra, opts.stream)

    run.gate("build", gate_build)

    wants_runtime = "run" in declared or "smoke" in declared
    started: dict = {}

    def gate_run() -> Gate:
        if not wants_runtime:
            return Gate("run", "skipped", "conformance.verbs does not list run or smoke")
        repo = contract.load_repo(dest)
        p = runtime.plan(dest, repo, runtime.collect_secrets(dest, repo, None), runtime.generated_credentials(dest, repo))
        runtime.write(dest, p)
        started["project"] = p.project
        started["plan"] = p
        code = runtime.up(dest, p, stdout=opts.stream, stderr=opts.stream)
        if code != 0:
            return Gate("run", "failed", f"docker compose up failed (exit {code})")
        return Gate("run", "passed", f"started {p.project}", data={"endpoints": [e.as_dict() for e in p.endpoints]})

    run.gate("run", gate_run)

    def gate_smoke() -> Gate:
        if not wants_runtime:
            return Gate("smoke", "skipped", "conformance.verbs does not list run or smoke")
        if "plan" not in started:
            return Gate("smoke", "skipped", "run did not start anything")
        checks = runtime.smoke(dest, started["plan"], opts.smoke_timeout)
        shown = "; ".join(f"{c.component} {c.kind} {c.target}: {c.status}" for c in checks) or "nothing to check"
        status = "passed" if checks and all(c.ok for c in checks) else "failed"
        return Gate("smoke", status, shown, data={"checks": [c.as_dict() for c in checks]})

    run.gate("smoke", gate_smoke)

    # down always runs when run started something, even after a failure.
    if "project" in started:
        run.gate("down", lambda: gate_down(dest, started["project"], opts.stream), always=True)
    else:
        run.gate("down", lambda: Gate("down", "skipped", "nothing was started"))

    report.certified = not report.failed() and not report.skipped_by_caller(opts.skip)
    return report


def pipeline_gate(ctx: pipeline.Context, name: str) -> Gate:
    results = pipeline.run(ctx, [name], reset=name == PIPELINE_GATES[0])
    r = results[0]
    return Gate(name, r.status, r.detail, data=r.data | ({"components": r.components} if r.components else {}))


def run_extra_verbs(dest: Path, names: list[str], stream) -> Gate:
    repo = contract.load_repo(dest)
    ran = []
    for verb in names:
        for step in verbs.plan(dest, repo, verb):
            print(f"==> {verb} {step.component}: {' '.join(step.command)}", file=sys.stderr)
            verbs.execute(step, stdout=stream, stderr=stream)
            ran.append(step)
            if not step.ok:
                why = f" ({step.error})" if step.error else ""
                return Gate("build", "failed", f"{verb} {step.component} exited {step.exit_code}{why}", data={"steps": [s.as_dict(dest) for s in ran]})
    return Gate("build", "passed", ", ".join(names) + f": {len(ran)} step(s)", data={"steps": [s.as_dict(dest) for s in ran]})


def gate_down(dest: Path, project: str, stream) -> Gate:
    code = runtime.down(dest, project, volumes=True, stdout=stream, stderr=stream)
    if code != 0:
        return Gate("down", "failed", f"docker compose down failed (exit {code})")
    return Gate("down", "passed", f"stopped {project}")


def _clear_readonly(func, path, _exc) -> None:
    """rmtree hook: git writes its objects read-only, and Windows won't delete read-only files."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass  # best effort: a leftover temp dir is not worth failing over


def cleanup(report: Report, keep: bool) -> None:
    if keep or not report.rendered or not Path(report.rendered).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(report.rendered, onexc=_clear_readonly)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(report.rendered, onerror=_clear_readonly)


def summary(report: dict) -> str:
    """Markdown for $GITHUB_STEP_SUMMARY."""
    verdict = "certified" if report["certified"] else "**not certified**"
    head = f"{report['template']} {report.get('version') or ''}".strip()
    lines = [
        f"## Template conformance: {head} is {verdict}",
        "",
        f"rlp {report['rlp']}, contract {report.get('contract') or 'unknown'}"
        + (f", template commit {report['commit'][:12]}" if report.get("commit") else ""),
        "",
        "| Gate | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for g in report["gates"]:
        detail = g["detail"].replace("|", "\\|").replace("\n", " ")
        status = f"**{g['status']}**" if g["status"] == "failed" else g["status"]
        lines.append(f"| {g['gate']} | {status} | {detail} |")
    return "\n".join(lines) + "\n"

"""The rlp command line.

Every command accepts --output json for agents and scripts. In JSON mode,
stdout carries exactly one JSON document and everything else goes to stderr.
Exit codes: 0 success, 1 the check or verb failed, 2 usage or setup error.
"""

from __future__ import annotations

import dataclasses
import functools
import getpass
import json
import os
import sys
from pathlib import Path

import click

from rlp_cli import __version__, agent, artifacts, baseline, certify, change, contract, coverage, escalate, gate_review, infra, loop, loop_pr, loop_summary, onboard, pipeline, pipeline_report, platform_api, policy, publish, reports, runtime, scaffold, secrets, verbs

OUTPUT_CHOICE = click.Choice(["text", "json"])


def emit_json(payload: dict) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=False))


def fail(ctx: click.Context, message: str, code: int = 2) -> None:
    if ctx.obj["output"] == "json":
        emit_json({"ok": False, "error": message})
    else:
        click.echo(f"error: {message}", err=True)
    ctx.exit(code)


def output_option(func):
    """Add --output to a command; it overrides the group-level --output."""

    @click.option("--output", "-o", "output", type=OUTPUT_CHOICE, default=None, help="Output format.")
    @functools.wraps(func)
    def wrapper(*args, output, **kwargs):
        ctx = click.get_current_context()
        if output:
            ctx.obj["output"] = output
        return func(*args, **kwargs)

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rlp")
@click.option("--output", "-o", "output", type=OUTPUT_CHOICE, default="text", envvar="RLP_OUTPUT", show_default=True, help="Output format.")
@click.pass_context
def cli(ctx: click.Context, output: str) -> None:
    """rlp: validate contracts, check policy, run the standard verbs and scaffold repos."""
    ctx.ensure_object(dict)
    ctx.obj["output"] = output


# --- validate ---


@cli.command()
@click.argument("path", type=click.Path(path_type=Path), default=".")
@output_option
@click.pass_context
def validate(ctx: click.Context, path: Path) -> None:
    """Validate rlp.yaml and rlp-template.yaml against the JSON Schemas.

    PATH is a contract file or a directory (default: the current directory).
    """
    try:
        files = contract.find_contracts(path)
        results = {f: contract.validate_file(f, display=display_name(path, f)) for f in files}
    except contract.ContractError as err:
        fail(ctx, str(err))
    problems = [p for ps in results.values() for p in ps]
    if ctx.obj["output"] == "json":
        emit_json({
            "ok": not problems,
            "files": [display_name(path, f) for f in files],
            "problems": [p.as_dict() for p in problems],
        })
    else:
        for f, ps in results.items():
            name = display_name(path, f)
            if ps:
                click.echo(f"{name}: {len(ps)} problem(s)")
                for p in ps:
                    click.echo(f"  {p.path}: {p.message}")
            else:
                click.echo(f"{name}: valid")
    ctx.exit(1 if problems else 0)


def display_name(base: Path, file: Path) -> str:
    if base.is_dir():
        return file.relative_to(base).as_posix()
    return file.name


# --- policy ---


@cli.group(name="policy")
def policy_group() -> None:
    """Run the rlp policy bundle."""


@policy_group.command(name="check")
@click.argument("path", type=click.Path(path_type=Path, file_okay=False), default=".")
@click.option("--namespace", "-n", "namespaces", multiple=True, help="Only these policy namespaces, e.g. rlp.terraform. Repeatable. Default: all.")
@click.option("--base-results", type=click.Path(path_type=Path, dir_okay=False), default=None, help="results.json of the baseline commit, for the test-count and coverage rules. Default: $RLP_BASE_RESULTS.")
@click.option("--head-results", type=click.Path(path_type=Path, dir_okay=False), default=None, help="results.json of this change. Default: .rlp/pipeline/results.json under PATH.")
@output_option
@click.pass_context
def policy_check(ctx: click.Context, path: Path, namespaces: tuple[str, ...], base_results: Path | None, head_results: Path | None) -> None:
    """Check the repo at PATH against the policy bundle with conftest.

    The rlp.agent rules also judge the change since the baseline commit (see
    policy/README.md): who authored it, the files it touched, and test counts
    and coverage before and after. They warn rather than fail: an agent-authored
    gate change is printed as a warning for a human to review, and the check
    exits 0 when warnings are its only findings.
    """
    try:
        doc = change.build(path, base_results, head_results) if path.is_dir() else None
        report = policy.check(path, namespaces, doc)
    except (policy.PolicyError, change.ChangeError, baseline.BaselineError) as err:
        fail(ctx, str(err))
    violations, warnings = report.violations, report.warnings
    if ctx.obj["output"] == "json":
        emit_json({"ok": not violations, "violations": [v.as_dict() for v in violations], "warnings": [w.as_dict() for w in warnings]})
    else:
        for w in warnings:
            click.echo(f"warning: {w.message}")
        if warnings:
            click.echo(f"{len(warnings)} warning(s): gate changes needing review")
        if violations:
            for v in violations:
                click.echo(v.message)
            click.echo(f"{len(violations)} violation(s)")
        else:
            click.echo("policy: no violations")
    ctx.exit(1 if violations else 0)


# --- standard verbs ---


def run_verb(ctx: click.Context, name: str, components: tuple[str, ...], root: Path) -> None:
    """Run one standard verb for each (selected) component and exit with its result."""
    json_mode = ctx.obj["output"] == "json"
    try:
        doc = contract.load_repo(root)
        steps = verbs.plan(root, doc, name, components)
    except (contract.ContractError, verbs.VerbError) as err:
        fail(ctx, str(err))
    # In JSON mode the verbs' own output goes to stderr so stdout stays one JSON document.
    stream = sys.stderr if json_mode else None
    for step in steps:
        if not json_mode:
            click.echo(f"==> {step.component}: {' '.join(step.command)}", err=True)
        verbs.execute(step, stdout=stream, stderr=stream)
        if step.error:
            click.echo(f"error: {step.component}: {step.error}", err=True)
    ok = all(s.ok for s in steps)
    if json_mode:
        emit_json({"ok": ok, "verb": name, "steps": [s.as_dict(root) for s in steps]})
    else:
        for s in steps:
            status = "ok" if s.ok else f"failed (exit {s.exit_code})"
            click.echo(f"{name} {s.component}: {status}")
    ctx.exit(0 if ok else 1)


def make_verb(name: str) -> click.Command:
    @click.command(name=name, help=f"Run the {name} verb for every component (verbs.{name}, else `task {name}`).")
    @click.option("--component", "-c", "components", multiple=True, help="Only this component. Repeatable.")
    @click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
    @output_option
    @click.pass_context
    def command(ctx: click.Context, components: tuple[str, ...], root: Path) -> None:
        run_verb(ctx, name, components, root)

    return command


# run and smoke drive the local runtime (below); --native runs the verb instead.
for _verb in verbs.VERBS:
    if _verb not in ("run", "smoke"):
        cli.add_command(make_verb(_verb))


# --- local runtime: run, smoke, down ---


def prompt_secret(spec: runtime.SecretSpec) -> str | None:
    if not sys.stdin.isatty():
        return None
    label = f"{spec.component}/{spec.name}" + (f" ({spec.description})" if spec.description else "")
    value = click.prompt(label, hide_input=True, default="", show_default=False)
    return value or None


def runtime_plan(ctx: click.Context, root: Path, interactive: bool) -> runtime.Plan:
    try:
        doc = contract.load_repo(root)
        secret_values = runtime.collect_secrets(root, doc, prompt_secret if interactive else None)
        return runtime.plan(root, doc, secret_values, runtime.generated_credentials(root, doc))
    except contract.ContractError as err:
        fail(ctx, str(err))
    except runtime.RuntimeFailure as err:
        fail(ctx, str(err))
    raise AssertionError("unreachable")  # pragma: no cover


def report_checks(ctx: click.Context, checks: list[runtime.Check], extra: dict | None = None) -> bool:
    ok = all(c.ok for c in checks)
    if ctx.obj["output"] == "json":
        emit_json({"ok": ok, **(extra or {}), "checks": [c.as_dict() for c in checks]})
    else:
        for c in checks:
            click.echo(f"{c.component:12} {c.kind:6} {c.target}: {c.status}{'' if c.ok else ' (failed)'}")
    return ok


def runtime_options(func):
    func = click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")(func)
    func = click.option("--component", "-c", "components", multiple=True, help="With --native, only this component; otherwise only check this component. Repeatable.")(func)
    func = click.option("--native", is_flag=True, help="Run the component's own verb (verbs.<name>, else `task <name>`) instead of the local runtime.")(func)
    return func


@cli.command(name="run")
@runtime_options
@click.option("--no-start", is_flag=True, help="Only write .rlp/compose.yaml and the files it needs.")
@click.option("--no-package", is_flag=True, help="Use the zip and jar artifacts already built instead of running their package verb first.")
@click.option("--timeout", default=180.0, show_default=True, help="Seconds to wait for every component to pass smoke after starting.")
@output_option
@click.pass_context
def run_cmd(ctx: click.Context, root: Path, components: tuple[str, ...], native: bool, no_start: bool, no_package: bool, timeout: float) -> None:
    """Start every component and its backing services locally with Docker Compose.

    Generates .rlp/compose.yaml from rlp.yaml, builds and starts it in the
    background, then waits until `rlp smoke` passes. Secrets are asked for
    once (or read from the environment) and kept in .rlp/secrets/.
    """
    if native:
        run_verb(ctx, "run", components, root)
    json_mode = ctx.obj["output"] == "json"
    p = runtime_plan(ctx, root, interactive=not json_mode)
    if p.extract and not no_package:
        package_unpacked(ctx, root, p)
    try:
        written = runtime.write(root, p)
    except runtime.RuntimeFailure as err:
        fail(ctx, str(err))
    for path in runtime.unignored(root, [f for f in written if f.is_file()]):
        click.echo(f"warning: git does not ignore {path.relative_to(root).as_posix()}; add .rlp/ to .gitignore", err=True)
    if no_start:
        if json_mode:
            emit_json({"ok": True, "project": p.project, "compose": (root / runtime.RLP_DIR / "compose.yaml").as_posix(), "endpoints": [e.as_dict() for e in p.endpoints]})
        else:
            click.echo(f"wrote {(root / runtime.RLP_DIR / 'compose.yaml').as_posix()}")
        ctx.exit(0)
    stream = sys.stderr if json_mode else None
    try:
        code = runtime.up(root, p, stdout=stream, stderr=stream)
    except runtime.RuntimeFailure as err:
        fail(ctx, str(err))
    if code != 0:
        fail(ctx, f"docker compose up failed (exit {code})", code=1)
    checks = runtime.smoke(root, p, timeout, only=components)
    ok = report_checks(ctx, checks, {"project": p.project, "endpoints": [e.as_dict() for e in p.endpoints]})
    if not json_mode:
        click.echo("running; stop with: rlp down" if ok else "started, but smoke failed; logs: docker compose -p " + p.project + " logs")
    ctx.exit(0 if ok else 1)


def package_unpacked(ctx: click.Context, root: Path, p: runtime.Plan) -> None:
    """Run the package verb for zip and jar functions, whose archives the runtime unpacks."""
    names = tuple(target.split("/", 1)[1] for target in p.extract)
    try:
        steps = verbs.plan(root, contract.load_repo(root), "package", names)
    except (contract.ContractError, verbs.VerbError) as err:
        fail(ctx, str(err))
    for step in steps:
        click.echo(f"==> {step.component}: {' '.join(step.command)}", err=True)
        verbs.execute(step, stdout=sys.stderr, stderr=sys.stderr)
        if not step.ok:
            fail(ctx, f"package {step.component} failed (exit {step.exit_code}){': ' + step.error if step.error else ''}", code=1)


@cli.command(name="smoke")
@runtime_options
@click.option("--timeout", default=30.0, show_default=True, help="Seconds to keep retrying before reporting failure.")
@output_option
@click.pass_context
def smoke_cmd(ctx: click.Context, root: Path, components: tuple[str, ...], native: bool, timeout: float) -> None:
    """Check each running component: health endpoints, a function invoke, or a job's exit code."""
    if native:
        run_verb(ctx, "smoke", components, root)
    p = smoke_plan(ctx, root)
    checks = runtime.smoke(root, p, timeout, only=components)
    ctx.exit(0 if report_checks(ctx, checks) else 1)


def smoke_plan(ctx: click.Context, root: Path) -> runtime.Plan:
    """The plan smoke checks against: secrets are not needed to know the endpoints."""
    try:
        doc = contract.load_repo(root)
        return runtime.plan(root, doc, strict=False)
    except (contract.ContractError, runtime.RuntimeFailure) as err:
        fail(ctx, str(err))
    raise AssertionError("unreachable")  # pragma: no cover


@cli.command(name="down")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--volumes", "-v", is_flag=True, help="Also delete the local databases' volumes.")
@output_option
@click.pass_context
def down_cmd(ctx: click.Context, root: Path, volumes: bool) -> None:
    """Stop and remove everything `rlp run` started."""
    json_mode = ctx.obj["output"] == "json"
    try:
        doc = contract.load_repo(root)
        code = runtime.down(root, runtime.project_name(doc), volumes, stdout=sys.stderr if json_mode else None, stderr=sys.stderr if json_mode else None)
    except (contract.ContractError, runtime.RuntimeFailure) as err:
        fail(ctx, str(err))
    if json_mode:
        emit_json({"ok": code == 0, "exit_code": code})
    elif code == 0:
        click.echo("down: stopped")
    ctx.exit(0 if code == 0 else 1)


# --- reports ---


@cli.command(name="reports")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@output_option
@click.pass_context
def reports_cmd(ctx: click.Context, root: Path) -> None:
    """Print each component's report paths and the Sonar properties they map to."""
    try:
        doc = contract.load_repo(root)
    except contract.ContractError as err:
        fail(ctx, str(err))
    entries = reports.resolve(doc)
    props = reports.sonar_properties(entries)
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "reports": entries, "sonar": props})
        return
    if not entries:
        click.echo("no reports declared")
        return
    for e in entries:
        prop = e["sonar_property"] or "(no Sonar importer)"
        click.echo(f"{e['component']} {e['report']} ({e['format']}): {e['path']} -> {prop}")
    if props:
        click.echo("")
        click.echo("# sonar-project.properties")
        for k, v in props.items():
            click.echo(f"{k}={v}")


# --- artifacts, coverage, secrets, publish ---


def split_components(values: tuple[str, ...]) -> tuple[str, ...]:
    """--component a --component b, or --components a,b (the golden workflow's input)."""
    return tuple(c.strip() for v in values for c in v.split(",") if c.strip())


@cli.command(name="artifacts")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@output_option
@click.pass_context
def artifacts_cmd(ctx: click.Context, root: Path) -> None:
    """Print what each component's package verb produces (image tag or file path)."""
    try:
        doc = contract.load_repo(root)
    except contract.ContractError as err:
        fail(ctx, str(err))
    entries = artifacts.resolve(root, doc)
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "artifacts": entries})
        return
    for e in entries:
        click.echo(f"{e['component']} {e['kind']}: {e.get('image') or e.get('path')}")


@cli.command(name="coverage")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--component", "-c", "components", multiple=True, help="Only this component. Repeatable.")
@output_option
@click.pass_context
def coverage_cmd(ctx: click.Context, root: Path, components: tuple[str, ...]) -> None:
    """Check each component's coverage report against quality.coverage.minimum (or the platform floor)."""
    try:
        doc = contract.load_repo(root)
    except contract.ContractError as err:
        fail(ctx, str(err))
    results = coverage.check(root, doc, split_components(components))
    ok = all(r.ok for r in results)
    if ctx.obj["output"] == "json":
        emit_json({"ok": ok, "results": [r.as_dict() for r in results]})
    elif not results:
        click.echo("no component declares a coverage report")
    else:
        for r in results:
            if r.error:
                click.echo(f"{r.component}: {r.error}")
            else:
                verdict = "ok" if r.ok else "below minimum"
                click.echo(f"{r.component}: {r.percent:.1f}% of lines ({r.format}), minimum {r.minimum:g}%: {verdict}")
    ctx.exit(0 if ok else 1)


@cli.group(name="agent")
def agent_group() -> None:
    """The agent provider that remediation loops use (docs/loops.md)."""


def provider_option(func):
    """Add --provider; without it, RLP_AGENT_PROVIDER, else claude-code."""
    return click.option(
        "--provider",
        type=click.Choice(sorted(agent.PROVIDERS)),
        default=None,
        help=f"Agent provider. Default: ${agent.PROVIDER_ENV}, else {agent.DEFAULT_PROVIDER}.",
    )(func)


@agent_group.command(name="check")
@provider_option
@output_option
@click.pass_context
def agent_check(ctx: click.Context, provider: str | None) -> None:
    """Check that the agent provider can run here: credentials and tools."""
    try:
        chosen = agent.get_provider(provider)
        chosen.check()
    except agent.AgentError as err:
        fail(ctx, str(err))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "provider": chosen.name})
    else:
        click.echo(f"agent provider {chosen.name}: ready")


# --- loop ---


def loop_status_lines(rows: list[dict]) -> list[str]:
    lines = []
    for r in rows:
        state = "on" if r["enabled"] else "off"
        note = "" if r["implemented"] else " (not implemented yet)"
        lines.append(f"{r['loop']:8} {state:3} {r['reason']}{note}")
    return lines


def run_loop(ctx: click.Context, kind: str, root: Path, provider: str | None, max_iterations: int, max_spend: float | None, dry_run: bool) -> None:
    """Run one loop kind and exit with its outcome's code."""
    json_mode = ctx.obj["output"] == "json"
    try:
        opts = loop.Options(
            max_iterations=max_iterations,
            max_spend_usd=loop.default_max_spend() if max_spend is None else max_spend,
            dry_run=dry_run,
            provider=provider,
            on_event=None if json_mode else functools.partial(click.echo, err=True),
        )
        report = loop.run(root, kind, opts, stream=sys.stderr)
    except (loop.LoopError, agent.AgentError, contract.ContractError) as err:
        fail(ctx, str(err))
    if json_mode:
        emit_json(report.payload)
    elif report.outcome == "dry-run":
        p = report.payload
        click.echo(f"{kind}: dry run, {len(p['findings'])} finding(s); no agent called")
        for f in p["findings"]:
            click.echo(f"  - {f.get('message', '')}")
        click.echo(f"verify stages: {', '.join(p['verifyStages'])}; allowed paths: {', '.join(p['allowedPaths'])}")
        click.echo(f"limits: {p['limits']['maxIterations']} iteration(s), ${p['limits']['maxSpendUsd']:.2f}; provider {p['provider']}")
    else:
        click.echo(f"{kind}: {report.outcome}: {report.reason}")
        if report.state_file:
            click.echo(f"state: {report.state_file}")
    ctx.exit(report.exit_code)


def make_loop_command(kind: str) -> click.Command:
    registered = loop.KINDS.get(kind)
    summary = registered.description if registered else f"The {kind} remediation loop."

    @click.command(name=kind, help=f"{summary} Off unless rlp.yaml sets loops.enabled: true; RLP_LOOPS=off turns it off.")
    @click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
    @provider_option
    @click.option("--max-iterations", type=click.IntRange(min=1), default=loop.DEFAULT_MAX_ITERATIONS, show_default=True, help="Stop and escalate after this many agent calls.")
    @click.option("--max-spend", type=click.FloatRange(min=0, min_open=True), default=None, help="Spend cap for the run in USD. Default: loop_max_spend_usd in policy/platform/data.yaml.")
    @click.option("--dry-run", is_flag=True, help="Discover and print the plan; call no agent and change nothing.")
    @output_option
    @click.pass_context
    def command(ctx: click.Context, root: Path, provider: str | None, max_iterations: int, max_spend: float | None, dry_run: bool) -> None:
        run_loop(ctx, kind, root, provider, max_iterations, max_spend, dry_run)

    return command


class LoopGroup(click.Group):
    """`rlp loop status`, `rlp loop escalate`, `rlp loop pr` and `rlp loop summary`, plus one subcommand per loop kind, read from the registry when called."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *loop.kind_names()})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        found = super().get_command(ctx, cmd_name)
        if found is not None:
            return found
        return make_loop_command(cmd_name) if cmd_name in loop.kind_names() else None


@cli.group(name="loop", cls=LoopGroup)
def loop_group() -> None:
    """Remediation loops: an agent fixes what a gate found, bounded by iteration and spend caps (docs/loops.md)."""


@loop_group.command(name="status")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@output_option
@click.pass_context
def loop_status(ctx: click.Context, root: Path) -> None:
    """Show whether each loop kind is on for this repo, and why."""
    try:
        rows = loop.status(root)
    except contract.ContractError as err:
        fail(ctx, str(err))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "loops": rows})
        return
    for line in loop_status_lines(rows):
        click.echo(line)


@loop_group.command(name="escalate")
@click.argument("state_file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--post", is_flag=True, help="Open the escalation issue on GitHub, or comment on the open one for the same loop and base branch.")
@click.option("--repo", default=None, help="OWNER/NAME to post to. Default: $GITHUB_REPOSITORY.")
@click.option("--trigger", default=None, help="Link to the run that started the loop. Default: this GitHub Actions run, when there is one.")
@output_option
@click.pass_context
def loop_escalate(ctx: click.Context, state_file: Path, post: bool, repo: str | None, trigger: str | None) -> None:
    """Render the escalation issue for a loop that gave up, from its STATE_FILE.

    Without --post, prints the issue's Markdown. With --post, opens the issue
    (labels needs:human, area:agent and loop:<kind>) or, when one is already
    open for the same loop kind and base branch, comments on it. Posting reads
    the token from GH_TOKEN, else GITHUB_TOKEN.
    """
    json_mode = ctx.obj["output"] == "json"
    try:
        state = escalate.load_state(state_file)
        link = escalate.trigger_url() if trigger is None else trigger
        shown = state_file.as_posix()
        payload = {"ok": True, "title": escalate.title(state), "labels": escalate.labels(state), "body": escalate.render(state, link, shown)}
        if post:
            target = escalate.repo_from_env(repo)
            posted = escalate.post(escalate.client_from_env(), target, state, link, shown)
            payload |= {"repo": target, **posted.as_dict()}
    except escalate.EscalationError as err:
        fail(ctx, str(err))
    if json_mode:
        emit_json(payload)
    elif post:
        click.echo(f"{posted.action} {target}#{posted.number}: {posted.url}")
    else:
        click.echo(payload["body"], nl=False)


@loop_group.command(name="pr")
@click.argument("state_file", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--post", is_flag=True, help="Open the pull request from the loop branch into its base on GitHub. Push the branch first.")
@click.option("--repo", default=None, help="OWNER/NAME to post to. Default: $GITHUB_REPOSITORY.")
@click.option("--trigger", default=None, help="Link to the run that started the loop. Default: this GitHub Actions run, when there is one.")
@output_option
@click.pass_context
def loop_pull_request(ctx: click.Context, state_file: Path, post: bool, repo: str | None, trigger: str | None) -> None:
    """Render the pull request for a loop run that fixed its gate, from its STATE_FILE.

    Only a fixed run whose verify stages all passed on its last commit can
    open one. Without --post, prints the PR's Markdown. With --post, opens the
    PR from the loop branch into the base branch and labels it agent:authored
    and loop:<kind>. Posting reads the token from GH_TOKEN, else GITHUB_TOKEN.
    """
    json_mode = ctx.obj["output"] == "json"
    try:
        state = loop_pr.load_fixed(state_file)
        link = escalate.trigger_url() if trigger is None else trigger
        payload = {"ok": True, "title": loop_pr.title(state), "head": state["branch"], "base": state["base"],
                   "labels": loop_pr.labels(state), "body": loop_pr.render(state, link)}
        if post:
            target = escalate.repo_from_env(repo)
            posted = loop_pr.post(escalate.client_from_env(), target, state, link)
            payload |= {"repo": target, **posted.as_dict()}
    except (loop_pr.PullRequestError, escalate.EscalationError) as err:
        fail(ctx, str(err))
    if json_mode:
        emit_json(payload)
    elif post:
        click.echo(f"{posted.action} {target}#{posted.number}: {posted.url}")
    else:
        click.echo(payload["body"], nl=False)


@loop_group.command(name="summary")
@click.argument("state_file", type=click.Path(path_type=Path, dir_okay=False))
@output_option
@click.pass_context
def loop_summary_command(ctx: click.Context, state_file: Path) -> None:
    """Render a loop run's telemetry from its STATE_FILE, for the job summary.

    Prints a Markdown table, one row per iteration (findings before and after,
    gate results, cost, duration), then the outcome and the total spend. Loop
    workflows append it to $GITHUB_STEP_SUMMARY. Works for any outcome.
    """
    try:
        state = escalate.read_state(state_file)
    except escalate.EscalationError as err:
        fail(ctx, str(err))
    body = loop_summary.render(state)
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "loop": state["loop"], "runId": state["runId"], "outcome": state["outcome"],
                   "iterations": len(state["iterations"]), "totalSpendUsd": round(loop_summary.total_spend(state), 6), "body": body})
    else:
        click.echo(body, nl=False)


@cli.group(name="secrets")
def secrets_group() -> None:
    """Scan for committed secrets with the pinned gitleaks."""


@secrets_group.command(name="scan")
@click.argument("path", type=click.Path(path_type=Path, file_okay=False), default=".")
@output_option
@click.pass_context
def secrets_scan(ctx: click.Context, path: Path) -> None:
    """Scan PATH: its full git history in a work tree, else its files."""
    try:
        findings = secrets.scan(path)
    except secrets.ScanError as err:
        fail(ctx, str(err))
    if ctx.obj["output"] == "json":
        emit_json({"ok": not findings, "findings": findings})
    elif findings:
        for f in findings:
            click.echo(f"{f['file']}:{f['line']} {f['rule']} {f['commit']}".rstrip())
        click.echo(f"{len(findings)} secret(s) found")
    else:
        click.echo("secrets: none found")
    ctx.exit(1 if findings else 0)


@cli.command(name="publish")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--registry", help="Container registry prefix for images, e.g. ghcr.io/owner/repo.")
@click.option("--release", help="GitHub Release tag to attach zip and jar artifacts to.")
@click.option("--component", "-c", "components", multiple=True, help="Only this component. Repeatable.")
@output_option
@click.pass_context
def publish_cmd(ctx: click.Context, root: Path, registry: str | None, release: str | None, components: tuple[str, ...]) -> None:
    """Publish packaged artifacts: images to the registry, zips and jars to a GitHub Release."""
    try:
        doc = contract.load_repo(root)
        done = publish.publish(root, doc, registry, release, split_components(components))
    except (contract.ContractError, publish.PublishError) as err:
        fail(ctx, str(err), code=1)
    publish.save(root / pipeline.PUBLISHED, done)
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "published": done})
    else:
        for d in done:
            click.echo(f"{d['component']} {d['kind']}: {d['status']} ({d['detail']})")


# --- pipeline ---


@cli.group(name="pipeline")
def pipeline_group() -> None:
    """Run the golden pipeline's gates locally, exactly as CI does."""


@pipeline_group.command(name="stages")
@output_option
@click.pass_context
def pipeline_stages(ctx: click.Context) -> None:
    """List the gates in pipeline order."""
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "stages": [{"stage": n, "gates": w} for n, w in pipeline.STAGES]})
        return
    for name, what in pipeline.STAGES:
        click.echo(f"{name:10} {what}")


@pipeline_group.command(name="run")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--stage", "-s", "stages", multiple=True, type=click.Choice(pipeline.STAGE_NAMES), help="Run only this stage. Repeatable. Default: every stage.")
@click.option("--component", "-c", "components", multiple=True, help="Only these components (repeatable, or comma-separated).")
@click.option("--deploy-env", default="", help="Target environment for the deploy stage.")
@click.option("--deploy-local", is_flag=True, help="Target LocalStack instead of AWS (for act); the AWS terraform and deploy stages then skip.")
@click.option("--deploy-timeout", default=300.0, show_default=True, help="Seconds the deploy stage waits for readiness through the load balancer.")
@click.option("--reset", is_flag=True, help="Discard earlier results in .rlp/pipeline/results.json first.")
@output_option
@click.pass_context
def pipeline_run(ctx: click.Context, root: Path, stages: tuple[str, ...], components: tuple[str, ...], deploy_env: str, deploy_local: bool, deploy_timeout: float, reset: bool) -> None:
    """Run the gates in order and stop at the first failure.

    The terraform and deploy stages read RLP_REGISTRY, RLP_TF_STATE_BUCKET,
    AWS_REGION and RLP_MODULES_SOURCE from the environment (see docs/aws.md).
    """
    json_mode = ctx.obj["output"] == "json"
    run_ctx = pipeline.Context(
        root=root,
        components=split_components(components),
        deploy_env=deploy_env,
        deploy_local=deploy_local,
        smoke_timeout=deploy_timeout,
        stream=sys.stderr if json_mode else None,
    )
    results = pipeline.run(run_ctx, list(stages) or pipeline.STAGE_NAMES, reset=reset)
    ok = all(r.status != "failed" for r in results)
    if json_mode:
        emit_json({"ok": ok, "stages": [dataclasses.asdict(r) for r in results]})
    else:
        for r in results:
            click.echo(f"{r.stage:10} {r.status:8} {r.detail}")
    ctx.exit(0 if ok else 1)


@pipeline_group.command(name="summary")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@output_option
@click.pass_context
def pipeline_summary(ctx: click.Context, root: Path) -> None:
    """Print the recorded gate results as Markdown (for $GITHUB_STEP_SUMMARY)."""
    results = pipeline.load_results(root)
    if ctx.obj["output"] == "json":
        failed = [r for r in results.values() if r["status"] == "failed"]
        emit_json({"ok": not failed, "stages": list(results.values())})
        return
    click.echo(pipeline.summary(results), nl=False)


def _unreachable(err: platform_api.PlatformError) -> bool:
    """No answer, or the platform's own failure: the pipeline goes on without the report."""
    return err.status is None or err.status >= 500


@pipeline_group.command(name="report")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--deployment", is_flag=True, help="Report the deploy stage's deployments instead of the build.")
@click.option("--repo", default="", help="OWNER/NAME. Default: GITHUB_REPOSITORY, else the origin remote.")
@click.option("--commit", default="", help="The commit the run built. Default: GITHUB_SHA, else HEAD.")
@click.option("--branch", default="", help="Default: GITHUB_HEAD_REF, else GITHUB_REF_NAME, else the checked-out branch.")
@click.option("--deploy-env", default="", help="Environment, when the deploy stage didn't record one. Default: RLP_DEPLOY_ENV.")
@click.option("--component", "-c", "components", multiple=True, help="Only these components' deployments (repeatable, or comma-separated).")
@output_option
@click.pass_context
def pipeline_report_cmd(ctx: click.Context, root: Path, deployment: bool, repo: str, commit: str, branch: str, deploy_env: str, components: tuple[str, ...]) -> None:
    """Send this run's gate results (or its deployments) to the rlp platform.

    The build is .rlp/pipeline/results.json plus what `rlp publish` pushed;
    reporting the same run attempt (GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT) again
    updates the same build. With no platform configured this does nothing and
    exits 0; when the platform can't be reached it warns and exits 0, so a
    pipeline never fails for want of a report.
    """
    json_mode = ctx.obj["output"] == "json"

    def done(message: str, **payload) -> None:
        if json_mode:
            emit_json({"ok": True, **payload, "message": message})
        else:
            click.echo(message)
        ctx.exit(0)

    def warn(message: str) -> None:
        if json_mode:
            emit_json({"ok": True, "reported": False, "warning": message})
        else:
            click.echo(f"warning: {message}", err=True)
        ctx.exit(0)

    try:
        config = platform_api.load_config()
    except platform_api.ConfigError as err:
        warn(f"{err}; nothing reported")
    if not config.url or not config.token:
        done(f"no platform configured ({platform_api.URL_ENV} and {platform_api.TOKEN_ENV} are unset); nothing reported", reported=False)
    try:
        run = pipeline_report.resolve_run(root, repo=repo, commit=commit, branch=branch)
        if deployment:
            bodies = pipeline_report.deployments(root, run, deploy_env=deploy_env, only=split_components(components))
        else:
            body = pipeline_report.build(root, run)
            bodies = [body] if body is not None else []
    except pipeline_report.ReportError as err:
        fail(ctx, str(err))
    if not bodies:
        what = "no deploy stage ran" if deployment else f"no {pipeline.RESULTS.as_posix()} and nothing published"
        done(f"{what}; nothing reported", reported=False)
    try:
        with platform_api.PlatformClient.from_config(config, transport=ctx.obj.get("platform_transport")) as client:
            if deployment:
                recorded = [client.report_deployment(b) for b in bodies]
            else:
                recorded = [client.report_build(bodies[0])]
    except platform_api.PlatformError as err:
        if _unreachable(err):
            warn(f"{err}; nothing reported")
        fail(ctx, str(err), code=1)
    if json_mode:
        emit_json({"ok": True, "reported": True, ("deployments" if deployment else "build"): recorded if deployment else recorded[0]})
        return
    for r in recorded:
        if deployment:
            click.echo(f"deployment {r['id']}: {r['repo']} {r['component']} to {r['environment']}: {r['status']} ({r['artifact']['reference']})")
        else:
            failed = f" (failed: {', '.join(r['failed_stages'])})" if r["failed_stages"] else ""
            click.echo(f"build {r['id']}: {r['repo']}@{r['commit'][:12]} run {r['run_id']} attempt {r['run_attempt']}: {'passed' if r['passed'] else 'failed'}{failed}")


@pipeline_group.command(name="gate-review")
@click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
@click.option("--results", "results_file", type=click.Path(path_type=Path, dir_okay=False), default=None, help="results.json to read. Default: .rlp/pipeline/results.json under --path.")
@click.option("--repo", default="", help="OWNER/NAME of the pull request. Default: from the GitHub event.")
@click.option("--pr", "number", type=click.IntRange(min=1), default=None, help="Pull request number. Default: from the GitHub event.")
@click.option("--head-sha", default="", help="The PR's head commit. Default: from the GitHub event.")
@click.option("--author", default="", help="The PR author's login. Default: from the GitHub event.")
@output_option
@click.pass_context
def pipeline_gate_review(ctx: click.Context, root: Path, results_file: Path | None, repo: str, number: int | None, head_sha: str, author: str) -> None:
    """Hold agent gate changes for a human's approval (#73).

    Passes when the policy stage recorded no gate changes. Otherwise passes
    only when the pull request has an approving review on its head commit from
    someone who is neither its author nor an agent account. Reads reviews with
    GH_TOKEN (else GITHUB_TOKEN). Exits 1 when the review is missing.
    """
    try:
        results = gate_review.load_results(results_file or pipeline.results_path(root))
        pr = gate_review.resolve_pull_request(gate_review.pull_request_from_env(), repo, number, head_sha, author)
        verdict = gate_review.review(results, pr, gate_review.client_from_env, gate_review.agent_authors())
    except gate_review.GateReviewError as err:
        fail(ctx, str(err))
    if ctx.obj["output"] == "json":
        emit_json(verdict.as_dict())
    else:
        click.echo(gate_review.summary(verdict), nl=False)
    ctx.exit(0 if verdict.ok else 1)


# --- infra ---


@cli.group(name="infra")
def infra_group() -> None:
    """Render and tear down the AWS Terraform stack generated from rlp.yaml."""


def infra_options(func):
    for option in reversed([
        click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory)."),
        click.option("--env", "env", required=True, help="Environment, for example dev."),
        click.option("--component", "-c", "components", multiple=True, help="Only these components (repeatable, or comma-separated)."),
        click.option("--registry", help="Image registry prefix, e.g. ghcr.io/owner/repo. Default: $RLP_REGISTRY."),
        click.option("--state-bucket", help="Terraform state bucket. Default: $RLP_TF_STATE_BUCKET; without one, state stays local."),
        click.option("--region", help="AWS region. Default: $AWS_REGION, else us-east-1."),
        click.option("--modules", help="Module source: a directory holding the modules, or a go-getter URL. Default: $RLP_MODULES_SOURCE, else the modules from wherever this rlp was installed from (its git commit, its checkout, or its release tag)."),
    ]):
        func = option(func)
    return func


def render_stack(ctx: click.Context, root: Path, env: str, components: tuple[str, ...], **given: str | None) -> infra.Stack:
    try:
        stack = infra.render(root, contract.load_repo(root), env, infra.Settings.from_env(**given), split_components(components))
    except (contract.ContractError, infra.InfraError) as err:
        fail(ctx, str(err))
    return stack


@infra_group.command(name="render")
@infra_options
@output_option
@click.pass_context
def infra_render(ctx: click.Context, root: Path, env: str, components: tuple[str, ...], **given: str | None) -> None:
    """Write .rlp/infra/ENV/main.tf.json for the components that deploy to AWS."""
    stack = render_stack(ctx, root, env, components, **given)
    path = (stack.dir / infra.STACK_FILE).relative_to(root).as_posix()
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "env": env, "file": path if stack.components else None, "components": stack.components, "skipped": stack.skipped, "backend": stack.backend})
        return
    for name, why in stack.skipped.items():
        click.echo(f"skipped {name}: {why}", err=True)
    if not stack.components:
        click.echo("nothing deploys to AWS; no stack written")
        return
    if not stack.backend:
        click.echo("warning: no state bucket given; terraform keeps state in the stack directory", err=True)
    click.echo(f"wrote {path} ({', '.join(stack.components)})")


@infra_group.command(name="destroy")
@infra_options
@click.option("--auto-approve", is_flag=True, help="Destroy without asking terraform's confirmation.")
@output_option
@click.pass_context
def infra_destroy(ctx: click.Context, root: Path, env: str, components: tuple[str, ...], auto_approve: bool, **given: str | None) -> None:
    """Destroy the environment's stack: render it, init against its state, terraform destroy."""
    json_mode = ctx.obj["output"] == "json"
    if json_mode and not auto_approve:
        fail(ctx, "--output json needs --auto-approve: there is no terminal to confirm on")
    stack = render_stack(ctx, root, env, components, **given)
    if not stack.components:
        fail(ctx, "nothing in rlp.yaml deploys to AWS, so there is no stack to destroy")
    stream = sys.stderr if json_mode else None
    try:
        infra.init(stack, stream=stream)
        infra.destroy(stack, auto_approve, stream=stream)
    except infra.InfraError as err:
        fail(ctx, str(err), code=1)
    if json_mode:
        emit_json({"ok": True, "env": env, "destroyed": stack.components})
    else:
        click.echo(f"destroyed {env}: {', '.join(stack.components)}")


# --- onboard ---


@cli.command(name="onboard")
@click.argument("repo", metavar="OWNER/REPO")
@click.option("--env", "env", default="dev", show_default=True, help="Environment the repo's pipeline deploys to (RLP_DEPLOY_ENV).")
@click.option(
    "--bootstrap-dir",
    "bootstrap",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("infra/bootstrap"),
    show_default=True,
    help="The bootstrap stack's directory, where its state lives.",
)
@click.option("--dry-run", is_flag=True, help="Print what would change, and change nothing.")
@click.option("--yes", "-y", "yes", is_flag=True, help="Apply the bootstrap without asking terraform's confirmation.")
@output_option
@click.pass_context
def onboard_cmd(ctx: click.Context, repo: str, env: str, bootstrap: Path, dry_run: bool, yes: bool) -> None:
    """Let OWNER/REPO deploy through the golden pipeline.

    Adds the repo to the bootstrap's OIDC trust (terraform apply, with every
    other bootstrap variable unchanged), then sets its RLP_* repository
    variables and, when INFRACOST_API_KEY is set in the environment, the
    INFRACOST_API_KEY secret. Uses your own AWS credentials and gh login.
    """
    json_mode = ctx.obj["output"] == "json"
    try:
        plan, terraform, gh = onboard.prepare(repo, env, bootstrap)
    except (onboard.OnboardError, infra.InfraError) as err:
        fail(ctx, str(err))
    doc = plan.as_dict()

    if plan.onboarded:
        if json_mode:
            emit_json({"ok": True, "dry_run": dry_run, **doc, "applied": False, "set": []})
        else:
            click.echo(f"{plan.repo} is already onboarded ({env})")
        return

    if not json_mode:
        describe_onboard(plan, dry_run)
    if dry_run:
        if json_mode:
            emit_json({"ok": True, "dry_run": True, **doc, "applied": False, "set": []})
        return
    if json_mode and plan.add_trust and not yes:
        fail(ctx, "--output json needs --yes when the bootstrap must be applied: there is no terminal to confirm on")

    done: list[str] = []
    try:
        if plan.add_trust:
            onboard.apply_trust(terraform, bootstrap, plan, yes, stream=sys.stderr if json_mode else None)
        done += onboard.set_variables(gh, plan)
        if plan.secret == "set":
            onboard.set_secret(gh, plan, os.environ[onboard.SECRET])
            done.append(onboard.SECRET)
    except onboard.StepError as err:
        if json_mode:
            emit_json({"ok": False, "error": str(err), **doc, "set": done})
        else:
            click.echo(f"error: {err}", err=True)
        ctx.exit(1)
    if json_mode:
        emit_json({"ok": True, "dry_run": False, **doc, "applied": plan.add_trust, "set": done})
    else:
        click.echo(f"onboarded {plan.repo} ({env})")


def describe_onboard(plan: onboard.Plan, dry_run: bool) -> None:
    would = "would " if dry_run else ""
    if plan.add_trust:
        click.echo(f"trust: {would}add {plan.repo} to the bootstrap's github_repositories")
        click.echo(f"  {json.dumps(plan.trusted)} -> {json.dumps(plan.tf_vars['github_repositories'])}")
    else:
        click.echo(f"trust: {plan.repo} is already trusted")
    for name, value in plan.variables.items():
        state = f"{would}set" if name in plan.changed_variables else "unchanged"
        click.echo(f"variable {name}={value} ({state})")
    if plan.secret == "set":
        click.echo(f"secret {onboard.SECRET}: {would}set from $INFRACOST_API_KEY")
    elif plan.secret == "exists":
        click.echo(f"secret {onboard.SECRET}: already set; kept (replace it with `gh secret set`)")
    else:
        click.echo(f"secret {onboard.SECRET}: skipped (INFRACOST_API_KEY is not set)")


# --- templates ---


@cli.command()
@click.argument("template")
@click.argument("dest", type=click.Path(path_type=Path, file_okay=False), default=".")
@click.option("--data", "-d", "data", multiple=True, metavar="KEY=VALUE", help="Answer a template question. Repeatable.")
@click.option("--defaults", is_flag=True, help="Use the default answer for every question not given with --data.")
@click.option("--vcs-ref", help="Template git ref to render (default: latest tag).")
@click.option("--trust", is_flag=True, help="Allow the template's tasks and extensions to run.")
@output_option
@click.pass_context
def new(ctx: click.Context, template: str, dest: Path, data: tuple[str, ...], defaults: bool, vcs_ref: str | None, trust: bool) -> None:
    """Render TEMPLATE into DEST with Copier, then validate the result."""
    try:
        scaffold.new(template, dest, scaffold.parse_data(data), defaults, vcs_ref, trust)
    except scaffold.ScaffoldError as err:
        fail(ctx, str(err), code=1)
    report_render(ctx, dest, "new")


@cli.command()
@click.argument("path", type=click.Path(path_type=Path, file_okay=False), default=".")
@click.option("--data", "-d", "data", multiple=True, metavar="KEY=VALUE", help="Change an answer. Repeatable.")
@click.option("--defaults", is_flag=True, help="Keep previous answers without asking.")
@click.option("--vcs-ref", help="Template git ref to update to (default: latest tag).")
@click.option("--trust", is_flag=True, help="Allow the template's tasks, migrations and extensions to run.")
@output_option
@click.pass_context
def update(ctx: click.Context, path: Path, data: tuple[str, ...], defaults: bool, vcs_ref: str | None, trust: bool) -> None:
    """Update the repo at PATH to a newer version of its template with Copier, then validate it."""
    try:
        scaffold.update(path, scaffold.parse_data(data), defaults, vcs_ref, trust)
    except scaffold.ScaffoldError as err:
        fail(ctx, str(err), code=1)
    report_render(ctx, path, "update")


@cli.group(name="template")
def template_group() -> None:
    """Work on rlp templates: certify that one renders a repo passing every gate."""


@template_group.command(name="certify")
@click.argument("path", type=click.Path(path_type=Path, file_okay=False), default=".")
@click.option("--dest", type=click.Path(path_type=Path, file_okay=False), help="Render into this empty directory (default: a new temporary directory).")
@click.option("--keep", is_flag=True, help="Keep the rendered repo afterwards (always kept with --dest).")
@click.option("--vcs-ref", default="HEAD", show_default=True, help="Template git ref to render; HEAD includes uncommitted changes.")
@click.option("--trust", is_flag=True, help="Allow the template's tasks and extensions to run.")
@click.option("--skip", "skip", multiple=True, metavar="GATE", help=f"Skip a gate (repeatable, or comma-separated); the template is then not certified. Gates: {', '.join(certify.GATES)}.")
@click.option("--smoke-timeout", default=180.0, show_default=True, help="Seconds to wait for the running repo to pass smoke.")
@click.option("--report", "report_path", type=click.Path(path_type=Path, dir_okay=False), help="Also write the conformance report as JSON to this file.")
@output_option
@click.pass_context
def template_certify(ctx: click.Context, path: Path, dest: Path | None, keep: bool, vcs_ref: str, trust: bool, skip: tuple[str, ...], smoke_timeout: float, report_path: Path | None) -> None:
    """Certify the template at PATH: render it with conformance.answers, run every gate on the result.

    The gates are the golden pipeline's (deploy excluded), any other verb in
    conformance.verbs, then `rlp run`, `rlp smoke` and `rlp down`. Exits 0 only
    when the template is certified.
    """
    json_mode = ctx.obj["output"] == "json"
    opts = certify.Options(
        template=path,
        dest=dest,
        vcs_ref=vcs_ref or None,
        trust=trust,
        skip=split_components(skip),
        smoke_timeout=smoke_timeout,
        stream=sys.stderr if json_mode else None,
    )
    try:
        report = certify.certify(opts)
    except (certify.CertifyError, contract.ContractError) as err:
        fail(ctx, str(err))
    certify.cleanup(report, keep=keep or dest is not None)
    doc = report.as_dict()
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if json_mode:
        emit_json({"ok": report.certified, **doc})
    else:
        for g in report.gates:
            click.echo(f"{g.gate:10} {g.status:8} {g.detail}")
        verdict = "certified" if report.certified else "not certified"
        click.echo(f"{report.template} {report.version or ''}: {verdict}".replace("  ", " "))
    ctx.exit(0 if report.certified else 1)


@template_group.command(name="summary")
@click.argument("report_path", type=click.Path(path_type=Path, dir_okay=False, exists=True))
@click.pass_context
def template_summary(ctx: click.Context, report_path: Path) -> None:
    """Print a conformance report (from certify --report) as Markdown, for $GITHUB_STEP_SUMMARY."""
    try:
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        fail(ctx, f"cannot read {report_path}: {err}")
    click.echo(certify.summary(doc), nl=False)


def report_render(ctx: click.Context, dest: Path, action: str) -> None:
    try:
        files = contract.find_contracts(dest)
        problems = [p for f in files for p in contract.validate_file(f)]
    except contract.ContractError as err:
        fail(ctx, f"rendered repo has no valid contract: {err}", code=1)
    if ctx.obj["output"] == "json":
        emit_json({"ok": not problems, "action": action, "path": dest.as_posix(), "problems": [p.as_dict() for p in problems]})
    else:
        for p in problems:
            click.echo(str(p))
        click.echo(f"{action}: {dest.as_posix()} {'is valid' if not problems else 'has contract problems'}")
    ctx.exit(1 if problems else 0)


# --- platform: config, project, repo, lifecycle ---


def platform_config(ctx: click.Context) -> platform_api.Config:
    """The platform settings; a config problem exits 2 (fail raises click's Exit)."""
    try:
        config = platform_api.load_config()
    except platform_api.ConfigError as err:
        fail(ctx, str(err))
    return config


def platform_call(ctx: click.Context, call):
    """Run `call(client)` against the configured platform; exit 2 on a setup problem, 1 when the platform says no."""
    config = platform_config(ctx)
    try:
        with platform_api.PlatformClient.from_config(config, transport=ctx.obj.get("platform_transport")) as client:
            return call(client)
    except platform_api.ConfigError as err:
        fail(ctx, str(err))
    except platform_api.TransitionRejected:
        raise
    except platform_api.PlatformError as err:
        # Not found, conflicts and validation errors are the answer; no platform or a bad token is setup.
        fail(ctx, str(err), code=1 if err.status is not None and err.status != 401 else 2)


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(v)) for v in col) for col in zip(headers, *rows, strict=False)]
    for row in [headers, *rows]:
        click.echo("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=False)).rstrip())


def print_fields(doc: dict, keys: list[str]) -> None:
    width = max(len(k) for k in keys)
    for k in keys:
        value = doc.get(k)
        click.echo(f"{k.ljust(width)}  {'-' if value in (None, '') else value}")


PROJECT_FIELDS = ["name", "phase", "owner", "description", "dashboard_url", "created_at", "updated_at"]
REPO_FIELDS = ["full_name", "project", "default_branch", "template_source", "template_version", "last_indexed_commit", "index_error", "created_at", "updated_at"]


@cli.group(name="config")
def config_group() -> None:
    """Where this CLI finds the rlp platform."""


@config_group.command(name="show")
@output_option
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the platform URL and (masked) token, and where each came from."""
    config = platform_config(ctx)
    doc = {
        "path": config.path.as_posix(),
        "platform": {
            "url": {"value": config.url, "source": config.sources["url"]},
            "token": {"value": platform_api.mask(config.token), "source": config.sources["token"]},
        },
    }
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, **doc})
        return
    click.echo(f"config file     {doc['path']}")
    for key in ("url", "token"):
        entry = doc["platform"][key]
        click.echo(f"platform.{key.ljust(6)}  {entry['value'] or '-'}  ({entry['source']})")


@cli.group(name="project")
def project_group() -> None:
    """Projects in the platform's record."""


@project_group.command(name="list")
@click.option("--phase", default=None, help="Only projects in this phase, e.g. in-dev.")
@output_option
@click.pass_context
def project_list(ctx: click.Context, phase: str | None) -> None:
    """List projects, by name."""
    rows = platform_call(ctx, lambda c: c.list_projects(phase))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "projects": rows})
        return
    print_table([[p["name"], p["phase"], p["owner"] or "-"] for p in rows], ["NAME", "PHASE", "OWNER"])


@project_group.command(name="show")
@click.argument("name")
@output_option
@click.pass_context
def project_show(ctx: click.Context, name: str) -> None:
    """Show a project and its repos."""
    project, project_repos = platform_call(ctx, lambda c: (c.get_project(name), c.list_repos(name)))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "project": project, "repos": project_repos})
        return
    print_fields({**project, "repos": ", ".join(r["full_name"] for r in project_repos)}, [*PROJECT_FIELDS, "repos"])


def emit_project(ctx: click.Context, project: dict, action: str) -> None:
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "project": project})
    else:
        click.echo(f"{action} project {project['name']}")
        print_fields(project, PROJECT_FIELDS)


@project_group.command(name="create")
@click.argument("name")
@click.option("--owner", default="", help="Who owns it; live needs one.")
@click.option("--description", default="", help="What it is; designed needs one.")
@click.option("--dashboard-url", default=None, help="Its dashboard; live needs one.")
@output_option
@click.pass_context
def project_create(ctx: click.Context, name: str, owner: str, description: str, dashboard_url: str | None) -> None:
    """Create a project. It starts in the proposed phase."""
    project = platform_call(ctx, lambda c: c.create_project(name, owner=owner, description=description, dashboard_url=dashboard_url))
    emit_project(ctx, project, "created")


@project_group.command(name="update")
@click.argument("name")
@click.option("--owner", default=None, help="New owner.")
@click.option("--description", default=None, help="New description.")
@click.option("--dashboard-url", default=None, help="New dashboard URL; an empty value clears it.")
@output_option
@click.pass_context
def project_update(ctx: click.Context, name: str, owner: str | None, description: str | None, dashboard_url: str | None) -> None:
    """Change a project's owner, description or dashboard URL. Use rlp lifecycle move for its phase."""
    fields: dict[str, str | None] = {k: v for k, v in (("owner", owner), ("description", description)) if v is not None}
    if dashboard_url is not None:
        fields["dashboard_url"] = dashboard_url or None
    if not fields:
        fail(ctx, "nothing to change: pass --owner, --description or --dashboard-url")
    project = platform_call(ctx, lambda c: c.update_project(name, **fields))
    emit_project(ctx, project, "updated")


@cli.group(name="repo")
def repo_group() -> None:
    """Repos in the platform's record."""


@repo_group.command(name="list")
@click.option("--project", default=None, help="Only this project's repos.")
@output_option
@click.pass_context
def repo_list(ctx: click.Context, project: str | None) -> None:
    """List repos, by full name."""
    rows = platform_call(ctx, lambda c: c.list_repos(project))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "repos": rows})
        return
    print_table([[r["full_name"], r["project"], r["default_branch"]] for r in rows], ["REPO", "PROJECT", "BRANCH"])


@repo_group.command(name="show")
@click.argument("full_name", metavar="OWNER/NAME")
@output_option
@click.pass_context
def repo_show(ctx: click.Context, full_name: str) -> None:
    """Show a repo and its components."""
    repo, repo_components = platform_call(ctx, lambda c: (c.get_repo(full_name), c.list_repo_components(full_name)))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "repo": repo, "components": repo_components})
        return
    print_fields(repo, REPO_FIELDS)
    if repo_components:
        click.echo("")
        print_table([[c["name"], c["language"], c["artifact_kind"], c["runtime_kind"]] for c in repo_components], ["COMPONENT", "LANGUAGE", "ARTIFACT", "RUNTIME"])


@repo_group.command(name="sync")
@click.argument("full_name", metavar="[OWNER/NAME]", required=False)
@output_option
@click.pass_context
def repo_sync(ctx: click.Context, full_name: str | None) -> None:
    """Re-index a repo, or every repo the platform can see, from GitHub. Exits 1 if any repo wasn't indexed."""
    doc = platform_call(ctx, lambda c: c.sync(full_name))
    if ctx.obj["output"] == "json":
        emit_json(doc)
    else:
        for r in doc["results"]:
            line = f"{r['repo']}: {r['status']}"
            if r["status"] == "indexed":
                line += f" at {r['commit']} (project {r['project']}; components {', '.join(r['components']) or '-'})"
                if r["removed"]:
                    line += f"; removed {', '.join(r['removed'])}"
            click.echo(line)
            if r["error"]:
                for error_line in r["error"].splitlines():
                    click.echo(f"  {error_line}")
        if not doc["results"]:
            click.echo("no repos to sync")
    ctx.exit(0 if doc["ok"] else 1)


@cli.group(name="lifecycle")
def lifecycle_group() -> None:
    """A project's lifecycle phase: show it and its history, or move it."""


@lifecycle_group.command(name="show")
@click.argument("project")
@output_option
@click.pass_context
def lifecycle_show(ctx: click.Context, project: str) -> None:
    """Print a project's phase and every move it has made, oldest first."""
    record, history = platform_call(ctx, lambda c: (c.get_project(project), c.list_transitions(project)))
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "project": record["name"], "phase": record["phase"], "history": history})
        return
    click.echo(f"{record['name']}: {record['phase']}")
    if history:
        click.echo("")
        print_table([[t["at"], t["from_phase"] or "-", t["to_phase"], t["actor"], t["reason"] or "-"] for t in history], ["AT", "FROM", "TO", "BY", "REASON"])


def default_actor() -> str:
    """Who the move is recorded as: the local login name."""
    try:
        return getpass.getuser() or "rlp-cli"
    except (ImportError, KeyError, OSError):  # no login name in some containers
        return "rlp-cli"


@lifecycle_group.command(name="move")
@click.argument("project")
@click.argument("phase")
@click.option("--reason", required=True, help="Why; recorded with the move.")
@output_option
@click.pass_context
def lifecycle_move(ctx: click.Context, project: str, phase: str, reason: str) -> None:
    """Move PROJECT to PHASE if every entry rule passes. A rejected move exits 1 and lists the failing rules."""
    actor = default_actor()
    try:
        moved = platform_call(ctx, lambda c: c.move(project, phase, reason=reason, actor=actor))
    except platform_api.TransitionRejected as err:
        if ctx.obj["output"] == "json":
            emit_json({"ok": False, "error": str(err), **err.rejection})
        else:
            click.echo(f"{project}: {err.rejection['from_phase']} -> {phase} rejected", err=True)
            for rule in err.failures:
                for message in rule["messages"] or [f"[{rule['rule']}] {rule['description']}"]:
                    click.echo(f"  {message}", err=True)
        ctx.exit(1)
    if ctx.obj["output"] == "json":
        emit_json({"ok": True, "project": project, "transition": moved})
    else:
        click.echo(f"{project}: {moved['from_phase'] or '-'} -> {moved['to_phase']}")


def main() -> None:
    cli(obj={})

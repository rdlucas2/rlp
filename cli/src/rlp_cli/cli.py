"""The rlp command line.

Every command accepts --output json for agents and scripts. In JSON mode,
stdout carries exactly one JSON document and everything else goes to stderr.
Exit codes: 0 success, 1 the check or verb failed, 2 usage or setup error.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import sys
from pathlib import Path

import click

from rlp_cli import __version__, artifacts, certify, contract, coverage, infra, onboard, pipeline, policy, publish, reports, runtime, scaffold, secrets, verbs

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
@output_option
@click.pass_context
def policy_check(ctx: click.Context, path: Path, namespaces: tuple[str, ...]) -> None:
    """Check the repo at PATH against the policy bundle with conftest."""
    try:
        violations = policy.check(path, namespaces)
    except policy.PolicyError as err:
        fail(ctx, str(err))
    if ctx.obj["output"] == "json":
        emit_json({"ok": not violations, "violations": [v.as_dict() for v in violations]})
    elif violations:
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


def main() -> None:
    cli(obj={})

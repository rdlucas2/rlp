"""The rlp command line.

Every command accepts --output json for agents and scripts. In JSON mode,
stdout carries exactly one JSON document and everything else goes to stderr.
Exit codes: 0 success, 1 the check or verb failed, 2 usage or setup error.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import sys
from pathlib import Path

import click

from rlp_cli import __version__, artifacts, contract, coverage, pipeline, policy, publish, reports, scaffold, secrets, verbs

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


def make_verb(name: str) -> click.Command:
    @click.command(name=name, help=f"Run the {name} verb for every component (verbs.{name}, else `task {name}`).")
    @click.option("--component", "-c", "components", multiple=True, help="Only this component. Repeatable.")
    @click.option("--path", "root", type=click.Path(path_type=Path, file_okay=False), default=".", help="Repo root (default: current directory).")
    @output_option
    @click.pass_context
    def command(ctx: click.Context, components: tuple[str, ...], root: Path) -> None:
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

    return command


for _verb in verbs.VERBS:
    cli.add_command(make_verb(_verb))


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
@click.option("--deploy-local", is_flag=True, help="Deploy to LocalStack instead of AWS (for act and laptops).")
@click.option("--reset", is_flag=True, help="Discard earlier results in .rlp/pipeline/results.json first.")
@output_option
@click.pass_context
def pipeline_run(ctx: click.Context, root: Path, stages: tuple[str, ...], components: tuple[str, ...], deploy_env: str, deploy_local: bool, reset: bool) -> None:
    """Run the gates in order and stop at the first failure."""
    json_mode = ctx.obj["output"] == "json"
    run_ctx = pipeline.Context(
        root=root,
        components=split_components(components),
        deploy_env=deploy_env,
        deploy_local=deploy_local,
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

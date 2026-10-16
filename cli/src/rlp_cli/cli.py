"""The rlp command line.

Every command accepts --output json for agents and scripts. In JSON mode,
stdout carries exactly one JSON document and everything else goes to stderr.
Exit codes: 0 success, 1 the check or verb failed, 2 usage or setup error.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import click

from rlp_cli import __version__, contract, policy, reports, scaffold, verbs

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

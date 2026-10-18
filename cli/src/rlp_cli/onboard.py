"""`rlp onboard OWNER/REPO`: let a repo deploy through the golden pipeline.

Run on the operator's machine, next to the bootstrap stack's local state, with
the operator's own AWS credentials and `gh` login. It:

1. reads the bootstrap's outputs with `terraform output -json`;
2. when the repo isn't in `github_repositories` yet, runs `terraform apply`
   in the bootstrap directory with every bootstrap variable passed explicitly
   in a var file (the current values, plus the repo appended to the list), so
   nothing else in the stack changes;
3. sets the repo's RLP_* variables with `gh variable set`;
4. when INFRACOST_API_KEY is in the environment and the repo has no such
   secret yet, sets it with `gh secret set`, passing the value on stdin.

No CI job holds the AWS IAM-admin or cross-repo GitHub credentials this needs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rlp_cli import infra

# Same rule as the bootstrap's github_repositories validation.
REPO_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
OUTPUTS = ("state_bucket", "region", "github_repositories", "deploy_branch", "plan_role_arn", "deploy_role_arn")
SECRET = "INFRACOST_API_KEY"


class OnboardError(Exception):
    """A setup problem: a missing tool, no login, no bootstrap state, no access to the repo."""


class StepError(Exception):
    """terraform apply or a gh write failed partway."""


@dataclass
class Plan:
    repo: str
    env: str
    trusted: list[str]  # github_repositories now
    add_trust: bool
    variables: dict[str, str]  # name -> wanted value
    current: dict[str, str]  # name -> value the repo has now
    secret: str  # "set", "exists" or "skip" (no INFRACOST_API_KEY in the environment)
    tf_vars: dict = field(default_factory=dict)

    @property
    def changed_variables(self) -> dict[str, str]:
        return {k: v for k, v in self.variables.items() if self.current.get(k) != v}

    @property
    def onboarded(self) -> bool:
        return not self.add_trust and not self.changed_variables and self.secret != "set"

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "env": self.env,
            "trust": {"add": self.add_trust, "github_repositories": self.tf_vars.get("github_repositories", self.trusted)},
            "variables": {k: {"value": v, "change": k in self.changed_variables} for k, v in self.variables.items()},
            "secret": {"name": SECRET, "action": self.secret},
            "already_onboarded": self.onboarded,
        }


def check_repo(repo: str) -> None:
    if not REPO_NAME.match(repo or ""):
        raise OnboardError(f"{repo!r} is not OWNER/REPO, for example rdlucas2/hello-api")


def which(tool: str, why: str) -> str:
    exe = shutil.which(tool)
    if exe is None:
        raise OnboardError(f"{tool} is not installed or not on PATH ({why})")
    return exe


def run(cmd: list[str], cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, encoding="utf-8", check=False)


def last_line(text: str) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --- reading the current state ---


def bootstrap_outputs(terraform: str, bootstrap: Path) -> dict:
    if not bootstrap.is_dir():
        raise OnboardError(f"bootstrap directory {bootstrap} does not exist (pass --bootstrap-dir)")
    proc = run([terraform, "output", "-no-color", "-json"], cwd=bootstrap)
    if proc.returncode != 0:
        raise OnboardError(f"terraform output failed in {bootstrap}: {last_line(proc.stderr) or proc.returncode}")
    try:
        raw = json.loads(proc.stdout or "{}")
    except ValueError as err:
        raise OnboardError(f"terraform output -json in {bootstrap} did not print JSON: {err}") from None
    values = {name: out.get("value") for name, out in (raw or {}).items() if isinstance(out, dict)}
    if not values:
        raise OnboardError(f"the bootstrap in {bootstrap} has no state: apply it first (docs/aws.md, 'Set up an account')")
    missing = [name for name in OUTPUTS if name not in values]
    if missing:
        raise OnboardError(
            f"the bootstrap state has no {', '.join(missing)} output: run `terraform apply` once in {bootstrap} "
            "with this rlp's infra/bootstrap to record them"
        )
    if not isinstance(values["github_repositories"], list):
        raise OnboardError("the bootstrap's github_repositories output is not a list")
    return values


def gh_preflight(gh: str, repo: str) -> None:
    proc = run([gh, "auth", "status"])
    if proc.returncode != 0:
        raise OnboardError("gh is not logged in: run `gh auth login`")
    proc = run([gh, "api", f"repos/{repo}"])
    if proc.returncode != 0:
        raise OnboardError(f"repository {repo} does not exist or this gh login cannot see it: {last_line(proc.stderr)}")
    try:
        perms = json.loads(proc.stdout).get("permissions") or {}
    except (ValueError, AttributeError):
        perms = {}
    # Repository variables and secrets need admin on the repo (the owner, for a personal repo).
    if not perms.get("admin"):
        raise OnboardError(f"this gh login cannot write {repo}'s variables and secrets: it needs admin access to the repo")


def gh_json(gh: str, *args: str) -> list:
    proc = run([gh, *args])
    if proc.returncode != 0:
        raise OnboardError(f"gh {' '.join(args[:2])} failed: {last_line(proc.stderr) or proc.returncode}")
    try:
        doc = json.loads(proc.stdout or "[]")
    except ValueError as err:
        raise OnboardError(f"gh {' '.join(args[:2])} did not print JSON: {err}") from None
    return doc if isinstance(doc, list) else []


def wanted_variables(outs: dict, env: str) -> dict[str, str]:
    return {
        "RLP_AWS_REGION": str(outs["region"]),
        "RLP_AWS_PLAN_ROLE_ARN": str(outs["plan_role_arn"]),
        "RLP_AWS_DEPLOY_ROLE_ARN": str(outs["deploy_role_arn"]),
        "RLP_TF_STATE_BUCKET": str(outs["state_bucket"]),
        "RLP_DEPLOY_ENV": env,
    }


def prepare(repo: str, env: str, bootstrap: Path, environ: dict | None = None) -> tuple[Plan, str, str]:
    """Check the tools, the login and the repo, read the current state, and work out what to change."""
    environ = os.environ if environ is None else environ
    check_repo(repo)
    infra.check_env(env)
    terraform = which("terraform", "rlp onboard reads and applies the bootstrap stack")
    gh = which("gh", "rlp onboard sets the repo's variables with the GitHub CLI")
    outs = bootstrap_outputs(terraform, bootstrap)
    gh_preflight(gh, repo)
    current = {v.get("name"): v.get("value") for v in gh_json(gh, "variable", "list", "--repo", repo, "--json", "name,value") if isinstance(v, dict)}
    if environ.get(SECRET):
        secrets_now = {s.get("name") for s in gh_json(gh, "secret", "list", "--repo", repo, "--json", "name") if isinstance(s, dict)}
        secret = "exists" if SECRET in secrets_now else "set"
    else:
        secret = "skip"
    trusted = [str(r) for r in outs["github_repositories"]]
    add = repo.lower() not in {r.lower() for r in trusted}
    plan = Plan(
        repo=repo,
        env=env,
        trusted=trusted,
        add_trust=add,
        variables=wanted_variables(outs, env),
        current=current,
        secret=secret,
        tf_vars={
            "region": outs["region"],
            "state_bucket": outs["state_bucket"],
            "github_repositories": [*trusted, repo] if add else trusted,
            "deploy_branch": outs["deploy_branch"],
        },
    )
    return plan, terraform, gh


# --- changing it ---


def apply_trust(terraform: str, bootstrap: Path, plan: Plan, auto_approve: bool, stream=None) -> None:
    """terraform apply in the bootstrap directory, every variable passed explicitly in a var file."""
    handle, name = tempfile.mkstemp(prefix="rlp-onboard-", suffix=".tfvars.json")
    var_file = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(plan.tf_vars, f, indent=2)
        args = [terraform, "apply", "-no-color", f"-var-file={var_file}"]
        if auto_approve:
            args += ["-input=false", "-auto-approve"]
        # Not captured: without --yes, terraform shows the plan and asks for approval on the terminal.
        proc = subprocess.run(args, cwd=bootstrap, stdout=stream, stderr=stream, check=False)
        if proc.returncode != 0:
            raise StepError(f"terraform apply failed in {bootstrap} (exit {proc.returncode}); nothing was set on {plan.repo}")
    finally:
        var_file.unlink(missing_ok=True)


def set_variables(gh: str, plan: Plan) -> list[str]:
    done = []
    for name, value in plan.changed_variables.items():
        proc = run([gh, "variable", "set", name, "--repo", plan.repo, "--body", value])
        if proc.returncode != 0:
            raise StepError(f"gh variable set {name} failed: {last_line(proc.stderr) or proc.returncode}")
        done.append(name)
    return done


def set_secret(gh: str, plan: Plan, value: str) -> None:
    # The value goes on stdin only: never in argv, where other processes and logs can see it.
    proc = run([gh, "secret", "set", SECRET, "--repo", plan.repo], stdin=value)
    if proc.returncode != 0:
        raise StepError(f"gh secret set {SECRET} failed: {last_line(proc.stderr) or proc.returncode}")

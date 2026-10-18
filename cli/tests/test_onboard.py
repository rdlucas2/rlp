"""`rlp onboard`: driven against fake `terraform` and `gh` executables on PATH.

The fakes are one small Python script behind a per-OS launcher. They answer
from a JSON state file (bootstrap outputs, repos, variables, secrets), update
it on writes the way the real tools would, and log every call (argv, stdin,
and the var file terraform apply was given). No real AWS or GitHub calls.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = "rdlucas2/x"
PLAN_ARN = "arn:aws:iam::123456789012:role/rlp-plan"
DEPLOY_ARN = "arn:aws:iam::123456789012:role/rlp-deploy"
KEY = "dummy-value"

FAKE = r'''
import json, os, sys

tool = sys.argv[1]
args = sys.argv[2:]
state_path = os.environ["FAKE_STATE"]
with open(state_path, encoding="utf-8") as f:
    state = json.load(f)
call = {"tool": tool, "args": args}

def save():
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

code = 0
if tool == "terraform":
    if args[:1] == ["output"]:
        print(json.dumps({k: {"value": v} for k, v in state["outputs"].items()}))
    elif args[:1] == ["apply"]:
        var_file = next(a.split("=", 1)[1] for a in args if a.startswith("-var-file="))
        with open(var_file, encoding="utf-8") as f:
            call["vars"] = json.load(f)
        code = state.get("apply_exit", 0)
        if code == 0:
            state["outputs"]["github_repositories"] = call["vars"]["github_repositories"]
else:
    repo = opt("--repo")
    if args[:2] == ["auth", "status"]:
        code = state.get("auth_exit", 0)
    elif args[:1] == ["api"]:
        name = args[1].removeprefix("repos/")
        if name in state["repos"]:
            print(json.dumps({"full_name": name, "permissions": state["repos"][name]}))
        else:
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            code = 1
    elif args[:2] == ["variable", "list"]:
        print(json.dumps([{"name": k, "value": v} for k, v in state["variables"].get(repo, {}).items()]))
    elif args[:2] == ["variable", "set"]:
        state["variables"].setdefault(repo, {})[args[2]] = opt("--body")
    elif args[:2] == ["secret", "list"]:
        print(json.dumps([{"name": k} for k in state["secrets"].get(repo, {})]))
    elif args[:2] == ["secret", "set"]:
        call["stdin"] = sys.stdin.read()
        state["secrets"].setdefault(repo, {})[args[2]] = call["stdin"]
state.setdefault("calls", []).append(call)
save()
sys.exit(code)
'''


def outputs(repos: list[str]) -> dict:
    return {
        "state_bucket": "rlp-tfstate-123456789012",
        "region": "us-east-1",
        "github_repositories": repos,
        "deploy_branch": "main",
        "plan_role_arn": PLAN_ARN,
        "deploy_role_arn": DEPLOY_ARN,
    }


EXPECTED_VARS = {
    "RLP_AWS_REGION": "us-east-1",
    "RLP_AWS_PLAN_ROLE_ARN": PLAN_ARN,
    "RLP_AWS_DEPLOY_ROLE_ARN": DEPLOY_ARN,
    "RLP_TF_STATE_BUCKET": "rlp-tfstate-123456789012",
    "RLP_DEPLOY_ENV": "dev",
}


class Fakes:
    def __init__(self, root: Path, tools: tuple[str, ...] = ("terraform", "gh")):
        self.bin = root / "fakebin"
        self.bin.mkdir()
        self.bootstrap = root / "bootstrap"
        self.bootstrap.mkdir()
        self.state_path = root / "state.json"
        script = root / "fake_tool.py"
        script.write_text(FAKE, encoding="utf-8")
        for tool in tools:
            if os.name == "nt":
                (self.bin / f"{tool}.cmd").write_text(f'@"{sys.executable}" "{script}" {tool} %*\r\n', encoding="utf-8")
            else:
                launcher = self.bin / tool
                launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {tool} "$@"\n', encoding="utf-8")
                launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.write(
            {
                "outputs": outputs(["rdlucas2/hello-api"]),
                "repos": {REPO: {"admin": True, "push": True, "pull": True}},
                "variables": {},
                "secrets": {},
            }
        )

    def write(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    @property
    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def update(self, **changes) -> None:
        self.write({**self.state, **changes})

    def calls(self, tool: str | None = None, *prefix: str) -> list[dict]:
        return [c for c in self.state.get("calls", []) if (tool is None or c["tool"] == tool) and c["args"][: len(prefix)] == list(prefix)]

    def env(self, **extra: str) -> dict:
        # Only the fakes on PATH, so a real terraform or gh can never answer.
        env = {"PATH": str(self.bin), "FAKE_STATE": str(self.state_path), **extra}
        if SECRET_ENV not in extra:
            env[SECRET_ENV] = ""
        return env


SECRET_ENV = "INFRACOST_API_KEY"


@pytest.fixture
def fakes(tmp_path) -> Fakes:
    return Fakes(tmp_path)


def onboard(rlp, fakes: Fakes, *args: str, **env: str):
    return rlp("onboard", REPO, "--bootstrap-dir", str(fakes.bootstrap), *args, env=fakes.env(**env))


def writes(fakes: Fakes) -> list[dict]:
    return fakes.calls("terraform", "apply") + fakes.calls("gh", "variable", "set") + fakes.calls("gh", "secret", "set")


# --- acceptance ---


def test_dry_run_shows_the_trust_change_and_every_variable_and_changes_nothing(rlp, fakes):
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 0, r.stderr
    assert '["rdlucas2/hello-api"] -> ["rdlucas2/hello-api", "rdlucas2/x"]' in r.stdout
    for name, value in EXPECTED_VARS.items():
        assert f"variable {name}={value} (would set)" in r.stdout
    assert writes(fakes) == []


def test_dry_run_json(rlp, fakes):
    r = onboard(rlp, fakes, "--dry-run", "--output", "json")
    assert r.code == 0, r.stderr
    doc = r.json
    assert doc["ok"] is True and doc["dry_run"] is True and doc["applied"] is False
    assert doc["trust"] == {"add": True, "github_repositories": ["rdlucas2/hello-api", REPO]}
    assert {k: v["value"] for k, v in doc["variables"].items()} == EXPECTED_VARS
    assert all(v["change"] for v in doc["variables"].values())
    assert doc["secret"] == {"name": SECRET_ENV, "action": "skip"}
    assert writes(fakes) == []


def test_onboards_with_one_apply_that_changes_only_the_trust_list(rlp, fakes):
    r = onboard(rlp, fakes, "--yes")
    assert r.code == 0, r.stderr
    applies = fakes.calls("terraform", "apply")
    assert len(applies) == 1
    assert "-auto-approve" in applies[0]["args"]
    assert applies[0]["vars"] == {
        "region": "us-east-1",
        "state_bucket": "rlp-tfstate-123456789012",
        "github_repositories": ["rdlucas2/hello-api", REPO],
        "deploy_branch": "main",
    }
    sets = fakes.calls("gh", "variable", "set")
    assert {c["args"][2]: c["args"][c["args"].index("--body") + 1] for c in sets} == EXPECTED_VARS
    assert all(c["args"][c["args"].index("--repo") + 1] == REPO for c in sets)
    assert fakes.calls("gh", "secret") == []  # no INFRACOST_API_KEY in the environment
    assert f"onboarded {REPO} (dev)" in r.stdout


def test_without_yes_terraform_asks_for_approval(rlp, fakes):
    r = onboard(rlp, fakes)
    assert r.code == 0, r.stderr
    (apply,) = fakes.calls("terraform", "apply")
    assert "-auto-approve" not in apply["args"]


def test_the_infracost_key_goes_through_stdin_only(rlp, fakes):
    r = onboard(rlp, fakes, "--yes", INFRACOST_API_KEY=KEY)
    assert r.code == 0, r.stderr
    (secret,) = fakes.calls("gh", "secret", "set")
    assert secret["args"] == ["secret", "set", SECRET_ENV, "--repo", REPO]
    assert secret["stdin"] == KEY
    assert all(KEY not in " ".join(c["args"]) for c in fakes.calls())
    assert KEY not in r.stdout and KEY not in r.stderr


def test_a_second_run_changes_nothing(rlp, fakes):
    assert onboard(rlp, fakes, "--yes", INFRACOST_API_KEY=KEY).code == 0
    before = len(writes(fakes))
    r = onboard(rlp, fakes, "--yes", INFRACOST_API_KEY=KEY)
    assert r.code == 0, r.stderr
    assert "already onboarded" in r.stdout
    assert len(writes(fakes)) == before
    assert len(fakes.calls("terraform", "apply")) == 1


def test_a_trusted_repo_only_gets_its_missing_variables(rlp, fakes):
    fakes.update(outputs=outputs(["rdlucas2/hello-api", REPO]), variables={REPO: {**EXPECTED_VARS, "RLP_DEPLOY_ENV": "staging"}})
    r = onboard(rlp, fakes, "--output", "json")  # no --yes needed: nothing to apply
    assert r.code == 0, r.stderr
    assert fakes.calls("terraform", "apply") == []
    assert [c["args"][2] for c in fakes.calls("gh", "variable", "set")] == ["RLP_DEPLOY_ENV"]
    assert r.json["applied"] is False and r.json["set"] == ["RLP_DEPLOY_ENV"]


def test_env_option_sets_the_deploy_env(rlp, fakes):
    r = onboard(rlp, fakes, "--yes", "--env", "prod")
    assert r.code == 0, r.stderr
    sets = {c["args"][2]: c["args"][-1] for c in fakes.calls("gh", "variable", "set")}
    assert sets["RLP_DEPLOY_ENV"] == "prod"


def test_an_existing_secret_is_kept(rlp, fakes):
    fakes.update(secrets={REPO: {SECRET_ENV: "old"}})
    r = onboard(rlp, fakes, "--yes", INFRACOST_API_KEY=KEY)
    assert r.code == 0, r.stderr
    assert fakes.calls("gh", "secret", "set") == []
    assert "already set; kept" in r.stdout


# --- failures ---


def test_json_needs_yes_when_the_bootstrap_must_be_applied(rlp, fakes):
    r = onboard(rlp, fakes, "--output", "json")
    assert r.code == 2
    assert "--yes" in r.json["error"]
    assert writes(fakes) == []


def test_a_failed_apply_sets_nothing(rlp, fakes):
    fakes.update(apply_exit=1)
    r = onboard(rlp, fakes, "--yes")
    assert r.code == 1
    assert "terraform apply failed" in r.stderr
    assert fakes.calls("gh", "variable", "set") == []


@pytest.mark.parametrize("missing", ["terraform", "gh"])
def test_a_missing_tool_is_a_setup_error(rlp, tmp_path, missing):
    present = tuple(t for t in ("terraform", "gh") if t != missing)
    fakes = Fakes(tmp_path, tools=present)
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 2
    assert f"{missing} is not installed" in r.stderr


def test_gh_not_logged_in(rlp, fakes):
    fakes.update(auth_exit=1)
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 2
    assert "gh auth login" in r.stderr


def test_bootstrap_without_state(rlp, fakes):
    fakes.update(outputs={})
    r = onboard(rlp, fakes, "--dry-run", "--output", "json")
    assert r.code == 2
    assert "has no state" in r.json["error"]


def test_bootstrap_state_from_before_the_new_outputs(rlp, fakes):
    old = outputs(["rdlucas2/hello-api"])
    del old["github_repositories"], old["deploy_branch"]
    fakes.update(outputs=old)
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 2
    assert "github_repositories, deploy_branch" in r.stderr


def test_repo_that_does_not_exist(rlp, fakes):
    fakes.update(repos={})
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 2
    assert "does not exist" in r.stderr


def test_repo_without_admin_access(rlp, fakes):
    fakes.update(repos={REPO: {"admin": False, "push": True, "pull": True}})
    r = onboard(rlp, fakes, "--dry-run")
    assert r.code == 2
    assert "admin access" in r.stderr


def test_bad_repo_name(rlp, fakes):
    r = rlp("onboard", "not-a-repo", "--bootstrap-dir", str(fakes.bootstrap), env=fakes.env())
    assert r.code == 2
    assert "OWNER/REPO" in r.stderr
    assert fakes.calls() == []

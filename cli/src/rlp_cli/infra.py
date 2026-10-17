"""AWS deploys: Terraform rendered from rlp.yaml, run with the terraform CLI.

`rlp infra render` writes one root stack per environment to
.rlp/infra/<env>/main.tf.json (Terraform's JSON syntax, so no templating).
The stack calls rlp's versioned modules from infra/modules/, one module block
per deployable component, and keeps its state in the S3 bucket the bootstrap
stack created, under rlp/apps/<project>/<repo>/<env>.tfstate.

The golden pipeline's terraform stage renders, plans and checks the plan JSON
with the policy bundle (rlp.terraform); its deploy stage applies that saved
plan and checks each component's readiness path through the load balancer.
`rlp infra destroy` tears an environment's stack down again.

Only container-service components with a container-image artifact deploy in
S0. The S0 service mapping is postgres as "bring your own": the module creates
an empty secret for the connection string, and an operator fills it.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from rlp_cli import __version__, artifacts

MODULES_REPO = "git::https://github.com/rdlucas2/rlp.git//infra/modules"
TERRAFORM_VERSION = ">= 1.10"  # native S3 lock files
AWS_PROVIDER = "~> 6.0"
DEFAULT_REGION = "us-east-1"
ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
# Service kinds the S0 AWS mapping knows. The rest arrive with later runtime issues.
AWS_SERVICES = {"postgres"}
PLAN_FILE = "plan.tfplan"
STACK_FILE = "main.tf.json"


class InfraError(Exception):
    """The stack cannot be rendered, or terraform failed."""


@dataclass
class Settings:
    """Where a stack's image, modules and state live. Each falls back to an environment variable."""

    registry: str = ""
    state_bucket: str = ""
    region: str = ""
    modules: str = ""

    @classmethod
    def from_env(cls, **given: str | None) -> Settings:
        env = os.environ
        return cls(
            registry=given.get("registry") or env.get("RLP_REGISTRY", ""),
            state_bucket=given.get("state_bucket") or env.get("RLP_TF_STATE_BUCKET", ""),
            region=given.get("region") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or DEFAULT_REGION,
            modules=given.get("modules") or env.get("RLP_MODULES_SOURCE", ""),
        )


@dataclass
class Stack:
    env: str
    dir: Path
    components: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # component -> why it does not deploy
    backend: bool = False
    config: dict = field(default_factory=dict)

    @property
    def plan_file(self) -> Path:
        return self.dir / PLAN_FILE

    @property
    def plan_json(self) -> Path:
        # Its own directory: the policy check runs conftest there and must see only the plan.
        return self.dir / "policy" / "plan.json"


def stack_dir(root: Path, env: str) -> Path:
    return root / ".rlp" / "infra" / env


def check_env(env: str) -> None:
    if not ENV_NAME.match(env or ""):
        raise InfraError(f"environment {env!r} must be 1-16 lowercase letters, digits or hyphens, starting with a letter")


def deployable(doc: dict, only: tuple[str, ...] = ()) -> tuple[list[dict], dict[str, str]]:
    """Components that deploy to AWS in S0, and why each other one does not."""
    names = {c["name"] for c in doc.get("components") or []}
    unknown = [n for n in only if n not in names]
    if unknown:
        raise InfraError(f"no component named {', '.join(unknown)} in rlp.yaml")
    chosen, skipped = [], {}
    for c in doc.get("components") or []:
        if only and c["name"] not in only:
            continue
        runtime = (c.get("runtime") or {}).get("kind")
        artifact = (c.get("artifact") or {}).get("kind")
        if runtime == "container-service" and artifact == "container-image":
            chosen.append(c)
        else:
            skipped[c["name"]] = f"runtime {runtime} with artifact {artifact} has no AWS deploy yet (S0 deploys container-service images)"
    return chosen, skipped


def module_source(base: str, module: str, stack: Path) -> str:
    """Source for one module under BASE: a go-getter URL (…//infra/modules?ref=v1) or a local directory."""
    if "::" in base or base.startswith(("github.com/", "git@")):
        path, sep, query = base.partition("?")
        return f"{path.rstrip('/')}/{module}{sep}{query}"
    # Terraform only treats ./ and ../ paths as local, so make it relative to the stack.
    target = Path(base).resolve() / module
    rel = Path(os.path.relpath(target, stack.resolve())).as_posix()
    return rel if rel.startswith("../") else f"./{rel}"


def health_path(c: dict) -> str:
    health = (c.get("runtime") or {}).get("health") or {}
    return health.get("readiness") or health.get("liveness") or "/"


def config_mapping(c: dict) -> dict:
    """rlp.yaml config entries as the container-service module's inputs."""
    services = {s["name"]: s for s in c.get("services") or []}
    for s in services.values():
        if s["kind"] not in AWS_SERVICES:
            raise InfraError(
                f"component {c['name']}: service {s['name']} is {s['kind']}, which has no AWS mapping yet (S0 maps postgres only)"
            )
    mapping: dict = {"config": {}, "unset_config": [], "secrets": [], "service_config": {}, "environment_variables": {}}
    for entry in c.get("config") or []:
        name = entry["name"]
        source = entry.get("from")
        if source:
            service = source.split(":", 1)[1]
            if service not in services:
                raise InfraError(f"component {c['name']}: config {name} is from {source}, which the component does not declare")
            mapping["service_config"][name] = service
        elif entry.get("secret"):
            mapping["secrets"].append(name)
        elif entry.get("default") is not None:
            # SSM rejects empty values, so an empty default is a plain environment variable.
            if entry["default"] == "":
                mapping["environment_variables"][name] = ""
            else:
                mapping["config"][name] = entry["default"]
        else:
            mapping["unset_config"].append(name)
    return mapping


def state_key(doc: dict, env: str) -> str:
    meta = doc["metadata"]
    return f"rlp/apps/{meta['project']}/{meta['name']}/{env}.tfstate"


def render(root: Path, doc: dict, env: str, settings: Settings, only: tuple[str, ...] = (), ver: str | None = None) -> Stack:
    """Write .rlp/infra/<env>/main.tf.json for the deployable components."""
    check_env(env)
    chosen, skipped = deployable(doc, only)
    directory = stack_dir(root, env)
    stack = Stack(env=env, dir=directory, components=[c["name"] for c in chosen], skipped=skipped, backend=bool(settings.state_bucket))
    if not chosen:
        return stack
    ver = ver or artifacts.version(root)
    base = settings.modules or f"{MODULES_REPO}?ref=v{__version__}"
    meta = doc["metadata"]

    terraform: dict = {
        "required_version": TERRAFORM_VERSION,
        "required_providers": {"aws": {"source": "hashicorp/aws", "version": AWS_PROVIDER}},
    }
    if settings.state_bucket:
        terraform["backend"] = {
            "s3": {
                "bucket": settings.state_bucket,
                "key": state_key(doc, env),
                "region": settings.region,
                "use_lockfile": True,
                "encrypt": True,
            }
        }
    tags = {
        "rlp:project": meta["project"],
        "rlp:environment": env,
        "rlp:managed-by": "rlp",
        "rlp:repo": meta["name"],
    }
    modules, outputs = {}, {}
    for c in chosen:
        image = f"{artifacts.image_name(doc, c)}:{ver}"
        if settings.registry:
            image = f"{settings.registry.rstrip('/')}/{image}"
        modules[c["name"]] = {
            "source": module_source(base, "container-service", directory),
            "environment": env,
            "repo": meta["name"],
            "component": c["name"],
            "image": image,
            "port": c["runtime"]["port"],
            "health_path": health_path(c),
            **config_mapping(c),
        }
        outputs[c["name"]] = {"value": f"${{module.{c['name']}}}"}
    stack.config = {
        "//": "Generated by `rlp infra render` from rlp.yaml. Do not edit: it is rewritten on every run.",
        "terraform": terraform,
        "provider": {"aws": {"region": settings.region, "default_tags": {"tags": tags}}},
        "module": modules,
        "output": outputs,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STACK_FILE).write_text(json.dumps(stack.config, indent=2) + "\n", encoding="utf-8")
    return stack


# --- running terraform ---


def terraform_bin() -> str:
    exe = shutil.which("terraform")
    if exe is None:
        raise InfraError("terraform is not installed or not on PATH (rlp needs Terraform >= 1.10)")
    return exe


def tf(stack: Stack, *args: str, stream=None, capture: bool = False, interactive: bool = False) -> str:
    env = {**os.environ, "TF_IN_AUTOMATION": "1"}
    if not interactive:
        env["TF_INPUT"] = "0"
    cmd = [terraform_bin(), *args]
    if capture:
        proc = subprocess.run(cmd, cwd=stack.dir, env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    else:
        proc = subprocess.run(cmd, cwd=stack.dir, env=env, stdout=stream, stderr=stream, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-2000:] if capture else "see the output above"
        raise InfraError(f"terraform {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc.stdout if capture else ""


def init(stack: Stack, stream=None) -> None:
    tf(stack, "init", "-input=false", "-no-color", "-reconfigure", stream=stream)


def plan(stack: Stack, stream=None) -> dict:
    """Plan into plan.tfplan and write its JSON to policy/plan.json; returns the plan JSON."""
    tf(stack, "plan", "-input=false", "-no-color", "-lock-timeout=5m", f"-out={PLAN_FILE}", stream=stream)
    doc = json.loads(tf(stack, "show", "-json", PLAN_FILE, capture=True))
    stack.plan_json.parent.mkdir(parents=True, exist_ok=True)
    stack.plan_json.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def apply(stack: Stack, stream=None) -> None:
    if not stack.plan_file.is_file():
        raise InfraError(f"no saved plan at {stack.plan_file}; run the terraform stage first")
    tf(stack, "apply", "-input=false", "-no-color", "-lock-timeout=5m", PLAN_FILE, stream=stream)


def outputs(stack: Stack) -> dict:
    raw = json.loads(tf(stack, "output", "-json", capture=True) or "{}")
    return {name: out.get("value") for name, out in raw.items()}


def destroy(stack: Stack, auto_approve: bool, stream=None) -> None:
    args = ["destroy", "-no-color", "-lock-timeout=5m"]
    if auto_approve:
        args += ["-input=false", "-auto-approve"]
    tf(stack, *args, stream=stream, interactive=not auto_approve)


def changes(plan_doc: dict) -> dict[str, int]:
    """Count the plan's resource changes by action: create, update, replace, delete."""
    counts = {"create": 0, "update": 0, "replace": 0, "delete": 0}
    for rc in plan_doc.get("resource_changes") or []:
        actions = rc.get("change", {}).get("actions") or []
        if "create" in actions and "delete" in actions:
            counts["replace"] += 1
        elif actions == ["create"]:
            counts["create"] += 1
        elif actions == ["update"]:
            counts["update"] += 1
        elif actions == ["delete"]:
            counts["delete"] += 1
    return counts


# --- smoke through the load balancer ---


@dataclass
class Check:
    component: str
    url: str
    host: str
    status: int | None
    ok: bool

    def as_dict(self) -> dict:
        return {"component": self.component, "url": self.url, "host": self.host, "status": self.status, "ok": self.ok}

    def text(self) -> str:
        got = self.status if self.status is not None else "no answer"
        return f"{self.component}: GET {self.url} (Host: {self.host}) -> {got}"


def get_status(url: str, host: str, timeout: float = 5.0) -> int | None:
    parts = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.netloc, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/", headers={"Host": host})
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def smoke(outs: dict, timeout: float = 300.0, interval: float = 5.0, getter=get_status) -> list[Check]:
    """GET each component's health path through the load balancer until every one answers 200."""
    deadline = time.monotonic() + timeout
    while True:
        checks = []
        for name, out in sorted(outs.items()):
            url = f"{out['url'].rstrip('/')}{out['health_path']}"
            status = getter(url, out["host"])
            checks.append(Check(name, url, out["host"], status, status == 200))
        if all(c.ok for c in checks) or time.monotonic() >= deadline:
            return checks
        time.sleep(interval)

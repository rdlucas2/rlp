"""AWS deploys: the Terraform stack rendered from rlp.yaml, and the terraform and deploy stages.

Terraform itself never runs here: tests that drive the stages replace
infra.tf with a fake that answers like terraform would. The `infra` job in
ci.yml runs `terraform validate` on the modules and on a rendered stack.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from conftest import EXAMPLES, REPO
from rlp_cli import infra, pipeline

MODULES = REPO / "infra" / "modules"
REQUIRED_TAGS = {"rlp:project": "demo", "rlp:environment": "dev", "rlp:managed-by": "rlp"}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_repo(root: Path, doc: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def repo_doc(components: list[dict], name: str = "demo") -> dict:
    return {"apiVersion": "rlp/v1alpha1", "kind": "Repo", "metadata": {"name": name, "project": name}, "components": components}


def service(name: str = "api", port: int = 8000, **extra) -> dict:
    c = {
        "name": name,
        "language": "python",
        "artifact": {"kind": "container-image"},
        "runtime": {"kind": "container-service", "port": port},
    }
    c.update(extra)
    return c


SETTINGS = infra.Settings(registry="ghcr.io/owner/repo", state_bucket="rlp-state", region="eu-west-1", modules=str(MODULES))


# --- render ---


def test_example_renders_one_module_per_container_service(tmp_path):
    root = write_repo(tmp_path / "repo", load(EXAMPLES / "rlp.yaml"))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", SETTINGS, ver="abc123")
    written = json.loads((root / ".rlp" / "infra" / "dev" / "main.tf.json").read_text(encoding="utf-8"))
    assert written == stack.config
    assert stack.components == ["api"]
    api = written["module"]["api"]
    assert api["image"] == "ghcr.io/owner/repo/hello-api-api:abc123"
    assert api["port"] == 8000
    assert api["health_path"] == "/readyz"
    # plain default -> SSM, secret -> Secrets Manager, from: service -> bring-your-own secret
    assert api["config"] == {"LOG_LEVEL": "info"}
    assert api["secrets"] == ["API_TOKEN"]
    assert api["service_config"] == {"DATABASE_URL": "db"}
    assert api["unset_config"] == []
    assert written["output"]["api"] == {"value": "${module.api}"}


def test_stack_keeps_state_in_the_bucket_and_tags_everything(tmp_path):
    root = write_repo(tmp_path / "repo", load(EXAMPLES / "rlp.yaml"))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", SETTINGS, ver="abc123")
    backend = stack.config["terraform"]["backend"]["s3"]
    assert backend == {
        "bucket": "rlp-state",
        "key": "rlp/apps/hello-api/hello-api/dev.tfstate",
        "region": "eu-west-1",
        "use_lockfile": True,
        "encrypt": True,
    }
    tags = stack.config["provider"]["aws"]["default_tags"]["tags"]
    assert {"rlp:project", "rlp:environment", "rlp:managed-by"} <= set(tags)
    assert tags["rlp:environment"] == "dev"


def test_without_a_bucket_state_stays_local(tmp_path):
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    settings = infra.Settings(modules=str(MODULES))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", settings, ver="v1")
    assert "backend" not in stack.config["terraform"]
    assert stack.backend is False
    assert stack.config["module"]["api"]["image"] == "demo-api:v1"


def test_local_module_source_is_relative_to_the_stack(tmp_path):
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", SETTINGS, ver="v1")
    source = stack.config["module"]["api"]["source"]
    # Relative when it can be; on Windows with the checkout and the temp dir on different drives, absolute.
    assert source.startswith("../") or Path(source).is_absolute()
    assert (stack.dir / source).resolve() == (MODULES / "container-service").resolve()


def test_module_source_across_drives_is_absolute(tmp_path, monkeypatch):
    def no_relpath(*args):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(infra.os.path, "relpath", no_relpath)
    source = infra.module_source(str(MODULES), "container-service", tmp_path)
    assert source == (MODULES / "container-service").resolve().as_posix()


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("git::https://github.com/rdlucas2/rlp.git//infra/modules?ref=v0.3.0", "git::https://github.com/rdlucas2/rlp.git//infra/modules/container-service?ref=v0.3.0"),
        ("git::https://github.com/rdlucas2/rlp.git//infra/modules/", "git::https://github.com/rdlucas2/rlp.git//infra/modules/container-service"),
    ],
)
def test_remote_module_source_keeps_its_ref(tmp_path, base, expected):
    assert infra.module_source(base, "container-service", tmp_path) == expected


def test_default_modules_are_this_versions_tag(tmp_path, monkeypatch):
    monkeypatch.delenv("RLP_MODULES_SOURCE", raising=False)
    monkeypatch.setattr(infra, "install_origin", dict)  # a release install has no direct_url.json
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", infra.Settings.from_env(), ver="v1")
    assert stack.config["module"]["api"]["source"].endswith(f"//infra/modules/container-service?ref=v{infra.__version__}")


SHA = "ba8222704aa208d64b63c4d3703aec5a9d519576"


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            {"url": "https://github.com/rdlucas2/rlp", "vcs_info": {"vcs": "git", "commit_id": SHA, "requested_revision": "main"}},
            f"git::https://github.com/rdlucas2/rlp.git//infra/modules?ref={SHA}",
        ),
        (
            {"url": "https://github.com/someone/rlp-fork.git", "vcs_info": {"vcs": "git", "commit_id": SHA}},
            f"git::https://github.com/someone/rlp-fork.git//infra/modules?ref={SHA}",
        ),
        (
            {"url": "ssh://git@github.com/rdlucas2/rlp.git", "vcs_info": {"vcs": "git", "commit_id": SHA}},
            f"{infra.MODULES_REPO}?ref={SHA}",
        ),
        ({}, f"{infra.MODULES_REPO}?ref=v{infra.__version__}"),
        ({"url": "https://example.com/rlp.zip", "archive_info": {}}, f"{infra.MODULES_REPO}?ref=v{infra.__version__}"),
    ],
)
def test_default_modules_follow_the_install_source(monkeypatch, origin, expected):
    monkeypatch.setattr(infra, "install_origin", lambda: origin)
    assert infra.default_modules() == expected


def test_default_modules_from_a_checkout_are_its_own(monkeypatch):
    monkeypatch.setattr(infra, "install_origin", lambda: {"url": REPO.as_uri(), "dir_info": {"editable": True}})
    assert Path(infra.default_modules()) == MODULES


def test_default_modules_from_a_directory_without_modules_use_the_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(infra, "install_origin", lambda: {"url": tmp_path.as_uri(), "dir_info": {}})
    assert infra.default_modules() == f"{infra.MODULES_REPO}?ref=v{infra.__version__}"


def test_install_origin_reads_this_installs_direct_url():
    # Whatever this environment installed, the answer is a dict and never raises.
    assert isinstance(infra.install_origin(), dict)


def test_config_without_default_and_empty_defaults(tmp_path):
    config = [
        {"name": "FEATURE", "required": True},
        {"name": "EMPTY", "default": ""},
        {"name": "PLAIN", "default": "x"},
    ]
    mapping = infra.config_mapping(service(config=config))
    assert mapping["unset_config"] == ["FEATURE"]
    assert mapping["environment_variables"] == {"EMPTY": ""}
    assert mapping["config"] == {"PLAIN": "x"}


def test_health_path_falls_back_to_liveness_then_root():
    assert infra.health_path(service(runtime={"kind": "container-service", "port": 1, "health": {"liveness": "/live"}})) == "/live"
    assert infra.health_path(service()) == "/"


def test_only_container_services_deploy(tmp_path):
    fn = {"name": "fn", "language": "python", "artifact": {"kind": "zip", "path": "dist/fn.zip"}, "runtime": {"kind": "function", "handler": "app.handler"}}
    root = write_repo(tmp_path / "repo", repo_doc([service(), fn]))
    stack = infra.render(root, load(root / "rlp.yaml"), "dev", SETTINGS, ver="v1")
    assert stack.components == ["api"]
    assert "fn" in stack.skipped
    assert list(stack.config["module"]) == ["api"]


def test_services_without_an_aws_mapping_fail(tmp_path):
    root = write_repo(tmp_path / "repo", repo_doc([service(services=[{"name": "cache", "kind": "redis"}])]))
    with pytest.raises(infra.InfraError, match="redis, which has no AWS mapping"):
        infra.render(root, load(root / "rlp.yaml"), "dev", SETTINGS, ver="v1")


@pytest.mark.parametrize("env", ["", "Dev", "1dev", "a" * 17, "dev_1"])
def test_environment_names_are_checked(tmp_path, env):
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    with pytest.raises(infra.InfraError, match="environment"):
        infra.render(root, load(root / "rlp.yaml"), env, SETTINGS)


def test_render_command(tmp_path, rlp):
    root = write_repo(tmp_path / "repo", load(EXAMPLES / "rlp.yaml"))
    result = rlp("infra", "render", "--path", str(root), "--env", "dev", "--modules", str(MODULES), "--registry", "ghcr.io/o/r", "-o", "json")
    assert result.code == 0, result.stderr
    assert result.json["file"] == ".rlp/infra/dev/main.tf.json"
    assert result.json["components"] == ["api"]
    assert (root / ".rlp" / "infra" / "dev" / "main.tf.json").is_file()


def test_render_command_reports_unknown_components(tmp_path, rlp):
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    result = rlp("infra", "render", "--path", str(root), "--env", "dev", "-c", "nope")
    assert result.code == 2
    assert "no component named nope" in result.stderr


def test_destroy_in_json_mode_needs_auto_approve(tmp_path, rlp):
    root = write_repo(tmp_path / "repo", repo_doc([service()]))
    result = rlp("infra", "destroy", "--path", str(root), "--env", "dev", "-o", "json")
    assert result.code == 2
    assert "--auto-approve" in result.json["error"]


# --- the terraform and deploy stages ---


def plan_doc(tags: dict) -> dict:
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "module.api.aws_cloudwatch_log_group.this",
                "type": "aws_cloudwatch_log_group",
                "change": {"actions": ["create"], "after": {"name": "/rlp/dev/demo/api", "tags": None, "tags_all": tags}, "after_unknown": {}},
            },
            {
                "address": "module.api.aws_ecs_service.this",
                "type": "aws_ecs_service",
                "change": {"actions": ["delete", "create"], "after": {"name": "dev-demo-api", "tags": None, "tags_all": tags}, "after_unknown": {}},
            },
        ],
    }


class FakeTerraform:
    """Stands in for infra.tf: records commands and answers show and output."""

    def __init__(self, plan: dict, outputs: dict | None = None):
        self.plan = plan
        self.outputs = outputs or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, stack, *args, stream=None, capture=False, interactive=False):
        self.calls.append(args)
        if args[0] == "plan":
            (stack.dir / infra.PLAN_FILE).write_text("binary plan", encoding="utf-8")
        if args[0] == "show":
            return json.dumps(self.plan)
        if args[0] == "output":
            return json.dumps({k: {"value": v} for k, v in self.outputs.items()})
        return ""


@pytest.fixture
def deploy_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("RLP_TF_STATE_BUCKET", "rlp-state")
    monkeypatch.setenv("RLP_REGISTRY", "ghcr.io/owner/repo")
    monkeypatch.setenv("RLP_MODULES_SOURCE", str(MODULES))
    monkeypatch.setenv("RLP_VERSION", "v1")
    return write_repo(tmp_path / "repo", repo_doc([service(runtime={"kind": "container-service", "port": 8000, "health": {"readiness": "/readyz"}})]))


def run_stages(root: Path, stages: list[str], **ctx) -> list[pipeline.StageResult]:
    return pipeline.run(pipeline.Context(root=root, **ctx), stages, reset=True)


def test_stages_skip_without_a_deploy_env(deploy_repo):
    results = run_stages(deploy_repo, ["terraform", "deploy"])
    assert [(r.stage, r.status) for r in results] == [("terraform", "skipped"), ("deploy", "skipped")]
    assert results[0].detail == "no deploy-env given"


def test_stages_skip_for_local_deploys(deploy_repo):
    results = run_stages(deploy_repo, ["terraform", "deploy"], deploy_env="dev", deploy_local=True)
    assert {r.status for r in results} == {"skipped"}
    assert "LocalStack" in results[0].detail


def test_stages_skip_when_nothing_deploys(tmp_path):
    job = {"name": "nightly", "language": "python", "artifact": {"kind": "container-image"}, "runtime": {"kind": "job"}}
    root = write_repo(tmp_path / "repo", repo_doc([job]))
    results = run_stages(root, ["terraform"], deploy_env="dev")
    assert results[0].status == "skipped"
    assert "nothing deploys to AWS" in results[0].detail


def test_terraform_stage_needs_a_state_bucket(deploy_repo, monkeypatch):
    monkeypatch.delenv("RLP_TF_STATE_BUCKET")
    results = run_stages(deploy_repo, ["terraform"], deploy_env="dev")
    assert results[0].status == "failed"
    assert "RLP_TF_STATE_BUCKET" in results[0].detail


def test_terraform_stage_plans_and_checks_the_plan(deploy_repo, monkeypatch):
    fake = FakeTerraform(plan_doc(REQUIRED_TAGS))
    monkeypatch.setattr(infra, "tf", fake)
    results = run_stages(deploy_repo, ["terraform"], deploy_env="dev")
    r = results[0]
    assert r.status == "passed", r.detail
    assert [c[0] for c in fake.calls] == ["init", "plan", "show"]
    assert r.data["changes"] == {"create": 1, "update": 0, "replace": 1, "delete": 0}
    assert "1 to create, 1 to replace" in r.detail
    assert (deploy_repo / ".rlp" / "infra" / "dev" / "policy" / "plan.json").is_file()


def test_terraform_stage_fails_a_plan_that_drops_required_tags(deploy_repo, monkeypatch):
    monkeypatch.setattr(infra, "tf", FakeTerraform(plan_doc({"rlp:project": "demo"})))
    results = run_stages(deploy_repo, ["terraform"], deploy_env="dev")
    r = results[0]
    assert r.status == "failed"
    assert "RLP-T001" in r.detail
    assert {v["rule"] for v in r.data["violations"]} == {"RLP-T001"}


def test_deploy_stage_needs_a_saved_plan(deploy_repo, monkeypatch):
    monkeypatch.setattr(infra, "tf", FakeTerraform(plan_doc(REQUIRED_TAGS)))
    results = run_stages(deploy_repo, ["deploy"], deploy_env="dev")
    assert results[0].status == "failed"
    assert "run the terraform stage first" in results[0].detail


class HostHandler(BaseHTTPRequestHandler):
    """Answers 200 on /readyz only for the expected Host, like the shared listener's rule."""

    host: ClassVar[str] = "api-demo.dev.rlp.internal"

    def do_GET(self):  # noqa: N802 (http.server API)
        ok = self.path == "/readyz" and self.headers.get("Host") == self.host
        self.send_response(200 if ok else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def load_balancer():
    server = ThreadingHTTPServer(("127.0.0.1", 0), HostHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_deploy_applies_the_plan_and_checks_readiness_through_the_load_balancer(deploy_repo, monkeypatch, load_balancer):
    out = {"api": {"url": load_balancer, "host": HostHandler.host, "health_path": "/readyz"}}
    fake = FakeTerraform(plan_doc(REQUIRED_TAGS), outputs=out)
    monkeypatch.setattr(infra, "tf", fake)
    results = run_stages(deploy_repo, ["terraform", "deploy"], deploy_env="dev")
    assert [(r.stage, r.status) for r in results] == [("terraform", "passed"), ("deploy", "passed")], results[-1].detail
    assert [c[0] for c in fake.calls] == ["init", "plan", "show", "apply", "output"]
    assert infra.PLAN_FILE in fake.calls[3]
    assert results[1].data["checks"][0]["status"] == 200


def test_deploy_fails_when_readiness_never_answers(deploy_repo, monkeypatch, load_balancer):
    out = {"api": {"url": load_balancer, "host": "wrong.host", "health_path": "/readyz"}}
    monkeypatch.setattr(infra, "tf", FakeTerraform(plan_doc(REQUIRED_TAGS), outputs=out))
    results = run_stages(deploy_repo, ["terraform", "deploy"], deploy_env="dev", smoke_timeout=0)
    assert results[1].status == "failed"
    assert "readiness failed" in results[1].detail
    assert "-> 404" in results[1].detail

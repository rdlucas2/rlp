"""The local runtime: rlp.yaml to Compose (plan), the files rlp run writes, and run/smoke/down."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from conftest import EXAMPLES, REPO
from rlp_cli import runtime

FUNCTION_FIXTURE = REPO / "tests" / "runtime" / "fixtures" / "hello-function"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def repo_doc(components: list[dict], name: str = "demo") -> dict:
    return {"apiVersion": "rlp/v1alpha1", "kind": "Repo", "metadata": {"name": name, "project": name}, "components": components}


def write_repo(root: Path, doc: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def service(name: str, port: int, **extra) -> dict:
    c = {
        "name": name,
        "language": "python",
        "artifact": {"kind": "container-image"},
        "runtime": {"kind": "container-service", "port": port},
        "telemetry": {"enabled": False},
    }
    c.update(extra)
    return c


# --- plan: the example contract (container-service, postgres, secret, telemetry) ---


@pytest.fixture
def example() -> dict:
    return load(EXAMPLES / "rlp.yaml")


def test_example_component_builds_from_its_dockerfile(tmp_path, example):
    p = runtime.plan(tmp_path, example, {"api": {"API_TOKEN": "t0ken"}}, {"RLP_API_DB_PASSWORD": "pw"})
    api = p.compose["services"]["api"]
    assert p.project == "rlp-hello-api"
    assert api["build"] == {"context": "..", "dockerfile": "Dockerfile"}
    assert api["image"] == "hello-api-api:local"
    assert api["ports"] == ["127.0.0.1:8000:8000"]
    assert [e.as_dict() for e in p.endpoints] == [
        {"component": "api", "kind": "http", "service": "api", "url": "http://127.0.0.1:8000", "paths": ["/healthz", "/readyz"]}
    ]


def test_example_config_resolves_defaults_and_services(tmp_path, example):
    p = runtime.plan(tmp_path, example, {}, {})
    env = p.compose["services"]["api"]["environment"]
    assert env["LOG_LEVEL"] == "info"
    assert env["DATABASE_URL"] == "postgresql://app:${RLP_API_DB_PASSWORD}@api-db:5432/app"
    assert "API_TOKEN" not in env


def test_example_postgres_is_healthchecked_with_a_volume(tmp_path, example):
    p = runtime.plan(tmp_path, example, {}, {})
    db = p.compose["services"]["api-db"]
    assert db["image"] == "postgres:17"
    assert db["environment"]["POSTGRES_PASSWORD"] == "${RLP_API_DB_PASSWORD}"
    assert db["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U app -d app"]
    assert db["volumes"] == ["api-db-data:/var/lib/postgresql"]
    assert p.compose["volumes"] == {"api-db-data": {}}
    assert p.compose["services"]["api"]["depends_on"]["api-db"] == {"condition": "service_healthy"}


def test_example_telemetry_gets_a_collector(tmp_path, example):
    p = runtime.plan(tmp_path, example, {}, {})
    env = p.compose["services"]["api"]["environment"]
    assert env["OTEL_SERVICE_NAME"] == "hello-api"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4318"
    assert p.compose["services"]["otel-collector"]["image"] == runtime.COLLECTOR_IMAGE
    assert "receivers:" in p.files["otel-collector.yaml"]


def test_secrets_go_to_an_env_file_never_into_compose(tmp_path, example):
    p = runtime.plan(tmp_path, example, {"api": {"API_TOKEN": "t0ken-value"}}, {"RLP_API_DB_PASSWORD": "db-pass"})
    assert p.compose["services"]["api"]["env_file"] == ["secrets/api.env"]
    runtime.write(tmp_path, p)
    compose_text = (tmp_path / ".rlp" / "compose.yaml").read_text(encoding="utf-8")
    assert "t0ken-value" not in compose_text
    assert "db-pass" not in compose_text
    assert runtime.read_env_file(tmp_path / ".rlp" / "secrets" / "api.env") == {"API_TOKEN": "t0ken-value"}
    assert runtime.read_env_file(tmp_path / ".rlp" / ".env") == {"COMPOSE_PROJECT_NAME": "rlp-hello-api", "RLP_API_DB_PASSWORD": "db-pass"}
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / ".rlp" / "secrets" / "api.env").stat().st_mode) == 0o600


def test_literal_dollars_are_escaped_for_compose(tmp_path):
    doc = repo_doc([service("api", 8000, config=[{"name": "PATTERN", "default": "a$b"}])])
    assert runtime.plan(tmp_path, doc).compose["services"]["api"]["environment"]["PATTERN"] == "a$$b"


def test_env_file_values_round_trip_quotes(tmp_path):
    path = tmp_path / "x.env"
    runtime.write_env_file(path, {"A": "s3cr$t", "B": "it's"}, "test")
    assert runtime.read_env_file(path) == {"A": "s3cr$t", "B": "it's"}
    with pytest.raises(runtime.RuntimeFailure):
        runtime.write_env_file(path, {"C": "it's $x"}, "test")


# --- plan: other runtimes ---


def test_zip_function_runs_in_the_lambda_base_image(tmp_path):
    doc = load(FUNCTION_FIXTURE / "rlp.yaml")
    p = runtime.plan(tmp_path, doc)
    fn = p.compose["services"]["fn"]
    assert fn["image"] == runtime.LAMBDA_IMAGES["python"]
    assert fn["command"] == ["app.handler"]
    assert fn["volumes"] == ["./functions/fn:/var/task:ro"]
    assert fn["ports"] == ["127.0.0.1:9000:8080"]
    assert fn["environment"] == {"GREETING": "hello"}
    assert p.extract == {"functions/fn": tmp_path / "dist" / "function.zip"}
    assert p.endpoints[0].url == f"http://127.0.0.1:9000{runtime.INVOKE_PATH}"
    assert "otel-collector" not in p.compose["services"]


def test_function_language_without_a_base_image_is_an_error(tmp_path):
    doc = repo_doc([{
        "name": "fn", "language": "cobol", "artifact": {"kind": "zip", "path": "f.zip"}, "runtime": {"kind": "function", "handler": "x"},
    }])
    with pytest.raises(runtime.RuntimeFailure, match="no Lambda base image for language 'cobol'"):
        runtime.plan(tmp_path, doc)


def test_static_site_is_served_by_nginx(tmp_path):
    doc = repo_doc([{
        "name": "web", "path": "web", "language": "typescript",
        "artifact": {"kind": "static", "path": "dist"}, "runtime": {"kind": "static-site"},
    }])
    p = runtime.plan(tmp_path, doc)
    web = p.compose["services"]["web"]
    assert web["image"] == runtime.NGINX_IMAGE
    assert web["volumes"] == ["../web/dist:/usr/share/nginx/html:ro"]
    assert web["ports"] == ["127.0.0.1:8080:80"]
    assert p.endpoints[0].paths == ["/"]


def test_job_is_one_shot(tmp_path):
    doc = repo_doc([{"name": "nightly", "language": "go", "artifact": {"kind": "container-image"}, "runtime": {"kind": "job", "schedule": "rate(1 day)"}}])
    p = runtime.plan(tmp_path, doc)
    assert p.compose["services"]["nightly"]["restart"] == "no"
    assert "ports" not in p.compose["services"]["nightly"]
    assert p.endpoints[0].kind == "job"


def test_unsupported_combination_is_an_error(tmp_path):
    doc = repo_doc([{"name": "api", "language": "java", "artifact": {"kind": "jar", "path": "a.jar"}, "runtime": {"kind": "container-service", "port": 8080}}])
    with pytest.raises(runtime.RuntimeFailure, match="container-service with artifact jar cannot run locally"):
        runtime.plan(tmp_path, doc)


def test_host_ports_never_collide(tmp_path):
    doc = repo_doc([service("a", 8000), service("b", 8000), service("c", 8001)])
    ports = [runtime.plan(tmp_path, doc).compose["services"][n]["ports"][0] for n in "abc"]
    assert ports == ["127.0.0.1:8000:8000", "127.0.0.1:8001:8000", "127.0.0.1:8002:8001"]


def test_localstack_services_get_resources_and_aws_settings(tmp_path):
    doc = repo_doc([service("api", 8000, services=[
        {"name": "uploads", "kind": "s3"}, {"name": "jobs", "kind": "sqs"}, {"name": "events", "kind": "sns"}, {"name": "cache", "kind": "redis"},
    ], config=[
        {"name": "BUCKET", "from": "service:uploads"}, {"name": "QUEUE_URL", "from": "service:jobs"},
        {"name": "TOPIC_ARN", "from": "service:events"}, {"name": "REDIS_URL", "from": "service:cache"},
    ])])
    p = runtime.plan(tmp_path, doc)
    env = p.compose["services"]["api"]["environment"]
    assert env["BUCKET"] == "demo-uploads"
    assert env["QUEUE_URL"] == "http://localstack:4566/000000000000/demo-jobs"
    assert env["TOPIC_ARN"] == "arn:aws:sns:us-east-1:000000000000:demo-events"
    assert env["REDIS_URL"] == "redis://api-cache:6379/0"
    assert env["AWS_ENDPOINT_URL"] == "http://localstack:4566"
    assert p.compose["services"]["localstack"]["image"] == runtime.LOCALSTACK_IMAGE
    assert p.compose["services"]["api-cache"]["image"] == "redis:7.4"
    script = p.files["localstack/ready.d/rlp-init.sh"]
    assert "awslocal s3 mb s3://demo-uploads" in script
    assert "awslocal sqs create-queue --queue-name demo-jobs" in script
    assert "awslocal sns create-topic --name demo-events" in script
    assert set(p.compose["services"]["api"]["depends_on"]) == {"localstack", "api-cache"}


def test_required_config_comes_from_the_environment(tmp_path):
    doc = repo_doc([service("api", 8000, config=[{"name": "REGION", "required": True}])])
    with pytest.raises(runtime.RuntimeFailure, match="REGION"):
        runtime.plan(tmp_path, doc, environ={})
    assert runtime.plan(tmp_path, doc, environ={"REGION": "eu"}).compose["services"]["api"]["environment"]["REGION"] == "eu"
    assert "REGION" not in runtime.plan(tmp_path, doc, environ={}, strict=False).compose["services"]["api"]["environment"]


# --- secrets and credentials ---


def secret_doc(required: bool = True) -> dict:
    return repo_doc([service("api", 8000, config=[{"name": "API_TOKEN", "secret": True, "required": required}])])


def test_secrets_from_environment_then_prompt(tmp_path):
    assert runtime.collect_secrets(tmp_path, secret_doc(), None, {"API_TOKEN": "env"}) == {"api": {"API_TOKEN": "env"}}
    asked = []
    found = runtime.collect_secrets(tmp_path, secret_doc(), lambda spec: asked.append(spec.name) or "typed", {})
    assert found == {"api": {"API_TOKEN": "typed"}} and asked == ["API_TOKEN"]


def test_stored_secret_is_not_asked_again(tmp_path):
    runtime.write_env_file(runtime.secrets_path(tmp_path, "api"), {"API_TOKEN": "stored"}, "t")
    found = runtime.collect_secrets(tmp_path, secret_doc(), lambda spec: pytest.fail("prompted"), {"API_TOKEN": "env"})
    assert found == {"api": {"API_TOKEN": "stored"}}


def test_missing_required_secret_is_an_error_optional_is_skipped(tmp_path):
    with pytest.raises(runtime.RuntimeFailure, match="api/API_TOKEN"):
        runtime.collect_secrets(tmp_path, secret_doc(), None, {})
    assert runtime.collect_secrets(tmp_path, secret_doc(required=False), None, {}) == {}


def test_database_password_is_generated_once(tmp_path, example):
    first = runtime.generated_credentials(tmp_path, example)
    assert len(first["RLP_API_DB_PASSWORD"]) == 32
    runtime.write(tmp_path, runtime.plan(tmp_path, example, {}, first))
    assert runtime.generated_credentials(tmp_path, example) == first


# --- write: unpacking function archives ---


def test_write_unpacks_the_function_zip(tmp_path):
    repo = tmp_path / "fn"
    shutil.copytree(FUNCTION_FIXTURE, repo)
    (repo / "dist").mkdir()
    with zipfile.ZipFile(repo / "dist" / "function.zip", "w") as zf:
        zf.write(repo / "src" / "app.py", "app.py")
    runtime.write(repo, runtime.plan(repo, load(repo / "rlp.yaml")))
    assert (repo / ".rlp" / "functions" / "fn" / "app.py").is_file()


def test_write_rejects_zip_entries_outside_the_target(tmp_path):
    repo = tmp_path / "fn"
    shutil.copytree(FUNCTION_FIXTURE, repo)
    (repo / "dist").mkdir()
    with zipfile.ZipFile(repo / "dist" / "function.zip", "w") as zf:
        zf.writestr("../../evil.py", "x")
    with pytest.raises(runtime.RuntimeFailure, match="escapes"):
        runtime.write(repo, runtime.plan(repo, load(repo / "rlp.yaml")))


def test_write_needs_the_package_first(tmp_path):
    repo = tmp_path / "fn"
    shutil.copytree(FUNCTION_FIXTURE, repo)
    with pytest.raises(runtime.RuntimeFailure, match="package the component first"):
        runtime.write(repo, runtime.plan(repo, load(repo / "rlp.yaml")))


# --- CLI ---


def test_run_no_start_packages_and_writes_compose(rlp, tmp_path):
    if shutil.which("task") is None:
        pytest.fail("go-task (`task`) must be on PATH for the CLI tests")
    repo = tmp_path / "fn"
    shutil.copytree(FUNCTION_FIXTURE, repo)
    result = rlp("run", "--no-start", "--path", str(repo), "-o", "json")
    assert result.code == 0, result.stdout + result.stderr
    assert result.json["project"] == "rlp-hello-function"
    assert result.json["endpoints"][0]["kind"] == "invoke"
    assert load(repo / ".rlp" / "compose.yaml")["services"]["fn"]["command"] == ["app.handler"]
    assert (repo / ".rlp" / "functions" / "fn" / "app.py").is_file()


def test_run_without_a_value_for_a_required_secret_fails_in_json_mode(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", secret_doc())
    result = rlp("run", "--no-start", "--path", str(repo), "-o", "json", env={"API_TOKEN": ""})
    assert result.code == 2
    assert "api/API_TOKEN" in result.json["error"]


def fake_docker(bin_dir: Path, ps_json: str = "") -> Path:
    """A docker stand-in that logs its arguments and answers `compose ps`."""
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "docker.log"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$*" in *" ps "*) cat <<\'EOF\'\n' + ps_json + "\nEOF\n;; esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return log


def path_env(bin_dir: Path) -> dict:
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_run_starts_compose_then_waits_for_smoke(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", repo_doc([{"name": "once", "language": "go", "artifact": {"kind": "container-image"}, "runtime": {"kind": "job"}, "telemetry": {"enabled": False}}]))
    log = fake_docker(tmp_path / "bin", json.dumps({"Service": "once", "State": "exited", "ExitCode": 0}))
    result = rlp("run", "--path", str(repo), "-o", "json", env=path_env(tmp_path / "bin"))
    assert result.code == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("compose --project-name rlp-demo --project-directory")
    assert calls[0].endswith("up --detach --build --remove-orphans")
    assert result.json["checks"] == [{"component": "once", "kind": "job", "target": "once", "ok": True, "status": "exited 0"}]


@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_failed_job_fails_smoke_without_waiting(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", repo_doc([{"name": "once", "language": "go", "artifact": {"kind": "container-image"}, "runtime": {"kind": "job"}}]))
    fake_docker(tmp_path / "bin", json.dumps({"Service": "once", "State": "exited", "ExitCode": 3}))
    result = rlp("smoke", "--path", str(repo), "--timeout", "60", "-o", "json", env=path_env(tmp_path / "bin"))
    assert result.code == 1
    assert result.json["checks"][0]["status"] == "exited 3"


@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_down_removes_the_project_and_optionally_volumes(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", repo_doc([service("api", 8000)]))
    log = fake_docker(tmp_path / "bin")
    assert rlp("down", "--path", str(repo), env=path_env(tmp_path / "bin")).code == 2  # nothing generated yet
    assert rlp("run", "--no-start", "--path", str(repo), env=path_env(tmp_path / "bin")).code == 0
    assert rlp("down", "--path", str(repo), "-v", env=path_env(tmp_path / "bin")).code == 0
    assert log.read_text().splitlines()[-1].endswith("down --remove-orphans --volumes")


def test_run_without_docker_is_a_setup_error(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", repo_doc([service("api", 8000)]))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = rlp("run", "--path", str(repo), "-o", "json", env={"PATH": str(empty)})
    assert result.code == 2
    assert "docker not found" in result.json["error"]


# --- smoke against real HTTP endpoints ---


class Handler(BaseHTTPRequestHandler):
    get_status: ClassVar[dict[str, int]] = {}
    function_error: ClassVar[bool] = False

    def do_GET(self):
        self.send_response(self.get_status.get(self.path, 404))
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200 if self.path == runtime.INVOKE_PATH else 404)
        if self.function_error:
            self.send_header("X-Amz-Function-Error", "Unhandled")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health_repo(tmp_path: Path, port: int) -> Path:
    c = service("api", port)
    c["runtime"]["health"] = {"liveness": "/healthz", "readiness": "/readyz"}
    return write_repo(tmp_path / "repo", repo_doc([c]))


def test_smoke_checks_declared_health_endpoints(rlp, tmp_path, server):
    Handler.get_status = {"/healthz": 200, "/readyz": 200}
    result = rlp("smoke", "--path", str(health_repo(tmp_path, server)), "-o", "json")
    assert result.code == 0, result.stdout + result.stderr
    assert [c["status"] for c in result.json["checks"]] == ["200", "200"]


def test_smoke_fails_when_not_ready(rlp, tmp_path, server):
    Handler.get_status = {"/healthz": 200, "/readyz": 503}
    result = rlp("smoke", "--path", str(health_repo(tmp_path, server)), "--timeout", "0", "-o", "json")
    assert result.code == 1
    assert result.json["checks"][1] == {"component": "api", "kind": "http", "target": f"http://127.0.0.1:{server}/readyz", "ok": False, "status": "503"}


def test_smoke_reports_no_answer(rlp, tmp_path):
    result = rlp("smoke", "--path", str(health_repo(tmp_path, free_port())), "--timeout", "0")
    assert result.code == 1
    assert "no answer (failed)" in result.stdout


def function_endpoint(port: int) -> runtime.Endpoint:
    return runtime.Endpoint("fn", "invoke", "fn", f"http://127.0.0.1:{port}{runtime.INVOKE_PATH}")


def test_invoke_passes_and_function_errors_fail(tmp_path, server):
    Handler.function_error = False
    (check,) = runtime.check_once(tmp_path, "p", function_endpoint(server))
    assert check.ok and check.status == "200"
    Handler.function_error = True
    try:
        (check,) = runtime.check_once(tmp_path, "p", function_endpoint(server))
    finally:
        Handler.function_error = False
    assert not check.ok and check.status == "function error (Unhandled)"


def test_undeclared_health_accepts_any_answer_below_500(tmp_path, server):
    Handler.get_status = {}
    (check,) = runtime.check_once(tmp_path, "p", runtime.Endpoint("api", "http", "api", f"http://127.0.0.1:{server}"))
    assert check.ok and check.status == "404"

"""The local runtime: rlp.yaml rendered as a Docker Compose environment.

`rlp run` writes these files under .rlp/ (gitignored) and starts them:

- compose.yaml: one service per component and per backing service
- .env: the Compose project name and the generated local credentials
- secrets/<component>.env: each component's secret config entries
- otel-collector.yaml, localstack/ready.d/rlp-init.sh, functions/<component>/

Secrets live only in secrets/*.env; compose.yaml references those files and
never holds a secret value. Generated credentials (the local Postgres
password) are kept in .env so a database volume stays usable across runs.

Everything here is pure except write(), up(), down() and smoke(); plan() turns
a parsed rlp.yaml into the files without touching Docker.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from rlp_cli.contract import component_dir

# Pinned images. Bumping one is a reviewed change, like any other pin.
COLLECTOR_IMAGE = "otel/opentelemetry-collector:0.130.0"
NGINX_IMAGE = "nginx:1.29-alpine"
# LocalStack images from 2026.03 on need a LocalStack account token; 4.9 does not.
# Set LOCALSTACK_AUTH_TOKEN in your environment to use a newer image.
LOCALSTACK_IMAGE = "localstack/localstack:4.9"
SERVICE_IMAGES = {"postgres": ("postgres", "17"), "redis": ("redis", "7.4")}
LAMBDA_IMAGES = {
    "python": "public.ecr.aws/lambda/python:3.13",
    "javascript": "public.ecr.aws/lambda/nodejs:22",
    "typescript": "public.ecr.aws/lambda/nodejs:22",
    "nodejs": "public.ecr.aws/lambda/nodejs:22",
    "java": "public.ecr.aws/lambda/java:21",
}
LOCALSTACK_KINDS = {"s3", "sqs", "sns"}
AWS_ACCOUNT = "000000000000"
AWS_REGION = "us-east-1"
COLLECTOR = "otel-collector"
LOCALSTACK = "localstack"
FUNCTION_PORT = 8080
INVOKE_PATH = "/2015-03-31/functions/function/invocations"

RLP_DIR = ".rlp"


class RuntimeFailure(Exception):
    """The local runtime cannot be planned or started."""


@dataclass
class Endpoint:
    """What smoke checks for one component."""

    component: str
    kind: str  # http, invoke or job
    service: str
    url: str = ""
    paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"component": self.component, "kind": self.kind, "service": self.service}
        if self.url:
            d["url"] = self.url
        if self.paths:
            d["paths"] = self.paths
        return d


@dataclass
class SecretSpec:
    component: str
    name: str
    required: bool
    description: str = ""


@dataclass
class Plan:
    project: str
    compose: dict
    env: dict[str, str]
    endpoints: list[Endpoint]
    files: dict[str, str] = field(default_factory=dict)  # extra files under .rlp/, path -> content
    extract: dict[str, Path] = field(default_factory=dict)  # .rlp/functions/<c> -> zip or jar to unpack
    secret_files: dict[str, dict[str, str]] = field(default_factory=dict)  # component -> {NAME: value}


# --- naming ---


def project_name(doc: dict) -> str:
    return "rlp-" + re.sub(r"[^a-z0-9_-]", "-", doc["metadata"]["name"].lower())


def env_key(*parts: str) -> str:
    return "_".join(re.sub(r"[^A-Za-z0-9]", "_", p).upper() for p in parts)


def escape(value: str) -> str:
    """A literal value for Compose: `$` would otherwise start an interpolation."""
    return value.replace("$", "$$")


def rel(from_dir: Path, to: Path) -> str:
    """POSIX relative path from .rlp/ to a repo path, as Compose expects."""
    return PurePosixPath(os.path.relpath(to, from_dir).replace(os.sep, "/")).as_posix()


def service_host(component: str, service: str) -> str:
    return f"{component}-{service}"


def aws_name(doc: dict, service: str) -> str:
    return f"{doc['metadata']['name']}-{service}".lower()


# --- secrets and generated credentials ---


def secret_specs(doc: dict) -> list[SecretSpec]:
    return [
        SecretSpec(c["name"], e["name"], bool(e.get("required")), e.get("description", ""))
        for c in doc.get("components") or []
        for e in c.get("config") or []
        if e.get("secret")
    ]


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = unquote(value)
    return values


def quote(value: str) -> str:
    """An env-file value Compose reads literally: single quotes stop interpolation."""
    if "'" not in value:
        return f"'{value}'"
    if "$" in value or '"' in value or "\\" in value:
        raise RuntimeFailure("a secret may not contain both a single quote and one of $ \" \\")
    return f'"{value}"'


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def write_env_file(path: Path, values: dict[str, str], header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {header}"] + [f"{k}={quote(v)}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - chmod is best effort on Windows
        pass


def collect_secrets(
    root: Path,
    doc: dict,
    prompt: Callable[[SecretSpec], str | None] | None,
    environ: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Each component's secret values: the stored file, else $NAME, else a prompt.

    Values found by environment or prompt are stored, so you are asked once.
    A required secret with no value is an error; an optional one is left unset.
    """
    environ = os.environ if environ is None else environ
    found: dict[str, dict[str, str]] = {}
    missing = []
    for spec in secret_specs(doc):
        stored = read_env_file(secrets_path(root, spec.component))
        value = stored.get(spec.name) or environ.get(spec.name)
        if not value and prompt is not None:
            value = prompt(spec)
        if value:
            if "\n" in value:
                raise RuntimeFailure(f"secret {spec.component}/{spec.name} contains a newline")
            found.setdefault(spec.component, {})[spec.name] = value
        elif spec.required:
            missing.append(f"{spec.component}/{spec.name}")
    if missing:
        raise RuntimeFailure(
            "required secret(s) have no value: " + ", ".join(missing)
            + "; set them in your environment or run rlp run in a terminal to be prompted"
        )
    return found


def secrets_path(root: Path, component: str) -> Path:
    return root / RLP_DIR / "secrets" / f"{component}.env"


def generated_credentials(root: Path, doc: dict) -> dict[str, str]:
    """Local database passwords: reused from .rlp/.env, else generated."""
    existing = read_env_file(root / RLP_DIR / ".env")
    creds = {}
    for c in doc.get("components") or []:
        for s in c.get("services") or []:
            if s["kind"] == "postgres":
                key = env_key("RLP", c["name"], s["name"], "PASSWORD")
                creds[key] = existing.get(key) or pysecrets.token_hex(16)
    return creds


# --- planning ---


class Ports:
    """Host ports, handed out in component order so smoke can recompute them."""

    def __init__(self) -> None:
        self.used: set[int] = set()

    def take(self, wanted: int) -> int:
        port = wanted
        while port in self.used:
            port += 1
        self.used.add(port)
        return port


def service_value(doc: dict, component: str, service: dict) -> str:
    """The value of config.from: service:<name>."""
    kind, name = service["kind"], service["name"]
    host = service_host(component, name)
    if kind == "postgres":
        password = "${" + env_key("RLP", component, name, "PASSWORD") + "}"
        return f"postgresql://app:{password}@{host}:5432/app"
    if kind == "redis":
        return f"redis://{host}:6379/0"
    resource = aws_name(doc, name)
    if kind == "s3":
        return resource
    if kind == "sqs":
        return f"http://{LOCALSTACK}:4566/{AWS_ACCOUNT}/{resource}"
    return f"arn:aws:sns:{AWS_REGION}:{AWS_ACCOUNT}:{resource}"


def component_environment(doc: dict, c: dict, environ: dict[str, str], strict: bool = True) -> dict[str, str]:
    env: dict[str, str] = {}
    services = {s["name"]: s for s in c.get("services") or []}
    missing = []
    for entry in c.get("config") or []:
        name = entry["name"]
        if entry.get("secret"):
            continue  # from secrets/<component>.env
        source = entry.get("from", "")
        if source.startswith("service:"):
            env[name] = service_value(doc, c["name"], services[source.removeprefix("service:")])
        elif "default" in entry:
            env[name] = escape(str(entry["default"]))
        elif environ.get(name):
            env[name] = escape(environ[name])
        elif entry.get("required"):
            missing.append(name)
    if missing and strict:
        raise RuntimeFailure(
            f"component {c['name']}: required config has no default and is not set in your environment: {', '.join(missing)}"
        )
    if any(s["kind"] in LOCALSTACK_KINDS for s in services.values()):
        env.update({
            "AWS_ENDPOINT_URL": f"http://{LOCALSTACK}:4566",
            "AWS_REGION": AWS_REGION,
            "AWS_DEFAULT_REGION": AWS_REGION,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
        })
    telemetry = c.get("telemetry") or {}
    if telemetry.get("enabled", True):
        env.update({
            "OTEL_SERVICE_NAME": telemetry.get("serviceName") or f"{doc['metadata']['name']}-{c['name']}",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{COLLECTOR}:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=local",
        })
    return env


def backing_service(c: dict, s: dict) -> dict:
    kind = s["kind"]
    image, default_version = SERVICE_IMAGES[kind]
    svc: dict = {"image": f"{image}:{s.get('version') or default_version}", "restart": "unless-stopped"}
    if kind == "postgres":
        svc["environment"] = {
            "POSTGRES_USER": "app",
            "POSTGRES_DB": "app",
            "POSTGRES_PASSWORD": "${" + env_key("RLP", c["name"], s["name"], "PASSWORD") + "}",
        }
        svc["healthcheck"] = {"test": ["CMD-SHELL", "pg_isready -U app -d app"], "interval": "2s", "timeout": "3s", "retries": 30}
        svc["volumes"] = [f"{service_host(c['name'], s['name'])}-data:/var/lib/postgresql"]
    else:
        svc["healthcheck"] = {"test": ["CMD", "redis-cli", "ping"], "interval": "2s", "timeout": "3s", "retries": 30}
    return svc


def localstack_init(doc: dict) -> str:
    lines = ["#!/bin/sh", "# Generated by rlp run: creates the S3, SQS and SNS resources rlp.yaml declares.", "set -e"]
    seen = set()
    for c in doc.get("components") or []:
        for s in c.get("services") or []:
            name = aws_name(doc, s["name"])
            if (s["kind"], name) in seen:
                continue
            seen.add((s["kind"], name))
            if s["kind"] == "s3":
                lines.append(f"awslocal s3 mb s3://{name}")
            elif s["kind"] == "sqs":
                lines.append(f"awslocal sqs create-queue --queue-name {name}")
            elif s["kind"] == "sns":
                lines.append(f"awslocal sns create-topic --name {name}")
    return "\n".join(lines) + "\n"


COLLECTOR_CONFIG = """\
# Generated by rlp run: a local OpenTelemetry collector that logs what it receives.
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
exporters:
  debug:
    verbosity: basic
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      exporters: [debug]
    logs:
      receivers: [otlp]
      exporters: [debug]
"""


def unsupported(c: dict) -> RuntimeFailure:
    return RuntimeFailure(
        f"component {c['name']}: runtime {c['runtime']['kind']} with artifact {c['artifact']['kind']} "
        "cannot run locally yet"
    )


def plan(
    root: Path,
    doc: dict,
    secret_values: dict[str, dict[str, str]] | None = None,
    credentials: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    strict: bool = True,
) -> Plan:
    """The Compose environment for rlp.yaml. strict=False skips the missing-config check (for smoke)."""
    environ = os.environ if environ is None else environ
    secret_values = secret_values or {}
    rlp_dir = root / RLP_DIR
    ports = Ports()
    services: dict[str, dict] = {}
    volumes: dict[str, dict] = {}
    endpoints: list[Endpoint] = []
    files: dict[str, str] = {}
    extract: dict[str, Path] = {}
    any_telemetry = False
    any_localstack = False

    for c in doc.get("components") or []:
        name = c["name"]
        runtime, artifact = c["runtime"], c["artifact"]
        rkind, akind = runtime["kind"], artifact["kind"]
        cdir = component_dir(root, c)
        svc: dict = {"environment": component_environment(doc, c, environ, strict)}

        if akind == "container-image":
            svc["build"] = {"context": rel(rlp_dir, cdir), "dockerfile": artifact.get("dockerfile", "Dockerfile")}
            svc["image"] = f"{doc['metadata']['name']}-{name}:local".lower()

        if rkind == "container-service":
            if akind != "container-image":
                raise unsupported(c)
            port = runtime["port"]
            host = ports.take(port)
            svc["ports"] = [f"127.0.0.1:{host}:{port}"]
            svc["restart"] = "unless-stopped"
            health = runtime.get("health") or {}
            paths = [p for p in (health.get("liveness"), health.get("readiness")) if p]
            endpoints.append(Endpoint(name, "http", name, f"http://127.0.0.1:{host}", paths))
        elif rkind == "function":
            if akind in ("zip", "jar"):
                image = LAMBDA_IMAGES.get(c["language"])
                if image is None:
                    raise RuntimeFailure(
                        f"component {name}: no Lambda base image for language {c['language']!r}; "
                        f"known: {', '.join(sorted(LAMBDA_IMAGES))}. Package it as a container-image instead"
                    )
                svc["image"] = image
                svc["command"] = [runtime["handler"]]
                target = f"functions/{name}"
                extract[target] = cdir.joinpath(*PurePosixPath(artifact["path"]).parts)
                svc["volumes"] = [f"./{target}:/var/task:ro"]
            elif akind != "container-image":
                raise unsupported(c)
            host = ports.take(9000)
            svc["ports"] = [f"127.0.0.1:{host}:{FUNCTION_PORT}"]
            endpoints.append(Endpoint(name, "invoke", name, f"http://127.0.0.1:{host}{INVOKE_PATH}"))
        elif rkind == "static-site":
            if akind == "static":
                svc["image"] = NGINX_IMAGE
                site = cdir.joinpath(*PurePosixPath(artifact["path"]).parts)
                svc["volumes"] = [f"{rel(rlp_dir, site)}:/usr/share/nginx/html:ro"]
                inner = 80
            elif akind == "container-image":
                inner = runtime.get("port", 80)
            else:
                raise unsupported(c)
            host = ports.take(runtime.get("port", 8080))
            svc["ports"] = [f"127.0.0.1:{host}:{inner}"]
            svc["restart"] = "unless-stopped"
            endpoints.append(Endpoint(name, "http", name, f"http://127.0.0.1:{host}", ["/"]))
        else:  # job
            if akind != "container-image":
                raise unsupported(c)
            svc["restart"] = "no"
            endpoints.append(Endpoint(name, "job", name))

        if c["name"] in secret_values:
            svc["env_file"] = [f"secrets/{name}.env"]

        depends: dict[str, dict] = {}
        for s in c.get("services") or []:
            if s["kind"] in LOCALSTACK_KINDS:
                any_localstack = True
                depends[LOCALSTACK] = {"condition": "service_healthy"}
                continue
            host_name = service_host(name, s["name"])
            services[host_name] = backing_service(c, s)
            if s["kind"] == "postgres":
                volumes[f"{host_name}-data"] = {}
            depends[host_name] = {"condition": "service_healthy"}
        if (c.get("telemetry") or {}).get("enabled", True):
            any_telemetry = True
            depends[COLLECTOR] = {"condition": "service_started"}
        if depends:
            svc["depends_on"] = depends
        if name in services:
            raise RuntimeFailure(f"component {name} clashes with a backing service of the same name")
        services[name] = svc

    if any_telemetry:
        services[COLLECTOR] = {
            "image": COLLECTOR_IMAGE,
            "command": ["--config=/etc/otelcol/rlp.yaml"],
            "volumes": ["./otel-collector.yaml:/etc/otelcol/rlp.yaml:ro"],
            "ports": ["127.0.0.1:4317:4317", "127.0.0.1:4318:4318"],
            "restart": "unless-stopped",
        }
        files["otel-collector.yaml"] = COLLECTOR_CONFIG
    if any_localstack:
        services[LOCALSTACK] = {
            "image": LOCALSTACK_IMAGE,
            "environment": {
                "SERVICES": "s3,sqs,sns",
                "AWS_DEFAULT_REGION": AWS_REGION,
                "LOCALSTACK_AUTH_TOKEN": "${LOCALSTACK_AUTH_TOKEN:-}",
            },
            "volumes": ["./localstack/ready.d:/etc/localstack/init/ready.d:ro"],
            "ports": ["127.0.0.1:4566:4566"],
            "healthcheck": {
                "test": ["CMD-SHELL", "curl -sf http://localhost:4566/_localstack/init/ready | grep -Eq '\"completed\": ?true'"],
                "interval": "2s",
                "timeout": "3s",
                "retries": 60,
            },
        }
        files["localstack/ready.d/rlp-init.sh"] = localstack_init(doc)

    compose: dict = {"name": project_name(doc), "services": services}
    if volumes:
        compose["volumes"] = volumes
    env = {"COMPOSE_PROJECT_NAME": project_name(doc), **(credentials or {})}
    return Plan(project_name(doc), compose, env, endpoints, files, extract, secret_values)


# --- writing and Docker ---


def write(root: Path, p: Plan) -> list[Path]:
    rlp_dir = root / RLP_DIR
    rlp_dir.mkdir(parents=True, exist_ok=True)
    written = []
    compose_file = rlp_dir / "compose.yaml"
    header = "# Generated by `rlp run` from rlp.yaml. Do not edit; it is rewritten on every run.\n"
    compose_file.write_text(header + yaml.safe_dump(p.compose, sort_keys=False), encoding="utf-8")
    written.append(compose_file)
    write_env_file(rlp_dir / ".env", p.env, "Generated by `rlp run`: Compose project and local credentials.")
    written.append(rlp_dir / ".env")
    for component, values in p.secret_files.items():
        path = secrets_path(root, component)
        write_env_file(path, values, f"Secrets for {component}. Local only; never commit this file.")
        written.append(path)
    for relpath, content in p.files.items():
        path = rlp_dir.joinpath(*PurePosixPath(relpath).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        if relpath.endswith(".sh"):
            path.chmod(0o755)
        written.append(path)
    for target, archive in p.extract.items():
        if not archive.is_file():
            raise RuntimeFailure(f"{archive} does not exist; package the component first (rlp package)")
        dest = rlp_dir.joinpath(*PurePosixPath(target).parts)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                resolved = (dest / member).resolve()
                if not resolved.is_relative_to(dest.resolve()):
                    raise RuntimeFailure(f"{archive}: entry {member!r} escapes the extraction directory")
            zf.extractall(dest)
        written.append(dest)
    return written


def unignored(root: Path, paths: list[Path]) -> list[Path]:
    """Files git would track, for a warning. Empty outside a git work tree."""
    if shutil.which("git") is None:
        return []
    out = []
    for path in paths:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)], cwd=root, capture_output=True, check=False
        )
        if result.returncode == 1:
            out.append(path)
        elif result.returncode == 128:  # not a git repo
            return []
    return out


def docker() -> str:
    exe = shutil.which("docker")
    if exe is None:
        raise RuntimeFailure("docker not found on PATH; install Docker (with Compose v2), or use --native")
    return exe


def compose_cmd(root: Path, p_name: str) -> list[str]:
    return [
        docker(), "compose",
        "--project-name", p_name,
        "--project-directory", str(root / RLP_DIR),
        "--file", str(root / RLP_DIR / "compose.yaml"),
        "--env-file", str(root / RLP_DIR / ".env"),
    ]


def up(root: Path, p: Plan, stdout=None, stderr=None) -> int:
    cmd = [*compose_cmd(root, p.project), "up", "--detach", "--build", "--remove-orphans"]
    return subprocess.run(cmd, cwd=root, stdout=stdout, stderr=stderr, check=False).returncode


def down(root: Path, project: str, volumes: bool, stdout=None, stderr=None) -> int:
    if not (root / RLP_DIR / "compose.yaml").is_file():
        raise RuntimeFailure(f"{root / RLP_DIR / 'compose.yaml'} does not exist; nothing to take down")
    cmd = [*compose_cmd(root, project), "down", "--remove-orphans"]
    if volumes:
        cmd.append("--volumes")
    return subprocess.run(cmd, cwd=root, stdout=stdout, stderr=stderr, check=False).returncode


# --- smoke ---


@dataclass
class Check:
    component: str
    kind: str
    target: str
    ok: bool = False
    status: str = "pending"

    def as_dict(self) -> dict:
        return {"component": self.component, "kind": self.kind, "target": self.target, "ok": self.ok, "status": self.status}


def http_status(url: str, method: str = "GET", body: bytes | None = None, timeout: float = 3.0) -> tuple[int | None, dict]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        path = parts.path or "/"
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}
    except OSError:
        return None, {}
    finally:
        conn.close()


def job_state(root: Path, project: str, service: str) -> tuple[str, int | None]:
    cmd = [*compose_cmd(root, project), "ps", "--all", "--format", "json", service]
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return "missing", None
    text = result.stdout.strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows:
        return "missing", None
    row = rows[0]
    return str(row.get("State", "")).lower(), row.get("ExitCode")


def check_once(root: Path, project: str, endpoint: Endpoint) -> list[Check]:
    if endpoint.kind == "http":
        checks = []
        for path in endpoint.paths or [""]:
            target = endpoint.url + path
            code, _ = http_status(target)
            if not endpoint.paths:  # nothing declared: any answer below 500 means it is up
                ok = code is not None and code < 500
            else:
                ok = code == 200
            checks.append(Check(endpoint.component, "http", target or endpoint.url, ok, str(code) if code else "no answer"))
        return checks
    if endpoint.kind == "invoke":
        code, headers = http_status(endpoint.url, "POST", b"{}")
        if code == 200 and "x-amz-function-error" in headers:
            return [Check(endpoint.component, "invoke", endpoint.url, False, f"function error ({headers['x-amz-function-error']})")]
        return [Check(endpoint.component, "invoke", endpoint.url, code == 200, str(code) if code else "no answer")]
    state, exit_code = job_state(root, project, endpoint.service)
    if state == "exited":
        return [Check(endpoint.component, "job", endpoint.service, exit_code == 0, f"exited {exit_code}")]
    return [Check(endpoint.component, "job", endpoint.service, False, state or "unknown")]


def finished(checks: list[Check]) -> bool:
    """Done polling: all passed, or a job already failed for good."""
    if all(c.ok for c in checks):
        return True
    return any(c.kind == "job" and c.status.startswith("exited") and not c.ok for c in checks)


def smoke(root: Path, p: Plan, timeout: float, interval: float = 1.0, only: tuple[str, ...] = ()) -> list[Check]:
    endpoints = [e for e in p.endpoints if not only or e.component in only]
    deadline = time.monotonic() + timeout
    pause = threading.Event()  # never set; wait() is the delay between polls
    while True:
        checks = [c for e in endpoints for c in check_once(root, p.project, e)]
        if finished(checks) or time.monotonic() >= deadline:
            return checks
        pause.wait(interval)

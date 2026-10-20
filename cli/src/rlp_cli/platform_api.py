"""A thin typed client for the platform API (/api/v1), and where the CLI finds it.

The platform's URL and token come from, in order:

1. RLP_PLATFORM_URL and RLP_PLATFORM_TOKEN
2. the config file: $XDG_CONFIG_HOME/rlp/config.yaml, else ~/.config/rlp/config.yaml,
   with `platform.url` and `platform.token`

Each value is looked up on its own, so the environment can override just one.
The file holds a secret, so on POSIX a token is read from it only when the file
is private to its owner (mode 600); `rlp bootstrap` writes it that way.

The API's own spec is platform/openapi.json; this client covers what the CLI
uses and returns the JSON documents as typed dicts. No generated code.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import quote

import httpx
import yaml

API_PREFIX = "/api/v1"
URL_ENV = "RLP_PLATFORM_URL"
TOKEN_ENV = "RLP_PLATFORM_TOKEN"
TIMEOUT = 30.0


class PlatformError(RuntimeError):
    """The platform could not be reached, or answered with an error."""

    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ConfigError(PlatformError):
    """The CLI doesn't know where the platform is, or can't use the config file."""


# --- response documents (see platform/openapi.json) ---


class Project(TypedDict):
    name: str
    description: str
    owner: str
    phase: str
    dashboard_url: str | None
    created_at: str
    updated_at: str


class Repo(TypedDict):
    full_name: str
    project: str
    default_branch: str
    template_source: str | None
    template_version: str | None
    last_indexed_commit: str | None
    created_at: str
    updated_at: str


class Component(TypedDict):
    repo: str
    name: str
    language: str
    artifact_kind: str
    runtime_kind: str


class RuleResult(TypedDict):
    rule: str
    description: str
    passed: bool
    messages: list[str]


class Transition(TypedDict):
    from_phase: str | None
    to_phase: str
    actor: str
    reason: str
    rule_results: list[RuleResult]
    at: str


class Rejection(TypedDict):
    detail: str
    project: str
    from_phase: str
    to_phase: str
    failures: list[RuleResult]
    results: list[RuleResult]


class TransitionRejected(PlatformError):
    """The lifecycle refused a move: at least one entry rule failed. Nothing changed."""

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(rejection["detail"], status=409, body=rejection)
        self.rejection = rejection

    @property
    def failures(self) -> list[RuleResult]:
        return self.rejection["failures"]


# --- configuration ---


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rlp" / "config.yaml"


@dataclass(frozen=True)
class Config:
    """Where the platform is. `sources` says where each value came from: an env var, the file, or unset."""

    url: str | None
    token: str | None = field(repr=False)
    sources: dict[str, str]
    path: Path

    def require(self) -> tuple[str, str]:
        if not self.url:
            raise ConfigError(f"no platform URL: set {URL_ENV} or platform.url in {self.path}")
        if not self.token:
            raise ConfigError(f"no platform token: set {TOKEN_ENV} or platform.token in {self.path}")
        return self.url, self.token


def mask(token: str | None) -> str | None:
    """Enough of a token to tell two apart, never enough to use: the last 4 characters of a long one."""
    if not token:
        return None
    return "****" + token[-4:] if len(token) >= 16 else "****"


def _read_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise ConfigError(f"{path} is not valid YAML: {err}") from err
    platform = doc.get("platform") if isinstance(doc, dict) else None
    if platform is None:
        return {}
    if not isinstance(platform, dict):
        raise ConfigError(f"{path}: platform must be a mapping with url and token")
    return platform


def _private(path: Path) -> bool:
    if os.name != "posix":
        return True  # Windows has no mode bits to check
    return not stat.S_IMODE(path.stat().st_mode) & 0o077


def load_config(environ: Mapping[str, str] | None = None, path: Path | None = None) -> Config:
    environ = os.environ if environ is None else environ
    path = path or config_path(environ)
    from_file = _read_file(path)
    values: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for key, env in (("url", URL_ENV), ("token", TOKEN_ENV)):
        if environ.get(env):
            values[key], sources[key] = environ[env], f"env {env}"
        elif from_file.get(key):
            if key == "token" and not _private(path):
                raise ConfigError(f"{path} holds a token but others can read it; run: chmod 600 {path}")
            values[key], sources[key] = str(from_file[key]), f"file {path}"
        else:
            values[key], sources[key] = None, "unset"
    return Config(url=values["url"], token=values["token"], sources=sources, path=path)


# --- the client ---


def _segment(value: str) -> str:
    return quote(value, safe="")


def _repo_path(full_name: str) -> str:
    owner, sep, name = full_name.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise PlatformError(f"a repo is OWNER/NAME, got {full_name!r}")
    return f"/repos/{_segment(owner)}/{_segment(name)}"


class PlatformClient:
    """Calls /api/v1 with the bearer token. Pass `transport` to talk to an app in-process (tests)."""

    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None, timeout: float = TIMEOUT):
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.url + API_PREFIX,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    @classmethod
    def from_config(cls, config: Config, **kwargs: Any) -> PlatformClient:
        url, token = config.require()
        return cls(url, token, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PlatformClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise PlatformError(f"cannot reach the platform at {self.url}: {err}") from err
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if 200 <= resp.status_code < 300:
            return body
        if resp.status_code == 401:
            raise PlatformError("the platform refused the token (401): check RLP_PLATFORM_TOKEN", 401, body)
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str):
            detail = str(detail or body or resp.reason_phrase)
        raise PlatformError(f"{detail} ({resp.status_code})", resp.status_code, body)

    # projects

    def list_projects(self, phase: str | None = None) -> list[Project]:
        return self._call("GET", "/projects", params={"phase": phase} if phase else None)

    def get_project(self, name: str) -> Project:
        return self._call("GET", f"/projects/{_segment(name)}")

    def create_project(
        self, name: str, *, owner: str = "", description: str = "", dashboard_url: str | None = None
    ) -> Project:
        body = {"name": name, "owner": owner, "description": description, "dashboard_url": dashboard_url}
        return self._call("POST", "/projects", json=body)

    def update_project(self, name: str, **fields: str | None) -> Project:
        """Change owner, description or dashboard_url; fields not passed keep their value."""
        return self._call("PATCH", f"/projects/{_segment(name)}", json=fields)

    # repos and components

    def list_repos(self, project: str | None = None) -> list[Repo]:
        return self._call("GET", "/repos", params={"project": project} if project else None)

    def get_repo(self, full_name: str) -> Repo:
        return self._call("GET", _repo_path(full_name))

    def list_repo_components(self, full_name: str) -> list[Component]:
        return self._call("GET", _repo_path(full_name) + "/components")

    # lifecycle

    def list_transitions(self, project: str) -> list[Transition]:
        return self._call("GET", f"/projects/{_segment(project)}/transitions")

    def move(self, project: str, to: str, *, reason: str = "", actor: str = "rlp-cli") -> Transition:
        """Move the project; raises TransitionRejected with the failing rules when an entry rule fails."""
        try:
            return self._call(
                "POST", f"/projects/{_segment(project)}/transitions", json={"to": to, "reason": reason, "actor": actor}
            )
        except PlatformError as err:
            if err.status == 409 and isinstance(err.body, dict) and "failures" in err.body:
                raise TransitionRejected(cast(Rejection, err.body)) from err
            raise

"""The FastAPI application: health endpoints, the /api/v1 routes (rlp_platform.api) and one sample route."""

from __future__ import annotations

import json
import logging
import socket
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response, status

from rlp_platform import __version__, api, telemetry
from rlp_platform.config import Settings

log = logging.getLogger(__name__)

DEFAULT_PORTS = {"postgres": 5432, "postgresql": 5432}

router = APIRouter()


def database_reachable(url: str, timeout: float = 1.0) -> bool:
    """True when the database host accepts a TCP connection. No driver needed."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    if not parts.hostname:
        return False
    try:
        port = parts.port or DEFAULT_PORTS.get(scheme, 5432)
    except ValueError:
        return False
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: every configured dependency is reachable."""
    settings: Settings = request.app.state.settings
    checks: dict[str, str] = {}
    if settings.database_url:
        checks["database"] = "ok" if database_reachable(settings.database_url) else "unreachable"
    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("not ready", extra={"checks": checks})
    return {"status": "ok" if ready else "unavailable", "checks": checks}


@router.get("/hello/{name}")
def hello(name: str) -> dict[str, str]:
    """Sample route."""
    log.info("greeting", extra={"name_length": len(name)})
    return {"message": f"hello, {name}"}


def create_app(settings: Settings | None = None, *, opa_path: str | None = None) -> FastAPI:
    """The service. `opa_path` is the opa binary the lifecycle runs; the default is the one on PATH."""
    app = FastAPI(title="rlp-platform", version=__version__)
    app.state.settings = settings or Settings.from_env()
    app.state.session_factory = api.make_session_factory(app.state.settings)
    app.state.opa_path = opa_path
    app.state.github_transport = None  # tests answer GitHub calls from recorded responses
    app.include_router(router)
    app.include_router(api.router)
    app.include_router(api.hooks)
    telemetry.setup(app)
    return app


def openapi_json(app: FastAPI) -> str:
    """The app's OpenAPI document, formatted as platform/openapi.json is committed."""
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"

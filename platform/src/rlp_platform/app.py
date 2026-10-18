"""The FastAPI application: health endpoints and one sample route."""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response, status

from rlp_platform import __version__, telemetry
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


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rlp-platform", version=__version__)
    app.state.settings = settings or Settings.from_env()
    app.include_router(router)
    telemetry.setup(app)
    return app

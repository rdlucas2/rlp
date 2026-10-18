"""Settings, read only from environment variables (declared in rlp.yaml under config)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


class ConfigError(ValueError):
    """An environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    log_level: str
    database_url: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        raw_port = env.get("PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError as err:
            raise ConfigError(f"PORT must be an integer, got {raw_port!r}") from err
        if not 1 <= port <= 65535:
            raise ConfigError(f"PORT must be between 1 and 65535, got {port}")
        log_level = env.get("LOG_LEVEL", "info").lower()
        if log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")
        return cls(
            # Loopback by default; the container image sets HOST so the service is reachable there.
            host=env.get("HOST", "127.0.0.1"),
            port=port,
            log_level=log_level,
            database_url=env.get("DATABASE_URL") or None,
        )

"""Settings, read only from environment variables (declared in rlp.yaml under config)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
BOOLEANS = {"true": True, "false": False}


class ConfigError(ValueError):
    """An environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    log_level: str
    database_url: str | None
    migrate_on_start: bool = False
    api_token: str | None = field(default=None, repr=False)
    api_token_disabled: bool = False

    def check_api_auth(self) -> None:
        """Refuse to serve an open API: RLP_API_TOKEN must be set unless RLP_API_TOKEN_DISABLED=true."""
        if not self.api_token and not self.api_token_disabled:
            raise ConfigError(
                "RLP_API_TOKEN is not set; set it, or set RLP_API_TOKEN_DISABLED=true for local development"
            )

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
        raw_migrate = env.get("RLP_MIGRATE_ON_START", "false").strip().lower()
        if raw_migrate not in BOOLEANS:
            raise ConfigError(f"RLP_MIGRATE_ON_START must be true or false, got {raw_migrate!r}")
        migrate_on_start = BOOLEANS[raw_migrate]
        raw_disabled = env.get("RLP_API_TOKEN_DISABLED", "false").strip().lower()
        if raw_disabled not in BOOLEANS:
            raise ConfigError(f"RLP_API_TOKEN_DISABLED must be true or false, got {raw_disabled!r}")
        database_url = env.get("DATABASE_URL") or None
        if migrate_on_start and database_url is None:
            raise ConfigError("RLP_MIGRATE_ON_START=true needs DATABASE_URL")
        return cls(
            # Loopback by default; the container image sets HOST so the service is reachable there.
            host=env.get("HOST", "127.0.0.1"),
            port=port,
            log_level=log_level,
            database_url=database_url,
            migrate_on_start=migrate_on_start,
            api_token=env.get("RLP_API_TOKEN") or None,
            api_token_disabled=BOOLEANS[raw_disabled],
        )

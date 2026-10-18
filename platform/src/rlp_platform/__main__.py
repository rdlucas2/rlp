"""Start the service: python -m rlp_platform."""

from __future__ import annotations

import uvicorn

from rlp_platform import logs
from rlp_platform.app import create_app
from rlp_platform.config import Settings


def main() -> None:
    settings = Settings.from_env()
    logs.configure(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep the JSON handler from logs.configure
        log_level=settings.log_level,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()

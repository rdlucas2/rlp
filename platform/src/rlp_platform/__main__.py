"""Start the service, or manage its database.

python -m rlp_platform                     start the service
python -m rlp_platform migrate             apply pending migrations ($DATABASE_URL)
python -m rlp_platform migrate --check     also fail if the models and migrations disagree
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from rlp_platform import db, logs
from rlp_platform.app import create_app
from rlp_platform.config import Settings

log = logging.getLogger("rlp_platform")


def serve(settings: Settings) -> int:
    if settings.migrate_on_start and settings.database_url:
        log.info("applying pending migrations (RLP_MIGRATE_ON_START=true)")
        db.upgrade(settings.database_url)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep the JSON handler from logs.configure
        log_level=settings.log_level,
        proxy_headers=True,
    )
    return 0


def migrate(settings: Settings, check: bool) -> int:
    if not settings.database_url:
        log.error("DATABASE_URL is not set")
        return 2
    if check:
        try:
            db.check(settings.database_url)
        except db.MigrationDrift as err:
            log.error(str(err))
            return 1
        log.info("database at head; models and migrations agree")
    else:
        db.upgrade(settings.database_url)
        log.info("database at head")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command in `argv` (the arguments after the module name); none starts the service."""
    parser = argparse.ArgumentParser(prog="python -m rlp_platform", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command")
    migrate_parser = commands.add_parser("migrate", help="apply pending migrations to $DATABASE_URL")
    migrate_parser.add_argument(
        "--check", action="store_true", help="then fail if the models describe a schema the migrations don't build"
    )
    args = parser.parse_args([] if argv is None else argv)
    settings = Settings.from_env()
    logs.configure(settings.log_level)
    if args.command == "migrate":
        return migrate(settings, args.check)
    return serve(settings)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

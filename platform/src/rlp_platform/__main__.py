"""Start the service, or manage its database.

python -m rlp_platform                     start the service
python -m rlp_platform migrate             apply pending migrations ($DATABASE_URL)
python -m rlp_platform migrate --check     also fail if the models and migrations disagree
python -m rlp_platform openapi             print the OpenAPI spec (platform/openapi.json)
python -m rlp_platform sync [OWNER/NAME]   re-index repos from GitHub (RLP_GITHUB_REPOS, else the installation's)
python -m rlp_platform expire-artifacts    delete artifacts past their expires_at from GHCR and GitHub releases
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import uvicorn

from rlp_platform import db, indexer, logs, retirement
from rlp_platform.app import create_app, openapi_json
from rlp_platform.config import REPO_NAME, ConfigError, Settings
from rlp_platform.github import GitHub, GitHubError
from rlp_platform.store import session_factory

log = logging.getLogger("rlp_platform")


def serve(settings: Settings) -> int:
    try:
        settings.check_api_auth()
    except ConfigError as err:
        log.error(str(err))
        return 2
    if settings.api_token_disabled:
        log.warning("the API is open: RLP_API_TOKEN_DISABLED=true; use this only for local development")
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


def sync(settings: Settings, repo: str | None, transport=None) -> int:
    """Re-index `repo`, or every repo the platform can see; print the results as one JSON line. Exit 1 if any failed."""
    if not settings.database_url:
        log.error("DATABASE_URL is not set")
        return 2
    if repo is not None and not REPO_NAME.match(repo):
        log.error(f"a repo is OWNER/NAME, got {repo!r}")
        return 2
    factory = session_factory(settings.database_url)
    with GitHub(settings.github_api_url, settings.github_token, transport=transport) as gh, factory() as session:
        try:
            names = [repo] if repo else indexer.sync_targets(gh, settings.github_repos)
        except GitHubError as err:
            log.error(str(err))
            return 2
        results = indexer.sync(session, gh, names)
    ok = all(r.ok for r in results)
    # One line, so it reads apart from the JSON log lines that share stdout.
    sys.stdout.write(json.dumps({"ok": ok, "results": [r.as_dict() for r in results]}) + "\n")
    return 0 if ok else 1


def expire_artifacts(settings: Settings, transport=None) -> int:
    """Delete expired artifacts and mark them expired; print the results as one JSON line. Exit 1 if any failed."""
    if not settings.database_url:
        log.error("DATABASE_URL is not set")
        return 2
    factory = session_factory(settings.database_url)
    with GitHub(settings.github_api_url, settings.github_token, transport=transport) as gh, factory() as session:
        results = retirement.expire_artifacts(session, retirement.GitHubRegistry(gh), commit=session.commit)
        session.commit()
    ok = all(r.status != "failed" for r in results)
    sys.stdout.write(json.dumps({"ok": ok, "results": [r.as_dict() for r in results]}) + "\n")
    return 0 if ok else 1


def openapi() -> int:
    """Print the API's OpenAPI document as platform/openapi.json holds it."""
    sys.stdout.write(openapi_json(create_app(Settings.from_env({}))))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command in `argv` (the arguments after the module name); none starts the service."""
    parser = argparse.ArgumentParser(prog="python -m rlp_platform", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command")
    migrate_parser = commands.add_parser("migrate", help="apply pending migrations to $DATABASE_URL")
    migrate_parser.add_argument(
        "--check", action="store_true", help="then fail if the models describe a schema the migrations don't build"
    )
    commands.add_parser("openapi", help="print the OpenAPI spec; platform/openapi.json is this output")
    sync_parser = commands.add_parser("sync", help="re-index repos from GitHub at their default branch's head")
    sync_parser.add_argument("repo", nargs="?", metavar="OWNER/NAME", help="only this repo")
    commands.add_parser(
        "expire-artifacts", help="delete artifacts past their expires_at from their registry; safe to run repeatedly"
    )
    args = parser.parse_args([] if argv is None else argv)
    if args.command == "openapi":
        return openapi()
    settings = Settings.from_env()
    logs.configure(settings.log_level)
    if args.command == "migrate":
        return migrate(settings, args.check)
    if args.command == "sync":
        return sync(settings, args.repo)
    if args.command == "expire-artifacts":
        return expire_artifacts(settings)
    return serve(settings)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

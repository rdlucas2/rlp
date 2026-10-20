"""Database engine and migrations: Alembic, driven from code so the service and the CLI share it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.engine import make_url

from rlp_platform.models import Base

_PACKAGE = Path(__file__).resolve().parent
# The wheel ships the migrations inside the package; a source checkout keeps them in platform/migrations/.
_CANDIDATES = (_PACKAGE / "migrations", _PACKAGE.parents[1] / "migrations")


class MigrationDrift(RuntimeError):
    """The models and the migrations disagree: a model changed without a migration."""


def migrations_dir() -> Path:
    for candidate in _CANDIDATES:
        if (candidate / "env.py").is_file():
            return candidate
    raise FileNotFoundError(f"no Alembic migrations in {', '.join(str(c) for c in _CANDIDATES)}")


def engine_url(url: str) -> str:
    """Use psycopg 3 for a plain postgres:// or postgresql:// URL, as DATABASE_URL usually is."""
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def make_engine(url: str) -> Engine:
    return create_engine(engine_url(url), pool_pre_ping=True)


@contextmanager
def _alembic(url: str) -> Iterator[Config]:
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            config = Config()
            config.set_main_option("script_location", str(migrations_dir()))
            config.attributes["connection"] = connection
            yield config
    finally:
        engine.dispose()


def upgrade(url: str, revision: str = "head") -> None:
    """Apply every pending migration up to `revision`. A database already there is left alone."""
    with _alembic(url) as config:
        command.upgrade(config, revision)


def downgrade(url: str, revision: str) -> None:
    with _alembic(url) as config:
        command.downgrade(config, revision)


def drift(url: str, metadata: MetaData = Base.metadata) -> list[object]:
    """Differences between `metadata` and the schema the migrations built at `url`."""
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return list(compare_metadata(context, metadata))
    finally:
        engine.dispose()


def check(url: str, metadata: MetaData = Base.metadata) -> None:
    """Migrate `url` to head, then fail if the models describe a schema the migrations don't build."""
    upgrade(url)
    diffs = drift(url, metadata)
    if diffs:
        lines = "\n".join(f"  {d}" for d in diffs)
        raise MigrationDrift(f"the models and migrations disagree; add a migration for:\n{lines}")

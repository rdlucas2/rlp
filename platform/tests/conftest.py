from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rlp_platform import db, opa
from rlp_platform.app import create_app
from rlp_platform.config import Settings


@pytest.fixture(autouse=True)
def quiet_telemetry(monkeypatch):
    """Tests never export spans, whatever the environment says."""
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env({})


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture(scope="session")
def postgres_url():
    """A Postgres 17 server for the database tests.

    testcontainers starts one in Docker (CI runners have it). Set
    RLP_TEST_DATABASE_URL to use a server you already run instead; the tests
    drop and recreate its public schema.
    """
    url = os.environ.get("RLP_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture
def empty_db(postgres_url):
    """The test server with an empty public schema, so every test migrates from nothing."""
    engine = sa.create_engine(db.engine_url(postgres_url))
    with engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    engine.dispose()
    return postgres_url


@pytest.fixture
def migrated_db(empty_db):
    db.upgrade(empty_db)
    return empty_db


@pytest.fixture
def session(migrated_db):
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture(scope="session")
def opa_path(tmp_path_factory) -> str:
    """The opa binary the lifecycle tests run: the one on PATH, else the pinned one, downloaded for this session."""
    try:
        return opa.find()
    except opa.OpaError:
        exe = ".exe" if os.name == "nt" else ""
        return str(opa.install(tmp_path_factory.mktemp("opa") / f"opa{exe}"))

from __future__ import annotations

import os
from pathlib import Path

import httpx
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


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"


class FakeGitHub:
    """The GitHub REST calls the indexer makes, answered from recorded responses. No network.

    `repos` maps OWNER/NAME to its default branch, head commit and files at each commit.
    Repos in `broken` answer 500 to everything. `requests` records every call.
    """

    def __init__(self) -> None:
        self.repos: dict[str, dict] = {}
        self.broken: set[str] = set()
        self.installation: list[str] = []
        self.requests: list[httpx.Request] = []

    def add(self, full_name: str, commit: str, files: dict[str, str], *, branch: str = "main") -> None:
        repo = self.repos.setdefault(full_name, {"default_branch": branch, "files": {}})
        repo["default_branch"], repo["head"] = branch, commit
        repo["files"][commit] = files

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if parts == ["installation", "repositories"]:
            page, per_page = int(request.url.params["page"]), int(request.url.params["per_page"])
            names = self.installation[(page - 1) * per_page : page * per_page]
            return httpx.Response(
                200,
                json={"total_count": len(self.installation), "repositories": [{"full_name": n} for n in names]},
            )
        if parts[0] != "repos" or len(parts) < 3:
            return httpx.Response(404, json={"message": "Not Found"})
        full_name = f"{parts[1]}/{parts[2]}"
        if full_name in self.broken:
            return httpx.Response(500, json={"message": "Server Error"})
        repo = self.repos.get(full_name)
        if repo is None:
            return httpx.Response(404, json={"message": "Not Found"})
        rest = parts[3:]
        if not rest:
            return httpx.Response(200, json={"full_name": full_name, "default_branch": repo["default_branch"]})
        if rest[0] == "branches" and len(rest) == 2:
            if rest[1] != repo["default_branch"]:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": rest[1], "commit": {"sha": repo["head"]}})
        if rest[0] == "contents":
            assert request.headers["accept"] == "application/vnd.github.raw+json"
            text = repo["files"].get(request.url.params["ref"], {}).get("/".join(rest[1:]))
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, text=text)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def platform_contract() -> dict[str, str]:
    """This repo's platform component as a repo rendered from the template: platform/rlp.yaml and its answers."""
    platform = Path(__file__).resolve().parents[1]
    return {
        "rlp.yaml": (platform / "rlp.yaml").read_text(encoding="utf-8"),
        ".copier-answers.yml": (platform / ".copier-answers.yml").read_text(encoding="utf-8"),
    }

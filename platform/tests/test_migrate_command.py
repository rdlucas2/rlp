from __future__ import annotations

import json

import pytest

import rlp_platform.__main__ as entrypoint
from rlp_platform import db


def last_log(capsys) -> dict[str, object]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_migrate_needs_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert entrypoint.main(["migrate"]) == 2
    assert last_log(capsys)["message"] == "DATABASE_URL is not set"


def test_migrate_upgrades_to_head(monkeypatch, capsys, empty_db):
    monkeypatch.setenv("DATABASE_URL", empty_db)
    assert entrypoint.main(["migrate"]) == 0
    assert db.drift(empty_db) == []
    assert entrypoint.main(["migrate"]) == 0
    assert last_log(capsys)["message"] == "database at head"


def test_migrate_check_passes_when_models_and_migrations_agree(monkeypatch, capsys, empty_db):
    monkeypatch.setenv("DATABASE_URL", empty_db)
    assert entrypoint.main(["migrate", "--check"]) == 0
    assert "agree" in last_log(capsys)["message"]


def test_migrate_check_fails_on_drift(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/app")

    def drifted(url):
        raise db.MigrationDrift("the models and migrations disagree")

    monkeypatch.setattr(entrypoint.db, "check", drifted)
    assert entrypoint.main(["migrate", "--check"]) == 1
    assert last_log(capsys)["level"] == "error"


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        entrypoint.main(["nope"])


@pytest.fixture
def no_server(monkeypatch):
    calls = {}
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.update(app=app, **kwargs))
    return calls


def test_start_migrates_when_asked(monkeypatch, no_server):
    upgraded = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/app")
    monkeypatch.setenv("RLP_MIGRATE_ON_START", "true")
    monkeypatch.setattr(entrypoint.db, "upgrade", upgraded.append)
    assert entrypoint.main([]) == 0
    assert upgraded == ["postgresql://db/app"]
    assert no_server["app"].title == "rlp-platform"


def test_start_leaves_the_database_alone_by_default(monkeypatch, no_server):
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/app")
    monkeypatch.delenv("RLP_MIGRATE_ON_START", raising=False)
    monkeypatch.setattr(entrypoint.db, "upgrade", lambda url: pytest.fail("migrated without being asked"))
    assert entrypoint.main([]) == 0
    assert "app" in no_server

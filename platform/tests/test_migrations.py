from __future__ import annotations

import pytest
import sqlalchemy as sa

from rlp_platform import db
from rlp_platform.models import Base

TABLES = {
    "projects",
    "repos",
    "components",
    "artifacts",
    "builds",
    "deployments",
    "lifecycle_transitions",
}


def schema(url: str) -> dict[str, object]:
    """Tables, columns, constraints and indexes: enough to tell whether a migration changed anything."""
    engine = sa.create_engine(db.engine_url(url))
    try:
        inspector = sa.inspect(engine)
        with engine.connect() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
        return {
            "version": version,
            "tables": {
                name: {
                    "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(name)],
                    "unique": sorted(u["name"] for u in inspector.get_unique_constraints(name)),
                    "foreign_keys": sorted(f["name"] for f in inspector.get_foreign_keys(name)),
                    "indexes": sorted(i["name"] for i in inspector.get_indexes(name)),
                }
                for name in inspector.get_table_names()
            },
        }
    finally:
        engine.dispose()


def test_migrate_creates_every_table(migrated_db):
    tables = set(schema(migrated_db)["tables"])
    assert tables == TABLES | {"alembic_version"}


def test_migrate_twice_changes_nothing(migrated_db):
    before = schema(migrated_db)
    db.upgrade(migrated_db)
    assert schema(migrated_db) == before


def test_downgrade_to_base_and_up_again(migrated_db):
    before = schema(migrated_db)
    db.downgrade(migrated_db, "base")
    assert set(schema(migrated_db)["tables"]) == {"alembic_version"}
    db.upgrade(migrated_db)
    assert schema(migrated_db) == before


def columns(url: str, table: str) -> list[str]:
    return [c[0] for c in schema(url)["tables"][table]["columns"]]


def test_downgrade_below_0002_and_up_again(migrated_db):
    before = schema(migrated_db)
    db.downgrade(migrated_db, "0001")
    assert schema(migrated_db)["version"] == ["0001"]
    assert "index_error" not in columns(migrated_db, "repos")
    assert "removed_at" not in columns(migrated_db, "components")
    db.upgrade(migrated_db)
    assert schema(migrated_db) == before
    assert "index_error" in columns(migrated_db, "repos")
    assert "removed_at" in columns(migrated_db, "components")


def test_downgrade_one_step_and_up_again(migrated_db):
    before = schema(migrated_db)
    db.downgrade(migrated_db, "-1")
    assert schema(migrated_db)["version"] == ["0002"]
    assert {"repo_full_name", "run_id", "run_attempt"}.isdisjoint(columns(migrated_db, "builds"))
    assert {"build_id", "component_name"}.isdisjoint(columns(migrated_db, "artifacts"))
    db.upgrade(migrated_db)
    assert schema(migrated_db) == before
    assert "uq_builds_repo_commit_run" in schema(migrated_db)["tables"]["builds"]["unique"]
    assert "uq_deployments_component_env_artifact_at" in schema(migrated_db)["tables"]["deployments"]["unique"]


def test_0003_keeps_existing_builds_and_artifacts(empty_db):
    """Rows written before 0003 get their repo's name, a run of their own and their component's name."""
    db.upgrade(empty_db, "0002")
    engine = sa.create_engine(db.engine_url(empty_db))
    with engine.begin() as connection:
        run = connection.execute
        run(sa.text("INSERT INTO projects (id, name, owner, phase) VALUES (1, 'orders', '', 'proposed')"))
        run(sa.text("INSERT INTO repos (id, full_name, project_id) VALUES (1, 'acme/orders-api', 1)"))
        run(
            sa.text(
                "INSERT INTO components (id, repo_id, name, language, artifact_kind, runtime_kind) "
                "VALUES (1, 1, 'api', 'python', 'container-image', 'container-service')"
            )
        )
        for build_id in (1, 2):
            run(
                sa.text(
                    "INSERT INTO builds (id, repo_id, commit, branch, stages, passed, started_at) "
                    "VALUES (:id, 1, 'abc', 'main', '{}', true, now())"
                ),
                {"id": build_id},
            )
        run(sa.text("INSERT INTO artifacts (id, component_id, kind, reference) VALUES (1, 1, 'zip', 'a.zip')"))
    db.upgrade(empty_db)
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT repo_full_name, run_id, run_attempt FROM builds ORDER BY id")).all()
        names = connection.execute(sa.text("SELECT component_name FROM artifacts")).scalars().all()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("acme/orders-api", "build-1", 1), ("acme/orders-api", "build-2", 1)]
    assert names == ["api"]


def test_phase_is_a_postgres_enum(migrated_db):
    engine = sa.create_engine(db.engine_url(migrated_db))
    with engine.connect() as connection:
        labels = connection.execute(
            sa.text("SELECT unnest(enum_range(NULL::project_phase))::text"),
        ).scalars()
        assert list(labels) == ["proposed", "designed", "scaffolded", "in-dev", "live", "deprecated", "retired"]
    engine.dispose()


def test_models_and_migrations_agree(empty_db):
    db.check(empty_db)
    assert db.drift(empty_db) == []


def changed_metadata() -> sa.MetaData:
    """The models plus one column that no migration adds."""
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    metadata.tables["projects"].append_column(sa.Column("tier", sa.String(20)))
    return metadata


def test_check_fails_when_a_model_changes_without_a_migration(empty_db):
    with pytest.raises(db.MigrationDrift, match=r"add_column.*tier"):
        db.check(empty_db, changed_metadata())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@db:5432/app", "postgresql+psycopg://u:p@db:5432/app"),
        ("postgres://u:p@db/app", "postgresql+psycopg://u:p@db/app"),
        ("postgresql+psycopg://u@db/app", "postgresql+psycopg://u@db/app"),
    ],
)
def test_engine_url_uses_psycopg(url, expected):
    assert db.engine_url(url) == expected


def test_migrations_dir_is_found():
    assert (db.migrations_dir() / "versions").is_dir()


def test_missing_migrations_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_CANDIDATES", (tmp_path / "nowhere",))
    with pytest.raises(FileNotFoundError, match="no Alembic migrations"):
        db.migrations_dir()

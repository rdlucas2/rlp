from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from rlp_platform import store
from rlp_platform.models import Phase, Project
from rlp_platform.store import artifacts, builds, components, deployments, projects, repos, transitions

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def project(session):
    return projects.create(session, name="rlp", owner="rdlucas2", description="the platform")


@pytest.fixture
def repo(session, project):
    return repos.create(
        session,
        full_name="rdlucas2/rlp",
        project_id=project.id,
        template_source="gh:rdlucas2/rlp-template-python-fastapi",
        template_version="d5f0037",
    )


@pytest.fixture
def component(session, repo):
    return components.create(
        session,
        repo_id=repo.id,
        name="platform",
        language="python",
        artifact_kind="container-image",
        runtime_kind="container-service",
    )


@pytest.fixture
def artifact(session, component):
    return artifacts.create(session, component_id=component.id, kind="container-image", reference="sha256:abc")


def test_project_defaults(session, project):
    assert project.id is not None
    assert project.phase is Phase.PROPOSED
    assert project.dashboard_url is None
    assert project.created_at is not None


def test_project_get_list_update(session, project):
    other = projects.create(session, name="api", owner="someone", phase=Phase.IN_DEV)
    assert projects.get(session, project.id) is project
    assert projects.get_by_name(session, "api") is other
    assert [p.name for p in projects.list_(session)] == ["api", "rlp"]
    assert projects.list_(session, phase=Phase.IN_DEV) == [other]
    projects.update(session, project, phase=Phase.LIVE, dashboard_url="https://grafana.example/d/rlp")
    session.expire_all()
    assert projects.get(session, project.id).phase is Phase.LIVE


def test_phase_is_stored_by_value(session):
    projects.create(session, name="p", owner="o", phase=Phase.IN_DEV)
    stored = session.execute(sa.text("SELECT phase::text FROM projects WHERE name = 'p'")).scalar_one()
    assert stored == "in-dev"


def test_missing_records_raise_not_found(session):
    with pytest.raises(store.NotFound, match="projects 999 not found"):
        projects.get(session, 999)
    with pytest.raises(store.NotFound, match="project 'nope'"):
        projects.get_by_name(session, "nope")
    with pytest.raises(store.NotFound, match="repo 'a/b'"):
        repos.get_by_full_name(session, "a/b")
    with pytest.raises(store.NotFound, match="component 'c'"):
        components.get_by_name(session, 1, "c")


def test_update_rejects_unknown_fields(session, project):
    with pytest.raises(store.UnknownField, match="name"):
        projects.update(session, project, name="renamed")


def test_project_names_are_unique(session, project):
    with pytest.raises(IntegrityError, match="uq_projects_name"):
        projects.create(session, name="rlp", owner="someone else")


def test_repo_get_list_update(session, project, repo):
    assert repo.default_branch == "main"
    assert repos.get(session, repo.id) is repo
    assert repos.get_by_full_name(session, "rdlucas2/rlp") is repo
    assert repos.list_(session, project_id=project.id) == [repo]
    assert repos.list_(session) == [repo]
    repos.update(session, repo, last_indexed_commit="8b833bf")
    assert repo.last_indexed_commit == "8b833bf"


def test_repo_full_names_are_unique(session, project, repo):
    with pytest.raises(IntegrityError, match="uq_repos_full_name"):
        repos.create(session, full_name="rdlucas2/rlp", project_id=project.id)


def test_repo_needs_an_existing_project(session):
    with pytest.raises(IntegrityError, match="fk_repos_project_id_projects"):
        repos.create(session, full_name="a/b", project_id=12345)


def test_deleting_a_project_with_repos_is_refused(session, project, repo):
    session.commit()
    session.delete(project)
    with pytest.raises(IntegrityError, match="fk_repos_project_id_projects"):
        session.flush()
    session.rollback()
    assert session.scalars(sa.select(Project)).one().name == "rlp"
    assert repos.get_by_full_name(session, "rdlucas2/rlp").project_id == project.id


def test_deleting_a_project_with_repos_is_refused_in_sql(session, project, repo):
    with pytest.raises(IntegrityError, match="fk_repos_project_id_projects"):
        session.execute(sa.text("DELETE FROM projects WHERE id = :id"), {"id": project.id})


def test_component_get_list_update(session, repo, component):
    assert components.get(session, component.id) is component
    assert components.get_by_name(session, repo.id, "platform") is component
    assert components.list_(session, repo_id=repo.id) == [component]
    assert components.list_(session) == [component]
    components.update(session, component, runtime_kind="function")
    assert component.runtime_kind == "function"


def test_component_names_are_unique_per_repo(session, project, repo, component):
    other = repos.create(session, full_name="rdlucas2/other", project_id=project.id)
    components.create(
        session, repo_id=other.id, name="platform", language="go", artifact_kind="zip", runtime_kind="function"
    )
    with pytest.raises(IntegrityError, match="uq_components_repo_id_name"):
        components.create(
            session, repo_id=repo.id, name="platform", language="go", artifact_kind="zip", runtime_kind="function"
        )


def test_artifact_get_list_update(session, component, artifact):
    newer = artifacts.create(session, component_id=component.id, kind="container-image", reference="sha256:def")
    assert artifacts.get(session, artifact.id) is artifact
    assert artifacts.list_(session, component_id=component.id) == [newer, artifact]
    assert artifacts.list_(session) == [newer, artifact]
    artifacts.update(session, artifact, expires_at=NOW + timedelta(days=30))
    assert artifact.expires_at == NOW + timedelta(days=30)


def test_build_get_list_update(session, repo):
    first = builds.create(session, repo_id=repo.id, commit="a" * 40, branch="main", passed=False, started_at=NOW)
    second = builds.create(
        session,
        repo_id=repo.id,
        commit="b" * 40,
        branch="feat/x",
        passed=True,
        started_at=NOW + timedelta(hours=1),
        stages={"lint": "passed", "test": {"result": "passed", "tests": 42}},
        workflow_run_url="https://github.com/rdlucas2/rlp/actions/runs/1",
    )
    assert first.stages == {}
    assert builds.get(session, second.id).stages["test"]["tests"] == 42
    assert builds.list_(session, repo_id=repo.id) == [second, first]
    assert builds.list_(session, branch="main") == [first]
    builds.update(session, first, passed=True, finished_at=NOW + timedelta(minutes=5))
    session.expire_all()
    assert builds.get(session, first.id).passed is True


def test_deployment_get_list_update(session, component, artifact):
    deployment = deployments.create(
        session, component_id=component.id, environment="dev", artifact_id=artifact.id, status="pending"
    )
    assert deployment.deployed_at is not None
    assert deployments.get(session, deployment.id) is deployment
    assert deployments.list_(session, component_id=component.id, environment="dev") == [deployment]
    assert deployments.list_(session, environment="prod") == []
    deployments.update(session, deployment, status="succeeded")
    assert deployment.status == "succeeded"


def test_deployment_keeps_its_artifact(session, component, artifact):
    deployments.create(session, component_id=component.id, environment="dev", artifact_id=artifact.id, status="ok")
    with pytest.raises(IntegrityError, match="fk_deployments_artifact_id_artifacts"):
        session.execute(sa.text("DELETE FROM artifacts WHERE id = :id"), {"id": artifact.id})


def test_transitions_record_history_in_order(session, project):
    first = transitions.create(session, project_id=project.id, from_phase=None, to_phase=Phase.PROPOSED, actor="rlp")
    second = transitions.create(
        session,
        project_id=project.id,
        from_phase=Phase.PROPOSED,
        to_phase=Phase.DESIGNED,
        actor="rdlucas2",
        reason="architecture spec accepted",
        rule_results=[{"rule": "spec-present", "passed": True}],
    )
    assert first.rule_results == []
    assert transitions.get(session, second.id).to_phase is Phase.DESIGNED
    assert transitions.list_(session, project_id=project.id) == [first, second]


def test_deleting_a_project_with_history_is_refused(session, project):
    transitions.create(session, project_id=project.id, from_phase=None, to_phase=Phase.PROPOSED, actor="rlp")
    with pytest.raises(IntegrityError, match="fk_lifecycle_transitions_project_id_projects"):
        session.execute(sa.text("DELETE FROM projects WHERE id = :id"), {"id": project.id})


def test_session_factory(migrated_db):
    factory = store.session_factory(migrated_db)
    with factory() as s, s.begin():
        projects.create(s, name="from-factory", owner="o")
    with factory() as s:
        assert projects.get_by_name(s, "from-factory").owner == "o"
    factory.kw["bind"].dispose()

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from rlp_platform import lifecycle
from rlp_platform.models import Phase
from rlp_platform.store import builds, projects, repos, transitions

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def doc(**overrides):
    base = {
        "project": {
            "name": "api",
            "description": "The orders API",
            "owner": "rdlucas2",
            "dashboard_url": "https://d/x",
        },
        "from": "in-dev",
        "to": "live",
        "repos": [
            {
                "full_name": "acme/api",
                "default_branch": "main",
                "builds": 1,
                "latest_default_branch_build": {"commit": "abc", "passed": True},
            }
        ],
        "teardown": None,
    }
    return base | overrides


def failing(results):
    return [r.rule for r in results if not r.passed]


# --- evaluate: the rules through opa, no database ---


def test_evaluate_passes_a_ready_project(opa_path):
    results = lifecycle.evaluate(doc(), opa_path=opa_path)
    assert [r.rule for r in results] == ["RLP-L101", "RLP-L105", "RLP-L106", "RLP-L107"]
    assert all(r.passed and r.messages == [] for r in results)
    assert results[1].description == "live needs an owner"


def test_evaluate_lists_both_missing_owner_and_dashboard(opa_path):
    project = {"name": "api", "description": "d", "owner": "", "dashboard_url": None}
    results = lifecycle.evaluate(doc(project=project), opa_path=opa_path)
    assert failing(results) == ["RLP-L105", "RLP-L106"]
    assert results[1].messages[0].startswith("[RLP-L105] rlp-transition.json project.owner: ")


def test_evaluate_rejects_an_unknown_phase_by_rule(opa_path):
    results = lifecycle.evaluate(doc(**{"from": "proposed", "to": "launched"}), opa_path=opa_path)
    assert [(r.rule, r.passed) for r in results] == [("RLP-L101", False)]


def test_evaluate_finds_opa_on_path(opa_path, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(opa_path))
    assert failing(lifecycle.evaluate(doc(**{"from": "proposed"}))) == ["RLP-L101"]


def test_evaluate_without_opa(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(lifecycle.LifecycleError, match="opa is not on PATH"):
        lifecycle.evaluate(doc())


def test_evaluate_with_a_missing_binary(tmp_path):
    with pytest.raises(lifecycle.LifecycleError, match="could not run opa"):
        lifecycle.evaluate(doc(), opa_path=str(tmp_path / "nope"))


def fake_opa(tmp_path, script: str) -> str:
    path = tmp_path / "opa"
    path.write_text(f"#!/bin/sh\ncat >/dev/null\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(os.name == "nt", reason="the fake opa is a shell script")
def test_evaluate_reports_an_opa_failure(tmp_path):
    exe = fake_opa(tmp_path, "echo 'rego_parse_error: boom' >&2\nexit 2")
    with pytest.raises(lifecycle.LifecycleError, match="opa eval failed: rego_parse_error: boom"):
        lifecycle.evaluate(doc(), opa_path=exe)


@pytest.mark.skipif(os.name == "nt", reason="the fake opa is a shell script")
def test_evaluate_reports_unexpected_output(tmp_path):
    exe = fake_opa(tmp_path, "echo '{\"result\": []}'")
    with pytest.raises(lifecycle.LifecycleError, match="opa answered something unexpected"):
        lifecycle.evaluate(doc(), opa_path=exe)


def test_shipped_rules_are_the_lifecycle_package():
    shipped = sorted(p.name for p in lifecycle.POLICY_DIR.iterdir())
    assert shipped == ["lifecycle.rego"]
    assert "package rlp.lifecycle\n" in (lifecycle.POLICY_DIR / "lifecycle.rego").read_text(encoding="utf-8")


# --- transition: the project record ---


@pytest.fixture
def project(session):
    return projects.create(session, name="api", owner="", description="The orders API")


@pytest.fixture
def repo(session, project):
    return repos.create(session, full_name="acme/api", project_id=project.id)


def add_build(session, repo, *, passed, branch="main", minutes=0, commit="abc"):
    return builds.create(
        session,
        repo_id=repo.id,
        commit=commit,
        branch=branch,
        passed=passed,
        started_at=NOW + timedelta(minutes=minutes),
    )


def test_live_without_owner_and_dashboard_is_rejected(session, opa_path, project, repo):
    add_build(session, repo, passed=True)
    projects.update(session, project, phase=Phase.IN_DEV)

    outcome = lifecycle.transition(session, project, Phase.LIVE, "rdlucas2", "ship it", opa_path=opa_path)

    assert not outcome.allowed
    assert outcome.transition is None
    assert [r.rule for r in outcome.failures] == ["RLP-L105", "RLP-L106"]
    assert project.phase is Phase.IN_DEV
    assert transitions.list_(session, project_id=project.id) == []


def test_proposed_straight_to_live_is_rejected_by_the_moves_rule(session, opa_path, project):
    projects.update(session, project, owner="rdlucas2", dashboard_url="https://grafana.example/d/api")

    outcome = lifecycle.transition(session, project, "live", "rdlucas2", opa_path=opa_path)

    assert not outcome.allowed
    assert "RLP-L101" in [r.rule for r in outcome.failures]
    assert outcome.failures[0].messages[0].startswith('[RLP-L101] rlp-transition.json to: project "api" may not move')
    assert project.phase is Phase.PROPOSED
    assert transitions.list_(session, project_id=project.id) == []


def test_rejected_move_is_kept_in_the_history_as_rejected(session, opa_path, project, repo):
    add_build(session, repo, passed=True)
    projects.update(session, project, phase=Phase.IN_DEV)

    outcome = lifecycle.transition(session, project, Phase.LIVE, "rdlucas2", "ship it", opa_path=opa_path)

    assert outcome.rejected is not None
    [row] = transitions.list_(session, project_id=project.id, include_rejected=True)
    assert row.id == outcome.rejected.id
    assert (row.from_phase, row.to_phase, row.actor, row.reason, row.accepted) == (
        Phase.IN_DEV,
        Phase.LIVE,
        "rdlucas2",
        "ship it",
        False,
    )
    assert [r["rule"] for r in row.rule_results if not r["passed"]] == ["RLP-L105", "RLP-L106"]
    assert project.phase is Phase.IN_DEV


def test_move_to_an_unknown_phase_is_not_recorded(session, opa_path, project):
    outcome = lifecycle.transition(session, project, "launched", "rdlucas2", opa_path=opa_path)

    assert not outcome.allowed
    assert outcome.rejected is None
    assert transitions.list_(session, project_id=project.id, include_rejected=True) == []


def test_allowed_move_sets_phase_and_records_one_transition(session, opa_path, project):
    outcome = lifecycle.transition(session, project, Phase.DESIGNED, "rdlucas2", "spec agreed", opa_path=opa_path)

    assert outcome.allowed
    assert outcome.failures == []
    assert (outcome.from_phase, outcome.to_phase) == (Phase.PROPOSED, "designed")
    session.expire_all()
    assert projects.get(session, project.id).phase is Phase.DESIGNED
    [row] = transitions.list_(session, project_id=project.id)
    assert row.id == outcome.transition.id
    assert (row.from_phase, row.to_phase, row.actor, row.reason) == (
        Phase.PROPOSED,
        Phase.DESIGNED,
        "rdlucas2",
        "spec agreed",
    )
    assert row.rule_results == [
        {"rule": "RLP-L101", "description": "the move is allowed", "passed": True, "messages": []},
        {"rule": "RLP-L102", "description": "designed needs a description", "passed": True, "messages": []},
    ]


def test_a_project_walks_every_phase(session, opa_path, project, repo):
    def move(to):
        return lifecycle.transition(session, project, to, "rdlucas2", opa_path=opa_path)

    assert move(Phase.DESIGNED).allowed
    assert move(Phase.SCAFFOLDED).allowed
    assert [r.rule for r in move(Phase.IN_DEV).failures] == ["RLP-L104"]
    add_build(session, repo, passed=False, branch="feature", minutes=1)
    assert move(Phase.IN_DEV).allowed
    projects.update(session, project, owner="rdlucas2", dashboard_url="https://grafana.example/d/api")
    assert [r.rule for r in move(Phase.LIVE).failures] == ["RLP-L107"]  # no default-branch build yet
    add_build(session, repo, passed=True, minutes=2)
    add_build(session, repo, passed=False, minutes=3, commit="def")
    failed = move(Phase.LIVE).failures
    assert [r.rule for r in failed] == ["RLP-L107"]
    assert "(commit def) failed" in failed[0].messages[0]
    add_build(session, repo, passed=True, minutes=4, commit="fed")
    assert move(Phase.LIVE).allowed
    assert move(Phase.DEPRECATED).allowed
    assert move(Phase.LIVE).allowed
    assert move(Phase.DEPRECATED).allowed
    assert [r.rule for r in move(Phase.RETIRED).failures] == ["RLP-L108"]
    history = transitions.list_(session, project_id=project.id)
    assert [t.to_phase for t in history] == [
        Phase.DESIGNED,
        Phase.SCAFFOLDED,
        Phase.IN_DEV,
        Phase.LIVE,
        Phase.DEPRECATED,
        Phase.LIVE,
        Phase.DEPRECATED,
    ]
    assert project.phase is Phase.DEPRECATED


def test_build_input_reads_the_record(session, project, repo):
    other = repos.create(session, full_name="acme/web", project_id=project.id, default_branch="trunk")
    add_build(session, repo, passed=True, minutes=1, commit="old")
    add_build(session, repo, passed=False, minutes=2, commit="new")
    add_build(session, repo, passed=True, branch="feature", minutes=3, commit="branch")
    add_build(session, other, passed=True, branch="main", commit="wrong-branch")

    document = lifecycle.build_input(session, project, Phase.LIVE)

    assert document == {
        "project": {"name": "api", "description": "The orders API", "owner": "", "dashboard_url": None},
        "from": "proposed",
        "to": "live",
        "repos": [
            {
                "full_name": "acme/api",
                "default_branch": "main",
                "builds": 3,
                "latest_default_branch_build": {"commit": "new", "passed": False},
            },
            {"full_name": "acme/web", "default_branch": "trunk", "builds": 1, "latest_default_branch_build": None},
        ],
        "teardown": None,
    }
    json.dumps(document)  # it goes to opa as JSON

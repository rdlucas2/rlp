"""The repo indexer against Postgres, with GitHub answered from recorded responses (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rlp_platform import indexer
from rlp_platform.github import GitHub, GitHubError, signature_ok
from rlp_platform.models import Phase
from rlp_platform.store import components, projects, repos

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"
SHA1 = "1" * 40
SHA2 = "2" * 40
REPO = "rdlucas2/rlp"


def client(github) -> GitHub:
    return GitHub("https://api.github.test", "test-token", transport=github.transport)


def with_component(contract: str, name: str) -> str:
    """platform/rlp.yaml with a second component, a job."""
    doc = yaml.safe_load(contract)
    doc["components"].append(
        {
            "name": name,
            "path": "worker",
            "language": "python",
            "artifact": {"kind": "container-image"},
            "runtime": {"kind": "job"},
        }
    )
    return yaml.safe_dump(doc)


def as_job(contract: str) -> str:
    """platform/rlp.yaml with its component run as a job."""
    doc = yaml.safe_load(contract)
    doc["components"][0]["runtime"] = {"kind": "job"}
    return yaml.safe_dump(doc)


def test_shipped_schema_is_the_contract_schema():
    shipped = json.loads(indexer.resources.files("rlp_platform").joinpath("schemas", "repo.schema.json").read_text())
    assert shipped == json.loads((ROOT / "schemas" / "v1alpha1" / "repo.schema.json").read_text()), (
        "platform/src/rlp_platform/schemas/repo.schema.json differs from schemas/v1alpha1/repo.schema.json; "
        "copy the schema over it"
    )


def test_new_repo_creates_its_project_in_proposed(session, github, platform_contract):
    github.add(REPO, SHA1, platform_contract)
    with client(github) as gh:
        result = indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert result.ok
    assert result.as_dict() == {
        "repo": REPO,
        "commit": SHA1,
        "status": "indexed",
        "project": "rlp-platform",
        "components": ["platform"],
        "removed": [],
        "error": None,
    }
    project = projects.get_by_name(session, "rlp-platform")
    assert project.phase is Phase.PROPOSED
    assert project.description == "FastAPI service rendered from the rlp python-fastapi template."
    repo = repos.get_by_full_name(session, REPO)
    assert repo.project_id == project.id
    assert (repo.template_source, repo.template_version) == ("gh:rdlucas2/rlp-template-python-fastapi", "d5f0037")
    assert repo.last_indexed_commit == SHA1
    assert repo.index_error is None
    [component] = components.list_(session, repo_id=repo.id)
    assert (component.name, component.language, component.artifact_kind, component.runtime_kind) == (
        "platform",
        "python",
        "container-image",
        "container-service",
    )
    assert {r.url.params["ref"] for r in github.requests} == {SHA1}


def test_the_repo_root_contract_links_to_its_own_project(session, github):
    """rdlucas2/rlp's own rlp.yaml names project rlp; owners[0] becomes the new project's owner."""
    github.add(REPO, SHA1, {"rlp.yaml": (ROOT / "rlp.yaml").read_text(encoding="utf-8")})
    with client(github) as gh:
        result = indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert (result.project, result.components) == ("rlp", ("platform",))
    assert projects.get_by_name(session, "rlp").owner == "rdlucas2"
    repo = repos.get_by_full_name(session, REPO)
    assert (repo.template_source, repo.template_version) == (None, None)


def test_an_existing_project_is_linked_not_changed(session, github, platform_contract):
    existing = projects.create(session, name="rlp-platform", owner="ryan", description="kept", phase=Phase.IN_DEV)
    github.add(REPO, SHA1, platform_contract)
    with client(github) as gh:
        indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert repos.get_by_full_name(session, REPO).project_id == existing.id
    assert (existing.owner, existing.description, existing.phase) == ("ryan", "kept", Phase.IN_DEV)


def test_metadata_template_is_used_without_copier_answers(session, github, platform_contract):
    github.add(REPO, SHA1, {"rlp.yaml": platform_contract["rlp.yaml"]})
    with client(github) as gh:
        indexer.index_commit(session, gh, REPO, SHA1, "main")
    repo = repos.get_by_full_name(session, REPO)
    assert (repo.template_source, repo.template_version) == ("gh:rdlucas2/rlp-template-python-fastapi", "d5f0037")


def test_changed_component_list_adds_marks_removed_and_restores(session, github, platform_contract):
    contract = platform_contract["rlp.yaml"]
    github.add(REPO, SHA1, {**platform_contract, "rlp.yaml": with_component(contract, "worker")})
    with client(github) as gh:
        first = indexer.index_commit(session, gh, REPO, SHA1, "main")
        assert first.components == ("platform", "worker")
        github.add(REPO, SHA2, platform_contract)
        second = indexer.index_commit(session, gh, REPO, SHA2, "main")
    assert (second.components, second.removed) == (("platform",), ("worker",))
    repo = repos.get_by_full_name(session, REPO)
    worker = components.get_by_name(session, repo.id, "worker")
    assert worker.removed_at is not None
    assert [c.name for c in components.list_(session, repo_id=repo.id, include_removed=False)] == ["platform"]
    assert [c.name for c in components.list_(session, repo_id=repo.id)] == ["platform", "worker"]

    # Indexing again doesn't remove it twice; declaring it again restores the same record.
    with client(github) as gh:
        assert indexer.index_commit(session, gh, REPO, SHA2, "main").removed == ()
        github.add(REPO, SHA1, {**platform_contract, "rlp.yaml": with_component(contract, "worker")})
        indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert components.get_by_name(session, repo.id, "worker") is worker
    assert worker.removed_at is None


def test_component_details_follow_rlp_yaml(session, github, platform_contract):
    github.add(REPO, SHA1, platform_contract)
    changed = as_job(platform_contract["rlp.yaml"])
    with client(github) as gh:
        indexer.index_commit(session, gh, REPO, SHA1, "main")
        github.add(REPO, SHA2, {**platform_contract, "rlp.yaml": changed})
        assert indexer.index_commit(session, gh, REPO, SHA2, "main").ok
    repo = repos.get_by_full_name(session, REPO)
    assert components.get_by_name(session, repo.id, "platform").runtime_kind == "job"


def test_invalid_rlp_yaml_keeps_the_previous_index(session, github, platform_contract):
    github.add(REPO, SHA1, platform_contract)
    invalid = (FIXTURES / "invalid-rlp.yaml").read_text(encoding="utf-8")
    with client(github) as gh:
        indexer.index_commit(session, gh, REPO, SHA1, "main")
        github.add(REPO, SHA2, {**platform_contract, "rlp.yaml": invalid})
        result = indexer.index_commit(session, gh, REPO, SHA2, "main")
    assert result.status == "invalid"
    assert result.project == "rlp-platform"
    assert result.error.startswith(f"at {SHA2}: rlp.yaml components.0")
    assert "'runtime' is a required property" in result.error
    assert "'wheel-of-cheese' is not one of" in result.error
    repo = repos.get_by_full_name(session, REPO)
    assert repo.index_error == result.error
    assert repo.last_indexed_commit == SHA1
    [component] = components.list_(session, repo_id=repo.id)
    assert (component.name, component.runtime_kind, component.removed_at) == ("platform", "container-service", None)

    # The next good push clears the error.
    with client(github) as gh:
        github.add(REPO, "3" * 40, platform_contract)
        indexer.index_commit(session, gh, REPO, "3" * 40, "main")
    assert repo.index_error is None


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({}, "no rlp.yaml at the repo root"),
        ({"rlp.yaml": "a: [b"}, "rlp.yaml is not valid YAML"),
        ({"rlp.yaml": "- just\n- a list\n"}, "rlp.yaml (root): ['just', 'a list'] is not of type 'object'"),
    ],
    ids=["missing", "not-yaml", "not-a-mapping"],
)
def test_missing_or_unreadable_rlp_yaml_is_an_error(session, github, platform_contract, files, error):
    github.add(REPO, SHA1, platform_contract)
    with client(github) as gh:
        indexer.index_commit(session, gh, REPO, SHA1, "main")
        github.add(REPO, SHA2, files)
        result = indexer.index_commit(session, gh, REPO, SHA2, "main")
    assert result.status == "invalid"
    assert error in result.error
    assert repos.get_by_full_name(session, REPO).index_error == result.error


@pytest.mark.parametrize(
    ("answers", "error"),
    [("_commit: [x", ".copier-answers.yml is not valid YAML"), ("- a\n", ".copier-answers.yml is not a mapping")],
)
def test_unreadable_copier_answers_are_an_error(session, github, platform_contract, answers, error):
    github.add(REPO, SHA1, {**platform_contract, ".copier-answers.yml": answers})
    with client(github) as gh:
        result = indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert result.status == "invalid"
    assert error in result.error


def test_an_invalid_new_repo_records_nothing(session, github):
    github.add(REPO, SHA1, {})
    with client(github) as gh:
        result = indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert (result.status, result.project) == ("invalid", None)
    assert repos.list_(session) == []
    assert projects.list_(session) == []


def test_many_schema_problems_are_capped(github):
    contract = "apiVersion: rlp/v1alpha1\nkind: Repo\nmetadata: {name: a, project: a}\ncomponents:\n"
    contract += "".join(f"  - name: c{i}\n" for i in range(30))
    with pytest.raises(indexer.InvalidContract) as err:
        indexer.parse_contract(contract)
    lines = str(err.value).splitlines()
    assert len(lines) == indexer.MAX_PROBLEMS + 1
    assert lines[-1].startswith("... and ")


def test_a_github_error_propagates_and_changes_nothing(session, github):
    github.broken.add(REPO)
    with client(github) as gh, pytest.raises(GitHubError, match="GitHub answered 500"):
        indexer.index_commit(session, gh, REPO, SHA1, "main")
    assert repos.list_(session) == []


def test_index_head_reads_the_default_branch(session, github, platform_contract):
    github.add(REPO, SHA2, platform_contract, branch="trunk")
    with client(github) as gh:
        result = indexer.index_head(session, gh, REPO)
    assert result.commit == SHA2
    assert repos.get_by_full_name(session, REPO).default_branch == "trunk"


def test_rename_follows_the_repo(session, github, platform_contract):
    github.add("rdlucas2/old", SHA1, platform_contract)
    with client(github) as gh:
        indexer.index_commit(session, gh, "rdlucas2/old", SHA1, "main")
    assert indexer.rename(session, "rdlucas2/old", REPO)
    assert repos.get_by_full_name(session, REPO).last_indexed_commit == SHA1
    assert not indexer.rename(session, "rdlucas2/old", REPO)


def test_sync_indexes_each_repo_and_reports_failures(session, github, platform_contract):
    github.add(REPO, SHA1, platform_contract)
    github.add("rdlucas2/other", SHA2, {})
    github.broken.add("rdlucas2/down")
    with client(github) as gh:
        results = indexer.sync(session, gh, [REPO, "rdlucas2/down", "rdlucas2/other"])
    assert [(r.repo, r.status) for r in results] == [
        (REPO, "indexed"),
        ("rdlucas2/down", "failed"),
        ("rdlucas2/other", "invalid"),
    ]
    session.rollback()  # the indexed repo was committed on its own
    assert [r.full_name for r in repos.list_(session)] == [REPO]


def test_sync_targets(github):
    github.installation = [f"acme/r{i}" for i in range(150)]
    with client(github) as gh:
        assert indexer.sync_targets(gh, ("a/b",)) == ["a/b"]
        assert indexer.sync_targets(gh, ()) == github.installation
    pages = [r.url.params["page"] for r in github.requests]
    assert pages == ["1", "2"]
    anonymous = GitHub("https://api.github.test", None, transport=github.transport)
    with anonymous, pytest.raises(GitHubError, match="needs RLP_GITHUB_TOKEN"):
        indexer.sync_targets(anonymous, ())


def test_github_client_sends_the_token_and_reports_errors(github):
    github.add(REPO, SHA1, {})
    with client(github) as gh:
        assert gh.default_branch(REPO) == "main"
        assert gh.file(REPO, "rlp.yaml", SHA1) is None
        with pytest.raises(GitHubError, match="answered 404"):
            gh.head(REPO, "nope")
    assert github.requests[0].headers["authorization"] == "Bearer test-token"
    github.broken.add(REPO)
    with client(github) as gh, pytest.raises(GitHubError, match=r"reading rlp\.yaml of rdlucas2/rlp"):
        gh.file(REPO, "rlp.yaml", SHA1)
    anonymous = GitHub("https://api.github.test", None, transport=github.transport)
    with anonymous, pytest.raises(GitHubError):
        anonymous.default_branch(REPO)
    assert "authorization" not in github.requests[-1].headers


def test_github_client_reports_unreachable_and_non_json():
    import httpx

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with GitHub(transport=httpx.MockTransport(refuse)) as gh, pytest.raises(GitHubError, match="cannot reach GitHub"):
        gh.default_branch(REPO)
    html = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    with GitHub(transport=html) as gh, pytest.raises(GitHubError, match="other than JSON"):
        gh.default_branch(REPO)


@pytest.mark.parametrize(
    ("secret", "header", "ok"),
    [
        ("s3cret", "sha256=" + "0" * 64, False),
        ("s3cret", None, False),
        ("s3cret", "sha1=abc", False),
        (None, "sha256=anything", False),
        ("s3cret", "good", True),
    ],
    ids=["wrong", "missing", "sha1", "no-secret", "good"],
)
def test_signature_ok(secret, header, ok):
    import hashlib
    import hmac

    body = b'{"zen": "Keep it logically awesome."}'
    if header == "good":
        header = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert signature_ok(secret, body, header) is ok

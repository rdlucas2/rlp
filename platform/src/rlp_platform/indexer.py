"""The repo indexer: keep repos and components in step with each repo's rlp.yaml on GitHub.

`index_commit` reads rlp.yaml and .copier-answers.yml at one commit of a repo's
default branch and upserts the repo, its project link, its components and its
template source and version. The contract is validated with the same schema
`rlp validate` uses (a copy shipped in this package; a test keeps it in step
with schemas/v1alpha1/repo.schema.json).

- A project named by `metadata.project` that doesn't exist yet is created in
  `proposed`.
- An invalid or missing rlp.yaml leaves the previous index in place and records
  the error on the repo (`index_error`).
- A component that left rlp.yaml is marked removed (`removed_at`), not deleted,
  so its builds and deployments keep their history; one that comes back is
  restored.

`sync` re-indexes every repo it can see at its default branch's head. Each
repo is indexed and committed on its own, so one failure doesn't undo the rest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from rlp_platform.github import GitHub, GitHubError
from rlp_platform.models import Repo
from rlp_platform.store import NotFound, components, projects, repos

log = logging.getLogger(__name__)

CONTRACT = "rlp.yaml"
COPIER_ANSWERS = ".copier-answers.yml"
# Keep a stored error readable: the first few problems are enough to act on.
MAX_PROBLEMS = 20


class InvalidContract(ValueError):
    """rlp.yaml is missing, isn't YAML, or doesn't match the schema."""


@dataclass(frozen=True)
class IndexResult:
    repo: str
    commit: str | None
    status: str  # indexed, invalid, failed
    project: str | None = None
    components: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "indexed"

    def as_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["components"], doc["removed"] = list(self.components), list(self.removed)
        return doc


@cache
def schema() -> dict[str, Any]:
    text = resources.files("rlp_platform").joinpath("schemas", "repo.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def _dotted(path: Any) -> str:
    return ".".join(str(p) for p in path) or "(root)"


def parse_contract(text: str | None) -> dict[str, Any]:
    """The schema-valid rlp.yaml document in `text`; InvalidContract says what is wrong."""
    if text is None:
        raise InvalidContract(f"no {CONTRACT} at the repo root")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise InvalidContract(f"{CONTRACT} is not valid YAML: {err}") from err
    validator = Draft202012Validator(schema())
    errors = sorted(validator.iter_errors(doc), key=lambda e: (list(map(str, e.absolute_path)), e.message))
    if errors:
        lines = [f"{CONTRACT} {_dotted(e.absolute_path)}: {e.message}" for e in errors[:MAX_PROBLEMS]]
        if len(errors) > MAX_PROBLEMS:
            lines.append(f"... and {len(errors) - MAX_PROBLEMS} more")
        raise InvalidContract("\n".join(lines))
    return doc


def template_of(contract: dict[str, Any], answers_text: str | None) -> tuple[str | None, str | None]:
    """(source, version) the repo was rendered from: .copier-answers.yml first, else metadata.template."""
    declared = contract["metadata"].get("template") or {}
    source, version = declared.get("source"), declared.get("version")
    if answers_text is not None:
        try:
            answers = yaml.safe_load(answers_text)
        except yaml.YAMLError as err:
            raise InvalidContract(f"{COPIER_ANSWERS} is not valid YAML: {err}") from err
        if not isinstance(answers, dict):
            raise InvalidContract(f"{COPIER_ANSWERS} is not a mapping")
        source = answers.get("_src_path") or source
        version = answers.get("_commit") or version
    return (str(source) if source else None, str(version) if version else None)


def _find_repo(session: Session, full_name: str) -> Repo | None:
    try:
        return repos.get_by_full_name(session, full_name)
    except NotFound:
        return None


def record_error(session: Session, full_name: str, commit: str, error: str) -> IndexResult:
    """Keep the previous index; note the error on the repo, if it is known."""
    message = f"at {commit}: {error}"
    repo = _find_repo(session, full_name)
    if repo is not None:
        repos.update(session, repo, index_error=message)
    log.warning("repo not indexed", extra={"repo": full_name, "commit": commit, "error": error})
    return IndexResult(
        repo=full_name,
        commit=commit,
        status="invalid",
        project=repo.project.name if repo is not None else None,
        error=message,
    )


def apply(
    session: Session,
    full_name: str,
    commit: str,
    default_branch: str,
    contract: dict[str, Any],
    template: tuple[str | None, str | None],
    *,
    now: datetime | None = None,
) -> IndexResult:
    """Upsert the repo, its project link and its components from a valid contract."""
    now = now or datetime.now(UTC)
    metadata = contract["metadata"]
    name = metadata["project"]
    try:
        project = projects.get_by_name(session, name)
    except NotFound:
        owners = metadata.get("owners") or []
        project = projects.create(
            session, name=name, owner=owners[0] if owners else "", description=metadata.get("description", "")
        )
        log.info("project created by the indexer", extra={"project": name, "repo": full_name})
    source, version = template
    fields = {
        "project_id": project.id,
        "default_branch": default_branch,
        "template_source": source,
        "template_version": version,
        "last_indexed_commit": commit,
        "index_error": None,
    }
    repo = _find_repo(session, full_name)
    if repo is None:
        repo = repos.create(session, full_name=full_name, **{k: v for k, v in fields.items() if k != "index_error"})
    else:
        repos.update(session, repo, **fields)

    declared = {c["name"]: c for c in contract["components"]}
    existing = {c.name: c for c in components.list_(session, repo_id=repo.id)}
    for cname, c in declared.items():
        values = {
            "language": c["language"],
            "artifact_kind": c["artifact"]["kind"],
            "runtime_kind": c["runtime"]["kind"],
        }
        if cname in existing:
            components.update(session, existing[cname], removed_at=None, **values)
        else:
            components.create(session, repo_id=repo.id, name=cname, **values)
    removed = []
    for cname, record in existing.items():
        if cname not in declared and record.removed_at is None:
            components.update(session, record, removed_at=now)
            removed.append(cname)
    log.info("repo indexed", extra={"repo": full_name, "commit": commit, "project": name})
    return IndexResult(
        repo=full_name,
        commit=commit,
        status="indexed",
        project=name,
        components=tuple(sorted(declared)),
        removed=tuple(sorted(removed)),
    )


def index_commit(session: Session, gh: GitHub, full_name: str, commit: str, default_branch: str) -> IndexResult:
    """Index `full_name` at `commit` of its default branch. Flushes; the caller commits.

    GitHubError propagates: when GitHub can't be read, nothing changes.
    """
    contract_text = gh.file(full_name, CONTRACT, commit)
    try:
        contract = parse_contract(contract_text)
        template = template_of(contract, gh.file(full_name, COPIER_ANSWERS, commit))
    except InvalidContract as err:
        return record_error(session, full_name, commit, str(err))
    return apply(session, full_name, commit, default_branch, contract, template)


def index_head(session: Session, gh: GitHub, full_name: str) -> IndexResult:
    """Index the head of the repo's default branch."""
    branch = gh.default_branch(full_name)
    return index_commit(session, gh, full_name, gh.head(full_name, branch), branch)


def rename(session: Session, old: str, new: str) -> bool:
    """Follow a repo renamed on GitHub. False when the old name isn't indexed."""
    repo = _find_repo(session, old)
    if repo is None or _find_repo(session, new) is not None:
        return False
    repos.update(session, repo, full_name=new)
    return True


def sync_targets(gh: GitHub, configured: tuple[str, ...]) -> list[str]:
    """RLP_GITHUB_REPOS when set, else every repo of the token's installation."""
    return list(configured) if configured else gh.installation_repos()


def sync(session: Session, gh: GitHub, names: list[str]) -> list[IndexResult]:
    """Re-index each repo at its default branch's head, committing each on its own."""
    results = []
    for full_name in names:
        try:
            result = index_head(session, gh, full_name)
        except GitHubError as err:
            session.rollback()
            log.warning("repo not synced", extra={"repo": full_name, "error": str(err)})
            results.append(IndexResult(repo=full_name, commit=None, status="failed", error=str(err)))
            continue
        session.commit()
        results.append(result)
    return results

"""The few GitHub REST calls the platform makes, and webhook signature checks.

The repo indexer reads repos and files. The retirement workflow dispatches a
repo's pipeline to tear an environment down, archives repos, and deletes
container image versions from GHCR. The retirement sweep opens and updates
one "Retire <project>?" issue per idle project.

Requests carry RLP_GITHUB_TOKEN when it is set; without it only public repos
can be read. Pass `transport` to answer the calls from recorded responses
(tests).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import quote

import httpx

TIMEOUT = 30.0
API_VERSION = "2022-11-28"
PER_PAGE = 100
# Stop paging after this many pages: 10,000 repos is far past what one rlp instance indexes.
MAX_PAGES = 100


class GitHubError(RuntimeError):
    """GitHub could not be reached, or answered with an error other than a missing file."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def signature_ok(secret: str | None, body: bytes, header: str | None) -> bool:
    """True when `header` (X-Hub-Signature-256) is the HMAC-SHA256 of `body` under `secret`.

    Fails closed: with no secret configured, or no header, nothing verifies.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.encode(), expected.encode())


def _refused(what: str, resp: httpx.Response) -> GitHubError:
    return GitHubError(f"{what}: GitHub answered {resp.status_code}", resp.status_code)


def _repo(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


class GitHub:
    def __init__(
        self,
        api_url: str = "https://api.github.com",
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.has_token = bool(token)
        self._http = httpx.Client(base_url=api_url.rstrip("/"), headers=headers, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.get(path, **kwargs)
        except httpx.HTTPError as err:
            raise GitHubError(f"cannot reach GitHub: {err}") from err
        return resp

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise GitHubError(f"cannot reach GitHub: {err}") from err

    def _json(self, path: str, **kwargs: Any) -> Any:
        resp = self._get(path, **kwargs)
        if resp.status_code != 200:
            raise GitHubError(f"GET {path}: GitHub answered {resp.status_code}", resp.status_code)
        try:
            return resp.json()
        except ValueError as err:
            raise GitHubError(f"GET {path}: GitHub answered with something other than JSON") from err

    def file(self, full_name: str, path: str, ref: str) -> str | None:
        """The text of `path` at commit `ref`, or None when the file isn't there."""
        resp = self._get(
            f"{_repo(full_name)}/contents/{quote(path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubError(
                f"reading {path} of {full_name} at {ref}: GitHub answered {resp.status_code}", resp.status_code
            )
        return resp.text

    def default_branch(self, full_name: str) -> str:
        return str(self._json(_repo(full_name))["default_branch"])

    def head(self, full_name: str, branch: str) -> str:
        """The commit `branch` points at."""
        return str(self._json(f"{_repo(full_name)}/branches/{quote(branch, safe='')}")["commit"]["sha"])

    def installation_repos(self) -> list[str]:
        """Every repo the token's GitHub App installation can see, by full name."""
        if not self.has_token:
            raise GitHubError("listing the installation's repos needs RLP_GITHUB_TOKEN; or set RLP_GITHUB_REPOS")
        names: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            doc = self._json("/installation/repositories", params={"per_page": PER_PAGE, "page": page})
            batch = [r["full_name"] for r in doc.get("repositories", [])]
            names.extend(batch)
            if len(batch) < PER_PAGE:
                break
        return names

    def dispatch_workflow(self, full_name: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        """Start a workflow_dispatch run of `workflow` (its file name) on `ref` with `inputs`."""
        path = f"{_repo(full_name)}/actions/workflows/{quote(workflow, safe='')}/dispatches"
        resp = self._send("POST", path, json={"ref": ref, "inputs": inputs})
        if resp.status_code not in (200, 204):
            raise _refused(f"dispatching {workflow} on {full_name}", resp)

    def archive(self, full_name: str) -> None:
        """Archive the repo. Archiving an archived repo succeeds too."""
        resp = self._send("PATCH", _repo(full_name), json={"archived": True})
        if resp.status_code != 200:
            raise _refused(f"archiving {full_name}", resp)

    def delete_container_version(self, owner: str, package: str, version: str) -> bool:
        """Delete the version of GHCR container `package` (OWNER's) whose name or tag is `version`.

        `version` is a digest (sha256:...) or a tag. Returns False when the
        package or the version is already gone, so deleting twice is safe.
        Tries OWNER as an organization, then as a user.
        """
        name = quote(package, safe="")
        for scope in (f"/orgs/{quote(owner, safe='')}", f"/users/{quote(owner, safe='')}"):
            base = f"{scope}/packages/container/{name}/versions"
            found: int | None = None
            missing = False
            for page in range(1, MAX_PAGES + 1):
                resp = self._get(base, params={"per_page": PER_PAGE, "page": page})
                if resp.status_code == 404:
                    missing = True
                    break
                if resp.status_code != 200:
                    raise _refused(f"listing {package} versions", resp)
                batch = resp.json()
                for v in batch:
                    tags = ((v.get("metadata") or {}).get("container") or {}).get("tags") or []
                    if v.get("name") == version or version in tags:
                        found = int(v["id"])
                        break
                if found is not None or len(batch) < PER_PAGE:
                    break
            if missing:
                continue
            if found is None:
                return False
            resp = self._send("DELETE", f"{base}/{found}")
            if resp.status_code == 404:
                return False
            if resp.status_code != 204:
                raise _refused(f"deleting {package} {version}", resp)
            return True
        return False

    def delete_release_asset(self, full_name: str, tag: str, name: str) -> bool:
        """Delete the asset `name` of the release tagged `tag`. Returns False when the release or the asset is gone."""
        resp = self._get(f"{_repo(full_name)}/releases/tags/{quote(tag, safe='')}")
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise _refused(f"reading release {tag} of {full_name}", resp)
        asset = next((a for a in resp.json().get("assets") or [] if a.get("name") == name), None)
        if asset is None:
            return False
        resp = self._send("DELETE", f"{_repo(full_name)}/releases/assets/{int(asset['id'])}")
        if resp.status_code == 404:
            return False
        if resp.status_code != 204:
            raise _refused(f"deleting {name} from {full_name} {tag}", resp)
        return True

    def find_issue(self, full_name: str, title: str) -> dict[str, Any] | None:
        """The open issue (not a pull request) titled exactly `title`, or None."""
        for page in range(1, MAX_PAGES + 1):
            params = {"state": "open", "per_page": PER_PAGE, "page": page}
            batch = self._json(f"{_repo(full_name)}/issues", params=params)
            for issue in batch:
                if "pull_request" not in issue and issue.get("title") == title:
                    return dict(issue)
            if len(batch) < PER_PAGE:
                break
        return None

    def create_issue(self, full_name: str, title: str, body: str) -> dict[str, Any]:
        """Open an issue; returns GitHub's issue document (number, html_url, ...)."""
        resp = self._send("POST", f"{_repo(full_name)}/issues", json={"title": title, "body": body})
        if resp.status_code != 201:
            raise _refused(f"opening an issue in {full_name}", resp)
        return dict(resp.json())

    def update_issue(self, full_name: str, number: int, body: str) -> dict[str, Any] | None:
        """Replace the body of issue `number`; None when the issue is gone (deleted, or the repo moved)."""
        resp = self._send("PATCH", f"{_repo(full_name)}/issues/{int(number)}", json={"body": body})
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise _refused(f"updating issue {number} of {full_name}", resp)
        return dict(resp.json())

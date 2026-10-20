"""The few GitHub REST calls the repo indexer makes, and webhook signature checks.

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

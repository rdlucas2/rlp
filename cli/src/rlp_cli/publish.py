"""Publish packaged artifacts. The only place rlp branches on artifact kind.

- container-image: tag the local image <registry>/<image>:<version> and push it.
- zip and jar: attach the file to the GitHub Release for --release TAG.
- static: published by the static-site deploy (S3 + CloudFront, later slice).

Each published record carries a `reference`: the pushed image's digest
(<registry>/<image>@sha256:..., from `docker push`'s output) or the release
asset's download URL (from GITHUB_SERVER_URL and GITHUB_REPOSITORY), falling
back to the tag, or the release and file, when those aren't known. `rlp
pipeline report` records them as the build's artifacts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from rlp_cli import artifacts


class PublishError(Exception):
    """A publish command failed."""


def _run(args: list[str], cwd: Path) -> str:
    exe = shutil.which(args[0])
    if exe is None:
        raise PublishError(f"{args[0]} is not installed")
    proc = subprocess.run([exe, *args[1:]], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PublishError(f"{' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def repository(remote: str) -> str:
    """<registry>/<image> without its tag; a registry port (host:5000/x) is not a tag."""
    slash = remote.rfind("/")
    colon = remote.rfind(":")
    return remote[:colon] if colon > slash else remote


# The last line of `docker push`: "<tag>: digest: sha256:<hex> size: <n>".
PUSHED_DIGEST = re.compile(r"digest: (sha256:[0-9a-f]{64})")


def image_digest(remote: str, push_output: str) -> str | None:
    """<repository>@sha256:... from `docker push`'s output; None when it names no digest."""
    found = PUSHED_DIGEST.findall(push_output)
    return f"{repository(remote)}@{found[-1]}" if found else None


def asset_url(release: str, name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The release asset's download URL in GitHub Actions; None outside it."""
    env = os.environ if environ is None else environ
    server, repo = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY")
    if not server or not repo:
        return None
    return f"{server.rstrip('/')}/{repo}/releases/download/{release}/{name}"


def publish(root: Path, doc: dict, registry: str | None, release: str | None, only: tuple[str, ...] = ()) -> list[dict]:
    done = []
    for entry in artifacts.resolve(root, doc):
        if only and entry["component"] not in only:
            continue
        kind = entry["kind"]
        record = {"component": entry["component"], "kind": kind}
        if kind == "container-image":
            if not registry:
                record.update(status="skipped", detail="no --registry given")
            else:
                remote = f"{registry.rstrip('/')}/{entry['image']}"
                _run(["docker", "tag", entry["image"], remote], root)
                pushed = _run(["docker", "push", remote], root)
                record.update(status="published", detail=remote, reference=image_digest(remote, pushed) or remote)
        elif kind in ("zip", "jar"):
            if not release:
                record.update(status="skipped", detail="no --release given")
            else:
                path = root.joinpath(*Path(entry["path"]).parts)
                if not path.is_file():
                    raise PublishError(f"{entry['path']} not found; run the package verb first")
                _run(["gh", "release", "upload", release, str(path), "--clobber"], root)
                detail = f"release {release}: {entry['path']}"
                record.update(status="published", detail=detail, reference=asset_url(release, path.name) or detail)
        else:
            record.update(status="skipped", detail="static sites publish with their deploy")
        done.append(record)
    return done


def save(path: Path, done: list[dict]) -> None:
    """Write the publish records for `rlp pipeline report`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(done, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> list[dict]:
    """The records `save` wrote; none when the file is missing."""
    if not path.is_file():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [r for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []

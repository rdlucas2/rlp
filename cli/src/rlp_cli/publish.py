"""Publish packaged artifacts. The only place rlp branches on artifact kind.

- container-image: tag the local image <registry>/<image>:<version> and push it.
- zip and jar: attach the file to the GitHub Release for --release TAG.
- static: published by the static-site deploy (S3 + CloudFront, later slice).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rlp_cli import artifacts


class PublishError(Exception):
    """A publish command failed."""


def _run(args: list[str], cwd: Path) -> None:
    exe = shutil.which(args[0])
    if exe is None:
        raise PublishError(f"{args[0]} is not installed")
    proc = subprocess.run([exe, *args[1:]], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PublishError(f"{' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")


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
                _run(["docker", "push", remote], root)
                record.update(status="published", detail=remote)
        elif kind in ("zip", "jar"):
            if not release:
                record.update(status="skipped", detail="no --release given")
            else:
                path = root.joinpath(*Path(entry["path"]).parts)
                if not path.is_file():
                    raise PublishError(f"{entry['path']} not found; run the package verb first")
                _run(["gh", "release", "upload", release, str(path), "--clobber"], root)
                record.update(status="published", detail=f"release {release}: {entry['path']}")
        else:
            record.update(status="skipped", detail="static sites publish with their deploy")
        done.append(record)
    return done

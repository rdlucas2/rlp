"""What each component's package verb produces, and the environment rlp gives verbs.

Every verb runs with these variables set, so Taskfiles never guess names:

- RLP_PROJECT, RLP_COMPONENT: metadata.project and the component's name
- RLP_VERSION: $RLP_VERSION if set, else the short git commit, else "dev"
- RLP_IMAGE: for container-image components, the local image tag to build
- RLP_ARTIFACT: for zip, jar and static components, artifact.path
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath


def version(root: Path) -> str:
    explicit = os.environ.get("RLP_VERSION")
    if explicit:
        return explicit
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return "dev"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "dev"


def image_name(doc: dict, component: dict) -> str:
    """Local image repository for a component: <metadata.name>-<component>, lowercase."""
    name = f"{doc['metadata']['name']}-{component['name']}".lower()
    return re.sub(r"[^a-z0-9._-]", "-", name)


def verb_env(doc: dict, component: dict, ver: str) -> dict[str, str]:
    env = {
        "RLP_PROJECT": str(doc["metadata"].get("project", "")),
        "RLP_COMPONENT": component["name"],
        "RLP_VERSION": ver,
    }
    artifact = component.get("artifact") or {}
    if artifact.get("kind") == "container-image":
        env["RLP_IMAGE"] = f"{image_name(doc, component)}:{ver}"
    elif artifact.get("path"):
        env["RLP_ARTIFACT"] = artifact["path"]
    return env


def resolve(root: Path, doc: dict) -> list[dict]:
    """One entry per component: its artifact kind and where package leaves it."""
    ver = version(root)
    entries = []
    for c in doc.get("components") or []:
        artifact = c.get("artifact") or {}
        kind = artifact.get("kind")
        entry = {"component": c["name"], "kind": kind, "version": ver}
        if kind == "container-image":
            entry["image"] = f"{image_name(doc, c)}:{ver}"
        else:
            base = PurePosixPath(c.get("path", "."))
            entry["path"] = (base / artifact.get("path", "")).as_posix()
        entries.append(entry)
    return entries

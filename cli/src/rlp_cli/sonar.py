"""Optional Sonar analysis, for a SonarQube you run yourself.

The golden pipeline's gates are Semgrep (sast) and diff-cover (coverage on new
code); they need no server. This stage is an extra for people who want Sonar's
dashboard and history: it runs only when SONAR_TOKEN is set, which CI never
sets, so in practice it runs on a laptop against a local SonarQube.

rlp keeps each language's native report formats and maps them to the matching
Sonar properties (rlp_cli.reports, also shown by `rlp reports`), so templates
never carry Sonar configuration. The scanner is pysonar (`pip install pysonar`), which provisions
its own JRE; it runs with `sonar.qualitygate.wait=true`, so its exit code is the
stage's result.

- SONAR_TOKEN (required): without it the stage is skipped
- SONAR_HOST_URL: default http://localhost:9000; SonarQube Cloud also works
- SONAR_ORGANIZATION: needed for SonarQube Cloud only; default the GitHub owner
- SONAR_PROJECT_KEY: default <owner>_<repo> in GitHub Actions, else metadata.project
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rlp_cli import reports
from rlp_cli.contract import component_dir

SONARCLOUD = "https://sonarcloud.io"
LOCAL = "http://localhost:9000"


class SonarError(Exception):
    """The scanner is missing or the analysis could not be configured."""


def _rel(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel or "."


def properties(root: Path, doc: dict, env: dict[str, str] | None = None) -> dict[str, str]:
    """Analysis properties for the repo at root: identity, sources and report paths."""
    env = os.environ if env is None else env
    host = env.get("SONAR_HOST_URL") or LOCAL
    owner, _, repo = (env.get("GITHUB_REPOSITORY") or "").partition("/")
    key = env.get("SONAR_PROJECT_KEY") or (f"{owner}_{repo}" if owner and repo else doc["metadata"].get("project") or doc["metadata"]["name"])
    props = {
        "sonar.host.url": host,
        "sonar.projectKey": key,
        "sonar.projectBaseDir": str(root),
        "sonar.qualitygate.wait": "true",
    }
    org = env.get("SONAR_ORGANIZATION") or owner
    if host.rstrip("/") == SONARCLOUD:
        if not org:
            raise SonarError("SonarQube Cloud needs an organization: set SONAR_ORGANIZATION")
        props["sonar.organization"] = org
    sources = [_rel(root, component_dir(root, c)) for c in doc.get("components") or []]
    props["sonar.sources"] = ",".join(dict.fromkeys(sources)) or "."
    props.update(reports.sonar_properties(reports.resolve(doc)))
    return props


def configured(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("SONAR_TOKEN"))


def analyse(root: Path, doc: dict, stream=None) -> int:
    """Run pysonar and wait for the quality gate. Returns the scanner's exit code (0 = gate passed)."""
    exe = shutil.which("pysonar")
    if exe is None:
        raise SonarError("pysonar is not installed: pip install pysonar")
    args = [exe, *(f"-D{k}={v}" for k, v in properties(root, doc).items())]
    # The token stays in the environment (SONAR_TOKEN), never on the command line.
    return subprocess.run(args, cwd=root, stdout=stream, stderr=stream, check=False).returncode

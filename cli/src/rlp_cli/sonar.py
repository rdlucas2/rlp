"""Sonar analysis and quality gate.

rlp keeps each language's native coverage format and maps it to the matching
Sonar property (COVERAGE_PROPERTIES), so template authors never write Sonar
configuration. The scanner is pysonar, which provisions its own JRE; it runs
with `sonar.qualitygate.wait=true`, so its exit code is the gate.

Settings come from the environment, so the same command runs on a laptop and
in CI:

- SONAR_TOKEN (required): without it the stage is skipped, not passed
- SONAR_HOST_URL: default https://sonarcloud.io (SonarQube Cloud); set it to a
  self-hosted SonarQube Community Build for local or private use
- SONAR_ORGANIZATION: SonarQube Cloud organization; default the GitHub owner
- SONAR_PROJECT_KEY: default <owner>_<repo> (SonarQube Cloud's key for an
  imported GitHub repo), else metadata.project
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from rlp_cli.contract import component_dir

SONARCLOUD = "https://sonarcloud.io"

# Native coverage format -> the Sonar analysis property that reads it.
COVERAGE_PROPERTIES = {
    "cobertura": "sonar.python.coverage.reportPaths",
    "lcov": "sonar.javascript.lcov.reportPaths",
    "jacoco": "sonar.coverage.jacoco.xmlReportPaths",
    "opencover": "sonar.cs.opencover.reportsPaths",
}


class SonarError(Exception):
    """The scanner is missing or the analysis could not be configured."""


def _rel(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel or "."


def properties(root: Path, doc: dict, env: dict[str, str] | None = None) -> dict[str, str]:
    """Analysis properties for the repo at root: identity, sources and coverage report paths."""
    env = os.environ if env is None else env
    host = env.get("SONAR_HOST_URL") or SONARCLOUD
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
    sources, coverage = [], {}
    for c in doc.get("components") or []:
        sources.append(_rel(root, component_dir(root, c)))
        cov = (c.get("reports") or {}).get("coverage")
        if not cov:
            continue
        prop = COVERAGE_PROPERTIES.get(cov["format"])
        if prop is None:
            raise SonarError(f"component {c['name']}: no Sonar mapping for coverage format {cov['format']}")
        path = component_dir(root, c).joinpath(*PurePosixPath(cov["path"]).parts)
        coverage.setdefault(prop, []).append(_rel(root, path))
    props["sonar.sources"] = ",".join(dict.fromkeys(sources)) or "."
    for prop, paths in coverage.items():
        props[prop] = ",".join(paths)
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

"""Line coverage from each component's native report, checked against the minimum.

The minimum is quality.coverage.minimum from rlp.yaml, or the platform floor
from the policy bundle when rlp.yaml sets none. Two checks use it:

- overall: the report's line coverage (check)
- new code: coverage of the lines changed since the baseline commit, measured
  with diff-cover (new_code). OpenCover reports are not supported there.
"""

from __future__ import annotations

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from rlp_cli import resources
from rlp_cli.contract import component_dir


class CoverageError(Exception):
    """A report is missing or cannot be read."""


@dataclass
class Result:
    component: str
    format: str
    path: str
    percent: float | None
    minimum: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.percent is not None and self.percent >= self.minimum

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "format": self.format,
            "path": self.path,
            "percent": None if self.percent is None else round(self.percent, 2),
            "minimum": self.minimum,
            "ok": self.ok,
            "error": self.error,
        }


def platform_floor() -> float:
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8"))
    return float(data["coverage_floor"])


def minimum(doc: dict) -> float:
    declared = ((doc.get("quality") or {}).get("coverage") or {}).get("minimum")
    return float(declared) if declared is not None else platform_floor()


def _xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as err:
        raise CoverageError(f"{path.name} is not valid XML: {err}") from err


def cobertura(path: Path) -> float:
    rate = _xml(path).get("line-rate")
    if rate is None:
        raise CoverageError(f"{path.name} has no line-rate attribute")
    return float(rate) * 100


def lcov(path: Path) -> float:
    found = hit = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("LF:"):
            found += int(line[3:])
        elif line.startswith("LH:"):
            hit += int(line[3:])
    if found == 0:
        raise CoverageError(f"{path.name} reports no instrumented lines")
    return hit / found * 100


def jacoco(path: Path) -> float:
    root = _xml(path)
    for counter in root.findall("counter"):
        if counter.get("type") == "LINE":
            missed, covered = int(counter.get("missed", 0)), int(counter.get("covered", 0))
            if missed + covered == 0:
                raise CoverageError(f"{path.name} reports no lines")
            return covered / (missed + covered) * 100
    raise CoverageError(f"{path.name} has no report-level LINE counter")


def opencover(path: Path) -> float:
    summary = _xml(path).find("Summary")
    if summary is None or summary.get("sequenceCoverage") is None:
        raise CoverageError(f"{path.name} has no Summary sequenceCoverage")
    return float(summary.get("sequenceCoverage"))


PARSERS = {"cobertura": cobertura, "lcov": lcov, "jacoco": jacoco, "opencover": opencover}


def check(root: Path, doc: dict, only: tuple[str, ...] = ()) -> list[Result]:
    floor = minimum(doc)
    results = []
    for c in doc.get("components") or []:
        if only and c["name"] not in only:
            continue
        cov = (c.get("reports") or {}).get("coverage")
        if not cov:
            continue
        path = component_dir(root, c).joinpath(*PurePosixPath(cov["path"]).parts)
        rel = path.relative_to(root).as_posix()
        result = Result(c["name"], cov["format"], rel, None, floor)
        if not path.is_file():
            result.error = f"{rel} not found; does the test verb write it?"
        else:
            try:
                result.percent = PARSERS[cov["format"]](path)
            except (CoverageError, ValueError, OSError) as err:
                result.error = str(err)
        results.append(result)
    return results


# Formats diff-cover reads (it tells Cobertura and JaCoCo XML apart itself).
NEW_CODE_FORMATS = {"cobertura", "lcov", "jacoco"}


@dataclass
class NewCode:
    component: str
    percent: float | None  # None when skipped
    lines: int  # changed lines that have coverage data
    minimum: float
    note: str = ""
    missing: dict[str, list[int]] | None = None  # file -> uncovered changed lines

    @property
    def ok(self) -> bool:
        return self.percent is None or self.lines == 0 or self.percent >= self.minimum

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "percent": None if self.percent is None else round(self.percent, 2),
            "lines": self.lines,
            "minimum": self.minimum,
            "ok": self.ok,
            "note": self.note,
            "missing": self.missing or {},
        }

    def text(self) -> str:
        if self.percent is None:
            return f"{self.component} skipped ({self.note})"
        if self.lines == 0:
            return f"{self.component} no changed lines with coverage data"
        return f"{self.component} {self.percent:.1f}% of {self.lines} changed line(s) (minimum {self.minimum:g}%)"


def _diff_cover(report: Path, baseline: str, cwd: Path) -> dict:
    from diff_cover import diff_cover_tool  # imported here: it is only needed for this check

    from diff_cover.command_runner import CommandError

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "diff-cover.json"
        argv = ["diff-cover", str(report), "--compare-branch", baseline, "--format", f"json:{out}", "--quiet", "--total-percent-float"]
        # diff-cover runs `git diff` in the process's working directory.
        previous = os.getcwd()
        os.chdir(cwd)
        try:
            code = diff_cover_tool.main(argv=argv, directory=str(cwd))
        except CommandError as err:
            raise CoverageError(f"diff-cover could not compare {report.name} with {baseline[:12]}: {str(err).strip()}") from err
        finally:
            os.chdir(previous)
        if code != 0 or not out.is_file():
            raise CoverageError(f"diff-cover could not compare {report.name} with {baseline[:12]}")
        return json.loads(out.read_text(encoding="utf-8"))


def new_code(root: Path, doc: dict, baseline: str | None, only: tuple[str, ...] = ()) -> list[NewCode]:
    """Coverage of the lines changed since baseline, per component with a coverage report."""
    floor = minimum(doc)
    results = []
    for c in doc.get("components") or []:
        if only and c["name"] not in only:
            continue
        cov = (c.get("reports") or {}).get("coverage")
        if not cov:
            continue
        if baseline is None:
            results.append(NewCode(c["name"], None, 0, floor, "no baseline commit"))
            continue
        if cov["format"] not in NEW_CODE_FORMATS:
            results.append(NewCode(c["name"], None, 0, floor, f"diff-cover can't read {cov['format']}"))
            continue
        cdir = component_dir(root, c)
        path = cdir.joinpath(*PurePosixPath(cov["path"]).parts)
        if not path.is_file():
            continue  # the overall check already reports the missing file
        report = _diff_cover(path.resolve(), baseline, cdir.resolve())
        missing = {f: s.get("violation_lines", []) for f, s in (report.get("src_stats") or {}).items() if s.get("violation_lines")}
        results.append(NewCode(c["name"], float(report.get("total_percent_covered", 100)), int(report.get("total_num_lines", 0)), floor, missing=missing))
    return results

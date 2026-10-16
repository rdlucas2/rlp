"""Resolve report paths and map them to Sonar analysis properties."""

from __future__ import annotations

from pathlib import PurePosixPath

# (coverage format, language) -> Sonar property; language None matches any.
COVERAGE_PROPERTIES = {
    ("cobertura", "python"): "sonar.python.coverage.reportPaths",
    ("lcov", "javascript"): "sonar.javascript.lcov.reportPaths",
    ("lcov", "typescript"): "sonar.javascript.lcov.reportPaths",
    ("lcov", None): "sonar.javascript.lcov.reportPaths",
    ("jacoco", None): "sonar.coverage.jacoco.xmlReportPaths",
    ("opencover", None): "sonar.cs.opencover.reportsPaths",
}

JUNIT_PROPERTIES = {
    "python": "sonar.python.xunit.reportPath",
    "java": "sonar.junit.reportPaths",
    "kotlin": "sonar.junit.reportPaths",
}


def repo_path(component: dict, rel: str) -> str:
    """A report path relative to the repo root, with forward slashes."""
    base = PurePosixPath(component.get("path", "."))
    return (base / rel).as_posix()


def coverage_property(fmt: str, language: str) -> str | None:
    return COVERAGE_PROPERTIES.get((fmt, language)) or COVERAGE_PROPERTIES.get((fmt, None))


def resolve(doc: dict) -> list[dict]:
    """One entry per declared report, with its Sonar property (None when Sonar has no importer for it)."""
    entries = []
    for c in doc.get("components") or []:
        reports = c.get("reports") or {}
        language = c.get("language", "")
        if "junit" in reports:
            entries.append({
                "component": c["name"],
                "report": "junit",
                "format": "junit",
                "path": repo_path(c, reports["junit"]),
                "sonar_property": JUNIT_PROPERTIES.get(language),
            })
        coverage = reports.get("coverage")
        if coverage:
            entries.append({
                "component": c["name"],
                "report": "coverage",
                "format": coverage["format"],
                "path": repo_path(c, coverage["path"]),
                "sonar_property": coverage_property(coverage["format"], language),
            })
    return entries


def sonar_properties(entries: list[dict]) -> dict[str, str]:
    """Merge entries into Sonar properties; several components' paths join with commas."""
    props: dict[str, list[str]] = {}
    for e in entries:
        if e["sonar_property"]:
            props.setdefault(e["sonar_property"], []).append(e["path"])
    return {k: ",".join(v) for k, v in sorted(props.items())}

"""Merge the Python and UI test reports into the component's declared reports.

rlp.yaml declares one JUnit report and one Cobertura report for the platform
component; `task test` writes pytest's and Vitest's separately and this joins
them, so the pipeline's test count, coverage and new-code checks see both.

    python scripts/merge_reports.py --junit OUT IN... --cobertura OUT IN...

Cobertura file names are rewritten relative to the current directory (the
component's), with that directory as the one source, so diff-cover finds
src/rlp_platform/... and ui/src/... alike. Line and branch rates are
recomputed from the merged counts.

Reports are read with defusedxml; the merged documents are built from the
parsed elements (Element.makeelement), so nothing here imports xml directly.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET

# The xml.etree Element type; named loosely so the stdlib xml module needn't be imported.
Element = Any

JUNIT_TOTALS = ("tests", "failures", "errors", "skipped")


def _parse(path: Path) -> Element:
    return SafeET.parse(path).getroot()


def _element(tag: str, attrib: dict[str, str] | None = None) -> Element:
    """A new element, made from a parsed one so the stdlib xml module needn't be imported."""
    return SafeET.fromstring("<_/>").makeelement(tag, attrib or {})


def _child(parent: Element, tag: str, attrib: dict[str, str] | None = None) -> Element:
    child = _element(tag, attrib)
    parent.append(child)
    return child


def merge_junit(inputs: list[Path]) -> Element:
    """One <testsuites> holding every input's <testsuite> elements, with summed totals."""
    merged = _element("testsuites", {"name": "platform"})
    totals = dict.fromkeys(JUNIT_TOTALS, 0)
    seconds = 0.0
    for path in inputs:
        root = _parse(path)
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            merged.append(suite)
            for key in JUNIT_TOTALS:
                totals[key] += int(suite.get(key, "0"))
            seconds += float(suite.get("time", "0") or 0)
    for key, value in totals.items():
        merged.set(key, str(value))
    merged.set("time", f"{seconds:.3f}")
    return merged


def _rate(covered: int, valid: int) -> str:
    return f"{covered / valid:.4f}" if valid else "1"


def merge_cobertura(inputs: list[Path], base: Path) -> Element:
    """One <coverage> with every input's packages, file names relative to `base`."""
    base = base.resolve()
    merged = _element("coverage", {"version": "rlp-merge", "timestamp": "0", "complexity": "0"})
    _child(_child(merged, "sources"), "source").text = str(base)
    packages = _child(merged, "packages")
    counts = dict.fromkeys(("lines-valid", "lines-covered", "branches-valid", "branches-covered"), 0)
    for path in inputs:
        root = _parse(path)
        source_text = root.findtext("sources/source") or str(path.parent)
        source = Path(source_text.strip())
        for key in counts:
            counts[key] += int(root.get(key, "0"))
        for package in root.findall("packages/package"):
            for cls in package.iter("class"):
                filename = cls.get("filename")
                if filename:
                    cls.set("filename", Path(os.path.relpath(source / filename, base)).as_posix())
            packages.append(package)
    for key, value in counts.items():
        merged.set(key, str(value))
    merged.set("line-rate", _rate(counts["lines-covered"], counts["lines-valid"]))
    merged.set("branch-rate", _rate(counts["branches-covered"], counts["branches-valid"]))
    return merged


def write(root: Element, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(SafeET.tostring(root, encoding="utf-8", xml_declaration=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", nargs="+", metavar=("OUT", "IN"), required=True)
    parser.add_argument("--cobertura", nargs="+", metavar=("OUT", "IN"), required=True)
    args = parser.parse_args(argv)
    for flag, values in (("--junit", args.junit), ("--cobertura", args.cobertura)):
        if len(values) < 2:
            parser.error(f"{flag} needs an output and at least one input")
        missing = [v for v in values[1:] if not Path(v).is_file()]
        if missing:
            print(f"merge_reports: missing report(s): {', '.join(missing)}", file=sys.stderr)
            return 1
    junit_out, *junit_in = map(Path, args.junit)
    cov_out, *cov_in = map(Path, args.cobertura)
    write(merge_junit(junit_in), junit_out)
    write(merge_cobertura(cov_in, Path.cwd()), cov_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

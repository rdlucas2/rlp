"""scripts/merge_reports.py: `task test` joins the Python and UI reports into the declared ones."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from defusedxml import ElementTree as SafeET

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_reports.py"
spec = importlib.util.spec_from_file_location("merge_reports", SCRIPT)
assert spec is not None and spec.loader is not None
merge_reports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_reports)

PYTEST_JUNIT = (
    '<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3" time="1.5">'
    '<testcase classname="t" name="a"/><testcase classname="t" name="b"/><testcase classname="t" name="c"/>'
    "</testsuite></testsuites>"
)
VITEST_JUNIT = (
    '<testsuites name="vitest tests" tests="2"><testsuite name="src/App.test.tsx" errors="1" failures="0" '
    'skipped="0" tests="2" time="0.25"><testcase name="x"/><testcase name="y"/></testsuite></testsuites>'
)
BARE_SUITE = '<testsuite name="other" tests="1"><testcase name="z"/></testsuite>'


def cobertura(source: Path, filename: str, valid: int, covered: int, bvalid: int, bcovered: int) -> str:
    return (
        f'<coverage lines-valid="{valid}" lines-covered="{covered}" branches-valid="{bvalid}" '
        f'branches-covered="{bcovered}" line-rate="0" branch-rate="0">'
        f"<sources><source>{source}</source></sources>"
        f'<packages><package name="p"><classes><class name="c" filename="{filename}">'
        '<lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>'
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_junit_keeps_every_suite_and_sums_the_totals(tmp_path):
    merged = merge_reports.merge_junit(
        [
            write(tmp_path / "py.xml", PYTEST_JUNIT),
            write(tmp_path / "ui.xml", VITEST_JUNIT),
            write(tmp_path / "bare.xml", BARE_SUITE),
        ]
    )
    assert merged.tag == "testsuites"
    assert [s.get("name") for s in merged.findall("testsuite")] == ["pytest", "src/App.test.tsx", "other"]
    assert sum(1 for _ in merged.iter("testcase")) == 6
    assert {k: merged.get(k) for k in ("tests", "failures", "errors", "skipped", "time")} == {
        "tests": "6",
        "failures": "1",
        "errors": "1",
        "skipped": "1",
        "time": "1.750",
    }


def test_cobertura_rebases_file_names_and_recomputes_rates(tmp_path):
    base = tmp_path / "platform"
    merged = merge_reports.merge_cobertura(
        [
            write(tmp_path / "py.xml", cobertura(base / "src", "rlp_platform/api.py", 90, 81, 10, 5)),
            write(tmp_path / "ui.xml", cobertura(base / "ui", "src/App.tsx", 10, 10, 0, 0)),
        ],
        base,
    )
    assert [s.text for s in merged.findall("sources/source")] == [str(base.resolve())]
    assert [c.get("filename") for c in merged.iter("class")] == ["src/rlp_platform/api.py", "ui/src/App.tsx"]
    assert (merged.get("lines-valid"), merged.get("lines-covered")) == ("100", "91")
    assert (merged.get("line-rate"), merged.get("branch-rate")) == ("0.9100", "0.5000")


def test_cobertura_without_lines_counts_as_covered(tmp_path):
    report = write(tmp_path / "empty.xml", '<coverage><packages><package name="p"/></packages></coverage>')
    merged = merge_reports.merge_cobertura([report], tmp_path)
    assert (merged.get("line-rate"), merged.get("branch-rate")) == ("1", "1")


def test_main_writes_both_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "in" / "py.xml", PYTEST_JUNIT)
    write(tmp_path / "in" / "ui.xml", VITEST_JUNIT)
    write(tmp_path / "in" / "py-cov.xml", cobertura(tmp_path / "src", "m.py", 4, 3, 0, 0))
    code = merge_reports.main(
        ["--junit", "out/junit.xml", "in/py.xml", "in/ui.xml", "--cobertura", "out/coverage.xml", "in/py-cov.xml"]
    )
    assert code == 0
    assert sum(1 for _ in SafeET.parse(tmp_path / "out" / "junit.xml").getroot().iter("testcase")) == 5
    coverage = SafeET.parse(tmp_path / "out" / "coverage.xml").getroot()
    assert coverage.get("line-rate") == "0.7500"
    assert [c.get("filename") for c in coverage.iter("class")] == ["src/m.py"]


def test_main_fails_on_a_missing_report(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "py.xml", PYTEST_JUNIT)
    code = merge_reports.main(["--junit", "j.xml", "py.xml", "--cobertura", "c.xml", "nope.xml"])
    assert code == 1
    assert "missing report(s): nope.xml" in capsys.readouterr().err
    assert not (tmp_path / "j.xml").exists()


def test_main_needs_an_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        merge_reports.main(["--junit", "j.xml", "--cobertura", "c.xml", "in.xml"])


def test_written_reports_are_xml(tmp_path):
    out = tmp_path / "deep" / "r.xml"
    merge_reports.write(SafeET.fromstring("<testsuites/>"), out)
    assert out.read_text(encoding="utf-8").startswith("<?xml")

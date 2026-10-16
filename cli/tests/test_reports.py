from __future__ import annotations

from conftest import EXAMPLES


def test_example_reports(rlp):
    result = rlp("reports", "--path", str(EXAMPLES), "-o", "json")
    assert result.code == 0
    assert result.json["sonar"] == {
        "sonar.python.coverage.reportPaths": "reports/coverage.xml",
        "sonar.python.xunit.reportPath": "reports/junit.xml",
    }


def test_text_output_prints_properties(rlp):
    result = rlp("reports", "--path", str(EXAMPLES))
    assert "sonar.python.coverage.reportPaths=reports/coverage.xml" in result.stdout


def test_paths_are_relative_to_repo_and_merged():
    from rlp_cli.reports import resolve, sonar_properties

    doc = {"components": [
        {"name": "web", "path": "web", "language": "typescript", "reports": {"coverage": {"format": "lcov", "path": "coverage/lcov.info"}}},
        {"name": "admin", "path": "admin", "language": "javascript", "reports": {"coverage": {"format": "lcov", "path": "coverage/lcov.info"}}},
        {"name": "svc", "language": "go", "reports": {"coverage": {"format": "cobertura", "path": "cover.xml"}}},
    ]}
    entries = resolve(doc)
    assert [e["path"] for e in entries] == ["web/coverage/lcov.info", "admin/coverage/lcov.info", "cover.xml"]
    assert entries[2]["sonar_property"] is None
    assert sonar_properties(entries) == {"sonar.javascript.lcov.reportPaths": "web/coverage/lcov.info,admin/coverage/lcov.info"}

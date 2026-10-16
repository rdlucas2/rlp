from __future__ import annotations

from rlp_cli import __version__


def test_version(rlp):
    result = rlp("--version")
    assert result.code == 0
    assert __version__ in result.stdout


def test_help_lists_every_command(rlp):
    result = rlp("--help")
    assert result.code == 0
    for command in ["validate", "policy", "setup", "lint", "test", "build", "package", "run", "smoke", "new", "update", "reports"]:
        assert command in result.stdout


def test_unknown_command_is_a_usage_error(rlp):
    assert rlp("deploy").code == 2


def test_output_json_works_before_or_after_the_command(rlp, tmp_path):
    before = rlp("--output", "json", "validate", str(tmp_path))
    after = rlp("validate", str(tmp_path), "-o", "json")
    assert before.code == after.code == 2
    assert before.json == after.json
    assert before.json["ok"] is False

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RLP_YAML = """\
apiVersion: rlp/v1alpha1
kind: Repo
metadata:
  name: {{ project_id }}
  project: {{ project_id }}
components:
  - name: api
    language: python
    artifact:
      kind: container-image
    runtime:
      kind: container-service
      port: {{ port }}
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "rlp-tests",
    "GIT_AUTHOR_EMAIL": "rlp-tests@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-tests",
    "GIT_COMMITTER_EMAIL": "rlp-tests@example.invalid",
}


def git(cwd: Path, *args: str) -> None:
    import os

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def make_template(root: Path, port_default: int = 8000, rlp_yaml: str = RLP_YAML) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "copier.yml").write_text(
        "_answers_file: .copier-answers.yml\n"
        "project_id:\n  type: str\n  default: hello\n"
        f"port:\n  type: int\n  default: {port_default}\n",
        encoding="utf-8",
    )
    (root / "rlp.yaml.jinja").write_text(rlp_yaml, encoding="utf-8")
    (root / "{{ _copier_conf.answers_file }}.jinja").write_text("{{ _copier_answers|to_nice_yaml }}", encoding="utf-8")
    return root


@pytest.fixture
def need_git():
    if shutil.which("git") is None:
        pytest.fail("git must be on PATH for the template tests")


def test_new_renders_and_validates(rlp, tmp_path, need_git):
    template = make_template(tmp_path / "tpl")
    dest = tmp_path / "out"
    result = rlp("new", str(template), str(dest), "--defaults", "--data", "project_id=demo", "-o", "json")
    assert result.code == 0, result.stdout + result.stderr
    assert result.json["ok"] is True
    assert "name: demo" in (dest / "rlp.yaml").read_text(encoding="utf-8")


def test_new_fails_when_rendered_contract_is_invalid(rlp, tmp_path, need_git):
    template = make_template(tmp_path / "tpl", rlp_yaml=RLP_YAML.replace("container-service", "vm"))
    result = rlp("new", str(template), str(tmp_path / "out"), "--defaults", "-o", "json")
    assert result.code == 1
    assert result.json["problems"][0]["path"] == "components[0].runtime.kind"


def test_new_rejects_malformed_data(rlp, tmp_path):
    result = rlp("new", str(tmp_path / "tpl"), str(tmp_path / "out"), "--data", "novalue")
    assert result.code == 1
    assert "--data expects KEY=VALUE" in result.stderr


def test_update_applies_new_template_version(rlp, tmp_path, need_git):
    template = make_template(tmp_path / "tpl")
    git(template, "init", "-q")
    git(template, "add", "-A")
    git(template, "commit", "-qm", "v1")
    git(template, "tag", "v1.0.0")

    dest = tmp_path / "out"
    assert rlp("new", str(template), str(dest), "--defaults").code == 0
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", "rendered")

    make_template(template, rlp_yaml=RLP_YAML.replace("language: python", "language: python3"))
    git(template, "commit", "-qam", "v2")
    git(template, "tag", "v2.0.0")

    result = rlp("update", str(dest), "--defaults", "-o", "json")
    assert result.code == 0, result.stdout + result.stderr
    assert "language: python3" in (dest / "rlp.yaml").read_text(encoding="utf-8")

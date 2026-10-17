# Template conformance

A template is only as good as the repos it renders. Conformance certifies a Copier template: render it with its default answers, run the full golden pipeline on the result, start it locally and smoke-test it. Only a certified template gets a release. See #17.

## The sequence

`rlp template certify [PATH]` runs on a template repo (one with `rlp-template.yaml`) and stops at the first failure:

| Gate | What it checks |
| --- | --- |
| `template` | `rlp-template.yaml` matches the schema and its `conformance.answers` file exists |
| `render` | `copier copy` with those answers into a fresh directory, committed as a new git repo |
| `precommit` … `trivy` | The golden pipeline's gates on the rendered repo, in pipeline order (see [pipeline.md](pipeline.md)); deploy is not part of certification |
| `build` | Any verb in `conformance.verbs` the pipeline doesn't run (today, `build`), per component |
| `run`, `smoke` | `rlp run` and `rlp smoke` on the rendered repo, when `conformance.verbs` lists them |
| `down` | `rlp down -v`; it runs whenever `run` started something, even after a failure |

The template is **certified** when no gate failed and none was skipped with `--skip`. Gates the pipeline skips on its own, such as `sonar` without `SONAR_TOKEN`, don't block certification. The rendered repo is all new code: certify ignores `RLP_BASELINE`, so `sast` scans everything.

```sh
rlp template certify .                               # the working tree, uncommitted changes included
rlp template certify . --vcs-ref v0.2.0              # a tag
rlp template certify . --skip package,trivy,run,smoke --keep   # no Docker here; never certifies
rlp template certify . --report conformance.json -o json
```

| Option | Meaning |
| --- | --- |
| `--vcs-ref REF` | Template git ref to render; default `HEAD`, which includes uncommitted changes |
| `--dest DIR` | Render into this empty directory and keep it; default a temporary directory, removed afterwards |
| `--keep` | Keep the temporary rendered repo |
| `--skip GATE` | Skip a gate (repeatable or comma-separated); the result is then never certified |
| `--trust` | Allow the template's Copier tasks and extensions |
| `--smoke-timeout S` | Seconds to wait for smoke after `run` (default 180) |
| `--report FILE` | Also write the conformance report as JSON |

Exit codes: 0 certified, 1 not certified, 2 usage error (no `rlp-template.yaml`, unknown gate).

## The report

The JSON report (also printed with `-o json`) records what was certified and how each gate went:

```json
{
  "template": "python-fastapi",
  "version": "0.1.0",
  "contract": "rlp/v1alpha1",
  "rlp": "0.2.0",
  "source": "/work/rlp-template-python-fastapi",
  "ref": "HEAD",
  "commit": "0f70b39…",
  "rendered": "/tmp/rlp-certify-python-fastapi-…",
  "certified": true,
  "started": "2026-10-17T12:00:00+00:00",
  "gates": [{"gate": "template", "status": "passed", "detail": "…", "seconds": 0.01, "data": {}}]
}
```

A gate's `status` is `passed`, `failed`, `skipped` or `not run` (an earlier gate failed). `rlp template summary FILE` renders it as Markdown.

## In CI

`.github/workflows/template-conformance.yml` is a reusable workflow that runs the same command. A template repo calls it on every PR, on `main`, on tags and weekly, so new CVEs in the rendered repo show up without a code change:

```yaml
# .github/workflows/conformance.yml in a template repo
name: conformance
on:
  pull_request:
  push:
    branches: [main]
    tags: ["v*"]
  schedule:
    - cron: "17 6 * * 1"
jobs:
  conformance:
    uses: rdlucas2/rlp/.github/workflows/template-conformance.yml@<rlp commit SHA or tag>
    with:
      rlp-version: "git:<the same rlp commit SHA or tag>"
      release: true
    permissions:
      contents: write   # release: publish the GitHub Release of a certified tag
```

| Input | Default | Meaning |
| --- | --- | --- |
| `rlp-version` | required | rlp-cli to install: a PyPI version, `git:<ref>`, or `path:<dir>` in the calling repo |
| `template-repository` | `""` | Template repo to certify as `owner/name`; empty means the calling repo |
| `template-ref` | `""` | Ref of `template-repository` to check out |
| `working-directory` | `.` | Template root inside the template checkout |
| `python-version` | `3.13` | Python used to run rlp |
| `release` | `false` | On a tag push, publish a GitHub Release for the tag once certified |

**The certification marker** is the release: the `release` job runs only after `certify` passed, checks that the report says `certified`, and publishes the tag's GitHub Release with `conformance.json` attached. A tag whose commit fails certification gets no release. Stopping such a tag from being pushed at all is a repository ruleset, which the template repo's owner sets.

The rlp repo runs `template-conformance-selftest.yml`: it certifies the FastAPI template with the rlp in the PR on every PR, on `main`, weekly and on demand, so a CLI or policy change that would break the template fails before it merges.

# The golden pipeline

Every rlp repo runs the same pipeline: `.github/workflows/golden.yml`, a reusable workflow it calls rather than owns. Each step is a thin wrapper around `rlp pipeline run --stage <name>`, so the gate logic lives in the CLI and a laptop, `act` and GitHub Actions produce the same results. See #13.

## Calling it

```yaml
# .github/workflows/pipeline.yml in a project repo
name: pipeline
on:
  pull_request:
  push:
    branches: [main]
    tags: ["v*"]
jobs:
  golden:
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<rlp commit SHA or tag>
    with:
      rlp-version: "0.2.0"
    permissions:
      contents: write          # publish: attach zips and jars to GitHub Releases
      packages: write          # publish: push images to GHCR
      security-events: write   # gates: upload SARIF to code scanning
```

| Input | Default | Meaning |
| --- | --- | --- |
| `rlp-version` | required | rlp-cli to install: a PyPI version (`0.2.0`), `git:<ref>`, or `path:<dir>` inside the checkout |
| `deploy-env` | `""` | Target environment for the deploy stage; empty skips deploy |
| `deploy-local` | `false` | Deploy to LocalStack instead of AWS (for `act`) |
| `components` | `""` | Comma-separated component names; empty means all |
| `working-directory` | `.` | Where the project's `rlp.yaml` lives inside the checkout |
| `python-version` | `3.13` | Python used to run rlp |
| `publish` | `true` | Publish artifacts on pushes to the default branch and on tags |

The workflow sets `RLP_BASELINE` to the PR's base commit (or the commit before a push), so "new code" means what the PR or push changed. The scanners, the baseline and their settings are described in [quality-gates.md](quality-gates.md).

Pin both the workflow ref and `rlp-version`. Third-party actions inside the workflow are pinned by commit SHA.

## Stages

The gates run in this order and stop at the first failure (`rlp pipeline stages` prints the same list):

| Stage | Gate | Fails when |
| --- | --- | --- |
| `precommit` | `pre-commit run --all-files` (if the repo has a config), then the pinned gitleaks over the full git history | a hook fails, or a secret is found anywhere in history |
| `validate` | `rlp validate` | `rlp.yaml` or `rlp-template.yaml` breaks the schema |
| `policy` | `rlp policy check` | the OPA bundle reports a violation |
| `setup` | the setup verb, per component | a component's setup fails |
| `lint` | the lint verb, per component | lint fails |
| `test` | the test verb, per component | a test fails |
| `coverage` | each declared coverage report, in its native format, overall and on new code (diff-cover) | line coverage, or coverage of the changed lines, is below `quality.coverage.minimum` (or the platform floor when unset) |
| `sast` | Semgrep (Community Edition) with the rules for each component's language | an ERROR or WARNING finding is new since the baseline commit |
| `sonar` | optional SonarQube analysis on your machine; not run by the workflow | the quality gate fails; skipped when `SONAR_TOKEN` is unset |
| `package` | the package verb, per component | packaging fails |
| `trivy` | Trivy scan of dependencies and each built image | a HIGH or CRITICAL vulnerability has a fix and no unexpired exception in `.rlp/vex.yaml` |
| `terraform` | Terraform plan checked by OPA | wired in #16; skipped until then |
| `deploy` | deploy to `deploy-env` | wired in #16; skipped until then |

`setup` isn't in the original stage list in #13. It runs before lint because lint and test need the component's dependencies installed.

The pre-commit hook for gitleaks only scans staged changes, which is nothing in CI. That's why `precommit` also runs a full `gitleaks git` scan.

### Results and summaries

Each stage records its result in `.rlp/pipeline/results.json`. `rlp pipeline summary` renders the file as Markdown: one row per stage, then a per-component table for the verb stages. The workflow appends that to the job summary and uploads the JSON as the `rlp-gate-results` artifact for agents and later slices. The scanners also write SARIF and JSON reports to `.rlp/pipeline/reports/`; the workflow uploads the SARIF to code scanning and adds the reports to the same artifact.

### Publishing

The `publish` job runs only on pushes to the default branch and on tags, and only after every gate passes. It is the one place rlp branches on `artifact.kind`:

| Artifact | Destination |
| --- | --- |
| `container-image` | `ghcr.io/<owner>/<repo>/<metadata.name>-<component>:<version>` |
| `zip`, `jar` | attached to the GitHub Release for the pushed tag |
| `static` | published by its static-site deploy (later slice) |

Verbs get `RLP_PROJECT`, `RLP_COMPONENT`, `RLP_VERSION` (the short commit SHA), and `RLP_IMAGE` for images or `RLP_ARTIFACT` for files. So a package task builds `docker build -t "$RLP_IMAGE" .` and never guesses names.

## Running it locally

```sh
rlp pipeline run --reset          # every gate, in order
rlp pipeline run -s test -s coverage
rlp pipeline summary
```

## Running it under act

The repo root carries an `act` profile (`.actrc`) and a secrets template (`.secrets.example`).

```sh
cp .secrets.example .secrets     # optional; the gates need no secrets
act pull_request -W .github/workflows/golden-selftest.yml
```

Under `act`, the deploy stage stays skipped unless the caller sets `deploy-local: true` (or you run `rlp pipeline run -s deploy --deploy-local`). Then it targets LocalStack, never AWS.

### Known not to run the same under act

| Behaviour | Under act | Effect on gate results |
| --- | --- | --- |
| Job summary (`$GITHUB_STEP_SUMMARY`) | written to a file, not rendered | none: the summary step also prints it to the log |
| `actions/upload-artifact` | needs `--artifact-server-path` (set in `.actrc`) | none |
| `publish` job | skipped: act runs `pull_request` by default, and GHCR and Releases need a real token | none: publishing isn't a gate |
| SARIF upload to code scanning | skipped: act has no code scanning API | none: the reports stay in `.rlp/pipeline/reports/` |
| GitHub OIDC (`id-token: write`) | not available | the AWS deploy (#16) can't run; use `deploy-local` |
| `actions/cache` | ephemeral per run | none, only speed |
| Runner image | `catthehacker/ubuntu:act-latest`, not the real `ubuntu-latest` | tool versions can differ; rlp pins conftest, OPA, gitleaks and Trivy itself, and golden.yml pins Semgrep |

The package stage needs Docker. act mounts the host's Docker socket, so image builds work as long as Docker runs locally.

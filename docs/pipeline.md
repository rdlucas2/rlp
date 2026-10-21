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
      # id-token: write        # deploy: only with deploy-env, for AWS through GitHub OIDC
```

| Input | Default | Meaning |
| --- | --- | --- |
| `rlp-version` | required | rlp-cli to install: a PyPI version (`0.2.0`), `git:<ref>`, or `path:<dir>` inside the checkout |
| `deploy-env` | `""` | AWS environment to deploy to (plan on PRs, apply on the default branch); empty skips the `deploy` job. See [aws.md](aws.md) |
| `destroy-env` | `""` | AWS environment to tear down with `rlp infra destroy` instead of running the pipeline: only the `destroy` job runs, and only from a `workflow_dispatch` on the default branch. The platform's retirement workflow dispatches it; see [Teardown runs](#teardown-runs) |
| `deploy-local` | `false` | Target LocalStack instead of AWS (for `act`); the AWS stages skip |
| `aws-region` | `us-east-1` | Region of the environment |
| `aws-plan-role-arn` | `""` | Read-only role pull requests assume through GitHub OIDC to plan |
| `aws-deploy-role-arn` | `""` | Role the default branch assumes through GitHub OIDC to apply |
| `tf-state-bucket` | `""` | Terraform state bucket from the bootstrap stack |

| Secret | Meaning |
| --- | --- |
| `infracost-api-key` | Optional. Infracost API key for the `cost` stage; pass `${{ secrets.INFRACOST_API_KEY }}` |
| `RLP_PLATFORM_TOKEN` | Optional. The rlp platform's API token, for [reporting to the platform](#reporting-to-the-platform); pass `${{ secrets.RLP_PLATFORM_TOKEN }}` |
| `components` | `""` | Comma-separated component names; empty means all |
| `working-directory` | `.` | Where the project's `rlp.yaml` lives inside the checkout |
| `python-version` | `3.13` | Python used to run rlp |
| `publish` | `true` | Publish artifacts on pushes to the default branch and on tags |
| `platform-url` | `""` | rlp platform to report to; empty uses the `RLP_PLATFORM_URL` variable |

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
| `terraform` | `rlp infra render`, `terraform plan`, then the policy bundle's `rlp.terraform` rules on the plan JSON; runs in the `deploy` job | the plan breaks a rule (required tags, public S3, unencrypted storage); skipped without `deploy-env` |
| `cost` | Infracost estimate of that plan's monthly cost and its change against what's deployed; runs in the `deploy` job | the estimate is above `quality.cost.monthlyBudget`, or a budget is declared without `INFRACOST_API_KEY`; skipped with neither |
| `deploy` | `terraform apply` of that saved plan, then each component's readiness path through the load balancer; default branch only | apply fails, the service never gets healthy, or readiness doesn't answer 200 within 5 minutes |

`setup` isn't in the original stage list in #13. It runs before lint because lint and test need the component's dependencies installed.

The pre-commit hook for gitleaks only scans staged changes, which is nothing in CI. That's why `precommit` also runs a full `gitleaks git` scan.

### Test-count and coverage rules

The anti-cheat rules RLP-A005 and RLP-A006 ([policy/README.md](../policy/README.md#agent-authored-changes)) warn when an agent-authored change lowers a component's test count or line coverage. They need two `results.json` files, so the workflow wires them in around the stages:

1. Each push to the default branch whose gates pass saves its `.rlp/pipeline/results.json` to the Actions cache, keyed by the commit (and by `working-directory` and `components`, so callers with several projects don't mix them up).
2. On a pull request, once the coverage stage has run (passed or failed), the workflow restores the base commit's results and runs the `policy` stage again with `RLP_BASE_RESULTS` pointing at them. The head results are the ones this run just wrote.

Their warnings are recorded with the other gate changes in the policy stage's `data.gate_changes` and listed in the summary; like every `rlp.agent` rule they don't fail the stage, and the `gate-review` job holds the pull request for a human's approval ([gate review](quality-gates.md#gate-review)). Pull request reviews re-run the pipeline, so this step runs on `pull_request_review` events too. With no cached results for the base commit (the first run after this lands, or a cache entry evicted after 7 days unused), the step passes, prints a notice, and the policy stage's detail says the two rules were skipped. The cache needs no extra permissions from the caller, unlike reading another run's artifacts, which needs `actions: read`.

Locally, pass both files yourself: `rlp policy check --namespace rlp.agent --base-results <base results.json>`.

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

### Reporting to the platform

When a platform is configured, each run reports to it, so the project record shows what was built, whether it passed, and what runs where ([platform.md](platform.md#builds-and-deployments)):

| Job | Step | Reports |
| --- | --- | --- |
| `gates` | Report the build to the platform | `results.json` as this run attempt's build, passed or failed, with every stage's result |
| `publish` | Report the artifacts to the platform | the published image digests and release asset URLs (from `.rlp/pipeline/published.json`), added to the same build |
| `deploy` | Report the deployment to the platform | each deployed component: environment, image, `succeeded` or `failed`, and when (default branch only) |
| `destroy` | Report the teardown to the platform | each component that deploys to AWS: environment, `destroyed` or `destroy-failed` (teardown runs only) |

To turn it on in a repo, set:

- the **`RLP_PLATFORM_URL` variable** (Settings → Secrets and variables → Actions → Variables), or the workflow's `platform-url` input, to the platform's URL;
- the **`RLP_PLATFORM_TOKEN` secret** to the platform's `RLP_API_TOKEN`, and pass it to the workflow:

```yaml
jobs:
  golden:
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<ref>
    with:
      rlp-version: "0.2.0"
    secrets:
      RLP_PLATFORM_TOKEN: ${{ secrets.RLP_PLATFORM_TOKEN }}
```

Without the URL the report steps are skipped, and the pipeline behaves exactly as before; without the token they print a notice and do nothing. A report never fails the pipeline: the steps run with `continue-on-error`, and `rlp pipeline report` itself exits 0 with a warning when the platform can't be reached. The token is set only on the report steps, never on the steps that run the repo's verbs.

A build is identified by the repo, the commit (a pull request's head commit, not its merge commit), `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT`, so a re-run attempt is a new build and reporting the same attempt twice records one. Locally the run is `local`, attempt 1: `rlp pipeline report --repo OWNER/NAME` with `RLP_PLATFORM_URL` and `RLP_PLATFORM_TOKEN` set sends your last `rlp pipeline run`.

### Teardown runs

The platform's retirement workflow (`rlp lifecycle retire`, [lifecycle.md](lifecycle.md#retirement)) tears a project's environments down through each repo's own pipeline, so the platform never holds AWS credentials. It dispatches the calling workflow (`pipeline.yml` by default) on the default branch with a `destroy-env` input, so the caller must accept one and pass it on:

```yaml
on:
  # ...the triggers above, plus:
  workflow_dispatch:
    inputs:
      destroy-env:
        type: string
        default: ""
jobs:
  golden:
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<ref>
    with:
      rlp-version: "0.2.0"
      destroy-env: ${{ inputs.destroy-env || '' }}
      # deploy-env, the AWS roles and tf-state-bucket as for a deploy
    secrets:
      RLP_PLATFORM_TOKEN: ${{ secrets.RLP_PLATFORM_TOKEN }}
```

With `destroy-env` set, the `gates` job is skipped (and so `gate-review`, `publish` and `deploy`), and the `destroy` job assumes the deploy role through GitHub OIDC, runs `rlp infra destroy --env <destroy-env> --auto-approve`, and reports each component as `destroyed`, or `destroy-failed` when any step before it failed. The report is what lets the retirement go on, so a repo being retired needs reporting turned on (above). The job shares the deploy job's concurrency group, so a teardown never runs alongside a deploy of the same environment.

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

Under `act`, the `deploy` job runs only if the caller sets `deploy-env`. Set `deploy-local: true` too: the AWS stages then skip, because there is no OIDC token under act and LocalStack's free edition has no ECS. `act` never reaches AWS.

### Known not to run the same under act

| Behaviour | Under act | Effect on gate results |
| --- | --- | --- |
| Job summary (`$GITHUB_STEP_SUMMARY`) | written to a file, not rendered | none: the summary step also prints it to the log |
| `actions/upload-artifact` | needs `--artifact-server-path` (set in `.actrc`) | none |
| `publish` job | skipped: act runs `pull_request` by default, and GHCR and Releases need a real token | none: publishing isn't a gate |
| SARIF upload to code scanning | skipped: act has no code scanning API | none: the reports stay in `.rlp/pipeline/reports/` |
| GitHub OIDC (`id-token: write`) | not available | the AWS terraform and deploy stages can't run; use `deploy-local`, which skips them |
| `actions/cache` | ephemeral per run | none, only speed |
| Runner image | `catthehacker/ubuntu:act-latest`, not the real `ubuntu-latest` | tool versions can differ; rlp pins conftest, OPA, gitleaks and Trivy itself, and golden.yml pins Semgrep |

The package stage needs Docker. act mounts the host's Docker socket, so image builds work as long as Docker runs locally.

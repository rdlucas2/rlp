# Quality gates: Sonar, Trivy, gitleaks

Three scanners gate every rlp repo inside the [golden pipeline](pipeline.md). Each runs from the `rlp` CLI, so `rlp pipeline run` on a laptop gives the same result as CI, and each leaves a machine-readable report in `.rlp/pipeline/` for agents (slice 1's remediation loops use them as verifiers). See #14.

| Gate | Stage | Fails when | Reports |
| --- | --- | --- | --- |
| gitleaks | `precommit` | a secret is found anywhere in git history | `reports/gitleaks.sarif`, findings in `results.json` |
| Sonar | `sonar` | the Sonar quality gate fails | the Sonar dashboard; properties in `results.json` |
| Trivy | `trivy` | a vulnerability at HIGH or above has a fix and no unexpired VEX exception | `reports/trivy-fs.{json,sarif}`, `reports/trivy-image-<component>.{json,sarif}` |

Paths are relative to `.rlp/pipeline/`. The golden workflow uploads every SARIF file to GitHub code scanning (each with its own category) and attaches the reports to the `rlp-gate-results` artifact.

## gitleaks

The pinned gitleaks CLI (`rlp_cli/tools.py`) runs in two places:

- **pre-commit**, as a hook, on staged changes. That's right for local commits.
- **the `precommit` stage**, over the full git history, so a secret committed and later deleted still fails.

rlp uses the CLI rather than the gitleaks GitHub Action, which needs a `GITLEAKS_LICENSE` key for repos owned by an organization (free, but a sign-up).

## Sonar

`rlp` maps each component's native coverage report to the Sonar property that reads it, so templates never carry Sonar configuration:

| `reports.coverage.format` | Sonar property |
| --- | --- |
| `cobertura` | `sonar.python.coverage.reportPaths` |
| `lcov` | `sonar.javascript.lcov.reportPaths` |
| `jacoco` | `sonar.coverage.jacoco.xmlReportPaths` |
| `opencover` | `sonar.cs.opencover.reportsPaths` |

The scanner is [pysonar](https://pypi.org/project/pysonar/) (pinned in `golden.yml`), which provisions its own JRE. It runs with `sonar.qualitygate.wait=true`, so the scanner's exit code is the gate.

| Setting | Default | Meaning |
| --- | --- | --- |
| `SONAR_TOKEN` | none | Analysis token. Without it the stage is **skipped** and says so; it never passes silently |
| `SONAR_HOST_URL` | `https://sonarcloud.io` | SonarQube Cloud, or a self-hosted server |
| `SONAR_ORGANIZATION` | the GitHub owner | SonarQube Cloud organization |
| `SONAR_PROJECT_KEY` | `<owner>_<repo>` | The key SonarQube Cloud gives an imported GitHub repo |

In the golden workflow these come from the `SONAR_TOKEN` secret and the `sonar-host-url`, `sonar-organization` and `sonar-project-key` inputs.

### The "rlp default" quality gate

| Condition (on new code) | Fails when |
| --- | --- |
| Issues | more than 0 |
| Line coverage | below the platform floor (`coverage_floor` in `policy/platform/data.yaml`) |

A Sonar quality gate is server-side and shared, so it can't read each repo's `quality.coverage.minimum`. The `coverage` stage enforces the declared minimum on overall coverage; Sonar's gate covers new code.

### Free-tier terms (checked 2026-10-16)

- **SonarQube Cloud, Free plan:** unlimited public projects; private projects up to 50k lines of code per organization; at most 5 members. Only the main branch is analysed, and pull requests only when they target the main branch. **Custom quality gates are not included** (Team plan and above), so on the Free plan "rlp default" can't be created and projects use the built-in "Sonar way" gate, which already fails on any new issue and on new-code coverage below 80%. [Subscription plans](https://docs.sonarsource.com/sonarcloud/administering-sonarcloud/managing-subscription/subscription-plans).
- **SonarQube Community Build (self-hosted fallback):** free, and allows custom quality gates, so "rlp default" can be created there. It analyses a single branch only: no branch analysis, no pull request analysis or decoration (Developer Edition and above). Use it for local and private work, and expect it to judge the main branch, not a PR. Point `SONAR_HOST_URL` at it.
- **Trivy:** Apache-2.0, no account. It downloads its vulnerability database from `mirror.gcr.io` (then `ghcr.io`), which can rate-limit busy runners.
- **gitleaks:** MIT CLI, no account. Only the GitHub Action needs a licence key (for organizations).
- **GitHub code scanning (SARIF upload):** available on public repositories. Private and internal repositories need GitHub Code Security. Two SARIF runs from the same tool need distinct categories, which rlp sets.

## Trivy

The pinned Trivy (`rlp_cli/tools.py`, installed on first use) scans:

1. the repo's dependencies: `trivy fs` at the repo root;
2. each `container-image` component's image, as built by the package stage: `trivy image <metadata.name>-<component>:<version>`.

Both use `--scanners vuln --severity HIGH,CRITICAL --ignore-unfixed`. Any finding left after VEX exceptions fails the stage, which lists each one as `CVE (severity) in package installed-version, fixed in fixed-version [target: file]`.

### VEX exceptions

`.rlp/vex.yaml` lists accepted vulnerabilities:

```yaml
exceptions:
  - id: CVE-2026-0001
    component: urllib3        # optional: only this package; omit for every package with the CVE
    reason: The vulnerable function is never called.
    expires: "2026-12-31"
```

- An unexpired exception lets its CVE pass the trivy stage; the finding stays in the report, marked with the reason.
- An expired exception stops covering its CVE, and the policy stage fails on it (`RLP-L005`). So does an exception without a reason (`RLP-L003`) or a valid expiry (`RLP-L004`).
- Exceptions are a quality gate's configuration: agent-authored changes may not add or extend them.

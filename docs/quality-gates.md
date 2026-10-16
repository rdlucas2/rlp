# Quality gates: Semgrep, diff-cover, Trivy, gitleaks

These scanners gate every rlp repo inside the [golden pipeline](pipeline.md). None of them needs an account or a server. Each runs from the `rlp` CLI, so `rlp pipeline run` on a laptop gives the same result as CI. Each leaves a machine-readable report in `.rlp/pipeline/` for agents; slice 1's remediation loops use these reports as verifiers. See #14.

| Gate | Stage | Fails when | Reports |
| --- | --- | --- | --- |
| gitleaks | `precommit` | a secret is found anywhere in git history | `reports/gitleaks.sarif`, findings in `results.json` |
| coverage (overall) | `coverage` | line coverage is below the declared minimum | `results.json` |
| coverage (new code) | `coverage` | lines changed since the baseline are covered below the declared minimum | `results.json`, with uncovered lines per file |
| Semgrep | `sast` | an ERROR or WARNING finding is new since the baseline | `reports/semgrep.{json,sarif}` |
| Trivy | `trivy` | a vulnerability at HIGH or above has a fix and no unexpired VEX exception | `reports/trivy-fs.{json,sarif}`, `reports/trivy-image-<component>.{json,sarif}` |
| SonarQube (optional) | `sonar` | the Sonar quality gate fails; skipped without `SONAR_TOKEN`, and never run in CI | your SonarQube dashboard |

Report paths are relative to `.rlp/pipeline/`. The golden workflow uploads every SARIF file to GitHub code scanning, each with its own category, and attaches the reports to the `rlp-gate-results` artifact.

## New code and the baseline commit

"New" means changed since the **baseline commit**, which is the merge base of `HEAD` and:

- `$RLP_BASELINE`, when it is set. `golden.yml` sets it to the pull request's base commit, or to the commit before a push. Empty or all zeros, as on the first push of a branch, means there is no baseline.
- otherwise the remote's default branch (`origin/HEAD`, then `origin/main`), so a local run measures the same change a PR would.

With no baseline, the whole repo counts as new. `sast` then reports every finding, and the new-code coverage check is skipped. The baseline must be in the clone, so CI checks out with `fetch-depth: 0`.

## Coverage on new code (diff-cover)

[diff-cover](https://github.com/Bachmann1234/diff_cover) (Apache-2.0) is a Python dependency of rlp. It reads each component's native coverage report and measures the lines changed since the baseline. The minimum is the same one the overall check uses: `quality.coverage.minimum`, or the platform floor when the repo sets none. So each repo's own declared minimum applies to its new code.

| `reports.coverage.format` | New-code check |
| --- | --- |
| `cobertura`, `jacoco`, `lcov` | supported |
| `opencover` | skipped with a note: diff-cover can't read it. .NET templates should emit Cobertura (coverlet does). |

A change with no measured lines, such as docs or tests only, passes.

## Static analysis (Semgrep)

[Semgrep Community Edition](https://github.com/semgrep/semgrep) (LGPL-2.1) runs with rules from a pinned commit of [semgrep-rules](https://github.com/semgrep/semgrep-rules):

- **Semgrep itself** is a Python package. `golden.yml` installs it at the version pinned in `rlp_cli/tools.py` (`SEMGREP_VERSION`), and so does the devcontainer. Locally, run `pip install semgrep==<that version>`. The stage notes it if another version is installed.
- **The rules** are fetched with git at a pinned commit, so a scan needs no Semgrep account, login or registry access, and runs with metrics off. Bumping the commit is a normal reviewed change.

**Rule selection.** The rule directories follow each component's `language`. For example, `python` maps to `python/`, and `typescript` maps to `typescript/` and `javascript/`. Other languages use the directory of the same name. Every repo also gets `dockerfile/` and `yaml/github-actions/`. Rules under `audit/` are left out, because they flag patterns for manual review, not defects.

**What fails.** An ERROR or WARNING finding that is new since the baseline.

**Suppressions.** A `# nosemgrep` comment silences one finding. Like VEX exceptions, suppressions are quality-gate configuration, and agent-authored changes may not add them.

**Licences and limits.**

- The Semgrep CE engine is LGPL-2.1.
- The Semgrep-maintained rules are under the Semgrep Rules License v1.0. It allows internal use but not use in a competing product or paid service. rlp is open source and doesn't sell scanning, so running the rules here is fine. Someone offering rlp as a hosted service would need to check that licence.
- Cross-file (taint) analysis and Semgrep's Pro rules are not in Community Edition.

Code smells and complexity come from each template's `lint` verb (for example ruff for Python), which the `lint` stage already gates.

## Trivy

The pinned Trivy (installed on first use) scans:

1. the repo's dependencies: `trivy fs` at the repo root;
2. each `container-image` component's image, as built by the package stage: `trivy image <metadata.name>-<component>:<version>`.

Both use `--scanners vuln --severity HIGH,CRITICAL --ignore-unfixed`. Any finding left after VEX exceptions fails the stage. The stage lists each finding as `CVE (severity) in package installed-version, fixed in fixed-version [target: path]`.

### VEX exceptions

`.rlp/vex.yaml` lists accepted vulnerabilities:

```yaml
exceptions:
  - id: CVE-2026-0001
    component: urllib3        # optional: only this package; omit for every package with the CVE
    reason: The vulnerable function is never called.
    expires: "2026-12-31"
```

- An unexpired exception lets its CVE pass the trivy stage. The finding stays in the report, marked with the reason.
- An expired exception stops covering its CVE, and the policy stage fails on it (`RLP-L005`).
- The policy stage also fails on an exception without a reason (`RLP-L003`) or without a valid expiry (`RLP-L004`).
- Agent-authored changes may not add or extend exceptions.

## gitleaks

The pinned gitleaks CLI runs in two places:

- **pre-commit**, as a hook, on staged changes. That's right for local commits.
- **the `precommit` stage**, over the full git history, so a secret committed and later deleted still fails.

rlp uses the CLI rather than the gitleaks GitHub Action, which needs a `GITLEAKS_LICENSE` key for repos owned by an organization.

## Optional: SonarQube on your machine

The `sonar` stage is for people who want Sonar's dashboard and history as well. It never gates CI, because the golden workflow doesn't run it. It runs only when `SONAR_TOKEN` is set.

```sh
docker run -d --name sonarqube -p 9000:9000 sonarqube:community   # SonarQube Community Build
# open http://localhost:9000, create a project and a token, then:
pip install pysonar
SONAR_TOKEN=<token> rlp pipeline run -s test -s sonar
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `SONAR_TOKEN` | none | Analysis token; without it the stage is skipped |
| `SONAR_HOST_URL` | `http://localhost:9000` | Your SonarQube. SonarQube Cloud (`https://sonarcloud.io`) also works |
| `SONAR_ORGANIZATION` | the GitHub owner | SonarQube Cloud only |
| `SONAR_PROJECT_KEY` | `metadata.project` (in Actions: `<owner>_<repo>`) | The Sonar project |

rlp maps each native coverage format to the Sonar property that reads it, so templates carry no Sonar configuration:

| `reports.coverage.format` | Sonar property |
| --- | --- |
| `cobertura` | `sonar.python.coverage.reportPaths` |
| `lcov` | `sonar.javascript.lcov.reportPaths` |
| `jacoco` | `sonar.coverage.jacoco.xmlReportPaths` |
| `opencover` | `sonar.cs.opencover.reportsPaths` |

Community Build analyses a single branch, so each local run replaces the last one. That's fine for a personal dashboard, which is all this stage is for.

## Free-tier terms (checked 2026-10-16)

| Tool | Licence | Account | Notes |
| --- | --- | --- | --- |
| Semgrep CE | LGPL-2.1 | none | rules: Semgrep Rules License v1.0 (internal use; no competing product or paid service) |
| diff-cover | Apache-2.0 | none | Python package |
| Trivy | Apache-2.0 | none | downloads its DB from `mirror.gcr.io` (then `ghcr.io`), which can rate-limit busy runners |
| gitleaks | MIT | none | only the GitHub Action needs a licence key, for organizations |
| GitHub code scanning | n/a | n/a | SARIF upload works on public repos; private repos need GitHub Code Security |
| SonarQube Community Build (optional) | free | local only | one branch, no PR analysis or decoration |

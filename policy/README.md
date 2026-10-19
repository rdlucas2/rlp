# policy

The rlp policy bundle: Rego rules that gate contract files, Terraform plans and Dockerfiles. JSON Schema (`schemas/`) is the authoring aid; this bundle is the gate. One bundle, one engine (OPA via conftest), the same result on a laptop and in CI. See #10.

## Layout

| Path | Package | Checks |
| --- | --- | --- |
| `lib/` | `rlp.lib` | Shared helpers, enums and the message format |
| `contract/presence.rego` | `rlp.contract.presence` | Contract files a repo must carry |
| `contract/structure.rego` | `rlp.contract.structure` | Required fields, enums and `apiVersion` (mirrors the JSON Schema) |
| `contract/crossfile.rego` | `rlp.contract.crossfile` | References between `rlp.yaml` and other files |
| `contract/platform.rego` | `rlp.contract.platform` | Platform limits and VEX exceptions |
| `terraform/` | `rlp.terraform` | Terraform plan JSON |
| `container/` | `rlp.container` | Dockerfiles |
| `agent/` | `rlp.agent` | Agent-authored changes that would weaken a gate |
| `platform/data.yaml` | `data.platform` | Coverage floor and required tags (data, not code) |

Every package has a `_test.rego` file with at least one passing and one failing case per rule.

## Running it

Every package reads conftest's combined input, so always pass `--combine` and run from the repo root (paths in messages are relative to it):

```sh
# Whole repo: contract, Dockerfiles, and any plan JSON in the tree
conftest test --combine --all-namespaces -p policy -d policy .

# A Terraform plan on its own
terraform show -json plan.tfplan > plan.json
conftest test --combine --namespace rlp.terraform -p policy -d policy plan.json
```

`rlp policy check` (#11) will wrap these. `task test` runs `opa test policy/` and the fixture repos in `tests/policy/`, using pinned OPA and conftest it installs into `.tools/bin`.

## Messages

Every violation has the form:

```text
[RULE-ID] <file> <field path>: <problem>. Fix: <what to change>.
```

For example: `[RLP-X003] rlp.yaml components[0].config[0].from: refers to service "cache", which components[0].services does not declare. Fix: declare a service named cache in components[0].services, or fix the reference.`

## Rules

| Id | Rule |
| --- | --- |
| RLP-P001 | An app repo has `rlp.yaml` at its root |
| RLP-P002 | A repo with `rlp.yaml` has a Taskfile at its root |
| RLP-P003 | `.copier-answers.yml` is present when `metadata.template` is set |
| RLP-P004 | A Copier template repo has `rlp-template.yaml` |
| RLP-P005 | `rlp-template.yaml` sets `conformance.answers` |
| RLP-P006 | The `conformance.answers` file exists |
| RLP-S001 | `apiVersion` is `rlp/v1alpha1` |
| RLP-S002 | `kind` is `Repo` in `rlp.yaml`, `Template` in `rlp-template.yaml` |
| RLP-S003 | Required fields are present |
| RLP-S004 | Artifact, runtime, coverage-format and service kinds are known values |
| RLP-S005 | A `container-service` has a `port`; a `function` has a `handler` |
| RLP-X001 | Every verb a component runs with go-task exists in its Taskfile |
| RLP-X002 | A component outside the root that uses go-task has its own Taskfile |
| RLP-X003 | Every `config.from: service:<name>` names a service the component declares |
| RLP-X004 | Component names are unique |
| RLP-L001 | `quality.coverage.minimum` is at least the platform floor |
| RLP-L002 | A `secret: true` config entry has no `default` |
| RLP-L003 | Every VEX exception has a `reason` |
| RLP-L004 | Every VEX exception has a valid `expires` date |
| RLP-L005 | No VEX exception has expired |
| RLP-T001 | Taggable resources carry the required tags (`tags` or provider `default_tags`) |
| RLP-T002 | No public S3: no public ACLs, every public access block setting true, a public access block whenever the plan creates a bucket |
| RLP-T003 | Encryption at rest for RDS, EBS, EFS, ElastiCache, SQS and SNS (S3 encrypts by default) |
| RLP-D001 | Base images are pinned to a tag other than `latest`, or a digest |
| RLP-D002 | The final stage runs as a non-root `USER` |
| RLP-A001 | An agent-authored change doesn't touch a protected gate file: `policy/`, `.github/workflows/`, `.rlp/vex.yaml`, `.pre-commit-config.yaml`, Semgrep, Trivy or coverage settings |
| RLP-A002 | An agent-authored change doesn't modify the `quality` block of `rlp.yaml` |
| RLP-A003 | An agent-authored change doesn't modify the test verb: the Taskfile `test` task or a component's `verbs.test` |
| RLP-A004 | An agent-authored change adds no suppression comments (`nosemgrep`, `NOSONAR`, `noqa`, `type: ignore`, `pragma: no cover`, `eslint-disable` and equivalents) |
| RLP-A005 | An agent-authored change doesn't lower a component's test count (base and head results) |
| RLP-A006 | An agent-authored change doesn't lower a component's line coverage (base and head results) |
| RLP-A007 | An agent-authored change doesn't remove more test definitions than it adds |

## Agent-authored changes

"Verifiers the agent can't change without sign-off" (docs/PLAN.md): the `rlp.agent` rules flag an agent-authored pull request that changes a gate. They are conftest `warn` rules, not `deny` rules, so an agent can make a gate change an issue calls for (removing a flaky test, say), and a human reviews each one before merge (#73). `rlp policy check` prints them as warnings and exits 0 when they are its only findings; the policy stage records them in `results.json` under `data.gate_changes`, and `rlp pipeline summary` lists them under "Gate changes needing review". The A-rule rows in the table above describe what each rule checks; a breach is a warning, not a failure. A change is agent-authored when it carries the `agent:authored` label or its author is in `agent_authors` in `platform/data.yaml` (the agent identity, #51, adds its login there). The same change from a human passes these rules.

The rules don't read repo files. `rlp policy check` and the policy stage add one more document to conftest's input, `rlp_change` (built by `cli/src/rlp_cli/change.py`):

| Field | From |
| --- | --- |
| `author`, `labels` | `$RLP_PR_AUTHOR` and `$RLP_PR_LABELS` (comma-separated) when set, otherwise the `pull_request` event at `$GITHUB_EVENT_PATH` |
| `files` | Every file changed since the baseline commit (`$RLP_BASELINE`, see `baseline.py`), with its added lines and removed lines; untracked files count as added |
| `contracts` | `rlp.yaml` and Taskfiles in the diff, parsed before and after |
| `results` | Test counts and line coverage per component from a base and a head `results.json`: `--base-results` or `$RLP_BASE_RESULTS`, and `--head-results` (default `.rlp/pipeline/results.json`) |

Without a baseline there is no diff, so only the results rules can fire. The policy stage runs before the test stage, so at first RLP-A007 guards the test count from the diff. On pull requests the golden pipeline runs the policy stage again after the coverage stage, with `$RLP_BASE_RESULTS` set to the base commit's `results.json` (kept in the Actions cache by each push to the default branch), so RLP-A005 and RLP-A006 compare the two and their warnings join `data.gate_changes`. With no base results, a first run for example, that run passes and its detail says the two rules were skipped. See [docs/pipeline.md](../docs/pipeline.md#test-count-and-coverage-rules).

## VEX exceptions

Accepted vulnerabilities live in `.rlp/vex.yaml` at the repo root. The trivy stage of the golden pipeline reads the same file ([docs/quality-gates.md](../docs/quality-gates.md)):

```yaml
exceptions:
  - id: CVE-2026-0001
    component: urllib3      # optional: only this package
    reason: The vulnerable function is never called from our code.
    expires: "2026-12-31"   # valid through this day, then the gate fails again
```

## Distribution (proposed)

Slice 0 distributes the bundle as a **pinned git ref**: consumers run `conftest pull 'git::https://github.com/rdlucas2/rlp.git//policy?ref=<tag>'`, or the golden pipeline checks out rlp at that tag. It needs no registry, credentials or publish step. Publishing an OCI artifact to GHCR (`conftest push`) can follow once rlp cuts releases. Pending Ryan's confirmation in #10.

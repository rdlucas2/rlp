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

## VEX exceptions

Accepted vulnerabilities live in `.rlp/vex.yaml` at the repo root. The quality gates (#14) read the same file:

```yaml
exceptions:
  - id: CVE-2026-0001
    reason: The vulnerable function is never called from our code.
    expires: "2026-12-31"   # valid through this day, then the gate fails again
```

## Distribution (proposed)

Slice 0 distributes the bundle as a **pinned git ref**: consumers run `conftest pull 'git::https://github.com/rdlucas2/rlp.git//policy?ref=<tag>'`, or the golden pipeline checks out rlp at that tag. It needs no registry, credentials or publish step. Publishing an OCI artifact to GHCR (`conftest push`) can follow once rlp cuts releases. Pending Ryan's confirmation in #10.

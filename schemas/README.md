# schemas

JSON Schemas (draft 2020-12) for the rlp contract kinds, versioned by `apiVersion`. See #9.

| Kind | File | Schema |
| --- | --- | --- |
| `Repo` | `rlp.yaml` at an app repo's root | [`v1alpha1/repo.schema.json`](v1alpha1/repo.schema.json) |
| `Template` | `rlp-template.yaml` at a template repo's root | [`v1alpha1/template.schema.json`](v1alpha1/template.schema.json) |
| `LoopState` | `.rlp/loops/<kind>-<run-id>.json`, written by `rlp loop <kind>` | [`v1alpha1/loop-state.schema.json`](v1alpha1/loop-state.schema.json) |

The two contract files start with `apiVersion: rlp/v1alpha1`; any other value is rejected. Worked slice-0 examples are in [`examples/`](../examples).

## Editor support

Each schema's `$id` is its raw URL on `main`. Put this first line in a contract file and the YAML language server (VS Code YAML extension, and others) resolves it for completion and inline errors:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/rdlucas2/rlp/main/schemas/v1alpha1/repo.schema.json
```

## Repo contract in brief

- `metadata`: `name`, `project` (the project record id), optional `owners` and `template` (Copier `source` and `version`).
- `components[]`: one entry per buildable unit, each with `name`, `path`, `language`, `artifact`, `runtime`, `verbs`, `reports`, `config`, `services` and `telemetry`.
- Artifact kinds: `container-image`, `zip`, `jar`, `static`. `zip`, `jar` and `static` need `artifact.path`.
- Runtime kinds: `container-service` (needs `port`), `function` (needs `handler`), `static-site`, `job`.
- Coverage formats: `cobertura`, `lcov`, `jacoco`, `opencover`. Service kinds: `postgres`, `redis`, `s3`, `sqs`, `sns`.
- **Verb defaults:** when `verbs.<name>` is absent, the verb runs `task <name>` in the component's `path`. Set it to a command line to override.
- `quality.coverage.minimum`: a percentage; policy (#10) enforces the platform floor.
- `quality.cost.monthlyBudget`: USD per month per environment; the pipeline's `cost` stage fails an AWS plan whose Infracost estimate is above it (#39).
- `loops.enabled`: opt the repo in to remediation loops (`rlp loop <kind>`); off when absent. `loops.kinds` limits them to some of `quality` and `vuln`. `RLP_LOOPS=off` in the environment turns them off regardless (see [docs/loops.md](../docs/loops.md)).

Rules that span files or need platform context (for example, that `config.from: service:db` names a declared service, or that `secret: true` entries carry no `default`) belong to the OPA bundle in `policy/`, not here.

## Tests

`task test` runs `schemas/tests/` with pytest. Fixtures live in `tests/fixtures/<kind>/{valid,invalid}/`. Every invalid fixture starts with an `# expect-error: <JSON pointer>` comment naming the one path its errors must point at (`(root)` for the document itself).

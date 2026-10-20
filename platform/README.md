# rlp-platform

A FastAPI service rendered from the [rlp python-fastapi template](https://github.com/rdlucas2/rlp-template-python-fastapi). Its contract is [`rlp.yaml`](rlp.yaml); the golden pipeline gates every change.

## Endpoints

| Path | Purpose |
| --- | --- |
| `/healthz` | Liveness: the process is up |
| `/readyz` | Readiness: `DATABASE_URL`, when set, is reachable |
| `/hello/{name}` | Sample route |
| `/api/v1/...` | The project record and lifecycle moves, behind a bearer token; spec in [`openapi.json`](openapi.json), guide in [docs/platform.md](../docs/platform.md) |

## Configuration

Only environment variables, as declared under `config` in `rlp.yaml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8000` | Port to listen on |
| `HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Interface to listen on |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` or `critical` |
| `DATABASE_URL` | unset | Postgres connection string, provided by the `db` service |
| `RLP_API_TOKEN` | unset | Bearer token for `/api/v1`; the service won't start without it unless `RLP_API_TOKEN_DISABLED=true` |
| `RLP_API_TOKEN_DISABLED` | `false` | `true` serves `/api/v1` without a token, for local development |
| `RLP_MIGRATE_ON_START` | `false` | `true` applies pending migrations before the service starts; needs `DATABASE_URL` |
| `OTEL_*` | unset | Standard OpenTelemetry settings, for example `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SERVICE_NAME` |

Logs are JSON, one object per line on stdout, with `trace_id` and `span_id` when a span is active.

## Project record

The platform keeps the project record in Postgres (17 locally, RDS in AWS). The models are in [`src/rlp_platform/models.py`](src/rlp_platform/models.py); typed create, get, list and update functions for each table are in [`src/rlp_platform/store/`](src/rlp_platform/store/).

| Table | Purpose |
| --- | --- |
| `projects` | The root record: name (unique), description, owner, lifecycle phase, dashboard URL |
| `repos` | A git repository (full name, unique) linked to its project by `metadata.project` in its `rlp.yaml`, with its default branch, template source and version, and the last indexed commit |
| `components` | A component a repo's `rlp.yaml` declares: name (unique per repo), language, artifact kind, runtime kind |
| `artifacts` | What a component's `package` verb produced: kind, reference (an image digest, a zip URI), created and expiry times |
| `builds` | One pipeline run on a repo commit: branch, workflow run URL, each stage's result, whether it passed, start and finish times |
| `deployments` | An artifact of a component deployed to an environment, with its status and time |
| `lifecycle_transitions` | Each phase change of a project: from and to phase, actor, reason, the entry rules' results, time |

The phase is a Postgres enum: `proposed`, `designed`, `scaffolded`, `in-dev`, `live`, `deprecated`, `retired`. Every foreign key is `RESTRICT`, so deleting a record that others point at (a project that still has repos, say) fails instead of cascading.

Migrations are Alembic revisions in [`migrations/`](migrations/), shipped inside the package so the image can run them:

```sh
python -m rlp_platform migrate            # apply pending migrations to $DATABASE_URL
python -m rlp_platform migrate --check    # also fail if the models and migrations disagree
```

After changing a model, add a revision and commit it with the change; `migrate --check` and the tests fail until you do. The tests start Postgres 17 with testcontainers, so they need Docker; set `RLP_TEST_DATABASE_URL` to use a server you already run instead (the tests empty its `public` schema).

## Lifecycle

A project moves through `proposed`, `designed`, `scaffolded`, `in-dev`, `live`, `deprecated` and `retired`. [`rlp_platform.lifecycle.transition()`](src/rlp_platform/lifecycle.py) checks each move against the Rego entry rules in [`src/rlp_platform/policy/lifecycle/`](src/rlp_platform/policy/lifecycle/) (a copy of `policy/lifecycle/` at the rlp repo root) with `opa eval`, then sets the phase and records a `lifecycle_transitions` row with the rule results, or writes nothing and returns the failing rules. The image ships the pinned OPA at `/usr/local/bin/opa` ([`src/rlp_platform/opa.py`](src/rlp_platform/opa.py)); the tests use the `opa` on `PATH`, or download the pinned one. The phases, moves and rules are in the rlp repo's [docs/lifecycle.md](https://github.com/rdlucas2/rlp/blob/main/docs/lifecycle.md).

## Standard verbs

```sh
rlp setup     # uv sync --locked
rlp lint      # ruff check and ruff format --check
rlp test      # pytest; reports/junit.xml and reports/coverage.xml
rlp build     # byte-compile src/
rlp package   # docker build
rlp run       # start the service on $PORT
rlp smoke     # check /healthz and /readyz of a running instance
```

`rlp pipeline run` runs every gate locally, exactly as `.github/workflows/pipeline.yml` does in CI. Update the repo to a newer template version with `rlp update`.

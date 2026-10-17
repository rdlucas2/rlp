# rlp-platform

A FastAPI service rendered from the [rlp python-fastapi template](https://github.com/rdlucas2/rlp-template-python-fastapi). Its contract is [`rlp.yaml`](rlp.yaml); the golden pipeline gates every change.

## Endpoints

| Path | Purpose |
| --- | --- |
| `/healthz` | Liveness: the process is up |
| `/readyz` | Readiness: `DATABASE_URL`, when set, is reachable |
| `/hello/{name}` | Sample route |

## Configuration

Only environment variables, as declared under `config` in `rlp.yaml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8000` | Port to listen on |
| `HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Interface to listen on |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` or `critical` |
| `DATABASE_URL` | unset | Postgres connection string, provided by the `db` service |
| `OTEL_*` | unset | Standard OpenTelemetry settings, for example `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SERVICE_NAME` |

Logs are JSON, one object per line on stdout, with `trace_id` and `span_id` when a span is active.

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

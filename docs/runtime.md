# The local runtime

`rlp run` starts every component in `rlp.yaml`, with its backing services, as a Docker Compose environment on your machine. `rlp smoke` checks that each component answers, and `rlp down` removes everything again. It needs Docker with Compose v2 (`docker compose`). See #15.

```sh
rlp run          # generate .rlp/compose.yaml, build, start, wait until smoke passes
rlp smoke        # check every component again
rlp down         # stop and remove the containers; add -v to delete database volumes
```

`rlp run --native` and `rlp smoke --native` run the component's own `run` and `smoke` verbs (`verbs.<name>`, else `task <name>`) instead, without Docker.

## What `rlp run` does

1. Reads `rlp.yaml`, collects secrets (below) and plans one Compose service per component and per backing service.
2. Runs the `package` verb for zip and jar functions, then unpacks each archive into `.rlp/functions/<component>/`. `--no-package` uses the archive already built.
3. Writes these files. `.rlp/` is gitignored in every rlp repo; `rlp run` warns if git would track them.

   | File | Contents |
   | --- | --- |
   | `.rlp/compose.yaml` | The Compose project, rewritten on every run |
   | `.rlp/.env` | The Compose project name and generated local credentials, such as each Postgres password |
   | `.rlp/secrets/<component>.env` | That component's secret config values (mode 600) |
   | `.rlp/otel-collector.yaml` | The local OpenTelemetry collector's config |
   | `.rlp/localstack/ready.d/rlp-init.sh` | Creates the declared S3 buckets, SQS queues and SNS topics |

4. `docker compose up --detach --build`, then polls the smoke checks until they pass or `--timeout` (180 seconds) runs out.

`--no-start` stops after step 3, which is useful to read the generated Compose file.

## Runtime mapping

| `runtime.kind` | `artifact.kind` | Local service | Host port | Smoke check |
| --- | --- | --- | --- | --- |
| `container-service` | `container-image` | built from `artifact.dockerfile` | `runtime.port` | `GET` liveness and readiness paths answer 200 (any answer below 500 when none is declared) |
| `function` | `zip`, `jar` | AWS Lambda base image for `language`, archive mounted at `/var/task`, `runtime.handler` as the command | 9000 | `POST {}` to the Runtime Interface Emulator answers 200 with no function error |
| `function` | `container-image` | built from the Dockerfile, which must start from a Lambda base image | 9000 | as above |
| `static-site` | `static` | nginx serving `artifact.path` | `runtime.port`, else 8080 | `GET /` answers 200 |
| `static-site` | `container-image` | built from the Dockerfile, serving on `runtime.port` (default 80) | `runtime.port`, else 8080 | `GET /` answers 200 |
| `job` | `container-image` | built, run once, not restarted | none | the container exited 0 |

Ports bind to `127.0.0.1` only. When two components want the same host port, the later one gets the next free number; `rlp run` prints the URLs it checks.

Lambda base images by `language`: `python` → `public.ecr.aws/lambda/python:3.13`; `javascript`, `typescript`, `nodejs` → `public.ecr.aws/lambda/nodejs:22`; `java` → `public.ecr.aws/lambda/java:21`. Other languages should package the function as a container image.

## Backing services

| `services[].kind` | Local container | Compose host name | `config.from: service:<name>` resolves to |
| --- | --- | --- | --- |
| `postgres` | `postgres:<version>` (default 17), healthchecked, data in a named volume | `<component>-<name>` | `postgresql://app:<password>@<component>-<name>:5432/app` |
| `redis` | `redis:<version>` (default 7.4), healthchecked | `<component>-<name>` | `redis://<component>-<name>:6379/0` |
| `s3` | one shared LocalStack container | `localstack` | the bucket name, `<metadata.name>-<name>` |
| `sqs` | LocalStack | `localstack` | the queue URL |
| `sns` | LocalStack | `localstack` | the topic ARN |

A component starts only after its services are healthy. A component that uses LocalStack also gets `AWS_ENDPOINT_URL`, `AWS_REGION` and dummy credentials, so AWS SDKs talk to LocalStack.

The Postgres password is generated on the first run and kept in `.rlp/.env`, so the database volume keeps working across runs. `rlp down -v` deletes the volume.

**LocalStack licensing.** LocalStack images from release 2026.03 on need a LocalStack account token. rlp pins `localstack/localstack:4.9`, which doesn't. To use a newer image, set `LOCALSTACK_AUTH_TOKEN` in your environment; `rlp run` passes it through.

## Config and secrets

Each entry in a component's `config` becomes an environment variable:

| Entry | Value |
| --- | --- |
| `from: service:<name>` | the connection string above |
| `default` | the default |
| `secret: true` | from `.rlp/secrets/<component>.env`, else your environment variable of the same name, else a hidden prompt. The value is stored so you're asked once. Never written into `compose.yaml` |
| `required: true`, no default | your environment variable of the same name; `rlp run` fails without it |
| anything else | your environment variable of the same name, if set |

In JSON mode (`-o json`) and when stdin is not a terminal, `rlp run` never prompts: a required secret must come from the stored file or the environment.

## Telemetry

When a component has `telemetry.enabled` (the default), `rlp run` adds an OpenTelemetry collector container that logs what it receives, and gives the component:

- `OTEL_SERVICE_NAME`: `telemetry.serviceName`, else `<metadata.name>-<component>`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=local`

The collector also listens on `127.0.0.1:4317` and `4318`, so a component started with `--native` can export to it. Watch it with `docker compose -p rlp-<metadata.name> logs -f otel-collector`. Dashboards come in slice 4.

## Pinned images

| Image | Pin |
| --- | --- |
| OpenTelemetry collector | `otel/opentelemetry-collector:0.130.0` |
| nginx | `nginx:1.29-alpine` |
| LocalStack | `localstack/localstack:4.9` |

The pins live in `cli/src/rlp_cli/runtime.py`; bumping one is a reviewed change.

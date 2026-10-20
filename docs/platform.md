# Platform API and CLI

The platform service (`platform/`) keeps the project record in Postgres and serves it over a versioned REST API under `/api/v1`. The `rlp` CLI's `project`, `repo` and `lifecycle` commands call that API, so anything the UI or a workflow does is scriptable. See #92.

## Running the service

The service reads its settings from environment variables, declared under `config` in `platform/rlp.yaml`:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | The Postgres store. Without it every `/api/v1` route answers 503 |
| `RLP_API_TOKEN` | The bearer token every `/api/v1` request must carry (a `secret: true` entry) |
| `RLP_API_TOKEN_DISABLED` | `true` serves `/api/v1` without a token. For local development only |
| `RLP_MIGRATE_ON_START` | `true` applies pending migrations at startup |

The service refuses to start without `RLP_API_TOKEN` unless `RLP_API_TOKEN_DISABLED=true`, and logs a warning when the API is open. Generate a token with, for example, `python -c "import secrets; print(secrets.token_urlsafe(32))"`. `rlp run` asks for it once and keeps it in `.rlp/secrets/platform.env`; in AWS it is a Secrets Manager entry an operator fills.

```sh
cd platform
export DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app RLP_API_TOKEN=... RLP_MIGRATE_ON_START=true
uv run python -m rlp_platform
```

## Authentication

rlp is single-user until slice 6, so one token stands in for users and roles. Every `/api/v1` route needs `Authorization: Bearer <token>`; the service compares it with `RLP_API_TOKEN` in constant time and answers 401 with `WWW-Authenticate: Bearer` when it is missing or wrong. With no token configured and the API not disabled, every request answers 401. `/healthz` and `/readyz` stay open.

## Routes

The spec is committed as [`platform/openapi.json`](../platform/openapi.json). A test fails when it differs from the running app; after changing a route, regenerate it:

```sh
cd platform && uv run python -m rlp_platform openapi > openapi.json
```

| Method and path | Answers |
| --- | --- |
| `GET /api/v1/projects?phase=` | Projects by name, optionally only those in one phase |
| `POST /api/v1/projects` | Create a project (`name`, `owner`, `description`, `dashboard_url`); it starts `proposed`. 201, or 409 when the name is taken |
| `GET /api/v1/projects/{name}` | One project |
| `PATCH /api/v1/projects/{name}` | Change `owner`, `description` or `dashboard_url`; fields left out keep their value. The phase changes only through transitions |
| `GET /api/v1/projects/{name}/repos` | The project's repos |
| `GET /api/v1/projects/{name}/components` | Every component of the project's repos |
| `GET /api/v1/projects/{name}/builds?limit=` | The project's builds, newest first (default 100, at most 1000) |
| `GET /api/v1/projects/{name}/deployments?limit=` | The project's deployments, newest first |
| `GET /api/v1/projects/{name}/transitions` | The project's phase history, oldest first |
| `POST /api/v1/projects/{name}/transitions` | Move the project (`to`, `reason`, `actor`); see below |
| `GET /api/v1/repos?project=` | Repos by full name, optionally one project's |
| `GET /api/v1/repos/{owner}/{name}` | One repo |
| `GET /api/v1/repos/{owner}/{name}/components` | The repo's components |
| `GET /api/v1/repos/{owner}/{name}/builds?limit=` | The repo's builds, newest first |
| `GET /api/v1/repos/{owner}/{name}/deployments?limit=` | The repo's deployments, newest first |

Repos, components, builds and deployments are read-only here: the repo indexer (#93) and build ingestion (#94) write them. An unknown project or repo answers 404 with `{"detail": "..."}`; a malformed body answers 422.

### Moving a project

`POST /api/v1/projects/{name}/transitions` runs the lifecycle's entry rules ([lifecycle.md](lifecycle.md)). When every rule passes it sets the phase, records the transition and answers 201 with it:

```json
{"from_phase": "in-dev", "to_phase": "live", "actor": "ryan", "reason": "launch", "rule_results": [...], "at": "..."}
```

When a rule fails it changes nothing and answers 409 with the failing rules and every rule's result:

```json
{
  "detail": "orders: in-dev -> live failed RLP-L105, RLP-L106",
  "project": "orders", "from_phase": "in-dev", "to_phase": "live",
  "failures": [{"rule": "RLP-L105", "description": "live needs an owner", "passed": false, "messages": ["[RLP-L105] ..."]}, ...],
  "results": [...]
}
```

A move to an unknown phase fails rule RLP-L101 the same way. If the rules can't be evaluated (no `opa`), it answers 503.

## CLI

### Where the CLI finds the platform

The CLI reads the platform URL and token from, in order:

1. `RLP_PLATFORM_URL` and `RLP_PLATFORM_TOKEN`
2. `~/.config/rlp/config.yaml` (`$XDG_CONFIG_HOME/rlp/config.yaml` when that is set):

   ```yaml
   platform:
     url: http://127.0.0.1:8000
     token: ...
   ```

Each value is looked up on its own, so the environment can override just one. The file holds a secret: on Linux and macOS the CLI reads a token from it only when its mode is 600 (`chmod 600 ~/.config/rlp/config.yaml`). `rlp bootstrap` (#98) will write it.

`rlp config show` prints both values and where each came from, with the token masked to its last four characters.

### Commands

Every command takes `--output json` (`-o json`): stdout is then one JSON document with `"ok"`.

| Command | Does |
| --- | --- |
| `rlp config show` | The platform URL and masked token, and their sources |
| `rlp project list [--phase PHASE]` | List projects |
| `rlp project show NAME` | A project and its repos |
| `rlp project create NAME [--owner O] [--description D] [--dashboard-url U]` | Create a project in `proposed` |
| `rlp project update NAME [--owner O] [--description D] [--dashboard-url U]` | Change those fields; `--dashboard-url ""` clears it |
| `rlp repo list [--project NAME]` | List repos |
| `rlp repo show OWNER/NAME` | A repo and its components |
| `rlp lifecycle show PROJECT` | The project's phase and its history |
| `rlp lifecycle move PROJECT PHASE --reason TEXT` | Move the project; recorded with your login name and the reason |

Exit codes follow the rest of the CLI: 0 success; 1 when the platform says no (a rejected move, an unknown project, a name already taken); 2 for setup problems (no URL or token, the platform unreachable, a refused token).

A rejected move exits 1 and prints each failing rule's message:

```text
$ rlp lifecycle move orders live --reason launch
orders: in-dev -> live rejected
  [RLP-L105] rlp-transition.json project.owner: project "orders" has no owner, which live needs. Fix: set the project's owner.
  [RLP-L106] rlp-transition.json project.dashboard_url: ...
```

The client is `rlp_cli.platform_api.PlatformClient`, a thin httpx wrapper with typed results; no generated code is checked in.

# Platform API and CLI

The platform service (`platform/`) keeps the project record in Postgres and serves it over a versioned REST API under `/api/v1`. The `rlp` CLI's `project`, `repo` and `lifecycle` commands call that API, so anything the UI or a workflow does is scriptable. The repo indexer keeps repos and components in step with GitHub (see [The repo indexer](#the-repo-indexer)). See #92 and #93.

## Running the service

The service reads its settings from environment variables, declared under `config` in `platform/rlp.yaml`:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | The Postgres store. Without it every `/api/v1` route answers 503 |
| `RLP_API_TOKEN` | The bearer token every `/api/v1` request must carry (a `secret: true` entry) |
| `RLP_API_TOKEN_DISABLED` | `true` serves `/api/v1` without a token. For local development only |
| `RLP_MIGRATE_ON_START` | `true` applies pending migrations at startup |
| `RLP_GITHUB_WEBHOOK_SECRET` | The secret GitHub signs webhook deliveries with (a `secret: true` entry). Without it every delivery answers 401 |
| `RLP_GITHUB_TOKEN` | The token the indexer reads repos with (a `secret: true` entry). Without it only public repos can be read. The retirement workflow and `expire-artifacts` also use it to dispatch teardowns, archive repos and delete artifacts; see [lifecycle.md](lifecycle.md#what-an-administrator-grants) |
| `RLP_GITHUB_REPOS` | The repos `sync` re-indexes, `OWNER/NAME` separated by commas. Unset: every repo of the token's installation |
| `RLP_GITHUB_API_URL` | The GitHub REST API; default `https://api.github.com`. Change it for GitHub Enterprise Server |

The service refuses to start without `RLP_API_TOKEN` unless `RLP_API_TOKEN_DISABLED=true`, and logs a warning when the API is open. Generate a token with, for example, `python -c "import secrets; print(secrets.token_urlsafe(32))"`. `rlp run` asks for it once and keeps it in `.rlp/secrets/platform.env`; in AWS it is a Secrets Manager entry an operator fills.

```sh
cd platform
export DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app RLP_API_TOKEN=... RLP_MIGRATE_ON_START=true
uv run python -m rlp_platform
```

## Inventory UI

The platform serves a React UI (`platform/ui/`) at `/`, built into the image; the API stays under `/api`. The inventory lists every project with its owner, phase, repo count, latest build and last deployment, filterable by phase and searchable by name; the filter is in the URL (`/?phase=in-dev&q=orders`), so a reload or a shared link keeps it. A project's page (`/projects/{name}`) shows its description, owner and dashboard link, its lifecycle history with each move's rule results (rejected moves too), its repos with their components and template versions, the latest build per repo with a link to the run, and the current deployment per environment.

The UI reads only the API, so everything it shows is also available through `rlp`. It asks for `RLP_API_TOKEN` once per browser session and keeps it in `sessionStorage`; a 401 sends it back to the token prompt. See `platform/README.md` for working on it.

## Authentication

rlp is single-user until slice 6, so one token stands in for users and roles. Every `/api/v1` route but the GitHub webhook needs `Authorization: Bearer <token>`; the service compares it with `RLP_API_TOKEN` in constant time and answers 401 with `WWW-Authenticate: Bearer` when it is missing or wrong. With no token configured and the API not disabled, every request answers 401. `/healthz` and `/readyz` stay open. `POST /api/v1/hooks/github` takes no bearer token (GitHub can't send one): it checks each delivery's `X-Hub-Signature-256` instead.

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
| `GET /api/v1/projects/{name}/components?include_removed=` | Every component of the project's repos; `include_removed=true` adds those that left `rlp.yaml` |
| `GET /api/v1/projects/{name}/builds?limit=` | The project's builds, newest first (default 100, at most 1000) |
| `GET /api/v1/projects/{name}/deployments?limit=` | The project's deployments, newest first |
| `GET /api/v1/projects/{name}/transitions` | The project's phase history, oldest first; `include_rejected=true` adds the moves an entry rule rejected (`accepted: false`) |
| `POST /api/v1/projects/{name}/transitions` | Move the project (`to`, `reason`, `actor`); see below |
| `GET /api/v1/projects/{name}/retirement` | The project's retirement and each step's status; 404 when none was started |
| `POST /api/v1/projects/{name}/retirement` | Start or resume the retirement of a `deprecated` project (`actor`, `reason`, `artifact_retention_days`, `workflow`); `dry_run: true` answers the plan. 409 when the project isn't deprecated. See [lifecycle.md](lifecycle.md#retirement) |
| `GET /api/v1/repos?project=` | Repos by full name, optionally one project's |
| `GET /api/v1/repos/{owner}/{name}` | One repo |
| `GET /api/v1/repos/{owner}/{name}/components?include_removed=` | The repo's components; `include_removed=true` adds those that left `rlp.yaml` |
| `GET /api/v1/repos/{owner}/{name}/builds?limit=` | The repo's builds, newest first |
| `GET /api/v1/repos/{owner}/{name}/deployments?limit=` | The repo's deployments, newest first |
| `POST /api/v1/builds` | Record a pipeline run attempt's gate results and artifacts (`rlp pipeline report`); see [Builds and deployments](#builds-and-deployments). 201, or 200 when the attempt was reported before |
| `POST /api/v1/deployments` | Record a component deployed to an environment (`rlp pipeline report --deployment`), or torn down (`--teardown`). 201, 200 when reported before, 404 when the repo or component isn't indexed, 422 for a report with no artifact about a component never deployed there |
| `POST /api/v1/hooks/github` | GitHub's `push` and `repository` webhook events, signed; see [The repo indexer](#the-repo-indexer) |
| `POST /api/v1/sync` | Re-index one repo (`{"repo": "OWNER/NAME"}`) or, with no body, every repo the platform can see |

Repos and components are read-only here: the repo indexer writes them. Builds and deployments are written only by the pipeline's reports. A repo carries `index_error` (why its last index failed, or null) and a component carries `removed_at` (when it left `rlp.yaml`, or null). An unknown project or repo answers 404 with `{"detail": "..."}`; a malformed body answers 422.

### Builds and deployments

The golden pipeline reports each run to the platform with `rlp pipeline report` ([pipeline.md](pipeline.md#reporting-to-the-platform)).

- **A build is one run attempt**: (repo, commit, `run_id`, `run_attempt`) identifies it, so reporting the same attempt again updates that build instead of adding one. Each report's stage results (the entries of `.rlp/pipeline/results.json`) merge into the build by stage name, so the gates and publish jobs of a run add up to one build. A build has `passed: true` when it has stages and none failed; `failed_stages` lists the ones that did.
- **Artifacts** are what the run published, each with its component, kind and reference (an image digest such as `ghcr.io/acme/x/demo-api@sha256:...`, or a release asset URL). The same artifact reported twice is recorded once.
- **A build for a repo the indexer hasn't seen yet** is accepted and kept under the reported name. The first time the indexer indexes that repo, it attaches the build and links its artifacts to the components they name.
- **A deployment** is one component's artifact in one environment at one time: (component, environment, artifact, `deployed_at`) identifies it, and reporting it again only updates its `status`. When the report names its run and that run's build recorded an artifact for the component, the deployment links to it (the digest, not just the tag). The repo and the component must already be indexed; otherwise the report answers 404.
- **A teardown** is reported the same way, with status `destroyed` or `destroy-failed` and no artifact: it is recorded against the artifact the component last had deployed in that environment, and it settles the retirement step waiting on it ([lifecycle.md](lifecycle.md#retirement)).

### Moving a project

`POST /api/v1/projects/{name}/transitions` runs the lifecycle's entry rules ([lifecycle.md](lifecycle.md)). When every rule passes it sets the phase, records the transition and answers 201 with it:

```json
{"from_phase": "in-dev", "to_phase": "live", "actor": "ryan", "reason": "launch", "accepted": true, "rule_results": [...], "at": "..."}
```

When a rule fails the phase stays as it was. The attempt is kept in the history with `accepted: false` and every rule's result (the inventory UI shows it), and the API answers 409 with the failing rules and every rule's result:

```json
{
  "detail": "orders: in-dev -> live failed RLP-L105, RLP-L106",
  "project": "orders", "from_phase": "in-dev", "to_phase": "live",
  "failures": [{"rule": "RLP-L105", "description": "live needs an owner", "passed": false, "messages": ["[RLP-L105] ..."]}, ...],
  "results": [...]
}
```

A move to an unknown phase fails rule RLP-L101 the same way, but isn't kept in the history. If the rules can't be evaluated (no `opa`), it answers 503.

## The repo indexer

Repo contracts live in git; the platform only indexes them. A push to a repo's default branch re-reads its `rlp.yaml` and `.copier-answers.yml` at that commit, and a periodic sync catches anything a webhook missed.

For each repo the indexer:

1. Fetches `rlp.yaml` and `.copier-answers.yml` at the commit through the GitHub REST API, with `RLP_GITHUB_TOKEN`.
2. Validates `rlp.yaml` against `schemas/v1alpha1/repo.schema.json`, the schema `rlp validate` uses. The platform image is built from `platform/`, so the package ships a copy in `platform/src/rlp_platform/schemas/`; a test fails when the copy differs. After changing the schema, copy it over.
3. Links the repo to the project named by `metadata.project`. A project that doesn't exist yet is created in `proposed`, with `metadata.description` and the first of `metadata.owners`.
4. Records the template source and version from `.copier-answers.yml` (`_src_path`, `_commit`), else from `metadata.template`, and the indexed commit.
5. Upserts the components. A component that left `rlp.yaml` is marked removed (`removed_at`), not deleted, so its builds and deployments keep their history; one that comes back is restored.

An invalid or missing `rlp.yaml` (or an unreadable `.copier-answers.yml`) changes nothing but the repo's `index_error`, which names the commit and the schema problems; `rlp repo show` prints it. The next good index clears it. A repo the platform doesn't know yet is not recorded until its contract is valid. When GitHub can't be read, nothing changes: the webhook answers 502 and `sync` reports the repo as failed.

### The webhook

`POST /api/v1/hooks/github` handles:

| Event | Does |
| --- | --- |
| `push` to the default branch | Indexes the repo at the pushed commit (`after`) |
| `push` to any other branch or a tag, or a branch deletion | Nothing (`"action": "ignored"`) |
| `repository` `renamed` | Moves the record to the new name, then indexes the default branch head |
| `repository` `deleted` or `archived` | Nothing: the record is kept; retiring it is the retirement workflow's job |
| any other `repository` action | Indexes the default branch head |
| `ping` and every other event | Nothing |

A delivery with no `X-Hub-Signature-256`, or one that isn't the HMAC-SHA256 of the body under `RLP_GITHUB_WEBHOOK_SECRET`, answers 401 and changes nothing. So does every delivery when no secret is configured. The answer is a JSON document with `event`, `action` (`indexed` or `ignored`), `reason` and, when it indexed, `result`.

**Setting it up.** An administrator registers the webhook; rlp doesn't. On the repo (Settings, Webhooks, Add webhook), or on the rlp GitHub App to cover every repo it is installed on:

| Field | Value |
| --- | --- |
| Payload URL | `https://<platform host>/api/v1/hooks/github` |
| Content type | `application/json` |
| Secret | The value of `RLP_GITHUB_WEBHOOK_SECRET`, for example from `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| Events | "Let me select individual events": **Pushes** and **Repositories** |

GitHub sends a `ping` when the webhook is created; the platform answers it with 200 once the secret matches. The token in `RLP_GITHUB_TOKEN` needs read access to the repos' contents and metadata: an installation token of the rlp GitHub App, or a fine-grained personal access token with "Contents: read".

### Sync

`python -m rlp_platform sync [OWNER/NAME]`, `POST /api/v1/sync` and `rlp repo sync [OWNER/NAME]` re-index repos at their default branch's head: the one named, else the repos in `RLP_GITHUB_REPOS`, else every repo the token's GitHub App installation can see (`GET /installation/repositories`, which needs an installation token). Each repo is indexed and committed on its own, so one failure doesn't undo the rest. The module command prints one JSON line with every repo's result and exits 1 if any repo wasn't indexed.

**Scheduling it.** Run the sync every hour. It isn't a component of its own yet; schedule one of:

- the platform's image with the command `python -m rlp_platform sync` and the service's environment, as a scheduled ECS task or a Kubernetes CronJob (cron `0 * * * *`);
- `rlp repo sync` from any machine or workflow that has the platform URL and token, for example a GitHub Actions workflow on `schedule: [{cron: "0 * * * *"}]` with `RLP_PLATFORM_URL` and `RLP_PLATFORM_TOKEN` as secrets.

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

**A platform on AWS without DNS.** The platform runs as a `container-service` behind the environment's shared load balancer, which routes by host (`platform-rlp.<env>.rlp.internal`, [aws.md](aws.md)). Until it has a real DNS name, point the URL at the load balancer and give the host separately, instead of editing `/etc/hosts`:

```sh
export RLP_PLATFORM_URL="http://$(aws ssm get-parameter --name /rlp/dev/platform/alb_dns_name --query Parameter.Value --output text)"
export RLP_PLATFORM_HOST=platform-rlp.dev.rlp.internal   # or platform.host in the config file
```

`RLP_PLATFORM_HOST` (`platform.host`) is optional; when set, every request carries it as the `Host` header. The listener is plain HTTP, so the token crosses the network unencrypted until the platform has DNS and HTTPS.

`rlp config show` prints the values and where each came from, with the token masked to its last four characters.

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
| `rlp repo show OWNER/NAME` | A repo, its last index error and its components |
| `rlp repo sync [OWNER/NAME]` | Re-index one repo, or every repo the platform can see; exits 1 if any wasn't indexed |
| `rlp lifecycle show PROJECT` | The project's phase and its history |
| `rlp lifecycle move PROJECT PHASE --reason TEXT` | Move the project; recorded with your login name and the reason |
| `rlp lifecycle retire PROJECT [--yes] [--status] [--reason TEXT] [--workflow FILE]` | Show a deprecated project's retirement plan; `--yes` starts or resumes it, `--status` shows each step. Exits 1 when a step failed. See [lifecycle.md](lifecycle.md#retirement) |
| `rlp pipeline report [--deployment \| --teardown STATUS]` | Send this run's `results.json` and published artifacts as a build, the deploy stage's deployments, or a teardown's result (`destroyed` or `destroy-failed`) for each component. Exits 0 without reporting when no platform is configured or it can't be reached |

Exit codes follow the rest of the CLI: 0 success; 1 when the platform says no (a rejected move, an unknown project, a name already taken); 2 for setup problems (no URL or token, the platform unreachable, a refused token).

A rejected move exits 1 and prints each failing rule's message:

```text
$ rlp lifecycle move orders live --reason launch
orders: in-dev -> live rejected
  [RLP-L105] rlp-transition.json project.owner: project "orders" has no owner, which live needs. Fix: set the project's owner.
  [RLP-L106] rlp-transition.json project.dashboard_url: ...
```

The client is `rlp_cli.platform_api.PlatformClient`, a thin httpx wrapper with typed results; no generated code is checked in.

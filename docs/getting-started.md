# Getting started

From nothing to a service that runs locally and ships through the golden pipeline. The rlp repo itself follows these steps: its `platform/` component was scaffolded this way (#18).

## 1. Install the CLI

Pick one channel ([cli/README.md](../cli/README.md) has the details):

```sh
uv tool install rlp-cli                                                       # or: pipx install rlp-cli
curl -fsSL https://raw.githubusercontent.com/rdlucas2/rlp/main/install.sh | sh  # Linux and macOS binary
```

```powershell
irm https://raw.githubusercontent.com/rdlucas2/rlp/main/install.ps1 | iex      # Windows binary
```

Besides `rlp`, you need git, [go-task](https://taskfile.dev) and Docker with Compose v2. The python-fastapi template's verbs also need [uv](https://docs.astral.sh/uv/). Check the install with `rlp --version`.

## 2. Scaffold

```sh
rlp new gh:rdlucas2/rlp-template-python-fastapi hello-api --data project_id=hello-api --defaults
cd hello-api
git init && git add -A && git commit -m "chore: scaffold from rlp-template-python-fastapi"
```

`rlp new` renders the template with Copier and validates the result. Leave out `--defaults` to answer each question (component name, Python version, port, coverage minimum). The repo you get has:

| File | What it is |
| --- | --- |
| `rlp.yaml` | The contract: components, artifact, runtime, health paths, config, backing services |
| `Taskfile.yml` | The standard verbs: `setup`, `lint`, `test`, `build`, `package`, `run`, `smoke` |
| `.copier-answers.yml` | Your answers; `rlp update` uses them to pull in newer template versions |
| `.github/workflows/pipeline.yml` | Calls the golden pipeline on every pull request and push to `main` |

## 3. Run it locally

```sh
rlp validate          # the contract is well-formed
rlp policy check      # the repo complies with the policy bundle
rlp setup && rlp test # install dependencies, run the tests and write the reports
rlp run               # build the image and start it with Postgres in Docker Compose
rlp smoke             # check /healthz and /readyz
rlp down              # stop everything
```

`rlp run --native` runs the service with its own `run` verb instead, without Docker. [runtime.md](runtime.md) covers the local runtime.

To run every gate exactly as CI does, use `rlp pipeline run`. `rlp pipeline stages` lists them.

## 4. Ship

Push the repo to GitHub. The `pipeline` workflow calls `golden.yml`, which runs every gate on each pull request and, on pushes to `main` and tags, publishes the image to GHCR. [pipeline.md](pipeline.md) lists the gates and the workflow's inputs.

Deploying to AWS needs the one-time bootstrap and platform stacks, then a `deploy-env` on the pipeline call. [aws.md](aws.md) walks through both. With `deploy-env` set, pull requests plan and estimate the cost; pushes to `main` apply and wait for the readiness path to answer through the load balancer.

## 5. Keep it current

```sh
rlp update            # move to the template's latest version; review the diff, then commit
```

## In this repo

The rlp repo is project #1. Its root [`rlp.yaml`](../rlp.yaml) declares the `platform` component (`platform/`, rendered from the python-fastapi template), so from the repo root:

```sh
task run      # rlp run: the platform stub in Docker Compose
task smoke    # rlp smoke
```

The CLI isn't a component yet: its wheel and binaries have no artifact kind in v1alpha1, so it keeps its own `ci`, `cli` and `release` workflows.

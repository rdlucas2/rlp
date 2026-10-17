# rlp CLI

`rlp` validates contracts, checks policy, runs the standard verbs for every component and scaffolds repos from templates. Local runs, `act` runs and CI runs all go through it, so they execute the same commands. Later slices add commands that drive the platform API.

It is published as the `rlp-cli` package (the `rlp` name on PyPI is taken); the command is `rlp`.

## Install

Pick one:

| Channel | Command | Needs |
| --- | --- | --- |
| PyPI | `uv tool install rlp-cli` or `pipx install rlp-cli` | Python 3.11+ |
| Binary, Linux and macOS | `curl -fsSL https://raw.githubusercontent.com/rdlucas2/rlp/main/install.sh \| sh` | nothing |
| Binary, Windows | `irm https://raw.githubusercontent.com/rdlucas2/rlp/main/install.ps1 \| iex` | nothing |

The install scripts download the binary for your OS from the latest GitHub Release, check it against the release's `SHA256SUMS`, and put it on your `PATH` (`~/.local/bin`, or `%LOCALAPPDATA%\rlp\bin` on Windows). `RLP_VERSION=v0.1.0` installs a specific release; `RLP_INSTALL_DIR` changes the location.

Binaries exist for Linux (x86_64, arm64), macOS (Apple silicon) and Windows (x86_64). On an Intel Mac, use the PyPI package.

**Unsigned binaries.** The binaries are not code-signed or notarized yet:

- **macOS**: Gatekeeper may say the binary "cannot be opened". The install script prints the fix: `xattr -d com.apple.quarantine ~/.local/bin/rlp`.
- **Windows**: SmartScreen may warn on first run. Choose **More info**, then **Run anyway**.

Besides itself, `rlp` needs only go-task (for verbs that run `task <name>`), git (for `rlp new` and `rlp update` with git templates) Docker with Compose v2 (for `rlp run`, `rlp smoke` and `rlp down`) and Terraform 1.10 or later (for `rlp infra` and the terraform and deploy stages). `rlp policy check` downloads the pinned conftest (and the cost stage the pinned Infracost) on first use into your user cache, checked against its published SHA256 (`RLP_TOOLS_DIR` overrides the location).

## Commands

| Command | What it does |
| --- | --- |
| `rlp validate [PATH]` | Validate `rlp.yaml` and `rlp-template.yaml` against the JSON Schemas; errors name the field path |
| `rlp policy check [PATH] [--namespace NS]` | Run the policy bundle on the repo through conftest |
| `rlp setup\|lint\|test\|build\|package [--component NAME] [--path ROOT]` | Run that verb for every component (or the named ones): `verbs.<name>` from `rlp.yaml` if set, else `task <name>`, in the component's `path` |
| `rlp run [--no-start] [--timeout S]` | Start every component and its backing services locally with Docker Compose, then wait until smoke passes ([docs/runtime.md](../docs/runtime.md)) |
| `rlp smoke [--timeout S]` | Check each running component: health endpoints, a function invoke, or a job's exit code |
| `rlp down [-v]` | Stop and remove what `rlp run` started; `-v` also deletes database volumes |
| `rlp run\|smoke --native` | Run the component's own `run` or `smoke` verb instead, without Docker |
| `rlp new TEMPLATE [DEST] [--data K=V] [--defaults]` | Render a Copier template, then validate the result |
| `rlp update [PATH] [--data K=V] [--defaults]` | Update a rendered repo to a newer template version with Copier, then validate it |
| `rlp reports [--path ROOT]` | Print each component's report paths and the Sonar properties they map to |
| `rlp coverage [--path ROOT]` | Check each component's coverage report against `quality.coverage.minimum`, or the platform floor |
| `rlp secrets scan [PATH]` | Scan for secrets with the pinned gitleaks: full git history in a work tree, else the files |
| `rlp artifacts [--path ROOT]` | Print what each component's package verb produces (image tag or file path) |
| `rlp publish [--registry R] [--release TAG]` | Push images to a registry; attach zips and jars to a GitHub Release |
| `rlp infra render --env ENV` | Write the AWS Terraform stack for `rlp.yaml` to `.rlp/infra/ENV/main.tf.json` ([docs/aws.md](../docs/aws.md)) |
| `rlp infra destroy --env ENV` | Tear the environment's stack down with `terraform destroy` |
| `rlp pipeline stages\|run\|summary` | Run the golden pipeline's gates locally, exactly as CI does ([docs/pipeline.md](../docs/pipeline.md)) |

Verb overrides are split with POSIX shell rules on every OS and run without a shell: no pipes, globs or variable expansion. Put anything more complex in a Taskfile task. Write paths with forward slashes.

`rlp new` and `rlp update` refuse templates that run tasks or load Jinja extensions unless you pass `--trust`.

## Output and exit codes

Every command takes `--output json` (or `-o json`, or `RLP_OUTPUT=json`) for agents and scripts. In JSON mode stdout holds exactly one JSON document with an `ok` field; the verbs' own output goes to stderr.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | The check found problems, or a verb failed |
| 2 | Usage or setup error (bad arguments, missing or invalid `rlp.yaml`, conftest unavailable) |

## Development

From the repo root:

```sh
task test      # schemas, policy bundle and CLI tests (installs .venv and pinned tools)
task package   # wheel and sdist into dist/ with uv
task binary    # standalone binary for this OS into dist/ with PyInstaller
```

The CLI tests drive `rlp` as a subprocess. Set `RLP_BIN=dist/rlp-<os>-<arch>` to run the same suite against a built binary. The `cli` workflow runs the suite on Linux, Windows and macOS, against both the installed wheel and the binary, and tests the install scripts against that binary.

Cross-platform rules: `pathlib` for every path, subprocesses called with argument lists (never shell strings), no POSIX-only modules.

## Releasing

Releases are cut by pushing a version tag that matches `rlp_cli.__version__` (for example `v0.1.0`). The `release` workflow builds the wheel, sdist and per-OS binaries, tests each binary, attaches everything with `SHA256SUMS` to a GitHub Release, and publishes to PyPI through trusted publishing. PyPI publishing needs a one-time trusted-publisher setup on pypi.org (#23). Code signing is pending a decision (#24).

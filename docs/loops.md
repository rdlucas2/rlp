# Remediation loops

A remediation loop reads a failing gate, asks an agent to fix the code, reruns the gate and opens a PR only when it passes. This page covers the pieces every loop shares: the **on/off toggle**, the **loop runner** (`rlp loop <kind>`) and the **agent provider**, the interface a loop uses to ask an agent for a change. The individual loops build on them: [the quality loop](#the-quality-loop) (#48) and [the vulnerability loop](#the-vulnerability-loop) (#49).

## Turning loops on and off

Loops are **off by default**. A repo opts in in `rlp.yaml`:

```yaml
loops:
  enabled: true
  kinds: [quality]   # optional: only these kinds; absent means every kind
```

`RLP_LOOPS=off` in the environment is a kill switch: it turns every loop off whatever `rlp.yaml` says (`0`, `false`, `no` and `disabled` work too). In CI, set it as a repository or organization Actions variable, so loops stop without a commit; delete the variable to turn them back on.

When a loop is off, `rlp loop <kind>` calls no agent, spends nothing, prints why it's off (`rlp.yaml` or `RLP_LOOPS`) and exits 0 with outcome `disabled`. `rlp loop status` shows each kind, whether it's on, and why.

## The loop runner

```sh
rlp loop status                                  # which kinds are on, and why
rlp loop quality --dry-run                       # discover and print the plan; no agent, no changes
rlp loop quality --max-iterations 3 --max-spend 5 --output json
```

One run goes:

1. **Discover.** The kind lists its findings. None: outcome `no-findings`, nothing else happens.
2. **Branch.** The runner refuses a work tree with uncommitted changes to tracked files, then creates `rlp/loop/<kind>/<run-id>` from the current branch. It never commits to the base branch, and checks the base branch out again when the run ends.
3. **Act.** One agent call in a fresh context. The prompt is rebuilt every iteration from the current findings and the last verification's output; no conversation carries over. The agent may change only the kind's allowed paths.
4. **Verify.** The kind's golden pipeline stages run, as `rlp pipeline run -s <stage>` would. An attempt that changed anything outside the allowed paths, or any of the kind's guarded files, is thrown away first (tracked files restored, new files deleted, guarded files put back byte for byte) and the next prompt says why. A kept attempt is committed to the loop branch.
5. **Iterate** until the verify stages pass, or a cap is hit.

A kind can also hand a run to a human: before the first agent call (its `preflight` hook, for example a fix that needs a major-version bump), or after the verify stages pass (its `review` hook, for a change a human must decide on). Either ends the run `escalated`, so no PR opens.

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-iterations N` | 3 | Agent calls before the loop escalates |
| `--max-spend USD` | `loop_max_spend_usd` in `policy/platform/data.yaml` | Total the run may spend, counted from what the provider reports. Each call's budget is what's left |
| `--provider NAME` | `RLP_AGENT_PROVIDER`, else `claude-code` | See [Choosing a provider](#choosing-a-provider) |
| `--dry-run` | | Discover and print the plan, including the first prompt, without calling the agent or touching git |

Both caps must be positive.

| Outcome | Exit code | When |
| --- | --- | --- |
| `fixed` | 0 | The verify stages passed |
| `no-findings` | 0 | Discovery found nothing to fix |
| `disabled` | 0 | Loops are off for this repo or kind |
| `escalated` | 1 | The iteration cap or the spend cap was reached with the gate still failing, or the kind's `preflight` or `review` hook handed the run to a human |
| `error` | 2 | The loop couldn't start or continue: bad options, a dirty work tree, missing credentials, a git or agent failure |

Opening the PR is a separate step, done as the loop's [agent identity](#agent-identity). An `escalated` or `error` run hands over to a human through an [escalation issue](#escalation).

### State

Every run that gets past discovery writes `.rlp/loops/<kind>-<run-id>.json`, updated after each iteration, so a run in progress or a finished one can be inspected. It records the kind, branch and base, the limits, the findings, and for each iteration the findings its prompt was built from, the agent result, the diff stat, the gate results, the commit, whether the attempt was thrown away, the spend so far, the findings left afterwards (`findingsAfter`) and how long it took (`durationSeconds`), then the outcome and why. It validates against [`schemas/v1alpha1/loop-state.schema.json`](../schemas/v1alpha1/loop-state.schema.json). `--output json` prints the same document.

### Telemetry

`rlp loop summary <state-file>` renders a run's telemetry as Markdown: one table row per iteration with the findings before and after, the gate results, the cost and the duration, then the outcome and the total spend. The total is the sum of the iterations' agent costs. It works for any outcome, including runs that stopped before the first iteration.

```sh
rlp loop summary .rlp/loops/quality-20261019T120000Z-abc123.json >> "$GITHUB_STEP_SUMMARY"
```

Both loop workflows run it after the loop step, so every run's job summary shows the table, and upload the state file as the `rlp-loop-<kind>` artifact. `--output json` adds the iteration count and total spend alongside the Markdown. State files from before rlp recorded `findingsAfter` and `durationSeconds` still render; unknown values show as `-`. Dashboards and long-term storage come in slice 4.

### Adding a loop kind

A kind is a `rlp_cli.loop.LoopKind` registered with `loop.register()`:

- `discover(root, doc)` returns the current findings, each a dict with at least `message` (and usually `path`, `line`, `rule`, `severity`).
- `verify_stages` names the golden pipeline stages that must pass.
- `allowed_paths(root, doc)` returns the paths the agent may change. Quality config, suppressions and exception files are never among them.
- `build_prompt(PromptInput)` is optional; without it the runner's default prompt lists the findings and the failing gate output.
- `preflight(root, doc, findings)` is optional: a non-empty reason escalates the run before any agent call.
- `review(root, base)` is optional: called once the verify stages pass, with the loop branch checked out; a non-empty reason escalates the run instead of ending it `fixed`.
- `guarded` is optional: files an attempt must leave exactly as they were, checked by content even where git ignores them (exception files the gate reads, for example). A changed one is put back and the attempt is thrown away.

Add the kind's name to `KNOWN_KINDS` in `loop.py` and to the `loops.kinds` enum in `schemas/v1alpha1/repo.schema.json`.

## The quality loop

`rlp loop quality` fixes what the code-quality gates found: `lint`, `test`, `coverage` (overall and on new code) and `sast` (Semgrep). The same four stages are its verify stages, so a run is `fixed` only when all of them pass on the loop branch.

**Discovery** reads the gates' last results, so run them first; the loop workflow runs each one on its own so every failure is recorded:

```sh
rlp pipeline run --reset -s setup
for s in lint test coverage sast; do rlp pipeline run -s "$s"; done
rlp loop quality
```

| Gate | What goes in the prompt |
| --- | --- |
| `lint`, `test` | The verb's output for each failing component (its last 4,000 characters). Discovery runs the verb again to capture it, since the agent can't run commands |
| `coverage` | Components below the minimum, and each file's changed lines that no test covers, from the new-code check |
| `sast` | Each Semgrep finding's file, line, rule and message, from `results.json` (else `.rlp/pipeline/reports/semgrep.json`) |
| `sonar` | Only when `SONAR_TOKEN` is set and the optional `sonar` stage failed: the project's open issues from the Sonar issues API |

A stage after the first failing one keeps its last recorded result until it reruns, so a later iteration can still list a finding an earlier attempt already fixed; the gates decide.

**Allowed paths** are each component's tracked top-level directories, for example `src` and `tests` (or `platform/src` and `platform/tests` for a component at `platform/`). Hidden directories (`.github`, `.rlp`), `policy`, `infra`, `reports`, another component's directory and every file at a component's root (`Taskfile.yml`, `pyproject.toml`, `rlp.yaml`, lint and coverage config) are never among them. The `rlp.agent` rules judge what the agent does change.

**The pull request.** A fixed run's branch becomes a PR with `rlp loop pr`, which refuses any run that isn't `fixed` or whose last kept iteration has a verify stage that didn't pass, so no PR opens while a gate is red:

```sh
rlp loop pr .rlp/loops/quality-<run-id>.json           # print the Markdown
git push origin rlp/loop/quality/<run-id>
rlp loop pr .rlp/loops/quality-<run-id>.json --post    # open it into the base branch, labelled agent:authored and loop:quality
```

**In CI**, call the reusable [`loop-quality.yml`](../.github/workflows/loop-quality.yml) workflow after a failed golden run:

```yaml
jobs:
  golden:
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<rlp git ref>
    with: {rlp-version: "0.2.0"}
  quality-loop:
    needs: golden
    if: failure()
    uses: rdlucas2/rlp/.github/workflows/loop-quality.yml@<rlp git ref>
    with: {rlp-version: "0.2.0"}
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
      anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The job is skipped when the `RLP_LOOPS` variable is off, and every step after the toggle check is skipped unless `rlp.yaml` turns the quality loop on, so a repo that hasn't opted in spends nothing. Otherwise it reruns the gates, runs `rlp loop quality` with the `claude-code` provider, then pushes the branch and opens the PR as the [agent identity](#agent-identity) when the run is `fixed`, or opens an [escalation issue](#escalation) when it is `escalated` or `error`. The state file and gate results are uploaded as the `rlp-loop-quality` artifact.

| Input | Default | Meaning |
| --- | --- | --- |
| `rlp-version` | (required) | rlp-cli to install, as for `golden.yml` |
| `working-directory` | `.` | Repo root of the project |
| `max-iterations` | 3 | `--max-iterations` |
| `max-spend` | policy default | `--max-spend` in USD |
| `claude-code-version` | pinned | The Claude Code CLI the provider runs |

Seeded cases for the loop (a Semgrep finding, a lint error, a drop in new-code coverage and an unfixable finding) are in [`tests/loops/quality/`](../tests/loops/quality/README.md).

## The vulnerability loop

`rlp loop vuln` fixes what the `trivy` gate found by upgrading: a vulnerable dependency to a release that fixes it, or a container's base image to a newer tag. It never adds an exception: `.rlp/vex.yaml`, `.trivyignore` and the Trivy settings files are guarded, so an attempt that touches one is put back and thrown away, even where git ignores the file.

**Discovery** reads the Trivy JSON reports the last trivy stage wrote (`.rlp/pipeline/reports/trivy-fs.json` and `trivy-image-<component>.json`), so run the gates first:

```sh
rlp pipeline run --reset -s setup
for s in test package trivy; do rlp pipeline run -s "$s"; done
rlp loop vuln
```

Each finding is a fixable HIGH or CRITICAL vulnerability that no unexpired `.rlp/vex.yaml` entry covers, as the gate counts them, with its package, installed version, fixed version, scan target and the manifest to change. The prompt groups them by manifest:

| Found by | Manifest in the prompt |
| --- | --- |
| The filesystem scan | The file Trivy found the package in: `requirements.txt`, `uv.lock`, `package-lock.json` and so on |
| An image scan, in a package the app declares | The requirements file, `pyproject.toml` or `package.json` that declares it |
| An image scan, anything else (OS packages, tools that came with the base image) | The component's Dockerfile, marked as the base image |

**Verify stages** are `setup`, `test`, `package` and `trivy`, so a fix is `fixed` only when the dependencies still install, the tests pass, the image builds and Trivy is clean. The golden pipeline has no separate `build` stage; the package verb builds what it ships.

**Allowed paths** are dependency manifests, lockfiles and Dockerfiles: the tracked ones at each component's root, each container component's declared Dockerfile, and any manifest the filesystem scan named, outside hidden directories, `policy`, `infra` and `reports`.

**Major-version bumps escalate.** Choosing a new major version is a human's call, so the loop never opens a PR for one:

- Before the first agent call, a finding in a direct dependency whose every fixed version is on a new major version (installed 1.7.1, fixed in 2.4.0) escalates the run. Nothing is spent.
- After the verify stages pass, the loop compares the branch with its base. If a direct dependency's pin (in a requirements file, `pyproject.toml`, `package.json`, or a lockfile entry for a declared package) or a Dockerfile's `FROM` tag moved to a new major version, the run escalates instead of ending `fixed`.

A direct dependency is one a requirements file, `pyproject.toml` or `package.json` declares. A transitive package's major bump in a lockfile doesn't escalate by itself.

**Lockfiles are regenerated, not hand-edited.** The agent can't run commands, so after a kept attempt that changed a dependency manifest at a component's root, the loop runs that component's lock command itself, in the component's directory, before committing:

- The command is the component's `lock` in `rlp.yaml`, for example `lock: uv lock`. When it is absent, a tracked `uv.lock` beside the manifests means `uv lock`; otherwise nothing runs and a lockfile the agent can't keep consistent fails the `setup` stage.
- The regenerated lockfile goes into the iteration's commit, so the major-bump check above reads it: a lock that moves a direct dependency to a new major version escalates.
- A lock command that fails, or that changes a file outside the allowed paths, throws the attempt away; its output goes into the next prompt, as for any rejected attempt.
- The prompt tells the agent which lock command runs, so it raises the pin in the manifest (for example `cryptography==42.0.4` in `pyproject.toml`) rather than editing the lockfile.
- Each iteration's state records what ran under `afterAttempt`: the component, the command, its exit code and the files it changed.

**In CI**, call the reusable [`loop-vuln.yml`](../.github/workflows/loop-vuln.yml) workflow after a failed golden run, and on a schedule so a CVE published against unchanged code is caught too:

```yaml
on:
  push: {branches: [main]}
  schedule: [{cron: "17 6 * * 1"}]
jobs:
  golden:
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<rlp git ref>
    with: {rlp-version: "0.2.0"}
  vuln-loop:
    needs: golden
    if: failure()
    uses: rdlucas2/rlp/.github/workflows/loop-vuln.yml@<rlp git ref>
    with: {rlp-version: "0.2.0"}
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
      anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

It takes the same inputs and secrets as `loop-quality.yml` and honours the same toggle: the job is skipped when `RLP_LOOPS` is off, and every step after the toggle check is skipped unless `rlp.yaml` turns the vuln loop on. A `fixed` run pushes its branch and opens a PR labelled `agent:authored` and `loop:vuln` as the [agent identity](#agent-identity); an `escalated` or `error` run opens an [escalation issue](#escalation). The state file and gate results are uploaded as the `rlp-loop-vuln` artifact.

Seeded cases (a vulnerable pinned package with a patch release, the same in a `uv` project whose `uv.lock` the loop regenerates, a vulnerable base image, and a fix that needs a major bump) are in [`tests/loops/vuln/`](../tests/loops/vuln/README.md).

## Escalation

Every loop ends in a green PR or in an escalation issue: one GitHub issue a human can take over from without re-running anything. `rlp loop escalate` renders it from the run's state file:

```sh
rlp loop escalate .rlp/loops/quality-<run-id>.json           # print the Markdown
rlp loop escalate .rlp/loops/quality-<run-id>.json --post    # open the issue, or comment on the open one
```

The issue has:

- the loop kind, outcome and reason, base and loop branch, provider, iterations used and spend against the caps
- the trigger: the GitHub Actions run that started the loop, read from `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID` (or `--trigger URL`)
- the last failing gate's output, cut to its last 4,000 characters
- for each iteration: the findings its prompt was built from, the agent's summary, cost and tokens, the changed files, the diff stat and commit (or why the attempt was thrown away), and the gate results

It is labelled `needs:human`, `area:agent` and `loop:<kind>`. There is **one open escalation per loop kind and base branch**: the body carries a hidden marker naming both, and a repeat escalation for the same pair adds a comment ("Escalated again: run ...") with the new run's details instead of opening another issue. Closing the issue lets the next escalation open a fresh one.

Only `escalated` and `error` runs can be escalated; any other outcome exits 2. `--post` calls the GitHub REST API with `GH_TOKEN` (else `GITHUB_TOKEN`), against `GITHUB_API_URL` when it is set, in the repo named by `--repo OWNER/NAME` (else `GITHUB_REPOSITORY`). In a loop workflow, pass the [agent identity](#agent-identity)'s token so the issue is opened by `localrlp[bot]`. `--output json` prints the title, labels and body, plus the issue number, URL and whether it was `opened` or `commented` when posting.

## Agent identity

Loop commits and PRs are made by a dedicated GitHub App, **localrlp**, not by a person or by `GITHUB_TOKEN`. That makes agent changes easy to tell apart: the PR author is `localrlp[bot]`, which `agent_authors` in [`policy/platform/data.yaml`](../policy/platform/data.yaml) lists, so the `rlp.agent` anti-cheat rules judge its PRs as agent-authored even without the `agent:authored` label. Pushes made with an App token also trigger the PR's checks, which pushes made with `GITHUB_TOKEN` don't.

| Setting | Value |
| --- | --- |
| App | `localrlp`, installed on the rlp repos with `contents`, `pull-requests` and `issues` write |
| Bot login | `localrlp[bot]` |
| Secrets | `APP_ID` and `APP_PRIVATE_KEY`, as repository secrets |

A loop workflow gets the identity from the [`agent-identity`](../.github/actions/agent-identity/action.yml) composite action. It mints a short-lived installation token, scoped to those three permissions and revoked when the job ends, and sets git to commit as the bot user. Git authenticates through `gh`, so every step that pushes or calls the API sets `GH_TOKEN` to the action's `token` output:

```yaml
- uses: actions/checkout@<sha>
  with:
    persist-credentials: false  # else pushes go out as GITHUB_TOKEN
- id: agent
  uses: ./.github/actions/agent-identity
  with:
    app-id: ${{ secrets.APP_ID }}
    private-key: ${{ secrets.APP_PRIVATE_KEY }}
- run: |
    git push origin "$BRANCH"
    gh api "repos/$GITHUB_REPOSITORY/pulls" -f head="$BRANCH" -f base=main -f title="..."
  env:
    GH_TOKEN: ${{ steps.agent.outputs.token }}
```

Outputs: `token`, `login` (`localrlp[bot]`) and `email` (the bot's noreply address). To reach another rlp repo, such as a template repo, pass `repositories: rlp-template-python-fastapi`; the App must be installed there.

**Checking it.** The `agent-identity` workflow runs the action by hand (Actions, agent-identity, Run workflow). It pushes a throwaway branch, opens a draft PR labelled `agent:authored`, fails unless GitHub shows `localrlp[bot]` as the author of both the commit and the PR, then closes the PR and deletes the branch. Tick **keep-open** to leave them for inspection.

**Rotating the key.** Generate a new private key in the App's settings, replace `APP_PRIVATE_KEY`, run the `agent-identity` workflow, then delete the old key. Renaming the App changes its bot login: update `agent_authors` in the same PR.

## The provider interface

Loop code imports `rlp_cli.agent` and nothing provider-specific:

```python
from rlp_cli import agent

provider = agent.get_provider(name)   # --provider, else RLP_AGENT_PROVIDER, else claude-code
provider.check()                      # raises agent.CredentialsError with the variable to set
spend = agent.Spend(cap_usd=5.00)

result = provider.run(
    prompt="Fix the failing lint findings listed below ...",
    workdir=Path("."),
    allowed_paths=["app", "tests"],
    budget=spend.budget(per_call_usd=1.00),   # per-call limit, capped by what's left
)
spend.charge(result)                          # counts exactly result.cost_usd
```

`run(prompt, workdir, allowed_paths, budget) -> AgentResult` is one agent call in a fresh context: nothing carries over from an earlier call, so the loop must put everything the agent needs in the prompt.

| `AgentResult` field | Meaning |
| --- | --- |
| `ok` | The agent finished without an error and within its budget |
| `summary` | The agent's account of what it did, or why it stopped |
| `changed_files` | Files that changed on disk during the call, as POSIX paths relative to `workdir` |
| `outside_allowed` | Changed files that are not under any of `allowed_paths`; the loop must reject the attempt if this is not empty |
| `input_tokens`, `output_tokens`, `tokens` | Token use, cache reads and writes included in input |
| `cost_usd` | What the call cost, as the provider reports it |
| `over_budget` | The call hit or went over `budget.max_usd` |

Changed files are measured by the interface, not reported by the agent: every file is hashed before and after the call (in a git work tree, tracked and untracked-but-not-ignored files). An agent can't hide a change by leaving it out of its summary.

### Spend

`agent.Spend` is the loop's spend cap. It counts the `cost_usd` each provider reports, nothing else. `spend.budget(per_call)` gives the next call the smaller of the per-call limit and what's left, and raises `AgentError` once the cap is reached, so the loop escalates instead of calling again.

## Choosing a provider

`--provider NAME` on a command, else the `RLP_AGENT_PROVIDER` environment variable, else `claude-code`. Check that the chosen provider can run on this machine or runner with:

```sh
rlp agent check                 # exit 0: ready; exit 2: what's missing
rlp agent check --provider fake
```

Credentials only ever come from the environment. A provider never reads a key from a file in the repo it is changing.

### `claude-code`

Runs the [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) CLI headless (`claude --print --output-format json`) in the working tree. In CI, the Claude Code GitHub Action installs the same CLI; loop workflows wire that up per loop.

- **Credentials:** `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` (what the GitHub Action sets). Without either, the provider refuses to run and names the variable to set.
- **Budget:** passed as `--max-budget-usd`. The reported `total_cost_usd` is checked against it again afterwards.
- **Permissions:** the agent may read anything in the tree (`Read`, `Glob`, `Grep`) and edit or write only under `allowed_paths`. Every other tool, shell commands included, is denied (`--permission-mode dontAsk`). The loop reruns the gates itself.
- **Isolation:** only user-level settings load (`--setting-sources user`) and no MCP servers (`--strict-mcp-config`), so settings committed to the repo can't widen permissions or supply credentials.

### `fake`

For tests. Each call applies the next scripted patch to the working tree and reports a fixed cost, with no network and no credentials. In Python, pass `FakeProvider([FakeStep(files={...}, cost_usd=0.05), ...])`. From the command line, point `RLP_FAKE_AGENT_SCRIPT` at a JSON list of steps:

```json
[
  {"files": {"app/main.py": "print('fixed')\n", "old.py": null}, "summary": "fixed lint", "costUsd": 0.05},
  {"files": {}, "summary": "gave up", "ok": false}
]
```

A string value writes that file; `null` deletes it. `costUsd`, `inputTokens`, `outputTokens`, `summary` and `ok` are optional. Once the script runs out, calls change nothing and return `ok: false`.

## Adding a provider

1. In `cli/src/rlp_cli/agent.py`, write a class with a `name` attribute and the two methods of `AgentProvider`:
   - `check()` raises `CredentialsError` naming the environment variable to set, or `AgentError` for anything else missing (a CLI not on `PATH`, say).
   - `run(prompt, workdir, allowed_paths, budget)` calls `check()`, normalises `allowed_paths` with `_normalise_allowed`, takes a `snapshot()` of the tree, runs the agent in a fresh context capped at `budget.max_usd`, then fills `changed_files` and `outside_allowed` from a second snapshot with `changed()` and `outside()`.
2. Read credentials only from the `env` mapping the factory receives.
3. Register it in `PROVIDERS` under its name. `--provider` and `rlp agent check` pick it up from there.
4. Add tests next to `cli/tests/test_agent.py` that stub the agent process, as the `claude-code` tests do; tests never call a real model.

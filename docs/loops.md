# Remediation loops

A remediation loop reads a failing gate, asks an agent to fix the code, reruns the gate and opens a PR only when it passes. This page covers the pieces every loop shares: the **on/off toggle**, the **loop runner** (`rlp loop <kind>`) and the **agent provider**, the interface a loop uses to ask an agent for a change. The individual loops (quality #48, vuln #49) build on them.

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
4. **Verify.** The kind's golden pipeline stages run, as `rlp pipeline run -s <stage>` would. An attempt that changed anything outside the allowed paths is thrown away first (tracked files restored, new files deleted) and the next prompt says why. A kept attempt is committed to the loop branch.
5. **Iterate** until the verify stages pass, or a cap is hit.

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
| `escalated` | 1 | The iteration cap or the spend cap was reached with the gate still failing |
| `error` | 2 | The loop couldn't start or continue: bad options, a dirty work tree, missing credentials, a git or agent failure |

Opening the PR or the escalation issue is a separate step (#52).

### State

Every run that gets past discovery writes `.rlp/loops/<kind>-<run-id>.json`, updated after each iteration, so a run in progress or a finished one can be inspected. It records the kind, branch and base, the limits, the findings, and for each iteration the findings its prompt was built from, the agent result, the diff stat, the gate results, the commit, whether the attempt was thrown away and the spend so far, then the outcome and why. It validates against [`schemas/v1alpha1/loop-state.schema.json`](../schemas/v1alpha1/loop-state.schema.json). `--output json` prints the same document.

### Adding a loop kind

A kind is a `rlp_cli.loop.LoopKind` registered with `loop.register()`:

- `discover(root, doc)` returns the current findings, each a dict with at least `message` (and usually `path`, `line`, `rule`, `severity`).
- `verify_stages` names the golden pipeline stages that must pass.
- `allowed_paths(root, doc)` returns the paths the agent may change. Quality config, suppressions and exception files are never among them.
- `build_prompt(PromptInput)` is optional; without it the runner's default prompt lists the findings and the failing gate output.

Add the kind's name to `KNOWN_KINDS` in `loop.py` and to the `loops.kinds` enum in `schemas/v1alpha1/repo.schema.json`.

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

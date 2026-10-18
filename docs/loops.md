# Remediation loops

A remediation loop reads a failing gate, asks an agent to fix the code, reruns the gate and opens a PR only when it passes. This page covers the piece every loop shares: the **agent provider**, the interface a loop uses to ask an agent for a change. The loop runner (`rlp loop <kind>`, #46) and the individual loops build on it.

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

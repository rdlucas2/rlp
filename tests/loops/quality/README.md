# Seeded fixtures for the quality loop

Each directory seeds one failure of the quality loop's gates (#48) into a copy of the pipeline fixture repo, [`tests/pipeline/fixtures/hello`](../../pipeline/fixtures/hello). `cli/tests/test_loop_quality.py` runs the real `lint`, `test` and `coverage` stages on it, and a fake `semgrep` for `sast`, then runs `rlp loop quality` with the fake agent provider.

| Case | Seeded failure | Expected outcome |
| --- | --- | --- |
| `semgrep` | `src/hello/digest.py` hashes with MD5 | `fixed` in 1 iteration |
| `lint` | `src/hello/shout.py` has a syntax error the lint verb rejects | `fixed` in 1 iteration |
| `coverage` | `src/hello/farewell.py` is new and untested, so new-code coverage drops to 0% | `fixed` in 1 iteration |
| `unfixable` | a shell-injection finding in `.github/workflows/triage.yml`, which the loop may not change | `escalated` at the iteration cap |

In each directory:

- `seed/` holds the files committed on top of the baseline. They end in `.fixture` so this repo's own linters and Semgrep don't read them; the test drops the suffix.
- `case.yaml` describes the case, the expected outcome, and the findings the fake `semgrep` reports while a pattern is still in a file.
- `agent.json` is the fake agent's script (the `RLP_FAKE_AGENT_SCRIPT` format in [docs/loops.md](../../../docs/loops.md#fake)): the change a competent agent makes. In the unfixable case every attempt edits `.github/`, so each is thrown away.

To try a case with a real agent, copy the fixture repo, apply `seed/` (dropping `.fixture`), add `loops: {enabled: true}` to its `rlp.yaml`, commit, run the gates and then `rlp loop quality`, with `RLP_BASELINE` set to the commit before the seed.

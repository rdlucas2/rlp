# Seeded fixtures for the vulnerability loop

Each directory seeds one vulnerability into a copy of the pipeline fixture repo, [`tests/pipeline/fixtures/hello`](../../pipeline/fixtures/hello). `cli/tests/test_loop_vuln.py` runs the real `setup` and `test` stages on it, a `package` verb that builds nothing (the tests have no Docker daemon), and a fake `trivy`, then runs `rlp loop vuln` with the fake agent provider.

| Case | Seeded vulnerability | Expected outcome |
| --- | --- | --- |
| `patch` | `requirements.txt` pins `cryptography==42.0.0` (CVE-2024-26130); 42.0.4 fixes it | `fixed` in 1 iteration |
| `base-image` | the Dockerfile builds `FROM python:3.13.0-slim`, whose `libexpat1` has a fixable CVE | `fixed` in 1 iteration |
| `major` | `requirements.txt` pins `pyjwt==1.7.1` (CVE-2022-29217); the only fix is 2.4.0, a major bump | `escalated` before any agent call, no PR |

In each directory:

- `seed/` holds the files committed on top of the baseline. They end in `.fixture` so this repo's own Trivy, linters and Semgrep don't read them; the test drops the suffix.
- `case.yaml` describes the case, the expected outcome, and what the fake `trivy` reports while a pattern is still in a file. The base-image CVE and versions are illustrative.
- `agent.json` is the fake agent's script (the `RLP_FAKE_AGENT_SCRIPT` format in [docs/loops.md](../../../docs/loops.md#fake)): the change a competent agent makes. In the major case the loop escalates before calling it.

To try a case with a real agent and the real Trivy, copy the fixture repo, apply `seed/` (dropping `.fixture`), add `loops: {enabled: true}` to its `rlp.yaml`, commit, run `setup`, `test`, `package` and `trivy`, then `rlp loop vuln`.

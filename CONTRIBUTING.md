# Contributing to rlp

## Workflow

1. Pick an issue. Every change starts from a GitHub issue.
2. Branch off the latest `main`: `feat/<short-name>`, `fix/<short-name>` or `chore/<short-name>`.
3. Commit in small, focused steps. Never commit directly to `main`.
4. Open one pull request per feature. Put `Closes #<issue>` in the description.
5. Merge only when every check is green.

## Local setup

The devcontainer installs the full toolchain (Docker, go-task, Copier, OPA and conftest, Trivy, gitleaks, `act`, uv, Terraform, `gh`). Outside the devcontainer, install those tools yourself, then:

```sh
task setup   # install git hooks
task lint    # run every pre-commit hook against the repo
```

## Standard verbs

Every rlp repo, including this one, implements the same Taskfile targets: `setup`, `lint`, `test`, `build`, `package`, `run` and `smoke`. CI and the `rlp` CLI call them by name, so a check that passes locally passes in CI.

## Labels and milestones

Issues carry a type (`type:epic`, `type:feature`, `type:chore`), a slice (`slice:0` to `slice:6`) and an area (`area:cli`, `area:policy`, and so on). Each slice is a milestone with one epic issue; child issues are sub-issues of their epic. The full plan is in [docs/PLAN.md](docs/PLAN.md).

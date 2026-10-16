# rlp — notes for Claude

rlp is an open-source, agent-native internal developer platform. The plan, principles and architecture are in `docs/PLAN.md`; conventions are in `CONTRIBUTING.md`. Work is tracked as GitHub issues, one epic per slice milestone.

## Always

- `main` is the default branch and is never committed to directly. Cut a feature branch off the latest `main` for each piece of work and open one PR per feature, with `Closes #<issue>` in the body.
- Every repo implements the standard Taskfile verbs (`setup`, `lint`, `test`, `build`, `package`, `run`, `smoke`). Run `task lint` and `task test` before pushing.
- Never weaken a quality gate to make a check pass.
- Use the GitHub REST API (`gh api repos/rdlucas2/rlp/...`); GraphQL is not available in Claude sessions.
- Commit as `rdlucas2 <rdlucas2@users.noreply.github.com>`.

## Autonomous runs

A scheduled build loop works the backlog every 4 hours. If you are running as that loop, `docs/LOOP.md` is your full instruction set: follow it exactly.

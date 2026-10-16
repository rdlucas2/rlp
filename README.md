# rlp

An open-source, agent-native internal developer platform: from idea to architecture, scaffolded code, gated pipeline, deployment, monitoring and retirement — running identically locally and in AWS.

Work is tracked in [GitHub Issues](https://github.com/rdlucas2/rlp/issues), organized by slice milestones. The plan, principles and architecture are in [docs/PLAN.md](docs/PLAN.md); setup and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

| Path | What lives there |
| --- | --- |
| `cli/` | The `rlp` CLI (Python + Click); install and usage in [cli/README.md](cli/README.md) |
| `schemas/` | JSON Schemas for the contract files |
| `policy/` | OPA/Rego policy bundle |
| `platform/` | Platform API, state store and UI |
| `infra/` | Terraform bootstrap and runtime modules |
| `docs/` | Plan and guides |

## Contributing workflow

- `main` is the default branch and is never committed to directly.
- Cut a feature branch off `main` for each piece of work (`feat/<short-name>`, `fix/<short-name>`, `chore/<short-name>`).
- Open one pull request per feature, linked to its issue.

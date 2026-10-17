# infra

Terraform for deploying rlp components to AWS. How the pieces fit and how to set up an account: [docs/aws.md](../docs/aws.md). See #16.

| Directory | What it is | Applied by |
| --- | --- | --- |
| `bootstrap/` | State bucket, GitHub OIDC provider, `rlp-plan` and `rlp-deploy` roles | an administrator, once per account |
| `platform/` | Shared ECS cluster, load balancer and listener for one environment; publishes them under `/rlp/<env>/platform/` in SSM | an administrator, once per environment |
| `modules/container-service/` | One component on ECS Fargate: task definition, service, target group, listener rule, log group, config and secrets | the golden pipeline, through the stack `rlp infra render` writes |

App stacks reference the modules by git tag (`?ref=v<rlp version>`), so a module change reaches a project only when it moves to a newer rlp.

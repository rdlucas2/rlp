# Deploying to AWS

A `container-service` component runs on ECS Fargate behind an Application Load Balancer. The golden pipeline plans the change on every pull request, checks the plan against the policy bundle, and applies it on pushes to the default branch. GitHub Actions authenticates with OIDC, so no AWS keys are stored in GitHub. See #16.

## The pieces

| Piece | Where | Who runs it | How often |
| --- | --- | --- | --- |
| Bootstrap: state bucket, GitHub OIDC provider, `rlp-plan` and `rlp-deploy` roles | `infra/bootstrap` | an administrator, by hand | once per AWS account |
| Platform: shared ECS cluster, load balancer, HTTP listener | `infra/platform` | an administrator, by hand | once per environment |
| App stack: one `container-service` module per component | rendered by `rlp infra render` into `.rlp/infra/<env>/` | the golden pipeline | every run with `deploy-env` |

The platform stack publishes what app stacks need (cluster, listener, subnets, load balancer) as SSM parameters under `/rlp/<env>/platform/`. App stacks read them there, so they never touch the platform's state.

## Account strategy for S0 (proposed)

One AWS account. Environments (`dev`, later `prod`) share it and are kept apart by name: every resource name starts with the environment, carries the `rlp:environment` tag, and keeps its state under its own key. This is the cheapest way to start and needs one bootstrap.

Nothing in the modules assumes a single account. Moving an environment to its own account later means bootstrapping that account and pointing that environment's pipeline at its roles and bucket.

## What a component gets

| From `rlp.yaml` | On AWS |
| --- | --- |
| the image built by `package` and pushed by `publish` | `ghcr.io/<owner>/<repo>/<metadata.name>-<component>:<commit>`, run as an ECS Fargate service (256 CPU units, 512 MiB, one task) in the default VPC's public subnets |
| `runtime.port` | the container port; only the load balancer may reach it |
| `runtime.health.readiness`, else `liveness`, else `/` | the target group's health check, and what the deploy stage requests |
| — | a host-header rule on the shared listener for `<component>-<repo>.<env>.<domain>` (`domain` defaults to `rlp.internal`) |
| `config` entry with a `default` | SSM parameter `/rlp/<env>/<repo>/<component>/<NAME>` (an empty default becomes a plain environment variable) |
| `config` entry with no default | SSM parameter holding `unset`; overwrite it with the real value |
| `config` entry with `secret: true` | empty Secrets Manager secret `rlp/<env>/<repo>/<component>/<NAME>`; put the value in it |
| `config` entry with `from: service:<name>` (postgres) | the same kind of empty secret, for the connection string of a database you bring yourself. rlp does not create RDS in S0 |
| other service kinds (redis, s3, sqs, sns) | not mapped yet: `rlp infra render` fails |
| logs | CloudWatch Logs group `/rlp/<env>/<repo>/<component>`, kept 30 days |

Terraform never sees a secret value: it creates the empty secrets and the task reads them when it starts. A task whose secret is still empty fails to start, and the deploy stage reports the service as unhealthy.

Every resource carries the `rlp:project`, `rlp:environment`, `rlp:managed-by` and `rlp:repo` tags through the provider's default tags. The deploy role may only change resources tagged `rlp:managed-by = rlp`.

There is no DNS or TLS yet. Clients reach a service at the load balancer's DNS name with the service's host in the `Host` header; the deploy stage does the same.

## Set up an account

You need Terraform 1.10 or later and administrator credentials for the account.

1. Bootstrap the account:

   ```sh
   cd infra/bootstrap
   terraform init
   terraform apply -var state_bucket=rlp-tfstate-<account id> -var 'github_repositories=["<owner>/<repo>"]'
   ```

   Keep `terraform.tfstate` from this directory somewhere safe. The outputs are the values for step 3.

2. Create each environment's shared platform:

   ```sh
   cd infra/platform
   terraform init -backend-config=bucket=rlp-tfstate-<account id> -backend-config=key=rlp/platform/dev.tfstate -backend-config=region=us-east-1
   terraform apply -var environment=dev
   ```

3. In each project repo, add the bootstrap outputs as repository variables: `RLP_AWS_PLAN_ROLE_ARN`, `RLP_AWS_DEPLOY_ROLE_ARN`, `RLP_TF_STATE_BUCKET` and `RLP_AWS_REGION`. They are identifiers, not secrets.

4. Call the golden pipeline with a `deploy-env` and grant `id-token: write`:

   ```yaml
   jobs:
     golden:
       uses: rdlucas2/rlp/.github/workflows/golden.yml@<rlp commit SHA or tag>
       with:
         rlp-version: "0.2.0"
         deploy-env: dev
         aws-region: ${{ vars.RLP_AWS_REGION }}
         aws-plan-role-arn: ${{ vars.RLP_AWS_PLAN_ROLE_ARN }}
         aws-deploy-role-arn: ${{ vars.RLP_AWS_DEPLOY_ROLE_ARN }}
         tf-state-bucket: ${{ vars.RLP_TF_STATE_BUCKET }}
       permissions:
         contents: write
         packages: write
         security-events: write
         id-token: write
   ```

5. The first deploy creates the secrets empty, so its tasks can't start yet. Put each value, then re-run the workflow:

   ```sh
   aws secretsmanager put-secret-value --secret-id rlp/dev/<repo>/<component>/<NAME> --secret-string '<value>'
   ```

The image must be pullable without credentials: make the GHCR package public.

## The pipeline

The `deploy` job in `golden.yml` runs after the gates (and after publish, on the default branch) when `deploy-env` is set:

1. It installs the pinned Terraform, checked against HashiCorp's published SHA256.
2. It exchanges the job's GitHub OIDC token for a session of `rlp-plan` (pull requests and other refs) or `rlp-deploy` (pushes to the default branch). `rlp-deploy` trusts only the default branch.
3. The `terraform` stage renders the stack, runs `terraform init` and `terraform plan`, and checks the plan JSON with the `rlp.terraform` policy rules. A plan that drops a required tag, makes an S3 bucket public or stores data unencrypted fails here.
4. The `cost` stage prices the plan with Infracost (see below). A plan over the repo's budget stops here.
5. On the default branch, the `deploy` stage applies that saved plan. ECS waits for the service to become stable and rolls back if the new tasks never get healthy. The stage then requests each readiness path through the load balancer until it answers 200 (5 minutes at most).

The job uploads `results.json`, the plan JSON and Infracost's report as the `rlp-deploy-results` artifact.

## Cost estimates

The `cost` stage runs the pinned [Infracost](https://www.infracost.io/) CLI on the plan JSON. It puts the estimated monthly total, the change against what's deployed now, and the most expensive resources in the job summary and in `results.json`, and writes Infracost's full report to `.rlp/pipeline/reports/infracost.json`.

Set a budget in `rlp.yaml` to make it a gate. The stage fails when the estimate for an environment is above it, before anything is applied:

```yaml
quality:
  cost:
    monthlyBudget: 50   # USD per month, per environment
```

Infracost prices resources through its hosted pricing API, which needs a free API key (`infracost auth login`, or sign up at infracost.io). Add it as the repository secret `INFRACOST_API_KEY` and pass it to the golden pipeline:

```yaml
    uses: rdlucas2/rlp/.github/workflows/golden.yml@<ref>
    secrets:
      infracost-api-key: ${{ secrets.INFRACOST_API_KEY }}
```

Infracost needs no AWS credentials: it reads the plan JSON and asks its API for the prices of the resource types it finds. With no key, the stage is skipped when no budget is declared, and fails when one is, so a budget is never silently unchecked. If Infracost can't reach its pricing API, or finds none of the plan's resources, the stage fails rather than report $0.

What the estimate covers: what Terraform declares for the app stack, mainly the Fargate task size and count. It leaves out:

- usage-based charges such as data transfer and log ingestion (no `infracost-usage.yml` yet)
- the shared platform (the load balancer costs about the same however many services use it) and the bootstrap stack

Locally: `INFRACOST_API_KEY=... rlp pipeline run -s terraform -s cost --deploy-env dev`.

## Locally

```sh
export RLP_REGISTRY=ghcr.io/<owner>/<repo> RLP_TF_STATE_BUCKET=rlp-tfstate-<account id> AWS_REGION=us-east-1
rlp infra render --env dev                     # write .rlp/infra/dev/main.tf.json
rlp pipeline run -s terraform --deploy-env dev  # plan and policy-check it with your own AWS credentials
rlp infra destroy --env dev                    # tear the app stack down
```

`--modules <dir>` (or `RLP_MODULES_SOURCE`) uses modules from a checkout instead of the ones tagged with your rlp version.

## Not yet

- DNS names and HTTPS (Route 53 and ACM).
- RDS for `postgres`, and AWS mappings for redis, s3, sqs and sns.
- Lambda, S3 + CloudFront and scheduled ECS tasks.
- Per-component CPU, memory and task count in `rlp.yaml`.

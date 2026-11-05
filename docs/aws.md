# Deploying to AWS

A `container-service` component runs on ECS Fargate behind an Application Load Balancer. The golden pipeline plans the change on every pull request, checks the plan against the policy bundle, and applies it on pushes to the default branch. GitHub Actions authenticates with OIDC, so no AWS keys are stored in GitHub. See #16.

## The pieces

| Piece | Where | Who runs it | How often |
| --- | --- | --- | --- |
| Bootstrap: state bucket, GitHub OIDC provider, `rlp-plan` and `rlp-deploy` roles | `infra/bootstrap` | an administrator, by hand | once per AWS account |
| Platform: shared ECS cluster, load balancer, HTTP listener; optionally an HTTPS listener, certificate and Cognito sign-in for public hosts | `infra/platform` | an administrator, by hand | once per environment |
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

Clients reach a service at the load balancer's DNS name with the service's host in the `Host` header; the deploy stage does the same. To give a component a real DNS name over HTTPS, see [Public hosts](#public-hosts-https-and-sign-in).

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

3. Onboard each project repo with `rlp onboard` (see [Onboard a repo](#onboard-a-repo)), or by hand: re-apply the bootstrap with the repo added to `github_repositories`, then add the bootstrap outputs as repository variables: `RLP_AWS_PLAN_ROLE_ARN`, `RLP_AWS_DEPLOY_ROLE_ARN`, `RLP_TF_STATE_BUCKET` and `RLP_AWS_REGION`, plus `RLP_DEPLOY_ENV`, the environment its pipeline deploys to. They are identifiers, not secrets.

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

## Onboard a repo

`rlp onboard OWNER/REPO` does step 3 in one command. Run it where the bootstrap's state lives, with the same administrator AWS credentials and a `gh` login that has admin access to the repo:

```sh
rlp onboard rdlucas2/hello-api --dry-run          # show what would change
INFRACOST_API_KEY=... rlp onboard rdlucas2/hello-api --env dev
```

It:

1. reads the bootstrap's outputs with `terraform output -json` (from `infra/bootstrap`, or `--bootstrap-dir`);
2. when the repo isn't in `github_repositories` yet, runs `terraform apply` there with every bootstrap variable passed explicitly in a var file: the current values, with the repo appended to the list, so nothing else changes. Terraform shows the plan and asks for approval unless you pass `--yes`;
3. sets `RLP_AWS_REGION`, `RLP_AWS_PLAN_ROLE_ARN`, `RLP_AWS_DEPLOY_ROLE_ARN`, `RLP_TF_STATE_BUCKET` and `RLP_DEPLOY_ENV` (from `--env`, default `dev`) on the repo with `gh variable set`;
4. when `INFRACOST_API_KEY` is set in your environment and the repo has no such secret yet, sets it with `gh secret set`, passing the value on stdin, never on the command line. An existing secret is kept; replace it with `gh secret set` yourself.

Running it again on an onboarded repo prints "already onboarded" and changes nothing. `--output json` needs `--yes` when the bootstrap must be applied.

A bootstrap applied before `rlp onboard` existed has no `github_repositories` and `deploy_branch` outputs yet. `rlp onboard` says so; run `terraform apply` once in `infra/bootstrap` with your usual variables to record them.

Still manual, after the repo's first publish:

- make its GHCR package public (GitHub has no API for package visibility on personal accounts);
- mark the pipeline's checks as required in the repo's branch protection, if you want them to gate merges.

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

By default the modules come from the same rlp as your CLI: a release install uses its version's tag, an install from a git commit (`pip install "rlp-cli @ git+https://github.com/rdlucas2/rlp@<ref>"`) uses that commit, and an install from a checkout uses the checkout's `infra/modules`. `--modules <dir or go-getter URL>` (or `RLP_MODULES_SOURCE`) overrides it.

## Public hosts: HTTPS and sign-in

The platform stack can serve a component under a real DNS name, such as `rlp.example.com`, over HTTPS, optionally behind a sign-in. DNS stays wherever your domain lives (a registrar such as Squarespace, Cloudflare, Route 53): rlp needs no hosted zone, and you create two kinds of CNAME record there yourself.

Give the platform stack the hosts, and who may sign in, in `infra/platform/terraform.tfvars` (gitignored):

```hcl
environment = "dev"
region      = "us-east-2"

public_hosts = {
  "rlp.example.com" = {
    repo         = "rlp"      # metadata.name of the rlp.yaml
    component    = "platform"
    authenticate = true       # the default
    # Reached without signing in, by clients with their own credentials:
    # the CLI and pipeline (bearer token) and the GitHub webhook (signature).
    open_paths = ["/api/*", "/healthz"]
  }
}

auth_users = ["you@example.com"]
```

For each public host the platform stack adds:

| What | Why |
| --- | --- |
| an ACM certificate covering every public host, validated by DNS | TLS on the load balancer |
| an HTTPS listener on 443 (TLS 1.2 and 1.3) and a 443 ingress rule for `allowed_cidrs` | the public host is served there |
| a rule on the HTTP listener that redirects the host to HTTPS | the public host is HTTPS only |
| with `authenticate = true`: a Cognito user pool with sign-up closed, its hosted sign-in domain and an app client for the load balancer, and a user per `auth_users` entry | the load balancer signs people in before it forwards a request |
| SSM parameters under `/rlp/<env>/platform/` and `/rlp/<env>/public/<repo>/<component>` | the component's stack finds the listener, the user pool and its host there |

The component's stack (the `container-service` module, through the golden pipeline) then adds, on its next deploy:

- a rule on the HTTPS listener for the public host, which runs the Cognito sign-in first when `authenticate = true`;
- before it, a rule that forwards the component's health path and `open_paths` without sign-in;
- with `authenticate = true`, it narrows the internal host's rule on the HTTP listener (`<component>-<repo>.<env>.<domain>`) to those same open paths, so the UI can't be reached around the sign-in. The deploy stage's readiness check and `RLP_PLATFORM_HOST` clients keep working; a hosts-file entry for the internal host no longer reaches the UI.

The sign-in is in front of what the component already does: the platform's API still needs its bearer token, and the UI still asks for it once per browser session.

### Set it up

1. Re-apply `infra/bootstrap` with your usual variables: the deploy role needs `cognito-idp:DescribeUserPoolClient` (on rlp-tagged user pools) to create a rule with a sign-in.
2. Create the certificate on its own, then read the records that prove you own the names:

   ```sh
   cd infra/platform
   terraform apply -target=aws_acm_certificate.this
   terraform output certificate_validation_records
   ```

3. At your DNS provider, create each validation record as a CNAME. Most providers want the host relative to the domain: for `_3f2a….rlp.example.com.` on `example.com`, the host is `_3f2a….rlp`. In Squarespace: **Domains**, the domain, **DNS** (DNS Settings), **Custom records**, **Add record**, type CNAME, host as above, data the record's value.
4. Apply the rest. The apply waits until ACM has issued the certificate (usually minutes after the record resolves; up to 2 hours before it gives up):

   ```sh
   terraform apply
   terraform output dns_records
   ```

5. Point each public host at the load balancer: a CNAME from the host (`rlp` for `rlp.example.com`) to the load balancer's DNS name in `dns_records`. A CNAME can't sit at the domain's apex, so a public host is always a subdomain unless your DNS provider offers ALIAS or flattening.
6. Re-run the component's pipeline (or `rlp infra` locally) so its stack adds the public rules. The output `public_url` of its module names the host.
7. Each `auth_users` address gets an email from Cognito with a temporary password. Open the public host, sign in, set a password, and optionally set up an authenticator app (MFA is optional per user). Add more users later with `auth_users`, or with `aws cognito-idp admin-create-user --user-pool-id <auth_user_pool_id> --username <email>`.

For the platform itself, point GitHub's webhook at `https://<public host>/api/v1/hooks/github` and set `RLP_PLATFORM_URL=https://<public host>` (no `RLP_PLATFORM_HOST` needed).

The certificate renews on its own as long as the validation records stay in place. Cognito's built-in email sender is limited to a few dozen messages a day, enough for invitations and password resets for a handful of users.

## Not yet

- Route 53 hosted zones (rlp creates no DNS records; see [Public hosts](#public-hosts-https-and-sign-in)).
- Public hosts declared in `rlp.yaml`: for now the platform stack's `public_hosts` maps them.
- RDS for `postgres`, and AWS mappings for redis, s3, sqs and sns.
- Lambda, S3 + CloudFront and scheduled ECS tasks.
- Per-component CPU, memory and task count in `rlp.yaml`.

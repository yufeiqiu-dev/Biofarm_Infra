# Biofarm_Infra

Python CDK for the Oasis Biofarm infrastructure. Point it at an empty AWS
account, run `cdk deploy`, and get a complete environment.

The application repositories are separate: `Biofarm_Backend` (FastAPI) and
`Biofarm_Frontend` (React/Vite). This repository creates what they run on.

## Layout

| Stack | Instances | Holds |
|---|---|---|
| `Biofarm-Cicd` | one | GitHub OIDC provider, the deploy role, the ECR repository |
| `Biofarm-Network` | one | VPC, subnets, NAT instance, isolation between environments |
| `Biofarm-Data-<env>` | per environment | RDS, Cognito, S3 + CloudFront, credentials |
| `Biofarm-App-<env>` | per environment | App Runner, its VPC connector, the scheduled cleanup job |
| `Biofarm-Web-<env>` | per environment | Amplify app and branch for the frontend |

One ECR repository serves both environments rather than one each, so promoting
staging to production retags a digest that has already been tested instead of
rebuilding and hoping the result is identical.

Two environments, `staging` and `prod`, defined in `config.py`. That file is the
only place they differ - the stacks contain no environment branching of their own.

## Two decisions worth knowing before you read the code

**Staging and production share one VPC.** This halves the NAT cost, and it means
security groups are the only thing separating staging from production data. So
the isolation is made structural rather than incidental: each environment gets
its own private subnet group, network ACLs deny traffic between them, and the
database accepts traffic only from its own environment's security group. Removing
any of that fails the tests, which was checked rather than assumed. Treat those
tests as load-bearing, not decoration.

**Staging is stopped when idle.** `scripts/staging_power.py up|down|status`
stops its database and pauses its App Runner service, taking the environment from
roughly $28 a month to near the storage floor. Worth knowing: AWS force-starts a
stopped RDS instance after 7 days and there is no way to disable that, so `down`
is a scheduled action rather than a switch that stays off.

## Running it

Requires Python 3.11+, Node (for the CDK CLI), and an AWS SSO profile.

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1          # Windows; POSIX: source .venv/bin/activate
pip install -r requirements.txt

pytest tests/ -q                    # no AWS account needed
npx aws-cdk synth                   # both environments, offline
```

Against a real account:

```bash
aws sso login --profile biofarm-prod
npx aws-cdk bootstrap --profile biofarm-prod        # once per account and region
npx aws-cdk diff   --profile biofarm-prod           # always read this first
npx aws-cdk deploy --profile biofarm-prod Biofarm-Cicd
```

The stacks are environment-agnostic, so account and region come from the profile
and the same code deploys anywhere without edits.

## Tests

`pytest tests/` asserts against the synthesized CloudFormation template - no
account, no credentials, no network. They exist to catch the failures that stay
invisible until they matter:

- each database accepts traffic only from its own environment
- network ACLs deny traffic between the environments' subnets
- the NAT instance is not reachable from the internet
- production refuses to be deleted; staging is disposable
- no Stripe key appears anywhere in any template
- only the deploying branches can assume the deploy role

## Credentials

None are in this repository, and none reach a CloudFormation template. A test
greps every synthesized stack for key prefixes to keep it that way.

They live in two places, for two different reasons.

**The database password is in Secrets Manager.** RDS generates it there itself
and rotation is built in. The alternative is supplying a password, which means it
exists somewhere in this repo or in CDK context and lands in the template in
plaintext. Nothing to do by hand.

**Stripe keys are in SSM Parameter Store.** App Runner reads either source, and
standard SSM parameters are free where Secrets Manager is $0.40 per secret per
month - and nothing here needs rotation or cross-account sharing, so the paid
features would go unused. CDK creates them holding `replace-me`; writing the real
values also converts them to SecureString in place:

```bash
aws ssm put-parameter --name /biofarm/prod/stripe-secret-key \
  --value "sk_live_..." --type SecureString --overwrite --profile biofarm-prod

aws ssm put-parameter --name /biofarm/prod/stripe-webhook-secret \
  --value "whsec_..."  --type SecureString --overwrite --profile biofarm-prod
```

Two things about the Stripe values specifically:

- **The modes must match.** Staging is `test`, production is `live`, and the
  backend refuses to start if `STRIPE_SECRET_KEY` disagrees with the
  `STRIPE_MODE` this stack sets. Live keys in staging charge real cards; test
  keys in production take no money at all, silently.
- **The webhook secret is per environment and per mode.** It comes from a webhook
  endpoint created in the Stripe Dashboard against that environment's App Runner
  URL. It is **not** the one `stripe listen` prints - that belongs to a local CLI
  session and nothing else.

## Deploying an environment

The first deploy cannot set two values, because they refer to each other:
`CORS_ORIGINS` needs the frontend's domain, and the frontend's
`VITE_API_BASE_URL` needs the App Runner domain. So it is two passes.

```bash
npx aws-cdk deploy --profile <p> Biofarm-Cicd Biofarm-Network
npx aws-cdk deploy --profile <p> Biofarm-Data-staging Biofarm-App-staging

# The frontend needs a GitHub token. AWS::Amplify::App has no declarative way to
# connect a repository, and ssm-secure dynamic references do not work here - that
# pattern is supported on an allowlist of eleven resource properties and Amplify
# is not one of them. So it is a NoEcho parameter, read from the environment so
# it stays out of shell history. Amplify does not retain it.
export GITHUB_TOKEN=...
npx aws-cdk deploy --profile <p> Biofarm-Web-staging   --parameters GitHubAccessToken=$GITHUB_TOKEN
```

Then, before the service can become healthy:

1. **Write the Stripe values** into the SSM parameters (see above). The service
   will not pass its health check until they are real - the backend refuses to
   start with a placeholder, which is the intended behaviour.
2. **Push an image.** App Runner points at `biofarm-backend:<env>` in ECR, and
   nothing is there until CI has run once.
3. **Create the webhook endpoint** in the Stripe Dashboard, in the matching mode,
   against the service URL from the stack output. Put the signing secret it gives
   you into that environment's `stripe-webhook-secret` parameter.
4. **Set `CORS_ORIGINS`** to the frontend origin and deploy again.
5. **Console:** add the Amplify branch URL to the Cognito app client's callback
   and sign-out URLs, or sign-in fails at the redirect with an error page from
   Cognito rather than from this application.
6. **Console:** set `VITE_STRIPE_PUBLISHABLE_KEY` on the Amplify branch. It is
   publishable by design and ships inside the bundle, so it is a plain
   environment variable rather than a secret.
7. **Console:** create your own user in the environment's Cognito pool and put
   it in the `Admin` group. The group name is case-sensitive and checked in
   three places.
8. **Staging only — seed the test accounts:**

   ```bash
   python scripts/seed_test_users.py --profile <p>
   ```

   The stack creates the two end-to-end accounts and generates a password for
   each in Secrets Manager, but it cannot make them usable.
   `AWS::Cognito::UserPoolUser` has no password property - not a permanent one
   and not even a temporary one - so an account created by a deploy always lands
   in `FORCE_CHANGE_PASSWORD`, and `AdminSetUserPassword` is the only API that
   moves it out. The script applies the generated password. It is idempotent, so
   CI can run it before every suite.

   Production has no test accounts and the script refuses to touch it. A seeded
   account with a machine-readable password is a way in; a test asserts the
   production template contains no user, no group attachment and no password
   secret.

Deployments are not triggered by an ECR push. Both environments share one
repository, so an automatic trigger would ship whatever landed under a tag,
including a push meant for the other environment. CI starts the deployment
explicitly.

## Status

All five stacks are built, with 134 tests, all offline.

Still to come: the `staging` branches and CI workflows in the application
repositories, and the Playwright suite that signs in as the seeded accounts.

Nothing has been deployed to an account yet. See `documentation/launch/` in
`Biofarm_KnowledgeBase` for the wider launch plan this fits into.

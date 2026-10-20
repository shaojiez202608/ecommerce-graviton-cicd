# ecommerce-graviton-cicd

> **Disclaimer — prototype for demonstration and learning, not for production use.**
> - Deploy it only into a non-production, preferably disposable, AWS account.
> - You are responsible for what you deploy, how you use it, and the consequences (including cost).
> - Review it with your own security and legal teams before any further use, following the
>   [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/).
>   The [hardening checklist](docs/handover.md#4-上生产前需要加固的清单) lists what production needs.

A Graviton-first test environment and CI/CD reference for a self-hosted **Magento Open Source**
storefront on AWS, plus a bridge that fulfils storefront orders through **Amazon Multi-Channel
Fulfillment (MCF)**. Scenario (generic): an IoT device maker launches an overseas direct-to-consumer
store, starts with test environments, and ships orders from FBA inventory.

## Architecture

Details, trust boundaries, trade-offs and the fact check: [docs/architecture.md](docs/architecture.md)
(the mermaid diagram there is the source of truth).

```
<PROJECT>-cicd            ECR, S3 source/artifact buckets, arm64 image build, pipelines, approvals
<PROJECT>-<ENV>-platform  VPC, internal ALB (http, or https:443 with an ACM certificate), ECS Fargate
                          ARM64, RDS MySQL, Valkey, OpenSearch, EFS, alarms, in-VPC deploy project
<PROJECT>-<ENV>-mcf       EventBridge bus, SQS + DLQ, DynamoDB, 3 arm64 Lambda functions, alarms
(<PROJECT> comes from the profile; default proto-ecom)
```

What is different from existing samples ([docs/prior-art.md](docs/prior-art.md)):

1. **Magento-specific release order inside a build-once / promote-everywhere pipeline** — immutable
   arm64 image (composer, `setup:di:compile`, `setup:static-content:deploy` at build time,
   `config.php` from Git), `env.php` rendered at runtime from Secrets Manager/SSM, one serialized
   migration task per environment (MySQL named lock) whose maintenance window spans the service
   switch, `cache:flush` after the rollout, and a per-environment policy for failed releases. Two
   pipelines (`develop` → dev; `release` → test → approval → uat (blue/green) → approval → prod
   (blue/green)), stage rollback and ECS-level rollback. Generic blue/green with pre-traffic checks
   already exists in AWS samples (prior-art A12/A13); the difference is the Magento order around it.
2. **All-Graviton stack** with a per-dependency arm64 checklist
   ([docs/graviton-compatibility.md](docs/graviton-compatibility.md)).
3. **Storefront ↔ MCF bridge (one half of the loop)**: SKU ↔ MSKU mapping, inventory sync per
   marketplace/SP-API region, idempotent `createFulfillmentOrder` with retry-then-alert and explicit
   DLQ redrive, cancellation before shipment, tracking write-back, transactional outbox. The SP-API
   layer defaults to a **mock**. **The storefront side is not implemented**: the Magento module that
   publishes `Order Paid`/`Order Cancelled` and consumes shipment/inventory events is only an event contract.

## Prerequisites and cost

- A non-production AWS account and region with CDK bootstrap already done (this project never
  modifies the CDKToolkit stack); Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js (for
  `npx -y aws-cdk@2`), AWS CLI v2, git, zip. No local Docker: images are built on CodeBuild (arm64).
- Parameters and prerequisites in detail: [docs/deployment-guide.md](docs/deployment-guide.md).
- **Cost**: running stacks are billed per service dimension — Fargate vCPU/memory-hours, RDS
  instance-hours and storage, ElastiCache node-hours, OpenSearch instance-hours and storage, NAT
  gateway hours and data processed, ALB hours and LCUs, EFS storage, CodeBuild build minutes,
  CodePipeline pipelines, Lambda requests/duration, DynamoDB on-demand requests, CloudWatch logs and
  alarms. See each service's official pricing page (start at https://aws.amazon.com/pricing/); no free
  tier is assumed. Consider an [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html)
  alert on the sandbox account, and destroy the stacks when you are done.

## Deployment

```bash
make init                       # uv sync + config/*.json copied from config/*.example.json (edit them)
CONFIRM=<LAST_4_DIGITS> make preflight   # read-only checks, refuses destructive diffs
CONFIRM=<LAST_4_DIGITS> make deploy      # = preflight + cdk deploy --all for ECOM_CONFIG (default config/sandbox.json)
git commit ...                  # the image tag is the commit SHA
scripts/package_source.sh develop   # start the develop pipeline with the committed tree
```

`ECOM_CONFIG=config/customer.json` deploys the four-environment handover profile once its blank
instance classes (and, for uat/prod, the certificate ARN and host name) are filled in. Re-deploying a
platform stack later: pass the running image tags, see
[docs/deployment-guide.md](docs/deployment-guide.md#首次部署之后).

## Usage and verification

- `make verify` — pipeline results, deny-path checks (secret resource policies, deploy-role scope,
  trust conditions, protected promotion) and the MCF end-to-end run with the outbox drill.
- Walk-through: [docs/demo-script.md](docs/demo-script.md). Operations (promotion, rollback, DLQ
  redrive, maintenance policy): [docs/handover.md](docs/handover.md).
- What was actually run and what it does not prove: [VERIFICATION.md](VERIFICATION.md),
  [DEPLOYMENT-LOG.md](DEPLOYMENT-LOG.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `configuration file ... not found; run make init` | Real profiles are local copies of `config/*.example.json` |
| `... not set; choose a Graviton instance class` | The handover profile leaves sizing blank on purpose; fill it in |
| preflight `refusing: the change deletes or replaces resources` | Inspect `/tmp/preflight-diff.txt`; re-run with `ALLOW_DESTRUCTIVE=1` (and `ALLOW_DATA_REPLACEMENT=1` for uat/prod data stores) only after a backup |
| Deploy stage waits on `waiting for scan (status PENDING)` | Account-level enhanced scanning has no result yet; profile `scan_unavailable_action` decides (`warn` in the sandbox) |
| Build fails at `check-modules` | `config.php` does not match the modules; run `scripts/export_magento_config.sh` and commit |
| Deploy fails after `upgrade required`; site shows maintenance | The environment's `maintenance_on_failure=keep`; see handover §3 |
| Service back at desiredCount 0 after `cdk deploy` | Pass `-c imageTags=<ENV>=<TAG>` (current tag in SSM `<SSM_PREFIX>/<ENV>/current_image_tag`) |

## Security and limitations

[SECURITY.md](SECURITY.md) (controls and data handling), [SECURITY-NOTES.md](SECURITY-NOTES.md)
(least privilege, cdk-nag acknowledgements, known exceptions), [THREAT_MODEL.md](THREAT_MODEL.md)
(draft). Limitations: [docs/architecture.md](docs/architecture.md#6-局限). Decisions and open
questions: [DECISIONS.md](DECISIONS.md), [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## Cleanup

```bash
CONFIRM_DESTROY=<PROJECT> CONFIRM_DATA=yes make destroy   # asks twice when run interactively
make verify-cleanup                                       # non-zero exit if anything is left
```

Not deleted automatically by `cdk destroy` — handled by `make destroy` where possible, otherwise listed
by `make verify-cleanup` (details in [docs/cleanup.md](docs/cleanup.md)): ECS task-definition revisions
registered by the deploy script; log groups created by services (RDS log exports, Container Insights,
CDK handler functions); secrets in their scheduled-deletion window; pre-deploy RDS snapshots
(uat/prod); retained data in the handover profile (`removal_policy=retain`: databases, caches, search
domains, file systems, buckets, repositories, secrets); this project's objects in the shared CDK
bootstrap bucket (removed by asset hash; the CDKToolkit stack and CDK's shared provider packages
stay). Do not assume a finished command means everything is gone — check the residual report.

## Repository layout

| Path | Content |
|---|---|
| `infra/` | CDK app (Python) and stacks; `nag.py` acknowledgements; `trust.py` trust conditions |
| `config/` | Profile templates (`*.example.json`; local copies are gitignored) |
| `app/magento/` | Immutable Magento image (arm64), committed `config.php`, project modules, runtime scripts |
| `app/smoke/` | Lightweight stand-in image to exercise the pipeline |
| `buildspec/`, `scripts/deploy/`, `scripts/build/` | Build, scan gate and promotion logic |
| `scripts/` | Preflight, destroy, residual check, verification, operator tools |
| `src/mcf/` | MCF bridge (Lambda) with mock and live SP-API clients |
| `tests/`, `local/`, `sample-data/` | Tests, local demo, synthetic data |
| `.github/` | CI and deploy-verification workflows (not run on GitHub yet), templates, CODEOWNERS |

## Local testing

```bash
make lint test synth scan       # no AWS account needed
uv run python -m local.run_mcf_demo   # MCF flow end to end with the mock SP-API
```

## Contributing

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## Security

See [SECURITY](SECURITY.md) for how to report security issues.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
Third-party components keep their own licenses: [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES).
Magento Open Source (OSL-3.0) is not included; it is fetched from the official repository at build time.

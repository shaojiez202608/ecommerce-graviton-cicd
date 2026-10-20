# ecommerce-graviton-cicd

> **Prototype — not for production use.** For non-production test environments only. See
> [Before production](#before-production-hardening-checklist).

A Graviton-first test environment and CI/CD reference for a self-hosted **Magento Open Source**
storefront on AWS, plus a small bridge that fulfils storefront orders through **Amazon
Multi-Channel Fulfillment (MCF)**. Scenario (generic): an IoT device maker launches an overseas
direct-to-consumer store, starts with test environments, and ships orders from FBA inventory.

Suggested repository name: `ecommerce-graviton-cicd`.

## What is different from existing samples

See [docs/prior-art.md](docs/prior-art.md). In short, this prototype focuses on:

1. **Magento-specific CI/CD steps inside a build-once / promote-everywhere pipeline** — immutable
   image (composer, `setup:di:compile`, `setup:static-content:deploy` at build time), `env.php`
   rendered at runtime from Secrets Manager/SSM, a single serialized migration task per
   environment with maintenance mode, and `cache:flush` after the service is stable. Two
   pipelines (`develop` → dev; `release` → test → approval → uat (blue/green) → approval → prod
   (blue/green)), stage rollback and ECS-level rollback.
2. **All-Graviton stack** with a per-dependency arm64 checklist
   ([docs/graviton-compatibility.md](docs/graviton-compatibility.md)): ECS on Fargate ARM64,
   CodeBuild ARM, RDS for MySQL 8.4, ElastiCache for Valkey, OpenSearch Service, Lambda arm64.
3. **Storefront ↔ MCF glue**: SKU ↔ MSKU mapping and validation, inventory sync events,
   idempotent `createFulfillmentOrder` with retry-then-alert, tracking write-back events. The
   SP-API layer defaults to a **mock**; no real seller account or credentials are involved.

It does not re-implement Magento features (cart, checkout, payments) or the SP-API operation
samples that Amazon already publishes.

## Architecture

See [docs/architecture.md](docs/architecture.md) (diagram, trade-offs, limitations, fact check).

```
proto-ecom-cicd            ECR, S3 source/artifact buckets, arm64 image build, pipelines, approvals
proto-ecom-<env>-platform  VPC, internal ALB, ECS Fargate ARM64, RDS MySQL, Valkey, OpenSearch, EFS,
                         alarms, in-VPC deploy project
proto-ecom-<env>-mcf       EventBridge bus, SQS + DLQ, DynamoDB, 3 arm64 Lambda functions, alarms
```

## Quick start

```bash
uv sync
uv run pytest                                   # unit tests + CDK assertions (no AWS needed)
uv run python -m local.run_mcf_demo             # MCF flow end to end with the mock SP-API

# deploy to a sandbox account (one environment + pipeline + MCF bridge)
npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never
scripts/package_source.sh develop               # start the pipeline with the committed tree
```

All parameters live in one file (`config/sandbox.json` or `config/customer.json`). Guides:
[deployment](docs/deployment-guide.md) · [demo script](docs/demo-script.md) ·
[handover](docs/handover.md) · [cleanup](docs/cleanup.md).

## Repository layout

| Path | Content |
|---|---|
| `infra/` | CDK app (Python) and stacks |
| `config/` | Parameter files (sandbox / customer handover) |
| `app/magento/` | Immutable Magento image (arm64) and runtime scripts |
| `app/smoke/` | Lightweight stand-in image to exercise the pipeline |
| `buildspec/`, `scripts/deploy/` | Build and promotion logic |
| `src/mcf/` | MCF bridge (Lambda) with mock and live SP-API clients |
| `tests/`, `local/`, `sample-data/` | Tests, local demo, synthetic data |

## Before production (hardening checklist)

Public entry with CloudFront + WAF + TLS; separate production account; secret rotation and
customer-managed KMS keys; Multi-AZ for data tier; Magento cron and queue consumers; pinned base
images and private dependency mirrors; masked data for UAT; dashboards and on-call alarms;
SP-API sandbox validation, rate limiting and credential rotation; PCI scope review; review every
cdk-nag acknowledgement. Full list: [docs/handover.md](docs/handover.md#4-上生产前需要加固的清单).

## Security and licensing

- Security notes: [SECURITY-NOTES.md](SECURITY-NOTES.md), threat model: [THREAT_MODEL.md](THREAT_MODEL.md),
  reporting: [SECURITY.md](SECURITY.md).
- This repository is licensed under [MIT-0](LICENSE). Magento Open Source (OSL-3.0) is **not**
  included; it is fetched from the official repository at build time.
- Verification results: [VERIFICATION.md](VERIFICATION.md), [DEPLOYMENT-LOG.md](DEPLOYMENT-LOG.md).
  Open questions and decisions: [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md), [DECISIONS.md](DECISIONS.md).

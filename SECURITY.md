# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS/Amazon Security via the
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or by email
to [aws-security@amazon.com](mailto:aws-security@amazon.com). Please do **not** create a public
GitHub issue.

## What this prototype deploys

Into one AWS account and region (see `docs/architecture.md`):

- `<PROJECT>-cicd`: ECR repository, S3 source and artifact buckets, an arm64 CodeBuild image build,
  CodePipeline pipelines with manual approvals, SNS approval topic.
- `<PROJECT>-<ENV>-platform` per environment: VPC (NAT only in public subnets), internal ALB,
  ECS on Fargate (ARM64), RDS for MySQL, ElastiCache for Valkey, OpenSearch Service, EFS,
  Secrets Manager secrets, SSM parameters, CloudWatch alarms, an in-VPC CodeBuild deploy project.
- `<PROJECT>-<ENV>-mcf` per environment: EventBridge bus, SQS queue and DLQ, DynamoDB table,
  three arm64 Lambda functions, a placeholder SP-API secret, alarms.

It is a **prototype for non-production accounts**. Nothing is exposed to the internet.

## How data is handled

| Data | Where | Protection (implementation) |
|---|---|---|
| Shop database (Magento schema) | RDS for MySQL in isolated subnets | Encryption at rest, `require_secure_transport=ON`, CA bundle in the image (`platform_stack.py`, `build-env.php`) |
| Sessions, cache, full-page cache | ElastiCache Valkey | At-rest + in-transit encryption, AUTH token from Secrets Manager (`platform_stack.py`) |
| Catalog search index | OpenSearch VPC domain | Encryption at rest, node-to-node TLS, HTTPS only, fine-grained access control (`platform_stack.py`) |
| Media and `var/` | EFS | Encrypted, TLS + IAM-authorised access points (`platform_stack.py`) |
| Order events incl. shipping addresses | SQS (in transit), DynamoDB stores order state only | SQS-managed encryption, TLS-only queue policy; addresses are not logged (`mcf_stack.py`, `service.py`) |
| Credentials (DB, search, cache, crypt key, admin, SP-API) | Secrets Manager | Generated, injected through ECS secrets; resource policies deny reads except to the consuming roles (`platform_stack.py` `_lock_secrets`, `mcf_stack.py`) |
| Build artifacts and images | S3, ECR | Block Public Access, TLS-only, SSE; immutable tags, scan gate with quarantine (`cicd_stack.py`, `scan_gate.py`) |

Only synthetic sample data (`sample-data/`) is used. No payment card data is processed.

## Controls

| Control | Implementation |
|---|---|
| No public ingress; no `0.0.0.0/0` ingress (validated at load time) | `infra/ecom_infra/config.py`, `platform_stack.py` (internal ALB, security groups) |
| TLS for https environments (ACM certificate, ELB post-quantum TLS policy) | `platform_stack.py`, `scripts/deploy/smoke_test.sh` |
| Least-privilege roles; resource-scoped grants; conditions on RunTask/PassRole | `platform_stack.py` `_deploy_project`, `cicd_stack.py`, `mcf_stack.py` |
| Confused-deputy conditions (`aws:SourceAccount`, `aws:SourceArn`) on service-role trust policies | `infra/ecom_infra/trust.py` |
| cdk-nag AwsSolutions with construct-scoped, reasoned acknowledgements | `infra/ecom_infra/nag.py`, `SECURITY-NOTES.md` |
| Image scan gate at build, re-check before every deploy, quarantine of blocked images | `scripts/build/scan_gate.py`, `buildspec/build.yml`, `scripts/deploy/ecs_deploy.sh` |
| Manual approvals before uat/prod; protected out-of-pipeline promotion | `cicd_stack.py`, `scripts/promote_image.sh` |
| Destructive-change guard before deploy | `scripts/preflight.sh`, `scripts/check_destructive.py` |
| Live SP-API mode refuses placeholder credentials | `src/mcf/mcf_bridge/handlers.py` |
| Logs with retention; VPC flow logs | stacks (`logs.LogGroup` with retention) |

## Known caveats

See `SECURITY-NOTES.md` ("已知风险与例外"): CDK-provided handler Lambdas run on x86_64; TLS ends at
the ALB; tasks reach AWS APIs through NAT on 443; `promote_image.sh --break-glass` bypasses the
pipeline approval by design (audited reason); MCF events are delivered at least once; services
whose trust policies have no documented confused-deputy keys are listed there.

## Before production

Follow the hardening checklist in `docs/handover.md` §4: public entry with CloudFront + WAF,
separate production account, secret rotation and customer-managed KMS keys, Multi-AZ data tier,
end-to-end TLS if required, pinned base images and private dependency mirrors, masked UAT data,
dashboards and on-call alarms, SP-API sandbox validation, PCI scope review, and a review of every
cdk-nag acknowledgement.

## Cleanup

See README "Cleanup": `make destroy` (two confirmations, data deletion confirmed separately) and
`make verify-cleanup` (non-zero exit when anything is left).

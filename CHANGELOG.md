# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-21

### Added
- CDK app with three stack types (CI/CD, per-environment platform, MCF bridge) and two profiles
  (sandbox, four-environment handover) shipped as templates with input validation.
- Immutable Magento 2.4.8 image for linux/arm64 built on CodeBuild; committed `config.php`;
  single serialized migration task (MySQL named lock) with a maintenance window that spans the
  service switch; cache flush after the rollout; HTTPS listener for https environments.
- Scan gate with quarantine of blocked images and re-check before every promotion; per-environment
  image tags; protected promotion for approval-gated environments.
- MCF bridge: SKU mapping, inventory sync per SP-API region, idempotent order submission with
  transactional outbox, DLQ redrive, cancellation, tracking write-back; mock SP-API.
- Confused-deputy conditions on service-role trust policies; deny-by-default secret policies;
  construct-scoped cdk-nag acknowledgements.
- Preflight with destructive-change guard, cleanup and residual-check scripts, bootstrap-asset
  cleanup for this project's objects, CI and deploy-verification workflows (not run on GitHub).

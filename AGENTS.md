# AGENTS.md

Instructions for AI coding assistants working in this repository.

## Role

You maintain a **prototype** (not production) that deploys a Graviton-first Magento test
environment, its CI/CD pipeline and an Amazon MCF fulfillment bridge with AWS CDK (Python).
Keep changes small, tested and safe for a disposable sandbox account.

## Common commands

| Task | Command |
|---|---|
| Create local profiles from the templates | `make init` |
| Lint / unit + CDK tests | `make lint` / `make test` |
| Synthesize both profiles with cdk-nag | `make synth` |
| Secret scan (full history + canary) | `make scan` |
| Read-only pre-deploy checks | `make preflight` |
| Deploy / verify / destroy / residual check | `make deploy` / `make verify` / `make destroy` / `make verify-cleanup` |

## Git workflow

- Conventional Commits with a body (what, why, how verified); end with the co-author line your tool requires.
- Never push to a remote, never rewrite published history, never commit `config/*.json` (only `*.example.json`).

## Boundaries

**Always**
- Run `make lint test synth` before proposing a commit; add tests with every change.
- Use placeholders: `111122223333`, `example.com`, `<UPPER_SNAKE>`; synthetic data only.
- Keep cdk-nag acknowledgements construct-scoped with a reason; update `SECURITY-NOTES.md`.
- Redact 12-digit numbers before writing command output into any file.

**Ask first**
- Any `cdk deploy`, `cdk destroy`, or command that creates, changes or deletes AWS resources.
- Changing IAM policies, trust policies, security groups, secret resource policies or `nag.py`.
- Changing CI workflows, `.gitleaks.toml`, `CODEOWNERS` or this file.
- Adding dependencies or base images.

**Never**
- Commit credentials, account IDs, real customer/shop data or domains.
- Modify the CDKToolkit stack, account-level settings, or resources this project did not create.
- Disable a security control or a test to make a change pass.

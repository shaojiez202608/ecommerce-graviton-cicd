# Contributing

Thanks for your interest. This repository is a prototype; contributions that keep it small,
testable and safe to deploy in a sandbox are welcome.

## Workflow

1. Open an issue describing the change (use the issue forms; security issues go to
   https://aws.amazon.com/security/vulnerability-reporting/ or aws-security@amazon.com, never a public issue).
2. Fork, create a branch, keep one topic per pull request (squash on merge).
3. Run the checks locally (no AWS account needed): `make init lint test synth scan`.
4. Open a pull request using the template: list which checks you ran and what you did not test.

## Tests are required

Every change comes with a test: unit tests for Python logic (`tests/`), CDK assertions for
infrastructure (`tests/test_infra.py`), guard tests for shell/Dockerfile ordering
(`tests/test_config_and_keypoints.py`). A change to deployment behaviour also needs a sandbox
run recorded in `VERIFICATION.md` (date, region, profile, commit, failures, what it does not prove).

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(scope): ...`, `fix`, `docs`,
`test`, `chore`, `ci`, `refactor`. The body says what changed, why, and how it was verified.

## Rules

- Never commit credentials, account IDs, real customer/shop data, domains or payment details. Use
  `111122223333` for account IDs, `example.com` for host names, `<UPPER_SNAKE>` for other placeholders,
  and synthetic sample data. Real configuration files (`config/*.json`) stay local; only
  `config/*.example.json` is committed.
- Every new cdk-nag acknowledgement is attached to a specific construct in `infra/ecom_infra/nag.py`
  with a written reason, and listed in `SECURITY-NOTES.md`.
- Facts about AWS services link to the official documentation (`docs/architecture.md`, "Fact check").
- Do not add price, quota, sizing or latency numbers to the docs; link to the official page.

## AI-assisted contributions

AI tools may be used. You must be able to explain every line you submit, you are responsible for
its correctness and licensing, and the same review and test rules apply. Repository instructions
for coding assistants are in `AGENTS.md`.

## Licensing

By contributing you confirm that you have the right to submit the work and agree that it is
licensed under the MIT-0 license in `LICENSE`. Magento Open Source is not part of this
repository (see `THIRD-PARTY-LICENSES`).

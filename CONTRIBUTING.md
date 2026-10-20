# Contributing

Thanks for your interest. This repository is a prototype; contributions that keep it small,
testable and safe to deploy in a sandbox are welcome.

## Workflow

1. Open an issue describing the change.
2. Fork, create a branch, and keep commits focused.
3. Run the checks locally (no AWS account needed):
   ```bash
   uv sync
   uv run ruff check .
   uv run pytest
   npx -y aws-cdk@2 synth -c config=config/sandbox.json
   ```
4. Scan for secrets before you push (`uvx detect-secrets scan` or `gitleaks detect`).
5. Open a pull request that explains what changed and how you tested it.

## Rules

- Never commit credentials, account IDs, real customer/shop data, domains or payment details.
  Use `<ACCOUNT_ID>`, `<REGION>` and synthetic sample data.
- Every new cdk-nag acknowledgement needs a written reason in `infra/ecom_infra/nag.py` and a
  line in `SECURITY-NOTES.md`.
- Facts about AWS services must link to the official documentation (`docs/architecture.md`,
  section "Fact check").
- Do not add price, quota, sizing or latency numbers to the docs; link to the official page.

## Licensing

By contributing you agree that your contributions are licensed under the MIT-0 license in
`LICENSE`. Magento Open Source is not part of this repository (see `docs/prior-art.md`).

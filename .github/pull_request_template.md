## What and why

<!-- What changed, why, and which issue it addresses. -->

## Checks run

- [ ] `make lint`
- [ ] `make test`
- [ ] `make synth` (both profiles, cdk-nag)
- [ ] `make scan` (gitleaks)
- [ ] Deployed to a sandbox and ran `make verify` / `make verify-cleanup` (say which commit)

## Not tested

<!-- List what you did not run or could not verify, and why. -->

## Hygiene

- [ ] No account IDs (use 111122223333), credentials, real shop/customer data or domains (use example.com)
- [ ] New cdk-nag acknowledgements have a reason in `infra/ecom_infra/nag.py` and `SECURITY-NOTES.md`
- [ ] Commit messages follow Conventional Commits

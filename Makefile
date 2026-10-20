# Single entry point. Targets used by CI (lint, test, synth, scan) only check and never change files.
SHELL := /bin/bash
ECOM_CONFIG ?= config/sandbox.json
CDK := npx -y aws-cdk@2
export ECOM_CONFIG

.PHONY: help init preflight lint test synth deploy verify destroy verify-cleanup scan

help:
	@echo "targets: init preflight lint test synth deploy verify destroy verify-cleanup scan"
	@echo "ECOM_CONFIG=$(ECOM_CONFIG) (copy of config/*.example.json created by 'make init')"

init:  ## create local profiles from the committed templates (never overwrites)
	uv sync
	@for f in config/*.example.json; do t="$${f%.example.json}.json"; \
	  if [ -e "$$t" ]; then echo "keep $$t"; else cp "$$f" "$$t"; echo "created $$t from $$f"; fi; done

preflight:  ## read-only: account/region confirmation, toolchain, availability, destructive-change guard
	bash scripts/preflight.sh

lint:
	uv run ruff check .
	@for f in $$(git ls-files '*.sh'); do bash -n "$$f" || exit 1; done
	@for f in app/magento/docker/*.sh app/smoke/*.sh; do sh -n "$$f" || exit 1; done

test:
	uv run pytest

synth:  ## both profiles (templates), cdk-nag AwsSolutions must be clean
	$(CDK) synth -q -c config=config/sandbox.example.json -o cdk.out/sandbox
	$(CDK) synth -q -c config=config/customer.example.json -c synthCheck=true -o cdk.out/customer

deploy: preflight
	$(CDK) deploy --all -c config=$(ECOM_CONFIG) --require-approval never --concurrency 3

verify:  ## end-to-end checks against the deployed sandbox (see scripts/verify.sh)
	bash scripts/verify.sh

destroy:  ## two confirmations; data deletion confirmed separately; warns and continues per step
	bash scripts/destroy.sh

verify-cleanup:  ## non-zero exit when anything of this project is left
	bash scripts/check_residuals.sh

scan:  ## gitleaks over the full history plus a canary that must be detected
	bash scripts/secret_scan.sh

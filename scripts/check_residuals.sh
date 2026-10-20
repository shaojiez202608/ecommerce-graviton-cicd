#!/usr/bin/env bash
# Residual check after destroy (read-only). Exits non-zero when anything of this project is left.
# Usage: [ECOM_CONFIG=config/<profile>.json] scripts/check_residuals.sh [--region <r> ...] [--prefix <p>]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run --project "$ROOT" python "$ROOT/scripts/check_residuals.py" --config "${ECOM_CONFIG:-config/sandbox.json}" "$@"

# shellcheck shell=bash
# Resolve the resource-name prefix and SSM prefix for operator scripts.
# Precedence: ECOM_PROJECT / SSM_PREFIX environment variables, then the profile named by
# ECOM_CONFIG (default config/sandbox.json, keys "project" and "ssm_prefix").
# Nothing is hard-coded here, so renaming the prefix only means editing the profile.
_ECOM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ECOM_CONFIG="${ECOM_CONFIG:-config/sandbox.json}"
case "$ECOM_CONFIG" in /*) _ECOM_CFG="$ECOM_CONFIG" ;; *) _ECOM_CFG="$_ECOM_ROOT/$ECOM_CONFIG" ;; esac
# Names only: fall back to the committed template when the local copy (make init) does not exist.
[ -f "$_ECOM_CFG" ] || _ECOM_CFG="${_ECOM_CFG%.json}.example.json"
_ecom_cfg() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$_ECOM_CFG" "$1"; }
PROJECT="${ECOM_PROJECT:-$(_ecom_cfg project)}"
SSM_PREFIX="${SSM_PREFIX:-$(_ecom_cfg ssm_prefix)}"
export PROJECT SSM_PREFIX

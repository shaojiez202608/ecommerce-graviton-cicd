#!/usr/bin/env bash
# Operator tool: promote an image tag that already exists in ECR to one environment by running
# that environment's deploy project outside the pipeline (same scripts, same ordering, same scan
# re-check before anything changes).
# Intended for sandbox verification and break-glass use; normal promotion goes through the pipeline.
#
# Environments behind a manual approval in the profile (e.g. uat, prod) are refused unless
# --break-glass "<reason>" is given; the reason is passed to the deploy build (visible in its logs
# and build environment) so the bypass is auditable.
#
# Usage: [ECOM_CONFIG=config/<profile>.json] scripts/promote_image.sh <env> <image-tag> [--break-glass "<reason>"]
set -euo pipefail
ENV_NAME="$1"; TAG="$2"; shift 2
REASON=""
if [ "${1:-}" = "--break-glass" ]; then REASON="${2:?--break-glass needs a reason}"; fi
source "$(dirname "$0")/lib/prefix.sh"
PROTECTED=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(" ".join(sorted({e for p in c["pipelines"] for e in p.get("approvals_before", {})})))' "$_ECOM_CFG")
for p in $PROTECTED; do
  if [ "$p" = "$ENV_NAME" ] && [ -z "$REASON" ]; then
    echo "refusing: $ENV_NAME is promoted through a manual approval in the pipeline." >&2
    echo "use the pipeline, or pass --break-glass \"<reason>\" (recorded in the deploy build)." >&2
    exit 2
  fi
done
ROOT=$(git rev-parse --show-toplevel)
REPO_URI=$(aws cloudformation describe-stacks --stack-name "$PROJECT-cicd" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
aws ecr describe-images --repository-name "${REPO_URI#*/}" --image-ids imageTag="$TAG" >/dev/null
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "${AWS_REGION:-us-east-1}")
BUCKET="$PROJECT-artifacts-$ACCOUNT-$REGION"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/deploy" && cp "$ROOT"/scripts/deploy/*.sh "$ROOT"/scripts/build/scan_gate.py "$TMP/deploy/"
printf '{"imageUri":"%s:%s","imageTag":"%s","revision":"manual","variant":"manual"}\n' "$REPO_URI" "$TAG" "$TAG" > "$TMP/imageDetail.json"
(cd "$TMP" && zip -qr promote.zip imageDetail.json deploy)
KEY="manual/$ENV_NAME-$TAG.zip"
aws s3 cp "$TMP/promote.zip" "s3://$BUCKET/$KEY" --only-show-errors
BUILD=$(aws codebuild start-build --project-name "$PROJECT-$ENV_NAME-deploy" \
  --source-type-override S3 --source-location-override "$BUCKET/$KEY" --artifacts-override type=NO_ARTIFACTS \
  --environment-variables-override "name=PROMOTE_REASON,value=${REASON:-manual promotion (unprotected env)},type=PLAINTEXT" \
  --query build.id --output text)
echo "started ${BUILD} (promote $TAG to $ENV_NAME)"

#!/usr/bin/env bash
# Operator tool: promote an image tag that already exists in ECR to one environment by running
# that environment's deploy project outside the pipeline (same scripts, same ordering).
# Intended for sandbox verification and break-glass use; normal promotion goes through the pipeline.
# Usage: scripts/promote_image.sh <env> <image-tag> [project]
set -euo pipefail
ENV_NAME="$1"; TAG="$2"; PROJECT="${3:-proto-ecom}"
ROOT=$(git rev-parse --show-toplevel)
REPO_URI=$(aws cloudformation describe-stacks --stack-name "$PROJECT-cicd" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)
aws ecr describe-images --repository-name "${REPO_URI#*/}" --image-ids imageTag="$TAG" >/dev/null
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "${AWS_REGION:-us-east-1}")
BUCKET="$PROJECT-artifacts-$ACCOUNT-$REGION"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/deploy" && cp "$ROOT"/scripts/deploy/*.sh "$TMP/deploy/"
printf '{"imageUri":"%s:%s","imageTag":"%s","revision":"manual","variant":"manual"}\n' "$REPO_URI" "$TAG" "$TAG" > "$TMP/imageDetail.json"
(cd "$TMP" && zip -qr promote.zip imageDetail.json deploy)
KEY="manual/$ENV_NAME-$TAG.zip"
aws s3 cp "$TMP/promote.zip" "s3://$BUCKET/$KEY" --only-show-errors
BUILD=$(aws codebuild start-build --project-name "$PROJECT-$ENV_NAME-deploy" \
  --source-type-override S3 --source-location-override "$BUCKET/$KEY" --artifacts-override type=NO_ARTIFACTS \
  --query build.id --output text)
echo "started ${BUILD} (promote $TAG to $ENV_NAME)"

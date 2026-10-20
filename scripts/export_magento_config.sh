#!/usr/bin/env bash
# Regenerate app/magento/config/config.php (Magento key point 2) after adding or removing modules,
# without a local Docker/PHP toolchain: runs the Dockerfile's config-export stage once on the
# arm64 image-build project and downloads the result. Review the diff, then commit it.
#   module list : bin/magento module:enable --all over Magento + app/magento/modules
#   scopes/i18n : merged from app/magento/config/config.scopes.php
# Uses the working tree of app/magento (no commit needed). Needs the cicd stack deployed.
# Usage: [ECOM_CONFIG=config/<profile>.json] scripts/export_magento_config.sh
set -euo pipefail
source "$(dirname "$0")/lib/prefix.sh"
ROOT=$(git rev-parse --show-toplevel)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "${AWS_REGION:-us-east-1}")
BUCKET="$PROJECT-artifacts-$ACCOUNT-$REGION"
STAMP=$(date -u +%Y%m%d%H%M%S)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
(cd "$ROOT" && zip -qr "$TMP/src.zip" app/magento)
aws s3 cp "$TMP/src.zip" "s3://$BUCKET/config-export/$STAMP/src.zip" --only-show-errors
SPEC=$(cat <<'YAML'
version: 0.2
phases:
  build:
    commands:
      - cd app/magento
      - docker build --platform linux/arm64 --build-arg PHP_IMAGE="$PHP_IMAGE" --build-arg MAGENTO_REF="$MAGENTO_REF" --target config-export-run -t config-export .
      - id=$(docker create config-export) && docker cp "$id:/tmp/config.php" /tmp/config.php && docker rm "$id"
      - aws s3 cp /tmp/config.php "s3://$EXPORT_BUCKET/$EXPORT_PREFIX/config.php"
YAML
)
BUILD=$(aws codebuild start-build --project-name "$PROJECT-image-build" \
  --source-type-override S3 --source-location-override "$BUCKET/config-export/$STAMP/src.zip" \
  --artifacts-override type=NO_ARTIFACTS --buildspec-override "$SPEC" \
  --environment-variables-override "name=EXPORT_BUCKET,value=$BUCKET,type=PLAINTEXT" \
    "name=EXPORT_PREFIX,value=config-export/$STAMP,type=PLAINTEXT" \
  --query build.id --output text)
echo "started $BUILD"
while :; do
  STATUS=$(aws codebuild batch-get-builds --ids "$BUILD" --query 'builds[0].buildStatus' --output text)
  [ "$STATUS" = "IN_PROGRESS" ] || break
  sleep 30
done
echo "build $STATUS"
[ "$STATUS" = "SUCCEEDED" ] || exit 1
aws s3 cp "s3://$BUCKET/config-export/$STAMP/config.php" "$ROOT/app/magento/config/config.php" --only-show-errors
aws s3 rm "s3://$BUCKET/config-export/$STAMP/" --recursive --only-show-errors
echo "updated app/magento/config/config.php; review with git diff and commit"

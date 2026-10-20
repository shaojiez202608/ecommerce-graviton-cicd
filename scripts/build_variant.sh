#!/usr/bin/env bash
# Operator tool: one-off image build of a given commit and variant (magento | smoke) on the arm64
# image-build project, outside the pipeline (same buildspec, same scan gate). The source zip goes
# to manual/ in the source bucket, so no pipeline is triggered. Prints the build id.
# Usage: [ECOM_CONFIG=config/<PROFILE>.json] scripts/build_variant.sh <COMMIT> <magento|smoke>
set -euo pipefail
COMMIT="$1"; VARIANT="$2"
source "$(dirname "$0")/lib/prefix.sh"
ROOT=$(git rev-parse --show-toplevel)
SHA=$(git -C "$ROOT" rev-parse "$COMMIT")
BUCKET=$(aws cloudformation describe-stacks --stack-name "$PROJECT-cicd" \
  --query "Stacks[0].Outputs[?OutputKey=='SourceBucketName'].OutputValue" --output text)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git -C "$ROOT" archive --format=zip -o "$TMP/source.zip" "$SHA"
echo "$SHA" > "$TMP/REVISION"
(cd "$TMP" && zip -q source.zip REVISION)
KEY="manual/$VARIANT-$SHA.zip"
aws s3 cp "$TMP/source.zip" "s3://$BUCKET/$KEY" --only-show-errors
aws codebuild start-build --project-name "$PROJECT-image-build" \
  --source-type-override S3 --source-location-override "$BUCKET/$KEY" --artifacts-override type=NO_ARTIFACTS \
  --environment-variables-override "name=IMAGE_VARIANT,value=$VARIANT,type=PLAINTEXT" \
  --query build.id --output text

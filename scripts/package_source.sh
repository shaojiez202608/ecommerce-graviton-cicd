#!/usr/bin/env bash
# Package the committed tree (git archive HEAD) plus a REVISION file and upload it to the
# pipeline source bucket. The upload event starts the matching pipeline.
# Usage: scripts/package_source.sh <pipeline-name> [stack-name]
set -euo pipefail
PIPELINE="${1:-develop}"; STACK="${2:-proto-ecom-cicd}"
ROOT=$(git rev-parse --show-toplevel); cd "$ROOT"
SHA=$(git rev-parse HEAD)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "working tree has uncommitted changes; the image tag is the commit SHA, so commit first" >&2; exit 1
fi
BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='SourceBucketName'].OutputValue" --output text)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git archive --format=zip -o "$TMP/source.zip" HEAD
echo "$SHA" > "$TMP/REVISION"
(cd "$TMP" && zip -q source.zip REVISION)
aws s3 cp "$TMP/source.zip" "s3://$BUCKET/source/$PIPELINE.zip" --only-show-errors
echo "uploaded commit $SHA to pipeline '$PIPELINE'"

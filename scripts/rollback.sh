#!/usr/bin/env bash
# Manual rollback of the web service to a previous task definition revision (no DB change).
# Usage: scripts/rollback.sh <env> [task-definition-arn]   (default: previous revision)
# Database migrations are forward-only: if the bad release changed the schema, restore the
# pre-deploy snapshot instead (uat/prod take one automatically). See docs/handover.md.
set -euo pipefail
ENV_NAME="$1"; TARGET="${2:-}"; PREFIX="${SSM_PREFIX:-/proto-ecom}"
META=$(aws ssm get-parameter --name "$PREFIX/$ENV_NAME/deploy" --query Parameter.Value --output text)
j() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$META" "$1"; }
CLUSTER=$(j cluster); SERVICE=$(j service); FAMILY=$(j webFamily)
CURRENT=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].taskDefinition' --output text)
if [ -z "$TARGET" ]; then
  TARGET=$(aws ecs list-task-definitions --family-prefix "$FAMILY" --sort DESC --query 'taskDefinitionArns' --output text \
    | tr '\t' '\n' | grep -v -x "$CURRENT" | head -1)
fi
echo "rolling back $SERVICE: $CURRENT -> $TARGET"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$TARGET" >/dev/null
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
echo "done"

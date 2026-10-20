#!/usr/bin/env bash
# Promote one already-built image to one environment (runs inside the env's VPC in CodeBuild).
# Order matters (Magento key points 3 and 4):
#   1. register new task definition revisions that point at the promoted image tag
#   2. (optional) RDS snapshot before migration
#   3. run ONE migration task (setup:install / setup:upgrade) and wait; abort on failure
#   4. update the ECS service (rolling with circuit-breaker rollback, or native blue/green)
#   5. run ONE cache:flush task
# Usage: ecs_deploy.sh <env> <imageDetail.json>
set -euo pipefail
ENV_NAME="$1"; DETAIL="$2"
PREFIX="${SSM_PREFIX:?SSM_PREFIX must be set (environment variable of the deploy project)}"
IMAGE_URI=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageUri"])' "$DETAIL")
META=$(aws ssm get-parameter --name "$PREFIX/$ENV_NAME/deploy" --query Parameter.Value --output text)
j() { python3 -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(",".join(v) if isinstance(v,list) else v)' "$META" "$1"; }
CLUSTER=$(j cluster); SERVICE=$(j service); WEB_FAMILY=$(j webFamily); OPS_FAMILY=$(j opsFamily)
SUBNETS=$(j subnets); SG=$(j securityGroup); DB=$(j dbInstance); SNAP=$(j snapshotBeforeMigrate)
DESIRED=$(j desiredCount); NAME_PREFIX=$(j namePrefix); SNAP_PREFIX=$(j snapshotPrefix)
OPS_STARTED_BY="${NAME_PREFIX}-ops"
echo "[deploy] env=$ENV_NAME image=$IMAGE_URI cluster=$CLUSTER service=$SERVICE"

register() {  # family -> new task definition ARN with the container image replaced
  local family="$1"
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json > td.json
  python3 - "$IMAGE_URI" <<'PY'
import json, sys
td = json.load(open("td.json"))
for c in td["containerDefinitions"]:
    if c["name"] == "web":
        c["image"] = sys.argv[1]
keep = ["family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes",
        "requiresCompatibilities", "cpu", "memory", "runtimePlatform"]
json.dump({k: td[k] for k in keep if k in td}, open("td-new.json", "w"))
PY
  aws ecs register-task-definition --cli-input-json file://td-new.json --query taskDefinition.taskDefinitionArn --output text
}

run_once() {  # ops task with a command; waits and fails on non-zero exit
  local td="$1" cmd="$2" label="$3"
  local running
  running=$(aws ecs list-tasks --cluster "$CLUSTER" --started-by "$OPS_STARTED_BY" --desired-status RUNNING --query 'length(taskArns)' --output text)
  if [ "$running" != "0" ]; then echo "[deploy] another ops task is running; refusing to start a concurrent $label" >&2; exit 1; fi
  local overrides
  overrides=$(printf '{"containerOverrides":[{"name":"web","command":["%s"]}]}' "$cmd")
  local arn
  arn=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$td" --launch-type FARGATE --count 1 \
    --started-by "$OPS_STARTED_BY" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text)
  echo "[deploy] $label task: ${arn##*/}"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn" || aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn"
  local code reason
  code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" --query 'tasks[0].containers[0].exitCode' --output text)
  reason=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" --query 'tasks[0].stoppedReason' --output text)
  echo "[deploy] $label exit=$code reason=$reason"
  [ "$code" = "0" ] || { echo "[deploy] $label failed; service left unchanged" >&2; exit 1; }
}

WEB_TD=$(register "$WEB_FAMILY"); OPS_TD=$(register "$OPS_FAMILY")
echo "[deploy] web=$WEB_TD ops=$OPS_TD"

if [ "$SNAP" = "True" ] || [ "$SNAP" = "true" ]; then
  SNAP_ID="${SNAP_PREFIX}-$(date -u +%Y%m%d%H%M%S)"  # matches the IAM grant on <prefix>-<env>-predeploy-*
  aws rds create-db-snapshot --db-instance-identifier "$DB" --db-snapshot-identifier "$SNAP_ID" >/dev/null
  aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP_ID"
  echo "[deploy] snapshot $SNAP_ID ready"
fi

run_once "$OPS_TD" /usr/local/bin/magento-migrate migrate

aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$WEB_TD" \
  --desired-count "$DESIRED" --query 'service.deployments[0].id' --output text
echo "[deploy] waiting for service to become stable"
if ! aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"; then
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" || true
fi
# services-stable can return before rolloutState settles; poll the PRIMARY deployment.
for _ in $(seq 1 40); do
  read -r PRIMARY_TD ROLLOUT < <(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition,rolloutState]' --output text)
  echo "[deploy] primary=${PRIMARY_TD##*/} rollout=$ROLLOUT"
  if [ "$PRIMARY_TD" != "$WEB_TD" ]; then
    echo "[deploy] service rolled back to ${PRIMARY_TD##*/}; failing the stage" >&2; exit 1
  fi
  case "$ROLLOUT" in
    COMPLETED) break ;;
    FAILED) echo "[deploy] deployment FAILED" >&2; exit 1 ;;
  esac
  sleep 15
done
[ "$ROLLOUT" = "COMPLETED" ] || { echo "[deploy] deployment did not complete in time" >&2; exit 1; }

run_once "$OPS_TD" /usr/local/bin/magento-cache-flush cache-flush
echo "[deploy] $ENV_NAME now runs $IMAGE_URI"

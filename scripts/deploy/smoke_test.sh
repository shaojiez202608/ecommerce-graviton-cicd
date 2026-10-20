#!/usr/bin/env bash
# Post-deploy smoke test from inside the VPC against the internal ALB.
set -euo pipefail
ENV_NAME="$1"; PREFIX="${SSM_PREFIX:-/proto-ecom}"
META=$(aws ssm get-parameter --name "$PREFIX/$ENV_NAME/deploy" --query Parameter.Value --output text)
ALB=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["albDns"])' "$META")
for path in /health_check.php /; do
  for i in $(seq 1 10); do
    code=$(curl -s -o /tmp/body -w '%{http_code}' --max-time 20 "http://$ALB$path" || true)
    if [ "$code" = "200" ]; then echo "[smoke] $path -> 200"; head -c 300 /tmp/body; echo; break; fi
    echo "[smoke] $path -> $code (attempt $i)"; sleep 15
    [ "$i" = "10" ] && { echo "[smoke] $path failed" >&2; exit 1; }
  done
done

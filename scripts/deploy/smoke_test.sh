#!/usr/bin/env bash
# Post-deploy smoke test from inside the VPC against the internal ALB.
# HTTP environments: http://<ALB_DNS>/...
# HTTPS environments: https://<SITE_HOST>/... with normal certificate verification; curl is told to
# connect to the internal ALB (--connect-to), so no DNS record is needed for the check itself.
set -euo pipefail
ENV_NAME="$1"; PREFIX="${SSM_PREFIX:?SSM_PREFIX must be set (environment variable of the deploy project)}"
META=$(aws ssm get-parameter --name "$PREFIX/$ENV_NAME/deploy" --query Parameter.Value --output text)
m() { python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], sys.argv[3]))' "$META" "$1" "${2:-}"; }
ALB=$(m albDns); SCHEME=$(m scheme http); HOST=$(m siteHost "$ALB")
if [ "$SCHEME" = "https" ]; then
  BASE="https://$HOST"; CURL_ARGS=(--connect-to "$HOST:443:$ALB:443")
else
  BASE="http://$ALB"; CURL_ARGS=()
fi
echo "[smoke] base=$BASE"
for path in /health_check.php /; do
  for i in $(seq 1 10); do
    code=$(curl -s -o /tmp/body -w '%{http_code}' --max-time 20 ${CURL_ARGS[@]+"${CURL_ARGS[@]}"} "$BASE$path" || true)
    if [ "$code" = "200" ]; then echo "[smoke] $path -> 200"; head -c 300 /tmp/body; echo; break; fi
    echo "[smoke] $path -> $code (attempt $i)"; sleep 15
    [ "$i" = "10" ] && { echo "[smoke] $path failed" >&2; exit 1; }
  done
done

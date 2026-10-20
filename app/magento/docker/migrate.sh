#!/bin/sh
# One-off migration task (Magento key point 3). Started by the deploy script as a single
# ECS task per environment, before web tasks switch to the new image.
#  - a MySQL named lock (GET_LOCK) makes a second concurrent migration fail fast
#  - first run:   setup:install (schema + data), using the injected configuration
#  - later runs:  setup:db:status; when an upgrade is required, enable maintenance mode and run
#                 setup:upgrade --keep-generated (generated code is already in the image).
#                 Maintenance mode then stays ON: it is closed by the cache-flush task once the
#                 new web tasks are serving, so old code never serves the upgraded schema.
#  var/ is on shared EFS, so the flag (and the deploy marker) are seen by every web task.
# Signals: the task runs with an init process; TERM/INT run the cleanup trap. If the task fails
# before setup:upgrade started, maintenance is switched off again here; after that, the deploy
# script applies the environment's maintenance_on_failure policy.
set -eu
cd /var/www/magento
MARKER=var/.deploy-maintenance
UPGRADE_STARTED=0
LOCK_PID=""
CHILD=""

cleanup() {
  code=$?
  trap - EXIT INT TERM
  [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null || true
  if [ "$code" -ne 0 ] && [ -f "$MARKER" ]; then
    if [ "$UPGRADE_STARTED" = "0" ]; then
      php bin/magento maintenance:disable || true
      rm -f "$MARKER"
      echo "[migrate] failed before setup:upgrade started; maintenance mode switched off" >&2
    else
      echo "[migrate] failed during the upgrade; maintenance mode left ON for the deploy policy" >&2
    fi
  fi
  [ -n "$LOCK_PID" ] && kill "$LOCK_PID" 2>/dev/null || true
  exit "$code"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Run long commands in the background and wait, so a TERM is handled immediately.
run() { "$@" & CHILD=$!; wait "$CHILD"; rc=$?; CHILD=""; return "$rc"; }

php /usr/local/lib/magento/env-tool.php render app/etc/env.php

# Serialize migrations per database: hold a MySQL named lock for the lifetime of this script.
rm -f /tmp/migrate.lock
php /usr/local/lib/magento/env-tool.php hold-lock "${MAGENTO_DB_NAME}-migrate" /tmp/migrate.lock $$ &
LOCK_PID=$!
for _ in $(seq 1 30); do
  [ -s /tmp/migrate.lock ] && break
  kill -0 "$LOCK_PID" 2>/dev/null || break
  sleep 1
done
if [ "$(cat /tmp/migrate.lock 2>/dev/null)" != "ok" ]; then
  echo "[migrate] another migration holds the lock (or the lock could not be taken); refusing" >&2
  exit 75
fi
echo "[migrate] migration lock acquired"

# Ensure the least-privilege application user exists (master credentials are only in this task).
php -r '
$o = [PDO::MYSQL_ATTR_SSL_CA => "/etc/ssl/certs/rds-global-bundle.pem", PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION];
$p = new PDO("mysql:host=" . getenv("MAGENTO_DB_HOST"), getenv("MAGENTO_DB_MASTER_USER"), getenv("MAGENTO_DB_MASTER_PASSWORD"), $o);
$db = str_replace("`", "", getenv("MAGENTO_DB_NAME"));
$u = $p->quote(getenv("MAGENTO_DB_USER")); $pw = $p->quote(getenv("MAGENTO_DB_PASSWORD"));
$p->exec("CREATE DATABASE IF NOT EXISTS `$db`");
$q = $p->prepare("SELECT COUNT(*) FROM mysql.user WHERE user = ? AND host = \"%\"");
$q->execute([getenv("MAGENTO_DB_USER")]);
// CREATE USER IF NOT EXISTS is rejected by MySQL 8.4 once the user is a trigger definer,
// so only create it when it is missing.
if ((int) $q->fetchColumn() === 0) { $p->exec("CREATE USER $u@\"%\" IDENTIFIED BY $pw"); }
else { $p->exec("ALTER USER $u@\"%\" IDENTIFIED BY $pw"); }
$p->exec("GRANT ALL PRIVILEGES ON `$db`.* TO $u@\"%\"");
echo "[migrate] application database user ready\n";'

installed=$(php -r '
$e = require "app/etc/env.php"; $c = $e["db"]["connection"]["default"];
$o = [PDO::MYSQL_ATTR_SSL_CA => "/etc/ssl/certs/rds-global-bundle.pem"];
try { $p = new PDO("mysql:host={$c["host"]};dbname={$c["dbname"]}", $c["username"], $c["password"], $o);
  echo $p->query("SHOW TABLES LIKE \"setup_module\"")->rowCount() > 0 ? "yes" : "no"; }
catch (Throwable $t) { fwrite(STDERR, "db check failed: " . $t->getMessage() . "\n"); exit(3); }')

if [ "$installed" = "no" ]; then
  echo "[migrate] database empty -> setup:install"
  # setup:install must start without an env.php: with an install date present Magento treats
  # the deployment as installed and looks for scopes in the (still empty) database.
  rm -f app/etc/env.php
  run php -d memory_limit=-1 bin/magento setup:install \
    --db-host="$MAGENTO_DB_HOST" --db-name="$MAGENTO_DB_NAME" \
    --db-user="$MAGENTO_DB_USER" --db-password="$MAGENTO_DB_PASSWORD" \
    --db-ssl-ca=/etc/ssl/certs/rds-global-bundle.pem --db-ssl-verify \
    --key="$MAGENTO_CRYPT_KEY" \
    --base-url="$MAGENTO_BASE_URL" --backend-frontname="${MAGENTO_ADMIN_FRONTNAME:-admin}" \
    --admin-user="$MAGENTO_ADMIN_USER" --admin-password="$MAGENTO_ADMIN_PASSWORD" \
    --admin-email="${MAGENTO_ADMIN_EMAIL:-admin@example.com}" --admin-firstname=Admin --admin-lastname=User \
    --language=en_US --currency=USD --timezone=UTC --use-rewrites=1 \
    --search-engine=opensearch --opensearch-host="https://$MAGENTO_OPENSEARCH_HOST" --opensearch-port=443 \
    --opensearch-enable-auth=1 --opensearch-username="$MAGENTO_OPENSEARCH_USER" \
    --opensearch-password="$MAGENTO_OPENSEARCH_PASSWORD" --opensearch-index-prefix="${MAGENTO_OPENSEARCH_PREFIX:-magento2}" \
    --no-interaction
  # setup:install rewrote env.php; restore the injected one so runtime and migration agree.
  php /usr/local/lib/magento/env-tool.php render app/etc/env.php
  php bin/magento app:config:import --no-interaction
  echo "[migrate] install complete"
  exit 0
fi

set +e
php bin/magento setup:db:status --no-ansi
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "[migrate] schema and data up to date; importing config only"
  php bin/magento app:config:import --no-interaction
  exit 0
fi
if [ "$status" -ne 2 ]; then
  echo "[migrate] setup:db:status failed with $status" >&2
  exit "$status"
fi

echo "[migrate] upgrade required -> maintenance window"
date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
php bin/magento maintenance:enable
UPGRADE_STARTED=1
run php -d memory_limit=-1 bin/magento setup:upgrade --keep-generated --no-interaction
run php bin/magento app:config:import --no-interaction
echo "[migrate] upgrade complete; maintenance mode stays ON until cache-flush after the new tasks serve"

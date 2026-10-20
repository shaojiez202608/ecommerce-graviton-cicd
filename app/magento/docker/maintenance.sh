#!/bin/sh
# Applies the environment's maintenance_on_failure policy after a failed deployment.
#   magento-maintenance disable  -> switch maintenance off if a deployment left it on
#   magento-maintenance keep     -> leave it on (old code must not serve a partially upgraded
#                                   schema); report so an operator restores the pre-deploy snapshot
# Only acts when the deploy marker exists, i.e. maintenance was switched on by a deployment.
set -eu
cd /var/www/magento
policy="${1:-keep}"
php /usr/local/lib/magento/env-tool.php render app/etc/env.php
if [ ! -f var/.deploy-maintenance ]; then
  echo "[maintenance] no maintenance window opened by a deployment; nothing to do"
  exit 0
fi
if [ "$policy" = "disable" ]; then
  php bin/magento maintenance:disable
  rm -f var/.deploy-maintenance
  echo "[maintenance] policy=disable: maintenance mode switched off after the failed deployment"
else
  echo "[maintenance] policy=keep: maintenance mode stays ON (opened $(cat var/.deploy-maintenance));" \
       "restore the pre-deploy snapshot or finish the release, then run: bin/magento maintenance:disable"
fi

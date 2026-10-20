#!/bin/sh
# Post-deploy cache flush (Magento key point 4). Runs as a one-off task after the new web tasks
# are healthy; cache and full-page cache live in the shared Valkey cluster. If this deployment
# opened a maintenance window (schema upgrade), it is closed here, after the flush.
set -eu
cd /var/www/magento
php /usr/local/lib/magento/env-tool.php render app/etc/env.php
php bin/magento cache:flush
if [ -f var/.deploy-maintenance ]; then
  php bin/magento maintenance:disable
  rm -f var/.deploy-maintenance
  echo "[cache-flush] maintenance window opened by this deployment is closed"
fi

#!/bin/sh
# Post-deploy cache flush (Magento key point 4). Runs as a one-off task after the new
# web tasks are healthy; cache and full-page cache live in the shared Valkey cluster.
set -eu
cd /var/www/magento
php /usr/local/lib/magento/env-tool.php render app/etc/env.php
php bin/magento cache:flush

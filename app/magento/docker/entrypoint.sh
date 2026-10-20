#!/bin/sh
# Web container entrypoint: render env.php from injected configuration, then run php-fpm + nginx.
# Never runs setup:upgrade (Magento key point 3: migrations run once per environment in a
# separate one-off task).
set -eu
cd /var/www/magento
php /usr/local/lib/magento/env-tool.php render app/etc/env.php
mkdir -p /tmp/nginx
php-fpm
exec nginx -g 'daemon off;'

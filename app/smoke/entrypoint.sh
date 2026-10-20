#!/bin/sh
set -eu
mkdir -p /tmp/nginx
php-fpm -D
exec nginx -g 'daemon off;'

# Lightweight stand-in image used to exercise the pipeline, deploy script, migration-task
# ordering and rollback without a full Magento build. It is NOT Magento: the migrate and
# cache-flush entrypoints only print what the Magento image would do.
ARG PHP_IMAGE=public.ecr.aws/docker/library/php:8.3-fpm-bookworm
FROM ${PHP_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends nginx-light && rm -rf /var/lib/apt/lists/*
COPY nginx.conf /etc/nginx/nginx.conf
COPY pub/ /var/www/magento/pub/
COPY entrypoint.sh /usr/local/bin/magento-entrypoint
COPY noop.sh /usr/local/bin/magento-migrate
COPY noop.sh /usr/local/bin/magento-cache-flush
ARG IMAGE_REVISION=unknown
RUN chmod 0755 /usr/local/bin/magento-* && echo "${IMAGE_REVISION}" > /var/www/magento/pub/revision.txt \
    && sed -i 's/^listen = .*/listen = 127.0.0.1:9000/' /usr/local/etc/php-fpm.d/www.conf \
    && mkdir -p /tmp/nginx && chown -R www-data:www-data /tmp/nginx /var/www/magento
USER www-data
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/magento-entrypoint"]

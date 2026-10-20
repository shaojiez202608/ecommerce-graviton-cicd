# syntax=docker/dockerfile:1
# Immutable Magento Open Source image for linux/arm64 (AWS Graviton).
# composer install, setup:di:compile and setup:static-content:deploy run at BUILD time
# (Magento key point 1). Nothing here talks to a database.
# Base images come from the ECR Public mirror of Docker Official Images.
ARG PHP_IMAGE=public.ecr.aws/docker/library/php:8.3-fpm-bookworm
ARG COMPOSER_IMAGE=public.ecr.aws/docker/library/composer:2

# checkov:skip=CKV_DOCKER_7: the base image comes from a build argument whose default and profile value are pinned tags
FROM ${COMPOSER_IMAGE} AS composer

FROM ${PHP_IMAGE} AS base
# Apply Debian security updates on top of the base image (the scan gate blocks images whose
# OS packages have fixable critical findings).
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends; \
    apt-get install -y --no-install-recommends \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev libwebp-dev libicu-dev libxslt1-dev \
      libzip-dev libsodium-dev libonig-dev libxml2-dev nginx-light ca-certificates default-mysql-client; \
    docker-php-ext-configure gd --with-freetype --with-jpeg --with-webp; \
    docker-php-ext-install -j"$(nproc)" bcmath ftp gd intl pdo_mysql soap sockets sodium xsl zip opcache; \
    apt-get purge -y --auto-remove libfreetype6-dev libjpeg62-turbo-dev libpng-dev libwebp-dev libicu-dev \
      libxslt1-dev libzip-dev libonig-dev libxml2-dev; \
    apt-get install -y --no-install-recommends libfreetype6 libjpeg62-turbo libpng16-16 libwebp7 libicu72 \
      libxslt1.1 libzip4; \
    rm -rf /var/lib/apt/lists/*
# CA bundle for TLS connections to Amazon RDS.
RUN curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && chmod 0644 /etc/ssl/certs/rds-global-bundle.pem
COPY docker/php.ini /usr/local/etc/php/conf.d/zz-magento.ini
COPY docker/php-fpm-pool.conf /usr/local/etc/php-fpm.d/zz-magento.conf

FROM base AS source
ARG MAGENTO_REPO=https://github.com/magento/magento2.git
ARG MAGENTO_REF=2.4.8-p5
COPY --from=composer /usr/bin/composer /usr/bin/composer
RUN apt-get update && apt-get install -y --no-install-recommends git unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /var/www/magento
RUN git clone --depth 1 --branch "${MAGENTO_REF}" "${MAGENTO_REPO}" . && rm -rf .git
ENV COMPOSER_ALLOW_SUPERUSER=1 COMPOSER_NO_INTERACTION=1
RUN composer install --no-dev --prefer-dist --optimize-autoloader --no-progress
# Project modules (app/code). Proto_DeployDemo only adds one table so that a release can exercise
# the setup:upgrade + maintenance-mode path; remove it (and its config.php line) in real projects.
COPY modules/ app/code/
COPY docker/build-env.php /tmp/build-env.php

# Developer tool, not part of the image: regenerate app/etc/config.php after adding/removing modules
#   docker build --target config-export -o type=local,dest=out app/magento   (or scripts/export_magento_config.sh)
# then copy out/config.php to app/magento/config/config.php and commit it.
FROM source AS config-export-run
COPY config/config.scopes.php /tmp/config.scopes.php
RUN php bin/magento module:enable --all \
    && php /tmp/build-env.php merge-config /tmp/config.scopes.php app/etc/config.php \
    && cp app/etc/config.php /tmp/config.php
FROM scratch AS config-export
COPY --from=config-export-run /tmp/config.php /config.php

FROM source AS builder
ARG STATIC_LOCALES="en_US"
ARG SCD_JOBS=4
# Magento key point 2: app/etc/config.php (modules, scopes, themes, i18n) is committed to Git and
# used as-is; the build fails if it does not list exactly the modules present in the code base.
COPY config/config.php app/etc/config.php
RUN set -eux; \
    php /tmp/build-env.php check-modules app/etc/config.php; \
    php /tmp/build-env.php write-stub app/etc/env.php; \
    php bin/magento deploy:mode:set production --skip-compilation; \
    php -d memory_limit=-1 bin/magento setup:di:compile; \
    php /tmp/build-env.php write-stub app/etc/env.php nodb; \
    rm -rf var/cache/* var/page_cache/*; \
    php -d memory_limit=-1 bin/magento setup:static-content:deploy -f --jobs "${SCD_JOBS}" \
      --area frontend --theme Magento/luma ${STATIC_LOCALES}; \
    php -d memory_limit=-1 bin/magento setup:static-content:deploy -f --jobs "${SCD_JOBS}" \
      --area adminhtml --theme Magento/backend ${STATIC_LOCALES}; \
    composer dump-autoload --no-dev --optimize --apcu || composer dump-autoload --no-dev --optimize; \
    rm -f app/etc/env.php; \
    rm -rf var/cache/* var/page_cache/* var/view_preprocessed/* dev/tests

FROM base AS runtime
LABEL org.opencontainers.image.description="Magento Open Source (prototype, linux/arm64)"
WORKDIR /var/www/magento
COPY --from=builder --chown=www-data:www-data /var/www/magento /var/www/magento
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/build-env.php /usr/local/lib/magento/env-tool.php
COPY docker/entrypoint.sh /usr/local/bin/magento-entrypoint
COPY docker/migrate.sh /usr/local/bin/magento-migrate
COPY docker/cache-flush.sh /usr/local/bin/magento-cache-flush
COPY docker/maintenance.sh /usr/local/bin/magento-maintenance
RUN chmod 0755 /usr/local/bin/magento-* && mkdir -p /tmp/nginx && chown -R www-data:www-data /tmp/nginx \
    && echo "${MAGENTO_REF:-unknown}" > /var/www/magento/.image-magento-ref
USER www-data
EXPOSE 8080
HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/magento-entrypoint"]

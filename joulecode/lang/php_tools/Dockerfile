# joule:code PHP tools – built by joule:code on first use
# (tag joulecode/php-tools:<hash of this directory>). Runs without network.
FROM php:8.3-cli

RUN apt-get update \
    && apt-get install -y --no-install-recommends git unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

WORKDIR /opt/joulecode
COPY composer.json ./
RUN composer install --no-dev --no-interaction --no-progress --prefer-dist \
    && composer clear-cache
COPY tool.php rector.php cachegrind.php ./

ENV PATH="/opt/joulecode/vendor/bin:${PATH}"

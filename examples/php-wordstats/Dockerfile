FROM php:8.3-cli
# Install dependencies at build time – containers have no network at run time.
RUN apt-get update && apt-get install -y --no-install-recommends git unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /app
COPY composer.json ./
RUN composer install --no-interaction --no-progress --no-autoloader
COPY . /app
RUN composer dump-autoload --optimize

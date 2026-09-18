#!/usr/bin/env bash
# Renew the certificate for rfp.malambomutila.com only, then reload nginx.
#
# Why this exists separately from /home/mm/ssl-renew.sh:
#   That script runs "certbot renew" across every certificate and uses
#   "set -euo pipefail". Certbot exits non-zero if any certificate fails, so a
#   failure on the shared ohasp certificate aborts the script before nginx is
#   reloaded, and a freshly renewed certificate would never be picked up.
#   Scoping this to one certificate with --cert-name keeps the two independent.
#
# It reloads nginx rather than restarting it, so live traffic to the other
# sites on this host is never dropped.
set -euo pipefail

SSL_DIR=/home/mm/srv/ohasp/nginx/ssl
WEBROOT=/home/mm/srv/ohasp/nginx/certbot/www

echo "$(date -u +%FT%TZ): starting renewal check for rfp.malambomutila.com"

# Certbot only acts if the certificate is within 30 days of expiry, so this is
# safe to run often. It runs as root inside the container, which is how it can
# write to the root-owned letsencrypt directories without sudo on the host.
docker run --rm \
  -v "${WEBROOT}:/var/www/certbot" \
  -v "${SSL_DIR}:/etc/letsencrypt" \
  certbot/certbot renew --cert-name rfp.malambomutila.com --quiet

# Validate before reloading. If the config is bad, stop and leave the running
# nginx untouched rather than reloading a broken configuration.
docker exec nginx nginx -t
docker exec nginx nginx -s reload

echo "$(date -u +%FT%TZ): renewal check complete, nginx reloaded"

#!/bin/bash
# Pull the Jolly image from GHCR and (re)start it behind Caddy.
# Usage: sudo /opt/jolly/deploy.sh [image-tag]
#   tag defaults to SSM <prefix>/image-tag, then "latest".
# Rolls back to the previous image if the new one is not healthy in time.
set -euo pipefail
source /opt/jolly/infra.env
umask 077

TAG="${1:-}"
if [ -z "$TAG" ]; then
  TAG=$(aws ssm get-parameter --region "$REGION" --name "$PARAM_PREFIX/image-tag" \
        --query Parameter.Value --output text 2>/dev/null || echo latest)
fi
NEW="$IMAGE:$TAG"
echo "[deploy] target image $NEW"

# 1. Runtime config: plain values from SSM <prefix>/env/*, secrets from Secrets Manager.
TMP=$(mktemp)
aws ssm get-parameters-by-path --region "$REGION" --path "$PARAM_PREFIX/env/" --recursive \
  --with-decryption --query 'Parameters[].[Name,Value]' --output text |
  while IFS=$'\t' read -r name value; do [ -n "$name" ] && echo "${name##*/}=$value"; done >> "$TMP"
SECRET_JSON=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
  --query SecretString --output text)
echo "$SECRET_JSON" | jq -r 'to_entries[]
  | select((.key | startswith("GHCR_")) | not)
  | select(.value != "" and .value != "CHANGE_ME")
  | "\(.key)=\(.value)"' >> "$TMP"
echo "DATABASE_URL=sqlite:////data/jolly.db" >> "$TMP"
mv "$TMP" /opt/jolly/.env && chmod 600 /opt/jolly/.env

# 2. Optional GHCR login (only needed while the package is private).
GHCR_USER=$(echo "$SECRET_JSON" | jq -r '.GHCR_USER // empty')
GHCR_TOKEN=$(echo "$SECRET_JSON" | jq -r '.GHCR_TOKEN // empty')
if [ -n "$GHCR_TOKEN" ] && [ "$GHCR_TOKEN" != "CHANGE_ME" ]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-token}" --password-stdin >/dev/null
fi

docker pull "$NEW"
docker network inspect jolly >/dev/null 2>&1 || docker network create jolly >/dev/null
PREV=$(docker inspect -f '{{.Config.Image}}' api 2>/dev/null || true)

run_api() {
  docker rm -f api >/dev/null 2>&1 || true
  docker run -d --name api --restart unless-stopped --network jolly \
    -p 127.0.0.1:8000:8000 --env-file /opt/jolly/.env -v /opt/jolly/data:/data \
    --log-driver awslogs --log-opt awslogs-region="$REGION" \
    --log-opt awslogs-group="$LOG_GROUP" --log-opt awslogs-stream=api \
    "$1" >/dev/null
}

wait_healthy() {
  for _ in $(seq 1 60); do   # up to ~5 min: startup seeds data and builds the embedding index
    curl -fsS -m 4 http://127.0.0.1:8000/health >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

run_api "$NEW"
if wait_healthy; then
  echo "[deploy] $NEW is healthy"
  aws ssm put-parameter --region "$REGION" --name "$PARAM_PREFIX/last-good-image" \
    --type String --overwrite --value "$NEW" >/dev/null 2>&1 || true
else
  echo "[deploy] $NEW failed its health check" >&2
  if [ -n "$PREV" ] && [ "$PREV" != "$NEW" ]; then
    echo "[deploy] rolling back to $PREV" >&2
    run_api "$PREV"; wait_healthy || true
  fi
  exit 1
fi

# 3. Caddy terminates TLS for $DOMAIN and proxies to api:8000.
if ! docker ps --format '{{.Names}}' | grep -qx caddy; then
  docker rm -f caddy >/dev/null 2>&1 || true
  docker run -d --name caddy --restart unless-stopped --network jolly \
    -p 80:80 -p 443:443 -e DOMAIN="$DOMAIN" \
    -v /opt/jolly/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v caddy_data:/data -v caddy_config:/config caddy:2 >/dev/null
fi
docker image prune -f >/dev/null
echo "[deploy] done"

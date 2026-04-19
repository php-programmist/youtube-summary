#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "❌ .env not found. Copy .env.example → .env and fill the secrets." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${OLLAMA_MODEL:?OLLAMA_MODEL must be set in .env}"

echo "▸ Starting ollama..."
docker compose up -d ollama

echo "▸ Waiting for ollama healthcheck..."
cid=$(docker compose ps -q ollama)
for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "starting")
  [ "$status" = "healthy" ] && break
  sleep 2
done
if [ "$status" != "healthy" ]; then
  echo "❌ ollama did not become healthy in time" >&2
  exit 1
fi

echo "▸ Checking model: $OLLAMA_MODEL"
if docker compose exec -T ollama ollama show "$OLLAMA_MODEL" >/dev/null 2>&1; then
  echo "  ✓ already present, skipping pull"
else
  echo "  ↓ pulling (may take several minutes)..."
  docker compose exec -T ollama ollama pull "$OLLAMA_MODEL"
fi

echo "▸ Starting n8n..."
docker compose up -d n8n

echo "✓ Done. n8n UI: http://localhost:5678"

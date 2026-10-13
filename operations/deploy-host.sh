#!/bin/sh
# Run from an immutable /opt/flowops/releases/<revision> checkout on the VPS.
set -eu
umask 077
: "${FLOWOPS_IMAGE:?Verified image tag required}"
: "${FLOWOPS_IMAGE_ID:?Verified image ID required}"
: "${FLOWOPS_ARCHIVE_SHA256:?Verified archive SHA-256 required}"
case "$PWD" in /opt/flowops/releases/*) ;; *) echo 'Invalid release directory' >&2; exit 1;; esac
test -f /opt/flowops/incoming/image.tar.gz
printf '%s  %s\n' "$FLOWOPS_ARCHIVE_SHA256" /opt/flowops/incoming/image.tar.gz | sha256sum -c -
docker network inspect agentops_edge >/dev/null
docker inspect gappazappa-nginx-1 --format '{{json .NetworkSettings.Networks}}' | python3 -c 'import json,sys; assert "agentops_edge" in json.load(sys.stdin)'
docker load -i /opt/flowops/incoming/image.tar.gz
test "$(docker image inspect "$FLOWOPS_IMAGE" --format '{{.Id}}')" = "$FLOWOPS_IMAGE_ID"
test "$(docker image inspect "$FLOWOPS_IMAGE" --format '{{.Architecture}}')" = amd64
mkdir -p /opt/flowops/shared/.local
if test ! -e .local; then ln -s /opt/flowops/shared/.local .local; fi
test "$(readlink -f .local)" = /opt/flowops/shared/.local
python3 -m scripts.init_local
printf 'FLOWOPS_IMAGE=%s\n' "$FLOWOPS_IMAGE" > .env
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml up -d
attempt=0
until docker compose -f compose.production.yaml exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/projects/flowops/health',timeout=2)"; do
  attempt=$((attempt+1))
  test "$attempt" -lt 30
  sleep 2
done
docker compose -f compose.production.yaml ps
echo 'Backend ready; public routes have not been changed by this script.'

#!/usr/bin/env bash
# Boot one built image with disposable credentials and validate its runtime.
# Usage: bash scripts/smoke-docker-image.sh IMAGE coordinator|doc-extract
set -euo pipefail

image="${1:?image reference is required}"
component="${2:?component is required}"
case "$component" in
  coordinator) port=8000 ;;
  doc-extract) port=8092 ;;
  *) echo "Unknown component: $component" >&2; exit 2 ;;
esac

container=""
cleanup() {
  rc=$?
  if [[ -n "$container" ]]; then
    if [[ "$rc" -ne 0 ]]; then
      docker logs --tail 200 "$container" >&2 || true
    fi
    docker rm --force --volumes "$container" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

secret=$(openssl rand -hex 32)
fernet=$(openssl rand -base64 32 | tr '/+' '_-')
container=$(docker run --detach \
  --env ENVIRONMENT=development \
  --env API_SECRET_KEY="$secret" \
  --env JWT_SECRET_KEY="$secret" \
  --env HMAC_SECRET_KEY="$secret" \
  --env RECORDER_AUTH_SECRET="$secret" \
  --env INTERNAL_API_SECRET="$secret" \
  --env GATEWAY_SECRET="$secret" \
  --env DOC_EXTRACT_SECRET="$secret" \
  --env SECRET_ENCRYPTION_KEY="$fernet" \
  "$image")

# No host ports: concurrent local checks cannot collide with a running stack.
ready=false
for ((attempt = 0; attempt < 60; attempt++)); do
  if health=$(docker exec "$container" curl -fsS --max-time 5 "http://127.0.0.1:$port/health" 2>/dev/null); then
    ready=true
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]]; then
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo "$component did not become healthy" >&2
  exit 1
fi

printf '%s' "$health" | python3 -c '
import json, sys
component = sys.argv[1]
health = json.load(sys.stdin)
expected = "healthy" if component == "coordinator" else "ok"
assert health.get("status") == expected, health
if component == "doc-extract":
    assert health.get("ocr") == "ready", health
print(component, "health:", health)
' "$component"

if [[ "$component" == coordinator ]]; then
  # Starting Python alone would miss an empty/missing SPA in the final stage.
  docker exec "$container" python -c '
import urllib.request
from pathlib import Path
with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=10) as response:
    assert response.status == 200
    assert b"<html" in response.read().lower(), "SPA HTML missing"
assert any(Path("/app/ui/assets").glob("*.js")), "SPA JavaScript assets missing"
'
fi
echo "$component image passed its smoke test"

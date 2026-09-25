#!/bin/sh
# Boot check and memory measurement for a hass-remote-integration container.
#   verify.sh start     docker run the image, wait for the API (detects restart loops)
#   verify.sh recreate  remove the container and start it again on the current image
#   verify.sh status    status API + memory (container must be running)
#   verify.sh test      discovery components against HA's MQTT schemas (after any discovery change)
#   verify.sh unit      unit tests (tests/) in the container's HA venv, against the repo's copy of the code
# Reads HRI_NAME, HRI_PORT, HRI_IMAGE, HRI_NETWORK, HRI_PASSWORD (or HRI_PASSWORD_FILE, which wins) and TZ
# from the environment or a .env file.  HRI_ENV="K=V K2=V2" passes more to the container, which is how CI
# boots a version other than the newest (HA_VERSION_LATEST=0, HA_VERSION_DEFAULT=<version>).  start exits non-zero when the API does not come up (timeout, restart loop).
set -u
cd "$(dirname "$0")"
[ -f .env ] && . ./.env
NAME=${HRI_NAME:-hass-remote-integration}
PORT=${HRI_PORT:-8087}
IMAGE=${HRI_IMAGE:-hass-remote-integration:local}
URL=http://127.0.0.1:$PORT/api/status
password() {  # what the container uses: the file's content (stripped), else HRI_PASSWORD
  if [ -n "${HRI_PASSWORD_FILE:-}" ]; then tr -d '\r\n' < "$HRI_PASSWORD_FILE"; else printf '%s' "${HRI_PASSWORD:-}"; fi
}
api_get() {  # $1 timeout, $2 url; with a password set, the API wants it as a bearer token (passed on stdin, not in argv)
  pw=$(password)
  if [ -n "$pw" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$(printf '%s' "$pw" | sed 's/\\/\\\\/g; s/"/\\"/g')" | curl -s --max-time "$1" -K - "$2"
  else
    curl -s --max-time "$1" "$2"
  fi
}

start() {
  rc=0
  if [ -n "${HRI_PASSWORD_FILE:-}" ]; then
    set -- -v "$(cd "$(dirname "$HRI_PASSWORD_FILE")" && pwd)/$(basename "$HRI_PASSWORD_FILE"):/run/secrets/hri_password:ro" -e HRI_PASSWORD_FILE=/run/secrets/hri_password
  elif [ -n "${HRI_PASSWORD:-}" ]; then
    export HRI_PASSWORD
    set -- -e HRI_PASSWORD  # the value comes from this environment, not from docker's command line
  else
    set --
  fi
  for kv in ${HRI_ENV:-}; do set -- "$@" -e "$kv"; done
  docker run -d --name "$NAME" --restart unless-stopped --init --stop-timeout 240 \
    ${HRI_NETWORK:+--network "$HRI_NETWORK"} -p "$PORT:$PORT" \
    -v "$NAME:/config" \
    -e TZ="${TZ:-UTC}" -e HRI_PORT="$PORT" "$@" \
    --add-host host.docker.internal:host-gateway \
    "$IMAGE" | cut -c1-12 | sed 's/^/  id: /'
  echo "=== waiting for the manager API on $URL (a fresh volume installs Home Assistant first) ==="
  t0=$(date +%s)
  until api_get 2 "$URL" | python3 -c "import json,sys; sys.exit(0 if 'ha_version' in json.load(sys.stdin) else 1)" 2>/dev/null; do
    if [ $(( $(date +%s)-t0 )) -ge 900 ]; then echo "  TIMEOUT"; rc=1; break; fi
    if [ "$(docker inspect -f '{{.State.Restarting}}' "$NAME" 2>/dev/null)" = "true" ]; then
      echo "  RESTART LOOP detected, stopping"; docker stop "$NAME" >/dev/null; rc=1; break
    fi
    sleep 3
  done
  echo "  after $(( $(date +%s)-t0 ))s: $(docker inspect -f '{{.State.Status}} (restarts={{.RestartCount}})' "$NAME")"
  echo "=== log (errors / ready) ==="
  docker logs "$NAME" 2>&1 | grep -E "ERROR|Traceback|ready" | tail -12 | sed 's/^/  /'
  return $rc
}

status() {
  echo "=== status API ==="
  api_get 5 "$URL" | python3 -c "
import json,sys
try: s=json.load(sys.stdin)
except Exception: print('  (no JSON answer: still installing Home Assistant, or not running)'); sys.exit(0)
print('  HA:', s['ha_version'], '| components:', ', '.join(s['components']))
r=s.get('running') or {}
print('  running:', r.get('domain'), r.get('running_tag'), '| loaded:', r.get('loaded_as_integration'), '| entries:', [(e['title'], e['state']) for e in r.get('entries', [])])
print('  installed:', {d: sorted(x['versions']) for d, x in s['installed'].items()}, '| restart required:', s['state']['restart_required'])
print('  last action:', s['state']['last_action'] or '-', '| last error:', s['state']['last_error'] or '-')"
  echo "=== memory ==="
  docker stats --no-stream --format '  {{.Name}}: RAM={{.MemUsage}} CPU={{.CPUPerc}}' "$NAME"
}

recreate() {
  echo "=== recreating the container on the current image ==="
  docker stop "$NAME" >/dev/null 2>&1 && echo "  stopped"
  docker rm "$NAME" >/dev/null 2>&1 && echo "  removed"
  start
}


test() {
  echo "=== discovery schemas (test_components.py in the container's HA venv) ==="
  docker exec -i "$NAME" /config/venv-current/bin/python - < test_components.py
}

unit() {
  echo "=== unit tests (tests/ in the container's HA venv) ==="
  dir=/tmp/hri-tests
  # only the code under test: registry.json and patches/ are not copied (the tests do not read them; code that
  # does falls back to the image's /app copies)
  docker exec "$NAME" sh -c "rm -rf $dir && mkdir -p $dir/custom_components" || return 1
  for f in tests jsonio.py backupkit.py logbuffer.py entrypoint.py run.py docker-compose.yml Dockerfile README.md docs app repository.yaml; do docker cp -q "$f" "$NAME:$dir/" || return 1; done
  docker cp -q custom_components/integration_manager "$NAME:$dir/custom_components/" || return 1
  docker cp -q .github "$NAME:$dir/" || return 1  # the workflow guard tests (tests/test_r4_web.py) read it
  # HRI_TEST_PYPI=1 adds the two tests that run a real pip against PyPI (off by default: the suite stays offline)
  docker exec -w "$dir" -e PYTHONPATH="$dir" -e PYTHONDONTWRITEBYTECODE=1 -e HRI_TEST_PYPI="${HRI_TEST_PYPI:-0}" "$NAME" /config/venv-current/bin/python -m unittest discover -s tests -t .
}

case "${1:-}" in
  start) start; rc=$?; status; exit $rc ;;
  recreate) recreate; rc=$?; status; exit $rc ;;
  status) status ;;
  test) test ;;
  unit) unit ;;
  *) echo "usage: $0 start|recreate|status|test|unit"; exit 2 ;;
esac

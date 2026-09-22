#!/usr/bin/env bash
# End-to-end test stack for hass-remote-integration (HRI): one mosquitto broker, one throwaway "parent" Home
# Assistant consuming MQTT discovery, and one HRI container running hri_probe.  Everything is named hri-<name>-*,
# listens on 127.0.0.1 only, and never uses port 8123 on the host.  No accounts, tokens or passwords anywhere:
# the parent HA is driven through MQTT (topics e2e/call and e2e/states handled by its own automations).
#
#   stack.sh up <name> <baseport> [hri_image]   create broker + parent HA + HRI (HRI UI on 127.0.0.1:<baseport>)
#                                               HRI_ENV="K=V K2=V2" adds -e to the HRI container (e.g. HA_VERSION_LATEST=0)
#   stack.sh bootstrap <name> [tag]             wait for HRI, install+start hri_probe <tag> (v7.4.1), entry "demo", MQTT on
#   stack.sh down <name>                        remove the stack's containers, volumes and network
#   stack.sh api <name> <METHOD> <path> [json]  HRI API call (X-Requested-With: fetch), prints the JSON answer
#   stack.sh pub <name> <topic> <payload> [-r]  publish on the stack's broker
#   stack.sh sub <name> <topic> [secs] [count]  print "topic payload" lines (retained included)
#   stack.sh call <name> <action> [target_json] [data_json] [response:0|1]
#                                               call a service IN THE PARENT HA (through its e2e/call automation)
#   stack.sh states <name> [substring]          parent HA states as JSON (entity, state, attributes, device, area)
#   stack.sh registry <name> [substring]        parent HA MQTT entity registry view (incl. disabled) as JSON
#   stack.sh hri <name> <sh command>            run a shell command in the HRI container
#   stack.sh parent <name> <sh command>         run a shell command in the parent HA container
#   stack.sh logs <name> hri|parent|mqtt [n]    container log tail
set -u
PARENT_IMAGE=${PARENT_IMAGE:-ghcr.io/home-assistant/home-assistant:2026.9.2}
MQTT_IMAGE=${MQTT_IMAGE:-eclipse-mosquitto:2}
DEFAULT_HRI_IMAGE=${HRI_IMAGE:-trailro26/hass-remote-integration:0.17.0}

die() { echo "ERROR: $*" >&2; exit 1; }
n() { echo "hri-$1-$2"; }
port_free() { ! (netstat -ltn 2>/dev/null || ss -ltn 2>/dev/null || lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null) | grep -qE "[:.]$1[[:space:]]"; }
hriport() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$(n "$1" hri)" | sed -n 's/^HRI_PORT=//p'; }

up() {
  local name=$1 base=$2 image=${3:-$DEFAULT_HRI_IMAGE}
  [ "$base" = 8123 ] && die "port 8123 is not allowed"
  for p in "$base" $((base + 1)); do port_free "$p" || die "port $p is in use"; done
  docker ps -a --format '{{.Names}}' | grep -q "^hri-$name-" && die "stack $name already exists"
  docker network create "hri-$name-net" >/dev/null || die "network"
  docker run -d --name "$(n "$name" mqtt)" --network "hri-$name-net" --network-alias mqtt --memory 128m \
    -p "127.0.0.1:$((base + 1)):1883" "$MQTT_IMAGE" sh -c \
    'printf "listener 1883\nallow_anonymous true\npersistence true\npersistence_location /mosquitto/data/\n" > /mosquitto/config/e2e.conf && exec mosquitto -c /mosquitto/config/e2e.conf' \
    >/dev/null || die "docker run mqtt failed"
  # parent HA: config written into its volume before it starts
  docker volume create "hri-$name-parent" >/dev/null
  docker create --name "$(n "$name" parent)" --network "hri-$name-net" --memory 1500m --cpus 2 \
    -e TZ=UTC -v "hri-$name-parent:/config" "$PARENT_IMAGE" >/dev/null || die "docker create parent failed"
  local tmp; tmp=$(mktemp -d)
  mkdir -p "$tmp/.storage"
  cat > "$tmp/configuration.yaml" <<'YAML'
homeassistant:
  name: e2e-parent
  time_zone: UTC
  unit_system: metric
logger:
  default: warning
automation: !include automations.yaml
YAML
  cat > "$tmp/automations.yaml" <<'YAML'
- id: e2e_call
  alias: e2e call
  mode: parallel
  max: 100
  triggers:
    - trigger: mqtt
      topic: e2e/call
  actions:
    - variables:
        req: "{{ trigger.payload_json }}"
    - if: "{{ req.response | default(false) }}"
      then:
        - action: "{{ req.action }}"
          target: "{{ req.target | default({}) }}"
          data: "{{ req.data | default({}) }}"
          response_variable: resp
          continue_on_error: true
      else:
        - action: "{{ req.action }}"
          target: "{{ req.target | default({}) }}"
          data: "{{ req.data | default({}) }}"
          continue_on_error: true
    - action: mqtt.publish
      data:
        topic: "e2e/result/{{ req.id }}"
        payload: "{{ {'id': req.id, 'done': true, 'response': resp | default(none)} | to_json }}"
- id: e2e_states
  alias: e2e states
  mode: queued
  triggers:
    - trigger: mqtt
      topic: e2e/states
  actions:
    - action: mqtt.publish
      data:
        topic: e2e/states/reply
        payload: >-
          {% set f = trigger.payload %}{% set ns = namespace(items=[]) %}
          {% for s in states if (f == '' or f in s.entity_id) %}
          {% set at = namespace(d={}) %}{% for k, v in s.attributes.items() %}
          {% set at.d = dict(at.d, **{k: (v.isoformat() if v is datetime else v)}) %}{% endfor %}
          {% set ns.items = ns.items + [{'entity_id': s.entity_id, 'state': s.state, 'attributes': at.d,
             'device': device_attr(s.entity_id, 'name'), 'device_by_user': device_attr(s.entity_id, 'name_by_user'),
             'area': area_name(s.entity_id), 'last_changed': s.last_changed.isoformat()}] %}
          {% endfor %}{{ ns.items | to_json }}
- id: e2e_registry
  alias: e2e registry
  mode: queued
  triggers:
    - trigger: mqtt
      topic: e2e/registry
  actions:
    - action: mqtt.publish
      data:
        topic: e2e/registry/reply
        payload: >-
          {% set f = trigger.payload %}{% set ns = namespace(items=[]) %}
          {% for e in integration_entities('mqtt') if (f == '' or f in e) %}
          {% set ns.items = ns.items + [{'entity_id': e, 'state': states(e), 'device': device_attr(e, 'name'),
             'hidden': is_hidden_entity(e), 'labels': labels(e)}] %}
          {% endfor %}{{ ns.items | to_json }}
YAML
  cat > "$tmp/.storage/core.config_entries" <<JSON
{"version": 1, "minor_version": 1, "key": "core.config_entries", "data": {"entries": [
 {"entry_id": "e2emqttparent0000000000000000001", "version": 1, "minor_version": 1, "domain": "mqtt", "title": "e2e broker",
  "data": {"broker": "mqtt", "port": 1883, "discovery": true, "discovery_prefix": "homeassistant", "birth_message": {"topic": "homeassistant/status", "payload": "online", "qos": 0, "retain": false}, "will_message": {"topic": "homeassistant/status", "payload": "offline", "qos": 0, "retain": false}},
  "options": {}, "pref_disable_new_entities": false, "pref_disable_polling": false, "source": "user", "unique_id": null, "disabled_by": null}
]}}
JSON
  docker cp "$tmp/." "$(n "$name" parent):/config/" >/dev/null || die "docker cp parent config"
  rm -rf "$tmp"
  docker start "$(n "$name" parent)" >/dev/null || die "docker start parent failed"
  docker volume create "hri-$name-hri" >/dev/null
  local extra=""  # HRI_ENV="A=1 B=2" reaches the container as -e A=1 -e B=2 (HA_VERSION_LATEST=0 for a baseline test)
  for kv in ${HRI_ENV:-}; do extra="$extra -e $kv"; done
  # shellcheck disable=SC2086
  docker run -d --name "$(n "$name" hri)" --network "hri-$name-net" --memory 2g --cpus 2 --init --stop-timeout 240 \
    --restart unless-stopped -e TZ=UTC -e "HRI_PORT=$base" $extra -p "127.0.0.1:$base:$base" -v "hri-$name-hri:/config" "$image" \
    >/dev/null || die "docker run hri failed"
  echo "stack $name up: HRI http://127.0.0.1:$base  broker 127.0.0.1:$((base + 1))  image $image"
}

down() {
  local name=$1
  # helper containers a test attached to the stack network (a second broker, …) go too, or the network stays
  docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "hri-$name-net" 2>/dev/null | tr ' ' '\n' | grep "^hri-$name" | xargs -r docker rm -f >/dev/null 2>&1
  docker rm -f "$(n "$name" hri)" "$(n "$name" parent)" "$(n "$name" mqtt)" >/dev/null 2>&1
  docker volume rm "hri-$name-hri" "hri-$name-parent" >/dev/null 2>&1
  docker volume ls -q | grep "^hri-$name-" | xargs -r docker volume rm >/dev/null 2>&1
  docker network rm "hri-$name-net" >/dev/null 2>&1
  echo "stack $name removed"
}

api() {  # api <name> <METHOD> <path> [json]
  local name=$1 method=$2 path=$3 body=${4:-}
  docker exec -i -e M="$method" -e P="$path" -e B="$body" -e PORT="$(hriport "$name")" "$(n "$name" hri)" python3 -c '
import os, sys, urllib.request, urllib.error
b = os.environ["B"].encode() if os.environ["B"] else None
r = urllib.request.Request("http://127.0.0.1:%s%s" % (os.environ["PORT"], os.environ["P"]), data=b, method=os.environ["M"],
                           headers={"X-Requested-With": "fetch", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=900) as resp:
        sys.stdout.write(resp.read().decode(errors="replace"))
except urllib.error.HTTPError as e:
    sys.stdout.write("HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:2000]))
except Exception as e:
    sys.stdout.write("ERROR %s" % e)
'
  echo
}

pub() { local name=$1 topic=$2 payload=$3; shift 3; docker exec "$(n "$name" mqtt)" mosquitto_pub -h localhost -t "$topic" -m "$payload" "$@"; }
sub() { local name=$1 topic=$2 secs=${3:-3} count=${4:-100000}; docker exec "$(n "$name" mqtt)" mosquitto_sub -h localhost -t "$topic" -v -W "$secs" -C "$count" 2>/dev/null; true; }

_request() {  # _request <name> <topic> <reply topic> <payload> <secs>
  local name=$1 topic=$2 reply=$3 payload=$4 secs=$5 out
  out=$(mktemp)
  docker exec "$(n "$name" mqtt)" mosquitto_sub -h localhost -t "$reply" -C 1 -W "$secs" > "$out" 2>/dev/null &
  local pid=$!
  sleep 0.7
  pub "$name" "$topic" "$payload"
  wait "$pid"
  cat "$out"; rm -f "$out"
}
call() {  # call <name> <action> [target] [data] [response]
  local name=$1 action=$2 target=${3:-"{}"} data=${4:-"{}"} resp=${5:-0} id
  id="c$(date +%s)$RANDOM"
  local r; [ "$resp" = 1 ] && r=true || r=false
  _request "$name" e2e/call "e2e/result/$id" "{\"id\":\"$id\",\"action\":\"$action\",\"target\":$target,\"data\":$data,\"response\":$r}" 60
}
states() { _request "$1" e2e/states e2e/states/reply "${2:-}" 30; }
registry() { _request "$1" e2e/registry e2e/registry/reply "${2:-}" 30; }

bootstrap() {
  local name=$1 tag=${2:-v7.4.1} i out
  echo "waiting for the HRI API (Home Assistant installs on first start)…"
  for i in $(seq 1 180); do
    out=$(api "$name" GET /api/status 2>/dev/null)
    case "$out" in \{*) break ;; esac
    sleep 5
  done
  case "$out" in \{*) ;; *) die "HRI API not up: $out" ;; esac
  api "$name" POST /api/registry '{"domain":"hri_probe","repo":"trailro/hri-test-integration","name":"HRI probe"}'
  api "$name" POST /api/install "{\"domain\":\"hri_probe\",\"tag\":\"$tag\"}" | cut -c1-300
  api "$name" POST /api/run/start "{\"domain\":\"hri_probe\",\"tag\":\"$tag\"}" | cut -c1-400
  # a start that needs a restart: restart and wait
  api "$name" GET /api/status | grep -q '"restart_required": *true' && { api "$name" POST /api/restart '{}' >/dev/null; sleep 20; for i in $(seq 1 60); do out=$(api "$name" GET /api/status 2>/dev/null); case "$out" in \{*) break;; esac; sleep 5; done; }
  local flow; flow=$(api "$name" POST /api/flow/start '{"domain":"hri_probe"}')
  local fid; fid=$(printf '%s' "$flow" | sed -n 's/.*"flow_id": *"\([^"]*\)".*/\1/p')
  [ -n "$fid" ] || die "flow start: $flow"
  api "$name" POST "/api/flow/$fid" '{"user_input":{"name":"demo","initial":1}}' | cut -c1-200
  api "$name" POST "/api/flow/$fid" '{"user_input":{"next_step_id":"basic"}}' | cut -c1-300
  api "$name" POST /api/mqtt/config '{"enabled":true,"host":"mqtt","port":1883,"discovery_enabled":true,"manager_discovery":true,"manager_commands":true}' | cut -c1-300
  echo "bootstrap of $name done"
}

cmd=${1:-}; shift || true
case "$cmd" in
  up) up "$@" ;;
  down) down "$@" ;;
  bootstrap) bootstrap "$@" ;;
  api) api "$@" ;;
  pub) pub "$@" ;;
  sub) sub "$@" ;;
  call) call "$@" ;;
  states) states "$@" ;;
  registry) registry "$@" ;;
  hri) name=$1; shift; docker exec "$(n "$name" hri)" sh -c "$*" ;;
  parent) name=$1; shift; docker exec "$(n "$name" parent)" sh -c "$*" ;;
  logs) docker logs --tail "${3:-80}" "$(n "$1" "$2")" 2>&1 ;;
  *) sed -n 2,22p "$0"; exit 2 ;;
esac

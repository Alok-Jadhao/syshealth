#!/usr/bin/env bash
# The whole loop, against a container that is really broken.
#
#   chaos -> detection -> investigation -> diagnosis -> remediation -> verification
#
# Everything runs locally: one Docker container as the faulty service, and
# SysHealth on the host measuring that container's cgroup. Nothing touches
# infrastructure you care about, and `cleanup` removes all of it.
#
#   ./chaos/demo.sh up          start the faulty app and the fleet server
#   ./chaos/demo.sh churn       sustained memory pressure (container survives)
#   ./chaos/demo.sh leak        leak past the limit (container gets OOM-killed)
#   ./chaos/demo.sh watch       show what SysHealth measures, live
#   ./chaos/demo.sh sre [MODE]  run the incident loop (default ASSIST)
#   ./chaos/demo.sh report      show the incident in full
#   ./chaos/demo.sh down        stop and remove everything

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
STATE="${SYSHEALTH_CHAOS_STATE:-$HERE/.state}"
NODE="chaos-app"
PORT="${SYSHEALTH_CHAOS_PORT:-5055}"

SYSHEALTH="${SYSHEALTH_BIN:-$ROOT/.venv/bin/syshealth}"
[ -x "$SYSHEALTH" ] || SYSHEALTH="syshealth"

# Compose ships either as a docker plugin or as a standalone binary, and which
# one is present varies by distribution. Detect rather than assume.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
  exit 1
fi

mkdir -p "$STATE"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

need_app() {
  docker inspect chaos-app >/dev/null 2>&1 || die "chaos-app is not running. Run: $0 up"
}

cgroup_of() {
  # The container's own cgroup, which is where its PSI counters live.
  local id
  id="$(docker inspect -f '{{.Id}}' chaos-app)"
  for path in \
    "/sys/fs/cgroup/system.slice/docker-${id}.scope" \
    "/sys/fs/cgroup/docker/${id}" \
    "/sys/fs/cgroup/docker-${id}.scope"; do
    [ -r "$path/memory.pressure" ] && { echo "$path"; return 0; }
  done
  die "no cgroup with memory.pressure for chaos-app. cgroup v2 with CONFIG_PSI=y is required."
}

case "${1:-help}" in

up)
  say "building and starting the faulty app"
  "${COMPOSE[@]}" -f "$HERE/docker-compose.yml" up -d --build
  sleep 3
  curl -sf localhost:8080/health | python3 -m json.tool || die "app did not come up"

  say "starting the fleet server on :$PORT"
  rm -f "$STATE"/*.db
  "$SYSHEALTH" serve --host 127.0.0.1 --port "$PORT" \
    --db "$STATE/fleet.db" --incidents-db "$STATE/incidents.db" \
    > "$STATE/server.log" 2>&1 &
  echo $! > "$STATE/server.pid"
  sleep 2

  say "starting the agent against the container's cgroup"
  "$SYSHEALTH" agent --server "http://127.0.0.1:$PORT" \
    --cgroup "$(cgroup_of)" --node-name "$NODE" --instance-type t3.micro \
    --interval 2 > "$STATE/agent.log" 2>&1 &
  echo $! > "$STATE/agent.pid"
  sleep 3

  echo
  echo "  app        http://localhost:8080/health"
  echo "  dashboard  http://localhost:$PORT/"
  echo
  echo "Next:  $0 leak    then    $0 sre"
  ;;

churn)
  need_app
  SECS="${2:-120}"
  say "sustained memory pressure for ${SECS}s (thrashes, does not die)"
  curl -sf -X POST "localhost:8080/chaos/churn?seconds=$SECS&mb=512" | python3 -m json.tool
  echo
  echo "Give it ~40s to build measurable pressure, then: $0 sre"
  ;;

leak)
  need_app
  MB="${2:-400}"
  say "leaking ${MB}MB into a container limited to 256MB (expect an OOM kill)"
  curl -sf -X POST "localhost:8080/chaos/memory-leak?mb=$MB&rate=48" | python3 -m json.tool
  echo
  echo "Anonymous memory with no swap cannot be reclaimed, so this ends in an"
  echo "OOM kill rather than a stall. For sustained pressure use: $0 churn"
  ;;

burn)
  need_app
  say "burning CPU for ${2:-60}s"
  curl -sf -X POST "localhost:8080/chaos/cpu-burn?seconds=${2:-60}" | python3 -m json.tool
  ;;

crash)
  need_app
  say "killing the app process"
  curl -sf -X POST localhost:8080/chaos/crash | python3 -m json.tool || true
  ;;

reset)
  need_app
  say "clearing every injected fault (what a good remediation looks like)"
  curl -sf -X POST localhost:8080/chaos/reset | python3 -m json.tool
  ;;

watch)
  say "what SysHealth measures for this container"
  curl -s "localhost:$PORT/nodes/$NODE/verdict" | python3 -m json.tool
  ;;

sre)
  MODE="${2:-ASSIST}"
  say "incident loop in $MODE mode"
  "$SYSHEALTH" sre \
    --db "$STATE/fleet.db" --incidents-db "$STATE/incidents.db" \
    --mode "$MODE" --managed "$NODE=container:chaos-app" \
    --autonomous-actions restart_container --once || true
  echo
  echo "Full report:  $0 report"
  ;;

report)
  "$SYSHEALTH" incidents --incidents-db "$STATE/incidents.db"
  echo
  LAST="$("$SYSHEALTH" incidents --incidents-db "$STATE/incidents.db" --json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')"
  [ -n "$LAST" ] && "$SYSHEALTH" incidents "$LAST" --incidents-db "$STATE/incidents.db"
  ;;

down)
  say "stopping everything"
  for pid in agent server; do
    [ -f "$STATE/$pid.pid" ] && kill "$(cat "$STATE/$pid.pid")" 2>/dev/null || true
    rm -f "$STATE/$pid.pid"
  done
  "${COMPOSE[@]}" -f "$HERE/docker-compose.yml" down -v --remove-orphans
  echo "done. State left in $STATE (remove it to reset)."
  ;;

*)
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  ;;
esac

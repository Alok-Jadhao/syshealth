#!/usr/bin/env bash
# Send this working tree to machines set up by hand (README, "Quick Start
# (manual)") and restart them. A fleet from ./aws.sh up has its own:
# ./aws.sh deploy, which already knows the addresses, the key and the token.
#
#   SERVER=1.2.3.4 AGENTS="5.6.7.8 9.10.11.12" SYSHEALTH_TOKEN=... ./deploy.sh [all|agents|server]
#
#   SERVER           the dashboard machine's address
#   AGENTS           the agents' addresses, separated by spaces
#   SYSHEALTH_TOKEN  the token the server and the agents share (README, step 2)
#   KEY              SSH private key (default: syshealth-key.pem, as the README makes it)
#
# Each machine runs from ~/syshealth, as the README sets it up. The files sent
# are the ones git tracks, uncommitted changes included; git add a new file for
# it to go. The agents send to http://$SERVER:5000/metrics.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=terraform/common.sh
source terraform/common.sh

SERVER=${SERVER:-}
AGENTS=${AGENTS:-}
KEY=${KEY:-syshealth-key.pem}
TOKEN=${SYSHEALTH_TOKEN:-}
WHICH=${1:-all}

case "$WHICH" in
  all|agents|server) ;;
  *) die "usage: SERVER=IP AGENTS=\"IP IP\" SYSHEALTH_TOKEN=... ./deploy.sh [all|agents|server]" ;;
esac
# The dashboard's address is needed either way: the agents send to it.
[ -n "$SERVER" ] || die "set SERVER to the dashboard machine's address (no address is built in)"
[ "$WHICH" = server ] || [ -n "$AGENTS" ] || die "set AGENTS to the agents' addresses, separated by spaces"
# Restarting without it would quietly turn the token check off on the server.
[ -n "$TOKEN" ] || die "set SYSHEALTH_TOKEN to the token the server and agents share"
need_tools ssh git python3

SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ -f "$KEY" ] && SSH+=(-i "$KEY")
BUNDLE=$(mktemp)
trap 'rm -f "$BUNDLE"' EXIT
pack_tree > "$BUNDLE"

# Copy the files, then restart one program with its settings. The settings go
# in on stdin, not on the command line, so the token is not in the process list.
# The match is anchored: only a command line that starts with the program, so
# not a shell that merely mentions it.
deploy_to() {   # ip, program, its log, settings
  "${SSH[@]}" "ubuntu@$1" 'mkdir -p ~/syshealth && tar -xzf - -C ~/syshealth' < "$BUNDLE"
  "${SSH[@]}" "ubuntu@$1" bash -s <<EOF
set -e
$4
cd ~/syshealth
pkill -f "^python3 $2" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "^python3 $2" >/dev/null || break; sleep 0.5; done
nohup python3 $2 >> $3 2>&1 < /dev/null &
sleep 3
pgrep -f "^python3 $2" >/dev/null || { echo "$1: $2 did not stay up:"; tail -20 $3; exit 1; }
echo "$1: $2 restarted"
EOF
}

if [ "$WHICH" != agents ]; then
  echo ">> Server. Restarting it empties the graphs: history is kept in memory only."
  deploy_to "$SERVER" server.py server.log "export SYSHEALTH_TOKEN=$(printf %q "$TOKEN")"
  echo ">> Dashboard: http://$SERVER:5000"
fi
if [ "$WHICH" != server ]; then
  echo ">> Agents"
  settings="export SYSHEALTH_TOKEN=$(printf %q "$TOKEN") SYSHEALTH_SERVER_URL=$(printf %q "http://$SERVER:5000/metrics")"
  pids=()
  for IP in $AGENTS; do
    deploy_to "$IP" syshealth.py agent.log "$settings" &
    pids+=($!)
  done
  failed=0
  for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
  [ "$failed" = 0 ] || die "an agent did not restart (above)"
fi

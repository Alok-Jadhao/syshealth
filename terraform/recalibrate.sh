#!/usr/bin/env bash
# Re-measure every agent's idle baseline, on boxes that are actually idle.
#
# Baselines are taken once, for 60 seconds, at first boot — immediately after
# apt-get and a git clone, while the box is still settling. Ubuntu's apt-daily
# and unattended-upgrades timers fire around then too. On a t3.micro there is no
# headroom to absorb that, so the "idle" floor is recorded far too high.
#
# States no longer depend on it - they use fixed thresholds, see
# docs/thresholds.md - so this is optional: it only corrects the idle reference
# each agent reports. It mattered when every state was a ratio against that
# floor, and an inflated baseline made the most stressed machine read HEALTHY.
#
# Nothing may be under load while this runs, or the load becomes the baseline.
set -uo pipefail

cd "$(dirname "$0")"
KEY=${KEY:-~/.ssh/syshealth-mumbai.pem}
KEY=${KEY/#\~/$HOME}

[ -f "$KEY" ] || {
  echo "No SSH key at $KEY" >&2
  echo "Set one:  KEY=~/.ssh/your-key.pem $0" >&2
  exit 1
}

AGENTS=$(terraform output -json instances 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(' '.join(v['public_ip'] for k, v in d.items() if k != 'dashboard'))
") || { echo "No terraform outputs. Has apply finished in this directory?" >&2; exit 1; }

[ -n "$AGENTS" ] || { echo "No agent instances found in the state." >&2; exit 1; }

echo "Recalibrating $(echo "$AGENTS" | wc -w) agents. 60 seconds, in parallel."
echo "Do not stress anything until this finishes."
echo

for ip in $AGENTS; do
  (
    out=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$KEY" \
      ubuntu@"$ip" '
        sudo systemctl stop syshealth
        cd /opt/syshealth || exit 1
        sudo python3 syshealth.py calibrate
        sudo systemctl start syshealth
      ' 2>&1)
    if [ $? -eq 0 ]; then
      printf '  %-16s %s\n' "$ip" "$(echo "$out" | grep -i 'baseline saved' || echo 'done')"
    else
      printf '  %-16s FAILED: %s\n' "$ip" "$(echo "$out" | tail -1)"
    fi
  ) &
done
wait

echo
echo "Baselines now in use:"
URL=$(terraform output -raw dashboard_url 2>/dev/null)
sleep 12   # let each agent push at least once with the new value
curl -s --max-time 10 "$URL/instances" 2>/dev/null | python3 -c "
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    print('  (dashboard not reachable — check ssh_cidr)'); sys.exit()
for i in rows:
    l = i.get('latest') or {}
    print('  %-11s baseline=%s' % (i['instance_type'], l.get('baseline')))
"
echo
echo "They should be the same order of magnitude. One far above the rest means"
echo "that box was still busy — wait a minute and run this again."

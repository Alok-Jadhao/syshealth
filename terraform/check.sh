#!/usr/bin/env bash
# Is the fleet reporting? Run from the terraform directory, after apply.
#
# Answers the only question that matters while you wait — which sizes are
# pushing and which are not — and for the ones that are not, prints the command
# that says why. Read-only: it starts nothing and changes nothing.
set -uo pipefail

cd "$(dirname "$0")"

if ! command -v terraform >/dev/null; then
  echo "terraform not on PATH; run this from a shell that has it" >&2
  exit 1
fi

URL=$(terraform output -raw dashboard_url 2>/dev/null)
if [ -z "$URL" ]; then
  echo "No dashboard_url output. Has 'terraform apply' finished in this directory?" >&2
  exit 1
fi

echo "dashboard: $URL"

SERIES=$(curl -fsS --max-time 10 "$URL/series" 2>/dev/null)
if [ -z "$SERIES" ]; then
  cat >&2 <<EOF

Could not reach $URL/series.

Two usual reasons, in order of likelihood:

  1. Your address changed, and the security group still names the old one.
     Compare:  curl -4 -s ifconfig.me
     against:  ssh_cidr in terraform.tfvars
     If they differ, update the file and 'terraform apply' again — only the
     security group rules change, the instances are left alone.

  2. The server is still installing. Give it two or three minutes from apply,
     then run this again. If it stays silent past five, ask the box why:

     ssh ubuntu@$(terraform output -json instances 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["dashboard"]["public_ip"])' 2>/dev/null) 'sudo tail -30 /var/log/syshealth-bootstrap.log'
     ssh ubuntu@$(terraform output -json instances 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["dashboard"]["public_ip"])' 2>/dev/null) 'sudo journalctl -u syshealth-server -n 50'
EOF
  exit 1
fi

python3 - "$SERIES" <<'PY'
import json, sys

want = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
found = json.loads(sys.argv[1])["instances"]

print()
print("%-11s %-8s %8s %8s %10s  %s" % (
    "SIZE", "ONLINE", "SAMPLES", "PSI", "BASELINE", "STATE"))

seen = set()
for i in found:
    size = i.get("instance_type") or "unknown"
    seen.add(size)
    latest = i.get("latest") or (i["samples"][-1] if i.get("samples") else {}) or {}
    print("%-11s %-8s %8d %8s %10s  %s" % (
        size,
        "yes" if i.get("online") else "no",
        len(i.get("samples") or []),
        latest.get("psi", "-"),
        latest.get("baseline", "-"),
        latest.get("state", "-"),
    ))

missing = [s for s in want if s not in seen]
print()
if not missing:
    print("All four sizes reporting. Open 'Load test' on the dashboard.")
    sys.exit(0)

print("Not reporting: " + ", ".join(missing))
print()
print("Each takes two to three minutes from apply — package install, then a")
print("60s idle calibration before the first push. If one is still absent after")
print("five, ask it directly (public_ip is in 'terraform output instances'):")
print()
print("  ssh ubuntu@<ip> 'sudo tail -30 /var/log/syshealth-bootstrap.log'")
print("  ssh ubuntu@<ip> 'sudo journalctl -u syshealth -n 30'")
print()
print("An agent that cannot reach the dashboard now says so in its own log:")
print("  'push to http://... failed'")
sys.exit(1)
PY

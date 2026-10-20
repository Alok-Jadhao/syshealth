# Shared by ../aws.sh, rightsize.sh and ../deploy.sh: sourced, not run. The
# terraform.tfvars helpers expect to be in the terraform directory.

SYSHEALTH_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The key the fleet is built with when terraform.tfvars names no key pair of
# its own. Made on first use; only the public half goes to AWS.
SSH_KEY=${SYSHEALTH_SSH_KEY:-$HOME/.ssh/syshealth_ed25519}
DEFAULT_TYPES="t3.micro t3.small t3.medium t3.large"

die() { echo "${0##*/}: $*" >&2; exit 1; }

need_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "$tool is not on PATH"
  done
}

# A setting from terraform.tfvars, if there is one and it sets it.
tfvar() {
  [ -f terraform.tfvars ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\(.*\)\".*/\1/p" terraform.tfvars | head -1
}

# The names terraform.tfvars sets, for saying that it applies too.
tfvars_names() {
  [ -f terraform.tfvars ] || return 0
  sed -n 's/^[[:space:]]*\([a-z_]*\)[[:space:]]*=.*/\1/p' terraform.tfvars | paste -sd, - | sed 's/,/, /g'
}

# Graviton families (t4g, m7g, c6gd...) need an arm64 image; main.tf looks up amd64.
check_x86() {
  local t
  for t in $1; do
    if [[ "${t%%.*}" =~ [0-9]g ]]; then
      die "$t is an ARM (Graviton) type, and the image main.tf uses is amd64. Compare x86 sizes."
    fi
  done
}

types_hcl() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$1"
}

# This machine's address as the internet sees it, as a /32: the one address
# the security groups admit. IPv4, because many ISPs answer with IPv6 unless
# asked and the instances' rules are IPv4.
my_cidr() {
  local ip
  ip=$(curl -4 -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null ||
       curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null) || return 1
  ip=$(printf '%s' "$ip" | tr -d '[:space:]')
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  echo "$ip/32"
}

ensure_ssh_key() {
  [ -f "$SSH_KEY" ] && return 0
  if [ ! -d "$(dirname "$SSH_KEY")" ]; then
    mkdir -p "$(dirname "$SSH_KEY")"
    chmod 700 "$(dirname "$SSH_KEY")"
  fi
  ssh-keygen -q -t ed25519 -N "" -C "syshealth" -f "$SSH_KEY"
  echo "Made an SSH key for the fleet: $SSH_KEY"
}

# "about $0.19 an hour" for these sizes and the dashboard's, at list price.
fleet_cost() {
  (cd "$SYSHEALTH_ROOT" && python3 -c '
import sys, pricing
types = sys.argv[2].split() + [sys.argv[3]]
prices = [pricing.lookup(t, sys.argv[1]) for t in types]
missing = [t for t, p in zip(types, prices) if not p]
total = sum(p["usd_hour"] for p in prices if p)
print("about $%.2f an hour" % total + (" plus %s, not in the price list" % ", ".join(missing) if missing else ""))
' "$1" "$2" "$3")
}

# This working tree as a .tar.gz on stdout: every file git tracks, as it is on
# disk now, uncommitted changes included. Untracked files stay behind, so
# state, tfvars and saved reports never leave this machine.
pack_tree() {
  git -C "$SYSHEALTH_ROOT" rev-parse --git-dir >/dev/null 2>&1 ||
    die "deploying sends the files git tracks, so it needs a git checkout of syshealth"
  (cd "$SYSHEALTH_ROOT" && python3 -c '
import os, subprocess, sys, tarfile
names = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
    for name in names.decode().split("\0"):
        if name and os.path.isfile(name):   # tracked but deleted here: skip
            tar.add(name)
')
}

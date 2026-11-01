#!/usr/bin/env bash
# Bring the fleet back up, with the things that have bitten before checked first.
#
# Run from anywhere:  ./terraform/relaunch.sh
#
# Every check here is one that has actually cost an evening: a stale ssh_cidr
# that locks you out of your own dashboard, an unpinned AMI that rebuilds the
# fleet, a repo_branch naming a tag that was never pushed. None of them announce
# themselves — they present as an instance that came up and does nothing.
set -uo pipefail

cd "$(dirname "$0")"
TFVARS=terraform.tfvars
fail() { printf '\n\033[31mSTOP:\033[0m %s\n' "$1" >&2; exit 1; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$1"; }

echo "=== pre-flight ==="

command -v terraform >/dev/null || fail "terraform is not on PATH."
command -v aws >/dev/null || fail "the aws CLI is not on PATH."
[ -f "$TFVARS" ] || fail "$TFVARS is missing. You are probably in the wrong checkout."

# Credentials, before anything slow. An expired session shows up here as a
# clear error rather than thirty seconds into a plan.
IDENT=$(aws sts get-caller-identity --query Account --output text 2>&1) \
  || fail "AWS credentials are not working:
$IDENT

Run 'aws configure' and try again."
ok "AWS account $IDENT"

# The AMI must be pinned, or apply may decide to rebuild everything because
# Canonical published a new image since the rehearsal.
grep -q '^ami_id' "$TFVARS" \
  || fail "ami_id is not set in $TFVARS.

Without it Terraform takes Canonical's newest image, which is not the one you
tested on. Read the value you rehearsed with out of your notes, or drop the pin
deliberately if you accept a different image."
ok "AMI pinned: $(grep '^ami_id' "$TFVARS" | cut -d'"' -f2)"

# repo_branch may name a tag, but only if that tag was actually pushed. If it
# was not, every instance fails at 'git clone --branch' and the fleet comes up
# empty with the reason buried in a log you have to SSH in to read.
REF=$(grep '^repo_branch' "$TFVARS" | cut -d'"' -f2)
if [ -n "$REF" ]; then
  URL=$(grep '^repo_url' "$TFVARS" | cut -d'"' -f2)
  [ -n "$URL" ] || URL=$(awk -F'"' '/default.*github.com.*syshealth/{print $2; exit}' variables.tf)
  git ls-remote --exit-code "$URL" "refs/tags/$REF" >/dev/null 2>&1 \
    || git ls-remote --exit-code "$URL" "refs/heads/$REF" >/dev/null 2>&1 \
    || fail "repo_branch is \"$REF\", but no branch or tag by that name exists on
$URL

The instances clone that ref at boot, so every one of them would fail to start.
Push the tag first:  git push origin $REF"
  ok "repo ref exists: $REF"
fi

# Your address, which changes when your network does. A stale value here does
# not fail loudly — the dashboard simply never loads, exactly as if the server
# had not started.
#
# Validated strictly, and against more than one service. These endpoints answer
# with an HTML error page or a captive-portal redirect often enough that a loose
# check writes prose into terraform.tfvars, and the failure then surfaces as an
# opaque provider error three steps later.
my_ip() {
  local svc ip
  for svc in https://ifconfig.me https://api.ipify.org https://icanhazip.com; do
    ip=$(curl -4 -fsS --max-time 10 "$svc" 2>/dev/null | tr -d '[:space:]')
    printf '%s' "$ip" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || continue
    printf '%s' "$ip" | awk -F. '{for (i = 1; i <= 4; i++) if ($i > 255) exit 1}' || continue
    printf '%s' "$ip"
    return 0
  done
  return 1
}

MYIP=$(my_ip) || fail "Could not determine your public IPv4 address.

Tried ifconfig.me, api.ipify.org and icanhazip.com; none returned one. Either
you are offline, behind something intercepting HTTPS, or on an IPv6-only
connection — and these security group rules have to be IPv4, because instances
in the default VPC get IPv4 addresses.

Find it another way and set ssh_cidr in $TFVARS by hand."

CUR=$(grep '^ssh_cidr' "$TFVARS" | cut -d'"' -f2)
if [ "$CUR" != "$MYIP/32" ]; then
  echo "  ssh_cidr is $CUR but you are coming from $MYIP"
  sed -i.bak "s|^ssh_cidr.*|ssh_cidr = \"$MYIP/32\"|" "$TFVARS"
  ok "ssh_cidr updated to $MYIP/32  (previous file kept as $TFVARS.bak)"
else
  ok "ssh_cidr already matches $MYIP"
fi

echo
echo "=== plan ==="
terraform init -input=false >/dev/null || fail "terraform init failed."
terraform plan -input=false -out=.relaunch.plan -no-color \
  | grep -E '^  # |^Plan:|^No changes' || true

echo
read -r -p "Apply this? [yes/no] " reply
[ "$reply" = "yes" ] || { rm -f .relaunch.plan; echo "Nothing applied."; exit 0; }

terraform apply -input=false .relaunch.plan || fail "apply failed — read the error above.
If it is InsufficientInstanceCapacity, that zone is short of that size right
now. Re-running often works, since instances spread across zones."
rm -f .relaunch.plan

echo
echo "=== waiting for agents ==="
echo "Package install, then a 60s idle calibration on each box before it pushes."

for i in $(seq 1 40); do
  if ./check.sh >/dev/null 2>&1; then
    echo
    ./check.sh
    echo
    echo "Open: $(terraform output -raw dashboard_url)"
    exit 0
  fi
  printf '\r  waiting... %ds' $((i * 15))
  sleep 15
done

echo
echo "Not every size is reporting after 10 minutes. Current state:"
echo
./check.sh
exit 1

#!/usr/bin/env bash
# One command: build a fleet of candidate sizes, run your app's load on all of
# them at once, write down which to buy, and tear the fleet down again.
#
#   ./rightsize.sh --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
#   ./rightsize.sh --peak 1600 --for 120 --types "t3.small t3a.medium m7i.large"
#
# Options of its own:
#   --types "A B C"   sizes to compare (default: t3.micro t3.small t3.medium t3.large);
#                     x86 only, as the image main.tf uses is amd64
#   --keep            leave the fleet up afterwards (it keeps billing)
#   --yes             do not ask before creating instances
# Everything else goes to `cli.py run`: --need, --margin, --peak/--for,
# --ramp/--wave/--bursts with --steps/--step-sec, --workers, --expect.
#
# It needs what terraform/README.md describes (credentials and a
# terraform.tfvars with key_name), works in its own Terraform workspace with
# its own resource names, so it cannot touch a fleet you built by hand, and
# destroys what it built when the test ends, fails or is interrupted. The
# report lands in ../rightsize-<date>.json; the exit status is cli.py's.
set -euo pipefail
cd "$(dirname "$0")"

WORKSPACE=rightsize
PREFIX=syshealth-rs
TYPES="t3.micro t3.small t3.medium t3.large"
KEEP=0
YES=0
CLI_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --types) TYPES="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) CLI_ARGS+=("$1"); shift ;;
  esac
done

die() { echo "rightsize: $*" >&2; exit 1; }

for tool in terraform python3 curl; do
  command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
[ -f terraform.tfvars ] || die "no terraform.tfvars here; see terraform/README.md (key_name at least)"
[ ${#CLI_ARGS[@]} -gt 0 ] || die "say what load to run, e.g. --need 1600 --ramp 200:3200 (see --help)"

# Check the test before paying for anything: the same validation the server
# applies, run here.
PLAN=$(python3 ../cli.py run --dry-run "${CLI_ARGS[@]}") || exit 1

for t in $TYPES; do
  # Graviton families (t4g, m7g, c6gd...) need an arm64 image; main.tf looks up amd64.
  if [[ "${t%%.*}" =~ [0-9]g ]]; then
    die "$t is an ARM (Graviton) type, and the image main.tf uses is amd64. Compare x86 sizes."
  fi
done

TYPES_HCL=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$TYPES")
COUNT=$(echo "$TYPES" | wc -w)
REGION=$(sed -n 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' terraform.tfvars | head -1)
REGION=${REGION:-ap-south-1}
SERVER_TYPE=$(sed -n 's/^[[:space:]]*server_instance_type[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' terraform.tfvars | head -1)
SERVER_TYPE=${SERVER_TYPE:-t3.small}

COST=$(cd .. && python3 -c '
import sys, pricing
types = sys.argv[2].split() + [sys.argv[3]]
prices = [pricing.lookup(t, sys.argv[1]) for t in types]
missing = [t for t, p in zip(types, prices) if not p]
total = sum(p["usd_hour"] for p in prices if p)
print("about $%.2f an hour" % total + (" plus %s, not in the price list" % ", ".join(missing) if missing else ""))
' "$REGION" "$TYPES" "$SERVER_TYPE")

# The security group admits one address: this machine's, as the internet sees it.
CIDR="$(curl -4 -fsS --max-time 10 ifconfig.me)/32" || die "could not find this machine's public IPv4 address"

cat <<EOF
This creates $((COUNT + 1)) EC2 instances in $REGION: $TYPES, and a $SERVER_TYPE
for the dashboard. While they are up they cost $COST.
The test: $PLAN. Allow about ten minutes more to build the fleet and let it settle.
$([ "$KEEP" = 1 ] && echo "--keep: they stay up afterwards, and keep billing, until you destroy them." \
                  || echo "They are destroyed when the test ends, fails or is interrupted.")
EOF
if [ "$YES" != 1 ]; then
  read -r -p "Go ahead? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Nothing created."; exit 0 ;; esac
fi

TF_VARS=(-var "instance_types=$TYPES_HCL" -var "name_prefix=$PREFIX" -var "ssh_cidr=$CIDR")
# The same, quoted for pasting into a shell.
DESTROY="cd $(pwd) && terraform workspace select $WORKSPACE && terraform destroy $(printf '%q ' "${TF_VARS[@]}")"

terraform init -input=false >/dev/null
terraform workspace select "$WORKSPACE" >/dev/null 2>&1 || terraform workspace new "$WORKSPACE" >/dev/null
if [ -n "$(terraform state list 2>/dev/null)" ]; then
  die "the '$WORKSPACE' workspace still holds instances from an earlier run. Destroy them first:
  $DESTROY"
fi

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$KEEP" = 1 ]; then
    echo
    echo "The fleet is still up (--keep). When you are done:"
    echo "  $DESTROY"
  else
    echo
    echo "Destroying the fleet..."
    if terraform destroy -auto-approve -input=false "${TF_VARS[@]}" >/dev/null; then
      terraform workspace select default >/dev/null && terraform workspace delete "$WORKSPACE" >/dev/null || true
      echo "Destroyed."
    else
      echo "DESTROY FAILED. The instances may still be running and billing. Run:" >&2
      echo "  $DESTROY" >&2
      status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "Creating the fleet (a few minutes)..."
terraform apply -auto-approve -input=false "${TF_VARS[@]}" >/dev/null
URL=$(terraform output -raw dashboard_url)
# The fleet's own secret: cli.py sends it, and the dashboard asks for it.
SYSHEALTH_TOKEN=$(terraform output -raw token)
export SYSHEALTH_TOKEN
echo "Dashboard: $URL  (to start a test from it: terraform output -raw token)"

# Installing, calibrating and the first minute of samples take a while.
python3 ../cli.py --server "$URL" wait --count "$COUNT" --timeout 900

REPORT="../rightsize-$(date +%Y-%m-%d-%H%M).json"
set +e
python3 ../cli.py --server "$URL" run "${CLI_ARGS[@]}" --save "$REPORT"
result=$?
set -e
exit "$result"

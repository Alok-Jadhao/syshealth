#!/usr/bin/env bash
# SysHealth on AWS: one command up, one command down.
#
#   ./aws.sh up --profile NAME     build the fleet (a dashboard and four sizes)
#                                  and wait until every size is reporting
#   ./aws.sh down                  destroy all of it
#
#   ./aws.sh open                  the dashboard in your browser, signed in
#   ./aws.sh status                what is up, since when, and what it has cost
#   ./aws.sh test --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
#                                  run the right-sizing test on it (cli.py run)
#   ./aws.sh ssh t3.small          a shell on one machine (or: dashboard)
#   ./aws.sh deploy [agents|server]
#                                  send this working tree to the fleet and
#                                  restart it, without rebuilding anything
#   ./aws.sh shortcuts --profile NAME
#                                  two icons, on the desktop and in the app list:
#                                  "SysHealth demo on" runs up and opens the
#                                  dashboard, "SysHealth demo off" runs down
#                                  (Linux; --remove takes them away again)
#
# Options for up:
#   --profile NAME   AWS CLI profile (default: $AWS_PROFILE, else your default keys)
#   --region NAME    default ap-south-1, or region in terraform/terraform.tfvars
#   --types "A B C"  sizes to compare (default: t3.micro t3.small t3.medium t3.large);
#                    x86 only, as the image main.tf uses is amd64
#   --yes            do not ask first (down takes it too)
#   --no-open        do not open the dashboard in a browser at the end
#
# Nothing to prepare but AWS credentials: no key pair (an SSH key is made at
# ~/.ssh/syshealth_ed25519 on first use) and no terraform.tfvars (one is still
# read if it is there). The firewall admits this machine's address only; run up
# again from a new address to move it. up remembers the account it built in,
# and down refuses any other, so a changed AWS_PROFILE cannot leave a fleet
# running unseen. Its own Terraform workspace, so it never touches a fleet
# built by hand or by terraform/rightsize.sh. deploy sends the files git
# tracks, uncommitted changes included; git add a new file for it to go.
set -euo pipefail
SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
cd "$(dirname "$0")/terraform"
# shellcheck source=terraform/common.sh
source ./common.sh

WORKSPACE=fleet
PREFIX=syshealth
SAVED=.aws-fleet                   # what up built with; see save_settings
KNOWN_HOSTS=.aws-fleet-known_hosts # fleet IPs are reused, so not ~/.ssh's
export TF_IN_AUTOMATION=1

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$SELF"; }

tf() { TF_WORKSPACE=$WORKSPACE terraform "$@"; }

ensure_workspace() {
  terraform init -input=false -no-color >/dev/null
  if ! terraform workspace list | sed 's/^[* ]*//' | grep -qx "$WORKSPACE"; then
    local current
    current=$(terraform workspace show)
    terraform workspace new "$WORKSPACE" >/dev/null
    terraform workspace select "$current" >/dev/null
  fi
}

load_settings() {
  S_PROFILE="" S_ACCOUNT="" S_REGION="" S_TYPES="" S_CIDR=""
  # shellcheck disable=SC1090
  [ -f "$SAVED" ] && source "$SAVED"
  return 0
}

save_settings() {
  printf 'S_PROFILE=%q\nS_ACCOUNT=%q\nS_REGION=%q\nS_TYPES=%q\nS_CIDR=%q\n' \
    "$1" "$2" "$3" "$4" "$5" > "$SAVED"
}

# Point Terraform and the AWS CLI at a profile and say which account that is.
use_profile() {
  if [ -n "$1" ]; then export AWS_PROFILE=$1; else unset AWS_PROFILE; fi
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) ||
    die "the AWS credentials${1:+ of profile '$1'} do not work. Try: aws sts get-caller-identity${1:+ --profile $1}"
}

# down, status and test act on what up built, and only from the account it is in.
require_fleet() {
  load_settings
  [ -n "$S_ACCOUNT" ] || { echo "Nothing is up. Build it with: ./aws.sh up --profile NAME"; exit 0; }
  use_profile "$S_PROFILE"
  [ "$ACCOUNT" = "$S_ACCOUNT" ] ||
    die "the fleet is in account $S_ACCOUNT, but ${S_PROFILE:+profile '$S_PROFILE' }now reaches account $ACCOUNT. Fix the credentials first: acting from the wrong account would lose track of the instances."
}

set_tf_vars() {   # region, types, cidr
  TF_VARS=(-var "region=$1" -var "instance_types=$(types_hcl "$2")"
           -var "name_prefix=$PREFIX" -var "ssh_cidr=$3")
  if [ -f "$SSH_KEY.pub" ]; then
    TF_VARS+=(-var "ssh_public_key=$(cat "$SSH_KEY.pub")")
  fi
}

instances_json() { tf output -json instances 2>/dev/null || echo '{}'; }

# The dashboard with its token after the #: the browser never sends that part
# to the server, and the page keeps it as if it had been typed, so Start works
# without anyone copying the token.
signed_in_link() {
  local url token
  url=$(tf output -raw dashboard_url 2>/dev/null) && token=$(tf output -raw token 2>/dev/null) ||
    return 1
  echo "$url/#token=$token"
}

# The browser of the desktop this runs on, if it has one. In its own session,
# so closing the terminal that started it does not close the browser too.
open_link() {
  if [ "$(uname)" = Darwin ]; then
    open "$1"
  elif command -v xdg-open >/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    setsid -f xdg-open "$1" >/dev/null 2>&1
  else
    return 1
  fi
}

# How to reach the fleet: its own key (unless terraform.tfvars names a key pair
# of yours, used from your agent) and its own known_hosts.
ssh_opts() {
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$PWD/$KNOWN_HOSTS")
  [ -n "$(tfvar key_name)" ] || SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
}

# Only the lines that say something finished, so a long apply shows progress.
progress() { awk '/complete|Apply complete|Destroy complete/ { print "  " $0; fflush() }'; }

confirm() {
  [ "$YES" = 1 ] && return 0
  local answer
  read -r -p "Go ahead? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Nothing changed."; exit 0 ;; esac
}

cmd_up() {
  local profile="" region="" types=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile=$2; shift 2 ;;
      --region) region=$2; shift 2 ;;
      --types) types=$2; shift 2 ;;
      --yes) YES=1; shift ;;
      --no-open) NO_OPEN=1; shift ;;
      *) die "up does not take '$1' (see ./aws.sh --help)" ;;
    esac
  done
  need_tools terraform aws python3 curl ssh-keygen

  load_settings
  if [ -n "$S_ACCOUNT" ]; then
    # Already up: run again, it updates the fleet (a new address, new sizes).
    profile=${profile:-$S_PROFILE}
    region=${region:-$S_REGION}
    types=${types:-$S_TYPES}
    [ "$region" = "$S_REGION" ] ||
      die "the fleet is in $S_REGION. Take it down first (./aws.sh down) to move it to $region."
  fi
  profile=${profile:-${AWS_PROFILE:-}}
  region=${region:-$(tfvar region)}
  region=${region:-ap-south-1}
  types=${types:-$DEFAULT_TYPES}
  check_x86 "$types"

  use_profile "$profile"
  if [ -n "$S_ACCOUNT" ] && [ "$ACCOUNT" != "$S_ACCOUNT" ]; then
    die "the fleet is in account $S_ACCOUNT, but these credentials reach $ACCOUNT. Take it down from $S_ACCOUNT first."
  fi
  ensure_ssh_key
  local cidr server_type count cost
  cidr=$(my_cidr) || die "could not find this machine's public IPv4 address"
  server_type=$(tfvar server_instance_type)
  server_type=${server_type:-t3.small}
  count=$(echo "$types" | wc -w | tr -d ' ')
  cost=$(fleet_cost "$region" "$types" "$server_type")

  ensure_workspace
  echo "Account $ACCOUNT${profile:+ (profile $profile)}, region $region."
  if [ -n "$(tf state list 2>/dev/null)" ]; then
    echo "The fleet is already up; this brings it in line: sizes $types,"
    echo "reachable from $cidr. Instances are only replaced if their size or setup changed."
  else
    echo "This creates $((count + 1)) EC2 instances: $types, and a $server_type for the dashboard,"
    echo "reachable from $cidr only. They cost $cost until ./aws.sh down."
  fi
  if [ -n "$(tfvars_names)" ]; then
    echo "terraform/terraform.tfvars applies too: $(tfvars_names)."
  fi
  confirm

  # Written before anything exists, so down can clean up after a failed apply.
  save_settings "$profile" "$ACCOUNT" "$region" "$types" "$cidr"
  set_tf_vars "$region" "$types" "$cidr"
  echo "Building (two or three minutes)..."
  if ! tf apply -auto-approve -input=false -no-color "${TF_VARS[@]}" | progress; then
    die "the build failed (above). Run ./aws.sh up again to retry, or ./aws.sh down to remove what was made."
  fi

  local url token opened
  url=$(tf output -raw dashboard_url)
  token=$(tf output -raw token)
  echo "Waiting for the sizes to install, calibrate and report (five minutes or so; Ctrl-C is safe)..."
  if ! python3 "$SYSHEALTH_ROOT/cli.py" --server "$url" wait --count "$count" --timeout 900; then
    echo "Not every size is reporting yet. It is up all the same; look at one with:"
    echo "  ./aws.sh ssh <size> sudo tail -30 /var/log/syshealth-bootstrap.log"
  fi
  opened="./aws.sh open opens it signed in"
  if [ "$NO_OPEN" != 1 ] && open_link "$(signed_in_link)"; then
    opened="opened in your browser, signed in"
  fi
  cat <<EOF

SysHealth is up in account $ACCOUNT ($region).
  Dashboard  $url   ($opened; from this machine's address only)
  Token      $token   (already filled in on the page ./aws.sh opens)
  Test       ./aws.sh test --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
  SSH        ./aws.sh ssh dashboard   (or a size: ${types// /, })
  Deploy     ./aws.sh deploy          (this working tree, without rebuilding)
  Status     ./aws.sh status
It costs $cost until you run:  ./aws.sh down
EOF
}

cmd_down() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes) YES=1; shift ;;
      *) die "down does not take '$1'" ;;
    esac
  done
  need_tools terraform aws python3
  require_fleet
  ensure_workspace
  local ids n
  ids=$(instances_json | python3 -c 'import json,sys; print(" ".join(v["id"] for v in json.load(sys.stdin).values()))')
  n=$(tf state list 2>/dev/null | grep -c '^aws_instance\.' || true)
  echo "This destroys the SysHealth fleet in account $S_ACCOUNT ($S_REGION):"
  echo "$n instances, their security groups and the key pair."
  confirm

  set_tf_vars "$S_REGION" "$S_TYPES" "$S_CIDR"
  echo "Destroying..."
  if ! tf destroy -auto-approve -input=false -no-color "${TF_VARS[@]}" | progress; then
    die "the destroy failed (above), so some of it may still be running and billing. Run ./aws.sh down again."
  fi
  if [ -n "$ids" ]; then
    # Check with EC2 itself, not just Terraform's word for it.
    local left
    # shellcheck disable=SC2086
    left=$(aws ec2 describe-instances --region "$S_REGION" --instance-ids $ids \
             --query 'Reservations[].Instances[?State.Name!=`terminated` && State.Name!=`shutting-down`].InstanceId' \
             --output text)
    [ -z "$left" ] || die "EC2 still lists $left as running. Run ./aws.sh down again, or look in the console."
  fi
  rm -f "$SAVED" "$KNOWN_HOSTS"
  terraform workspace delete "$WORKSPACE" >/dev/null 2>&1 || true
  echo "Destroyed: nothing of the fleet is left running in $S_ACCOUNT."
}

cmd_status() {
  need_tools terraform aws python3
  require_fleet
  local machines ids url ec2
  machines=$(instances_json)
  ids=$(python3 -c 'import json,sys; print(" ".join(v["id"] for v in json.loads(sys.argv[1]).values()))' "$machines")
  if [ -z "$ids" ]; then
    echo "Account $S_ACCOUNT ($S_REGION): no instances, though up was started. ./aws.sh up to finish, or ./aws.sh down."
    return 0
  fi
  # shellcheck disable=SC2086
  ec2=$(aws ec2 describe-instances --region "$S_REGION" --instance-ids $ids --output json \
    --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,state:State.Name,launched:LaunchTime,ip:PublicIpAddress}')
  echo "Account $S_ACCOUNT${S_PROFILE:+ (profile $S_PROFILE)}, region $S_REGION:"
  (cd "$SYSHEALTH_ROOT" && python3 - "$machines" "$ec2" "$S_REGION" <<'EOF'
import datetime, json, sys
import pricing
names = {v["id"]: k for k, v in json.loads(sys.argv[1]).items()}
order = list(names)
now = datetime.datetime.now(datetime.timezone.utc)
per_hour = spent = 0.0
print("  %-10s %-10s %-9s %-16s %6s" % ("MACHINE", "SIZE", "STATE", "ADDRESS", "UP"))
for r in sorted(json.loads(sys.argv[2]), key=lambda r: order.index(r["id"])):
    launched = datetime.datetime.fromisoformat(r["launched"].replace("Z", "+00:00"))
    hours = (now - launched).total_seconds() / 3600
    price = pricing.lookup(r["type"], sys.argv[3])
    if price and r["state"] in ("pending", "running"):
        per_hour += price["usd_hour"]
        spent += price["usd_hour"] * hours
    print("  %-10s %-10s %-9s %-16s %5.1fh" % (
        names.get(r["id"], "?"), r["type"], r["state"], r.get("ip") or "-", hours))
print("Costing about $%.2f an hour, about $%.2f so far (list prices)." % (per_hour, spent))
EOF
  )
  url=$(tf output -raw dashboard_url 2>/dev/null) || url=""
  if [ -n "$url" ]; then
    echo "Dashboard: $url"
    python3 "$SYSHEALTH_ROOT/cli.py" --server "$url" status ||
      echo "(The dashboard did not answer. If this machine's address changed, ./aws.sh up moves the firewall to it.)"
  fi
}

cmd_open() {
  [ $# -eq 0 ] || die "open takes nothing (see ./aws.sh --help)"
  need_tools terraform
  load_settings
  [ -n "$S_ACCOUNT" ] || die "nothing is up. Build it first: ./aws.sh up --profile NAME"
  local link
  link=$(signed_in_link) || die "the fleet has no dashboard yet. ./aws.sh up finishes it."
  if open_link "$link"; then
    echo "Opened the dashboard in your browser, signed in."
  else
    echo "No desktop browser here. Open this link, which carries the token:"
    echo "  $link"
  fi
}

# Two launchers: one click runs up and opens the dashboard signed in, one
# click runs down. Each opens a terminal, so the cost, the progress and any
# error stay in sight, and waits for Enter before it closes.
cmd_shortcuts() {
  local profile="" remove=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile=$2; shift 2 ;;
      --remove) remove=1; shift ;;
      *) die "shortcuts does not take '$1' (see ./aws.sh --help)" ;;
    esac
  done
  [ "$(uname)" = Linux ] || die "shortcuts are for Linux desktops. Elsewhere: ./aws.sh up and ./aws.sh down."
  # The command sits inside quotes in the launcher, where these would not survive.
  case "$SELF$profile" in
    *[\"\'\`\$\\%]*) die "the path to aws.sh or the profile has a quote, \$, \\ or % in it, which a launcher cannot hold." ;;
  esac
  local apps desktop file on=syshealth-demo-on.desktop off=syshealth-demo-off.desktop
  apps=${XDG_DATA_HOME:-$HOME/.local/share}/applications
  desktop=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
  # xdg-user-dir answers the home folder itself when there is no desktop folder.
  { [ "$desktop" != "$HOME" ] && [ -d "$desktop" ]; } || desktop=""
  if [ "$remove" = 1 ]; then
    rm -f "$apps/$on" "$apps/$off"
    [ -z "$desktop" ] || rm -f "$desktop/$on" "$desktop/$off"
    echo "Removed the SysHealth demo shortcuts."
    return 0
  fi
  mkdir -p "$apps"
  launcher "$apps/$on" "SysHealth demo on" "Build the SysHealth fleet on AWS and open the dashboard" \
    "up${profile:+ --profile $profile} --yes" media-playback-start
  launcher "$apps/$off" "SysHealth demo off" "Destroy the SysHealth fleet on AWS, so it stops billing" \
    "down --yes" media-playback-stop
  if [ -n "$desktop" ]; then
    for file in "$on" "$off"; do
      cp "$apps/$file" "$desktop/$file"
      # GNOME runs a launcher on the desktop only once it is marked trusted.
      gio set "$desktop/$file" metadata::trusted true 2>/dev/null || true
    done
  fi
  echo "Made two shortcuts${profile:+ for profile $profile}, in the app list (press Super, type SysHealth)${desktop:+ and on the desktop}:"
  echo "  SysHealth demo on    builds the fleet and opens the dashboard, signed in (7 to 8 minutes)"
  echo "  SysHealth demo off   destroys it, so it stops billing"
  echo "Neither asks before it acts. ./aws.sh shortcuts --remove takes them away."
}

launcher() {   # file, name, comment, aws.sh arguments, icon
  cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=bash -c "'$SELF' $4; echo; read -r -p 'Press Enter to close this window. ' _"
Icon=$5
Terminal=true
Categories=Utility;
EOF
  chmod +x "$1"
}

cmd_test() {
  load_settings
  [ -n "$S_ACCOUNT" ] || die "nothing is up. Build it first: ./aws.sh up --profile NAME"
  [ $# -gt 0 ] || die "say what load to run, e.g. ./aws.sh test --need 1600 --ramp 200:3200 (see: python3 cli.py run --help)"
  local url args=("$@")
  url=$(tf output -raw dashboard_url)
  SYSHEALTH_TOKEN=$(tf output -raw token)
  export SYSHEALTH_TOKEN
  case " $* " in
    *" --save "*) ;;
    *) args+=(--save "$SYSHEALTH_ROOT/rightsize-$(date +%Y-%m-%d-%H%M).json") ;;
  esac
  set +e
  python3 "$SYSHEALTH_ROOT/cli.py" --server "$url" run "${args[@]}"
  local status=$?
  set -e
  if [ "$status" = 1 ]; then
    echo "(If the dashboard could not be reached and this machine's address changed, ./aws.sh up moves the firewall to it.)" >&2
  fi
  return "$status"
}

cmd_ssh() {
  load_settings
  [ -n "$S_ACCOUNT" ] || die "nothing is up."
  local name=${1:-dashboard}
  [ $# -gt 0 ] && shift
  local ip
  ip=$(instances_json | python3 -c '
import json, sys
machines = json.load(sys.stdin)
name = sys.argv[1]
if name not in machines:
    sys.exit("no machine called %s; there are: %s" % (name, ", ".join(machines)))
print(machines[name]["public_ip"])
' "$name") || exit 1
  ssh_opts
  exec ssh "${SSH_OPTS[@]}" "ubuntu@$ip" "$@"
}

# The code on the fleet is a clone made at boot, in /opt/syshealth. This
# overwrites its files with this working tree's and restarts the services; the
# token and the dashboard's address are in their systemd units, so they stay.
cmd_deploy() {
  local which=${1:-all}
  case "$which" in
    all|agents|server) ;;
    *) die "deploy takes agents, server, or nothing for both" ;;
  esac
  need_tools terraform python3 ssh git
  load_settings
  [ -n "$S_ACCOUNT" ] || die "nothing is up. Build it first: ./aws.sh up --profile NAME"
  local bundle machines name ip unit failed=""
  bundle=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$bundle'" EXIT
  pack_tree > "$bundle"
  machines=$(instances_json | python3 -c '
import json, sys
for name, m in json.load(sys.stdin).items():
    print(name, m["public_ip"])
')
  [ -n "$machines" ] || die "the fleet has no machines yet. ./aws.sh up finishes it."
  ssh_opts
  if [ "$which" != agents ]; then
    echo "Restarting the dashboard empties its graphs: history is kept in memory only."
  fi
  # Dashboard first, so the agents come back to a server that knows what they send.
  while read -r name ip; do
    if [ "$name" = dashboard ]; then
      unit=syshealth-server
      [ "$which" = agents ] && continue
    else
      unit=syshealth
      [ "$which" = server ] && continue
    fi
    if ssh "${SSH_OPTS[@]}" -o ConnectTimeout=10 "ubuntu@$ip" \
         "sudo tar -xzf - --no-same-owner -C /opt/syshealth && sudo systemctl restart $unit &&
          sleep 3 && systemctl is-active --quiet $unit" < "$bundle"; then
      echo "  $name: updated and running"
    else
      echo "  $name: FAILED. Look with: ./aws.sh ssh $name sudo journalctl -u $unit -n 30"
      failed="$failed $name"
    fi
  done <<< "$machines"
  [ -z "$failed" ] || die "not deployed to:$failed"
  python3 "$SYSHEALTH_ROOT/cli.py" --server "$(tf output -raw dashboard_url)" status || true
}

YES=0
NO_OPEN=0
command=${1:-}
[ $# -gt 0 ] && shift
case "$command" in
  up) cmd_up "$@" ;;
  down) cmd_down "$@" ;;
  status) cmd_status "$@" ;;
  open) cmd_open "$@" ;;
  shortcuts) cmd_shortcuts "$@" ;;
  test) cmd_test "$@" ;;
  ssh) cmd_ssh "$@" ;;
  deploy) cmd_deploy "$@" ;;
  -h|--help|help) usage ;;
  "") usage; exit 1 ;;
  *) die "no command '$command'. Use up, down, status, test, ssh or deploy (--help)." ;;
esac

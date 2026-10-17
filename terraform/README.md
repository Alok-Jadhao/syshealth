# Terraform — dashboard plus four t3 sizes

One `terraform apply` brings up the whole thing: a dashboard server and
`t3.micro`, `t3.small`, `t3.medium`, `t3.large`, each running the same agent and
reporting in. Nothing to configure on the instances by hand.

Defaults to `ap-south-1` (Mumbai). Override with `region` in `terraform.tfvars`,
remembering that a key pair only exists in the region it was created in.

## What you need

- An EC2 key pair **in the target region**. Key pairs are per region, so a name
  that works in one is absent in another; the config looks yours up and fails at
  `plan` if it is missing, rather than part way through `apply`.
- AWS credentials with EC2 permissions (`AmazonEC2FullAccess` covers it).
  Terraform reads them the usual ways — `AWS_PROFILE`, env vars, or
  `~/.aws/credentials`.

That is all. The agents find the server over the VPC's private network, so
there is no address to write down.

## Run it

```bash
aws configure                     # if you have not already
cd terraform

# -4 matters: many ISPs answer with an IPv6 address by default, and the
# security group rules are IPv4.
cat > terraform.tfvars <<EOF
key_name = "my-keypair"
ssh_cidr = "$(curl -4 -s ifconfig.me)/32"
EOF

cat terraform.tfvars              # sanity-check: dots, not colons
terraform init
terraform plan                    # read it: this creates five billable instances
terraform apply
```

Then open the `dashboard_url` from the output. Two to three minutes for package
install, a 60-second idle calibration per agent, and the first pushes. The
**Instance** toggle appears once two agents report.

To watch them arrive without refreshing the page, and to be told which sizes are
still missing:

```bash
./check.sh
```

Open **Load test**, enter the memory your app needs, and start it. The first
Start asks for the fleet's token, which Terraform made:

```bash
terraform output -raw token
```

A peak test holds one level; a variable test ramps, waves or bursts between two. The four
are asked at the same moment, in parallel, because staggered starts would pull
the curves apart for a reason that has nothing to do with instance size. When it
finishes, the panel says which size to choose and what it costs a month.

## Or all of it in one command

`rightsize.sh` builds the fleet, waits for every size to report, runs the test,
prints the verdict, saves the full report to `../rightsize-<date>.json`, and
destroys the fleet, including when the test fails or you press Ctrl-C:

```bash
./rightsize.sh --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
./rightsize.sh --peak 1600 --for 120 --types "t3.small t3a.medium m7i.large"
```

It needs the same credentials and `terraform.tfvars` (`key_name`) as above, and
hands the fleet's token to `cli.py` itself. It says what it will create and roughly what that costs an hour, and asks before
going ahead (`--yes` skips the question). It works in its own Terraform
workspace, `rightsize`, with resource names starting `syshealth-rs`, so it never
touches a fleet you built with `terraform apply`. It opens the security group to
this machine's current address only. `--keep` leaves the fleet up afterwards.
The exit status is `cli.py`'s: 0 when a size was recommended, 2 when none fits,
3 when `--expect TYPE` named another size.

## Costs

Five on-demand instances bill by the second and **keep billing until you destroy
them**. Order of magnitude in a cheaper region: roughly US$0.20 an hour for the
set, so about $5 a day or $15 for a three-day run — check the [EC2 pricing
page](https://aws.amazon.com/ec2/pricing/on-demand/) for your region rather than
trusting that number.

```bash
terraform destroy
```

## Design notes

**Ubuntu 24.04, not Amazon Linux.** The agent's only real input is
`/proc/pressure/memory`. Ubuntu compiles PSI in and enables it by default; on
some kernels the file is absent unless you boot with `psi=1`, and
`collector.py` returns `0.0` when it cannot read it — which would give you a
dashboard of flat zeroes with nothing to explain it. The bootstrap script checks
for the file and fails loudly instead.

**Security groups reference each other, not addresses.** The agents accept
traffic on their stress control port only from the dashboard's security group,
and the dashboard accepts metrics only from the agents' group. No IP addresses
are written down, so nothing drifts.

**A token per fleet.** `random_password.token` is handed to the server and
every agent (`SYSHEALTH_TOKEN` in their systemd units). Agents send it with each
push; starting or stopping a test needs it, from the dashboard, `cli.py` or the
MCP server; and each agent's control port refuses anything without it. It
lives in the Terraform state, which is gitignored, and in the instances' user
data, readable from the instance itself and by anyone in the AWS account
allowed to read instance attributes.

**`ssh_cidr` also gates the dashboard UI, and rejects `0.0.0.0/0`.** The token
guards starting a test, but anyone who can reach port 5000 can still read
everything the fleet reports. One address only.

**A 512 MiB swapfile on the agents, by default.** With no swap, a `stress` run
on a `t3.micro` reaches the OOM killer almost at once: one spike and a dead
process. Swap gives the kernel something to reclaim, and reclaim stalls are
exactly what PSI measures. Set `swap_mb = 0` to turn it off.

**The agent is protected from the OOM killer** (`OOMScoreAdjust=-500`). The
thing watching the box should outlive the load it was asked to generate.

**IMDSv2 is required.** `instance.py` reads the instance type from the metadata
service, which is how the dashboard knows a box is a `t3.micro` — hostname alone
cannot tell you that. Reading your own metadata needs no IAM role and no
credentials, so no instance profile is created.

**Thresholds are absolute, the same on every size.** PSI is already a share of
wall-clock time, so 10% means the same on a `t3.micro` as on a `t3.large`. States
are fixed bands, not multiples of an idle baseline: a ratio against a near-zero
floor turned 0.1% into CRITICAL. The sources are in
[`docs/thresholds.md`](../docs/thresholds.md). Each agent still calibrates its
idle reading at boot (`calibrate`), but only as a reference. Nothing depends on
it, so `recalibrate.sh` is optional now.

## Known limit: history is in memory

`server.py` keeps samples in a bounded `deque` — one hour per instance — and
loses everything when the process restarts. That is fine for spot-checking a
load test. It is not a record you can scroll back through after two days, and
`terraform apply` replacing the server wipes it. Persisting to disk or a real
time-series store would be a separate change.

## Troubleshooting

**`ssh_cidr must be an IPv4 CIDR`** — `curl ifconfig.me` gave you an IPv6
address, and the `/32` went on the end of that. Instances in the default VPC get
IPv4 addresses, so the rules have to be IPv4:

```bash
curl -4 -s ifconfig.me; echo      # four dotted numbers, no colons
```

If `-4` returns nothing your connection is IPv6-only, and reaching an IPv4-only
instance needs a change beyond this config — enabling IPv6 on the VPC, subnet and
instances, or connecting from somewhere with IPv4.

Your address also changes when your network does, at which point the dashboard
stops loading. Rewrite `terraform.tfvars` and `apply` again; only the security
group rules change, and the instances are left alone.

**`InsufficientInstanceCapacity`** — AWS has no capacity for that instance type
in that availability zone right now. Nothing in this config causes it and
retrying the same zone will not help. The error is retryable, so left to itself
an `apply` hangs at "Still creating…" rather than failing; `create_timeout`
bounds that.

Instances are already spread across the default VPC's subnets, so one short zone
does not stop the fleet. If a whole region is short, move:

```bash
terraform apply -var='region=eu-west-1'    # create a key pair there first
```

To check a region has room before spending an `apply` on it, launch one
throwaway instance and terminate it:

```bash
aws ec2 run-instances --region <region> --instance-type t3.micro --count 1 \
  --image-id $(aws ec2 describe-images --region <region> --owners 099720109477 \
    --filters 'Name=name,Values=ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text) \
  --query 'Instances[].InstanceId' --output text
```

**An instance that never appears.** Start with `./check.sh`, which names the
missing sizes. Then ask that box directly:

```bash
ssh ubuntu@<public-ip> 'sudo tail -50 /var/log/syshealth-bootstrap.log'
ssh ubuntu@<public-ip> 'sudo journalctl -u syshealth -n 50'
```

An agent that is running but cannot reach the dashboard says so in its own
journal — `push to http://... failed (1 in a row)`, repeated every five minutes
until it recovers. Without that line the agent is not the problem; check that
`syshealth-server` is up on the dashboard box.

The dashboard itself:

```bash
ssh ubuntu@<dashboard-ip> 'sudo journalctl -u syshealth-server -n 50'
```

Reporting but flat at `0.00`: that is a genuinely idle box. Press **Stress
all**, or `stress --vm 2 --vm-bytes 800M --timeout 60s` over SSH.

Dashboard loads but the page is empty: check `curl localhost:5000/series` on the
server. If that 404s, the checked-out branch predates the graph.

# Every command

Each command SysHealth has, what it needs before you run it, and what it does.
Run them from the repository's top folder unless it says otherwise. New here?
Start with the README's [Setup from zero](../README.md#setup-from-zero).

| I want to... | Command |
|---|---|
| See the dashboard on my laptop, no AWS | [`npm run dev`](#npm-run-dev) |
| Build the test fleet on AWS, and take it down | [`./aws.sh up`](#awssh-up) / [`./aws.sh down`](#awssh-down) |
| Get the token, and the code for an audience, from the terminal | [`./aws.sh token`](#awssh-token) |
| Show it live at my own address, over HTTPS | [`./aws.sh domain`](#awssh-domain) |
| Find which size to buy | [`./aws.sh test`](#awssh-test), or [`python3 cli.py run`](#clipy-run) |
| Do the whole experiment in one go, and pay only while it runs | [`terraform/rightsize.sh`](#terraformrightsizesh) |
| Run it on machines I set up myself | [`server.py`](#serverpy), [`syshealth.py`](#syshealthpy), [`deploy.sh`](#deploysh) |
| Fail a CI build when my app outgrows its size | [`cli.py run --expect`](#clipy-run) |
| Let an AI agent run the test | [`mcp_server.py`](#mcp_serverpy) |
| Run the tests | [`npm test`](#npm-test) |

---

## On your laptop

### `npm run dev`

The dashboard with a simulated five-size fleet, at <http://127.0.0.1:5000>.
Nothing to configure, no AWS, no token.

- **Needs:** Python 3.10+ with Flask and requests (README, [Setup from
  zero](../README.md#setup-from-zero)). `npm` is only a familiar shortcut:
  `python3 dev.py` is the same thing, with no Node needed.
- **Does:** fakes only the pressure sensor. Everything after it is the real code,
  so the states, the load-test plans and the verdict come out as they would on
  AWS. Every page carries a DEMO banner.
- **Stop it:** Ctrl-C. Port 5000 must be free.

### `npm test`

The unit tests, the same ones CI runs: `python3 -m unittest discover -s tests`.
One test is skipped when waitress is not installed.

### Other npm shortcuts

| Shortcut | Runs | Notes |
|---|---|---|
| `npm start` | `python3 server.py` | The real server, [below](#serverpy) |
| `npm run agent` | `python3 syshealth.py` | An agent on this machine, sending to `http://127.0.0.1:5000/metrics` unless `SYSHEALTH_SERVER_URL` says otherwise |
| `npm run calibrate` | `python3 syshealth.py calibrate` | 60 s idle measurement, [below](#syshealthpy) |
| `npm run report` | `python3 cli.py report` | The verdict for the last test, [below](#clipy-report) |
| `npm run mcp` | `python3 mcp_server.py` | The MCP server, [below](#mcp_serverpy) |

---

## On AWS: `aws.sh`

One script builds the whole test fleet (a dashboard and one machine per size)
and takes it down again. It uses Terraform in a workspace of its own, so it
never touches anything else in your account.

**Needs, for every subcommand:** an AWS account, AWS CLI v2 with credentials
(`aws configure --profile NAME`), Terraform 1.3 or newer, Python 3, curl and
ssh. How to install and set them up: README, [Setup from
zero](../README.md#setup-from-zero). Linux; tested on Ubuntu.

### `./aws.sh up`

```bash
./aws.sh up --profile NAME
```

Builds the fleet, waits until every size is reporting, and opens the dashboard
in your browser, already signed in.

| Option | Meaning | Default |
|---|---|---|
| `--profile NAME` | The AWS CLI profile to use | `$AWS_PROFILE`, else your default keys |
| `--region NAME` | Where to build | `ap-south-1` (Mumbai), or `region` in `terraform/terraform.tfvars` |
| `--types "A B C"` | The sizes to compare. x86 only (the image is amd64) | `"t3.micro t3.small t3.medium t3.large t3.xlarge"` |
| `--yes` | Do not ask before building | asks |
| `--no-open` | Do not open the browser at the end | opens it |

What happens, in order:

1. It names the account and region, says what the fleet costs an hour (about
   $0.37 for the default six machines in Mumbai), and asks before going on.
2. It makes an SSH key at `~/.ssh/syshealth_ed25519` if there is none, and a
   firewall that lets in only this machine's public IPv4 address.
3. Terraform builds the machines (about a minute). Each one installs SysHealth
   from GitHub and starts reporting. With an address from
   [`./aws.sh domain`](#awssh-domain), it also builds a load balancer for
   `https://NAME` on that name's fixed IP address; the plan fails if the
   certificate is not issued yet, so `up` checks that first.
4. It waits until every size reports, four to eight minutes in all, up to 15
   minutes at most, asking the dashboard's own port. With an address, it then
   checks `https://NAME` answers.
5. It prints the dashboard's address, the token and the watch-only code, and
   opens `https://NAME/#token=...` (or `http://DASHBOARD:5000/#token=...`). The
   page keeps the token and takes it out of the address bar, so it opens and
   **Start** works without pasting anything.

Running `up` again while the fleet is up changes it in place: a new address for
the firewall after you change network, or other sizes with `--types`. To move it
to another region, `down` first.

### `./aws.sh down`

```bash
./aws.sh down           # asks first
./aws.sh down --yes     # does not
```

Destroys everything `up` made: the machines, their firewall, the key pair, and
the load balancer if there is one. An address from `./aws.sh domain` stays, for
the next `up`.
Then it asks EC2 itself whether any of the machines is still running, and says
so if one is. It refuses to run with credentials for a different AWS account
from the one `up` built in, so a changed profile cannot leave a fleet running
unseen.

### `./aws.sh open`

Opens the dashboard in your browser, signed in. Use it after closing the tab,
or when the page asks for a code, or **Start** says the token is wrong. With no
desktop browser it prints the link instead; the link contains the token, so
treat it like a password.

### `./aws.sh token`

```bash
./aws.sh token          # both codes, and a link with each
./aws.sh token --raw    # the token alone, for a script
```

Prints the two codes the dashboard asks for, read from the fleet's Terraform
state:

- **Server token:** opens everything, and alone starts and stops tests. Yours.
- **Watch-only code:** ten characters; opens everything and changes nothing.
  The one to give an audience.

With each, a link that carries it (`.../#token=...`), and with an address from
[`./aws.sh domain`](#awssh-domain), a reminder that people can just open the
address and type the code. Both are new on every `up`. Without `aws.sh`, from
`terraform/`:

```bash
TF_WORKSPACE=fleet terraform output -raw token        # a fleet from ./aws.sh up
TF_WORKSPACE=fleet terraform output -raw view_token
terraform output -raw token                            # a fleet you applied by hand
```

### `./aws.sh status`

What is running, its addresses, how long it has been up, what it costs an hour
and has cost so far (list prices), and each size's state. "Nothing is up" means
nothing of the fleet exists. With an address, it prints both: `https://NAME`,
and the dashboard's own port, which works from this machine whatever DNS says.

### `./aws.sh test`

```bash
./aws.sh test --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
./aws.sh test --users 200:650 --app-mb 300 --mb-per-user 2
```

Runs a load test on the fleet and prints which size to buy. It takes the same
options as [`cli.py run`](#clipy-run) and fills in the dashboard's address and
token for you. The report is saved as `rightsize-<date>-<time>.json` in the top
folder unless you give `--save`.

### `./aws.sh ssh`

```bash
./aws.sh ssh t3.small                     # a shell on one size
./aws.sh ssh dashboard                    # or on the dashboard machine (the default)
./aws.sh ssh t3.micro sudo journalctl -u syshealth -n 30   # run one command
```

### `./aws.sh deploy`

```bash
./aws.sh deploy           # the dashboard and every size
./aws.sh deploy agents    # the sizes only
./aws.sh deploy server    # the dashboard only
```

Sends your working tree to the fleet and restarts it, without rebuilding
anything: the files git tracks, uncommitted changes included (`git add` a new
file first). Restarting the dashboard empties its graphs, since history is kept
in memory. Without `deploy`, the fleet runs what is on GitHub.

### `./aws.sh domain`

```bash
./aws.sh domain syshealth.example.com --profile NAME   # once
./aws.sh domain                                        # where it is; carries on if it stopped
./aws.sh domain --remove                               # take it away (fleet down first)
```

Gives the dashboard an address of yours, over HTTPS, on every `up` from then
on: for a live demo, where the audience opens it on their own phones.

- **Needs:** a domain you own and can add DNS records to, and a name under it
  for SysHealth alone (`syshealth.example.com`, not `example.com`). `dig` (in
  `dnsutils`) lets it see when the records are in; without it, it just waits
  for the certificate.
- **Costs:** $3.65 a month for the fixed IP address, until `--remove` (AWS
  bills every public IPv4 address $0.005 an hour). The certificate is free.
  Each fleet's load balancer is about $0.03 an hour more while the fleet is up.

| Option | Meaning | Default |
|---|---|---|
| `--profile NAME` | The AWS CLI profile | the fleet's, else `$AWS_PROFILE` |
| `--region NAME` | Where the address and the certificate are made: it must be the fleet's | the fleet's, else `ap-south-1` |
| `--yes` | Do not ask first | asks |
| `--remove` | Give the address back and delete the certificate | |

What happens, in order:

1. It names the account and region and the cost, and asks.
2. Terraform (`terraform/domain/`, state in that folder) reserves the fixed
   IP address and asks for the certificate. It prints two records to add where
   your domain's DNS is kept: an `A` record `syshealth` with that address, and
   the certificate's `CNAME`. Nothing else there changes: a website on the
   domain stays as it is. Plain `A` and `CNAME` records work with any DNS
   host, Hostinger included, which cannot hand a subdomain to other name
   servers.
3. It asks your domain's own name servers every 20 seconds until both are
   there, for up to an hour. Ctrl-C is safe; run it again to carry on.
4. AWS checks the `CNAME` and issues the certificate, usually within ten
   minutes. Leave the `CNAME` in place: the certificate renews itself through
   it.

`up` then builds behind it. After `--remove`, delete both records at your DNS
host straight away: AWS may give the address to someone else, and the `A`
record would send your name to them.

### `./aws.sh shortcuts`

```bash
./aws.sh shortcuts --profile NAME     # make two icons
./aws.sh shortcuts --remove           # take them away
```

Linux desktops only. Makes **SysHealth demo on** and **SysHealth demo off**, on
the desktop and in the app list (press Super and type SysHealth).
Double-clicking one opens a terminal that runs `up --yes` or `down --yes`.
**Neither asks before it acts.** The terminal stays open, with the cost and any
error, until you press Enter.

---

## The whole experiment in one command

### `terraform/rightsize.sh`

```bash
cd terraform
./rightsize.sh --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
./rightsize.sh --peak 1600 --for 120 --types "t3.small t3a.medium m7i.large"
```

Builds a fleet of the sizes you name, waits for it, runs the test, saves the
report and destroys the fleet again, also when the test fails or you press
Ctrl-C. You pay only while it runs.

- **Needs:** the same as `aws.sh`. It picks the AWS profile from `AWS_PROFILE`.
- **Its own options:** `--types "A B C"` (default: t3.micro, small, medium,
  large), `--keep` to leave the fleet up afterwards (it keeps billing), `--yes`
  to skip the question. Every other option goes to [`cli.py run`](#clipy-run).
- **Result:** `rightsize-<date>.json` in the top folder; the exit status is
  `cli.py`'s.
- It has its own Terraform workspace and names, so it never touches a fleet
  from `aws.sh` or one built by hand.

### The plain-Terraform helpers

For a fleet built by hand with `terraform apply` in `terraform/` (see
[`terraform/README.md`](../terraform/README.md)), run from that folder:

| Script | Does |
|---|---|
| `./check.sh` | Which sizes are reporting, and for the ones that are not, the command that says why. Changes nothing |
| `./relaunch.sh` | Brings the fleet back up after checking the things that have broken it before (an old IP in `ssh_cidr`, a branch that was never pushed) |
| `./recalibrate.sh` | Measures every agent's idle baseline again. Optional: states do not depend on it |

For a fleet from `aws.sh`, use `./aws.sh status` instead.

---

## Talking to any SysHealth server: `cli.py`

`cli.py` works with the AWS fleet, your own servers or the demo, using only
Python's standard library.

- **Which server:** `--server URL` before the subcommand, else `$SYSHEALTH_URL`,
  else `http://127.0.0.1:5000` (the demo).
- **Token:** `$SYSHEALTH_TOKEN`, an environment variable so it stays out of your
  shell history. A server with a token answers nothing without it; to read
  only, the watch-only code works too.

### `cli.py status`

Which machines are reporting, the last minute's waiting for memory, memory in
use and the state of each.

### `cli.py wait`

```bash
python3 cli.py wait --count 4 --timeout 900
```

Waits until `--count` machines are reporting, up to `--timeout` seconds (15
minutes by default). `aws.sh up` uses it.

### `cli.py run`

```bash
python3 cli.py run --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
python3 cli.py run --peak 2250 --for 360 --workers 12 --background --swap 2048
python3 cli.py run --peak 1600 --for 120 --expect t3.medium --save report.json
python3 cli.py run --users 200:650 --app-mb 300 --mb-per-user 2
```

Starts the same load on every machine at once, shows its progress, then prints
the verdict. Ctrl-C stops the test everywhere.

**The load, exactly one of:**

| Option | Shape | Uses |
|---|---|---|
| `--peak MB` | Hold MB the whole time | `--for` seconds (default 60) |
| `--ramp LOW:HIGH` | Climb from LOW to HIGH | `--steps` levels (default 6), `--step-sec` each (default 60) |
| `--wave LOW:HIGH` | Climb, then come back down | the same |
| `--bursts LOW:HIGH` | LOW, then HIGH, `--steps` times over | the same |
| `--users NORMAL:BUSIEST` | Worked out from users at the same moment, as the dashboard's **Plan it from your users** does: a ramp from a normal day's memory to twice the busiest's, judged against the busiest. It prints the sums first | `--app-mb` and `--mb-per-user` (both needed), `--steps`, `--step-sec`; it sets `--need` itself |

With `--users`: `--app-mb MB` is your app's memory with no users, and
`--mb-per-user MB` what each user at the same moment adds (fractions are fine).
Measure it once: (memory at a busy moment − memory with no users) ÷ users then.

**Your app, and the answer:**

| Option | Meaning |
|---|---|
| `--need MB` | Your app's memory at its busiest. Each size is judged against it |
| `--margin %` | How much spare to keep above that (default 25) |
| `--cpu VCPUS` | Your app's average CPU (0.5 = half a vCPU). Prices in the CPU credits a burstable size would burn |
| `--expect TYPE` | Exit 3 unless TYPE is recommended (for CI) |
| `--save FILE` | Also write the full report to FILE |
| `--json` | Print the full report as JSON |
| `--dry-run` | Check the test and say what it would do; run nothing |

**Making it behave like a real server:**

| Option | Meaning |
|---|---|
| `--workers N` | Memory-filling processes, sharing the load evenly (default 2, at most 16). More of them compete harder for memory |
| `--background` | Keeps every vCPU busy at the lowest priority, like a server's other work. Without it a size goes from HEALTHY straight to CRITICAL |
| `--swap MB` | Extra swap on every machine for the test, removed after it (up to 4096). A size that cannot hold the load slows down instead of being killed |

**Exit status:** 0 a size was recommended; 2 none fits, or the test never
reached `--need`; 3 `--expect` named another size; 1 any error (for example,
the server could not be reached); 130 stopped with Ctrl-C.

Limits, checked before anything runs: up to 16 GB in all, steps of 10 to 1800
seconds, an hour in total.

### `cli.py report`

```bash
python3 cli.py report --need 1200 --margin 30 --cpu 0.5
```

The verdict for the last test again, judged against other numbers, with no new
test. `--test-id` picks an earlier test; `--json`, `--save` and `--expect` work
as for `run`.

---

## On machines you set up yourself

The README's [Quick Start (manual)](../README.md#quick-start-manual) walks
through it from the start.

### `server.py`

```bash
export SYSHEALTH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
python3 server.py
```

The central server and the dashboard, on port 5000 on every interface.

- **Needs:** Flask and requests. With waitress installed it serves with that;
  without it, Flask's development server, and it says so.
- **Settings:** `SYSHEALTH_TOKEN` (strongly advised: without it anyone who can
  reach port 5000 can see everything and start a test), `SYSHEALTH_VIEW_TOKEN`
  for a watch-only code, `SYSHEALTH_PRICES_FILE` and `SYSHEALTH_PRICE_REGION`
  ([settings](../README.md#settings)).
- History is kept in memory, so a restart empties the graphs.

### `syshealth.py`

```bash
export SYSHEALTH_SERVER_URL=http://SERVER_IP:5000/metrics
export SYSHEALTH_TOKEN=<the server's token>
python3 syshealth.py calibrate     # once: 60 s idle measurement
python3 syshealth.py               # the agent
```

The agent, one on each machine you want to compare. Every 5 seconds it reads
the kernel's memory pressure and sends it to the server. It also listens on
port 5001 for the server to start and stop load tests.

- **Needs:** Linux 4.20 or newer (memory pressure appeared there), Python 3,
  requests, and the `stress` package for load tests.
- **Settings:** `SYSHEALTH_SERVER_URL` is required (there is no built-in
  address); `SYSHEALTH_TOKEN`; `SYSHEALTH_APP` to also watch your app's own
  cgroup; `SYSHEALTH_CONTROL=off` on a production machine so it never takes a
  load test; `SYSHEALTH_INSTANCE_TYPE` and `SYSHEALTH_INSTANCE_REGION` off EC2
  ([settings](../README.md#settings)).
- **Firewall:** the server must reach the agent's port 5001, and the agent the
  server's port 5000.

### `deploy.sh`

```bash
SERVER=1.2.3.4 AGENTS="5.6.7.8 9.10.11.12" SYSHEALTH_TOKEN=... ./deploy.sh          # both
SERVER=1.2.3.4 AGENTS="5.6.7.8" SYSHEALTH_TOKEN=... ./deploy.sh agents             # or agents / server only
```

Sends the files git tracks (uncommitted changes included) to machines set up
by hand into `~/syshealth`, and restarts them with the token, pointing the
agents at `http://$SERVER:5000/metrics`. `SYSHEALTH_VIEW_TOKEN`, if set, goes to
the server as its watch-only code. `KEY` names the SSH key (default
`syshealth-key.pem`). For a fleet from `aws.sh`, use `./aws.sh deploy`.

---

## For AI agents

### `mcp_server.py`

```bash
python3 mcp_server.py --server http://SERVER_IP:5000
```

SysHealth as tools for an AI agent, over the Model Context Protocol on stdio.
The agent's client starts it; you do not run it yourself. The server is
`--server`, else `$SYSHEALTH_URL`, else the demo; the token is
`$SYSHEALTH_TOKEN`. The client entry and the five tools are in the README's
[Ask an AI agent (MCP)](../README.md#ask-an-ai-agent-mcp).

---

## Maintenance

| Command | Does | Needs |
|---|---|---|
| `python3 tools/update_prices.py [REGION ...]` | Refreshes `data/prices.json` from AWS's public price list | Internet; no AWS account |
| `python3 docs/make_result_figure.py [REPORT.json] [OUT.svg]` | Draws the README's picture from a saved report | Python 3 |
| `python3 docs/make_runbook.py` | Rebuilds `docs/syshealth-demo-runbook.pdf`, the printed demo script | reportlab (`pip install reportlab` in a virtualenv) |

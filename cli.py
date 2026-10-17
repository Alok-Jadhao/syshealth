#!/usr/bin/env python3
"""SysHealth from the command line: run a load test, get the verdict.

    python3 cli.py status
    python3 cli.py run --need 1600 --ramp 200:3200 --steps 6 --step-sec 60
    python3 cli.py run --peak 1600 --for 120 --expect t3.medium
    python3 cli.py report --need 1200 --margin 30 --cpu 0.5
    python3 cli.py wait --count 4

The server is $SYSHEALTH_URL, or --server, or http://127.0.0.1:5000 (which is
also where `npm run dev` puts the demo). A server with a token wants it in
$SYSHEALTH_TOKEN: an environment variable, so it stays out of shell history.
Only the standard library is used, so this runs anywhere Python 3 does.

`run` exits 0 when a size was recommended, 2 when none fits or the test fell
short of the app's need, 3 when --expect named a different size, and 1 on any
error. That makes it a CI gate: fail the build when the app's memory has
outgrown the size it runs on.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = os.environ.get("SYSHEALTH_URL") or "http://127.0.0.1:5000"
TOKEN = os.environ.get("SYSHEALTH_TOKEN", "").strip() or None
POLL_SEC = 10
SETTLE_SEC = 15     # the agents' last samples are a push or two behind
EXIT_OK, EXIT_ERROR, EXIT_NO_FIT, EXIT_UNEXPECTED = 0, 1, 2, 3


class CliError(Exception):
    pass


def call(server, path, body=None, method=None):
    url = server.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    request = urllib.request.Request(
        url, data=data, method=method or ("POST" if body is not None else "GET"), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("error")
        except ValueError:
            detail = None
        finally:
            exc.close()
        if exc.code == 401:
            detail = ("the server rejected $SYSHEALTH_TOKEN" if TOKEN else
                      "the server needs its token: set SYSHEALTH_TOKEN")
        raise CliError(detail or "%s answered %d" % (url, exc.code))
    except (urllib.error.URLError, OSError) as exc:
        raise CliError("Cannot reach %s: %s" % (server, getattr(exc, "reason", exc)))


def mb(value):
    if value is None:
        return "-"
    if value >= 1024:
        gb = value / 1024.0
        return ("%d GB" % round(gb)) if abs(gb - round(gb)) < 0.05 else "%.1f GB" % gb
    return "%d MB" % value


def capacity_text(cap):
    if not cap:
        return "-"
    if cap["kind"] == "at_least":
        return mb(cap["mb"]) + "+"
    if cap["kind"] == "below":
        return "under " + mb(cap["mb"])
    return "~" + mb(cap["mb"])


def spare_text(row):
    fit, pct = row.get("fit"), row.get("spare_pct")
    if fit == "fits":
        return ("at least " if row["capacity"]["kind"] == "at_least" else "") + "+%d%%" % pct
    return {"tight": "+%s%% (tight)" % pct, "unmeasured": "held it; room not measured",
            "too_small": "too small"}.get(fit, "not reached")


def cpu_text(cpu):
    if not cpu or cpu.get("fits") is None:
        return "-"
    if cpu["fits"] is False:
        return "too little"
    return "+$%.2f credits" % cpu["extra_usd_month"] if cpu["extra_usd_month"] else "ok"


def month_text(row):
    if not row.get("price"):
        return "-"
    return "$%.2f" % (row["price"]["usd_month"] + ((row.get("cpu") or {}).get("extra_usd_month") or 0))


def print_report(report, out=sys.stdout):
    if not report.get("test_id"):
        print("No load test has been run on this server yet.", file=out)
        return
    if report.get("error"):
        print(report["error"], file=out)
        return
    print("%s  ·  your app: %s%s, keeping %d%% spare" % (
        report["summary"], mb(report.get("need_mb")),
        " and %g vCPU on average" % report["cpu_vcpu"] if report.get("cpu_vcpu") else "",
        report.get("margin_pct", 0)), file=out)
    print(file=out)
    rec = report.get("recommendation") or {}
    cpu = report.get("cpu_vcpu")
    rows = [("", "SIZE", "HOLDS UP TO", "SPARE") + (("CPU",) if cpu else ()) + ("PER MONTH", "OUTCOME")]
    for row in report["rows"]:
        rows.append((
            "*" if row["hostname"] == rec.get("pick") else "",
            row["instance_type"] if row["instance_type"] != "unknown" else row["hostname"],
            capacity_text(row.get("capacity")),
            spare_text(row),
        ) + ((cpu_text(row.get("cpu")),) if cpu else ()) + (
            month_text(row),
            row["outcome"]["text"],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip(), file=out)
    print(file=out)
    if rec:
        print(rec.get("headline", ""), file=out)
        if rec.get("detail"):
            print(rec["detail"], file=out)
        for line in rec.get("lines") or []:
            print("  - " + line, file=out)
        if rec.get("usage_rule"):
            print(rec["usage_rule"]["text"], file=out)
    if any(r.get("price") for r in report["rows"]):
        print(file=out)
        print(report["prices"]["note"], file=out)


def spec_from(args):
    """The /run-stress body for the options given."""
    shapes = [s for s in ("peak", "ramp", "wave", "bursts") if getattr(args, s)]
    if len(shapes) != 1:
        raise CliError("Give exactly one of --peak, --ramp, --wave or --bursts.")
    if args.peak:
        spec = {"mode": "peak", "memory_mb": args.peak, "duration_sec": args.duration}
    else:
        pattern = shapes[0]
        try:
            low, high = [int(x) for x in getattr(args, pattern).split(":")]
        except ValueError:
            raise CliError("--%s takes LOW:HIGH in MB, e.g. 200:3200." % pattern)
        spec = {"mode": "variable", "pattern": pattern, "low_mb": low, "high_mb": high,
                "steps": args.steps, "step_sec": args.step_sec}
        if args.need:
            spec["need_mb"] = args.need
    spec["workers"] = args.workers
    return spec


def progress(server):
    """One line on the running test, from the agents' latest samples."""
    series = call(server, "/series?limit=1").get("instances", [])
    for inst in series:
        if inst.get("online") and inst["samples"] and inst["samples"][-1].get("load_test"):
            test = inst["samples"][-1]["load_test"]
            return "step %s of %s, holding %s, %ss left" % (
                test.get("step"), test.get("steps"), mb(test.get("load_mb")), test.get("remaining_sec"))
    return None


def report_query(args, test_id=None):
    params = {}
    if test_id or getattr(args, "test_id", None):
        params["test_id"] = test_id or args.test_id
    if args.need:
        params["need_mb"] = args.need
    if args.margin is not None:
        params["margin"] = args.margin
    if getattr(args, "cpu", None):
        params["cpu"] = args.cpu
    return "/report" + ("?" + urllib.parse.urlencode(params) if params else "")


def verdict_code(report, expect):
    pick = (report.get("recommendation") or {}).get("pick")
    if not pick:
        return EXIT_NO_FIT
    if expect:
        chosen = next(r["instance_type"] for r in report["rows"] if r["hostname"] == pick)
        if chosen != expect:
            print("\nExpected %s, but the recommendation is %s." % (expect, chosen), file=sys.stderr)
            return EXIT_UNEXPECTED
    return EXIT_OK


def finish(report, args):
    if args.save:
        with open(args.save, "w") as f:
            json.dump(report, f, indent=1)
            f.write("\n")
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print_report(report)
        if args.save:
            print("\nSaved the full report to %s" % args.save)
    return verdict_code(report, getattr(args, "expect", None))


def cmd_run(args):
    spec = spec_from(args)
    if args.dry_run:
        # The server's own checks, run here: no server needed.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import loadtest
        try:
            plan = loadtest.build_plan(spec)
        except loadtest.PlanError as exc:
            raise CliError(str(exc))
        print("%s, %d min in all" % (plan["summary"], -(-plan["total_sec"] // 60)))
        return EXIT_OK
    started = call(args.server, "/run-stress", spec)
    test_id = started["test_id"]
    targets = started.get("results") or {}
    log = sys.stderr if args.json else sys.stdout
    print("Started %s on %d instance(s): test %s" % (started["plan"]["summary"], len(targets), test_id),
          file=log)
    for host, result in sorted(targets.items()):
        print("  %-28s %s" % (host, result), file=log)
    if not any(str(r).startswith("started") for r in targets.values()):
        raise CliError("No instance started the test.")

    deadline = time.time() + started["plan"]["total_sec"] + 300
    seen_running = False
    try:
        while True:
            time.sleep(POLL_SEC)
            report = call(args.server, report_query(args, test_id))
            if report.get("running"):
                seen_running = True
                line = progress(args.server)
                if line:
                    print("  ... " + line, file=log)
            elif seen_running or time.time() > deadline:
                break
            if time.time() > deadline:
                raise CliError("The test has not finished well past its planned length.")
    except KeyboardInterrupt:
        print("\nStopping the test on every instance...", file=log)
        call(args.server, "/stop-stress", {})
        return 130
    time.sleep(SETTLE_SEC)
    return finish(call(args.server, report_query(args, test_id)), args)


def cmd_report(args):
    return finish(call(args.server, report_query(args)), args)


def cmd_status(args):
    series = call(args.server, "/series?limit=12").get("instances", [])
    if not series:
        print("No agents have reported to %s yet." % args.server)
        return EXIT_ERROR
    print("%-12s %-7s %8s %8s  %s" % ("SIZE", "ONLINE", "MINUTE", "IN USE", "STATE"))
    for inst in series:
        last = inst["samples"][-1] if inst["samples"] else {}
        used = (last.get("memory") or {}).get("used_pct")
        print("%-12s %-7s %7.2f%% %8s  %s" % (
            inst.get("instance_type") or inst["hostname"], "yes" if inst["online"] else "no",
            float(last.get("avg_psi") or 0), "-" if used is None else "%.0f%%" % used,
            last.get("state", "-")))
    return EXIT_OK


def cmd_wait(args):
    """Until `count` agents are online and have a minute of samples each."""
    deadline = time.time() + args.timeout
    last = None
    while True:
        try:
            series = call(args.server, "/series?limit=12").get("instances", [])
        except CliError as exc:
            series, status = [], str(exc)
        else:
            ready = [i for i in series if i["online"] and len(i["samples"]) >= 12]
            status = "%d of %d instances reporting with a minute of samples" % (len(ready), args.count)
            if len(ready) >= args.count:
                print(status)
                return EXIT_OK
        if status != last:
            print("  ... " + status, flush=True)
            last = status
        if time.time() > deadline:
            raise CliError("Gave up after %ds: %s." % (args.timeout, status))
        time.sleep(POLL_SEC)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help="SysHealth server (default $SYSHEALTH_URL or %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    def verdict_options(p):
        p.add_argument("--need", type=int, help="your app's memory at its busiest, MB")
        p.add_argument("--margin", type=int, help="%% to keep spare above that (default 25)")
        p.add_argument("--cpu", type=float, metavar="VCPUS",
                       help="your app's average CPU in vCPUs (0.5 = half of one); prices in CPU credits")
        p.add_argument("--json", action="store_true", help="print the full report as JSON")
        p.add_argument("--save", metavar="FILE", help="also write the full report to FILE")
        p.add_argument("--expect", metavar="TYPE", help="exit 3 unless TYPE is recommended")

    run = sub.add_parser("run", help="run a load test on every instance, then print the verdict")
    shape = run.add_argument_group("the load (one of)")
    shape.add_argument("--peak", type=int, metavar="MB", help="hold MB for --for seconds")
    shape.add_argument("--ramp", metavar="LOW:HIGH", help="climb from LOW to HIGH MB")
    shape.add_argument("--wave", metavar="LOW:HIGH", help="climb, then come back down")
    shape.add_argument("--bursts", metavar="LOW:HIGH", help="alternate a base load and a spike")
    run.add_argument("--for", dest="duration", type=int, default=60, help="peak: seconds (default 60)")
    run.add_argument("--steps", type=int, default=6, help="variable: levels (default 6)")
    run.add_argument("--step-sec", type=int, default=60, help="variable: seconds each (default 60)")
    run.add_argument("--workers", type=int, default=2, help="stress workers (default 2)")
    run.add_argument("--dry-run", action="store_true", help="check the test and say what it does; run nothing")
    verdict_options(run)
    run.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="the verdict for the last test (or --test-id)")
    rep.add_argument("--test-id")
    verdict_options(rep)
    rep.set_defaults(func=cmd_report)

    sub.add_parser("status", help="which instances are reporting, and their state").set_defaults(func=cmd_status)

    wait = sub.add_parser("wait", help="wait until COUNT instances are reporting")
    wait.add_argument("--count", type=int, required=True)
    wait.add_argument("--timeout", type=int, default=900, help="seconds (default 900)")
    wait.set_defaults(func=cmd_wait)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

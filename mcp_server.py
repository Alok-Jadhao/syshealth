#!/usr/bin/env python3
"""SysHealth for AI agents: an MCP server, so an assistant can ask "is this
machine big enough for my app?" and answer it with an experiment.

A monitor can only report what already happened. Through these tools an
agent (any MCP client) can look at the fleet, plan a load test, run it on
every size at once, and read back which size to buy and what it costs, with
the evidence and the limits of that answer alongside it.

Register it with the client as a stdio server: the command
`python3 /path/to/syshealth/mcp_server.py`, with SYSHEALTH_URL set to
http://<dashboard>:5000 and SYSHEALTH_TOKEN to the fleet's token.

Without SYSHEALTH_URL it talks to http://127.0.0.1:5000, where `npm run dev`
serves the demo fleet. It speaks MCP over stdio (newline-delimited JSON-RPC
2.0) with the standard library only, like cli.py, whose HTTP calls and report
text it shares. Nothing is written to stdout except protocol messages.
"""

import argparse
import io
import json
import sys
import traceback

import cli

SERVER_INFO = {"name": "syshealth", "title": "SysHealth", "version": "0.2.0"}
# Newest first. A client asking for one of these gets it; any other gets the
# newest, and decides for itself whether it can work with that.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
STRUCTURED_FROM = "2025-06-18"   # structuredContent in tool results

INSTRUCTIONS = """\
SysHealth finds the cheapest machine size that runs an app without it slowing
down for lack of memory. It does this by experiment: every machine reporting
to the SysHealth server (one per size) holds the same memory at the same
moment, and Linux's memory pressure (PSI) shows where each size starts to
slow down.

A good order:
1. fleet_status: which sizes are reporting, and, if an app is watched on a
   real machine, its measured peak memory and average CPU. Those are the
   app's need: use them, or ask the person.
2. plan_load_test: check the test and its length before running it. A ramp
   from well below to well past the app's peak shows where each size breaks.
3. start_load_test, only once the person has agreed: it fills memory on
   every machine that accepts tests, for minutes, and some may run out.
4. rightsize_report: poll until running is false, then give the person the
   recommendation, the money, and the limits listed with it.
"""


class ToolError(Exception):
    """A failure to report to the agent as a tool result, not a protocol error."""


def _int(value, name):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ToolError("%s must be a whole number." % name)
    return int(value)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

LOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "shape": {"type": "string", "enum": ["peak", "ramp", "wave", "bursts"],
                  "description": "peak: hold memory_mb for duration_sec. ramp: climb from low_mb "
                                 "to high_mb in steps (best for finding where each size breaks). "
                                 "wave: climb, then come back down. bursts: alternate low and high."},
        "memory_mb": {"type": "integer", "minimum": 1, "description": "peak: memory to hold, MB"},
        "duration_sec": {"type": "integer", "minimum": 10, "description": "peak: seconds (default 60)"},
        "low_mb": {"type": "integer", "minimum": 0, "description": "ramp/wave/bursts: lowest level, MB"},
        "high_mb": {"type": "integer", "minimum": 1, "description": "ramp/wave/bursts: highest level, MB"},
        "steps": {"type": "integer", "minimum": 2, "maximum": 20, "description": "levels (default 6)"},
        "step_sec": {"type": "integer", "minimum": 10, "description": "seconds per level (default 60)"},
        "need_mb": {"type": "integer", "minimum": 1,
                    "description": "the app's memory at its busiest, MB; every size is judged against it"},
        "workers": {"type": "integer", "minimum": 1, "maximum": 16,
                    "description": "stress workers sharing the memory (default 2)"},
        "background_cpu": {"type": "boolean",
                           "description": "keep every vCPU busy at low priority during the test, "
                                          "as a server's other jobs would; lets a size show DEGRADED"},
        "swap_mb": {"type": "integer", "minimum": 0, "maximum": 4096,
                    "description": "extra swap on every size for the test, removed after it"},
    },
    "required": ["shape"],
    "additionalProperties": False,
}


def load_spec(args):
    """The /run-stress body for a tool's arguments. The server checks the rest."""
    spec = _load_shape(args)
    if args.get("background_cpu"):
        spec["background_cpu"] = True
    swap = _int(args.get("swap_mb"), "swap_mb")
    if swap:
        spec["swap_mb"] = swap
    return spec


def _load_shape(args):
    shape = args.get("shape")
    if shape not in ("peak", "ramp", "wave", "bursts"):
        raise ToolError("shape must be one of peak, ramp, wave or bursts.")
    workers = _int(args.get("workers"), "workers") or 2
    if shape == "peak":
        memory = _int(args.get("memory_mb"), "memory_mb")
        if memory is None:
            raise ToolError("A peak test needs memory_mb.")
        return {"mode": "peak", "memory_mb": memory,
                "duration_sec": _int(args.get("duration_sec"), "duration_sec") or 60, "workers": workers}
    low, high = _int(args.get("low_mb"), "low_mb"), _int(args.get("high_mb"), "high_mb")
    if low is None or high is None:
        raise ToolError("A %s test needs low_mb and high_mb." % shape)
    spec = {"mode": "variable", "pattern": shape, "low_mb": low, "high_mb": high,
            "steps": _int(args.get("steps"), "steps") or 6,
            "step_sec": _int(args.get("step_sec"), "step_sec") or 60, "workers": workers}
    need = _int(args.get("need_mb"), "need_mb")
    if need:
        spec["need_mb"] = need
    return spec


class Tools:
    def __init__(self, server_url):
        self.url = server_url

    def call(self, path, body=None):
        try:
            return cli.call(self.url, path, body)
        except cli.CliError as exc:
            raise ToolError(str(exc))

    # -- fleet_status ------------------------------------------------------

    def fleet_status(self, args):
        data = self.call("/summary")
        rows = data.get("instances") or []
        host = args.get("hostname")
        if host:
            rows = [r for r in rows if host in (r["hostname"], r["instance_type"])]
            if not rows:
                raise ToolError("No machine called %s reports to %s." % (host, self.url))
        if not rows:
            return ("No machines report to %s yet. Start agents (or `npm run dev` for the demo)."
                    % self.url, data)

        lines = []
        for r in rows:
            now, mem = r["now"], r["now"].get("memory") or {}
            name = r["instance_type"] if r["instance_type"] != "unknown" else r["hostname"]
            line = "%s (%s): %s" % (name, r["hostname"], (now.get("state") or "no data").lower())
            if not r["online"]:
                line += ", offline for %ds" % r["seconds_since"]
            if now.get("pressure_minute") is not None:
                line += ", waiting for memory %.1f%% of the last minute" % now["pressure_minute"]
            if mem.get("available_mb") is not None:
                line += ", %s of %s available" % (cli.mb(mem["available_mb"]), cli.mb(mem["total_mb"]))
            if not r["accepts_tests"]:
                line += ", watch only (takes no load tests)"
            if now.get("load_test"):
                line += ", load test running"
            lines.append(line)
            if now.get("cause"):
                lines.append("  why: " + now["cause"]["text"])
            if now.get("leak"):
                lines.append("  growing: " + now["leak"]["text"])
            if host and mem:
                lines.append("  memory: programs %s, file cache %s, kernel and other %s, free %s; swap %s "
                             "of %s used" % tuple(cli.mb(mem.get(k)) for k in (
                                 "programs_mb", "cache_mb", "other_mb", "free_mb",
                                 "swap_used_mb", "swap_total_mb")))
            if host and now.get("top"):
                lines.append("  biggest: " + ", ".join("%s %s" % (p["name"], cli.mb(p["rss_mb"]))
                                                       for p in now["top"]))
            if host:
                hour = r["hour"]
                lines.append("  last %s min: worst minute %s%% waiting, states %s" % (
                    _num(hour["minutes"]), _num(hour["worst_minute"]),
                    ", ".join("%s %d" % (k.lower(), v) for k, v in sorted(hour["states"].items()))))
            app = r.get("app")
            if app:
                lines.append("  app %s over the last hour: peak %s (its own memory %s), CPU %s vCPU on "
                             "average, %s at most" % (
                                 app["name"], cli.mb(app["peak_mb"]), cli.mb(app["peak_anon_mb"]),
                                 _num(app["avg_cpu_vcpu"]), _num(app["peak_cpu_vcpu"])))
                verdict = (app.get("now") or {}).get("verdict")
                if verdict:
                    lines.append("  app now: " + verdict["text"])
        if data.get("running_tests"):
            lines.append("Test running: %s." % ", ".join(data["running_tests"]))
        testable = [r for r in rows if r["online"] and r["accepts_tests"]]
        lines.append("%d of %d machine(s) online and accepting load tests." % (len(testable), len(rows)))
        return "\n".join(lines), {"server": self.url, **data, "instances": rows}

    # -- plan / start / stop ----------------------------------------------

    def plan_load_test(self, args):
        spec = load_spec(args)
        plan = self.call("/load-plan", spec)["plan"]
        machines = [r for r in self.call("/summary").get("instances", [])
                    if r["online"] and r["accepts_tests"]]
        minutes = -(-plan["total_sec"] // 60)
        text = ("%s. %d min in all, on %d machine(s): %s. Nothing has been started."
                % (plan["summary"], minutes, len(machines),
                   ", ".join(r["instance_type"] for r in machines) or "none online"))
        if plan.get("need_mb"):
            text += " Each size will be judged against %s." % cli.mb(plan["need_mb"])
        return text, {"plan": plan, "spec": spec, "minutes": minutes,
                      "machines": [r["hostname"] for r in machines]}

    def start_load_test(self, args):
        spec = load_spec(args)
        started = self.call("/run-stress", spec)
        results = started.get("results") or {}
        ok = [h for h, r in results.items() if str(r).startswith("started")]
        minutes = -(-started["plan"]["total_sec"] // 60)
        lines = ["Started test %s: %s, about %d min." % (started["test_id"], started["plan"]["summary"], minutes)]
        lines += ["  %s: %s" % (h, r) for h, r in sorted(results.items())]
        if not ok:
            raise ToolError("\n".join(lines + ["No machine started the test."]))
        lines.append("Call rightsize_report until running is false (states trail the load by up to a "
                     "minute), or stop_load_test to end it early.")
        return "\n".join(lines), {"test_id": started["test_id"], "plan": started["plan"],
                                  "results": results, "minutes": minutes}

    def stop_load_test(self, args):
        results = self.call("/stop-stress", {}).get("results") or {}
        text = "Stop sent.\n" + "\n".join("  %s: %s" % (h, r) for h, r in sorted(results.items()))
        return text, {"results": results}

    # -- rightsize_report --------------------------------------------------

    def rightsize_report(self, args):
        ns = argparse.Namespace(
            test_id=args.get("test_id"),
            need=_int(args.get("need_mb"), "need_mb"),
            margin=_int(args.get("margin_pct"), "margin_pct"),
            cpu=args.get("cpu_vcpu"))
        if ns.cpu is not None and (isinstance(ns.cpu, bool) or not isinstance(ns.cpu, (int, float))):
            raise ToolError("cpu_vcpu must be a number of vCPUs, such as 0.5.")
        report = self.call(cli.report_query(ns))
        out = io.StringIO()
        cli.print_report(report, out)
        text = out.getvalue().rstrip()
        if not report.get("test_id") or report.get("error"):
            return text, {"report": report}
        limits = report_limits(report)
        text += "\n\nLimits of this answer:\n" + "\n".join("- " + x for x in limits)
        return text, {"report": compact(report), "limits": limits}


def _num(value):
    return "-" if value is None else ("%.2f" % value).rstrip("0").rstrip(".")


def compact(report):
    """The report without the per-level detail: what an agent needs to decide."""
    rec = report.get("recommendation") or {}
    pick = next((r for r in report["rows"] if r["hostname"] == rec.get("pick")), None)
    return {
        "test_id": report["test_id"],
        "running": report["running"],
        "summary": report["summary"],
        "need_mb": report["need_mb"],
        "margin_pct": report["margin_pct"],
        "required_mb": report["required_mb"],
        "cpu_vcpu": report.get("cpu_vcpu"),
        "recommendation": {
            "instance_type": pick["instance_type"] if pick else None,
            "usd_month": (round(pick["price"]["usd_month"] + ((pick.get("cpu") or {}).get("extra_usd_month") or 0), 2)
                          if pick and pick.get("price") else None),
            "headline": rec.get("headline"),
            "detail": rec.get("detail"),
            "lines": rec.get("lines") or [],
            "usage_rule": (rec.get("usage_rule") or {}).get("text"),
        } if rec else None,
        "sizes": [{
            "instance_type": r["instance_type"],
            "hostname": r["hostname"],
            "holds_mb": (r.get("capacity") or {}).get("mb"),
            "holds_kind": (r.get("capacity") or {}).get("kind"),
            "fit": r["fit"],
            "spare_pct": r["spare_pct"],
            "usd_month": r["price"]["usd_month"] if r.get("price") else None,
            "cpu": r.get("cpu"),
            "outcome": r["outcome"]["text"],
            # The evidence: sustained pressure at each load level.
            "levels": [{"mb": lv["mb"], "pressure_pct": lv["some"], "state": lv["state"],
                        "killed": lv["killed"]} for lv in r.get("levels") or []],
        } for r in report["rows"]],
        "prices": report["prices"]["note"],
    }


def report_limits(report):
    """What the answer does not cover, said plainly so an agent passes it on."""
    out = []
    if report.get("running"):
        out.append("The test is still running: this is the result so far, not the answer.")
    out.append("The load was synthetic: `stress` holding memory, not your app. It shows how much "
               "memory each size holds before programs wait for it; it does not model your app's "
               "file cache, CPU work or disk I/O.")
    out.append("Where a size broke between two load levels, what it holds is interpolated at the "
               "degraded line (10% of time waiting for memory), and errs low.")
    fits = {r["fit"] for r in report["rows"]}
    if "unmeasured" in fits or "untested" in fits:
        out.append("Some sizes were never pushed past your app's need plus its margin, so their "
                   "room to spare is unknown: ramp higher to measure it.")
    untested = (report.get("recommendation") or {}).get("untested")
    if untested:
        out.append("%s cost less with the same memory but were not in the test, so nothing here "
                   "says they hold the app." % " and ".join(u["instance_type"] for u in untested))
    if report.get("cpu_vcpu"):
        out.append("CPU is not measured by this test: it is worked out from each size's vCPUs and "
                   "CPU credits against the average you gave.")
    else:
        out.append("CPU was not considered. Pass cpu_vcpu (your app's average) to price in the CPU "
                   "credits a burstable size such as t3 would burn.")
    out.append(report["prices"]["note"])
    return out


TOOLS = [
    {
        "name": "fleet_status",
        "title": "Fleet status",
        "description": "The machines reporting to SysHealth: size, whether each is waiting for memory "
                       "now and why, what is growing, and, for an app watched with SYSHEALTH_APP, its "
                       "measured peak memory and average CPU over the last hour (use those as the app's "
                       "need). Give hostname for one machine.",
        "inputSchema": {"type": "object", "properties": {
            "hostname": {"type": "string", "description": "one machine's hostname or instance type"}},
            "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "plan_load_test",
        "title": "Plan a load test",
        "description": "Check a load test and say what it would do, how long it takes and which machines "
                       "would run it. Runs nothing.",
        "inputSchema": LOAD_SCHEMA,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "start_load_test",
        "title": "Start a load test",
        "description": "Make every machine that accepts tests hold the same memory at the same moment, "
                       "for minutes. It slows those machines down and may run the small ones out of "
                       "memory: confirm with the person first, and never point it at production. "
                       "Returns at once; follow with rightsize_report.",
        "inputSchema": LOAD_SCHEMA,
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                        "openWorldHint": False},
    },
    {
        "name": "stop_load_test",
        "title": "Stop the load test",
        "description": "Stop any load test running on the fleet.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "rightsize_report",
        "title": "Which size to buy",
        "description": "What the last load test (or test_id) showed: how much memory each size holds "
                       "before the app slows down, which is the cheapest that holds the app's need with "
                       "room to spare, its price a month, what a usage alarm would have picked instead, "
                       "the evidence (pressure at each load level), and the limits of the answer.",
        "inputSchema": {"type": "object", "properties": {
            "need_mb": {"type": "integer", "minimum": 1,
                        "description": "the app's memory at its busiest, MB (default: what the test was planned with)"},
            "margin_pct": {"type": "integer", "minimum": 0, "maximum": 200,
                           "description": "room to keep spare above that, % (default 25)"},
            "cpu_vcpu": {"type": "number", "exclusiveMinimum": 0, "maximum": 192,
                         "description": "the app's average CPU in vCPUs (0.5 = half of one); prices in CPU credits"},
            "test_id": {"type": "string", "description": "an earlier test (default: the latest)"}},
            "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, server_url):
        self.tools = Tools(server_url)
        self.version = PROTOCOL_VERSIONS[0]

    def handle(self, message):
        """One JSON-RPC message in; the response, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None,
                          -32600, "Invalid Request")
        method, params, msg_id = message["method"], message.get("params") or {}, message.get("id")
        notification = "id" not in message
        try:
            result = self.dispatch(method, params)
        except _RpcError as exc:
            return None if notification else _error(msg_id, exc.code, exc.message)
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result if result is not None else {}}

    def dispatch(self, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": self.version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        if method.startswith("notifications/"):
            return None
        raise _RpcError(-32601, "Method not found: %s" % method)

    def call_tool(self, name, args):
        if name not in {t["name"] for t in TOOLS}:
            raise _RpcError(-32602, "Unknown tool: %s" % name)
        if not isinstance(args, dict):
            raise _RpcError(-32602, "arguments must be an object")
        try:
            text, data = getattr(self.tools, name)(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # a bug here must not end the session
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": "SysHealth tool failed: %s" % exc}], "isError": True}
        # Readable text first; the same data as JSON for the agent to quote
        # exactly, and as structuredContent for clients that take it.
        result = {"content": [{"type": "text", "text": text},
                              {"type": "text", "text": json.dumps(data, separators=(",", ":"))}],
                  "isError": False}
        if self.version >= STRUCTURED_FROM:
            result["structuredContent"] = data
        return result


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(server, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "Parse error")
        else:
            if isinstance(message, list):  # a batch, from clients of the 2025-03-26 revision
                reply = [r for r in (server.handle(m) for m in message) if r is not None] or None
            else:
                reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="SysHealth's MCP server, over stdio.")
    parser.add_argument("--server", default=cli.DEFAULT_SERVER,
                        help="SysHealth server (default $SYSHEALTH_URL or %(default)s)")
    args = parser.parse_args(argv)
    print("syshealth MCP server: talking to %s%s" % (
        args.server, " with a token" if cli.TOKEN else ""), file=sys.stderr, flush=True)
    serve(Server(args.server))


if __name__ == "__main__":
    main()

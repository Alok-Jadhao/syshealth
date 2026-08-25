"""Command line interface.

Built on argparse and the standard library only, so ``syshealth watch`` and
``syshealth profile`` run on a fresh machine with nothing but Python 3.10
installed. The networked commands (``agent``, ``serve``) need extras; they say
so clearly instead of dying on an ImportError.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .analysis import Thresholds, summarise
from .catalog import Catalog
from .config import load as load_settings
from .models import Interval
from .procfs import ProcReader
from .render import format_live, format_summary, format_verdict
from .rightsize import Policy, Sizing, evaluate
from .sampler import Sampler

EXIT_OK = 0
EXIT_SATURATED = 2
EXIT_NO_PSI = 3
EXIT_ERROR = 1


# ---------------------------------------------------------------- doctor ---


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check whether this machine can be measured at all.

    Always the first thing to run on a new box, and the answer to "why does it
    say zero for everything?".
    """
    settings = load_settings(args.config, proc_root=args.proc_root)
    reader = ProcReader(settings.proc_root)

    print(f"syshealth {__version__}")
    print(f"python     {sys.version.split()[0]}")
    print(f"platform   {sys.platform}")
    print(f"proc root  {settings.proc_root}")
    print()

    ok = True

    if reader.has_psi():
        print("  [ok]   PSI available at /proc/pressure")
        for resource in ("cpu", "memory", "io"):
            reading = reader.read_pressure(resource)
            shares = [s for s in ("some", "full") if reading.line(s)]
            if shares:
                print(f"         {resource:<7} {', '.join(shares)}")
            else:
                print(f"  [warn] {resource:<7} present but unparseable")
    else:
        ok = False
        print("  [FAIL] PSI unavailable")
        print(f"         {reader.missing_psi_reason()}")
        print()
        print("         Saturation cannot be measured without it. Fixes:")
        print("           - Linux 4.20+ with CONFIG_PSI=y")
        print("           - some distros need psi=1 on the kernel cmdline")
        print("           - containers often do not expose it; measure the host")
        print()
        print("         You can still exercise the tool on recorded runs:")
        print("           syshealth report tests/fixtures/runs/thrashing.jsonl \\")
        print("               --instance-type t3.micro")

    mem = reader.read_meminfo()
    if mem.total_kb:
        print(f"  [ok]   meminfo   {mem.total_kb / (1024 * 1024):.2f} GB total")
    else:
        ok = False
        print("  [FAIL] meminfo unreadable")

    vmstat = reader.read_vmstat()
    print(f"  [ok]   vmstat    pgscan_direct={vmstat.pgscan_direct}")
    print(f"  [ok]   cpus      {reader.cpu_count()}")

    try:
        catalog = Catalog.load(args.catalog or settings.catalog_path or None)
        print(f"  [ok]   catalog   {len(catalog.types)} instance types")
    except ValueError as exc:
        ok = False
        print(f"  [FAIL] catalog   {exc}")

    print()
    print("ready" if ok else "not ready — see failures above")
    return EXIT_OK if ok else EXIT_NO_PSI


# ----------------------------------------------------------------- watch ---


def cmd_watch(args: argparse.Namespace) -> int:
    settings = load_settings(
        args.config,
        proc_root=args.proc_root,
        interval_s=args.interval,
    )
    reader = ProcReader(settings.proc_root)
    if not reader.has_psi():
        print(f"error: {reader.missing_psi_reason()}", file=sys.stderr)
        print("run 'syshealth doctor' for details", file=sys.stderr)
        return EXIT_NO_PSI

    sampler = Sampler(reader)
    print(
        f"watching {settings.proc_root} every {settings.interval_s:g}s "
        "— ctrl-c to stop"
    )
    print()
    try:
        for sample in sampler.stream(interval_s=settings.interval_s):
            print(format_live(sample))
    except KeyboardInterrupt:
        print("\nstopped")
    return EXIT_OK


# --------------------------------------------------------------- profile ---


def cmd_profile(args: argparse.Namespace) -> int:
    """Measure a window (optionally around a command) and give a verdict.

    This is the command the project is built around. ``syshealth profile --
    ./benchmark.sh`` answers "is this box the right size for this workload?"
    with evidence rather than intuition.
    """
    settings = load_settings(
        args.config,
        proc_root=args.proc_root,
        interval_s=args.interval,
        duration_s=args.duration,
        instance_type=args.instance_type,
    )
    reader = ProcReader(settings.proc_root)
    if not reader.has_psi():
        print(f"error: {reader.missing_psi_reason()}", file=sys.stderr)
        return EXIT_NO_PSI

    sampler = Sampler(reader)
    samples: list[Interval] = []
    proc: subprocess.Popen | None = None
    label = args.label or ""

    if args.command:
        label = label or " ".join(args.command)
        print(f"running: {' '.join(args.command)}")
        try:
            proc = subprocess.Popen(args.command)
        except (OSError, ValueError) as exc:
            print(f"error: could not start command: {exc}", file=sys.stderr)
            return EXIT_ERROR
    else:
        print(f"profiling for {settings.duration_s:g}s")

    print()
    sampler.tick()  # prime
    started = time.monotonic()
    stop = threading.Event()

    try:
        while True:
            stop.wait(settings.interval_s)
            sample = sampler.tick()
            if sample is not None:
                samples.append(sample)
                if not args.quiet:
                    print(format_live(sample))

            elapsed = time.monotonic() - started
            if proc is not None:
                if proc.poll() is not None:
                    print(f"\ncommand exited with code {proc.returncode}")
                    break
            elif elapsed >= settings.duration_s:
                break
    except KeyboardInterrupt:
        print("\ninterrupted — analysing what was collected")
        if proc is not None:
            proc.terminate()

    return _finish(args, samples, settings, label)


# ---------------------------------------------------------------- report ---


def cmd_report(args: argparse.Namespace) -> int:
    """Re-analyse samples recorded earlier, without touching the machine."""
    path = Path(args.source)
    if not path.exists():
        print(f"error: no such file: {path}", file=sys.stderr)
        return EXIT_ERROR

    samples: list[Interval] = []
    for line_no, raw in enumerate(path.read_text().splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            samples.append(Interval.from_dict(json.loads(raw)))
        except (ValueError, TypeError) as exc:
            print(f"warning: skipping line {line_no}: {exc}", file=sys.stderr)

    if not samples:
        print("error: no usable samples in file", file=sys.stderr)
        return EXIT_ERROR

    settings = load_settings(args.config, instance_type=args.instance_type)
    return _finish(args, samples, settings, args.label or path.name)


def _finish(
    args: argparse.Namespace,
    samples: list[Interval],
    settings,
    label: str,
) -> int:
    if not samples:
        print("error: no samples collected", file=sys.stderr)
        return EXIT_ERROR

    thresholds = Thresholds()
    policy = Policy()
    if getattr(args, "headroom", None) is not None:
        policy = Policy(headroom=args.headroom / 100.0)

    summary = summarise(samples, thresholds=thresholds, label=label)

    try:
        catalog = Catalog.load(getattr(args, "catalog", None) or settings.catalog_path or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    verdict = evaluate(
        summary,
        current_type=settings.instance_type or None,
        catalog=catalog,
        policy=policy,
        thresholds=thresholds,
    )

    if getattr(args, "save", None):
        Path(args.save).write_text(
            "\n".join(s.to_json() for s in samples) + "\n"
        )
        print(f"\nsaved {len(samples)} samples to {args.save}")

    if getattr(args, "json", False):
        print(json.dumps(_as_json(summary, verdict), indent=2, default=str))
    else:
        print(format_summary(summary))
        print(format_verdict(verdict))
        print()

    return EXIT_SATURATED if verdict.sizing is Sizing.UNDERSIZED else EXIT_OK


def _as_json(summary, verdict) -> dict:
    from dataclasses import asdict

    return {
        "summary": {
            **{
                k: v
                for k, v in asdict(summary).items()
                if k != "resources"
            },
            "state": summary.state.value,
            "bottleneck": summary.bottleneck,
            "divergence": round(summary.divergence, 2),
            "resources": {
                name: {**asdict(res), "state": res.state.value}
                for name, res in summary.resources.items()
            },
        },
        "verdict": {
            "sizing": verdict.sizing.value,
            "confidence": verdict.confidence.value,
            "headline": verdict.headline,
            "current": asdict(verdict.current) if verdict.current else None,
            "recommended": asdict(verdict.recommended) if verdict.recommended else None,
            "monthly_delta_usd": round(verdict.monthly_delta_usd, 2),
            "peak_working_set_gb": round(verdict.peak_working_set_gb, 3),
            "reasons": verdict.reasons,
            "evidence": verdict.evidence,
            "caveats": verdict.caveats,
        },
    }


# ------------------------------------------------------- agent / server ----


def cmd_agent(args: argparse.Namespace) -> int:
    settings = load_settings(
        args.config,
        proc_root=args.proc_root,
        interval_s=args.interval,
        server_url=args.server,
        node_name=args.node_name,
        instance_type=args.instance_type,
    )
    if not settings.server_url:
        print(
            "error: no server URL. Pass --server, or set SYSHEALTH_SERVER_URL.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        from .agent import run_agent
    except ImportError as exc:  # pragma: no cover
        print(f"error: agent needs the 'net' extra: pip install 'syshealth[net]' ({exc})", file=sys.stderr)
        return EXIT_ERROR

    return run_agent(settings)


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the read-only measurement tools over MCP on stdio.

    Nothing is printed to stdout here: on stdio, stdout is the JSON-RPC
    channel and anything else on it corrupts the session.
    """
    settings = load_settings(args.config, proc_root=args.proc_root, db_path=args.db)

    try:
        from .mcp.server import run_server as run_mcp
    except ImportError as exc:
        print(
            "error: the MCP server needs the SDK: pip install 'syshealth[mcp]'\n"
            f"       ({exc})",
            file=sys.stderr,
        )
        return EXIT_ERROR

    from .mcp.sources import LiveSource, ReplaySource, load_run
    from .mcp.tools import build_tools

    tools: dict = {}
    notes: list[str] = []

    if args.db:
        from .mcp.fleet import build_fleet_tools
        from .store import Store

        try:
            # Read-only: this process observes the fleet, it does not record
            # into it. A missing file fails here rather than silently becoming
            # a new empty database that reports an empty fleet.
            store = Store(args.db, read_only=True)
            catalog = Catalog.load(args.catalog or settings.catalog_path or None)
        except (sqlite3.Error, ValueError) as exc:
            print(f"error: could not open {args.db} for reading: {exc}", file=sys.stderr)
            return EXIT_ERROR

        tools |= build_fleet_tools(store, catalog)
        notes.append(f"fleet: {args.db} (read-only), {store.count()} samples stored")

    if not args.fleet_only:
        if args.replay:
            try:
                source = ReplaySource(load_run(args.replay), label=Path(args.replay).name)
            except (OSError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_ERROR
        else:
            source = LiveSource(settings.proc_root)
            if not source.psi_available:
                notes.append(
                    "warning: no PSI on this kernel — the local tools will report "
                    "psi_available=false and state UNKNOWN. Use --replay to serve "
                    "a recorded run instead."
                )
        tools |= build_tools(source)
        notes.append(f"local: {source.name}")

    if not tools:
        print("error: --fleet-only needs --db", file=sys.stderr)
        return EXIT_ERROR

    return run_mcp(tools, notes)


def cmd_serve(args: argparse.Namespace) -> int:
    settings = load_settings(
        args.config,
        bind_host=args.host,
        bind_port=args.port,
        db_path=args.db,
    )
    try:
        from .server import run_server
    except ImportError as exc:
        print(
            "error: the server needs Flask: pip install 'syshealth[server]'\n"
            f"       ({exc})",
            file=sys.stderr,
        )
        return EXIT_ERROR

    return run_server(settings)


# ------------------------------------------------------------------ main ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syshealth",
        description=(
            "Measure whether a machine is actually saturated, using Linux PSI, "
            "and say what size it should be."
        ),
    )
    parser.add_argument("--version", action="version", version=f"syshealth {__version__}")
    sub = parser.add_subparsers(dest="cmd_name", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path to a syshealth.toml")
        p.add_argument(
            "--proc-root",
            help=(
                "read kernel files from here instead of /proc. Use this in a "
                "container with the host's /proc bind-mounted, e.g. /host/proc"
            ),
        )

    doctor = sub.add_parser("doctor", help="check that this machine can be measured")
    common(doctor)
    doctor.add_argument("--catalog", help="instance catalog JSON to validate")
    doctor.set_defaults(func=cmd_doctor)

    watch = sub.add_parser("watch", help="live saturation, one line per sample")
    common(watch)
    watch.add_argument("--interval", type=float, help="seconds between samples")
    watch.set_defaults(func=cmd_watch)

    profile = sub.add_parser(
        "profile",
        help="measure a window or a command, then give a sizing verdict",
    )
    common(profile)
    profile.add_argument("--interval", type=float, help="seconds between samples")
    profile.add_argument("--duration", type=float, help="seconds to measure")
    profile.add_argument("--instance-type", help="what this box is, e.g. t3.medium")
    profile.add_argument("--label", help="name for this run")
    profile.add_argument("--catalog", help="instance catalog JSON")
    profile.add_argument("--headroom", type=float, help="percent spare RAM to keep (default 30)")
    profile.add_argument("--save", help="write raw samples to this JSONL file")
    profile.add_argument("--json", action="store_true", help="machine-readable output")
    profile.add_argument("--quiet", action="store_true", help="no live lines")
    profile.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="optional: -- COMMAND to profile until it exits",
    )
    profile.set_defaults(func=cmd_profile)

    report = sub.add_parser("report", help="re-analyse a saved JSONL run")
    report.add_argument("source", help="JSONL file written by --save")
    report.add_argument("--config")
    report.add_argument("--instance-type")
    report.add_argument("--label")
    report.add_argument("--catalog")
    report.add_argument("--headroom", type=float)
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    agent = sub.add_parser("agent", help="push measurements to a central server")
    common(agent)
    agent.add_argument("--server", help="e.g. http://10.0.0.5:5000")
    agent.add_argument("--interval", type=float)
    agent.add_argument("--node-name")
    agent.add_argument("--instance-type")
    agent.set_defaults(func=cmd_agent)

    mcp = sub.add_parser(
        "mcp",
        help="expose the read-only measurement tools to an AI client over MCP",
    )
    common(mcp)
    mcp.add_argument(
        "--replay",
        help=(
            "serve a recorded JSONL run instead of this machine. Lets the tools "
            "be exercised where there is no PSI kernel, and is the only way to "
            "demonstrate a saturated machine without saturating one"
        ),
    )
    mcp.add_argument(
        "--db",
        help=(
            "also expose fleet tools over this sqlite database, opened "
            "read-only. This is the telemetry agents push to 'syshealth serve'"
        ),
    )
    mcp.add_argument(
        "--fleet-only",
        action="store_true",
        help="omit the tools that measure this machine (needs --db)",
    )
    mcp.add_argument("--catalog", help="instance catalog JSON for sizing verdicts")
    mcp.set_defaults(func=cmd_mcp)

    serve = sub.add_parser("serve", help="run the fleet server and dashboard")
    serve.add_argument("--config")
    serve.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int)
    serve.add_argument("--db", help="sqlite path")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # argparse.REMAINDER keeps the "--" separator in the list; drop it so
    # `syshealth profile -- ls -l` runs `ls -l` and not `-- ls -l`.
    trailing = getattr(args, "command", None)
    if isinstance(trailing, list) and trailing and trailing[0] == "--":
        args.command = trailing[1:]

    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_ERROR

    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

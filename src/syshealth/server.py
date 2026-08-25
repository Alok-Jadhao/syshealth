"""The fleet server.

Aggregates pushes from agents and — the part that makes it more than a
dashboard — runs the same right-sizing engine the CLI uses over each node's
stored history, so ``/fleet`` answers "what should this fleet actually be
running?" rather than just plotting lines.

Flask is an optional dependency. Everything else in SysHealth works without it.
"""

from __future__ import annotations

import sys

from flask import Flask, jsonify, request

from .analysis import summarise
from .catalog import Catalog
from .config import Settings
from .models import Interval
from .rightsize import evaluate
from .store import Store

ONLINE_WINDOW_S = 20.0


def create_app(settings: Settings, store: Store | None = None) -> Flask:
    app = Flask(__name__)
    app.config["STORE"] = store or Store(settings.db_path)
    app.config["CATALOG"] = Catalog.load(settings.catalog_path or None)

    @app.post("/metrics")
    def ingest():
        data = request.get_json(silent=True) or {}
        node = data.get("node")
        sample = data.get("sample")

        if not node or not isinstance(sample, dict):
            return jsonify({"error": "expected {node, sample}"}), 400

        app.config["STORE"].record(
            node=str(node),
            payload=sample,
            instance_type=data.get("instance_type"),
            address=request.remote_addr,
        )
        return jsonify({"ok": True}), 202

    @app.get("/nodes")
    def nodes():
        return jsonify(app.config["STORE"].nodes(ONLINE_WINDOW_S))

    @app.get("/nodes/<node>/samples")
    def samples(node: str):
        limit = min(max(request.args.get("limit", 500, type=int), 1), 5000)
        return jsonify(app.config["STORE"].samples(node, limit))

    @app.get("/nodes/<node>/verdict")
    def node_verdict(node: str):
        result = _verdict_for(app, node)
        if result is None:
            return jsonify({"error": f"no samples for {node}"}), 404
        return jsonify(result)

    @app.get("/fleet")
    def fleet():
        """Every node's verdict, plus what the whole fleet is wasting."""
        store = app.config["STORE"]
        results = []
        monthly_delta = 0.0

        for entry in store.nodes(ONLINE_WINDOW_S):
            result = _verdict_for(app, entry["node"])
            if result is None:
                continue
            result["online"] = entry["online"]
            result["seconds_since"] = entry["seconds_since"]
            results.append(result)
            monthly_delta += result["monthly_delta_usd"]

        return jsonify(
            {
                "nodes": results,
                "monthly_delta_usd": round(monthly_delta, 2),
                "annual_delta_usd": round(monthly_delta * 12, 2),
                "undersized": sum(1 for r in results if r["sizing"] == "UNDERSIZED"),
                "oversized": sum(1 for r in results if r["sizing"] == "OVERSIZED"),
            }
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "samples": app.config["STORE"].count()})

    return app


def _verdict_for(app: Flask, node: str) -> dict | None:
    store = app.config["STORE"]
    raw = store.samples(node, limit=2000)
    if not raw:
        return None

    samples = [Interval.from_dict(r) for r in raw]
    meta = next((n for n in store.nodes() if n["node"] == node), {})
    summary = summarise(samples, label=node)
    verdict = evaluate(
        summary,
        current_type=meta.get("instance_type"),
        catalog=app.config["CATALOG"],
    )

    return {
        "node": node,
        "instance_type": meta.get("instance_type"),
        "state": summary.state.value,
        "bottleneck": summary.bottleneck,
        "sizing": verdict.sizing.value,
        "confidence": verdict.confidence.value,
        "headline": verdict.headline,
        "recommended": verdict.recommended.name if verdict.recommended else None,
        "monthly_delta_usd": round(verdict.monthly_delta_usd, 2),
        "samples": summary.samples,
        "duration_s": round(summary.duration_s, 1),
    }


def run_server(settings: Settings) -> int:
    app = create_app(settings)
    if settings.bind_host == "0.0.0.0":
        print(
            "warning: binding 0.0.0.0 with no authentication. Put this behind "
            "a security group, VPN or reverse proxy.",
            file=sys.stderr,
        )
    print(f"serving on http://{settings.bind_host}:{settings.bind_port}")
    print(f"database: {settings.db_path}")
    app.run(host=settings.bind_host, port=settings.bind_port)
    return 0

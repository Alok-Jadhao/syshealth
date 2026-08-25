"""The fleet server.

Aggregates pushes from agents and — the part that makes it more than a
dashboard — runs the same right-sizing engine the CLI uses over each node's
stored history, so ``/fleet`` answers "what should this fleet actually be
running?" rather than just plotting lines.

Flask is an optional dependency. Everything else in SysHealth works without it.
"""

from __future__ import annotations

import sys

from flask import Flask, jsonify, render_template_string, request

from .analysis import summarise
from .catalog import Catalog
from .config import Settings
from .dashboard import DASHBOARD
from .models import Interval
from .rightsize import evaluate
from .sre.actions import catalogue
from .sre.incidents import ActionStatus, IncidentStore, Status
from .store import Store

ONLINE_WINDOW_S = 20.0


def create_app(
    settings: Settings,
    store: Store | None = None,
    incidents: IncidentStore | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["STORE"] = store or Store(settings.db_path)
    app.config["CATALOG"] = Catalog.load(settings.catalog_path or None)
    app.config["INCIDENTS"] = incidents or IncidentStore(settings.incidents_db)
    app.config["SETTINGS"] = settings

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

    # ------------------------------------------------------------ incidents --

    @app.get("/incidents")
    def incidents_list():
        record = app.config["INCIDENTS"]
        status = request.args.get("status")
        try:
            wanted = Status(status.upper()) if status else None
        except ValueError:
            return jsonify({"error": f"unknown status {status!r}"}), 400

        rows = record.list_incidents(
            status=wanted,
            node=request.args.get("node"),
            limit=request.args.get("limit", 100, type=int),
        )
        return jsonify(
            {
                "count": len(rows),
                "incidents": [i.to_dict() for i in rows],
                "active": len([i for i in rows if not i.status.terminal]),
            }
        )

    @app.get("/incidents/<incident_id>")
    def incident_detail(incident_id: str):
        """The whole story: symptoms, evidence, diagnosis, actions, verification."""
        report = app.config["INCIDENTS"].report(incident_id)
        if report is None:
            return jsonify({"error": f"no such incident: {incident_id}"}), 404
        return jsonify(report)

    # ------------------------------------------------------------- approvals --

    @app.get("/approvals")
    def approvals():
        pending = app.config["INCIDENTS"].pending_approvals()
        return jsonify({"count": len(pending), "actions": pending})

    @app.post("/actions/<int:action_id>/decision")
    def decide(action_id: int):
        """A human answers an approval request.

        Only ever moves an action from AWAITING_APPROVAL. An action that was
        denied by policy, already dispatched, or already finished cannot be
        approved after the fact — the decision has to happen in the window the
        policy engine opened, not later.
        """
        record = app.config["INCIDENTS"]
        action = record.get_action(action_id)
        if action is None:
            return jsonify({"error": f"no such action: {action_id}"}), 404
        if action["status"] != ActionStatus.AWAITING_APPROVAL.value:
            return jsonify(
                {
                    "error": (
                        f"action {action_id} is {action['status']}, not awaiting "
                        "approval. Only a pending action can be decided."
                    )
                }
            ), 409

        data = request.get_json(silent=True) or {}
        approve = data.get("approve")
        who = str(data.get("by") or request.remote_addr or "unknown")
        if not isinstance(approve, bool):
            return jsonify({"error": "expected {approve: true|false, by: name}"}), 400

        record.set_action_status(
            action_id,
            ActionStatus.APPROVED if approve else ActionStatus.REJECTED,
            decided_by=who,
        )
        return jsonify({"ok": True, "action_id": action_id, "approved": approve, "by": who})

    # --------------------------------------------------------- action queue --

    @app.get("/nodes/<node>/actions/next")
    def next_action(node: str):
        """A node collects its next approved action, if there is one.

        Poll-based on purpose: the node opens no port, so there is nothing on
        it to reach. The claim is the UPDATE inside the store, so two pollers
        racing cannot both receive the same action.
        """
        return jsonify(app.config["INCIDENTS"].claim_next_action(node) or {})

    @app.post("/actions/<int:action_id>/result")
    def action_result(action_id: int):
        """A node reports what happened. Never resolves an incident by itself."""
        record = app.config["INCIDENTS"]
        action = record.get_action(action_id)
        if action is None:
            return jsonify({"error": f"no such action: {action_id}"}), 404
        if action["status"] != ActionStatus.DISPATCHED.value:
            return jsonify(
                {"error": f"action {action_id} is {action['status']}, not dispatched"}
            ), 409

        data = request.get_json(silent=True) or {}
        ok = bool(data.get("ok"))
        record.set_action_status(
            action_id,
            ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED,
            result=data.get("detail") or {},
        )
        record.set_status(
            action["incident_id"],
            Status.VERIFYING if ok else Status.OPEN,
            f"{action['action']} reported {'success' if ok else 'failure'} — "
            + ("verifying the machine actually recovered" if ok else "re-investigating"),
        )
        return jsonify({"ok": True, "action_id": action_id, "next": "VERIFYING" if ok else "OPEN"})

    @app.get("/actions/catalogue")
    def action_catalogue():
        """Everything this system is capable of doing, with tiers."""
        return jsonify({"actions": catalogue()})

    # ------------------------------------------------------------ dashboard --

    @app.get("/")
    def dashboard():
        return render_template_string(DASHBOARD)

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

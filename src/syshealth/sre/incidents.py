"""Incidents, their timelines, and the audit record.

Everything the system observes, concludes, proposes and does lands here before
or as it happens, in SQLite, so that the three questions in the brief have
answers that do not depend on anyone's recollection:

    why did the AI restart this container?      -> diagnosis + action.reason
    what evidence did it use?                   -> evidence rows, with values
    what happened after the action?             -> verification + timeline

Two design choices are load-bearing.

**Evidence is stored as rows, not prose.** A diagnosis references evidence by
id. A reasoner cannot cite a measurement that was never retrieved, because the
citation has to resolve against something the tool layer actually wrote. This
is the mechanism behind "the AI must not invent a root cause" — it is a
foreign key, not an instruction.

**An action is durable before it is dispatchable.** It exists as a row, with
its reason and its ruling, before any node can see it, and its result is
written whether it succeeded, failed or timed out. There is no state in which
something ran and nothing recorded it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             TEXT PRIMARY KEY,
    node           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    status         TEXT NOT NULL,
    title          TEXT NOT NULL,
    opened_ts      REAL NOT NULL,
    closed_ts      REAL,
    resolution     TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    mode           TEXT NOT NULL DEFAULT 'OBSERVE',
    fingerprint    TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status, opened_ts DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_node ON incidents (node, opened_ts DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents (fingerprint, status);

CREATE TABLE IF NOT EXISTS timeline (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL,
    ts             REAL NOT NULL,
    kind           TEXT NOT NULL,
    message        TEXT NOT NULL,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_timeline_incident ON timeline (incident_id, ts, id);

CREATE TABLE IF NOT EXISTS evidence (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL,
    ts             REAL NOT NULL,
    tool           TEXT NOT NULL,
    arguments      TEXT NOT NULL,
    result         TEXT NOT NULL,
    ok             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence (incident_id, id);

CREATE TABLE IF NOT EXISTS diagnoses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL,
    ts             REAL NOT NULL,
    reasoner       TEXT NOT NULL,
    cause          TEXT NOT NULL,
    confidence     TEXT NOT NULL,
    observations   TEXT NOT NULL,
    hypotheses     TEXT NOT NULL,
    cites          TEXT NOT NULL,
    recommended    TEXT
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_incident ON diagnoses (incident_id, id);

CREATE TABLE IF NOT EXISTS actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL,
    node           TEXT NOT NULL,
    action         TEXT NOT NULL,
    arguments      TEXT NOT NULL,
    tier           TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT NOT NULL,
    ruling         TEXT NOT NULL,
    created_ts     REAL NOT NULL,
    decided_ts     REAL,
    decided_by     TEXT,
    dispatched_ts  REAL,
    completed_ts   REAL,
    result         TEXT,
    attempt        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON actions (incident_id, id);
CREATE INDEX IF NOT EXISTS idx_actions_dispatch ON actions (node, status, created_ts);

CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL,
    action_id      INTEGER,
    ts             REAL NOT NULL,
    recovered      INTEGER NOT NULL,
    summary        TEXT NOT NULL,
    checks         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_incident ON verifications (incident_id, id);
"""


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"INFO": 0, "WARNING": 1, "CRITICAL": 2}[self.value]


class Status(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    """Handed to a human: remediation failed, was refused, or timed out."""

    @property
    def terminal(self) -> bool:
        return self in (Status.RESOLVED, Status.ESCALATED)


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DENIED = "DENIED"
    """Refused by policy. Distinct from REJECTED, which is a human saying no."""
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class TimelineEvent:
    ts: float
    kind: str
    message: str
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "clock": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "kind": self.kind,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class Incident:
    id: str
    node: str
    severity: Severity
    status: Status
    title: str
    opened_ts: float
    closed_ts: float | None = None
    resolution: str = ""
    attempts: int = 0
    mode: str = "OBSERVE"
    fingerprint: str = ""
    timeline: list[TimelineEvent] = field(default_factory=list)

    @property
    def age_s(self) -> float:
        return (self.closed_ts or time.time()) - self.opened_ts

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["status"] = self.status.value
        data["age_s"] = round(self.age_s, 1)
        data["timeline"] = [e.to_dict() for e in self.timeline]
        return data


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class IncidentStore:
    """SQLite persistence for incidents and everything attached to them.

    Separate database from the telemetry store on purpose. Telemetry is
    high-volume and expendable — it has a 24-hour retention and gets deleted on
    write. The audit trail is neither, and must not share a retention policy
    with the thing it is auditing.
    """

    def __init__(self, path: str | Path = "incidents.db", read_only: bool = False) -> None:
        self.path = str(path)
        self.read_only = read_only
        self._lock = threading.Lock()

        if read_only:
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            return

        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- incidents ----------------------------------------------------------

    def next_id(self) -> str:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()
        return f"INC-{1000 + row[0] + 1}"

    def open_incident(
        self,
        node: str,
        severity: Severity,
        title: str,
        mode: str = "OBSERVE",
        fingerprint: str = "",
        now: float | None = None,
    ) -> Incident:
        now = now if now is not None else time.time()
        incident = Incident(
            id=self.next_id(),
            node=node,
            severity=severity,
            status=Status.OPEN,
            title=title,
            opened_ts=now,
            mode=mode,
            fingerprint=fingerprint,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO incidents (id, node, severity, status, title, "
                "opened_ts, attempts, mode, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    incident.id,
                    node,
                    severity.value,
                    Status.OPEN.value,
                    title,
                    now,
                    0,
                    mode,
                    fingerprint,
                ),
            )
            self._conn.commit()
        self.add_event(incident.id, "detected", title, {"severity": severity.value}, now)
        return incident

    def find_open(self, fingerprint: str) -> Incident | None:
        """An incident already open for the same symptom on the same node.

        Deduplication is what stops a detector that runs every few seconds from
        opening hundreds of incidents for one ongoing problem.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE fingerprint = ? AND status NOT IN (?, ?) "
                "ORDER BY opened_ts DESC LIMIT 1",
                (fingerprint, Status.RESOLVED.value, Status.ESCALATED.value),
            ).fetchone()
        return self._incident(row) if row else None

    def get(self, incident_id: str, with_timeline: bool = True) -> Incident | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            return None
        incident = self._incident(row)
        if with_timeline:
            incident.timeline = self.timeline(incident_id)
        return incident

    def list_incidents(
        self, status: Status | None = None, node: str | None = None, limit: int = 100
    ) -> list[Incident]:
        query = "SELECT * FROM incidents"
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if node is not None:
            clauses.append("node = ?")
            params.append(node)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY opened_ts DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._incident(r) for r in rows]

    def active(self) -> list[Incident]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE status NOT IN (?, ?) ORDER BY opened_ts",
                (Status.RESOLVED.value, Status.ESCALATED.value),
            ).fetchall()
        return [self._incident(r) for r in rows]

    def set_status(
        self,
        incident_id: str,
        status: Status,
        message: str = "",
        resolution: str = "",
        now: float | None = None,
    ) -> None:
        now = now if now is not None else time.time()
        closed = now if status.terminal else None
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET status = ?, closed_ts = COALESCE(?, closed_ts), "
                "resolution = COALESCE(NULLIF(?, ''), resolution) WHERE id = ?",
                (status.value, closed, resolution, incident_id),
            )
            self._conn.commit()
        self.add_event(incident_id, "status", message or f"status -> {status.value}", None, now)

    def bump_attempts(self, incident_id: str) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET attempts = attempts + 1 WHERE id = ?", (incident_id,)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT attempts FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return row["attempts"] if row else 0

    # -- timeline -----------------------------------------------------------

    def add_event(
        self,
        incident_id: str,
        kind: str,
        message: str,
        detail: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO timeline (incident_id, ts, kind, message, detail) VALUES (?,?,?,?,?)",
                (incident_id, now, kind, message, _json(detail) if detail else None),
            )
            self._conn.commit()

    def timeline(self, incident_id: str) -> list[TimelineEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, message, detail FROM timeline "
                "WHERE incident_id = ? ORDER BY ts, id",
                (incident_id,),
            ).fetchall()
        return [
            TimelineEvent(
                ts=r["ts"],
                kind=r["kind"],
                message=r["message"],
                detail=json.loads(r["detail"]) if r["detail"] else None,
            )
            for r in rows
        ]

    # -- evidence -----------------------------------------------------------

    def add_evidence(
        self,
        incident_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: Any,
        ok: bool = True,
        now: float | None = None,
    ) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO evidence (incident_id, ts, tool, arguments, result, ok) "
                "VALUES (?,?,?,?,?,?)",
                (incident_id, now, tool, _json(arguments), _json(result), int(ok)),
            )
            self._conn.commit()
            evidence_id = int(cursor.lastrowid)

        self.add_event(
            incident_id,
            "evidence",
            f"{'collected' if ok else 'failed to collect'} {tool}",
            {"evidence_id": evidence_id, "tool": tool, "arguments": arguments},
            now,
        )
        return evidence_id

    def evidence(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "tool": r["tool"],
                "arguments": json.loads(r["arguments"]),
                "result": json.loads(r["result"]),
                "ok": bool(r["ok"]),
            }
            for r in rows
        ]

    # -- diagnoses ----------------------------------------------------------

    def add_diagnosis(self, incident_id: str, diagnosis, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO diagnoses (incident_id, ts, reasoner, cause, confidence, "
                "observations, hypotheses, cites, recommended) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    now,
                    diagnosis.reasoner,
                    diagnosis.cause,
                    diagnosis.confidence,
                    _json(diagnosis.observations),
                    _json(diagnosis.hypotheses),
                    _json(diagnosis.cites),
                    _json(diagnosis.recommended.describe()) if diagnosis.recommended else None,
                ),
            )
            self._conn.commit()
            diagnosis_id = int(cursor.lastrowid)

        self.add_event(
            incident_id,
            "diagnosis",
            f"probable cause: {diagnosis.cause} (confidence {diagnosis.confidence})",
            {
                "diagnosis_id": diagnosis_id,
                "reasoner": diagnosis.reasoner,
                "cites": diagnosis.cites,
                "recommended": diagnosis.recommended.describe()
                if diagnosis.recommended
                else None,
            },
            now,
        )
        return diagnosis_id

    def diagnoses(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM diagnoses WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "reasoner": r["reasoner"],
                "cause": r["cause"],
                "confidence": r["confidence"],
                "observations": json.loads(r["observations"]),
                "hypotheses": json.loads(r["hypotheses"]),
                "cites": json.loads(r["cites"]),
                "recommended": json.loads(r["recommended"]) if r["recommended"] else None,
            }
            for r in rows
        ]

    # -- actions ------------------------------------------------------------

    def record_action(
        self,
        incident_id: str,
        node: str,
        action,
        status: ActionStatus,
        reason: str,
        ruling: str,
        attempt: int = 1,
        now: float | None = None,
    ) -> int:
        """Write the action down before anything can act on it."""
        now = now if now is not None else time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO actions (incident_id, node, action, arguments, tier, status, "
                "reason, ruling, created_ts, attempt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    node,
                    action.name,
                    _json(action.args),
                    action.tier.value,
                    status.value,
                    reason,
                    ruling,
                    now,
                    attempt,
                ),
            )
            self._conn.commit()
            action_id = int(cursor.lastrowid)

        self.add_event(
            incident_id,
            "action",
            f"{action.describe()} -> {status.value}",
            {"action_id": action_id, "tier": action.tier.value, "ruling": ruling},
            now,
        )
        return action_id

    def set_action_status(
        self,
        action_id: int,
        status: ActionStatus,
        decided_by: str | None = None,
        result: Any = None,
        now: float | None = None,
    ) -> None:
        now = now if now is not None else time.time()
        # Which timestamp this transition stamps. The column name comes from
        # this fixed mapping and never from a caller, so the f-string below
        # cannot carry anything but one of these literals.
        stamp = {
            ActionStatus.APPROVED: "decided_ts",
            ActionStatus.REJECTED: "decided_ts",
            ActionStatus.DISPATCHED: "dispatched_ts",
            ActionStatus.SUCCEEDED: "completed_ts",
            ActionStatus.FAILED: "completed_ts",
            ActionStatus.EXPIRED: "completed_ts",
        }.get(status)

        sets = ["status = ?", "decided_by = COALESCE(?, decided_by)", "result = COALESCE(?, result)"]
        params: list[Any] = [
            status.value,
            decided_by,
            _json(result) if result is not None else None,
        ]
        if stamp:
            sets.append(f"{stamp} = ?")
            params.append(now)
        params.append(action_id)

        with self._lock:
            self._conn.execute(
                f"UPDATE actions SET {', '.join(sets)} WHERE id = ?", params
            )
            self._conn.commit()

        incident_id = self.action_incident(action_id)
        if incident_id:
            self.add_event(
                incident_id,
                "action",
                f"action {action_id} -> {status.value}"
                + (f" by {decided_by}" if decided_by else ""),
                {"action_id": action_id, "result": result},
                now,
            )

    def action_incident(self, action_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT incident_id FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return row["incident_id"] if row else None

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return self._action(row) if row else None

    def actions(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [self._action(r) for r in rows]

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE status = ? ORDER BY created_ts",
                (ActionStatus.AWAITING_APPROVAL.value,),
            ).fetchall()
        return [self._action(r) for r in rows]

    def claim_next_action(self, node: str, now: float | None = None) -> dict[str, Any] | None:
        """Hand one approved action to a node, exactly once.

        The node polls; nothing is pushed to it. The UPDATE is the claim, so
        two pollers racing cannot both receive the same action.
        """
        now = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE node = ? AND status = ? "
                "ORDER BY created_ts LIMIT 1",
                (node, ActionStatus.APPROVED.value),
            ).fetchone()
            if row is None:
                return None
            changed = self._conn.execute(
                "UPDATE actions SET status = ?, dispatched_ts = ? WHERE id = ? AND status = ?",
                (ActionStatus.DISPATCHED.value, now, row["id"], ActionStatus.APPROVED.value),
            ).rowcount
            self._conn.commit()
            if not changed:
                return None
            claimed = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (row["id"],)
            ).fetchone()

        action = self._action(claimed)
        self.add_event(
            action["incident_id"],
            "dispatch",
            f"{action['action']} dispatched to {node}",
            {"action_id": action["id"]},
            now,
        )
        return action

    def nodes_under_remediation(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT node FROM actions WHERE status IN (?, ?)",
                (ActionStatus.APPROVED.value, ActionStatus.DISPATCHED.value),
            ).fetchall()
        return {r["node"] for r in rows}

    # -- verification -------------------------------------------------------

    def add_verification(
        self,
        incident_id: str,
        action_id: int | None,
        result,
        now: float | None = None,
    ) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO verifications (incident_id, action_id, ts, recovered, "
                "summary, checks) VALUES (?,?,?,?,?,?)",
                (
                    incident_id,
                    action_id,
                    now,
                    int(result.recovered),
                    result.summary,
                    _json([asdict(c) for c in result.checks]),
                ),
            )
            self._conn.commit()
            verification_id = int(cursor.lastrowid)

        self.add_event(
            incident_id,
            "verification",
            result.summary,
            {
                "verification_id": verification_id,
                "recovered": result.recovered,
                "checks": [asdict(c) for c in result.checks],
            },
            now,
        )
        return verification_id

    def verifications(self, incident_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM verifications WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "action_id": r["action_id"],
                "recovered": bool(r["recovered"]),
                "summary": r["summary"],
                "checks": json.loads(r["checks"]),
            }
            for r in rows
        ]

    # -- the whole story ----------------------------------------------------

    def report(self, incident_id: str) -> dict[str, Any] | None:
        """Everything recorded about one incident, in one object."""
        incident = self.get(incident_id)
        if incident is None:
            return None
        return {
            "incident": incident.to_dict(),
            "evidence": self.evidence(incident_id),
            "diagnoses": self.diagnoses(incident_id),
            "actions": self.actions(incident_id),
            "verifications": self.verifications(incident_id),
        }

    # -- row mapping --------------------------------------------------------

    @staticmethod
    def _incident(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            node=row["node"],
            severity=Severity(row["severity"]),
            status=Status(row["status"]),
            title=row["title"],
            opened_ts=row["opened_ts"],
            closed_ts=row["closed_ts"],
            resolution=row["resolution"] or "",
            attempts=row["attempts"],
            mode=row["mode"],
            fingerprint=row["fingerprint"] or "",
        )

    @staticmethod
    def _action(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "incident_id": row["incident_id"],
            "node": row["node"],
            "action": row["action"],
            "arguments": json.loads(row["arguments"]),
            "tier": row["tier"],
            "status": row["status"],
            "reason": row["reason"],
            "ruling": row["ruling"],
            "created_ts": row["created_ts"],
            "decided_ts": row["decided_ts"],
            "decided_by": row["decided_by"],
            "dispatched_ts": row["dispatched_ts"],
            "completed_ts": row["completed_ts"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "attempt": row["attempt"],
        }

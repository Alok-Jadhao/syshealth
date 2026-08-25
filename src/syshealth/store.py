"""Persistence for the fleet server.

SQLite, from the standard library. The previous design kept everything in a
module-level dict, which meant a server restart silently threw away every
measurement — including the ones a sizing decision was about to be made from.

Retention is enforced on write rather than by a background job, so there is no
second moving part to forget about.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node          TEXT    NOT NULL,
    instance_type TEXT,
    received_ts   REAL    NOT NULL,
    payload       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_node_ts ON samples (node, received_ts DESC);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (received_ts);

CREATE TABLE IF NOT EXISTS nodes (
    node          TEXT PRIMARY KEY,
    instance_type TEXT,
    address       TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
);
"""


class Store:
    """Thread-safe enough for Flask's default threaded server.

    One connection guarded by a lock. This is not built for high write volume;
    a fleet pushing every few seconds is comfortably within what SQLite
    handles, and the simplicity is worth more than the throughput here.
    """

    def __init__(
        self,
        path: str | Path = "syshealth.db",
        retention_s: float = 86_400,
        read_only: bool = False,
    ) -> None:
        """``read_only`` opens the file with SQLite's ``mode=ro``.

        For consumers that only ever observe — the MCP tool layer — this makes
        that guarantee structural rather than a promise about which methods
        they happen to call. It also turns a mistyped path into an error
        instead of a brand new empty database silently reporting an empty
        fleet, which is the more dangerous failure of the two.
        """
        self.path = str(path)
        self.retention_s = retention_s
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
        # WAL lets the dashboard read while agents are writing.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def record(
        self,
        node: str,
        payload: dict,
        instance_type: str | None = None,
        address: str | None = None,
        now: float | None = None,
    ) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples (node, instance_type, received_ts, payload) "
                "VALUES (?, ?, ?, ?)",
                (node, instance_type, now, json.dumps(payload, separators=(",", ":"))),
            )
            self._conn.execute(
                """
                INSERT INTO nodes (node, instance_type, address, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node) DO UPDATE SET
                    last_seen     = excluded.last_seen,
                    instance_type = COALESCE(excluded.instance_type, nodes.instance_type),
                    address       = COALESCE(excluded.address, nodes.address)
                """,
                (node, instance_type, address, now, now),
            )
            self._conn.execute(
                "DELETE FROM samples WHERE received_ts < ?", (now - self.retention_s,)
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    def nodes(self, online_window_s: float = 20.0, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes ORDER BY node"
            ).fetchall()
        return [
            {
                "node": r["node"],
                "instance_type": r["instance_type"],
                "address": r["address"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "seconds_since": round(now - r["last_seen"], 1),
                "online": (now - r["last_seen"]) <= online_window_s,
            }
            for r in rows
        ]

    def samples(self, node: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM samples WHERE node = ? "
                "ORDER BY received_ts DESC LIMIT ?",
                (node, limit),
            ).fetchall()
        # Query descends so LIMIT takes the newest; callers want oldest first.
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

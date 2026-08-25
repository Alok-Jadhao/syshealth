#!/usr/bin/env python3
"""Drive the incident loop end to end, through every execution mode.

The unit tests check each component. This checks the thing they compose into:
that a saturated node opens an incident, gets investigated, gets a diagnosis
citing evidence that was really collected, and then — depending on the mode —
is refused, queued for a human, or acted on and verified.

Built from the recorded runs, so it needs no kernel, no fleet, and no network,
and therefore runs on the macOS CI leg alongside everything else.

    python tools/sre_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from syshealth.mcp import build_fleet_tools, load_run  # noqa: E402
from syshealth.sre.detect import Detector  # noqa: E402
from syshealth.sre.incidents import ActionStatus, IncidentStore, Status  # noqa: E402
from syshealth.sre.loop import IncidentLoop  # noqa: E402
from syshealth.sre.policy import Decision, Mode, Policy  # noqa: E402
from syshealth.sre.reason import RuleReasoner  # noqa: E402
from syshealth.store import Store  # noqa: E402

RUNS = REPO / "tests" / "fixtures" / "runs"
FLEET = {
    "web-01": ("thrashing", "t3.micro"),
    "cache-01": ("cache-heavy", "t3.large"),
}

failures = 0


def check(condition: bool, label: str) -> None:
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}")
    failures += not condition


def telemetry(tmp: Path) -> Store:
    store = Store(tmp / "fleet.db")
    now = time.time()
    for node, (run, instance_type) in FLEET.items():
        samples = load_run(RUNS / f"{run}.jsonl")
        for index, sample in enumerate(samples):
            store.record(
                node, sample.to_dict(), instance_type, now=now - (len(samples) - index) * 2.0
            )
    return store


def loop_for(tmp: Path, store: Store, mode: Mode, name: str) -> IncidentLoop:
    return IncidentLoop(
        tools=build_fleet_tools(store),
        store=IncidentStore(tmp / f"incidents-{name}.db"),
        policy=Policy(
            mode=mode,
            autonomous_actions=frozenset({"restart_service"}),
            cooldown_s=0.0,
        ),
        reasoner=RuleReasoner(),
        detector=Detector(consecutive=1),
        managed={"web-01": {"service": "app"}},
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        store = telemetry(tmp)

        print("\ndetection")
        loop = loop_for(tmp, store, Mode.OBSERVE, "observe")
        opened = loop.detect()
        check(len(opened) >= 1, f"opened {len(opened)} incident(s) from recorded telemetry")
        check(loop.detect() == [], "the same symptom does not open a second incident")

        print("\nOBSERVE: investigates, refuses to act")
        result = loop.advance(opened[0])
        check(result.diagnosis is not None, "produced a diagnosis")
        check(result.decision is Decision.DENY, "policy refused the action")
        check(result.status is Status.ESCALATED, "escalated to a human")
        actions = loop.store.actions(opened[0])
        check(
            all(a["status"] == ActionStatus.DENIED.value for a in actions),
            "what it wanted to do is still recorded",
        )

        print("\nevidence discipline")
        report = loop.store.report(opened[0])
        diagnosis = report["diagnoses"][0]
        collected = {e["id"] for e in report["evidence"]}
        check(bool(diagnosis["cites"]), "the diagnosis cites evidence")
        check(set(diagnosis["cites"]) <= collected, "every citation resolves to a real result")
        check(bool(diagnosis["observations"]), "observations are recorded separately")

        print("\nASSIST: proposes, waits")
        loop = loop_for(tmp, store, Mode.ASSIST, "assist")
        incident_id = loop.detect()[0]
        result = loop.advance(incident_id)
        check(result.status is Status.AWAITING_APPROVAL, "waiting on a human")
        check(result.action is not None, f"proposed {result.action.describe()}")
        check(
            loop.store.claim_next_action("web-01") is None,
            "nothing is collectable while it waits",
        )

        print("\napproval")
        loop.store.set_action_status(result.action_id, ActionStatus.APPROVED, decided_by="smoke")
        after = loop.advance(incident_id)
        check(after.status is Status.REMEDIATING, "an approved action is queued")
        claimed = loop.store.claim_next_action("web-01")
        check(claimed is not None and claimed["id"] == result.action_id, "the node can collect it")
        check(loop.store.claim_next_action("web-01") is None, "and only once")

        print("\nverification")
        verifying = loop.report_result(result.action_id, ok=True)
        check(verifying.status is Status.VERIFYING, "a successful command does NOT resolve")
        final = loop.advance(incident_id)
        check(
            final.verification is not None and not final.verification.recovered,
            "still-saturated telemetry is correctly not called recovered",
        )
        check(final.status is not Status.RESOLVED, "so the incident stays open")

        print("\nloop termination")
        loop = loop_for(tmp, store, Mode.AUTONOMOUS, "auto")
        loop.detect()
        try:
            loop.run_until_settled(max_passes=20)
            settled = True
        except RuntimeError as exc:
            settled = False
            print(f"        {exc}")
        check(settled, "every incident reached a terminal state")
        check(
            all(i.status.terminal for i in loop.store.list_incidents()),
            "nothing is left spinning",
        )
        check(
            all(i.attempts <= 2 for i in loop.store.list_incidents()),
            "the attempt limit was respected",
        )

        print("\nthe false alarm")
        cache = [i for i in loop.store.list_incidents() if i.node == "cache-01"]
        for incident in cache:
            check(
                not loop.store.actions(incident.id),
                f"{incident.id} on cache-01 proposed no action against high utilisation",
            )

    if failures:
        raise SystemExit(f"\n{failures} check(s) failed")
    print("\nall checks passed")


if __name__ == "__main__":
    main()

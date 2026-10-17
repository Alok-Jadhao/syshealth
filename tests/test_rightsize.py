"""Right-sizing, load plans and prices.

    python3 -m unittest discover -s tests      # or: npm test

The fleet here is dev.py's simulated one, driven in simulated time: no
waiting, no agents, and the same samples the real server would hold.
"""

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import dev  # noqa: E402
import loadtest  # noqa: E402
import pricing  # noqa: E402
import rightsize  # noqa: E402
import server  # noqa: E402
from analyzer import THRESHOLDS  # noqa: E402


def run_test(spec, need=None, margin=None, tail=70, record=True):
    """Run `spec` on a fresh simulated fleet and return the report."""
    server.instances.clear()
    server.tests.clear()
    machines = [dev.FakeMachine(t, ram, base, idle, i)
                for i, (t, ram, base, idle) in enumerate(dev.PROFILES)]
    plan = loadtest.build_plan(spec)
    now = time.time()
    start = now - plan["total_sec"] - tail
    for k in range(12):  # a quiet minute first
        ts = start - 60 + k * 5
        for m in machines:
            dev.ingest(m, m.sample(ts), ts)
    if record:
        server.record_test("t1", plan)
    for m in machines:
        m.start_test("t1", plan, start)
    ts = start
    while ts <= now:
        for m in machines:
            dev.ingest(m, m.sample(ts), ts)
        ts += 5
    return rightsize.report(server.snapshot(), None, need, margin, server.tests)


def rows_by_type(report):
    return {r["instance_type"]: r for r in report["rows"]}


RAMP = {"mode": "variable", "pattern": "ramp", "low_mb": 200, "high_mb": 4000,
        "steps": 8, "step_sec": 60, "workers": 2, "need_mb": 1600}
PEAK = {"mode": "peak", "memory_mb": 1600, "duration_sec": 60, "workers": 2}


class RecommendationTest(unittest.TestCase):

    def test_ramp_past_the_need_picks_the_cheapest_size_with_room(self):
        report = run_test(RAMP)
        rows = rows_by_type(report)
        self.assertEqual(report["need_mb"], 1600)
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-medium")
        self.assertIn("t3.medium", report["recommendation"]["headline"])
        self.assertIn("$30.37", report["recommendation"]["headline"])
        # Capacity grows with the size, and the largest never slowed down.
        caps = [rows[t]["capacity"]["mb"] for t in ("t3.micro", "t3.small", "t3.medium")]
        self.assertEqual(caps, sorted(caps))
        self.assertEqual(rows["t3.large"]["capacity"]["kind"], "at_least")
        self.assertEqual(rows["t3.small"]["fit"], "too_small")
        self.assertEqual(rows["t3.medium"]["fit"], "fits")
        # The alternatives are priced from unrounded figures: no stray cent.
        self.assertIn("would save $15.18 a month", report["recommendation"]["lines"][0])

    def test_need_and_margin_change_the_answer_without_a_new_test(self):
        report = run_test(RAMP, need=1000, margin=10)
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-small")
        report = rightsize.report(server.snapshot(), None, 3400, 25, server.tests)
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-large")
        self.assertIn("not measured", report["recommendation"]["detail"])

    def test_peak_test_cannot_measure_the_room_left(self):
        report = run_test(PEAK)
        rows = rows_by_type(report)
        self.assertEqual(rows["t3.medium"]["fit"], "unmeasured")
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-medium")
        self.assertIn("ramp up past", report["recommendation"]["detail"])
        # With no margin asked for, holding the peak is enough.
        report = rightsize.report(server.snapshot(), None, None, 0, server.tests)
        self.assertEqual(rows_by_type(report)["t3.medium"]["fit"], "fits")

    def test_a_killed_level_counts_as_failed(self):
        # stress is killed on the micro; its memory is freed and the pressure
        # falls, so the readings alone would call the level fine.
        report = run_test(PEAK)
        micro = rows_by_type(report)["t3.micro"]
        self.assertTrue(micro["levels"][0]["killed"])
        self.assertEqual(micro["levels"][0]["state"], "CRITICAL")
        self.assertEqual(micro["outcome"]["key"], "critical")
        self.assertEqual(micro["fit"], "too_small")

    def test_a_test_that_stops_short_says_so(self):
        spec = dict(RAMP, high_mb=1000, steps=3)
        report = run_test(spec, need=1600)
        self.assertIsNone(report["recommendation"]["pick"])
        self.assertIn("stopped short", report["recommendation"]["headline"])

    def test_nothing_big_enough(self):
        spec = dict(RAMP, low_mb=2000, high_mb=12000, steps=4)
        report = run_test(spec, need=9000)
        self.assertIsNone(report["recommendation"]["pick"])
        self.assertIn("None of the tested sizes", report["recommendation"]["headline"])

    def test_unrecorded_test_judges_against_its_peak(self):
        # A server restarted mid-test has lost the plan; the samples still say
        # what was held.
        report = run_test(PEAK, record=False)
        self.assertEqual(report["need_mb"], 1600)
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-medium")

    def test_running_test_withholds_the_verdict(self):
        report = run_test(RAMP, tail=-120)
        self.assertTrue(report["running"])
        self.assertIsNone(report["recommendation"]["pick"])

    def test_no_test_at_all(self):
        server.instances.clear()
        self.assertEqual(rightsize.report(server.snapshot()), {"test_id": None})


class CapacityTest(unittest.TestCase):

    def level(self, mb, some, full=0.0, killed=False):
        lv = {"mb": mb, "some": some, "full": full, "killed": killed}
        lv["state"] = rightsize.band(lv, THRESHOLDS)
        return lv

    def test_interpolates_where_the_degraded_line_is_crossed(self):
        cap = rightsize.capacity([self.level(1000, 2), self.level(2000, 18)], THRESHOLDS)
        self.assertEqual(cap, {"mb": 1500, "kind": "estimate", "held_mb": 1000, "failed_mb": 2000})

    def test_full_pressure_can_cross_first(self):
        cap = rightsize.capacity([self.level(1000, 2, 0), self.level(2000, 12, 40)], THRESHOLDS)
        self.assertEqual(cap["mb"], 1250)

    def test_killed_level_gives_only_what_was_held(self):
        cap = rightsize.capacity([self.level(1000, 2), self.level(2000, 3, killed=True)], THRESHOLDS)
        self.assertEqual(cap["mb"], 1000)

    def test_kinds(self):
        self.assertEqual(rightsize.capacity([self.level(500, 1), self.level(900, 2)], THRESHOLDS)["kind"],
                         "at_least")
        self.assertEqual(rightsize.capacity([self.level(500, 30)], THRESHOLDS)["kind"], "below")
        self.assertIsNone(rightsize.capacity([], THRESHOLDS))

    def test_fit(self):
        est = {"mb": 2000, "kind": "estimate"}
        self.assertEqual(rightsize.fit(est, 1500, 1875), "fits")
        self.assertEqual(rightsize.fit(est, 1800, 2250), "tight")
        self.assertEqual(rightsize.fit(est, 2500, 3125), "too_small")
        top = {"mb": 2000, "kind": "at_least"}
        self.assertEqual(rightsize.fit(top, 1500, 1875), "fits")
        self.assertEqual(rightsize.fit(top, 1800, 2250), "unmeasured")
        self.assertEqual(rightsize.fit(top, 2500, 3125), "untested")
        low = {"mb": 2000, "kind": "below"}
        self.assertEqual(rightsize.fit(low, 2500, 3125), "too_small")
        self.assertEqual(rightsize.fit(low, 1000, 1250), "untested")


class PlanTest(unittest.TestCase):

    def test_peak_need_is_what_it_holds(self):
        self.assertEqual(loadtest.build_plan(PEAK)["need_mb"], 1600)

    def test_variable_need_is_checked_and_optional(self):
        self.assertEqual(loadtest.build_plan(RAMP)["need_mb"], 1600)
        self.assertIsNone(loadtest.build_plan(dict(RAMP, need_mb=""))["need_mb"])
        with self.assertRaises(loadtest.PlanError):
            loadtest.build_plan(dict(RAMP, need_mb="1.5k"))
        with self.assertRaises(loadtest.PlanError):
            loadtest.build_plan(dict(RAMP, need_mb=99999))

    def test_agent_keeps_the_need_through_validation(self):
        plan = loadtest.build_plan(RAMP)
        self.assertEqual(loadtest.validate_plan(json.loads(json.dumps(plan)))["need_mb"], 1600)


class PricingTest(unittest.TestCase):

    def tearDown(self):
        os.environ.pop("SYSHEALTH_PRICES_FILE", None)
        pricing._table = None

    def test_list_price(self):
        price = pricing.lookup("t3.micro", "us-east-1")
        self.assertEqual(price["usd_hour"], 0.0104)
        self.assertEqual(price["usd_month"], 7.59)
        self.assertIsNone(pricing.lookup("unknown"))
        self.assertIsNone(pricing.lookup("z9.huge", "us-east-1"))

    def test_your_prices_win(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"t3.micro": 0.005, "ap-south-1": {"t3.small": 0.01}}, f)
        try:
            os.environ["SYSHEALTH_PRICES_FILE"] = f.name
            pricing._table = None
            self.assertEqual(pricing.lookup("t3.micro", "us-east-1")["usd_hour"], 0.005)
            self.assertEqual(pricing.lookup("t3.small", "ap-south-1")["usd_hour"], 0.01)
            self.assertEqual(pricing.lookup("t3.small", "us-east-1")["usd_hour"], 0.0208)
        finally:
            os.unlink(f.name)


class EndpointTest(unittest.TestCase):

    def setUp(self):
        self.client = server.app.test_client()

    def test_report_endpoint(self):
        run_test(RAMP)
        body = self.client.get("/report?need_mb=1200&margin=30").get_json()
        self.assertEqual(body["need_mb"], 1200)
        self.assertEqual(body["margin_pct"], 30)
        self.assertEqual(body["required_mb"], 1560)
        self.assertEqual(self.client.get("/report?need_mb=abc").status_code, 400)
        self.assertEqual(self.client.get("/report?margin=500").status_code, 400)

    def test_load_plan_preview_carries_the_need(self):
        body = self.client.post("/load-plan", json=RAMP).get_json()
        self.assertEqual(body["plan"]["need_mb"], 1600)


if __name__ == "__main__":
    unittest.main()

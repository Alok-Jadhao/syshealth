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


def run_test(spec, need=None, margin=None, tail=70, record=True, drop=None, stop_after=None):
    """Run `spec` on a fresh simulated fleet and return the report.

    drop: {instance type: seconds}, when that size's agent loses the test
    (it restarted) but reports on. stop_after: seconds, when Stop is pressed.
    """
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
            if drop and ts >= start + drop.get(m.instance_type, float("inf")):
                m.stop_test(ts)
            dev.ingest(m, m.sample(ts), ts)
        if stop_after is not None and ts >= start + stop_after:
            server.record_stop(ts)
            for m in machines:
                m.stop_test(ts)
            stop_after = None
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
        self.assertIn("$32.70", report["recommendation"]["headline"])
        # Capacity grows with the size, and the largest never slowed down.
        caps = [rows[t]["capacity"]["mb"] for t in ("t3.micro", "t3.small", "t3.medium")]
        self.assertEqual(caps, sorted(caps))
        self.assertEqual(rows["t3.large"]["capacity"]["kind"], "at_least")
        self.assertEqual(rows["t3.small"]["fit"], "too_small")
        self.assertEqual(rows["t3.medium"]["fit"], "fits")
        # The alternatives are priced from unrounded figures: no stray cent.
        self.assertIn("would save $16.35 a month", report["recommendation"]["lines"][0])

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

    def test_a_size_that_drops_out_says_so(self):
        # Its agent restarted part-way through (systemd stopped the unit when
        # the OOM killer took stress): it reports on, without the test.
        report = run_test(RAMP, drop={"t3.large": 150, "t3.micro": 300})
        rows = rows_by_type(report)
        large, micro = rows["t3.large"], rows["t3.micro"]
        self.assertEqual(large["dropped"]["step"], 3)
        self.assertEqual(large["outcome"], {"key": "unknown",
                                            "text": "Stopped reporting at step 3 of 8"})
        # A size that had already given out keeps that, and says the rest.
        self.assertEqual(micro["outcome"]["key"], "critical")
        self.assertTrue(micro["outcome"]["text"].endswith("stopped reporting at step 5 of 8"),
                        micro["outcome"]["text"])
        self.assertIsNone(rows["t3.medium"]["dropped"])
        self.assertEqual(report["recommendation"]["pick"], "demo-t3-medium")

    def test_a_stopped_test_is_not_a_drop_out(self):
        report = run_test(RAMP, stop_after=150)
        self.assertIn("stopped", server.tests["t1"])
        for row in report["rows"]:
            self.assertIsNone(row["dropped"], row["instance_type"])

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


class CpuTest(unittest.TestCase):
    """The app's average CPU against each size's vCPUs and CPU credits."""

    def test_unlimited_sizes_pay_for_cpu_above_their_baseline(self):
        cpu = rightsize.cpu_check("t3.micro", 0.5, 25)
        self.assertTrue(cpu["fits"])
        self.assertEqual(cpu["baseline_vcpu"], 0.2)
        # (0.5 - 0.2) vCPU x $0.05 a vCPU-hour x 730 hours
        self.assertAlmostEqual(cpu["extra_usd_month"], 10.95)
        self.assertEqual(rightsize.cpu_check("t3.large", 0.5, 25)["extra_usd_month"], 0)
        # t4g credits are cheaper
        self.assertAlmostEqual(rightsize.cpu_check("t4g.micro", 0.5, 25)["extra_usd_month"], 8.76)

    def test_standard_mode_holds_a_t2_to_its_baseline(self):
        cpu = rightsize.cpu_check("t2.micro", 0.2, 25)
        self.assertFalse(cpu["fits"])
        self.assertIn("held to 0.1 vCPU", cpu["text"])
        self.assertTrue(rightsize.cpu_check("t2.large", 0.4, 25)["fits"])

    def test_too_few_vcpus(self):
        self.assertFalse(rightsize.cpu_check("t3.large", 1.7, 25)["fits"])   # needs 2.125
        self.assertTrue(rightsize.cpu_check("m7i.large", 1.5, 25)["fits"])
        self.assertEqual(rightsize.cpu_check("m7i.large", 1.5, 25)["extra_usd_month"], 0)
        self.assertIsNone(rightsize.cpu_check("x9.huge", 0.5, 25)["fits"])

    def test_report_prices_the_credits_in(self):
        plain = run_test(RAMP)
        report = rightsize.report(server.snapshot(), None, 1600, 25, server.tests, cpu_vcpu=0.9)
        self.assertEqual(report["cpu_vcpu"], 0.9)
        rec, before = report["recommendation"], plain["recommendation"]
        self.assertEqual(rec["pick"], before["pick"])
        medium = rows_by_type(report)["t3.medium"]
        self.assertAlmostEqual(medium["cpu"]["extra_usd_month"], 18.25)
        self.assertIn("$50.95", rec["headline"])   # $32.70 + $18.25
        self.assertTrue(any("CPU credits" in line for line in rec["lines"]))
        self.assertIn("CPU credits", report["prices"]["note"])
        self.assertIsNone(rows_by_type(plain)["t3.medium"]["cpu"])

    def test_no_size_with_the_cpu(self):
        run_test(RAMP)
        report = rightsize.report(server.snapshot(), None, 1600, 25, server.tests, cpu_vcpu=1.7)
        rec = report["recommendation"]
        self.assertIsNone(rec["pick"])
        self.assertIn("has the CPU", rec["headline"])

    def test_a_cheaper_size_short_of_cpu_is_passed_over(self):
        def row(itype, host):
            return {"hostname": host, "instance_type": itype, "mem_total_mb": 4096,
                    "capacity": {"mb": 3000, "kind": "estimate", "held_mb": 2800, "failed_mb": 3200},
                    "fit": "fits", "spare_pct": 87, "levels": [],
                    "price": pricing.lookup(itype, "us-east-1"),
                    "cpu": rightsize.cpu_check(itype, 0.5, 25)}
        rows = [row("t2.medium", "a"), row("t3.medium", "b")]
        # $33.87 for the t2 against $30.37 + $3.65 of credits for the t3: the
        # t2 is cheaper, but held to 0.4 vCPU once its credits run out.
        rec = rightsize.recommend(rows, 1600, 25)
        self.assertEqual(rec["pick"], "b")
        self.assertTrue(any(line.startswith("t2.medium would save") and "CPU falls short" in line
                            for line in rec["lines"]))


class UntestedSizesTest(unittest.TestCase):
    """The same memory for less, on a size the test did not run."""

    def row(self, itype, host, region="ap-south-1", cpu=None):
        return {"hostname": host, "instance_type": itype, "mem_total_mb": 4096,
                "capacity": {"mb": 3200, "kind": "at_least", "held_mb": 3200, "failed_mb": None},
                "fit": "fits", "spare_pct": 60, "levels": [],
                "price": pricing.lookup(itype, region),
                "cpu": rightsize.cpu_check(itype, cpu, 25) if cpu else None}

    def test_processor(self):
        for itype, maker in (("t3.medium", "Intel"), ("t4g.medium", "Graviton"), ("t3a.small", "AMD"),
                             ("c6gn.large", "Graviton"), ("m6a.large", "AMD"), ("m7i.large", "Intel")):
            self.assertEqual(pricing.processor(itype), maker, itype)

    def test_same_memory_and_vcpus_cheapest_first(self):
        same = pricing.same_memory("t3.medium", "ap-south-1")
        self.assertEqual([p["instance_type"] for p in same[:2]], ["t4g.medium", "t3a.medium"])
        self.assertEqual([p["usd_month"] for p in same[:2]], [16.35, 17.96])
        for p in same:
            self.assertEqual(pricing.specs(p["instance_type"])["memory_gib"], 4.0)
            self.assertGreaterEqual(pricing.specs(p["instance_type"])["vcpu"], 2)
        self.assertNotIn("m6g.medium", [p["instance_type"] for p in same])   # one vCPU
        self.assertEqual(pricing.same_memory("z9.huge"), [])

    def test_the_verdict_names_them(self):
        rec = run_test(RAMP)["recommendation"]
        self.assertEqual(rec["headline"], "Choose t3.medium · $32.70 a month")
        self.assertEqual([u["instance_type"] for u in rec["untested"]], ["t4g.medium", "t3a.medium"])
        self.assertIn("Not tested, with the same 4 GiB: t4g.medium (Graviton) at $16.35 a month and "
                      "t3a.medium (AMD) at $17.96. Add them to a test before switching; Graviton is "
                      "Arm, so your app needs an Arm build.", rec["lines"])

    def test_a_size_in_the_test_is_not_named(self):
        rows = [self.row("t4g.medium", "a"), self.row("t3.medium", "b")]
        rows[0]["fit"] = "too_small"
        rec = rightsize.recommend(rows, 1600, 25)
        self.assertEqual(rec["pick"], "b")
        self.assertEqual([u["instance_type"] for u in rec["untested"]], ["t3a.medium", "c6g.large"])
        self.assertNotIn("t4g.medium", rec["lines"][-1])

    def test_cpu_credits_are_priced_in(self):
        # At 0.9 vCPU every burstable size pays for credits, so the c6g.large
        # (no credits) moves up between the t4g.medium and the t3a.medium.
        rows = [self.row("t3.medium", "b", cpu=0.9)]
        rec = rightsize.recommend(rows, 1600, 25, cpu_vcpu=0.9)
        self.assertEqual(rec["headline"], "Choose t3.medium · $50.95 a month")
        self.assertEqual(rec["untested"], [
            {"instance_type": "t4g.medium", "usd_month": 30.95, "processor": "Graviton"},
            {"instance_type": "c6g.large", "usd_month": 31.1, "processor": "Graviton"}])

    def test_nothing_cheaper_nothing_said(self):
        rec = rightsize.recommend([self.row("t4g.medium", "a")], 1600, 25)
        self.assertEqual(rec["untested"], [])
        self.assertFalse(any(line.startswith("Not tested") for line in rec["lines"]))
        unpriced = self.row("t3.medium", "b")
        unpriced["price"] = None
        self.assertEqual(rightsize.recommend([unpriced], 1600, 25)["untested"], [])


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
        for bad in ("abc", "0", "-1", "500"):
            self.assertEqual(self.client.get("/report?cpu=" + bad).status_code, 400, bad)
        self.assertEqual(self.client.get("/report?cpu=0.5").get_json()["cpu_vcpu"], 0.5)

    def test_load_plan_preview_carries_the_need(self):
        body = self.client.post("/load-plan", json=RAMP).get_json()
        self.assertEqual(body["plan"]["need_mb"], 1600)


if __name__ == "__main__":
    unittest.main()

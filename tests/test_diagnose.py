"""The diagnosis: where the memory is, the likely cause, and leaks."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import diagnose  # noqa: E402
import rightsize  # noqa: E402
from analyzer import THRESHOLDS  # noqa: E402

GB = 1024 * 1024  # kB


def meminfo(total_gb=2, free_mb=100, avail_mb=300, anon_mb=1500, cached_mb=200, swap_mb=0, swap_used_mb=0):
    return {"MemTotal": total_gb * GB, "MemFree": free_mb * 1024, "MemAvailable": avail_mb * 1024,
            "AnonPages": anon_mb * 1024, "Cached": cached_mb * 1024, "Buffers": 0, "Shmem": 0,
            "SwapTotal": swap_mb * 1024, "SwapFree": (swap_mb - swap_used_mb) * 1024}


DEGRADED = {"avg_psi": 15.0, "avg_psi_full": 0.0, "state": "DEGRADED"}
CALM = {"avg_psi": 0.2, "avg_psi_full": 0.0, "state": "HEALTHY"}


def mb_s(mb_per_s):
    """Pages per second for a rate in MB/s."""
    return mb_per_s * 1024.0 / diagnose.PAGE_KB


class BreakdownTest(unittest.TestCase):

    def test_parts_add_up_to_ram(self):
        m = diagnose.breakdown(meminfo())
        self.assertEqual(m["total_mb"], 2048)
        self.assertEqual(m["programs_mb"] + m["cache_mb"] + m["other_mb"] + m["free_mb"], 2048)
        self.assertEqual(m["used_pct"], round((2048 - 300) * 100 / 2048, 1))

    def test_shared_memory_is_not_cache(self):
        info = meminfo(cached_mb=500)
        info["Shmem"] = 300 * 1024
        self.assertEqual(diagnose.breakdown(info)["cache_mb"], 200)

    def test_nothing_without_meminfo(self):
        self.assertIsNone(diagnose.breakdown({}))


class CauseTest(unittest.TestCase):

    def classify(self, rates, info, analysis=DEGRADED):
        return diagnose.classify(rates, diagnose.breakdown(info), analysis["avg_psi"],
                                 analysis["avg_psi_full"], analysis["state"], THRESHOLDS)

    def test_nothing_to_explain_when_healthy(self):
        self.assertIsNone(self.classify({"pswpin": mb_s(50)}, meminfo(), CALM))

    def test_swap(self):
        cause = self.classify({"pswpin": mb_s(5)}, meminfo(swap_mb=1024, swap_used_mb=600))
        self.assertEqual(cause["key"], "swap")
        self.assertIn("600 MB is in swap", cause["text"])

    def test_programs_fill_ram(self):
        cause = self.classify({"workingset_refault_file": mb_s(3)}, meminfo(anon_mb=1700))
        self.assertEqual(cause["key"], "programs")

    def test_cache_does_not_fit(self):
        cause = self.classify({"workingset_refault_file": mb_s(3)}, meminfo(anon_mb=500, cached_mb=1300))
        self.assertEqual(cause["key"], "cache")

    def test_reclaim_and_unclear(self):
        self.assertEqual(self.classify({"pgscan_direct": 500}, meminfo())["key"], "reclaim")
        self.assertEqual(self.classify({}, meminfo())["key"], "unclear")


class RatesTest(unittest.TestCase):

    def test_per_second_and_old_kernels(self):
        d = diagnose.Diagnoser()
        self.assertEqual(d.rates({"pswpin": 100, "workingset_refault": 1000}, 0), {})
        rates = d.rates({"pswpin": 600, "workingset_refault": 2000}, 5)
        self.assertEqual(rates["pswpin"], 100)
        # Before 5.9 there is one refault counter; it is read as file refaults.
        self.assertEqual(rates["workingset_refault_file"], 200)


class LeakTest(unittest.TestCase):

    def run_minutes(self, sizes, name="java app.jar", exclude=()):
        d = diagnose.Diagnoser()
        memory = diagnose.breakdown(meminfo(avail_mb=600))
        leak = None
        for minute, rss in enumerate(sizes):
            top = [{"pid": 42, "name": name, "rss_mb": rss}, {"pid": 7, "name": "sshd", "rss_mb": 8}]
            leak = d.leak(top, memory, minute * 60.0, exclude)
        return leak

    def test_steady_growth_is_a_leak(self):
        leak = self.run_minutes([400 + 30 * m for m in range(8)])
        self.assertEqual(leak["name"], "java app.jar")
        self.assertEqual(leak["mb_per_min"], 30.0)
        self.assertEqual(leak["runs_out_min"], 20)  # 600 MB available at 30 MB a minute
        self.assertIn("runs out of memory in about 20 minutes", leak["text"])

    def test_flat_or_up_and_down_is_not(self):
        self.assertIsNone(self.run_minutes([400] * 8))
        self.assertIsNone(self.run_minutes([400, 600, 380, 610, 390, 620, 400, 640]))

    def test_small_growth_is_not(self):
        self.assertIsNone(self.run_minutes([400 + 5 * m for m in range(8)]))

    def test_too_soon_to_say(self):
        self.assertIsNone(self.run_minutes([400 + 50 * m for m in range(3)]))

    def test_the_load_test_is_not_a_leak(self):
        self.assertIsNone(self.run_minutes([100 + 100 * m for m in range(8)], name="stress",
                                           exclude=("stress",)))


class ProcessTest(unittest.TestCase):

    def test_interpreters_are_named_by_their_script(self):
        self.assertEqual(diagnose.label("python3", ["python3", "-u", "/opt/app/server.py"]),
                         "python3 server.py")
        self.assertEqual(diagnose.label("python3.12", ["python3.12", "worker.py"]),
                         "python3.12 worker.py")
        self.assertEqual(diagnose.label("nginx", ["nginx: worker process"]), "nginx")

    def test_reads_a_proc_tree(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, name, rss, cmd in ((1, "init", 9000, b"init"), (2, "kthreadd", None, b""),
                                        (30, "node", 512000, b"node\0server.js")):
                os.mkdir(os.path.join(proc, str(pid)))
                with open(os.path.join(proc, str(pid), "status"), "w") as f:
                    f.write("Name:\t%s\n" % name + ("VmRSS:\t%d kB\n" % rss if rss else ""))
                with open(os.path.join(proc, str(pid), "cmdline"), "wb") as f:
                    f.write(cmd)
            top = diagnose.top_processes(proc=proc)
        self.assertEqual([p["name"] for p in top], ["node server.js", "init"])
        self.assertEqual(top[0]["rss_mb"], 500)


class UsageRuleTest(unittest.TestCase):

    def row(self, host, itype, hourly, used, some, killed=False):
        return {"hostname": host, "instance_type": itype, "mem_total_mb": 1,
                "price": {"usd_hour": hourly, "usd_month": round(hourly * 730, 2)},
                "levels": [{"mb": 1000, "some": some, "full": 0, "killed": killed, "used_pct": used}],
                "used_at_need": 100.0 if killed else used, "killed": killed}

    def test_agrees(self):
        rows = [self.row("a", "t3.small", 0.0208, 90, 12), self.row("b", "t3.medium", 0.0416, 40, 0.1)]
        rule = rightsize.usage_rule(rows, 1000, "b")
        self.assertTrue(rule["agrees"])

    def test_over_buys_when_full_but_not_waiting(self):
        rows = [self.row("a", "t3.small", 0.0208, 88, 0.3), self.row("b", "t3.medium", 0.0416, 40, 0.1)]
        rule = rightsize.usage_rule(rows, 1000, "a")
        self.assertFalse(rule["agrees"])
        self.assertEqual(rule["pick"], "b")
        self.assertIn("$15.18 a month more", rule["text"])
        self.assertIn("88% in use", rule["text"])

    def test_under_buys_when_roomy_but_waiting(self):
        rows = [self.row("a", "t3.small", 0.0208, 35, 14), self.row("b", "t3.medium", 0.0416, 20, 0.1)]
        rule = rightsize.usage_rule(rows, 1000, "b")
        self.assertEqual(rule["pick"], "a")
        self.assertIn("waited for memory 14.0% of the time", rule["text"])

    def test_a_kill_at_the_need_counts_as_full(self):
        row = self.row("a", "t3.micro", 0.0104, 30, 2, killed=True)
        self.assertEqual(rightsize.used_at(row, 1000), 100.0)


if __name__ == "__main__":
    unittest.main()

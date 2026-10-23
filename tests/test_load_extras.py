"""Background work and extra swap: a test that makes each box behave like a real server."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cli  # noqa: E402
import dev  # noqa: E402
import loadtest  # noqa: E402
import mcp_server  # noqa: E402
import syshealth  # noqa: E402

RAMP = {"mode": "variable", "pattern": "ramp", "low_mb": 200, "high_mb": 3200,
        "steps": 6, "step_sec": 60, "workers": 2}


class PlanExtrasTest(unittest.TestCase):

    def test_off_unless_asked(self):
        plan = loadtest.build_plan(RAMP)
        self.assertFalse(plan["background_cpu"])
        self.assertEqual(plan["swap_mb"], 0)
        self.assertNotIn("background", plan["summary"])
        self.assertTrue(all(c.startswith("stress --vm") for c in loadtest.commands(plan)))

    def test_form_values_are_accepted(self):
        for on in (True, "true", "on", 1):
            self.assertTrue(loadtest.build_plan(dict(RAMP, background_cpu=on))["background_cpu"])
        for off in (False, "false", "off", 0, "", None):
            self.assertFalse(loadtest.build_plan(dict(RAMP, background_cpu=off))["background_cpu"])
        self.assertEqual(loadtest.build_plan(dict(RAMP, swap_mb="2048"))["swap_mb"], 2048)
        self.assertEqual(loadtest.build_plan(dict(RAMP, swap_mb=""))["swap_mb"], 0)

    def test_bad_values_are_refused(self):
        for bad in ({"background_cpu": "yes please"}, {"background_cpu": [1]},
                    {"swap_mb": -1}, {"swap_mb": 4097}, {"swap_mb": "2k"}, {"swap_mb": 1.5}):
            with self.subTest(bad), self.assertRaises(loadtest.PlanError):
                loadtest.build_plan(dict(RAMP, **bad))

    def test_the_agent_rechecks_them(self):
        plan = loadtest.build_plan(dict(RAMP, background_cpu=True, swap_mb=1024))
        self.assertEqual(loadtest.validate_plan(json.loads(json.dumps(plan))), plan)
        with self.assertRaises(loadtest.PlanError):
            loadtest.validate_plan(dict(plan, swap_mb=999999))

    def test_summary_and_commands_say_what_was_added(self):
        plan = loadtest.build_plan(loadtest.THREE_STATES_SPEC)
        self.assertEqual(plan["summary"], "Ramp up: 2.1 GB to 2.3 GB in 5 steps of 75 s"
                                          " · with background work · 2 GB extra swap")
        commands = loadtest.commands(plan)
        self.assertIn("2 GB more swap", commands[0])
        self.assertEqual(commands[1].split("#")[0].strip(),
                         "nice -n 19 stress --cpu $(nproc) --timeout 375s")
        self.assertEqual(len(commands), 2 + len(plan["phases"]))

    def test_cli_and_mcp_send_them(self):
        def args(**kw):
            base = dict(peak=None, ramp=None, wave=None, bursts=None, duration=60, steps=5,
                        step_sec=75, need=None, workers=8, background=False, swap=0)
            return argparse.Namespace(**dict(base, **kw))

        spec = cli.spec_from(args(ramp="1800:2600", background=True, swap=2048))
        self.assertTrue(spec["background_cpu"])
        self.assertEqual(spec["swap_mb"], 2048)
        self.assertNotIn("swap_mb", cli.spec_from(args(peak=800)))
        spec = mcp_server.load_spec({"shape": "ramp", "low_mb": 1800, "high_mb": 2600,
                                     "background_cpu": True, "swap_mb": 2048})
        self.assertTrue(spec["background_cpu"])
        self.assertEqual(spec["swap_mb"], 2048)


class TestSwapTest(unittest.TestCase):
    """The agent's swap file: added for a test, taken away after it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test-swap")

    def tearDown(self):
        self.dir.cleanup()

    def test_adds_with_fallocate_mkswap_swapon(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv[0])
            if argv[0] == "fallocate":
                open(self.path, "w").close()
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(syshealth.subprocess, "run", side_effect=run):
            self.assertIsNone(syshealth.add_test_swap(64, self.path))
        self.assertEqual(calls, ["fallocate", "mkswap", "swapon"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_refuses_to_fill_the_disk(self):
        fake = mock.Mock(f_bavail=1500, f_frsize=1024 * 1024)
        with mock.patch.object(syshealth.os, "statvfs", return_value=fake), \
                mock.patch.object(syshealth.subprocess, "run") as run:
            why = syshealth.add_test_swap(2048, self.path)
        self.assertIn("1500 MB of disk free", why)
        run.assert_not_called()

    def test_a_failure_says_why_and_leaves_nothing(self):
        def run(argv, **kw):
            if argv[0] == "fallocate":
                open(self.path, "w").close()
                return subprocess.CompletedProcess(argv, 0)
            raise subprocess.CalledProcessError(1, argv, stderr=b"swapon: Operation not permitted")

        with mock.patch.object(syshealth.subprocess, "run", side_effect=run):
            why = syshealth.add_test_swap(64, self.path)
        self.assertIn("not permitted", why)
        self.assertFalse(os.path.exists(self.path))

    def test_removes_with_swapoff(self):
        open(self.path, "w").close()
        listed = [[self.path], []]
        with mock.patch.object(syshealth, "_swaps", side_effect=lambda: listed.pop(0)), \
                mock.patch.object(syshealth.subprocess, "run") as run:
            syshealth.remove_test_swap(self.path)
        self.assertEqual(run.call_args[0][0], ["swapoff", self.path])
        self.assertFalse(os.path.exists(self.path))

    def test_swap_still_in_use_is_left_in_place(self):
        open(self.path, "w").close()
        with mock.patch.object(syshealth, "_swaps", return_value=[self.path]), \
                mock.patch.object(syshealth.subprocess, "run"):
            syshealth.remove_test_swap(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_nothing_to_remove(self):
        with mock.patch.object(syshealth.subprocess, "run") as run:
            syshealth.remove_test_swap(self.path)
        run.assert_not_called()


class RunnerExtrasTest(unittest.TestCase):
    """The runner starts both around the load and always cleans them up."""

    def run_plan(self, plan):
        runner = syshealth.LoadRunner()
        seen = {}
        real_wait = runner._stop.wait

        def wait(timeout):
            seen["status"] = runner.status()
            return real_wait(timeout)

        runner._stop.wait = wait
        with mock.patch.object(syshealth, "add_test_swap", return_value=None) as add, \
                mock.patch.object(syshealth, "remove_test_swap") as remove, \
                mock.patch.object(loadtest, "background_argv", return_value=["sleep", "30"]):
            started = time.time()
            runner._run("t1", plan)
            self.assertLess(time.time() - started, 5)
        return seen["status"], add, remove

    def test_background_and_swap(self):
        plan = {"total_sec": 1, "phases": [{"mb": 0, "seconds": 1}], "mode": "peak",
                "pattern": None, "summary": "x", "workers": 1,
                "background_cpu": True, "swap_mb": 512}
        with mock.patch.object(syshealth, "_killpg", wraps=syshealth._killpg) as killpg:
            status, add, remove = self.run_plan(plan)
        add.assert_called_once_with(512)
        remove.assert_called_once_with()
        self.assertTrue(status["background_cpu"])
        self.assertEqual(status["swap_added_mb"], 512)
        background = killpg.call_args[0][0]
        self.assertIsNotNone(background.poll())   # the sleep is gone

    def test_neither_when_not_asked(self):
        plan = {"total_sec": 1, "phases": [{"mb": 0, "seconds": 1}], "mode": "peak",
                "pattern": None, "summary": "x", "workers": 1}
        status, add, remove = self.run_plan(plan)
        add.assert_not_called()
        remove.assert_not_called()
        self.assertFalse(status["background_cpu"])
        self.assertEqual(status["swap_added_mb"], 0)


class DemoThreeStatesTest(unittest.TestCase):
    """The demo shows the picture the preset is for, as the real fleet did."""

    def test_critical_degraded_healthy_at_once(self):
        plan = loadtest.build_plan(loadtest.THREE_STATES_SPEC)
        machines = {p[0]: dev.FakeMachine(*p, i) for i, p in enumerate(dev.PROFILES)}
        start = 1_000_000.0
        for m in machines.values():
            m.start_test("three", plan, start)
        seen = []
        for t in range(0, plan["total_sec"], 5):
            states = {name: m.sample(start + t)["state"] for name, m in machines.items()}
            seen.append(states)
        want = {"t3.micro": "CRITICAL", "t3.small": "DEGRADED",
                "t3.medium": "HEALTHY", "t3.large": "HEALTHY"}
        self.assertGreaterEqual(sum(s == want for s in seen), 20)   # 100 s or more
        self.assertFalse(any(m.killed_phases for m in machines.values()))


if __name__ == "__main__":
    unittest.main()

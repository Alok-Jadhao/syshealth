"""Plan it from your users: users at once and memory per user, turned into a test.

    python3 -m unittest discover -s tests      # or: npm test
"""

import argparse
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cli  # noqa: E402
import loadtest  # noqa: E402
import server  # noqa: E402


def asked(**changes):
    spec = dict(loadtest.USERS_DEFAULTS)
    spec.update(changes)
    return spec


class UsersPlanTest(unittest.TestCase):

    def test_the_defaults_are_the_default_test(self):
        planned = loadtest.users_plan(loadtest.USERS_DEFAULTS)
        spec = planned["spec"]
        # 300 + 650 x 2 at the busiest, 300 + 200 x 2 on a normal day, to twice the busiest.
        self.assertEqual(planned["need_mb"], 1600)
        self.assertEqual((spec["mode"], spec["pattern"]), ("variable", "ramp"))
        self.assertEqual((spec["need_mb"], spec["low_mb"], spec["high_mb"]), (1600, 700, 3200))
        self.assertEqual((spec["steps"], spec["step_sec"], spec["workers"]), (6, 60, 2))
        self.assertFalse(spec["background_cpu"])
        self.assertEqual(spec["swap_mb"], 0)
        plan = loadtest.build_plan(spec)
        self.assertEqual([p["mb"] for p in plan["phases"]], [700, 1200, 1700, 2200, 2700, 3200])

    def test_the_sums_are_shown(self):
        lines = loadtest.users_plan(loadtest.USERS_DEFAULTS)["lines"]
        self.assertIn("300 MB + 650 users × 2 MB = 1600 MB", lines[0])
        self.assertIn("300 MB + 200 users × 2 MB = 700 MB", lines[1])
        self.assertIn("3200 MB, twice the busiest", lines[2])

    def test_form_strings_and_fractions(self):
        planned = loadtest.users_plan({"normal_users": "50", "busiest_users": "400",
                                       "app_mb": "120", "mb_per_user": "0.5"})
        self.assertEqual(planned["need_mb"], 320)
        self.assertEqual((planned["spec"]["low_mb"], planned["spec"]["high_mb"]), (145, 640))
        self.assertIn("0.5 MB", planned["lines"][0])

    def test_a_big_app_stops_at_the_test_limit(self):
        planned = loadtest.users_plan(asked(busiest_users=4000, mb_per_user=3, app_mb=400))
        self.assertEqual(planned["need_mb"], 12400)
        self.assertEqual(planned["spec"]["high_mb"], loadtest.LIMITS["max_mb"])
        self.assertNotIn("twice the busiest", planned["lines"][2])
        loadtest.build_plan(planned["spec"])

    def test_steps_can_be_changed(self):
        planned = loadtest.users_plan(asked(steps=4, step_sec=90))
        self.assertEqual((planned["spec"]["steps"], planned["spec"]["step_sec"]), (4, 90))
        self.assertIn("4 steps of 90 s", planned["lines"][2])

    def test_what_cannot_be_planned_is_refused(self):
        for spec, words in [
                (asked(normal_users=900), "at least as many users"),
                (asked(busiest_users=0), "Users at once at the busiest"),
                (asked(normal_users=-1), "Users at once on a normal day"),
                (asked(mb_per_user=0), "Memory per user"),
                (asked(mb_per_user="lots"), "Memory per user must be a number"),
                (asked(mb_per_user=True), "Memory per user must be a number"),
                (asked(busiest_users=100000), "more than a test can hold"),
                (asked(app_mb=0, mb_per_user=0.001, busiest_users=1, normal_users=0), "under 1 MB"),
                ("not a dict", "JSON object")]:
            with self.assertRaises(loadtest.PlanError) as caught:
                loadtest.users_plan(spec)
            self.assertIn(words, str(caught.exception))


class UsersPlanRouteTest(unittest.TestCase):

    def setUp(self):
        self.saved = (server.TOKEN, server.VIEW_TOKEN)
        server.TOKEN = server.VIEW_TOKEN = None
        self.client = server.app.test_client()

    def tearDown(self):
        server.TOKEN, server.VIEW_TOKEN = self.saved

    def test_it_plans_and_runs_nothing(self):
        response = self.client.post("/users-plan", json=loadtest.USERS_DEFAULTS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["spec"]["need_mb"], 1600)
        self.assertEqual(len(response.get_json()["lines"]), 3)

    def test_a_bad_answer_says_why(self):
        response = self.client.post("/users-plan", json=asked(normal_users=900))
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least as many users", response.get_json()["error"])

    def test_a_watcher_can_plan_but_a_stranger_cannot(self):
        server.TOKEN, server.VIEW_TOKEN = "s3cret-token", "a1b2c3d4e5"
        watcher = {"Authorization": "Bearer a1b2c3d4e5"}
        self.assertEqual(self.client.post("/users-plan", json=loadtest.USERS_DEFAULTS,
                                          headers=watcher).status_code, 200)
        self.assertEqual(self.client.post("/users-plan",
                                          json=loadtest.USERS_DEFAULTS).status_code, 401)

    def test_the_form_starts_from_the_defaults(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="lt-users"', html)
        for name, value in loadtest.USERS_DEFAULTS.items():
            self.assertIn('name="%s" value="%s"' % (name, value), html)


class UsersCliTest(unittest.TestCase):

    def args(self, **changes):
        values = dict(peak=None, ramp=None, wave=None, bursts=None, users="200:650",
                      app_mb=300, mb_per_user=2.0, need=None, steps=6, step_sec=60,
                      workers=2, background=False, swap=0, json=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def spec(self, **changes):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spec = cli.spec_from(self.args(**changes))
        return spec, out.getvalue()

    def test_users_become_the_same_ramp_as_the_dashboard(self):
        spec, printed = self.spec()
        self.assertEqual(spec, loadtest.users_plan(loadtest.USERS_DEFAULTS)["spec"])
        self.assertIn("= 1600 MB", printed)

    def test_background_and_swap_still_apply(self):
        spec, _ = self.spec(background=True, swap=2048, workers=4)
        self.assertEqual((spec["background_cpu"], spec["swap_mb"], spec["workers"]), (True, 2048, 4))

    def test_what_it_needs_is_asked_for(self):
        for changes, words in [(dict(app_mb=None), "--app-mb"),
                               (dict(mb_per_user=None), "--mb-per-user"),
                               (dict(need=1600), "leave --need out"),
                               (dict(users="650"), "NORMAL:BUSIEST"),
                               (dict(ramp="200:3200"), "exactly one of")]:
            with self.assertRaises(cli.CliError) as caught:
                self.spec(**changes)
            self.assertIn(words, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

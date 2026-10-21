"""One bad push must not break the pages: what the server takes in, and what it turns away.

    python3 -m unittest discover -s tests      # or: npm test

The server keeps samples as they arrive, and every page, the report and
/metrics are built from them. So each field of a realistic sample (dev.py's,
which mirrors the real agent's) is swapped for values of the wrong type or out
of range, pushed, and every read endpoint must still answer 200 with strict
JSON: no 500, and no bare NaN that the dashboard's response.json() chokes on.
"""

import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import dev  # noqa: E402
import loadtest  # noqa: E402
import schema  # noqa: E402
import server  # noqa: E402
import syshealth  # noqa: E402

TEST_ID = "fuzz1"
BAD_VALUES = [None, "x", 7, True, [], {}, -1, 1e308, "x" * 5000]


def strict(text):
    """json.loads that refuses NaN and Infinity, as browsers do."""
    def refuse(name):
        raise ValueError("not JSON: " + name)
    return json.loads(text, parse_constant=refuse)


def realistic_samples(count=10):
    """Samples from the demo's simulated t3.micro, part-way into a load test."""
    machine = dev.FakeMachine("t3.micro", 1024, 0.09, 0.10, 0)
    plan = loadtest.build_plan(dev.DEMO_TEST)
    start = time.time() - count * 5
    machine.start_test(TEST_ID, plan, start)
    return plan, [machine.sample(start + i * 5) for i in range(count)]


def leaf_paths(value, path=()):
    """Every field path in a sample, containers included; list items as index 0."""
    yield path
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaf_paths(item, path + (key,))
    elif isinstance(value, list) and value:
        yield from leaf_paths(value[0], path + (0,))


def replaced(sample, path, value):
    copy = json.loads(json.dumps(sample))
    target = copy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return copy


class IngestCase(unittest.TestCase):

    def setUp(self):
        self.saved_token = server.TOKEN
        server.TOKEN = None
        server.instances.clear()
        server.tests.clear()
        self.client = server.app.test_client()
        self.plan, self.samples = realistic_samples()
        server.record_test(TEST_ID, self.plan)

    def tearDown(self):
        server.TOKEN = self.saved_token
        server.instances.clear()
        server.tests.clear()

    def push(self, body, raw=False):
        if raw:
            return self.client.post("/metrics", data=body, content_type="application/json")
        return self.client.post("/metrics", json=body)

    def assert_pages_work(self, why):
        for url in ("/instances", "/series", "/summary",
                    "/report?test_id=%s&need_mb=600" % TEST_ID):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, "%s after %s" % (url, why))
            strict(response.get_data(as_text=True))
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, "GET /metrics after %s" % why)


class BadFieldsTest(IngestCase):

    def test_no_field_with_a_bad_value_breaks_a_page(self):
        good = self.samples[-1]
        warm_up = self.samples[:-1]
        for sample in warm_up:
            self.assertEqual(self.push(sample).status_code, 200)
        paths = [p for p in leaf_paths(good) if p]
        self.assertGreater(len(paths), 50)
        for path in paths:
            for value in BAD_VALUES:
                why = "%s = %r" % (".".join(map(str, path)), value if value != "x" * 5000 else "x" * 9 + "...")
                with self.subTest(why):
                    kept = len(server.instances["demo-t3-micro"]["history"])
                    status = self.push(replaced(good, path, value)).status_code
                    self.assertIn(status, (200, 422), why)
                    if status == 422:
                        # Turned away, so nothing was kept and the pages cannot change.
                        self.assertEqual(len(server.instances["demo-t3-micro"]["history"]), kept)
                        continue
                    self.assert_pages_work(why)
                    # A value that was kept must not linger either.
                    self.push(good)
                    self.assert_pages_work(why + ", then a good push")
                    # Start the next case from a short history again, so it stays quick.
                    server.instances.clear()
                    for sample in warm_up:
                        self.push(sample)

    def test_the_original_report_is_turned_away(self):
        response = self.push({"hostname": "b", "psi": 0.1, "instance_type": 123})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["field"], "instance_type")
        self.assertNotIn("b", server.instances)
        self.assert_pages_work("instance_type 123")

    def test_a_hostname_must_be_text(self):
        for hostname in (5, ["a"], {"a": 1}, True, "h" * 300):
            response = self.push({"hostname": hostname, "psi": 0.1})
            self.assertEqual(response.status_code, 422, repr(hostname))
            self.assertEqual(response.get_json()["field"], "hostname")
        self.assertEqual(len(server.instances), 0)
        self.assert_pages_work("bad hostnames")

    def test_the_error_names_the_field(self):
        sample = replaced(self.samples[-1], ("memory", "used_pct"), "lots")
        response = self.push(sample)
        self.assertEqual(response.status_code, 422)
        body = response.get_json()
        self.assertEqual(body["field"], "memory.used_pct")
        self.assertIn("memory.used_pct", body["error"])


class BadBodiesTest(IngestCase):

    def test_nan_and_infinity_are_turned_away_anywhere(self):
        for text in ('{"hostname": "n", "psi": NaN}',
                     '{"hostname": "n", "psi": Infinity}',
                     '{"hostname": "n", "psi": 1e400}',
                     '{"hostname": "n", "extra": {"deep": [1, -Infinity]}}'):
            response = self.push(text, raw=True)
            self.assertEqual(response.status_code, 422, text)
        self.assertNotIn("n", server.instances)
        self.assert_pages_work("NaN pushes")

    def test_a_body_that_is_not_an_object_is_a_400(self):
        for text in ("[]", '"x"', "5", "null", "{not json"):
            response = self.push(text, raw=True)
            self.assertEqual(response.status_code, 400, text)
        self.assertEqual(len(server.instances), 0)

    def test_an_oversized_push_is_refused(self):
        big = {"hostname": "big", "psi": 0.1, "padding": "x" * (schema.MAX_BODY_BYTES + 1)}
        self.assertEqual(self.push(big).status_code, 413)
        self.assertNotIn("big", server.instances)

    def test_unknown_fields_still_pass(self):
        # A newer agent may send more than this server knows about.
        sample = dict(self.samples[-1], future_field={"anything": [1, "two", None]})
        self.assertEqual(self.push(sample).status_code, 200)
        self.assert_pages_work("an unknown field")


class AgentTest(IngestCase):

    def test_the_agent_says_when_a_sample_is_turned_away(self):
        # Through the real endpoint, so the agent sees the server's own answer.
        def post(url, json=None, timeout=None, headers=None):
            answer = self.client.post("/metrics", json=json)
            reply = mock.Mock(status_code=answer.status_code, text=answer.get_data(as_text=True))
            reply.json.return_value = answer.get_json()
            return reply

        sample = replaced(self.samples[-1], ("instance_type",), 123)
        saved = syshealth._push_failures
        syshealth._push_failures = 0
        try:
            with mock.patch.object(syshealth.requests, "post", post), \
                    redirect_stderr(io.StringIO()) as err:
                syshealth.push_to_server(sample)
            self.assertEqual(syshealth._push_failures, 1)
            self.assertIn("turned the sample away (422): instance_type should be text", err.getvalue())
            with mock.patch.object(syshealth.requests, "post", post), \
                    redirect_stdout(io.StringIO()) as out:
                syshealth.push_to_server(self.samples[-1])
            self.assertIn("recovered after 1 failures", out.getvalue())
            self.assertEqual(syshealth._push_failures, 0)
        finally:
            syshealth._push_failures = saved


class WhatTheAgentsSendTest(unittest.TestCase):
    """The table must accept what the agents really send, or real hosts vanish."""

    def test_every_demo_sample_passes(self):
        for itype, ram, base, idle in dev.PROFILES:
            machine = dev.FakeMachine(itype, ram, base, idle, 0)
            plan = loadtest.build_plan(dev.DEMO_TEST)
            start = time.time() - 3600
            machine.start_test(TEST_ID, plan, start + 600)
            for i in range(720):
                sample = machine.sample(start + i * 5)
                self.assertIsNone(schema.problem(sample), (itype, i))

    def test_a_minimal_older_agent_passes(self):
        self.assertIsNone(schema.problem({"hostname": "old-box", "psi": 0.2}))
        self.assertIsNone(schema.problem({"hostname": "old-box", "psi": 0.2, "app": None,
                                          "load_test": None, "region": None}))


if __name__ == "__main__":
    unittest.main()

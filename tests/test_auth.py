"""The shared token: what it guards, what it leaves open, and who sends it.

    python3 -m unittest discover -s tests      # or: npm test

The agent's control port and the server run for real here, on local ports,
so the header really travels: server to agent, and cli.py to server.
"""

import logging
import os
import sys
import threading
import time
import unittest
from http.server import HTTPServer

from werkzeug.serving import make_server

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import cli  # noqa: E402
import server  # noqa: E402
import syshealth  # noqa: E402

TOKEN = "s3cret-token"
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # no request log in the test output
BEARER = {"Authorization": "Bearer " + TOKEN}


def sample(host="box-1", **extra):
    body = {"hostname": host, "timestamp": time.time(), "psi": 0.1, "psi_full": 0.0,
            "instance_type": "t3.micro"}
    body.update(extra)
    return body


class TokenCase(unittest.TestCase):

    def setUp(self):
        self.saved = (server.TOKEN, syshealth.TOKEN, cli.TOKEN)
        server.instances.clear()
        server.tests.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.TOKEN, syshealth.TOKEN, cli.TOKEN = self.saved
        server.instances.clear()
        server.tests.clear()


class ServerTokenTest(TokenCase):

    def test_without_a_token_nothing_is_checked(self):
        server.TOKEN = None
        self.assertEqual(self.client.post("/metrics", json=sample()).status_code, 200)

    def test_pushes_need_the_token(self):
        server.TOKEN = TOKEN
        self.assertEqual(self.client.post("/metrics", json=sample()).status_code, 401)
        wrong = {"Authorization": "Bearer nope"}
        self.assertEqual(self.client.post("/metrics", json=sample(), headers=wrong).status_code, 401)
        self.assertEqual(self.client.post("/metrics", json=sample(), headers={"Authorization": TOKEN})
                         .status_code, 401)  # no scheme
        self.assertNotIn("box-1", server.instances)
        self.assertEqual(self.client.post("/metrics", json=sample(), headers=BEARER).status_code, 200)
        self.assertIn("box-1", server.instances)

    def test_starting_and_stopping_need_the_token(self):
        server.TOKEN = TOKEN
        response = self.client.post("/run-stress", json={"mode": "peak", "memory_mb": 100,
                                                         "duration_sec": 10, "workers": 1})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.get_json()["token_required"])
        self.assertEqual(len(server.tests), 0)
        self.assertEqual(self.client.post("/stop-stress").status_code, 401)
        self.assertEqual(self.client.post("/stop-stress", headers=BEARER).status_code, 200)

    def test_reading_stays_open(self):
        server.TOKEN = TOKEN
        for path in ("/", "/series", "/instances", "/report", "/metrics"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        peak = {"mode": "peak", "memory_mb": 100, "duration_sec": 10, "workers": 1}
        self.assertEqual(self.client.post("/load-plan", json=peak).status_code, 200)

    def test_watch_only_machines_are_never_asked(self):
        server.TOKEN = None
        self.client.post("/metrics", json=sample("prod", accepts_tests=False))
        self.client.post("/metrics", json=sample("test-box", accepts_tests=True))
        results = {}
        targets = server._online_targets(time.time(), results)
        self.assertEqual(list(targets), ["test-box"])
        self.assertTrue(results["prod"].startswith("skipped"))


class AgentTokenTest(TokenCase):
    """The agent's control port, and the server talking to it."""

    def setUp(self):
        super().setUp()
        self.httpd = HTTPServer(("127.0.0.1", 0), syshealth.ControlHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.saved_port = server.AGENT_CONTROL_PORT
        server.AGENT_CONTROL_PORT = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.AGENT_CONTROL_PORT = self.saved_port
        super().tearDown()

    def test_matching_tokens(self):
        server.TOKEN = syshealth.TOKEN = TOKEN
        self.assertEqual(server._agent_post("127.0.0.1", "/stop"), "idle")

    def test_agent_refuses_a_missing_or_wrong_token(self):
        syshealth.TOKEN = TOKEN
        server.TOKEN = None
        self.assertTrue(server._agent_post("127.0.0.1", "/stop").startswith("rejected"))
        server.TOKEN = "other"
        self.assertTrue(server._agent_post("127.0.0.1", "/stop").startswith("rejected"))
        # Refused before the plan is read: nothing started.
        self.assertTrue(server._agent_post("127.0.0.1", "/stress", {"plan": {}}).startswith("rejected"))
        self.assertFalse(syshealth.runner.busy())

    def test_agent_without_a_token_still_answers(self):
        server.TOKEN = syshealth.TOKEN = None
        self.assertEqual(server._agent_post("127.0.0.1", "/stop"), "idle")


class CliTokenTest(TokenCase):
    """cli.py sends $SYSHEALTH_TOKEN, and says so when it is missing."""

    def setUp(self):
        super().setUp()
        self.httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        super().tearDown()

    def test_cli_sends_the_token(self):
        server.TOKEN = cli.TOKEN = TOKEN
        self.assertIn("results", cli.call(self.url, "/stop-stress", {}))

    def test_cli_says_what_is_missing(self):
        server.TOKEN = TOKEN
        cli.TOKEN = None
        with self.assertRaises(cli.CliError) as caught:
            cli.call(self.url, "/stop-stress", {})
        self.assertIn("SYSHEALTH_TOKEN", str(caught.exception))
        # Reading needs nothing.
        self.assertIn("instances", cli.call(self.url, "/series?limit=1"))


if __name__ == "__main__":
    unittest.main()

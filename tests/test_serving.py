"""How the server is served, and where the agent sends its samples.

    python3 -m unittest discover -s tests      # or: npm test

The waitress test needs waitress (pip install -r requirements.txt) and is
skipped without it.
"""

import os
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

import requests

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import server  # noqa: E402

try:
    import waitress
except ImportError:
    waitress = None


class AgentAddressTest(unittest.TestCase):

    def test_without_an_address_the_agent_stops_and_says_why(self):
        # A real agent, started the way systemd or nohup would start it. It
        # must stop before it measures, listens or sends anything.
        env = {k: v for k, v in os.environ.items() if k != "SYSHEALTH_SERVER_URL"}
        done = subprocess.run([sys.executable, "syshealth.py"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertIn("SYSHEALTH_SERVER_URL is not set", done.stderr)
        self.assertNotIn("SysHealth started", done.stdout)

    def test_an_address_that_is_only_spaces_counts_as_none(self):
        env = dict(os.environ, SYSHEALTH_SERVER_URL="  ")
        done = subprocess.run([sys.executable, "-c", "import syshealth; print(syshealth.SERVER_URL)"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "None")


class ServeTest(unittest.TestCase):

    def test_waitress_serves_it_in_one_process(self):
        fake = types.SimpleNamespace(serve=mock.Mock())
        with mock.patch.dict(sys.modules, {"waitress": fake}), \
             mock.patch("builtins.print"):
            server.serve(port=5999)
        fake.serve.assert_called_once()
        args, kwargs = fake.serve.call_args
        self.assertIs(args[0], server.app)
        self.assertEqual(kwargs["port"], 5999)
        self.assertGreater(kwargs["threads"], 1)
        self.assertTrue(kwargs["clear_untrusted_proxy_headers"])

    def test_without_waitress_it_falls_back_to_flask(self):
        with mock.patch.dict(sys.modules, {"waitress": None}), \
             mock.patch.object(server.app, "run") as run, \
             mock.patch("builtins.print") as said:
            server.serve(port=5999)
        run.assert_called_once_with(host="0.0.0.0", port=5999)
        self.assertIn("waitress is not installed", said.call_args[0][0])


@unittest.skipUnless(waitress, "waitress is not installed")
class UnderWaitressTest(unittest.TestCase):
    """The token header and the agent's address get through waitress intact."""

    def setUp(self):
        self.saved = server.TOKEN
        server.TOKEN = "s3cret-token"
        server.instances.clear()
        self.httpd = waitress.create_server(server.app, host="127.0.0.1", port=0, threads=2,
                                            clear_untrusted_proxy_headers=True)
        self.url = "http://127.0.0.1:%s" % self.httpd.effective_port
        threading.Thread(target=self.httpd.run, daemon=True).start()

    def tearDown(self):
        self.httpd.close()
        server.TOKEN = self.saved
        server.instances.clear()

    def test_a_push_with_the_token_is_kept(self):
        body = {"hostname": "box-1", "timestamp": 1.0, "psi": 0.1, "psi_full": 0.0}
        self.assertEqual(requests.post(self.url + "/metrics", json=body, timeout=5).status_code, 401)
        r = requests.post(self.url + "/metrics", json=body, timeout=5,
                          headers={"Authorization": "Bearer s3cret-token"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server.instances["box-1"]["agent_ip"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()

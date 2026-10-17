"""The MCP server: the protocol, and each tool against a real SysHealth server.

    python3 -m unittest discover -s tests      # or: npm test

The SysHealth server runs on a local port with dev.py's simulated fleet, after
a simulated ramp test; the last case drives mcp_server.py as a subprocess
over stdio, the way an agent does.
"""

import io
import json
import logging
import os
import subprocess
import sys
import threading
import unittest

from werkzeug.serving import make_server

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import mcp_server  # noqa: E402
import server  # noqa: E402
from test_rightsize import RAMP, run_test  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)


class McpCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.saved = (server.TOKEN, cli.TOKEN)
        run_test(RAMP)
        self.mcp = mcp_server.Server(self.url)
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})

    def tearDown(self):
        server.TOKEN, cli.TOKEN = self.saved

    def request(self, method, params=None, msg_id=1):
        return self.mcp.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def tool(self, name, **args):
        reply = self.request("tools/call", {"name": name, "arguments": args})
        self.assertIn("result", reply, reply)
        return reply["result"]


class ProtocolTest(McpCase):

    def test_initialize_agrees_a_version(self):
        reply = self.request("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(reply["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("rightsize_report", reply["result"]["instructions"])
        newest = self.request("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(newest["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSIONS[0])

    def test_lists_five_tools_with_schemas(self):
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [
            "fleet_status", "plan_load_test", "start_load_test", "stop_load_test", "rightsize_report"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
        start = next(t for t in tools if t["name"] == "start_load_test")
        self.assertTrue(start["annotations"]["destructiveHint"])

    def test_errors(self):
        self.assertEqual(self.request("nope")["error"]["code"], -32601)
        self.assertEqual(self.request("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(self.mcp.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(self.request("ping")["result"], {})
        # Notifications get no reply.
        self.assertIsNone(self.mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_serve_reads_lines_and_answers_parse_errors(self):
        out = io.StringIO()
        lines = ['{"jsonrpc":"2.0","id":7,"method":"ping"}', "not json", "",
                 '{"jsonrpc":"2.0","method":"notifications/initialized"}',
                 '[{"jsonrpc":"2.0","id":8,"method":"ping"}]']
        mcp_server.serve(self.mcp, io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(replies[2], [{"jsonrpc": "2.0", "id": 8, "result": {}}])
        self.assertEqual(len(replies), 3)

    def test_structured_content_only_for_clients_that_take_it(self):
        self.assertIn("structuredContent", self.tool("fleet_status"))
        self.request("initialize", {"protocolVersion": "2024-11-05"})
        result = self.tool("fleet_status")
        self.assertNotIn("structuredContent", result)
        # The same data is in the second text block either way.
        self.assertEqual(len(json.loads(result["content"][1]["text"])["instances"]), 4)


class ToolTest(McpCase):

    def test_fleet_status(self):
        result = self.tool("fleet_status")
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        for size in ("t3.micro", "t3.small", "t3.medium", "t3.large"):
            self.assertIn(size, text)
        self.assertIn("app node-app.service over the last hour", text)
        self.assertIn("4 of 4 machine(s) online and accepting load tests", text)
        large = self.tool("fleet_status", hostname="t3.large")
        self.assertIn("biggest:", large["content"][0]["text"])
        self.assertEqual(len(large["structuredContent"]["instances"]), 1)
        self.assertTrue(self.tool("fleet_status", hostname="t9.huge")["isError"])

    def test_rightsize_report(self):
        result = self.tool("rightsize_report", need_mb=1600, cpu_vcpu=0.9)
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("Choose t3.medium", text)
        self.assertIn("Limits of this answer", text)
        self.assertIn("synthetic", text)
        rec = result["structuredContent"]["report"]["recommendation"]
        self.assertEqual(rec["instance_type"], "t3.medium")
        self.assertEqual(rec["usd_month"], 48.62)   # $30.37 + $18.25 of CPU credits
        sizes = result["structuredContent"]["report"]["sizes"]
        self.assertTrue(all(s["levels"] for s in sizes))
        bad = self.tool("rightsize_report", cpu_vcpu="lots")
        self.assertTrue(bad["isError"])

    def test_plan_runs_nothing(self):
        result = self.tool("plan_load_test", shape="ramp", low_mb=200, high_mb=3200, need_mb=1600)
        self.assertFalse(result["isError"], result)
        self.assertIn("Nothing has been started", result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["plan"]["need_mb"], 1600)
        self.assertEqual(len(server.tests), 1)   # only run_test's own
        self.assertTrue(self.tool("plan_load_test", shape="peak")["isError"])
        self.assertTrue(self.tool("plan_load_test", shape="ramp", low_mb=200, high_mb=99999)["isError"])

    def test_start_needs_the_token(self):
        server.TOKEN = "s3cret"
        cli.TOKEN = None
        result = self.tool("start_load_test", shape="peak", memory_mb=100)
        self.assertTrue(result["isError"])
        self.assertIn("SYSHEALTH_TOKEN", result["content"][0]["text"])
        self.assertEqual(len(server.tests), 1)


class StdioTest(McpCase):
    """mcp_server.py as an agent runs it: a subprocess, JSON lines both ways."""

    def test_a_session(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "rightsize_report", "arguments": {"need_mb": 1600}}},
        ]
        env = dict(os.environ, SYSHEALTH_URL=self.url)
        env.pop("SYSHEALTH_TOKEN", None)
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "mcp_server.py")],
            input="".join(json.dumps(m) + "\n" for m in lines),
            capture_output=True, text=True, env=env, timeout=30)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3], proc.stderr)
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "syshealth")
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)
        self.assertIn("Choose t3.medium", replies[2]["result"]["content"][0]["text"])
        self.assertIn("talking to " + self.url, proc.stderr)


if __name__ == "__main__":
    unittest.main()

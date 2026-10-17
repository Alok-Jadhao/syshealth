"""GET /metrics: SysHealth in Prometheus's text format."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prometheus  # noqa: E402
import server  # noqa: E402
from test_rightsize import RAMP, run_test  # noqa: E402

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([a-zA-Z_][a-zA-Z0-9_]*="(\\.|[^"\\])*",?)*\})? '
                    r'(-?[0-9.e+-]+|NaN|\+Inf|-Inf)$')


def families(text):
    """{name: [sample lines]}, asserting the format's rules as it goes."""
    out = {}
    current = None
    for line in text.splitlines():
        if line.startswith("# HELP "):
            current = line.split()[2]
            assert current not in out, "%s declared twice" % current
            out[current] = []
        elif line.startswith("# TYPE "):
            assert line.split()[2] == current
        else:
            match = SAMPLE.match(line)
            assert match, "not a sample line: %r" % line
            assert match.group(1) == current, "%s outside its group" % match.group(1)
            out[current].append(line)
    return out


class PrometheusTest(unittest.TestCase):

    def test_scrape_after_a_test(self):
        run_test(RAMP)
        response = server.app.test_client().get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
        found = families(response.get_data(as_text=True))
        self.assertEqual(len(found["syshealth_up"]), 4)
        self.assertEqual(len(found["syshealth_state"]), 12)  # three states for each of four
        recommended = [l for l in found["syshealth_recommended"] if l.endswith(" 1")]
        self.assertEqual(len(recommended), 1)
        self.assertIn('instance_type="t3.medium"', recommended[0])
        self.assertIn('kind="at_least"', " ".join(found["syshealth_capacity_bytes"]))
        self.assertIn("syshealth_threshold_ratio", found)

    def test_agents_still_post_to_the_same_path(self):
        client = server.app.test_client()
        self.assertEqual(client.post("/metrics", json={"hostname": "x", "psi": 1}).status_code, 200)

    def test_label_values_are_escaped(self):
        snap = [{"hostname": 'odd"host\\name\nx', "instance_type": "t3.micro", "online": True,
                 "samples": [{"psi": 1.0, "state": "HEALTHY"}]}]
        text = prometheus.render(snap)
        families(text)
        self.assertIn(r'hostname="odd\"host\\name\nx"', text)


class SeriesTest(unittest.TestCase):

    def test_since_returns_only_newer_samples(self):
        run_test(RAMP)
        client = server.app.test_client()
        full = client.get("/series").get_json()
        self.assertIsNone(full["since"])
        newest = max(s["received_ts"] for i in full["instances"] for s in i["samples"])
        cut = newest - 12  # the last two or three samples of each
        delta = client.get("/series?since=%r" % cut).get_json()
        self.assertEqual(delta["since"], cut)
        self.assertEqual(len(delta["instances"]), 4)
        for inst in delta["instances"]:
            self.assertTrue(1 <= len(inst["samples"]) <= 3)
            self.assertTrue(all(s["received_ts"] > cut for s in inst["samples"]))
            # Only the newest sample carries the diagnosis.
            self.assertIn("diagnosis", inst["samples"][-1])
            self.assertTrue(all("diagnosis" not in s for s in inst["samples"][:-1]))
        self.assertEqual(client.get("/series?since=nonsense").get_json()["since"], None)


if __name__ == "__main__":
    unittest.main()

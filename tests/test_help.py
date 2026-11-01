"""Every ? on the dashboard opens an explanation, and every explanation has a ?.

    python3 -m unittest discover -s tests      # or: npm test

The keys live in three files: the page (templates/index.html), the parts
dashboard.js draws itself, and the texts in static/js/help.js. A key renamed in
one and not the others would leave a ? that opens nothing.
"""

import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import server  # noqa: E402


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def _explained():
    js = _read("static", "js", "help.js")
    texts = js[js.index("var HELP = {"):js.index("function gb(")]
    return set(re.findall(r"^    '?([a-z_0-9-]+)'?: \{", texts, re.M))


def _asked_for():
    html = server.app.test_client().get("/").get_data(as_text=True)
    keys = set(re.findall(r'data-help="([^"]+)"', html))
    drawn = _read("static", "js", "dashboard.js")
    keys |= set(re.findall(r"(?:term|helpButton)\('([^']+)'", drawn))
    return keys


class HelpKeysTest(unittest.TestCase):
    def test_every_question_mark_has_an_explanation(self):
        self.assertEqual(set(), _asked_for() - _explained())

    def test_every_explanation_has_a_question_mark(self):
        self.assertEqual(set(), _explained() - _asked_for())

    def test_the_page_loads_the_explanations_first(self):
        html = server.app.test_client().get("/").get_data(as_text=True)
        self.assertLess(html.index("js/help.js"), html.index("js/dashboard.js"))
        self.assertGreater(len(_asked_for()), 50)


class MemoryInUseTest(unittest.TestCase):
    """Memory in use has its own place, next to the waiting it is not."""

    def test_the_page_offers_it_on_the_tile_and_the_chart(self):
        html = server.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="tile-mem"', html)
        self.assertIn('data-metric="pressure"', html)
        self.assertIn('data-metric="used"', html)

    def test_the_cards_draw_it_and_the_load_line_drops_it(self):
        drawn = _read("static", "js", "dashboard.js")
        self.assertIn("term('mem-in-use', 'Memory in use')", drawn)
        self.assertIn("machineLoad(latest, true)", drawn)


if __name__ == "__main__":
    unittest.main()

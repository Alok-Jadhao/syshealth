"""Draw the README's picture of a real run: every size at every load level.

    python3 docs/make_result_figure.py [rightsize-REPORT.json] [out.svg]

Reads a report saved by terraform/rightsize.sh or `cli.py --save` (the
19 Oct 2026 run by default) and writes an SVG grid: one row per size, one
column per load level, each cell the share of time the machine was stalled
waiting for memory and the share of its memory in use, coloured by state.
Plain SVG on a white card, so it reads the same in light and dark themes.
"""

import json
import os
import sys
from xml.sax.saxutils import escape

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

import loadtest  # noqa: E402
import pricing  # noqa: E402

REPORT = os.path.join(ROOT, "rightsize-2026-10-19-2037.json")
OUT = os.path.join(ROOT, "docs", "img", "real-run-2026-10-19.svg")
CAPTION = "AWS ap-south-1, 19 October 2026"

INK, SECONDARY, MUTED = "#0b0b0b", "#52514e", "#898781"
FILL = {   # (cell, text) for each state, as the dashboard colours them
    "HEALTHY": ("#e2f3e2", INK),
    "DEGRADED": ("#fab219", INK),
    "CRITICAL": ("#d03b3b", "#ffffff"),
    "killed": ("#d6d5ce", SECONDARY),
}
PICK = "#2a78d6"
FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'

LEFT, TOP, CELL_W, CELL_H, GAP = 190, 112, 92, 50, 6


def pct(value):
    if value < 0.005:
        return "0%"
    if value < 1:
        return ("%.2f" % value).rstrip("0").rstrip(".") + "%"
    return "%d%%" % round(value)


def text(x, y, body, size=13, weight="normal", fill=INK, anchor="start"):
    return '<text x="%g" y="%g" font-size="%g" font-weight="%s" fill="%s" text-anchor="%s">%s</text>' % (
        x, y, size, weight, fill, anchor, escape(body))


def cell(x, y, level):
    state = "killed" if level["killed"] else level["state"]
    bg, ink = FILL.get(state, FILL["HEALTHY"])
    mid = x + CELL_W / 2.0
    out = ['<rect x="%g" y="%g" width="%g" height="%g" rx="6" fill="%s"/>' % (x, y, CELL_W, CELL_H, bg)]
    if level["killed"]:
        out.append(text(mid, y + 22, "killed", 14, "600", ink, "middle"))
        out.append(text(mid, y + 39, "OOM killer", 11, "normal", ink, "middle"))
    else:
        out.append(text(mid, y + 23, pct(level["some"]), 17, "700", ink, "middle"))
        out.append(text(mid, y + 40, "%d%% in use" % round(level["used_pct"]), 11, "normal", ink, "middle"))
    return out


def figure(report):
    rows = report["rows"]
    rec = report.get("recommendation") or {}
    picked = next((r["instance_type"] for r in rows if r["hostname"] == rec.get("pick")), None)
    loads = [lv["mb"] for lv in rows[0]["levels"]]
    width = LEFT + len(loads) * (CELL_W + GAP) - GAP + 24
    bottom = TOP + len(rows) * (CELL_H + GAP) - GAP
    height = bottom + 78

    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" %s>'
           % (width, height, width, height, FONT),
           '<title>The same memory load on four EC2 sizes at once: %s</title>' % escape(rec.get("headline", "")),
           '<rect width="%d" height="%d" rx="12" fill="#ffffff" stroke="#e1e0d9"/>' % (width, height),
           text(24, 36, "The same memory load on four EC2 sizes at once", 20, "700"),
           text(24, 60, "%s. Each cell: time stalled waiting for memory (PSI), and memory in use."
                % CAPTION, 13, "normal", SECONDARY)]

    for i, mb in enumerate(loads):
        x = LEFT + i * (CELL_W + GAP) + CELL_W / 2.0
        out.append(text(x, TOP - 12, "{:,} MB".format(mb), 12, "600", SECONDARY, "middle"))
    out.append(text(24, TOP - 12, "Load on every size", 12, "600", SECONDARY))

    for j, row in enumerate(rows):
        y = TOP + j * (CELL_H + GAP)
        itype = row["instance_type"]
        spec = pricing.specs(itype) or {}
        price = row.get("price") or {}
        if itype == picked:
            out.append('<rect x="14" y="%g" width="%g" height="%g" rx="9" fill="none" stroke="%s" '
                       'stroke-width="2.5"/>' % (y - 4, width - 28, CELL_H + 8, PICK))
        out.append(text(24, y + 22, itype + ("  ✓ pick" if itype == picked else ""), 15, "700",
                        PICK if itype == picked else INK))
        out.append(text(24, y + 40, "%g GiB · $%.2f a month" % (spec.get("memory_gib") or 0,
                                                             price.get("usd_month") or 0),
                        12, "normal", SECONDARY))
        for i, level in enumerate(row["levels"]):
            out.extend(cell(LEFT + i * (CELL_W + GAP), y, level))

    if rec.get("headline"):
        out.append(text(24, bottom + 34, "For a %s app with %d%% spare: %s" % (
            loadtest.format_mb(report["need_mb"]), report["margin_pct"], rec["headline"]), 15, "700"))
    out.append(text(24, bottom + 58, "Green: healthy. Red: critical, every task stalled more than 10% of "
                    "the time. Grey: the load no longer fit; the OOM killer ended it.",
                    12, "normal", MUTED))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else REPORT
    dst = sys.argv[2] if len(sys.argv) > 2 else OUT
    with open(src) as f:
        doc = json.load(f)
    svg = figure(doc.get("report", doc))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w") as f:
        f.write(svg)
    print("wrote", dst)


if __name__ == "__main__":
    main()

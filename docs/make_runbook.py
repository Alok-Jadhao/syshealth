#!/usr/bin/env python3
"""Generate the live-demo runbook PDF.

Kept in the repo rather than run once and thrown away, so the runbook can be
regenerated when the commands change — a printed sheet that has drifted from
the code is worse than no sheet at all.

    python3 docs/make_runbook.py
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph,
    Preformatted, Spacer, Table, TableStyle, KeepTogether,
)

INK = colors.HexColor("#16191d")
MUTED = colors.HexColor("#5c6570")
RULE = colors.HexColor("#d7dce2")
ACCENT = colors.HexColor("#1f5fa8")
WARN = colors.HexColor("#a8501f")
CODE_BG = colors.HexColor("#f2f4f7")
SAY_BG = colors.HexColor("#eef4fb")

styles = getSampleStyleSheet()


def S(name, **kw):
    base = dict(fontName="Helvetica", fontSize=9.4, leading=13.2,
                textColor=INK, alignment=TA_LEFT, spaceAfter=5)
    base.update(kw)
    return ParagraphStyle(name, **base)


TITLE = S("t", fontName="Helvetica-Bold", fontSize=19, leading=23, spaceAfter=2)
SUB = S("s", fontSize=10, textColor=MUTED, spaceAfter=13)
# keepWithNext: a heading never ends a page apart from what it heads.
H1 = S("h1", fontName="Helvetica-Bold", fontSize=13.5, leading=17,
       textColor=ACCENT, spaceBefore=13, spaceAfter=6, keepWithNext=1)
H2 = S("h2", fontName="Helvetica-Bold", fontSize=10.4, leading=14,
       spaceBefore=7, spaceAfter=3, keepWithNext=1)
BODY = S("b")
SMALL = S("sm", fontSize=8.5, leading=11.6, textColor=MUTED)
BULLET = S("bu", leftIndent=11, bulletIndent=2, spaceAfter=3)
TCELL = S("tc", fontSize=8.6, leading=11.4, spaceAfter=0)

CODE = ParagraphStyle("code", fontName="Courier", fontSize=8.3, leading=11.4,
                      textColor=INK)


# Courier is metrically fixed at 0.6 em, so the usable width is exact. Anything
# past it is clipped by the table cell rather than wrapped — which prints a
# command that looks complete and is not, the one failure a runbook must never
# have. Checked here so the build fails instead of the demo.
CODE_COLS = int((165 - 14) * mm / (0.6 * CODE.fontSize))


def code(text):
    """A command block. Selectable text, so it can be copied out of the PDF."""
    text = text.strip("\n")
    over = [l for l in text.splitlines() if len(l) > CODE_COLS]
    if over:
        raise SystemExit(
            "Command line too wide for the page (limit %d chars):\n  %s"
            % (CODE_COLS, "\n  ".join("%3d  %s" % (len(l), l) for l in over))
        )
    t = Table([[Preformatted(text, CODE)]],
              colWidths=[165 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [t, Spacer(1, 6)]


def say(text):
    """A line to actually speak out loud, visually distinct from a command."""
    p = Paragraph("<b>Say:</b>  " + text, S("say", fontSize=9.2, leading=12.8))
    t = Table([[p]], colWidths=[165 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SAY_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [t, Spacer(1, 6)]


def para(text, style=BODY):
    return Paragraph(text, style)


def bullets(items):
    return [Paragraph(i, BULLET, bulletText="•") for i in items]


def expect(text):
    return Paragraph("<b>Expect:</b> " + text,
                     S("e", fontSize=9, textColor=WARN, spaceAfter=7))


def table(rows, widths, head=True):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    st = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.6),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]
    if head:
        st += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
               ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK)]
    t.setStyle(TableStyle(st))
    return [t, Spacer(1, 9)]


# ---------------------------------------------------------------- content ---

story = []
A = story.append
E = story.extend

A(para("SysHealth — live demo runbook", TITLE))
A(para("Four EC2 sizes, one load, one verdict in dollars. "
       "Every command runs from <font face='Courier'>~/syshealth</font>.", SUB))

# ---- 1
A(para("1 &nbsp; The day before", H1))
E(bullets([
    "<b>Rehearse on the network you will present from.</b> The dashboard only lets in the "
    "address <font face='Courier'>./aws.sh up</font> ran from, on port 5000, and many campus "
    "networks block port 5000. Keep a phone hotspot as the way out.",
    "<b>Have the fallbacks ready:</b> the threshold screenshots, the 22 October step "
    "screenshots (<font face='Courier'>~/syshealth-aws-run-2026-10-22/</font>), the real "
    "four-state picture, from the button (<font face='Courier'>~/syshealth-aws-three-states-button.png</font>, "
    "step 5b), the README's results table, and <font face='Courier'>npm run dev</font> working "
    "on the laptop (section 5).",
    "<b>Push first, then rehearse.</b> The fleet installs the code on GitHub, so <b>Show all three "
    "states</b> and the <b>Machine load</b> line appear only once they are pushed. Do not run "
    "<font face='Courier'>./aws.sh deploy</font> on the day: keep the version you rehearsed.",
]))

# ---- 2
A(para("2 &nbsp; Forty-five minutes before", H1))
A(para("The fleet needs about five minutes to boot and calibrate, and you want one rehearsal "
       "run in hand before anyone is watching.", BODY))
A(para("Bring the fleet up", H2))
E(code("./aws.sh up --profile hackathon"))
A(para("It names the account and region, says what it costs and asks before building: about "
       "$0.19 an hour for the five machines, $0.22 with their addresses and disks.", SMALL))
A(expect("<font face='Courier'>4 of 4 instances reporting</font>, then the <b>Dashboard</b> "
         "address and the <b>Token</b>. Copy both."))
E(code("./aws.sh status"))
A(expect("four sizes, <font face='Courier'>ONLINE yes</font>, all HEALTHY."))
A(para("Rehearse once, from the dashboard", H2))
A(para("Open the Dashboard address. Press <b>Load test</b>, choose <b>Variable load</b> and "
       "<b>Ramp up</b>. The defaults are the test: your app at its busiest 1600 MB, from 200 to "
       "3200 MB, 6 steps of 60 seconds. Press <b>Start on 4 instances</b> and paste the token into "
       "<b>Server token</b> when it asks. The browser keeps the token, so the live run will not "
       "ask again.", BODY))
A(para("About seven minutes. It proves the whole chain works and leaves a finished result on "
       "the page in case the live run goes wrong. If step 5b is in the plan, run it once too "
       "(six minutes). Then wait two minutes for the lines to fall back to zero. Lost the token, or Start says it is wrong? A rebuilt fleet has a new one:",
       SMALL))
E(code("(cd terraform && TF_WORKSPACE=fleet terraform output -raw token)"))

A(PageBreak())

# ---- 3
A(para("3 &nbsp; The demo, about twelve minutes", H1))

A(para("Step 1 — The problem (one minute, no clicking)", H2))
E(say("Teams pick an instance size by guessing, or by an alarm at 80 percent of memory in "
      "use. One way they overpay, the other way the app slows down and nobody knows why. "
      "SysHealth measures it: the same load on four sizes at the same moment, then the "
      "cheapest size that holds your app, in dollars a month."))

A(para("Step 2 — The idle fleet", H2))
A(para("Four cards, all HEALTHY, a flat chart. Open <b>What the number means</b>.", BODY))
E(say("This number comes from the Linux kernel itself: the share of time programs waited "
      "for memory. Not how full memory is. Under 10 percent is healthy, 10 to 40 is degraded, "
      "40 or more is critical, and so is the whole machine stalling 10 percent of the time. "
      "Those lines come from systems that act on this signal in production: Android's "
      "low-memory killer, systemd-oomd and Meta's oomd. A state only changes after it has "
      "held for 15 seconds, so one spike is not a failure."))

A(para("Step 3 — Start the ramp", H2))
A(para("<b>Load test</b>, <b>Variable load</b>, <b>Ramp up</b>, the same defaults, "
       "<b>Start on 4 instances</b>.", BODY))
E(say("Same load, same moment, four machines. It climbs from 200 megabytes to 3.2 "
      "gigabytes over six minutes. Watch which ones feel it."))

A(para("Step 4 — Talk through the six minutes", H2))
E(table([
    ["Minute", "Load", "Point at"],
    ["1–2", "800 MB", Paragraph("<b>t3.micro turns CRITICAL</b>: 13% waiting at 97% in use. "
                                "Click it for <b>Where the memory is</b> and the likely cause.", TCELL)],
    ["2–3", "1.4 GB", Paragraph("The kernel <b>kills the test on the micro</b>; as its reading "
                                "falls, its card says RAN OUT OF MEMORY. <b>t3.small is 93% in use "
                                "and still HEALTHY.</b>", TCELL)],
    ["3–4", "2 GB", Paragraph("<b>t3.small turns CRITICAL</b>: 22% waiting.", TCELL)],
    ["4–6", "2.6–3.2 GB", Paragraph("<b>t3.medium reaches 95% in use and stays HEALTHY.</b> "
                                    "t3.large is flat.", TCELL)],
], [16 * mm, 22 * mm, 127 * mm]))
A(para("These are the 19 October numbers; the 22 October run matched them within a few "
       "points. States judge a "
       "one-minute average, so a card turns up to a minute after its step begins. Use the "
       "quiet stretches for the Prometheus question (section 4).", SMALL))
E(say("At the 1.4 gigabyte step: look at the small. 93 percent of its memory is in use, so an "
      "80 percent alarm would be ringing. But nothing is waiting, so nothing is slowed down. "
      "That is the difference between memory in use and memory pressure."))
A(para("Each card's <b>Machine load</b> line shows CPU near 100% during the test: the medium "
       "is working flat out with 0% waiting.", SMALL))

A(para("Step 5 — The verdict", H2))
A(para("Open <b>Load test</b>. <b>Which size to choose</b> leads the results.", BODY))
E(say("Choose t3.medium, 32 dollars 70 a month. The small would save 16 dollars 35, but it "
      "holds only about 1.6 gigabytes, 5 percent more than the app needs, and we asked for 25. "
      "The large costs twice as much for room this app does not use. A sizing decision made "
      "from measurement, and said in money. Its last line says what it did not test: the same "
      "4 gigabytes on Graviton or AMD, t4g.medium and t3a.medium, for about half. Those go in "
      "the next run before anyone switches."))
A(para("Then point at the <b>Pressure at each load level</b> chart: where a size's line "
       "crosses the degraded line is the most it can hold. Change <b>Your app at its busiest</b> "
       "or <b>Keep spare</b> and the answer is worked out again, without another test.", BODY))

A(para("Step 5b — All three states at once (optional, six minutes)", H2))
A(para("<b>Load test</b>, <b>Show all three states</b>, <b>Start</b>. From about a minute and a half "
       "to the end: micro CRITICAL, small DEGRADED, medium and large HEALTHY. Micro not red by "
       "step 3? Show the screenshot.", BODY))
E(say("A real server has other work that keeps running, so the small is slowed but still "
      "working: degraded. The medium and large are at 100 percent CPU, and nothing waits."))

A(para("Step 6 — Beyond the dashboard (optional, one minute)", H2))
A(para("The same verdict from the terminal, instantly, from the test that just ran:", BODY))
E(code("URL=http://<dashboard-ip>:5000      # ./aws.sh status prints it\n"
       "python3 cli.py --server $URL report"))
E(say("The same test runs from the command line, so it can gate a build: it fails when the "
      "size the app needs has changed. Prometheus can scrape everything at /metrics, and an "
      "AI agent can run the experiment through an MCP server."))

# ---- 4
A(para("4 &nbsp; Questions you will get", H1))

A(para("“Why is the micro's number so low at the end?”", H2))
E(say("It is not coping, it failed. The load did not fit in its memory and swap, so the "
      "kernel killed it. Pressure fell because the work stopped. That is why the card says "
      "ran out of memory, and the results mark every step after it as critical."))
A(para("Proof, live, if they want it: the kernel's own record, with times that match the chart.",
       SMALL))
E(code("./aws.sh ssh t3.micro 'sudo journalctl -k --since \"20 min ago\" | grep -i killed'"))

A(para("“Why did nothing show DEGRADED?”", H2))
E(say("The test load is the only work on the box, so when it waits, everything waits, and "
      "the whole-machine rule fires as soon as degraded would. Give it other work and it shows, "
      "as step 5b does."))
A(para("No time: <font face='Courier'>~/syshealth-aws-three-states-button.png</font>, 22 October.",
       SMALL))

A(para("“Does it work on a real app?”", H2))
E(say("Yes. We ran a language model, Gemma 4 in llama.cpp, on all four sizes. The micro could "
      "not load it, the small was killed twelve times, and the medium answered all 39 requests "
      "at 80 percent in use with nothing waiting."))

A(para("“How is this different from Prometheus and Grafana?”", H2))
E(say("Prometheus watches. It can graph this same kernel counter, but it never applies a "
      "load, never knows what load caused a reading, and never says what to buy. SysHealth "
      "runs the same load on every size at once, works out how much each holds before the "
      "app slows down, and names the cheapest that fits. They work together too: everything, "
      "the verdict included, is served at /metrics for Prometheus to scrape."))

A(para("“Why not just watch memory in use?”", H2))
E(say("It does not say whether it hurts. At 95 percent in use the medium was stalled 0.03 "
      "percent of the time; at 97 the micro was stalled 13. Users feel the waiting."))

A(para("“A peak test said t3.small. Why does the ramp say t3.medium?”", H2))
E(say("A peak test only asks whether each size can hold the app at 1.6 gigabytes, not how "
      "much more it could take. The ramp measures the room left, and with 25 percent spare "
      "asked for, the small's 5 percent rules it out."))

A(para("“Could this run in production?”", H2))
E(say("The measurement side, yes: Prometheus can scrape it today, and tests need a shared "
      "token. History is kept in memory, so a real deployment would need a database first."))

A(para("“What is it built on?”", H2))
E(say("Python and Flask. The agent reads the kernel's pressure files every five seconds; "
      "Terraform builds or destroys the fleet in one command."))

# ---- 5
A(para("5 &nbsp; If something breaks", H1))

A(para("The dashboard will not load", H2))
A(para("Almost always your address changed: a new network, or a mobile provider moving you "
       "overnight. Or the network blocks port 5000. It looks exactly like a dead server.", BODY))
E(code("./aws.sh up --profile hackathon      # moves the firewall to this address"))
A(expect("<font face='Courier'>0 added, 3 changed, 0 destroyed</font>. Still nothing: use the "
         "phone hotspot and run it again."))
A(para("A phone on the laptop's Wi-Fi can open it too; on mobile data it is blocked.", SMALL))

A(para("A size is missing", H2))
E(code("""./aws.sh status                      # which sizes are not reporting
./aws.sh ssh t3.small 'sudo tail -30 /var/log/syshealth-bootstrap.log'
./aws.sh ssh t3.small 'sudo journalctl -u syshealth -n 30'"""))

A(para("Everything stays HEALTHY under load", H2))
A(para("The load fits. Check the <b>Right now</b> tile moves at all, then raise <b>To</b> in the "
       "form.", BODY))

A(para("Nothing on AWS works and you must present anyway", H2))
A(para("A local simulation needs no AWS and no network: the real dashboard, a simulated fleet.",
       BODY))
E(code("""sudo apt install -y python3-flask python3-requests   # first time only
npm run dev                                          # http://127.0.0.1:5000"""))
A(para("Say it is simulated (every page says <b>Demo data</b>), then show the README's real "
       "results.", SMALL))

# ---- 6
A(para("6 &nbsp; Afterwards", H1))
A(para("Save the evidence first (history lives in the dashboard's memory; "
       "<font face='Courier'>URL</font> as in step 6), then destroy the fleet:", BODY))
E(code("""python3 cli.py --server $URL report --save rightsize-$(date +%F).json
curl -s "$URL/series?limit=720" > demo-evidence-$(date +%F).json
./aws.sh down && ./aws.sh status"""))
A(expect("<font face='Courier'>Nothing is up</font>: billing has stopped. Until then the "
         "fleet bills about $0.22 an hour, a little over $5 a day."))

# ------------------------------------------------------------------ build ---

def furniture(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.6)
    canvas.line(22 * mm, 16 * mm, 188 * mm, 16 * mm)
    canvas.setFont("Helvetica", 7.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(22 * mm, 11 * mm, "SysHealth demo runbook")
    canvas.drawRightString(188 * mm, 11 * mm, "Page %d" % doc.page)
    canvas.restoreState()


def build(path="docs/syshealth-demo-runbook.pdf"):
    doc = BaseDocTemplate(path, pagesize=A4,
                          leftMargin=22 * mm, rightMargin=22 * mm,
                          topMargin=18 * mm, bottomMargin=22 * mm,
                          title="SysHealth demo runbook",
                          author="aryangorde8")
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id="main",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=furniture)])
    doc.build(story)
    return path


if __name__ == "__main__":
    print("wrote", build())

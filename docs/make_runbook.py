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
       spaceBefore=9, spaceAfter=3, keepWithNext=1)
BODY = S("b")
SMALL = S("sm", fontSize=8.5, leading=11.6, textColor=MUTED)
BULLET = S("bu", leftIndent=11, bulletIndent=2, spaceAfter=3)

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
    return [t, Spacer(1, 7)]


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
A(para("Memory pressure across four EC2 instance sizes. "
       "Everything below runs from <font face='Courier'>~/syshealth/terraform</font>.", SUB))

# ---- 1
A(para("1 &nbsp; Fifteen minutes before", H1))
A(para("Do all of this before anyone is watching. The fleet needs three "
       "minutes to boot and calibrate, and you want one rehearsal run in hand.", BODY))

A(para("Bring the fleet up", H2))
E(code("cd ~/syshealth && git pull\n./terraform/relaunch.sh"))
A(para("Checks your credentials, that the AMI is pinned and the demo tag exists, "
       "rewrites <font face='Courier'>ssh_cidr</font> to whatever address you are on now, "
       "then applies and waits until all four sizes report.", SMALL))
A(expect("a table of four sizes, all <font face='Courier'>online=yes</font>, then a dashboard URL."))

A(para("Rehearse once, then let it settle", H2))
E(code("""export SYSHEALTH_TOKEN=$(terraform output -raw token)
curl -X POST -H "Authorization: Bearer $SYSHEALTH_TOKEN" \\
  "$(terraform output -raw dashboard_url)/run-stress"
sleep 45 && ./check.sh"""))
A(para("Starting a test needs the fleet's token, which Terraform made. Keep "
       "<font face='Courier'>terraform output -raw token</font> to hand: the dashboard asks "
       "for it on the first <b>Start</b>.", SMALL))
A(expect("micro and small DEGRADED or CRITICAL, medium and large HEALTHY. "
         "If not, see section 4 before your teacher arrives."))
A(para("Then wait about two minutes for pressure to fall back to zero, so the live "
       "run starts from a flat line.", SMALL))

A(PageBreak())

# ---- 2
A(para("2 &nbsp; The demo", H1))

A(para("Step 1 — Open the dashboard", H2))
E(code('terraform output -raw dashboard_url    # paste into the browser'))
A(para("Four cards, all HEALTHY, all green dots, a flat line across the chart.", SMALL))
E(say("Four EC2 instances, identical software, different sizes: micro at 1 gigabyte "
      "up to large at 8. Each one reads the kernel's own pressure counter every five "
      "seconds and reports here. Right now they are all idle."))

A(para("Step 2 — Explain what is being measured", H2))
E(say("This is PSI — pressure stall information, straight from the Linux kernel. It is "
      "the percentage of time processes were stalled waiting for memory. Not how much "
      "memory is used, but how much the machine is struggling to get it. A box can be "
      "at 90 percent memory and perfectly happy; this measures the pain, not the level."))

A(para("Step 3 — Load all four at once", H2))
A(para("Open <b>Load test</b>. Leave it on <b>Peak load</b>, 1600 MB for 60 seconds, "
       "and point at the preview: the peak is 168% of the micro's RAM and 84% of the "
       "small's. Press <b>Start</b> (pasting the token if it asks). Or from the terminal:", BODY))
E(code('curl -X POST -H "Authorization: Bearer $SYSHEALTH_TOKEN" \\\n'
       '  "$(terraform output -raw dashboard_url)/run-stress"'))
A(para("All four are asked in parallel, within a few milliseconds, so the comparison "
       "is fair. Each runs the same job: two workers, 800 MB each, for 60 seconds.", SMALL))
E(say("Same workload, same moment, four different machines. Watch which ones feel it."))

A(para("Step 4 — Watch the separation (60 to 90 seconds)", H2))
E(table([
    ["Instance", "RAM", "What happens"],
    ["t3.micro", "1 GB", "Spikes hard, then the OOM killer ends the job"],
    ["t3.small", "2 GB", "Climbs and stays there — the worst sustained case"],
    ["t3.medium", "4 GB", "Barely moves"],
    ["t3.large", "8 GB", "Flat"],
], [26 * mm, 16 * mm, 105 * mm]))
E(say("The 1.6 gigabyte job fits comfortably on medium and large — they never notice it. "
      "On small it fits, but only just, so the kernel spends the whole minute reclaiming "
      "memory. That is what pressure looks like."))

A(para("Step 5 — Show one machine on its own", H2))
A(para("Click <b>t3.small</b> in the Instance toggle. The headline is now one number on "
       "a Healthy, Degraded, Critical scale.", BODY))
E(say("That number is the share of the last minute that programs spent waiting for "
      "memory. Ten percent means one second in every ten was lost. Under ten is healthy, "
      "ten to forty is degraded, forty or more is critical. Those lines are not ours: "
      "they come from the systems that act on this signal in production, like Android's "
      "low-memory killer, systemd-oomd and Meta's oomd. The sources are in the repo."))

A(para("Step 6 — The result: which size to buy", H2))
A(para("Open <b>Load test</b> again. <b>Which size to choose</b> leads the results: the "
       "cheapest size that held the app with room to spare, its price a month, and what "
       "the sizes either side would mean.", BODY))
E(say("The answer is t3.medium, about 30 dollars a month. The small would save 15 dollars "
      "but it slows down under this load, and the large costs 30 dollars more for room "
      "this app does not use. That is a sizing decision made from measurement instead of "
      "guesswork, and it is said in money."))
A(para("To show how much room each size leaves, run a <b>Variable load</b> ramp up past "
       "the app's need (say 200 to 3200 MB, six steps). The <b>Pressure at each load level</b> "
       "chart then shows where each size gives out, and the table says how much each holds.",
       SMALL))

A(para("Step 7 — Why, and why not just watch usage", H2))
A(para("Click <b>t3.small</b> while the test runs. Under the tiles, <b>Where the memory "
       "is</b> splits the RAM, lists what holds it, and names the likely cause.", BODY))
E(say("It does not just say the machine is struggling, it says why. On the small, programs "
      "have taken nearly all the RAM, so the kernel is swapping, or throwing out and "
      "re-reading the files they run from, and it shows the numbers. And the report checks "
      "itself against the usual rule, an alarm at 80 percent memory in use, and says when "
      "that rule would have bought the wrong size."))

A(PageBreak())

# ---- 3
A(para("3 &nbsp; Questions you will get", H1))

A(para("“Why is small worse than micro? Shouldn't the smallest be worst?”", H2))
A(para("This is the best question anyone can ask you, and you have the evidence.", BODY))
E(say("It does not degrade — it fails. The job needs 1.6 gigabytes; micro has 1 plus half "
      "a gigabyte of swap. It does not fit, so the kernel kills the job outright. Pressure "
      "collapses because the work stopped, not because the machine coped. Micro's score "
      "understates the problem. Small is the worst case you can actually observe: big "
      "enough to accept the work, too small to finish it."))
A(para("Proof, live, if they want it:", BODY))
E(code("""MICRO=$(terraform output -json instances | python3 -c \\
  "import json,sys; print(json.load(sys.stdin)['t3.micro']['public_ip'])")

ssh -i $KEY ubuntu@$MICRO \\
  'sudo journalctl -k --since "20 min ago" | grep -i "killed process"'"""))
A(para("Prints the kernel's own record of killing the stress job, with a timestamp that "
       "matches the spike on the chart.", SMALL))

A(para("“How is this different from Prometheus and Grafana?”", H2))
E(say("Prometheus watches. It can graph this same kernel counter, but it never applies a "
      "load, never knows what load caused a reading, and never tells you what to buy. "
      "SysHealth runs the same load on every size at once, works out how much each can hold "
      "before the app slows down, and names the cheapest that fits. They work together, "
      "too: SysHealth serves all of it, the verdict included, for Prometheus to scrape."))

A(para("“Why not just watch memory usage?”", H2))
E(say("Usage tells you a number, not whether it hurts. A box sitting at 95 percent with "
      "nothing to reclaim is fine. A box at 60 percent thrashing is not. PSI measures time "
      "lost to waiting, which is the thing users actually feel."))

A(para("“How do the health states work?”", H2))
E(say("Fixed thresholds on the kernel's stall measurement, the same on every size, because "
      "the number is already a share of time. Under ten percent is healthy, forty or more, "
      "or the whole machine frozen ten percent of the time, is critical. They are judged on "
      "a one-minute average and must hold for fifteen seconds before the state changes, so "
      "one spike is not reported as a failure. We used to compare against each box's idle "
      "baseline, but dividing by an idle reading near zero turned a tenth of a percent into "
      "a critical alarm."))

A(para("“What is it built on?”", H2))
E(say("Python and Flask. The agent reads /proc/pressure/memory and /proc/vmstat. The chart "
      "is hand-written SVG, so there is no charting library and nothing loaded from the "
      "internet. The whole fleet is Terraform — one command builds all five machines."))

A(para("“Could this run in production?”", H2))
E(say("The measurement side, yes, and Prometheus can scrape it today. Two things would "
      "need work first: history is kept in memory and is lost if the server restarts, so it "
      "would need a real datastore. And the dashboard has no authentication — right now it is "
      "locked to a single IP address, which is fine for a demo and not for anything real."))

A(PageBreak())

# ---- 4
A(para("4 &nbsp; If something breaks", H1))

A(para("The dashboard will not load", H2))
A(para("Almost always your IP address changed — a different network, or the ISP moved you. "
       "The security group still allows the old one. It fails silently and looks exactly "
       "like a server that never started.", BODY))
E(code("./terraform/relaunch.sh    # detects it, fixes only the firewall rule"))
A(expect("<font face='Courier'>0 to add, 3 to change, 0 to destroy</font>. "
         "No instance may appear in that plan."))

A(para("A size is missing from the dashboard", H2))
E(code("""./check.sh                       # names which sizes are not reporting
terraform output instances       # public IP of each
ssh -i $KEY ubuntu@<ip> 'sudo tail -30 /var/log/syshealth-bootstrap.log'
ssh -i $KEY ubuntu@<ip> 'sudo journalctl -u syshealth -n 30'"""))
A(para("An agent that is running but cannot reach the dashboard says so in its own log: "
       "<font face='Courier'>push to http://... failed</font>.", SMALL))

A(para("Everything says HEALTHY even under load", H2))
A(para("The load fits. States are fixed thresholds now, so HEALTHY under load means no "
       "program waited long enough to matter. Check the <b>Right now</b> tile is moving at "
       "all, then raise <b>Memory to hold</b> in the Load test form, or run a variable "
       "ramp to find where each size gives way.", BODY))
A(para("Flat at 0.00 on every size even while a test runs means stress is not running: "
       "<font face='Courier'>sudo journalctl -u syshealth -n 30</font> on that box.", SMALL))

A(para("Nothing works and you must present anyway", H2))
A(para("There is a local simulation that needs no AWS and no network. It serves the real "
       "dashboard over a simulated four-size fleet, and every page is marked DEMO.", BODY))
E(code("""cd ~/syshealth
sudo apt install -y python3-flask python3-requests   # first time only
npm run dev        # then open http://127.0.0.1:5000"""))
A(para("Say plainly that it is simulated. The states and thresholds still come from the "
       "same analysis code that runs on the real instances — only the sensor is faked.", SMALL))

A(para("5 &nbsp; Afterwards", H1))
A(para("Save the evidence before you destroy anything. History lives in the server's memory "
       "and goes with it. The verdict, as JSON:", BODY))
E(code("""python3 ../cli.py --server "$(terraform output -raw dashboard_url)" \\
  report --save ~/syshealth/report-$(date +%F).json"""))
A(para("And the raw samples:", BODY))
E(code("""curl -s "$(terraform output -raw dashboard_url)/series?limit=720" \\
  > ~/syshealth/demo-evidence-$(date +%F).json

terraform destroy

aws ec2 describe-instances --region ap-south-1 \\
  --filters Name=instance-state-name,Values=running,pending \\
  --query 'Reservations[].Instances[].[InstanceId,InstanceType]' --output text"""))
A(expect("empty output from the last command. That means billing has stopped."))
A(para("Five instances cost roughly US$5 a day and bill until destroyed.", SMALL))


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

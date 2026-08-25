"""The operator dashboard.

One self-contained page, served by the fleet server. No build step, no CDN, no
framework — it reads the same JSON API anything else would, which keeps the API
honest: if the dashboard needs something the API cannot express, the API is
wrong.

Three things it has to make obvious, because they are what separates an
autonomous system you can supervise from one you cannot:

**What the system did, and why.** Every incident shows its evidence, its
diagnosis, the ruling that authorised or refused the action, and what happened
afterwards. The timeline is the primary view, not a detail pane.

**What it wants to do.** Pending approvals are at the top, with the blast
radius spelled out, because someone is about to make a decision on that basis.

**What mode it is in.** A person looking at this must never have to guess
whether the system can act right now.

Status colour follows the reserved good/warning/serious/critical roles and is
always paired with a glyph and a word — several of those steps sit below 3:1 on
a light surface by design, so colour never carries the meaning on its own.
"""

from __future__ import annotations

DASHBOARD = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SysHealth — autonomous SRE</title>
<style>
:root {
  color-scheme: light;
  --plane:      #f9f9f7;
  --surface:    #fcfcfb;
  --border:     #e2e1dc;
  --ink:        #0b0b0b;
  --ink-2:      #52514e;
  --muted:      #898781;
  --good:       #0ca30c;
  --warning:    #fab219;
  --serious:    #ec835a;
  --critical:   #d03b3b;
  --accent:     #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane:   #0d0d0d;
    --surface: #1a1a19;
    --border:  #33322f;
    --ink:     #ffffff;
    --ink-2:   #c3c2b7;
    --muted:   #898781;
    --accent:  #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane: #0d0d0d; --surface: #1a1a19; --border: #33322f;
  --ink: #ffffff; --ink-2: #c3c2b7; --accent: #3987e5;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 1.5rem 4rem;
  background: var(--plane); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
header {
  display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
  padding: 1.5rem 0 1rem; border-bottom: 1px solid var(--border); margin-bottom: 1.5rem;
}
h1 { font-size: 1.15rem; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.07em;
     color: var(--muted); margin: 2rem 0 0.75rem; font-weight: 600; }
main { max-width: 1100px; margin: 0 auto; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }

.mode {
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em;
  padding: 0.2rem 0.6rem; border-radius: 999px; border: 1px solid var(--border);
  color: var(--ink-2);
}
.mode[data-mode="AUTONOMOUS"] { border-color: var(--critical); color: var(--critical); }
.mode[data-mode="ASSIST"]     { border-color: var(--warning); color: var(--ink-2); }
.stale { color: var(--muted); font-size: 0.8rem; margin-left: auto; }

/* Stat tiles: a headline number is not a chart. */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem; }
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 0.9rem 1rem;
}
.tile .n { font-size: 1.9rem; font-weight: 650; letter-spacing: -0.02em; line-height: 1.1; }
.tile .k { font-size: 0.78rem; color: var(--ink-2); display: flex; align-items: center; gap: 0.35rem; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }

.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 0.75rem;
}
.card.pending { border-left: 3px solid var(--warning); }
.row { display: flex; gap: 1rem; align-items: baseline; flex-wrap: wrap; }
.grow { flex: 1 1 auto; min-width: 0; }
.sub { color: var(--ink-2); font-size: 0.87rem; }
.muted { color: var(--muted); font-size: 0.82rem; }

/* Status is never colour alone: glyph + word travel with it. */
.state { display: inline-flex; align-items: center; gap: 0.35rem;
         font-size: 0.75rem; font-weight: 600; letter-spacing: 0.03em; }
.state .g { font-size: 0.9em; }

button {
  font: inherit; font-size: 0.82rem; font-weight: 550;
  padding: 0.35rem 0.85rem; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--ink);
}
button:hover { border-color: var(--muted); }
button.approve { border-color: var(--good); color: var(--good); }
button.reject  { border-color: var(--critical); color: var(--critical); }
button:disabled { opacity: 0.45; cursor: not-allowed; }

.timeline { list-style: none; margin: 0.5rem 0 0; padding: 0; }
.timeline li {
  display: grid; grid-template-columns: 4.5rem 6.5rem 1fr; gap: 0.75rem;
  padding: 0.3rem 0; border-top: 1px dashed var(--border); font-size: 0.85rem;
}
.timeline li:first-child { border-top: none; }
.timeline .t { color: var(--muted); font-family: ui-monospace, monospace; font-size: 0.78rem; }
.timeline .k { color: var(--ink-2); font-size: 0.72rem; text-transform: uppercase;
               letter-spacing: 0.05em; }

details { margin-top: 0.6rem; }
summary { cursor: pointer; font-size: 0.82rem; color: var(--ink-2); }
pre {
  background: var(--plane); border: 1px solid var(--border); border-radius: 7px;
  padding: 0.7rem 0.85rem; overflow-x: auto; font-size: 0.78rem; margin: 0.5rem 0 0;
}
ul.facts { margin: 0.4rem 0 0; padding-left: 1.1rem; font-size: 0.86rem; color: var(--ink-2); }
ul.facts li { margin: 0.15rem 0; }
.empty { color: var(--muted); font-size: 0.88rem; padding: 0.5rem 0; }
.err { color: var(--critical); font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: 0.72rem;
     text-transform: uppercase; letter-spacing: 0.05em; padding: 0.3rem 0.6rem 0.3rem 0; }
td { padding: 0.35rem 0.6rem 0.35rem 0; border-top: 1px solid var(--border); }
.wrap { overflow-x: auto; }
</style>
</head>
<body>
<main>
  <header>
    <h1>SysHealth</h1>
    <span class="mode" id="mode">—</span>
    <span class="stale" id="stale"></span>
  </header>

  <div id="error" class="err"></div>

  <h2>Fleet</h2>
  <div class="tiles" id="tiles"></div>

  <h2>Awaiting approval</h2>
  <div id="approvals"></div>

  <h2>Incidents</h2>
  <div id="incidents"></div>
</main>

<script>
// Status roles are reserved and always ship with a glyph and a word, because
// warning and serious sit below 3:1 on the light surface by design.
const STATE = {
  HEALTHY:   { c: "var(--good)",     g: "\\u25CF", w: "healthy"   },
  DEGRADED:  { c: "var(--warning)",  g: "\\u25B2", w: "degraded"  },
  SATURATED: { c: "var(--critical)", g: "\\u25A0", w: "saturated" },
  UNKNOWN:   { c: "var(--muted)",    g: "\\u25CB", w: "unknown"   },
  CRITICAL:  { c: "var(--critical)", g: "\\u25A0", w: "critical"  },
  WARNING:   { c: "var(--warning)",  g: "\\u25B2", w: "warning"   },
  INFO:      { c: "var(--muted)",    g: "\\u25CB", w: "info"      },
  RESOLVED:  { c: "var(--good)",     g: "\\u2713", w: "resolved"  },
  ESCALATED: { c: "var(--serious)",  g: "\\u2191", w: "escalated" },
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

function badge(key) {
  const s = STATE[key] || { c: "var(--muted)", g: "\\u25CB", w: String(key||"").toLowerCase() };
  return `<span class="state" style="color:${s.c}"><span class="g">${s.g}</span>${esc(s.w)}</span>`;
}
function tile(n, label, key) {
  const s = STATE[key] || { c: "var(--muted)" };
  return `<div class="tile"><div class="n">${n}</div>
    <div class="k"><span class="dot" style="background:${s.c}"></span>${esc(label)}</div></div>`;
}

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

function renderFleet(fleet) {
  const by = { HEALTHY: 0, DEGRADED: 0, SATURATED: 0, UNKNOWN: 0 };
  for (const n of fleet.nodes || []) by[n.state] = (by[n.state] || 0) + 1;
  const money = (fleet.monthly_delta_usd || 0).toFixed(2);

  document.getElementById("tiles").innerHTML = [
    tile((fleet.nodes || []).length, "instances", "INFO"),
    tile(by.HEALTHY, "healthy", "HEALTHY"),
    tile(by.DEGRADED, "degraded", "DEGRADED"),
    tile(by.SATURATED, "saturated", "SATURATED"),
    tile("$" + money, "monthly delta", "INFO"),
  ].join("");
}

function renderApprovals(data) {
  const host = document.getElementById("approvals");
  if (!data.count) { host.innerHTML = `<div class="empty">Nothing waiting.</div>`; return; }

  host.innerHTML = data.actions.map((a) => `
    <div class="card pending">
      <div class="row">
        <div class="grow">
          <strong class="mono">${esc(a.action)}</strong>
          <span class="mono muted">${esc(JSON.stringify(a.arguments))}</span>
          &nbsp;${badge(a.tier === "HIGH_RISK" ? "CRITICAL" : "WARNING")}
          <div class="sub">on <strong>${esc(a.node)}</strong> for ${esc(a.incident_id)}</div>
          <div class="sub">Because: ${esc(a.reason)}</div>
          <div class="muted">Policy: ${esc(a.ruling)}</div>
        </div>
        <div>
          <button class="approve" onclick="decide(${a.id}, true)">Approve</button>
          <button class="reject" onclick="decide(${a.id}, false)">Reject</button>
        </div>
      </div>
    </div>`).join("");
}

async function decide(id, approve) {
  document.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const who = window.prompt(approve ? "Approve as:" : "Reject as:", "operator");
    if (who === null) return;
    const r = await fetch(`/actions/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approve, by: who }),
    });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
  } catch (e) {
    document.getElementById("error").textContent = String(e);
  } finally {
    document.querySelectorAll("button").forEach((b) => (b.disabled = false));
    refresh();
  }
}

function facts(title, items) {
  if (!items || !items.length) return "";
  return `<div class="sub" style="margin-top:.5rem"><strong>${esc(title)}</strong></div>
    <ul class="facts">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
}

function renderIncident(report) {
  const inc = report.incident;
  const dx = (report.diagnoses || []).slice(-1)[0];
  const vf = (report.verifications || []).slice(-1)[0];

  const evidence = (report.evidence || []).map((e) =>
    `<tr><td class="mono">#${e.id}</td><td class="mono">${esc(e.tool)}</td>
     <td class="mono muted">${esc(JSON.stringify(e.arguments))}</td>
     <td>${e.ok ? "ok" : '<span class="err">failed</span>'}</td></tr>`).join("");

  const actions = (report.actions || []).map((a) =>
    `<tr><td class="mono">${esc(a.action)}</td>
     <td class="mono muted">${esc(JSON.stringify(a.arguments))}</td>
     <td>${badge(a.status === "SUCCEEDED" ? "RESOLVED"
              : a.status === "DENIED" || a.status === "FAILED" ? "CRITICAL" : "WARNING")}
         <span class="muted">${esc(a.status)}</span></td>
     <td class="muted">${esc(a.ruling)}</td></tr>`).join("");

  const checks = vf ? vf.checks.map((c) =>
    `<li>${c.passed ? "\\u2713" : "\\u2717"} ${esc(c.name)} — ${esc(c.observed)}</li>`).join("") : "";

  return `
  <div class="card">
    <div class="row">
      <div class="grow">
        <strong>${esc(inc.id)}</strong> ${badge(inc.severity)} ${badge(inc.status)}
        <div class="sub">${esc(inc.title)}</div>
      </div>
      <div class="muted">${esc(inc.node)} · ${Math.round(inc.age_s)}s · mode ${esc(inc.mode)}</div>
    </div>

    ${dx ? `
      <div class="sub" style="margin-top:.7rem">
        <strong>Probable cause:</strong> ${esc(dx.cause)}
        <span class="muted">(confidence ${esc(dx.confidence)}, by ${esc(dx.reasoner)},
        citing evidence ${dx.cites.map((c) => "#" + c).join(", ")})</span>
      </div>
      ${facts("Observed", dx.observations)}
      ${facts("Inferred", dx.hypotheses)}` : ""}

    ${vf ? `<div class="sub" style="margin-top:.7rem">
      <strong>Verification:</strong> ${esc(vf.summary)}</div>
      <ul class="facts">${checks}</ul>` : ""}

    ${inc.resolution ? `<div class="sub" style="margin-top:.5rem">
      <strong>Resolution:</strong> ${esc(inc.resolution)}</div>` : ""}

    <details><summary>Timeline (${inc.timeline.length} events)</summary>
      <ul class="timeline">${inc.timeline.map((e) =>
        `<li><span class="t">${esc(e.clock)}</span><span class="k">${esc(e.kind)}</span>
         <span>${esc(e.message)}</span></li>`).join("")}</ul>
    </details>

    ${evidence ? `<details><summary>Evidence (${report.evidence.length})</summary>
      <div class="wrap"><table><thead><tr><th>id</th><th>tool</th><th>args</th><th></th></tr></thead>
      <tbody>${evidence}</tbody></table></div></details>` : ""}

    ${actions ? `<details><summary>Actions (${report.actions.length})</summary>
      <div class="wrap"><table><thead><tr><th>action</th><th>args</th><th>status</th><th>ruling</th></tr></thead>
      <tbody>${actions}</tbody></table></div></details>` : ""}
  </div>`;
}

async function refresh() {
  const err = document.getElementById("error");
  try {
    const [fleet, incidents, approvals, cat] = await Promise.all([
      get("/fleet"), get("/incidents?limit=25"), get("/approvals"), get("/actions/catalogue"),
    ]);
    err.textContent = "";

    renderFleet(fleet);
    renderApprovals(approvals);

    const host = document.getElementById("incidents");
    if (!incidents.count) {
      host.innerHTML = `<div class="empty">No incidents. ${cat.actions.length} actions in the
        catalogue; nothing outside it can be performed.</div>`;
    } else {
      const reports = await Promise.all(
        incidents.incidents.map((i) => get("/incidents/" + i.id)));
      host.innerHTML = reports.map(renderIncident).join("");
      const modes = new Set(incidents.incidents.map((i) => i.mode));
      const mode = modes.size === 1 ? [...modes][0] : "MIXED";
      const el = document.getElementById("mode");
      el.textContent = mode; el.dataset.mode = mode;
    }
    document.getElementById("stale").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    err.textContent = "Could not load: " + e;
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

"""
The design system.

Split into three so there is ONE source of truth for the look, shared by the
web app and the standalone report:

  TOKENS      colour, and the base element rules. Everything else references
              these; no component hard-codes a colour.
  COMPONENTS  cards, tables, pills, stats, money panels, findings, banners.
              Used by anything that renders findings, app or not.
  SHELL       the app frame - rail, top bar, page grid. Only the web app.

report.py imports TOKENS and COMPONENTS. It used to carry its own palette,
which drifted the moment the app was restyled and left two artefacts of the
same product looking like different products.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from engine.expected_value import rupees
from merchant.auth import ROLE_LABEL
from merchant.gateway import BEHAVIOUR_LABEL, BEHAVIOUR_NOTE, Behaviour

TOKENS = """
:root {
  /* Light, deliberately, with no dark variant.
     This is a finance dashboard shown on other people's machines and on
     projectors, and a palette that flips with the viewer's OS setting means
     nobody can be sure what it will look like when it matters. Committing to
     one palette costs a preference and buys certainty.
     `color-scheme: light` is the part people miss: without it, a browser in
     dark mode renders native inputs, selects and scrollbars dark anyway, and
     the form controls end up fighting the page. */
  color-scheme: light;
  --bg:#f6f7f9; --surface:#fff; --raised:#fbfcfd;
  --ink:#0f1724; --ink-2:#374151; --muted:#6b7280; --faint:#9ca3af;
  --line:#e6e8ec; --line-2:#eef0f3;
  --brand:#2b6cf0; --brand-ink:#1d4ed8; --brand-wash:#eef4ff;
  --good:#0f9d58; --good-wash:#e8f6ee;
  --warn:#b45309; --warn-wash:#fff8e6;
  --danger:#c2410c; --danger-wash:#fff1ea;
  --violet:#6d38d6; --violet-wash:#f3edff;
  --shadow:0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06);
}
* { box-sizing:border-box }
html, body { height:100% }
body { margin:0; background:var(--bg); color:var(--ink);
  font:13.5px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Inter,sans-serif;
  -webkit-font-smoothing:antialiased }
a { color:var(--brand-ink); text-decoration:none }

"""

SHELL = """/* --- frame ------------------------------------------------------------ */
.app { display:grid; grid-template-columns:216px 1fr; min-height:100vh }
.rail { background:var(--surface); border-right:1px solid var(--line);
  display:flex; flex-direction:column; position:sticky; top:0; height:100vh }
.rail .logo { display:flex; align-items:center; gap:8px; padding:14px 14px 10px;
  font-weight:680; letter-spacing:-.02em; font-size:14.5px; color:var(--ink) }
.mark { width:21px; height:21px; border-radius:5px; background:var(--brand);
  color:#fff; display:grid; place-items:center; font-size:12px; font-weight:700 }
.rail nav { padding:2px 8px; overflow-y:auto; flex:1 }
.group { font-size:10px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); padding:13px 7px 4px; font-weight:600 }
.item { display:flex; align-items:center; gap:9px; padding:6px 8px;
  border-radius:6px; color:var(--ink-2); font-size:12.8px; margin-bottom:0 }
.item:hover { background:var(--line-2) }
.item.on { background:var(--brand-wash); color:var(--brand-ink); font-weight:560 }
.item .ic { width:17px; text-align:center; opacity:.75; font-size:13px }
.item .tag { margin-left:auto; font-size:10px; padding:1px 6px; border-radius:20px;
  background:var(--line-2); color:var(--muted) }
.item.muted { color:var(--faint); cursor:default }
.item.muted:hover { background:transparent }
.rail .foot { border-top:1px solid var(--line); padding:7px 8px }

/* --- top bar ---------------------------------------------------------- */
.top { height:48px; background:var(--surface); border-bottom:1px solid var(--line);
  display:flex; align-items:center; gap:12px; padding:0 18px; position:sticky;
  top:0; z-index:5 }
.top .biz { display:flex; align-items:center; gap:8px }
.top select { border:1px solid var(--line); background:var(--raised);
  border-radius:6px; padding:4px 9px; font-size:12.8px; color:var(--ink);
  font-family:inherit; max-width:210px }
.sp { flex:1 }
.mode { display:inline-flex; align-items:center; gap:6px; padding:3px 9px;
  border-radius:20px; font-size:10.5px; font-weight:650; letter-spacing:.05em;
  text-transform:uppercase }
.mode.sim { background:var(--warn-wash); color:var(--warn) }
.mode.ok  { background:var(--good-wash); color:var(--good) }
.dot { width:6px; height:6px; border-radius:50%; background:currentColor }


@media (max-width:900px) {
  .app { grid-template-columns:1fr }
  .rail { position:static; height:auto; flex-direction:row; align-items:center;
    overflow-x:auto; border-right:0; border-bottom:1px solid var(--line) }
  .rail .logo { padding:12px 16px } .rail nav { display:flex; padding:0 8px }
  .group, .rail .foot { display:none }
  main { padding:18px 16px 60px }
}
"""

COMPONENTS = """/* --- content ---------------------------------------------------------- */
main { padding:20px 24px 60px; max-width:1120px }
h1 { font-size:18px; margin:0 0 2px; letter-spacing:-.02em; font-weight:640 }
h2 { font-size:13px; margin:0 0 2px; letter-spacing:-.01em; font-weight:620 }
.sub { color:var(--muted); font-size:12.3px; margin:0 0 12px }
.card { background:var(--surface); border:1px solid var(--line);
  border-radius:9px; padding:15px 16px; margin-bottom:11px; box-shadow:var(--shadow) }
.card.tint { background:var(--brand-wash); border-color:transparent }
.card.flush { padding:0; overflow:hidden }
.card-head { padding:11px 16px; border-bottom:1px solid var(--line-2);
  display:flex; align-items:center; gap:12px }
.card-head h2 { margin:0 }

/* --- forms ------------------------------------------------------------ */
label { display:block; font-size:12px; color:var(--muted); margin-bottom:5px;
  font-weight:500 }
input, select, textarea { width:100%; padding:7px 10px; font-size:13px;
  border:1px solid var(--line); border-radius:8px; background:var(--raised);
  color:var(--ink); font-family:inherit }
input:focus, select:focus, textarea:focus { outline:2px solid var(--brand-wash);
  border-color:var(--brand) }
input[readonly] { color:var(--muted); background:var(--line-2) }
.row { display:flex; gap:9px; align-items:flex-end; flex-wrap:wrap }
.row > div { flex:1; min-width:132px }
button, .btn { padding:7px 13px; font-size:12.8px; font-weight:560; border:0;
  border-radius:7px; background:var(--brand); color:#fff; cursor:pointer;
  font-family:inherit; text-decoration:none; display:inline-block;
  white-space:nowrap }
button:hover, .btn:hover { background:var(--brand-ink) }
button.ghost, .btn.ghost { background:var(--surface); color:var(--ink-2);
  border:1px solid var(--line) }
button.ghost:hover, .btn.ghost:hover { background:var(--line-2) }
/* Both selectors. `.small` matched only <button>, so every
   <a class="btn ... small"> rendered full size and drew as much
   attention as the primary action beside it. */
button.small, .btn.small { padding:3px 9px; font-size:11.5px;
  font-weight:540 }
button:disabled { opacity:.45; cursor:default }
details > summary { list-style:none }
details > details.finding > summary::-webkit-details-marker { display:none }
details > summary::before { content:'▸'; display:inline-block;
  margin-right:7px; color:var(--muted); transition:transform .12s }
details[open] > summary::before { transform:rotate(90deg) }

/* --- tables ----------------------------------------------------------- */
table { width:100%; border-collapse:collapse; font-size:12.6px }
th { text-align:left; font-weight:600; color:var(--muted); font-size:10px;
  text-transform:uppercase; letter-spacing:.05em; padding:7px 14px;
  background:var(--raised); border-bottom:1px solid var(--line-2) }
td { padding:7px 14px; border-bottom:1px solid var(--line-2);
  font-variant-numeric:tabular-nums; color:var(--ink-2) }
tr:last-child td { border-bottom:0 }
td.mono, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12px }
td.r, th.r { text-align:right }
.pill { display:inline-block; border-radius:4px; padding:1px 6px;
  font-size:10.5px; font-weight:600; background:var(--line-2); color:var(--muted) }
.pill.good { background:var(--good-wash); color:var(--good) }
.pill.warn { background:var(--warn-wash); color:var(--warn) }
.pill.danger { background:var(--danger-wash); color:var(--danger) }
.pill.violet { background:var(--violet-wash); color:var(--violet) }
.pill.brand { background:var(--brand-wash); color:var(--brand-ink) }

/* --- stats ------------------------------------------------------------ */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
  gap:2px; background:var(--line-2); border-radius:10px; overflow:hidden }
.stat { background:var(--surface); padding:11px 14px }
.stat b { display:block; font-size:19px; letter-spacing:-.03em; line-height:1.25;
  font-weight:660 }
.stat span { color:var(--muted); font-size:11.3px }
.stat.good b { color:var(--good) } .stat.bad b { color:var(--danger) }

/* --- money ------------------------------------------------------------ */
.money { display:grid; grid-template-columns:1fr auto; gap:4px 20px;
  font-size:12.6px }
.money .lbl { color:var(--muted) }
.money .val { text-align:right; font-variant-numeric:tabular-nums;
  color:var(--ink-2) }
.money .total { border-top:1px solid var(--line); padding-top:9px;
  font-weight:640; color:var(--ink) }

/* --- findings --------------------------------------------------------- */
details.finding { border:1px solid var(--line); border-radius:8px;
  margin-bottom:5px; background:var(--surface); box-shadow:var(--shadow) }
details.finding.rec { border-left:3px solid var(--danger) }
details.finding[open] { border-color:var(--brand) }
details.finding > summary { cursor:pointer; padding:8px 14px;
  display:grid;
  grid-template-columns:150px 1fr 1fr 96px 80px; gap:10px; align-items:center;
  font-size:12.4px; list-style:none }
summary::-webkit-details-marker { display:none }
details.finding > summary:hover { background:var(--raised) }
.detail { padding:2px 14px 14px; border-top:1px solid var(--line-2) }
.detail p { margin:13px 0 9px; color:var(--ink-2) }
.numbers { display:grid; grid-template-columns:repeat(auto-fit,minmax(96px,1fr));
  gap:9px; font-size:11.3px; color:var(--muted); padding:9px 12px;
  background:var(--raised); border-radius:8px; margin:12px 0 }
.numbers b { display:block; color:var(--ink); font-size:12.6px;
  font-variant-numeric:tabular-nums }
pre { margin:0; padding:12px; font-size:11.5px; line-height:1.5;
  white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--raised); border:1px solid var(--line); border-radius:8px;
  overflow-x:auto; color:var(--ink-2) }

/* --- misc ------------------------------------------------------------- */
.banner { border-radius:7px; padding:9px 12px; font-size:12.3px;
  margin-bottom:11px; display:flex; gap:10px; align-items:flex-start }
.banner.warn { background:var(--warn-wash); color:var(--warn) }
.banner.brand { background:var(--brand-wash); color:var(--brand-ink) }
.banner a { color:inherit; text-decoration:underline }
.empty { color:var(--muted); font-size:12.6px; padding:26px 16px;
  text-align:center }
.progress { height:6px; background:var(--line-2); border-radius:4px;
  overflow:hidden }

/* --- the agent's terminal ----------------------------------------------- */
/* Every class here is prefixed. The first version reused `.mark` and `.step`,
   which are the rail's logo chip and the first-run checklist - so the terminal
   rendered with blue chips and separator lines between every line. A shared
   stylesheet makes generic class names a collision waiting to happen. */
.agentterm { border-radius:10px; overflow:hidden; background:#0f1319;
  border:1px solid #1e242d }
.at-bar { display:flex; align-items:center; gap:7px; padding:9px 13px;
  background:#161b22; border-bottom:1px solid #1e242d }
.at-dot { width:10px; height:10px; border-radius:50%; flex:0 0 10px }
.at-dot.r { background:#ff5f57 } .at-dot.a { background:#febc2e }
.at-dot.g { background:#28c840 }
.at-title { margin-left:7px; color:#7d8792; font-size:11.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace }
.at-status { margin-left:auto; font-size:10px; font-weight:700;
  letter-spacing:.1em; font-family:ui-monospace,Menlo,monospace }
.at-status.running { color:#febc2e }
.at-status.complete { color:#28c840 }
.at-status.failed { color:#ff5f57 }
.at-body { padding:12px 13px 14px; font-size:11.7px; line-height:1.85;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  max-height:460px; overflow-y:auto }
.at-ln { display:flex; gap:9px; align-items:baseline }
.at-t { color:#414a55; flex:0 0 auto; font-size:11px }
.at-p { color:#4b5563; flex:0 0 auto }
.at-x { color:#8b95a5; white-space:pre-wrap; word-break:break-word }
.at-ln.say  .at-x { color:#79838f }
.at-ln.do   .at-x { color:#5ddc7a }
.at-ln.think .at-x { color:#67b8ff }
.at-ln.fact .at-x { color:#e8eaed; font-weight:600 }
.at-ln.ok   .at-x { color:#5ddc7a; font-weight:600 }
.at-ln.tool .at-x { color:#9d8cff }
.at-ln.note .at-x { color:#febc2e }
.at-ln.fail .at-x { color:#ff7b72 }
.at-ln.ok .at-p, .at-ln.do .at-p { color:#28c840 }
.at-ln.note .at-p { color:#febc2e }
.at-ln.fail .at-p { color:#ff5f57 }
.at-ln.tool .at-p { color:#9d8cff }
.at-cursor { display:inline-block; width:7px; height:13px; background:#5ddc7a;
  animation:at-blink 1.1s step-end infinite; vertical-align:-2px }
@keyframes at-blink { 50% { opacity:0 } }

/* --- ask ---------------------------------------------------------------- */
.qa { margin-bottom:13px }
.qa-q { display:flex; gap:9px; align-items:baseline; margin-bottom:9px }
.qa-who { flex:0 0 auto; font-size:10px; text-transform:uppercase;
  letter-spacing:.07em; font-weight:650; color:var(--faint); padding-top:2px }
.qa-text { font-size:14px; font-weight:560; color:var(--ink) }
.qa-a { background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--brand); border-radius:9px; padding:15px 17px;
  box-shadow:var(--shadow) }
.qa-a p { margin:0 0 10px; color:var(--ink-2); font-size:13.6px; line-height:1.62 }
.qa-a p:last-of-type { margin-bottom:0 }
.qa-meta { margin-top:12px; padding-top:10px; border-top:1px solid var(--line-2);
  color:var(--faint); font-size:11px; display:flex; gap:8px; flex-wrap:wrap }
.qa-thinking { color:var(--muted); font-size:13.5px }
.qa-dots::after { content:''; animation:qa-dots 1.4s steps(4,end) infinite }
@keyframes qa-dots { 0%{content:''} 25%{content:'.'} 50%{content:'..'}
  75%{content:'...'} }
.chip { display:inline-block; padding:6px 11px; margin:0 6px 6px 0;
  border:1px solid var(--line); border-radius:20px; background:var(--surface);
  color:var(--ink-2); font-size:12.3px; cursor:pointer; font-family:inherit }
.chip:hover { border-color:var(--brand); color:var(--brand-ink);
  background:var(--brand-wash) }
.scope { display:flex; gap:18px; flex-wrap:wrap; font-size:12px;
  color:var(--muted) }
.scope b { color:var(--ink); font-weight:600 }

/* --- first run --------------------------------------------------------- */
.steps { list-style:none; margin:0; padding:0 }
.step { display:flex; gap:12px; padding:12px 0; border-bottom:1px solid var(--line-2) }
.step:last-child { border-bottom:0 }
.step .tick { width:20px; height:20px; border-radius:50%; flex:0 0 20px;
  display:grid; place-items:center; font-size:11px; font-weight:700;
  background:var(--line-2); color:var(--faint); margin-top:1px }
.step.done .tick { background:var(--good-wash); color:var(--good) }
.step.now .tick { background:var(--brand); color:#fff }
.step .what { flex:1 }
.step .what b { font-size:13px; display:block }
.step.later .what b { color:var(--faint) }
.step .what span { color:var(--muted); font-size:12px }
.step .go { flex:0; align-self:center }
.progress div { height:6px; background:var(--brand); width:0;
  transition:width .4s }
.split { display:grid; grid-template-columns:1fr 1fr; gap:11px }

@media (max-width:900px) {
  .split, details.finding > summary { grid-template-columns:1fr }
}
"""

# What the web app loads.
COMPONENTS += """
/* --- second-level rail items ------------------------------------------ */
.rail .item.sub-item { padding-left:30px; font-size:12.6px; color:var(--ink-2) }
.rail .item.sub-item .ic { opacity:.5 }

/* --- agent workspace header ------------------------------------------- */
.agent-head { border-bottom:1px solid var(--line); margin:0 0 18px }
.agent-title { display:flex; align-items:flex-start; gap:14px;
  flex-wrap:wrap; padding-bottom:13px }
.agent-title h1 { margin:0 }
.agent-meta { margin-left:auto; display:flex; align-items:center; gap:9px;
  flex-wrap:wrap }
.tabs { display:flex; gap:2px; flex-wrap:wrap; margin-bottom:-1px }
.tab { padding:8px 13px; font-size:12.8px; font-weight:540; color:var(--muted);
  border-bottom:2px solid transparent; text-decoration:none;
  white-space:nowrap }
.tab:hover { color:var(--ink) }
.tab.on { color:var(--brand-ink); border-bottom-color:var(--brand) }

/* --- agent cards on the hub ------------------------------------------- */
.agent-card { display:flex; flex-direction:column; gap:13px }
.agent-card.muted { opacity:.62 }
.agent-card-head { display:flex; align-items:flex-start; gap:10px }
.agent-card-head > div:first-child { flex:1 }
.agent-card-name { font-weight:600; font-size:14px; letter-spacing:-.01em }
.agent-card-foot { margin-top:auto }
.minis { display:flex; gap:18px; flex-wrap:wrap }
.mini b { display:block; font-size:17px; font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums }
.mini span { font-size:11.3px; color:var(--muted) }
"""

COMPONENTS += """
/* --- a reconciliation finding ----------------------------------------- */
.finding-card { background:var(--surface); border:1px solid var(--line);
  border-radius:10px; padding:16px 18px; margin-bottom:11px;
  box-shadow:var(--shadow) }
.finding-card-top { display:flex; align-items:flex-start; gap:12px;
  margin-bottom:9px }
.finding-card-top > div:first-child { flex:1 }
.finding-card-who { font-weight:600; font-size:14px; letter-spacing:-.01em }
.finding-card-inv { color:var(--muted); font-size:11.8px; margin-top:2px }
.finding-card-why { margin:0 0 13px; color:var(--ink-2); font-size:13.2px;
  max-width:68ch }

.facts { display:flex; flex-wrap:wrap; gap:22px; padding:12px 0;
  border-top:1px solid var(--line-2); border-bottom:1px solid var(--line-2) }
.fact span { display:block; font-size:10.8px; letter-spacing:.05em;
  text-transform:uppercase; color:var(--muted); margin-bottom:3px }
.fact b { font-size:15px; font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums }
.fact em { display:block; font-style:normal; font-size:11.2px;
  color:var(--muted); margin-top:1px }

.recommend { display:flex; align-items:baseline; gap:9px; margin-top:13px;
  font-size:13.4px; font-weight:560; color:var(--ink) }
.recommend-label { font-size:10.5px; letter-spacing:.07em;
  text-transform:uppercase; color:var(--brand-ink); font-weight:600;
  background:var(--brand-wash); padding:2px 7px; border-radius:4px;
  white-space:nowrap }
.draft { margin-top:11px; padding:11px 13px; background:var(--raised);
  border:1px solid var(--line-2); border-radius:7px; font-size:12.4px;
  white-space:pre-wrap; color:var(--ink-2) }

.working { margin-top:12px; padding-top:11px;
  border-top:1px solid var(--line-2) }
.working > summary { cursor:pointer; font-size:12.2px; color:var(--brand-ink);
  font-weight:540 }
.working-body { margin-top:11px; padding:13px 15px; background:var(--raised);
  border:1px solid var(--line-2); border-radius:7px }
.working-line { display:flex; justify-content:space-between; gap:16px;
  padding:4px 0; font-size:12.6px; border-bottom:1px dashed var(--line-2) }
.working-line:last-of-type { border-bottom:0 }
.working-line span { color:var(--muted) }
.working-line b { font-weight:560; font-variant-numeric:tabular-nums }

/* --- while it runs ----------------------------------------------------- */
.spinner { width:15px; height:15px; border-radius:50%; flex:none;
  border:2px solid var(--line); border-top-color:var(--brand);
  animation:spin .8s linear infinite }
@keyframes spin { to { transform:rotate(360deg) } }
.found { margin-top:14px; padding-top:12px;
  border-top:1px solid var(--line-2); display:flex; flex-direction:column;
  gap:6px }
.found-line { font-size:12.8px; color:var(--ink-2) }
@media (prefers-reduced-motion:reduce) { .spinner { animation:none } }
"""

COMPONENTS += """
/* --- the supplier drawer ---------------------------------------------- */
.drawer-back { position:fixed; inset:0; background:rgba(15,23,36,.34);
  opacity:0; pointer-events:none; transition:opacity .15s; z-index:40 }
.drawer-back.open { opacity:1; pointer-events:auto }
.drawer { position:fixed; top:0; right:0; bottom:0; width:min(640px, 94vw);
  background:var(--surface); border-left:1px solid var(--line);
  box-shadow:-8px 0 28px rgba(16,24,40,.10); z-index:41;
  transform:translateX(100%); transition:transform .18s ease;
  display:flex; flex-direction:column }
.drawer.open { transform:none }
.drawer-head { padding:17px 20px; border-bottom:1px solid var(--line);
  display:flex; align-items:flex-start; gap:12px }
.drawer-head h2 { margin:0; font-size:16px }
.drawer-close { margin-left:auto; background:none; border:0; color:var(--muted);
  font-size:20px; line-height:1; cursor:pointer; padding:0 4px }
.drawer-close:hover { color:var(--ink); background:none }
.drawer-body { padding:18px 20px 32px; overflow-y:auto; flex:1 }
.drawer-body h3 { font-size:11.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); font-weight:600; margin:22px 0 10px }
.drawer-body h3:first-child { margin-top:0 }
tr.clickable { cursor:pointer }
tr.clickable:hover td { background:var(--raised) }

/* --- the 36-month grid ------------------------------------------------- */
.grid36 { display:grid; grid-template-columns:repeat(12, 1fr); gap:3px }
.grid36 i { display:block; height:17px; border-radius:3px; font-style:normal }
.g-on_time { background:var(--good) }
.g-late    { background:var(--warn) }
.g-missed  { background:var(--danger) }
.g-silent  { background:var(--line); border:1px dashed var(--faint) }
.grid-key { display:flex; gap:14px; flex-wrap:wrap; margin-top:9px;
  font-size:11.3px; color:var(--muted) }
.grid-key span { display:flex; align-items:center; gap:5px }
.grid-key i { width:11px; height:11px; border-radius:2px; display:inline-block }
.grid-years { display:flex; justify-content:space-between; margin-top:5px;
  font-size:10.6px; color:var(--faint) }

/* --- statutory clocks -------------------------------------------------- */
.clocks { display:grid; grid-template-columns:1fr 1fr; gap:11px }
.clock { border:1px solid var(--line); border-radius:9px; padding:13px 15px }
.clock.warn { border-color:var(--warn); background:var(--warn-wash) }
.clock.bad  { border-color:var(--danger); background:var(--danger-wash) }
.clock .rule { font-size:10.6px; letter-spacing:.06em; text-transform:uppercase;
  color:var(--muted); font-weight:600 }
.clock b { display:block; font-size:21px; font-weight:600; margin:5px 0 2px;
  letter-spacing:-.02em; font-variant-numeric:tabular-nums }
.clock .what { font-size:11.6px; color:var(--ink-2); line-height:1.45 }
@media (max-width:560px) { .clocks { grid-template-columns:1fr } }

/* --- which filing-history source is live ------------------------------- */
/* Stated on both the upload screen and the results, because a trust score
   reads as fact and one computed from generated dates has to carry that on
   the same screen rather than one click away. */
.src { display:flex; align-items:flex-start; gap:11px; padding:13px 16px;
  border:1px solid var(--line); border-radius:10px; margin-bottom:16px;
  background:var(--card) }
.src b { font-size:13px }
.src .src-what { font-size:11.8px; color:var(--ink-2); line-height:1.5;
  margin-top:3px }
.src > div { flex:1 }
.src-dot { width:9px; height:9px; border-radius:50%; flex:0 0 auto;
  margin-top:5px; background:var(--muted) }
.src.ok { border-color:var(--good); background:var(--good-wash) }
.src.ok .src-dot { background:var(--good) }
.src.demo { border-color:var(--warn); background:var(--warn-wash) }
.src.demo .src-dot { background:var(--warn) }
.src form, .src a.btn { flex:0 0 auto }

/* --- the three modes, side by side ------------------------------------- */
.modes { display:grid; grid-template-columns:repeat(3,1fr); gap:11px;
  margin-top:11px }
.modes > div { border:1px solid var(--line-2); border-radius:9px;
  padding:11px 13px }
.modes b { display:block; font-size:12.2px; margin-bottom:3px }
.modes span { font-size:11.4px; color:var(--ink-2); line-height:1.45 }
@media (max-width:720px) { .modes { grid-template-columns:1fr } }
"""

CSS = TOKENS + SHELL + COMPONENTS



def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def when(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b, %H:%M")


# The rail is ordered by what the product IS. Findings first, because
# settlements-in-findings-out is the whole job. The simulator sits at the
# bottom under its own heading, labelled demo, because it is scaffolding that
# stands in for a real data connection - putting it first made the app look
# like a point-of-sale system, which it is not.
def _rail(active: str, agents=(), enabled=frozenset(), source=None,
          viewer=None, role=None) -> str:
    """
    The sidebar, assembled from merchant/nav.py.

    It used to be built inline here: fifteen items in five ad-hoc groups, with
    agent pages sitting at the root beside account settings. The arrangement
    now lives in one small module so that disagreeing with it means editing a
    list rather than picking apart a frame.
    """
    from merchant.nav import route_for, visible

    def item(icon, label, href, key, tag="", cls=""):
        return (f'<a class="item {cls} {"on" if active == key else ""}" '
                f'href="{href}"><span class="ic">{icon}</span>{esc(label)}'
                f'{f"<span class=\'tag\'>{esc(tag)}</span>" if tag else ""}</a>')

    is_operator = bool(viewer is not None and getattr(viewer, "is_operator", False))
    source_tag = {"razorpay": "live", "simulator": "demo"}.get(source, "")

    blocks = []
    for label, items in visible(role, is_operator):
        if label:
            blocks.append(f'<div class="group">{esc(label)}</div>')
        for entry in items:
            tag = source_tag if entry.key == "data" else ""
            blocks.append(item(entry.icon, entry.label, entry.href, entry.key,
                               tag))
        # The live agents hang under Agents as a shallow second level, so the
        # way into a workspace is one click from anywhere without every agent
        # page becoming a root-level entry.
        if label == "Workspace":
            for spec in agents:
                if not spec.is_live:
                    continue
                route = route_for(spec.id)
                if route is None:
                    continue
                blocks.append(item(
                    "·", spec.rail_label, route.href, f"agent:{spec.id}",
                    "" if spec.id in enabled else "off", cls="sub-item"))

    return f"""
  <aside class="rail">
    <div class="logo"><span class="mark">L</span>Ledgerline</div>
    <nav>
      {''.join(blocks)}
    </nav>
    <div class="foot">
      <a class="item" href="/about"><span class="ic">?</span>What is this?</a>
    </div>
  </aside>"""


def auth_page(title: str, subtitle: str, body: str, footer: str = "") -> str:
    """The signed-out shell. No rail, no business - there is no context yet."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} &middot; Ledgerline</title><style>{CSS}</style></head>
<body><main style="max-width:400px;margin:0 auto;padding-top:76px">
  <div style="display:flex;align-items:center;gap:9px;margin-bottom:22px">
    <span class="mark">L</span>
    <span style="font-weight:680;letter-spacing:-.02em">Ledgerline</span>
  </div>
  <div class="card">
    <h1 style="font-size:17px">{esc(title)}</h1>
    <p class="sub">{esc(subtitle)}</p>
    {body}
  </div>
  {f'<p class="sub" style="text-align:center;margin-top:14px">{footer}</p>'
   if footer else ''}
</main></body></html>"""


def page(title: str, body: str, active: str = "", business=None,
         businesses=(), behaviour=None, agents=(), enabled=frozenset(),
         source=None, viewer=None, role=None) -> str:
    """
    The frame. Two things are on every page on purpose.

    The business selector, because in a multi-tenant app with no login the
    current tenant is the only context a person has, and getting it wrong means
    acting on someone else's books.

    The mode indicator, because the gateway behind this is simulated. Razorpay's
    own dashboard keeps a TEST marker in the top bar for exactly this reason;
    borrowing the idiom is better than inventing one, and better than hoping
    nobody forgets.
    """
    if business is None:
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body><main style="max-width:760px;margin:0 auto;padding-top:56px">{body}</main>
</body></html>"""

    options = "".join(
        f'<option value="{esc(b["business_id"])}"'
        f'{" selected" if b["business_id"] == business["business_id"] else ""}>'
        f'{esc(b["name"])}</option>' for b in businesses)

    # What the top bar says is where the DATA came from, not how the gateway is
    # behaving. That is the fact someone must never be wrong about: whether
    # what they are looking at is a real merchant's money or manufactured.
    if source == "razorpay":
        mode = ('<span class="mode ok"><span class="dot"></span>'
                'razorpay &middot; test mode</span>')
    elif source == "simulator":
        fault = behaviour is not None and behaviour != Behaviour.CORRECT
        mode = (f'<span class="mode sim"><span class="dot"></span>'
                f'demo data{" &middot; fault injected" if fault else ""}</span>')
    else:
        mode = ('<span class="mode sim"><span class="dot"></span>'
                'no data source</span>')

    who = ""
    if viewer is not None:
        badge = ('<span class="pill brand">operator</span>' if viewer.is_operator
                 else f'<span class="pill">{esc(ROLE_LABEL.get(role, ""))}</span>'
                 if role else "")
        who = (f'<span style="color:var(--muted);font-size:12.3px">'
               f'{esc(viewer.name)}</span>{badge}'
               f'<a href="/logout" style="font-size:12.3px">Sign out</a>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} &middot; Ledgerline</title><style>{CSS}</style></head>
<body><div class="app">
{_rail(active, agents, enabled, source, viewer, role)}
  <div>
    <div class="top">
      <div class="biz">
        <form method="get" action="/switch">
          <select name="business_id" onchange="this.form.submit()">{options}</select>
          <noscript><button class="ghost small">go</button></noscript>
        </form>
      </div>
      <span class="sp"></span>
      <a href="/settings" style="text-decoration:none">{mode}</a>
      {who}
    </div>
    <main>{body}</main>
  </div>
</div></body></html>"""


PROMPT = {"ok": "\u2713", "do": ">", "think": ">", "fact": ">", "say": ">",
          "tool": "\u21b3", "note": "!", "fail": "\u2717"}


def terminal_line(kind: str, text: str, at: str = "") -> str:
    return (f'<div class="at-ln {esc(kind)}">'
            f'<span class="at-t">{esc(at)}</span>'
            f'<span class="at-p">{PROMPT.get(kind, ">")}</span>'
            f'<span class="at-x">{esc(text)}</span></div>')


def terminal(lines, status: str = "complete", title: str = "settlement-auditor",
             live: bool = False) -> str:
    """
    The agent's run, as a transcript it narrates itself.

    Live and replayed use the same renderer and the same line builders, so what
    a person watches during a run is what they read afterwards. The FACTS come
    from the audit trail either way; only the timing differs.
    """
    body = "".join(terminal_line(l.kind, l.text, l.at) for l in lines)
    if live:
        body += ('<div class="at-ln"><span class="at-t"></span>'
                 '<span class="at-p">&gt;</span>'
                 '<span class="at-x"><span class="at-cursor"></span></span></div>')
    label = {"running": "RUNNING", "complete": "COMPLETE",
             "failed": "FAILED"}.get(status, status.upper())
    return f"""
    <div class="agentterm">
      <div class="at-bar">
        <span class="at-dot r"></span><span class="at-dot a"></span>
        <span class="at-dot g"></span>
        <span class="at-title">{esc(title)}</span>
        <span class="at-status {esc(status)}">{label}</span>
      </div>
      <div class="at-body" id="at-body">{body}</div>
    </div>"""


def checklist(steps) -> str:
    """
    What to do next, when there is nothing to show yet.

    An empty screen full of zeros is not neutral - it reads as broken, and it
    is the first impression every new business gets. Say what the screen will
    show once there is data, and give exactly one next action.

    steps: (state, title, detail, action_label, href) where state is
    "done" | "now" | "later".
    """
    out = []
    for state, title, detail, label, href in steps:
        mark = "&check;" if state == "done" else ("&rarr;" if state == "now" else "")
        button = (f'<div class="go"><a class="btn" href="{href}">{esc(label)}</a></div>'
                  if state == "now" and label else "")
        out.append(f"""
      <li class="step {state}">
        <span class="tick">{mark}</span>
        <div class="what"><b>{esc(title)}</b><span>{esc(detail)}</span></div>
        {button}
      </li>""")
    return f'<ul class="steps">{"".join(out)}</ul>'


def error_page(title: str, message: str, action: str = "", href: str = "") -> str:
    button = (f'<a class="btn" href="{href}" style="margin-top:14px">{esc(action)}</a>'
              if action else "")
    return page(title, f"""
<div class="card" style="text-align:center;padding:48px 24px">
  <h1>{esc(title)}</h1>
  <p class="sub" style="margin:8px auto 0;max-width:440px">{esc(message)}</p>
  {button}
</div>""")


def behaviour_banner(behaviour: Behaviour) -> str:
    if behaviour == Behaviour.CORRECT:
        return ""
    return (f'<div class="banner warn"><b>Gateway simulator</b>'
            f'<span>{esc(BEHAVIOUR_LABEL[behaviour])}. '
            f'{esc(BEHAVIOUR_NOTE[behaviour])} '
            f'<a href="/settings">Change</a></span></div>')


def agent_card(spec, enabled: bool, business_id: str = "") -> str:
    """One entry on the agent shelf. Planned agents cannot be turned on."""
    if spec.is_live:
        control = (f'<form method="post" action="/agents/{esc(spec.id)}/toggle">'
                   f'<button class="{"ghost" if enabled else ""}">'
                   f'{"Turn off" if enabled else "Turn on"}</button></form>')
        badge = f'<span class="pill good">{"active" if enabled else "available"}</span>'
        dim = ""
    else:
        control = '<span class="pill">not built yet</span>'
        badge = '<span class="pill">planned</span>'
        dim = "opacity:.66;"

    return f"""
    <div class="card" style="{dim}">
      <div class="row" style="align-items:flex-start;gap:18px">
        <div>
          <h2>{esc(spec.name)} &nbsp;{badge}</h2>
          <p class="sub" style="margin:3px 0 10px">{esc(spec.tagline)}</p>
          <p style="font-size:13.5px;margin:0;color:var(--ink-2)">
            &ldquo;{esc(spec.question)}&rdquo;</p>
          <p class="sub" style="margin:12px 0 0;font-size:12px">
            <b>Reads</b> {esc(", ".join(spec.reads))} &nbsp;&middot;&nbsp;
            <b>Argues from</b> {esc(spec.authority)}</p>
          {f'<p class="sub" style="margin:7px 0 0;font-size:12px">'
           f'<b>Why this gap exists</b> {esc(spec.why_unbuilt)}</p>'
           if spec.why_unbuilt else ''}
        </div>
        <div style="flex:0">{control}</div>
      </div>
    </div>"""


# --- the supplier-risk entry screen ---------------------------------------
#
# One screen, three states, decided by what the business is actually connected
# to. It used to show three upload boxes at once - register, GSTR-2B, filing
# history - which is the mechanism laid out on the page and left for the
# merchant to assemble into a workflow. A person arriving at that has to know
# what a GSTR-2B is before they can tell whether they need one.
#
# So the page asks what it needs for the state the business is in, and nothing
# else exists on screen:
#
#   DEMO         connected to the built-in simulator. There is nothing to
#                upload, so there are no upload boxes - one button.
#   LIVE_API     a GSP key is configured. The register is the only thing the
#                platform cannot fetch, so it is the only thing asked for.
#   LIVE_MANUAL  live, no GSP. Filing history has to come from the merchant,
#                once, and the page says why and offers the way out.

MODE_DEMO = "demo"
MODE_LIVE_API = "live_api"
MODE_LIVE_MANUAL = "live_manual"

REGISTER_HELP = (
    "A CSV or Excel export from Tally, Zoho, Busy or your own spreadsheet. It "
    "needs a supplier GSTIN column and the CGST, SGST and IGST amounts &mdash; "
    "the column names do not have to match anything, and anything unreadable "
    "is listed rather than silently dropped.")


def _register_field(label: str = "Purchase register") -> str:
    return f"""
    <div class="row">
      <div><label>{esc(label)}</label>
        <input type="file" name="register" accept=".csv,.xlsx,.xlsm,.txt"
          required></div>
      <div style="flex:0;align-self:flex-end">
        <button>Analyse suppliers</button></div>
    </div>"""


def _agent_toggle() -> str:
    return """
    <label style="display:flex;align-items:center;gap:8px;margin-top:12px;
      font-size:12.6px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent to judge each supplier &mdash; without this you still get
      every score and pattern, computed from their record.
    </label>"""


def _history_on_file(summary: dict) -> str:
    """The strip shown once a merchant has done the one-time upload."""
    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Supplier filing history on file</b>
    <div class="src-what">{summary["suppliers"]} suppliers,
      {summary["periods"]} tax periods,
      {esc(summary["first_period"])} to {esc(summary["last_period"])}.
      Risk is scored from this. You will not be asked again unless you
      replace it.</div></div>
  <form method="post" action="/agents/input-credit/history/forget"
    style="flex:0"><button class="ghost small">Replace</button></form>
</div>"""


def risk_demo_screen() -> str:
    """
    State 1. Connected to the simulator, so there is nothing to upload.

    Deliberately one button and no file inputs. Asking a person to download a
    sample file and upload it back is theatre - the platform already has both
    halves, and pretending otherwise just adds two clicks and a chance to pick
    the wrong file.
    """
    return """
<div class="src demo">
  <span class="src-dot"></span>
  <div><b>Demo mode &mdash; this business is on the built-in simulator</b>
    <div class="src-what">Both halves are generated: a purchase register of
      eight suppliers, and 36 months of GSTR-1 and GSTR-3B filing history for
      each of them, produced in the same JSON the GST portal returns and read
      back through the same parser a live connection uses. The arithmetic and
      the law are real; the companies and their filing dates are not.
      <b>Do not act on any of it against a real supplier.</b></div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">See what this agent does</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:56ch">One click builds
     the register and the filing history, scores every supplier, and opens the
     results. Nothing is uploaded and nothing leaves this machine.</p>
  <form method="post" action="/agents/input-credit/demo">
    <button style="font-size:14px;padding:12px 26px">
      Generate &amp; analyse demo data</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent to judge each supplier
    </label>
  </form>
</div>

<div class="card tint">
  <h2>Working with your own data instead</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Switch this business to
     a live data source in <a href="/data">Data &amp; integrations</a>. You will
     then be asked for your purchase register &mdash; and, if you have no GSP
     API configured, for your suppliers&rsquo; filing history once. Everything
     below that point is identical: same contract, same arithmetic, same
     screen.</p>
</div>"""


def risk_live_api_screen(api_message: str = "") -> str:
    """
    State 2. A GSP key is configured, so history is fetched, not asked for.

    One box, because there is exactly one thing the platform cannot get by
    itself. The merchant's own purchases are not on the GST portal.
    """
    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Connected GST API</b>
    <div class="src-what">Filing history is fetched per supplier when you run
      an analysis &mdash; 36 tax periods of GSTR-1 and GSTR-3B for every GSTIN
      in your register. You do not need to upload any of it.
      {esc(api_message)}</div></div>
  <a class="btn ghost small" href="/agents/input-credit/setup">Change</a>
</div>

<div class="card">
  <h2>Upload your purchase register</h2>
  <p class="sub" style="margin:3px 0 13px;max-width:66ch">{REGISTER_HELP}</p>
  <form method="post" action="/agents/input-credit" enctype="multipart/form-data">
    {_register_field("Purchase register (current period)")}
    {_agent_toggle()}
  </form>
</div>"""


def risk_live_manual_screen(summary: dict) -> str:
    """
    State 3. Live, no GSP key, so the history has to come from the merchant.

    Two things are asked for, and the page is explicit that the second is a
    one-time effort with a way out - because it IS a real cost, and hiding
    that behind a cheerful upload box is how a merchant discovers it halfway
    through assembling three years of filing dates.
    """
    if summary:
        # The one-time effort is done. Stop asking for it.
        return f"""
{_history_on_file(summary)}

<div class="card">
  <h2>Upload your purchase register</h2>
  <p class="sub" style="margin:3px 0 13px;max-width:66ch">{REGISTER_HELP}</p>
  <form method="post" action="/agents/input-credit" enctype="multipart/form-data">
    {_register_field("Purchase register (current period)")}
    {_agent_toggle()}
  </form>
</div>"""

    return f"""
<div class="banner warn">
  <span><b>No GST API is configured for this business.</b>
    <a href="/agents/input-credit/setup">Connect one in Setup</a> and filing
    history is fetched automatically. Without an API you have to provide your
    suppliers&rsquo; historical filing data yourself &mdash; once &mdash; before
    risk can be calculated.</span>
</div>

<div class="card">
  <div class="card-head"><h2>Step 1 &middot; Supplier filing history</h2>
    <span class="sub">one-time</span></div>
  <p class="sub" style="margin:3px 0 12px;max-width:70ch">What your suppliers
     <b>filed</b>, as against what you bought. This is the half that decides
     whether the credit exists at all &mdash; a supplier who files GSTR-1 and
     skips GSTR-3B reconciles perfectly against a credit that is not there.
     One row per supplier per month:
     <span class="mono">Supplier GSTIN, Period, GSTR-1 Filed Date,
     GSTR-3B Filed Date</span>. 24 to 36 months.</p>
  <form method="post" action="/agents/input-credit/history"
        enctype="multipart/form-data">
    <div class="row">
      <div><label>Filing history (CSV or Excel)</label>
        <input type="file" name="history" accept=".csv,.xlsx,.xlsm,.txt"
          required></div>
      <div style="flex:0;align-self:flex-end"><button>Upload</button></div>
    </div>
  </form>
  <p class="sub" style="margin:12px 0 0;font-size:11.5px">
    <b>A blank filing-date cell counts.</b> It says that period was checked and
    the return was not filed, which is what makes a default countable. A period
    with no row at all makes no claim and is not counted against anyone &mdash;
    so a partial file is safe. Suppliers missing from it are scored
    <i>unknown</i>, never clean.
  </p>
  <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line-2)">
    <a class="btn ghost small" href="/agents/input-credit/sample-history">
      Download the format</a>
    <span class="sub" style="margin-left:9px;font-size:11.5px">An example with
      the columns filled in, so you know what your accountant needs to
      produce.</span>
  </div>
</div>

<div class="card" style="opacity:.55">
  <div class="card-head"><h2>Step 2 &middot; Purchase register</h2>
    <span class="sub">after step 1</span></div>
  <p class="sub" style="margin:3px 0 0;max-width:66ch">Once the filing history
     is in, upload the period you want analysed and every supplier in it is
     scored against their record.</p>
</div>

<div class="card tint">
  <h2>Why GSTR-2B is not what is being asked for here</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">GSTR-2B is easy to
     download and it is the wrong file for this. It shows what suppliers
     <b>reported</b> (GSTR-1) and carries nothing about what they
     <b>paid</b> (GSTR-3B) &mdash; so building risk from it would report every
     supplier in your book as having never paid a rupee of tax. It has a real
     job on the <a href="/agents/input-credit/reconciliation">Reconciliation</a>
     tab, which is the question it can answer.</p>
</div>"""


def gstr2b_import_card(held=()) -> str:
    """
    Importing GSTR-2B, on the tab whose question it answers.

    This used to sit on the risk screen next to two other upload boxes, which
    put the most confusing file in the product on the page a person sees first
    and invited them to think it was what risk is built from. It is not - it
    has no GSTR-3B evidence in it at all. What it IS good for is the
    reconciliation on this tab: your books against what your suppliers
    reported about you.
    """
    periods_held = ""
    if held:
        rows = "".join(
            f'<div class="working-line"><span>{esc(h["filed_period"])}'
            f' &middot; {h["suppliers"]} suppliers</span>'
            f'<b>{rupees(h["tax"])}</b></div>' for h in held)
        enough = ("Enough history for the supplier watch."
                  if len(held) >= 3 else
                  f"{3 - len(held)} more period"
                  f"{'' if 3 - len(held) == 1 else 's'} would let the watch "
                  f"tell a supplier who stopped filing from one who has not "
                  f"filed yet.")
        periods_held = (
            f'<details class="working" style="margin-top:13px">'
            f'<summary>{len(held)} period'
            f'{"" if len(held) == 1 else "s"} imported</summary>'
            f'<div class="working-body">{rows}'
            f'<p style="margin:11px 0 0;font-size:11.5px;color:var(--muted)">'
            f'{esc(enough)}</p></div></details>')

    return f"""
<div class="card">
  <h2>Import GSTR-2B</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:68ch">The government&rsquo;s
     own record of what your suppliers reported about you. Download it from
     <b>gst.gov.in &rarr; Services &rarr; Returns &rarr; Return Dashboard</b>,
     pick a month, and take the <b>JSON</b> &mdash; not the Excel. Any
     registered business can pull their own; no special access needed.</p>
  <form method="post" action="/agents/input-credit/gstr2b"
        enctype="multipart/form-data">
    <div class="row">
      <div><label>GSTR-2B files</label>
        <input type="file" name="gstr2b" accept=".json" multiple required></div>
      <div style="flex:0;align-self:flex-end"><button>Import</button></div>
    </div>
  </form>
  <p class="sub" style="margin:12px 0 0;font-size:11.5px">
    Select several months at once. The supplier watch needs at least three
    periods to tell a supplier who has <i>stopped</i> filing from one who
    simply has not filed yet, so one month at a time is the slow road to a
    feature that cannot work. Importing a period you already have replaces it
    rather than duplicating it.
  </p>
  {periods_held}
</div>"""

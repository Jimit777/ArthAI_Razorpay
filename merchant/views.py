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
from typing import Optional

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
    Tab 1. Both halves generated, one button, no upload boxes.

    Asking a person to download a sample file and upload it back is theatre -
    the platform holds both halves already, and the round trip only adds two
    clicks and a chance to pick the wrong file.
    """
    return """
<div class="src demo">
  <span class="src-dot"></span>
  <div><b>Demo mode &mdash; nothing here is your data</b>
    <div class="src-what">A purchase register of eight suppliers, and 36 months
      of GSTR-1 and GSTR-3B history for each of them, generated in the same
      JSON the GST portal returns and read back through the same parser a live
      connection uses. The arithmetic and the law are real; the companies and
      their filing dates are not.
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
  <h2>Using your own data</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch"><b>Without API</b> takes
     your purchase register plus the GSTR-2B files you download from the portal
     yourself. <b>With API</b> takes the register alone and fetches every
     supplier&rsquo;s history for you. Both produce the same dashboard as this
     one, from the same arithmetic &mdash; the tabs differ only in where the
     history comes from.</p>
</div>"""


# The portal's own path to the file, spelled out. A merchant who has never
# downloaded one will not find it from "get your GSTR-2B": the JSON is four
# clicks deep and the tile offers an Excel first, which is the wrong file.
GSTR2B_PATH = ("Download the JSON files from: gst.gov.in &rarr; Services "
               "&rarr; Returns &rarr; Returns Dashboard &rarr; Select "
               "Financial Year &amp; Month &rarr; Search &rarr; GSTR-2B Tile "
               "&rarr; Download &rarr; Generate JSON File to Download.")


def risk_without_api_screen(summary: dict, register_ready: bool = False) -> str:
    """
    Tab 2. Register plus the GSTR-2B files a merchant can fetch themselves.

    Two uploads, and the second is a real cost - twenty-four to thirty-six
    files, one per month. The page says so rather than hiding it behind a
    cheerful box, and says what the files can and cannot show, because a
    merchant who believes GSTR-2B proves payment will read "payment not
    visible" as a defect rather than as the truth.
    """
    if summary:
        return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Supplier history on file</b>
    <div class="src-what">{summary["suppliers"]} suppliers,
      {summary["periods"]} tax periods,
      {esc(summary["first_period"])} to {esc(summary["last_period"])}.
      You will not be asked again unless you replace it.</div></div>
  <form method="post" action="/agents/input-credit/history/forget"
    style="flex:0"><button class="ghost small">Replace</button></form>
</div>

<div class="card">
  <h2>Upload your purchase register</h2>
  <p class="sub" style="margin:3px 0 13px;max-width:66ch">{REGISTER_HELP}</p>
  <form method="post" action="/agents/input-credit"
        enctype="multipart/form-data">
    {_register_field("Purchase register (current period)")}
    {_agent_toggle()}
  </form>
</div>"""

    return f"""
<div class="banner warn">
  <span><b>No GST API is configured, so supplier history has to come from
    you.</b> It is a one-time effort &mdash; or connect a GSP on the
    <a href="/agents/input-credit/with-api">With API</a> tab and it is fetched
    automatically.</span>
</div>

<div class="card">
  <div class="card-head"><h2>Step 1 &middot; Supplier filing history</h2>
    <span class="sub">one-time</span></div>
  <p class="sub" style="margin:3px 0 12px;max-width:70ch">Twenty-four to
     thirty-six monthly GSTR-2B files, selected together. Any registered
     business can pull their own &mdash; no GSP and no special access.</p>
  <p class="sub" style="margin:0 0 12px;max-width:70ch"><b>{GSTR2B_PATH}</b></p>
  <form method="post" action="/agents/input-credit/history"
        enctype="multipart/form-data">
    <div class="row">
      <div><label>GSTR-2B files (JSON), or a filing-history CSV</label>
        <input type="file" name="history" accept=".json,.csv,.xlsx,.xlsm"
          multiple required></div>
      <div style="flex:0;align-self:flex-end"><button>Upload</button></div>
    </div>
  </form>
  <p class="sub" style="margin:12px 0 0;font-size:11.5px">
    <b>What these files can and cannot show.</b> They prove what your suppliers
    <i>reported</i>: which months they filed GSTR-1 for, on what date, and
    whether they went quiet. They prove <i>payment</i> only where the portal
    itself flagged a Rule 37A reversal &mdash; so for most months payment is
    reported as <b>not visible</b> rather than guessed at. A supplier is never
    accused of not paying on the strength of a file that cannot see payment.
  </p>
</div>

<div class="card" style="opacity:.55">
  <div class="card-head"><h2>Step 2 &middot; Purchase register</h2>
    <span class="sub">after step 1</span></div>
  <p class="sub" style="margin:3px 0 0;max-width:66ch">Once the history is in,
     upload the period you want analysed and every supplier in it is scored
     against their record.</p>
</div>"""


def risk_with_api_screen(config: Optional[dict], vault_ready: bool = True
                         ) -> str:
    """
    Tab 3. Register only; history fetched per supplier over a GSP.

    The configuration lives on this tab rather than on a Setup page, because
    the connection is the only thing that makes this tab different from the
    one beside it.
    """
    if config and config.get("key_available"):
        return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Connected GST API</b>
    <div class="src-what">When you run an analysis, every GSTIN in your
      register is looked up and 36 tax periods of GSTR-1 and GSTR-3B are
      fetched for it. Nothing to upload but the register.
      {esc(config.get("message", ""))}</div></div>
  <form method="post" action="/agents/input-credit/filing-api/forget"
    style="flex:0"><button class="ghost small">Disconnect</button></form>
</div>

<div class="card">
  <h2>Upload your purchase register</h2>
  <p class="sub" style="margin:3px 0 13px;max-width:66ch">{REGISTER_HELP}</p>
  <form method="post" action="/agents/input-credit"
        enctype="multipart/form-data">
    {_register_field("Purchase register (current period)")}
    {_agent_toggle()}
  </form>
</div>"""

    stale = ""
    if config:
        stale = ('<div class="banner warn"><span>A connection is stored but '
                 'its key cannot be decrypted, so runs will not use it. '
                 'Re-enter it below.</span></div>')
    vault_note = "" if vault_ready else (
        '<p class="sub" style="margin:9px 0 0;font-size:11.5px;'
        'color:var(--warn)">No encryption key is configured, so the API key '
        'will not be stored. Set <span class="mono">LEDGERLINE_SECRET_KEY'
        '</span> to keep it between runs.</p>')

    return f"""
{stale}
<div class="card">
  <h2>Connect a GST filing-status API</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:70ch">With a GSP or
     verification API key, each supplier&rsquo;s GSTR-1 and GSTR-3B filing
     dates are fetched from their GSTIN &mdash; no monthly downloads, and
     payment history is genuinely visible rather than inferred. Everything
     after that is identical to the other two tabs.</p>
  <form method="post" action="/agents/input-credit/filing-api">
    <div><label>Endpoint URL</label>
      <input name="url_template" required
        placeholder="https://your-provider.example/returns/{{gstin}}"></div>
    <p class="sub" style="margin:5px 0 11px;font-size:11.4px">Must be https and
      must contain <span class="mono">{{gstin}}</span> &mdash; that is where
      each supplier&rsquo;s number is substituted in.</p>
    <div class="row">
      <div><label>API key</label>
        <input name="api_key" type="password" autocomplete="off"></div>
      <div><label>Header name</label>
        <input name="key_header" placeholder="x-api-key"></div>
      <div><label>or query parameter</label>
        <input name="key_param" placeholder="api_key"></div>
    </div>
    <div class="row" style="margin-top:11px">
      <div><label>Test with a GSTIN (optional)</label>
        <input name="probe_gstin" placeholder="27AAAAA0000A1Z5"></div>
      <div style="flex:0;align-self:flex-end"><button>Connect</button></div>
    </div>
  </form>
  {vault_note}
</div>

<div class="card tint">
  <h2>No GSP contract?</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">A GSP is a commercial
     agreement with a GSTN-authorised provider, not a signup, and most
     merchants will never have one. The
     <a href="/agents/input-credit/without-api">Without API</a> tab takes the
     GSTR-2B files you can download yourself and produces the same dashboard
     &mdash; with payment reported as not visible where those files cannot
     see it.</p>
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


# --- three-way reconciliation ---------------------------------------------
#
# Two screens, and the split is the whole design: what needs a decision, and
# what does not. A reconciliation that shows fifty green rows and six red ones
# in the same table buries the six, and the six are the entire product.
#
# So the landing screen is the exception list, and the fifty matched lines are
# a tab away - present, exportable, and not competing for attention.

RECON_ACTION_TONE = {
    "chase": "danger", "dispute": "danger",
    "investigate": "warn", "write_off": "", "none": "good",
}


def recon_start_screen() -> str:
    """Before a run: what it is about to do, and one button."""
    return """
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is filed, claimed or paid.</b> This reads three
  sources, joins them, and reports what it could not close.</span>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">Reconcile three sources</h2>
  <p class="sub" style="margin:7px auto 6px;max-width:60ch">What you billed,
     what your gateway says it settled after its fee, and what your bank
     actually credited. The join is arithmetic; the agent explains only the
     lines the arithmetic could not close.</p>
  <div class="three-src">
    <div><b>ERP invoices</b><span>what was billed</span></div>
    <div class="arrow">&rarr;</div>
    <div><b>Gateway settlements</b><span>what was processed, net of fee</span></div>
    <div class="arrow">&rarr;</div>
    <div><b>Bank credits</b><span>what actually arrived</span></div>
  </div>
  <form method="post" action="/agents/three-way/run" style="margin-top:20px">
    <button style="font-size:14px;padding:12px 26px">
      Run reconciliation (simulate 55 records)</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent to explain each exception
    </label>
  </form>
</div>

<div class="card tint">
  <h2>Where the numbers come from</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Fifty-five linked
     records across the three sources, generated with known faults planted in
     them &mdash; money that never arrived, credits short by an amount nobody
     accounted for, gateway lines with no invoice reference, and one credit
     that belongs to nobody. The generator returns the answer key, so the
     match rate on the next screen is <b>measured against it</b> rather than
     asserted.</p>
</div>"""


def recon_results(payload: dict, key: str = "") -> str:
    """The summary, then the lines that need a person."""
    meta = payload["metadata"]
    metrics = payload["match_metrics"]
    findings = payload["vocabulary"]["findings"]
    notes = payload["vocabulary"]["notes"]
    actions = payload["vocabulary"]["actions"]

    accuracy = metrics.get("accuracy") or {}
    measured = ""
    if accuracy.get("records_with_a_known_answer"):
        wrong = accuracy["wrong"]
        measured = (
            f'<div style="padding:11px 16px;border-top:1px solid var(--line-2);'
            f'color:var(--muted);font-size:11.5px">'
            f'<b>Checked against the answer key.</b> '
            f'{accuracy["correct"]} of '
            f'{accuracy["records_with_a_known_answer"]} lines were classified '
            f'as the generator built them '
            f'({accuracy["accuracy_percentage"]}%)'
            + (f', and {wrong} were not.' if wrong else
               ' &mdash; none were misclassified.')
            + ' The match rate above is a measurement, not a claim.</div>')

    passes = metrics["passes"]
    spend = ""
    usage = meta.get("usage") or {}
    if usage.get("calls"):
        spend = (
            f'<div style="padding:11px 16px;border-top:1px solid var(--line-2);'
            f'color:var(--muted);font-size:11.5px">'
            f'{usage["calls"]} agent call'
            f'{"" if usage["calls"] == 1 else "s"} &mdash; one per exception, '
            f'not one per record. <b>${usage["usd"]:.3f}</b> '
            f'(about Rs {usage["rupees"]:.0f}). The {metrics["successful_matches_count"]} '
            f'matched lines cost nothing, because arithmetic settled them.'
            f'</div>')

    failed = ""
    if meta.get("failed_calls"):
        failed = (f'<div class="banner warn"><span><b>{meta["failed_calls"]} '
                  f'exception{"" if meta["failed_calls"] == 1 else "s"} could '
                  f'not be explained by the agent.</b> Those rows show the '
                  f'finding and the recommended action, computed without '
                  f'it.</span></div>')

    rows = "".join(_recon_exception(e, findings, notes, actions)
                   for e in payload["exception_list"])
    if not rows:
        rows = ('<tr><td colspan="5" style="color:var(--muted);padding:22px">'
                'Every line closed. Nothing needs a decision.</td></tr>')

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is filed, claimed or paid.</b> Every action below is a
  proposal waiting for you.</span>
</div>
{failed}

<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  <div class="stats">
    <div class="stat"><b>{metrics["match_rate_percentage"]}%</b>
      <span>auto-reconciled</span></div>
    <div class="stat"><b>{meta["total_records_processed"]}</b>
      <span>records audited</span></div>
    <div class="stat"><b style="color:var(--danger)">
      {metrics["exception_count"]}</b>
      <span>need your decision</span></div>
    <div class="stat"><b style="color:var(--danger)">
      {metrics["at_stake_display"]}</b>
      <span>at stake</span></div>
  </div>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    {metrics["successful_matches_count"]} of
    {metrics["successful_matches_count"] + metrics["exception_count"]} lines
    closed in {meta["processing_time_ms"]} ms &mdash;
    <b>{passes["pass_1_exact"]}</b> on an exact reference,
    <b>{passes["pass_2_windowed"]}</b> on amount and date where no reference
    existed, and <b>{passes["pass_3_narration"]}</b> by reading the bank&rsquo;s
    narration. <a href="/agents/three-way/matched?key={esc(key)}">See the
    matched lines</a>.
  </div>
  {measured}
  {spend}
</div>

<div class="card flush">
  <div class="card-head"><h2>Needs your decision</h2>
    <span class="sub">worst first</span></div>
  <div style="overflow-x:auto">
  <table>
    <tr><th>Issue</th><th>Finding</th><th class="r">At stake</th>
        <th>Suggested action</th><th class="r"></th></tr>
    {rows}
  </table>
  </div>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    The action comes from the figures, not from the agent, so it does not
    change between runs. The agent reads each line and explains what it is
    likely to be. Nothing here is actioned for you &mdash; every button is a
    proposal.
  </div>
</div>"""


def _recon_exception(row: dict, findings: dict, notes: dict,
                     actions: dict) -> str:
    """One line that needs a person, and the three buttons they might press."""
    from merchant.ui import badge

    tone = RECON_ACTION_TONE.get(row["action"], "")
    suggested = row["action"]

    # What it searched before recommending this. Kept as a tight inline chip
    # row rather than the card treatment the cash forecaster uses - this is a
    # table row, and it stays scannable at fifty exceptions or it fails at the
    # thing an exception LIST is for.
    calls = row.get("tool_calls") or []
    checked = ""
    if calls:
        counted = {}
        for name in calls:
            counted[name] = counted.get(name, 0) + 1
        chips = " ".join(
            f'<span class="checked">{esc(RECON_TOOL_WORDS.get(n, n))}'
            + (f' &times;{c}' if c > 1 else '') + '</span>'
            for n, c in counted.items())
        checked = f'<div class="looked-up" style="margin-top:6px">{chips}</div>'

    return f"""
      <tr>
        <td style="max-width:44ch">
          <div style="font-weight:560">{esc(row["detail"])}</div>
          <div style="color:var(--muted);font-size:11.3px;margin-top:3px">
            {esc(row["reasoning"] or notes.get(row["finding_type"], ""))}</div>
          {checked}
          <div class="mono" style="color:var(--faint);font-size:10.5px;
            margin-top:3px">
            {esc(row["invoice_id"] or "no invoice")} &middot;
            {esc(row["txn_id"] or "no settlement")} &middot;
            {esc(row["utr_number"] or "no credit")}</div>
        </td>
        <td>{badge(findings.get(row["finding_type"], row["finding_type"]),
                   "bad" if tone == "danger" else "warn",
                   title=row["finding_type"])}</td>
        <td class="r" style="font-weight:600;color:var(--danger)">
          {rupees(row["at_stake"])}</td>
        <td>{esc(actions.get(suggested, suggested))}</td>
        <td class="r" style="white-space:nowrap">
          {_recon_buttons(row, suggested)}</td>
      </tr>"""


def _recon_buttons(row: dict, suggested: str) -> str:
    """
    Write off, dispute, investigate - with the suggested one emphasised.

    All three are always offered. The agent proposes; a person disposes, and a
    screen that hides the other two options is making the decision while
    appearing to ask for it.
    """
    out = []
    for action, label in (("write_off", "Write off"), ("dispute", "Dispute"),
                          ("investigate", "Investigate")):
        primary = action == suggested
        out.append(
            f'<form method="post" action="/agents/three-way/decide" '
            f'style="display:inline">'
            f'<input type="hidden" name="key" value="{{key}}">'
            f'<input type="hidden" name="line" value="{esc(row["invoice_id"] or row["txn_id"] or row["utr_number"])}">'
            f'<input type="hidden" name="decision" value="{action}">'
            f'<button class="{"" if primary else "ghost"} small"'
            f' style="margin-left:4px">{label}</button></form>')
    return "".join(out)


def recon_matched(payload: dict, key: str = "") -> str:
    """Every line the three sources closed between them."""
    metrics = payload["match_metrics"]
    rows = "".join(f"""
      <tr>
        <td class="mono">{esc(m["invoice_id"] or "&mdash;")}</td>
        <td>{esc(m["customer_name"])}</td>
        <td class="mono">{esc(m["txn_id"] or "&mdash;")}</td>
        <td class="mono">{esc(m["utr_number"] or "&mdash;")}</td>
        <td class="r">{esc(m["amount_display"])}</td>
        <td><span class="pill">{esc(m["matched_by"] or "exact")}</span></td>
      </tr>""" for m in payload["matched_records"])

    return f"""
<div class="card flush">
  <div class="card-head"><h2>Matched lines</h2>
    <span class="sub">{metrics["successful_matches_count"]} closed</span></div>
  <div style="overflow-x:auto">
  <table>
    <tr><th>Invoice</th><th>Customer</th><th>Gateway txn</th><th>Bank UTR</th>
        <th class="r">Net</th><th>Joined by</th></tr>
    {rows}
  </table>
  </div>
  <div style="padding:11px 16px;border-top:1px solid var(--line-2);
    color:var(--muted);font-size:11.5px">
    Three ids on every row, which is the artefact somebody would actually hand
    an auditor. <a href="/agents/three-way?key={esc(key)}">Back to the
    exceptions</a>.
  </div>
</div>"""


COMPONENTS += """
/* --- the three sources, side by side ----------------------------------- */
.three-src { display:flex; align-items:center; justify-content:center;
  gap:10px; flex-wrap:wrap; margin-top:16px }
.three-src > div:not(.arrow) { border:1px solid var(--line-2);
  border-radius:9px; padding:11px 15px; min-width:150px }
.three-src b { display:block; font-size:12.4px }
.three-src span { font-size:11.3px; color:var(--muted) }
.three-src .arrow { color:var(--faint); font-size:15px; border:0; padding:0 }
@media (max-width:720px) { .three-src .arrow { display:none } }
"""

CSS = TOKENS + SHELL + COMPONENTS


# --- the three-way agent's real-data tabs ---------------------------------

RECON_SOURCE_HELP = {
    "invoice": ("ERP sales invoices", "invoices",
                "What you billed. A CSV or Excel export from Tally, Zoho, "
                "Busy or your own spreadsheet - it needs an invoice number "
                "and an amount, and a date if you have one. Column names do "
                "not have to match anything."),
    "settlement": ("Gateway settlements", "settlements",
                   "What your gateway says it processed. Download the "
                   "settlement report from your gateway dashboard. It needs a "
                   "transaction id and either the net settled or the gross "
                   "and the fee; an invoice reference and a UTR make the "
                   "match exact rather than a search."),
    "bank": ("Bank statement", "bank",
             "What actually arrived. Export the statement from net banking "
             "as CSV or Excel - every bank offers it. Debits are ignored: "
             "this is about money coming in."),
}


def _recon_source_card(kind: str, held: dict) -> str:
    """One of the three uploads, or a note that it is already on file."""
    title, field, blurb = RECON_SOURCE_HELP[kind]
    on_file = held.get(kind)

    if on_file:
        return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>{esc(title)} &mdash; {on_file["records"]} records</b>
    <div class="src-what">From
      <span class="mono">{esc(on_file["source_file"] or "your upload")}</span>.
      Upload again to replace it.</div></div>
  <form method="post" action="/agents/three-way/upload" style="flex:0"
        enctype="multipart/form-data">
    <input type="hidden" name="kind" value="{kind}">
    <label class="btn ghost small" style="cursor:pointer">Replace
      <input type="file" name="{field}" accept=".csv,.xlsx,.xlsm,.txt"
        style="display:none" onchange="this.form.submit()"></label>
  </form>
</div>"""

    return f"""
<div class="card">
  <h2>{esc(title)}</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:68ch">{esc(blurb)}</p>
  <form method="post" action="/agents/three-way/upload"
        enctype="multipart/form-data">
    <input type="hidden" name="kind" value="{kind}">
    <div class="row">
      <div><label>{esc(title)} (CSV or Excel)</label>
        <input type="file" name="{field}" accept=".csv,.xlsx,.xlsm,.txt"
          required></div>
      <div style="flex:0;align-self:flex-end"><button>Upload</button></div>
    </div>
  </form>
</div>"""


def _recon_run_card(held: dict, ready: bool, connected: bool = False) -> str:
    """The run button, and what is still missing if it cannot run yet."""
    if not ready:
        missing = [RECON_SOURCE_HELP[k][0] for k in
                   ("invoice", "settlement", "bank") if not held.get(k)]
        return f"""
<div class="card tint">
  <h2>Not ready yet</h2>
  <p class="sub" style="margin:4px 0 0;max-width:68ch">Still needed:
     <b>{esc(", ".join(missing))}</b>. All three are required &mdash; a
     two-way join between books and bank tells you money is missing and
     nothing about where it went, which is the whole reason the gateway is in
     the middle of this.</p>
</div>"""

    return f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to reconcile</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {held.get("invoice", {}).get("records", 0)} invoices,
     {held.get("settlement", {}).get("records", 0)} settlements and
     {held.get("bank", {}).get("records", 0)} bank credits are on file.</p>
  <form method="post" action="/agents/three-way/run">
    <input type="hidden" name="source" value="{'connected' if connected else 'upload'}">
    <button style="font-size:14px;padding:12px 26px">Reconcile my data</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent to explain each exception
    </label>
  </form>
  <form method="post" action="/agents/three-way/forget" style="margin-top:12px">
    <button class="ghost small">Clear all three</button>
  </form>
</div>"""


def recon_upload_screen(held: dict) -> str:
    """
    Your own three exports. Works for any merchant with any bank.

    Deliberately not framed as the lesser option. For the bank leg it is
    currently the only honest one - there is no free API that hands an Indian
    merchant their own statement, and the regulated alternative needs a
    commercial relationship most merchants will never have.
    """
    ready = all(held.get(k) for k in ("invoice", "settlement", "bank"))
    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is filed, claimed or paid.</b> Your files are read,
  joined and reported on. Nothing is sent anywhere.</span>
</div>

{_recon_run_card(held, ready)}
{_recon_source_card("invoice", held)}
{_recon_source_card("settlement", held)}
{_recon_source_card("bank", held)}

<div class="card tint">
  <h2>Why the bank statement is a file and not a connection</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">There is no free API
     that hands an Indian merchant their own bank statement. The Account
     Aggregator framework is the regulated answer and needs an AA or TSP
     relationship &mdash; the same wall as a GSP for GST filing history. A
     net-banking CSV export works for every bank, today, with no commercial
     dependency, so that is what this asks for. If you have a corporate
     account with an API, the importer takes the same shape.</p>
</div>"""


def recon_connected_screen(held: dict, source_kind: Optional[str],
                           last_pull: str = "") -> str:
    """Settlements pulled from Razorpay; the other two still uploaded."""
    if source_kind != "razorpay":
        return f"""
<div class="banner warn">
  <span><b>No Razorpay account is connected to this business.</b> Connect one
    in <a href="/data">Data &amp; integrations</a> and settlement reports are
    pulled for you &mdash; you would still upload your invoices and your bank
    statement, because neither of those is on the gateway.</span>
</div>

<div class="card tint">
  <h2>What connecting changes, and what it does not</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">One of the three
     sources stops being a download. Razorpay&rsquo;s settlement recon report
     is the only endpoint in this product that states, line by line, what a
     gateway deducted and what it sent &mdash; so that leg becomes automatic
     and current. Your invoices live in your accounting system and your
     statement lives at your bank, so those two stay uploads on the
     <a href="/agents/three-way/upload">Upload</a> tab.</p>
</div>"""

    settled = held.get("settlement")
    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Razorpay connected</b>
    <div class="src-what">Settlement reports are pulled for this business.
      {esc(last_pull)}</div></div>
</div>

<div class="card">
  <h2>Pull settlements</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:68ch">One month of the
     settlement recon report, straight from Razorpay. Payment lines only
     &mdash; refunds, transfers and adjustments belong in a settlement audit
     rather than in a three-way match against sales invoices.</p>
  <form method="post" action="/agents/three-way/pull">
    <div class="row">
      <div><label>Year</label><input name="year" value="2026" required></div>
      <div><label>Month</label><input name="month" value="07" required></div>
      <div style="flex:0;align-self:flex-end"><button>Pull</button></div>
    </div>
  </form>
  {'<p class="sub" style="margin:11px 0 0;font-size:11.5px">'
   + str(settled["records"]) + ' settlement lines on file.</p>'
   if settled else ''}
</div>

{_recon_source_card("invoice", held)}
{_recon_source_card("bank", held)}
{_recon_run_card(held, all(held.get(k) for k in
                           ("invoice", "settlement", "bank")), connected=True)}"""


# --- the forward cash forecaster ------------------------------------------
#
# One chart and one decision. A thirty-day forecast is a shape before it is a
# table - a controller wants to see WHERE the dip is before they read what is
# in it - so the curve leads, and everything under it explains that shape.

# What each tool did, in words a merchant reads rather than a function name.
RISK_TOOL_WORDS = {
    "full_filing_history": "read their full filing history",
    "statutory_clocks": "checked the Rule 37 / s.16(4) clocks",
}


def risk_tools_checked(calls: list) -> str:
    """
    What the risk agent looked up before deciding, on the supplier drawer.

    A recommendation that read all thirty-six months and one that saw only
    the twelve it was handed produce the same sentence. This is how a
    merchant tells them apart.
    """
    if not calls:
        return ""
    counted = {}
    for name in calls:
        counted[name] = counted.get(name, 0) + 1
    chips = "".join(
        f'<span class="checked">{esc(RISK_TOOL_WORDS.get(name, name))}'
        + (f' &times;{n}' if n > 1 else '') + '</span>'
        for name, n in counted.items())
    return (f'<div class="looked-up" style="margin-top:11px">'
            f'<span class="looked-up-label">Before deciding, it checked'
            f'</span>{chips}</div>')


RECON_TOOL_WORDS = {
    "nearby_settlements": "searched for a settlement",
    "nearby_bank_credits": "searched for a credit",
}

TOOL_WORDS = {
    "what_if_delayed": "simulated moving a payment",
    "payout_detail": "looked up a payment",
    "movements_on": "opened a day in the forecast",
}

CASH_TONE = {
    "CASH_HEALTHY": "good", "CASH_TIGHT": "warn",
    "CASH_CRUNCH_WARNING": "bad", "CASH_OVERDRAWN": "bad",
}


# Round figures to put an axis on, in PAISE. Written out rather than computed
# because the obvious computation - divide by a power of ten - is what
# produced an axis reading 2.7L, and because a constant named 1_00_000 in a
# paise codebase reads as a lakh and is a thousand rupees.
GRID_STEPS_PAISE = (
    1_000_00,        # Rs 1,000
    5_000_00,        # Rs 5,000
    10_000_00,       # Rs 10,000
    25_000_00,       # Rs 25,000
    50_000_00,       # Rs 50,000
    1_00_000_00,     # Rs 1 lakh
    2_00_000_00,     # Rs 2 lakh
    5_00_000_00,     # Rs 5 lakh
    10_00_000_00,    # Rs 10 lakh
    25_00_000_00,    # Rs 25 lakh
    1_00_00_000_00,  # Rs 1 crore
)


def _short_rupees(paise: int) -> str:
    """
    A rupee figure short enough for an axis label.

    Lakhs, because that is how the reader thinks about their own balance -
    "Rs 7.1L" is read at a glance and "Rs 7,05,000.00" has to be counted.
    Only ever used on the chart furniture; every figure a decision rests on
    is written out in full.
    """
    rupees = paise / 100
    if abs(rupees) >= 1_00_000:
        lakhs = rupees / 1_00_000
        # No decimal on a whole number of lakhs. "Rs 10.0L" reads as a
        # measurement; "Rs 10L" reads as the round figure it is.
        return (f"Rs {lakhs:.0f}L" if abs(lakhs - round(lakhs)) < 0.05
                else f"Rs {lakhs:.1f}L")
    if abs(rupees) >= 1_000:
        return f"Rs {rupees / 1_000:.0f}k"
    return f"Rs {rupees:,.0f}"


def _spline(points: list[tuple[float, float]]) -> str:
    """
    A smooth path through every point, that never draws a value nobody had.

    Catmull-Rom converted to cubic bezier, with the control points CLAMPED to
    the range of the two data points they sit between.

    The clamp is the whole reason this is written out rather than borrowed. An
    unclamped spline overshoots at a sharp turn, so the cliff on the day
    payroll lands would dip visibly below the balance that was actually
    reached - a chart drawing a trough that did not happen, in a product whose
    entire claim is that the arithmetic is exact. The midpoint-quadratic
    smoothing most charts use has the opposite problem: it is safe from
    overshoot but does not pass through the data at all, so the dot marking
    the low point would sit off the line.

    This passes through every point and stays inside it.
    """
    if len(points) < 2:
        return ""
    out = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2

        c1x, c1y = p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6
        c2x, c2y = p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6

        low, high = min(p1[1], p2[1]), max(p1[1], p2[1])
        c1y = max(low, min(high, c1y))
        c2y = max(low, min(high, c2y))
        out.append(f"C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} "
                   f"{p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(out)


def cash_curve(forecast: dict, height: int = 250) -> str:
    """
    Thirty days, with a tooltip that follows the cursor.

    ## What the shape says

    The fill sits between the line and the SAFE FLOOR rather than between the
    line and the bottom of the chart, and it is clipped at the floor so it
    changes colour where the balance crosses it. A day above the floor is
    tinted calm; the stretch below is red. That is one glance rather than
    reading an axis, and it is honest because the floor is a real threshold
    rather than a decorative midpoint.

    ## The interaction

    A dark card follows the pointer, naming the day under it: balance, what
    came in, what went out and what fell due. Dots mark every day, coloured by
    which side of the floor they are on, so the data points are visible
    without hovering at all.

    ## Two things done in HTML rather than SVG, for the same reason

    The plot stretches to whatever width the card is, using
    preserveAspectRatio="none". Anything inside it stretches too - text turns
    into a condensed typeface and a circle turns into an ellipse. So the dots,
    the labels and the tooltip are absolutely positioned HTML at percentage
    coordinates, and the only things in the SVG are the paths - which carry
    vector-effect="non-scaling-stroke" so the line keeps an even weight when
    the horizontal scale is stretched and the vertical is not.
    """
    positions = forecast.get("positions") or []
    if not positions:
        return ""

    floor = forecast.get("floor", 0)
    balances = [p["closing"] for p in positions]

    high = max(max(balances), floor)
    low = min(min(balances), floor)
    top = high + (high - low) * 0.12
    bottom = min(low, 0) - max(floor * 0.5, (high - low) * 0.08, 1)
    span = (top - bottom) or 1

    width, pad = 720, 4
    step = (width - pad * 2) / max(1, len(positions) - 1)

    def pct(value: int) -> float:
        return (top - value) / span * 100

    def xy(i: int, value: int) -> tuple[float, float]:
        return pad + i * step, pct(value) / 100 * height

    line_points = [xy(i, b) for i, b in enumerate(balances)]
    path = _spline(line_points)
    floor_y = pct(floor) / 100 * height

    # The band between the line and the floor, closed along the floor so the
    # clip can split it. Drawn twice, once per side.
    band = (path + f" L{line_points[-1][0]:.1f},{floor_y:.1f}"
            f" L{line_points[0][0]:.1f},{floor_y:.1f} Z")

    trough = forecast.get("trough") or {}
    low_index = max(0, min(len(balances) - 1, trough.get("day", 1) - 1))
    uid = f"c{abs(hash(str(positions[0]['date']) + str(len(positions)))) % 100000}"

    dots, columns = [], []
    for i, position in enumerate(positions):
        x, _ = xy(i, position["closing"])
        left = x / width * 100
        under = position["closing"] < floor
        moved = [line.get("payee") or line.get("name", "")
                 for line in position["payout_lines"]
                 + position["recurring_lines"]]
        dots.append(
            f'<i class="curve-pt{" under" if under else ""}'
            f'{" low" if i == low_index else ""}" '
            f'style="left:{left:.3f}%;top:{pct(position["closing"]):.3f}%">'
            f'</i>')
        columns.append(
            f'<i class="curve-hit" style="left:{i / len(positions) * 100:.3f}%;'
            f'width:{100 / len(positions):.3f}%"'
            f' data-day="{position["day"]}"'
            f' data-date="{esc(_day_words(position["date"]))}"'
            f' data-bal="{esc(position["closing_display"])}"'
            f' data-under="{"1" if under else ""}"'
            f' data-in="{esc(rupees(position["receipts"]))}"'
            f' data-out="{esc(rupees(position["payouts"] + position["recurring"]))}"'
            f' data-what="{esc(", ".join(n for n in moved if n))}"'
            f' data-x="{left:.3f}"'
            f' data-y="{pct(position["closing"]):.3f}"></i>')

    lines = []
    for step_paise in GRID_STEPS_PAISE:
        if (top - bottom) / step_paise <= 6.5:
            break
    value = int(bottom // step_paise) * step_paise
    while value <= top:
        if bottom <= value <= top:
            lines.append(
                f'<div class="curve-grid" style="top:{pct(value):.1f}%">'
                f'<span>{esc(_short_rupees(int(value)))}</span></div>')
        value += step_paise

    # Where the receipts stop being money already earned.
    #
    # Every one of these thirty days is a projection, so shading "the forecast
    # part" would mean inventing a boundary. This one is real: up to it the
    # incoming money is settlements from payments already taken and invoices
    # already raised; past it the receipts assume trade carries on. A forecast
    # needs that assumption and is useless without it - what it must not do is
    # draw both halves identically and let somebody believe the fourth week
    # rests on the same evidence as the first.
    assumed = ""
    earned_day = forecast.get("earned_through_day") or 0
    if 0 < earned_day < len(positions):
        left = (pad + (earned_day - 1) * step) / width * 100
        assumed = (
            f'<div class="curve-assumed" style="left:{left:.3f}%"></div>'
            f'<div class="curve-earned-mark" style="left:{left:.3f}%">'
            f'<span>earned to here</span></div>')

    # A day label every five days, so the axis is readable without crowding.
    ticks = []
    for i, position in enumerate(positions):
        if position["day"] == 1 or position["day"] % 5 == 0:
            ticks.append(
                f'<span style="left:{xy(i, 0)[0] / width * 100:.3f}%">'
                f'{position["day"]}</span>')

    return f"""
<div class="curve" data-low="{low_index}">
  <div class="curve-frame">
    <div class="curve-ylabel">Balance</div>
    <div class="curve-plot" style="height:{height}px">
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none"
           role="img" aria-label="Thirty day cash projection">
        <defs>
          <clipPath id="{uid}-over">
            <rect x="0" y="0" width="{width}" height="{floor_y:.1f}"></rect>
          </clipPath>
          <clipPath id="{uid}-under">
            <rect x="0" y="{floor_y:.1f}" width="{width}"
              height="{max(0, height - floor_y):.1f}"></rect>
          </clipPath>
        </defs>
        <path d="{band}" fill="var(--brand)" opacity="0.10"
          clip-path="url(#{uid}-over)"></path>
        <path d="{band}" fill="var(--danger)" opacity="0.16"
          clip-path="url(#{uid}-under)"></path>
        <path d="{path}" fill="none" stroke="var(--brand)" stroke-width="2.2"
          stroke-linejoin="round" stroke-linecap="round"
          vector-effect="non-scaling-stroke"
          clip-path="url(#{uid}-over)"></path>
        <path d="{path}" fill="none" stroke="var(--danger)" stroke-width="2.2"
          stroke-linejoin="round" stroke-linecap="round"
          vector-effect="non-scaling-stroke"
          clip-path="url(#{uid}-under)"></path>
      </svg>
      {"".join(lines)}
      {assumed}
      <div class="curve-floor-line" style="top:{pct(floor):.1f}%">
        <span>safe floor {esc(forecast.get("floor_display", ""))}</span></div>
      <div class="curve-cross"></div>
      {"".join(dots)}
      {"".join(columns)}
      <div class="curve-tip">
        <div class="curve-tip-when"><b data-read="date"></b>
          <span data-read="day"></span></div>
        <div class="curve-tip-row"><span>Balance</span>
          <b data-read="bal"></b></div>
        <div class="curve-tip-row in"><span>Money in</span>
          <b data-read="in"></b></div>
        <div class="curve-tip-row out"><span>Money out</span>
          <b data-read="out"></b></div>
        <div class="curve-tip-what" data-read="what"></div>
      </div>
    </div>
  </div>
  <div class="curve-axis">{"".join(ticks)}</div>
  <div class="curve-xlabel">Day of the next 30</div>
</div>
{CURVE_SCRIPT}"""


# Vanilla, like the supplier drawer. Every figure is already in the DOM; this
# picks which one to show and moves three absolutely positioned elements. It
# computes nothing - the engine's rule, applied to the browser.
CURVE_SCRIPT = """
<script>
(function () {
  document.querySelectorAll('.curve').forEach(function (curve) {
    var plot = curve.querySelector('.curve-plot');
    var hits = curve.querySelectorAll('.curve-hit');
    var cross = curve.querySelector('.curve-cross');
    var tip = curve.querySelector('.curve-tip');
    if (!hits.length) { return; }

    function show(hit) {
      curve.querySelectorAll('[data-read]').forEach(function (slot) {
        var key = slot.getAttribute('data-read');
        var value = hit.getAttribute('data-' + key) || '';
        if (key === 'day') { value = value ? 'day ' + value : ''; }
        slot.textContent = value;
      });
      var what = tip.querySelector('.curve-tip-what');
      what.style.display = what.textContent ? '' : 'none';
      tip.classList.toggle('under', hit.getAttribute('data-under') === '1');

      var x = parseFloat(hit.getAttribute('data-x'));
      cross.style.left = x + '%';
      // Flip the card to the other side near the right edge, so it never
      // hangs off the chart.
      var flip = x > 62;
      tip.classList.toggle('flip', flip);
      tip.style.left = x + '%';
      tip.style.top = Math.min(78, Math.max(6,
        parseFloat(hit.getAttribute('data-y')))) + '%';
    }

    hits.forEach(function (hit) {
      hit.addEventListener('mouseenter', function () {
        curve.classList.add('live');
        show(hit);
      });
      hit.addEventListener('focus', function () {
        curve.classList.add('live');
        show(hit);
      });
    });
    plot.addEventListener('mouseleave', function () {
      curve.classList.remove('live');
    });
  });
})();
</script>"""


def cash_results(payload: dict, key: str = "") -> str:
    """The curve, the alert, then the days that made the shape."""
    from merchant.ui import badge

    meta = payload["metadata"]
    forecast = payload["forecast"]
    trough = forecast.get("trough") or {}
    tone = CASH_TONE.get(forecast["finding_type"], "")

    accuracy = meta.get("accuracy") or {}
    measured = ""
    if accuracy.get("total"):
        checks = "".join(
            f'<div class="working-line"><span>{esc(c["what"])}</span>'
            f'<b style="color:{"var(--good)" if c["ok"] else "var(--danger)"}">'
            f'{"yes" if c["ok"] else "no"}</b></div>'
            for c in accuracy["checks"])
        measured = (
            f'<details class="working" style="margin:0 16px 14px">'
            f'<summary>Checked against the planted scenario &mdash; '
            f'{accuracy["passed"]} of {accuracy["total"]}</summary>'
            f'<div class="working-body">{checks}'
            f'<p style="margin:11px 0 0;font-size:11.5px;color:var(--muted)">'
            f'The demo scenario was built to break on a particular day in a '
            f'particular way. These are the engine&rsquo;s answers against '
            f'that, so the crunch on the chart is a finding rather than a '
            f'coincidence.</p></div></details>')

    spend = ""
    usage = meta.get("usage") or {}
    if usage.get("calls"):
        spend = (f'<div style="padding:11px 16px;border-top:1px solid '
                 f'var(--line-2);color:var(--muted);font-size:11.5px">'
                 f'{usage["calls"]} agent call &mdash; '
                 f'<b>${usage["usd"]:.3f}</b> (about Rs '
                 f'{usage["rupees"]:.0f}). One, not thirty: there is a single '
                 f'decision in a cash forecast and it is what to do about the '
                 f'low point.</div>')

    # The verdict, before anything else. The first version led with "in the
    # account today", which is the least actionable number on the page - a
    # controller opening this wants to know whether they are fine, when they
    # are not, and what to do, in that order.
    breached = bool(trough.get("shortfall"))
    if breached:
        verdict = f"""
  <div class="verdict bad">
    <div>
      <div class="verdict-what">You run short on
        {esc(_day_words(trough.get("date", "")))}</div>
      <div class="verdict-why">
        {esc(trough.get("shortfall_display", ""))} short of the
        {esc(forecast.get("floor_display", ""))} floor, on day
        {trough.get("day", "")} of {meta["days"]}.
        {"Moving one payment covers it." if forecast.get("coverable_by_delay")
         else "Rescheduling will not cover it &mdash; this needs funding."}
      </div>
    </div>
    <div class="verdict-do">
      <span>What to do</span><b>{esc(forecast["action_label"])}</b></div>
  </div>"""
    else:
        verdict = f"""
  <div class="verdict ok">
    <div>
      <div class="verdict-what">The next {meta["days"]} days hold</div>
      <div class="verdict-why">The balance never falls below
        {esc(trough.get("balance_display", ""))}, and the floor is
        {esc(forecast.get("floor_display", ""))}.</div>
    </div>
    <div class="verdict-do"><span>What to do</span><b>Nothing</b></div>
  </div>"""

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is paid, moved or scheduled.</b> This projects a
  balance and recommends. Every action is a proposal waiting for you.</span>
</div>

<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  {verdict}
  {cash_curve(forecast)}
  <div class="curve-legend">
    <span><b>{esc(forecast["opening_display"])}</b> today</span>
    <span><b>{esc(forecast.get("receipts_after_trough_display", ""))}</b>
      arriving after the low point</span>
    <span><b>{esc(forecast["closing_display"])}</b> after {meta["days"]} days</span>
    {f'<span class="hatched">past day '
     f'{forecast["earned_through_day"]}, '
     f'<b>{esc(forecast.get("assumed_receipts_display", ""))}</b> of the '
     f'incoming money assumes trade carries on</span>'
     if forecast.get("earned_through_day") else ''}
  </div>
  {measured}
  {spend}
</div>

{_cash_alert(payload, tone, badge)}
{_cash_days(forecast)}"""


def _day_words(iso: str) -> str:
    """A date a person reads rather than parses. "10 September", not 2026-09-10."""
    from datetime import date as _date

    try:
        when = _date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso)
    return f"{when.day} {when.strftime('%B')}"


def _cash_alert(payload: dict, tone: str, badge) -> str:
    """The finding and what to do about it, as a finding-card."""
    forecast = payload["forecast"]
    verdict = payload.get("verdict") or {}
    trough = forecast.get("trough") or {}

    if forecast["finding_type"] == "CASH_HEALTHY":
        return f"""
<div class="finding-card">
  <div class="finding-card-top">
    <div><div class="finding-card-who">The month holds</div>
      <div class="finding-card-inv">Lowest point
        {esc(trough.get("balance_display", ""))} on
        {esc(trough.get("date", ""))}</div></div>
    {badge(forecast["finding_label"], "good")}
  </div>
  <p class="finding-card-why">{esc(forecast["detail"])}</p>
</div>"""

    held = ""
    if verdict.get("hold_payout_id"):
        days = verdict.get("hold_days")
        held = (f'<div class="draft"><b>Move '
                f'{esc(verdict["hold_payout_id"])}</b>'
                + (f' by {days} day{"" if days == 1 else "s"}.' if days
                   else '.')
                + ' Nothing is moved for you &mdash; this is the proposal.'
                + '</div>')

    # What it looked up before deciding.
    #
    # Without this the page cannot tell a merchant apart an agent that checked
    # three candidates from one that picked the first name on a list - the
    # prose reads identically either way, and only one of them deserves to be
    # believed.
    looked_up = ""
    calls = verdict.get("tool_calls") or []
    if calls:
        counted = {}
        for name in calls:
            counted[name] = counted.get(name, 0) + 1
        checks = "".join(
            f'<span class="checked">{esc(TOOL_WORDS.get(name, name))}'
            + (f' &times;{n}' if n > 1 else '') + '</span>'
            for name, n in counted.items())
        looked_up = (
            f'<div class="looked-up"><span class="looked-up-label">'
            f'Before deciding, it checked</span>{checks}</div>')

    corrections = ""
    if verdict.get("corrections"):
        corrections = (
            '<p class="sub" style="margin:9px 0 0;color:var(--warn);'
            'font-size:11.8px">The agent was corrected: '
            + esc("; ".join(verdict["corrections"])) + '</p>')

    unmovable = "".join(
        f'<div class="working-line"><span>{esc(row.get("payee", ""))} '
        f'&middot; {esc(row.get("kind", ""))}</span>'
        f'<b>{esc(row.get("amount_display", ""))}</b></div>'
        for row in forecast.get("unmovable_near_trough", []))
    movable = "".join(
        f'<div class="working-line"><span>'
        f'{esc(row.get("payee") or row.get("name", ""))} &middot; day '
        f'{row.get("day", "")} &middot; can move '
        f'{row.get("delay_days", 5)} days</span>'
        f'<b>{esc(row.get("amount_display", ""))}</b></div>'
        for row in forecast.get("movable_near_trough", []))

    return f"""
<div class="finding-card" style="border-left:3px solid var(--danger)">
  <div class="finding-card-top">
    <div>
      <div class="finding-card-who">Cash drops to
        {esc(trough.get("balance_display", ""))} on
        {esc(trough.get("date", ""))}</div>
      <div class="finding-card-inv">Day {trough.get("day", "")} of
        {len(forecast.get("positions", []))} &middot;
        {esc(trough.get("shortfall_display", ""))} below the
        {esc(forecast.get("floor_display", ""))} floor</div>
    </div>
    {badge(forecast["finding_label"], tone or "bad")}
  </div>
  <p class="finding-card-why">
    {esc(verdict["reasoning"]) if verdict.get("reasoning") else
     "The figures are below. What the agent adds is which of the movable "
     "payments to actually move &mdash; it weighs a supplier relationship "
     "against an interest charge, which no comparison of totals produces. "
     "Run this again with the agent on to get that."}</p>

  <div class="facts">
    <div class="fact"><span>Short by</span>
      <b style="color:var(--danger)">
        {esc(trough.get("shortfall_display", ""))}</b></div>
    <div class="fact"><span>Could be moved</span>
      <b>{esc(forecast.get("movable_total_display", ""))}</b>
      <em>{len(forecast.get("movable_near_trough", []))} payments</em></div>
    <div class="fact"><span>Cannot be moved</span>
      <b>{len(forecast.get("unmovable_near_trough", []))} payments</b>
      <em>payroll and statutory dues</em></div>
    <div class="fact"><span>Arriving after</span>
      <b>{esc(forecast.get("receipts_after_trough_display", ""))}</b></div>
  </div>

  <div class="recommend">
    <span class="recommend-label">Recommended</span>
    {esc(forecast["action_label"])}
  </div>
  {held}
  {looked_up}
  {corrections}

  <details class="working" style="margin-top:13px">
    <summary>What falls due around that date</summary>
    <div class="working-body">
      <p style="margin:0 0 7px;font-size:11.5px;color:var(--muted)">
        Cannot be moved</p>
      {unmovable or '<div class="working-line"><span>nothing</span></div>'}
      <p style="margin:11px 0 7px;font-size:11.5px;color:var(--muted)">
        Could be moved</p>
      {movable or '<div class="working-line"><span>nothing</span></div>'}
    </div>
  </details>
</div>"""


def _cash_days(forecast: dict) -> str:
    """The days that actually moved, so the shape can be traced to its causes."""
    rows = []
    for position in forecast.get("positions", []):
        if not (position["receipts"] or position["payouts"]
                or position["recurring"]):
            continue
        names = [line.get("payee") or line.get("name", "")
                 for line in position["payout_lines"]
                 + position["recurring_lines"]]
        low = (forecast.get("trough") or {}).get("day") == position["day"]
        rows.append(f"""
      <tr{' style="background:var(--danger-wash)"' if low else ''}>
        <td>{position["day"]}<div style="color:var(--muted);font-size:10.5px">
          {esc(position["date"])}</div></td>
        <td class="r" style="color:var(--good)">
          {esc(rupees(position["receipts"])) if position["receipts"] else "&mdash;"}</td>
        <td class="r" style="color:var(--danger)">
          {esc(rupees(position["payouts"] + position["recurring"]))
           if (position["payouts"] + position["recurring"]) else "&mdash;"}</td>
        <td style="color:var(--muted);font-size:11.5px;max-width:36ch">
          {esc(", ".join(n for n in names if n)) or "&mdash;"}</td>
        <td class="r" style="font-weight:{'600' if low else '400'}">
          {esc(position["closing_display"])}</td>
      </tr>""")

    return f"""
<div class="card flush">
  <div class="card-head"><h2>The days that move</h2>
    <span class="sub">quiet days are left out</span></div>
  <div style="overflow-x:auto">
  <table>
    <tr><th>Day</th><th class="r">In</th><th class="r">Out</th>
        <th>What</th><th class="r">Balance</th></tr>
    {"".join(rows)}
  </table>
  </div>
</div>"""


COMPONENTS += """
/* --- the thirty-day cash curve ----------------------------------------- */
/* The plot stretches to the card's width with preserveAspectRatio="none", so
   everything except the paths is absolutely positioned HTML - text inside a
   non-uniformly scaled SVG turns condensed and a circle turns into an
   ellipse. The paths carry vector-effect:non-scaling-stroke for the same
   reason. */
.curve { padding:6px 0 10px }
.curve-frame { display:flex; align-items:stretch; gap:8px; margin:0 18px }
.curve-ylabel { writing-mode:vertical-rl; transform:rotate(180deg);
  align-self:center; font-size:10.4px; letter-spacing:.05em;
  text-transform:uppercase; color:var(--faint) }
/* A gutter for the axis labels. They used to sit at left:0 INSIDE the plot,
   so the first day of the line ran straight through "Rs 8L". */
.curve-plot { position:relative; flex:1; margin-left:46px }
.curve-plot svg { width:100%; height:100%; display:block; position:absolute;
  inset:0 }

.curve-grid { position:absolute; left:0; right:0; height:0;
  border-top:1px solid var(--line-2); pointer-events:none }
.curve-grid span { position:absolute; right:100%; top:-8px; width:44px;
  padding-right:7px; text-align:right; font-size:10.2px; color:var(--faint);
  font-variant-numeric:tabular-nums }

.curve-floor-line { position:absolute; left:0; right:0; height:0;
  border-top:1px dashed var(--danger); opacity:.9; pointer-events:none }
.curve-floor-line span { position:absolute; right:0; top:-8px;
  padding:0 0 0 6px; background:var(--surface); font-size:10.2px;
  color:var(--danger); font-variant-numeric:tabular-nums }

/* A dot on every day, coloured by which side of the floor it is on - the
   data points are visible without hovering at all. */
.curve-pt { position:absolute; width:7px; height:7px; border-radius:50%;
  background:var(--brand); transform:translate(-3.5px,-3.5px);
  pointer-events:none; box-shadow:0 0 0 2px var(--surface) }
.curve-pt.under { background:var(--danger) }
.curve-pt.low { width:11px; height:11px; transform:translate(-5.5px,-5.5px);
  background:var(--surface); border:3px solid var(--danger) }

.curve-cross { position:absolute; top:0; bottom:0; width:1px;
  background:var(--ink); opacity:0; pointer-events:none;
  transform:translateX(-.5px); transition:opacity .12s }
.curve.live .curve-cross { opacity:.22 }

.curve-hit { position:absolute; top:0; bottom:0; display:block;
  cursor:crosshair }

/* Past here the receipts assume trade carries on, rather than being money
   already taken or already invoiced. Deliberately faint: it is a caveat on
   the far half of the chart, not a warning about it. */
.curve-assumed { position:absolute; top:0; bottom:0; right:0;
  background:repeating-linear-gradient(-45deg,
    var(--ink) 0 1px, transparent 1px 7px);
  opacity:.05; pointer-events:none }
.curve-earned-mark { position:absolute; top:0; bottom:0; width:0;
  border-left:1px dashed var(--line); pointer-events:none }
.curve-earned-mark span { position:absolute; top:2px; left:6px;
  font-size:9.8px; letter-spacing:.04em; text-transform:uppercase;
  color:var(--faint); white-space:nowrap }

/* The card that follows the pointer. */
.curve-tip { position:absolute; z-index:3; min-width:168px;
  margin:-14px 0 0 16px; padding:11px 13px; border-radius:10px;
  background:#151b2b; color:#fff; box-shadow:0 10px 28px rgba(15,23,42,.28);
  pointer-events:none; opacity:0; transition:opacity .12s;
  transform:translateY(-50%) }
.curve.live .curve-tip { opacity:1 }
.curve-tip.flip { margin-left:0; transform:translate(-100%,-50%);
  margin-right:16px }
.curve-tip.flip { left:auto }
.curve-tip-when { padding-bottom:8px; margin-bottom:8px;
  border-bottom:1px solid rgba(255,255,255,.14) }
.curve-tip-when b { font-size:13px; font-weight:600 }
.curve-tip-when span { font-size:11px; opacity:.6; margin-left:6px }
.curve-tip-row { display:flex; justify-content:space-between; gap:18px;
  font-size:11.6px; padding:2px 0 }
.curve-tip-row span { opacity:.62 }
.curve-tip-row b { font-weight:600; font-variant-numeric:tabular-nums }
.curve-tip-row.in b { color:#4ade80 }
.curve-tip-row.out b { color:#fb7185 }
.curve-tip.under .curve-tip-row:first-of-type b { color:#fb7185 }
.curve-tip-what { margin-top:8px; padding-top:8px;
  border-top:1px solid rgba(255,255,255,.14); font-size:11px; opacity:.72;
  max-width:230px; line-height:1.4 }

.curve-axis { position:relative; height:15px; margin:8px 18px 0 76px;
  font-size:10.4px; color:var(--faint) }
.curve-axis span { position:absolute; transform:translateX(-50%);
  font-variant-numeric:tabular-nums }
.curve-xlabel { text-align:center; margin-top:4px; font-size:10.4px;
  letter-spacing:.05em; text-transform:uppercase; color:var(--faint) }

/* --- what the agent checked before deciding ---------------------------- */
/* The difference between an agent that investigated and one that guessed is
   invisible in the prose. It should not be invisible on the page. */
.looked-up { display:flex; align-items:center; gap:7px; flex-wrap:wrap;
  margin-top:11px; padding-top:11px; border-top:1px solid var(--line-2) }
.looked-up-label { font-size:10.4px; letter-spacing:.06em;
  text-transform:uppercase; color:var(--muted); font-weight:600 }
.looked-up .checked { font-size:11.4px; color:var(--ink-2);
  background:var(--raised); border:1px solid var(--line-2);
  border-radius:20px; padding:3px 10px }

/* --- the verdict, before anything else --------------------------------- */
/* A controller opening this wants three things in order: am I fine, when am
   I not, and what do I do. The first version led with today's balance, which
   is the least actionable number on the page. */
.verdict { display:flex; align-items:center; gap:18px; padding:18px 20px;
  border-bottom:1px solid var(--line-2) }
.verdict > div:first-child { flex:1 }
.verdict-what { font-size:19px; font-weight:600; letter-spacing:-.02em }
.verdict-why { margin-top:4px; font-size:12.8px; color:var(--ink-2);
  max-width:60ch; line-height:1.5 }
.verdict.bad { background:var(--danger-wash) }
.verdict.bad .verdict-what { color:var(--danger) }
.verdict.ok { background:var(--good-wash) }
.verdict.ok .verdict-what { color:var(--good) }
.verdict-do { text-align:right; white-space:nowrap; flex:0 0 auto }
.verdict-do span { display:block; font-size:10.2px; letter-spacing:.06em;
  text-transform:uppercase; color:var(--muted); margin-bottom:2px }
.verdict-do b { font-size:14.5px; font-weight:600 }
@media (max-width:640px) {
  .verdict { flex-direction:column; align-items:flex-start; gap:11px }
  .verdict-do { text-align:left }
}

/* The supporting figures, under the chart where they belong - context, not
   headline. */
.curve-legend { display:flex; flex-wrap:wrap; gap:20px; padding:0 18px 14px;
  font-size:11.8px; color:var(--muted) }
.curve-legend b { color:var(--ink); font-weight:600;
  font-variant-numeric:tabular-nums }
.curve-legend .hatched { position:relative; padding-left:16px }
.curve-legend .hatched::before { content:""; position:absolute; left:0; top:2px;
  width:11px; height:11px; border:1px solid var(--line);
  background:repeating-linear-gradient(-45deg,
    var(--ink) 0 1px, transparent 1px 4px); opacity:.55 }

/* --- what a cash forecast needs before it can run ----------------------- */
.needs { display:grid; grid-template-columns:repeat(3,1fr); gap:11px;
  margin-top:12px }
.needs > div { border:1px solid var(--line-2); border-radius:9px;
  padding:11px 13px }
.needs b { display:block; font-size:12.3px; margin-bottom:3px }
.needs span { font-size:11.3px; color:var(--ink-2); line-height:1.45 }
.needs .have { border-color:var(--good); background:var(--good-wash) }
@media (max-width:720px) { .needs { grid-template-columns:1fr } }
"""

CSS = TOKENS + SHELL + COMPONENTS


CASH_UPLOAD_HELP = {
    "account": ("Bank balances", "balances",
                "What is in your accounts today. A one-line export, or the "
                "balance row from your statement. Needs a balance; an "
                "overdraft limit helps if you have one."),
    "payout": ("Scheduled payouts", "payouts",
               "What you owe and when - an AP ageing report or a payment run "
               "from your accounting package. Needs an amount and a due "
               "date. Payroll and tax rows are recognised by name and marked "
               "as unmovable, so nothing here will ever suggest delaying a "
               "salary."),
    "recurring": ("Recurring charges", "recurring",
                  "Optional. Rent, cloud bills, subscriptions. Leave it out "
                  "and they are inferred from your payout history instead, "
                  "with the confidence stated - because a forecast that "
                  "silently omits rent is cheerful and wrong."),
}


def _cash_need_card(kind: str, held: dict) -> str:
    title, field, blurb = CASH_UPLOAD_HELP[kind]
    on_file = held.get(kind)
    if on_file:
        return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>{esc(title)} &mdash; {on_file["records"]} records</b>
    <div class="src-what">From
      <span class="mono">{esc(on_file["source_file"] or "your upload")}</span>.
      Upload again to replace it.</div></div>
</div>"""
    optional = " <span class=\"sub\">(optional)</span>" if kind == "recurring" else ""
    return f"""
<div class="card">
  <h2>{esc(title)}{optional}</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:68ch">{esc(blurb)}</p>
  <form method="post" action="/agents/cash-forecaster/upload"
        enctype="multipart/form-data">
    <input type="hidden" name="kind" value="{kind}">
    <div class="row">
      <div><label>{esc(title)} (CSV or Excel)</label>
        <input type="file" name="{field}" accept=".csv,.xlsx,.xlsm,.txt"
          required></div>
      <div style="flex:0;align-self:flex-end"><button>Upload</button></div>
    </div>
  </form>
</div>"""


def cash_demo_screen() -> str:
    """Tab 1. A generated month with a crunch planted in it."""
    return """
<div class="src demo">
  <span class="src-dot"></span>
  <div><b>Demo mode &mdash; this is a generated month</b>
    <div class="src-what">A balance, three weeks of gateway settlements, a
      payroll run, a tax instalment, rent, a cloud bill and eight vendor
      invoices &mdash; with a cash crunch deliberately placed on day 14. The
      arithmetic is real; the company is not.</div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">Thirty days forward</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:58ch">One click builds
     the scenario, projects every day, finds the low point and asks the agent
     which payment to move. Nothing is scheduled and nothing is paid.</p>
  <form method="post" action="/agents/cash-forecaster/run">
    <input type="hidden" name="source" value="demo">
    <button style="font-size:14px;padding:12px 26px">
      Forecast the next 30 days</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent which payout to move
    </label>
  </form>
</div>

<div class="card tint">
  <h2>Why the crunch is on day 14 and not day 2</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">A cash crunch found on
     the day it happens is not a finding, it is a crisis. Found two weeks out
     it is a phone call to a supplier. That gap is the entire product, so the
     demo is built to show it &mdash; payroll and the quarterly tax instalment
     land together on the 14th, and the settlements that would have covered
     them arrive on the 16th.</p>
</div>"""


def cash_upload_screen(held: dict) -> str:
    """Tab 2. Your own balances and payables."""
    ready = bool(held.get("account") and held.get("payout"))
    run_card = f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to forecast</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {held.get("account", {}).get("records", 0)} accounts and
     {held.get("payout", {}).get("records", 0)} scheduled payouts on file.
     {"Recurring charges will be inferred from your payout history."
      if not held.get("recurring") else ""}</p>
  <form method="post" action="/agents/cash-forecaster/run">
    <input type="hidden" name="source" value="upload">
    <button style="font-size:14px;padding:12px 26px">
      Forecast the next 30 days</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent which payout to move
    </label>
  </form>
  <form method="post" action="/agents/cash-forecaster/forget"
        style="margin-top:12px">
    <button class="ghost small">Clear what is on file</button>
  </form>
</div>""" if ready else """
<div class="card tint">
  <h2>Not ready yet</h2>
  <p class="sub" style="margin:4px 0 0;max-width:68ch">A forecast needs a
     starting balance and something to spend. Without either it is not a
     cautious projection &mdash; it is a straight line, and drawing one would
     be worse than saying what is missing.</p>
</div>"""

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is paid, moved or scheduled.</b> Your files are read
  and projected forward. Nothing is sent anywhere.</span>
</div>

{run_card}
{_cash_need_card("account", held)}
{_cash_need_card("payout", held)}
{_cash_need_card("recurring", held)}

<div class="card tint">
  <h2>What is not asked for</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Your incoming gateway
     settlements. This platform already pulls those &mdash; connect an account
     on the <a href="/agents/cash-forecaster/connected">With API</a> tab and
     the receivable side of the forecast fills itself. Asking you to export a
     file the product can fetch would be inventing work.</p>
</div>"""


def cash_connected_screen(held: dict, source_kind: Optional[str],
                          receipts: int = 0) -> str:
    """Tab 3. Settlements pulled; balances still the merchant's to supply."""
    if source_kind != "razorpay":
        return """
<div class="banner warn">
  <span><b>No Razorpay account is connected to this business.</b> Connect one
    in <a href="/data">Data &amp; integrations</a> and the incoming half of
    the forecast fills itself from your pending settlements.</span>
</div>

<div class="card tint">
  <h2>What connecting changes, and what it does not</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Money coming IN stops
     being something you supply. Captured payments that have not yet been
     credited are exactly the receivable a thirty-day forecast turns on, and
     the gateway is the only party that knows them.</p>
  <p class="sub" style="margin:9px 0 0;max-width:70ch">Your balance and your
     payables stay uploads. The balance lives at your bank, which has no API
     a small merchant can use &mdash; the same wall as an Account Aggregator
     for statements, or a GSP for GST filing history. Open Banking would
     close it; nothing available today does.</p>
</div>"""

    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Razorpay connected</b>
    <div class="src-what">{receipts} pending settlements will be read as
      money arriving. Your balance and your payables are still yours to
      supply &mdash; neither is on the gateway.</div></div>
</div>

{_cash_need_card("account", held)}
{_cash_need_card("payout", held)}

<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Forecast with live receivables</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">Pending
     settlements are pulled at run time, so the incoming side is as current as
     your gateway is.</p>
  <form method="post" action="/agents/cash-forecaster/run">
    <input type="hidden" name="source" value="connected">
    <button style="font-size:14px;padding:12px 26px">
      Forecast the next 30 days</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent which payout to move
    </label>
  </form>
</div>"""

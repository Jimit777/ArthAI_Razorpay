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
/* minmax(0,1fr), not a bare 1fr: a bare 1fr floors at the track's
   min-content width, so main's 1120px cap plus the 216px rail forced a
   1336px page and every narrower viewport - 1280 and 1366 laptops
   included - scrolled sideways on every page. */
.app { display:grid; grid-template-columns:216px minmax(0,1fr);
  min-height:100vh }
.rail { background:var(--surface); border-right:1px solid var(--line);
  display:flex; flex-direction:column; position:sticky; top:0; height:100vh }
.rail .logo { display:flex; align-items:center; gap:8px; padding:14px 14px 10px;
  font-weight:680; letter-spacing:-.02em; font-size:14.5px; color:var(--ink) }
.mark { width:21px; height:21px; border-radius:5px; background:var(--brand);
  color:#fff; display:grid; place-items:center; font-size:12px; font-weight:700 }
.rail nav { padding:2px 8px; overflow-y:auto; flex:1 }
.group { font-size:10px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); padding:13px 7px 4px; font-weight:600 }
.rail .flow-group { font-size:10px; color:var(--faint); padding:7px 8px 2px 30px;
  font-weight:600; letter-spacing:.02em }
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
/* margin:0 auto centres the column inside its track. Without it the 1120px
   cap left-aligns and every pixel of slack piles up on the right, which on a
   wide screen reads as a broken layout rather than a measure. */
main { padding:20px 24px 60px; max-width:1120px; margin:0 auto }
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
/* Checkboxes and radios are not text fields - the rule above stretched them
   to fill their container and gave them a text-input's padding and border,
   which renders as a large blank box with the tick lost somewhere inside
   it rather than a small control next to its label. */
input[type="checkbox"], input[type="radio"] { width:16px; height:16px;
  min-width:16px; padding:0; border:0; background:none; accent-color:var(--brand);
  flex:0 0 auto; cursor:pointer }
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

/* --- category flows: home and the agents hub --------------------------- */
.flow-section { margin:22px 0 26px }
.flow-header { font-size:11.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); font-weight:600; margin-bottom:9px }
.track-scroll { overflow-x:auto; padding-bottom:4px; margin:0 -2px }
.track { display:flex; align-items:stretch; gap:0; min-width:min-content }
.track .card { flex:0 0 280px; width:280px }
.chevron { flex:0 0 30px; display:flex; align-items:center; justify-content:center;
  color:var(--faint); font-size:15px }
.stage-card.plumbing { display:flex; flex-direction:column; justify-content:center;
  gap:6px; border-style:dashed; background:var(--raised) }
.stage-card-label { font-weight:600; font-size:13.5px; color:var(--ink-2) }
.stage-note { margin:0; font-size:11.8px; color:var(--muted); line-height:1.5 }
@media (max-width:640px) {
  .track .card { flex-basis:220px; width:220px }
}
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
    from merchant.nav import FLOWS, route_for, visible

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
        # page becoming a root-level entry. Grouped by business process
        # (nav.FLOWS) rather than listed flat - the same regrouping Home and
        # the Agents hub got, so the rail stops being the one place in the
        # product still organised by agent instead of by the work a merchant
        # actually thinks in.
        if label == "Workspace":
            live_by_id = {spec.id: spec for spec in agents if spec.is_live}
            for flow in FLOWS:
                flow_specs = [live_by_id[stage.agent_id] for stage in flow.stages
                             if stage.agent_id in live_by_id]
                if not flow_specs:
                    continue
                blocks.append(f'<div class="flow-group">{esc(flow.label)}</div>')
                for spec in flow_specs:
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


def imported_payments_table(rows) -> str:
    """
    The transactions themselves - what the gateway said happened, before
    anything audits them.

    Deliberately shows no verdict. This is the raw import, and a merchant
    asking "did my transactions come through" is asking a different question
    from "was I charged correctly"; answering the second here would make the
    data panel a second, quieter opinion competing with the auditor's.
    """
    if not rows:
        return ""

    body = ""
    for r in rows:
        bits = [str(r["method"] or "unknown")]
        if r["card_network"]:
            bits.append(str(r["card_network"]))
        if r["card_type"]:
            bits.append(str(r["card_type"]))
        if r["is_international"]:
            bits.append("international")
        charged = (r["fee"] or 0) + (r["tax"] or 0)
        when = (_day_words(
            datetime.fromtimestamp(r["created_at"], timezone.utc)
            .date().isoformat()) if r["created_at"] else "&mdash;")
        body += f"""
    <tr>
      <td class="mono" style="font-size:11.5px">{esc(r["payment_id"])}</td>
      <td>{esc(" ".join(bits))}</td>
      <td class="r">{esc(rupees(r["amount"]))}</td>
      <td class="r">{esc(rupees(charged))}</td>
      <td class="r" style="color:var(--muted)">{when}</td>
    </tr>"""

    return f"""
    <div class="card flush" style="margin:0 0 12px">
      <div class="card-head"><h2>What came in</h2>
        <span class="sub">{len(rows)} transaction{'s' if len(rows) != 1 else ''},
          newest first</span></div>
      <div style="overflow-x:auto">
        <table>
          <tr><th>Payment</th><th>Instrument</th><th class="r">Amount</th>
              <th class="r">Charged</th><th class="r">Paid on</th></tr>
          {body}
        </table>
      </div>
      <div style="padding:10px 16px;border-top:1px solid var(--line-2);
        color:var(--muted);font-size:11.5px">
        Straight from the gateway, before any audit. &ldquo;Charged&rdquo; is
        the fee plus GST Razorpay deducted &mdash; whether it should have is
        the auditor&rsquo;s question, not this table&rsquo;s.
      </div>
    </div>"""


def summarise(text: str, words: int = 20) -> str:
    """
    The first sentence of an agent's reasoning, capped.

    The reasoning itself is the product and is never cut on the page where
    somebody chose to read it. A summary list is not that page: the Home
    queue was rendering nine findings at their full length - over nine
    hundred words of citation-dense argument stacked on the front door -
    which buries the one thing a list is for, deciding what to open.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    # Cut at the first sentence end that is not a decimal point or an
    # abbreviation like "s.10A" - a bare split on "." mangles both.
    for i, ch in enumerate(text):
        if ch in ".?!" and i + 1 < len(text) and text[i + 1] == " ":
            if not text[i - 1].isdigit():
                text = text[:i + 1]
                break
    parts = text.split()
    if len(parts) > words:
        return " ".join(parts[:words]).rstrip(".,;:") + "…"
    return text


# Acronyms that must not be sentence-cased. "Zero mdr violation" reads as a
# typo to the only people qualified to judge this product.
_ACRONYMS = {"mdr", "gst", "gstr", "tds", "upi", "itc", "hsn", "rcm", "utr",
             "qrmp", "irn", "pan", "b2b", "b2c", "api"}


def code_label(code: str) -> str:
    """
    An exception code as a person reads it: ZERO_MDR_VIOLATION becomes
    "Zero-MDR violation", not "Zero mdr violation".
    """
    words = (code or "").replace("_", " ").strip().split()
    if not words:
        return ""
    out = [w.upper() if w.lower() in _ACRONYMS else w.lower() for w in words]
    if out[0] not in _ACRONYMS and not out[0].isupper():
        out[0] = out[0].capitalize()
    return " ".join(out)


def google_button(label: str = "Continue with Google") -> str:
    """
    Google's sign-in button, plus the "or" divider under it.

    The mark is inlined as SVG rather than loaded from Google's CDN: this
    page is served to a signed-out visitor, and a remote asset on it would
    tell Google someone is looking at the login screen before they have
    chosen to involve Google at all. It is also the only way the button
    renders offline. The four brand colours are Google's own and are
    deliberately literal hex values, not theme tokens - the mark is a
    trademark and does not re-colour with our palette.
    """
    mark = (
        '<svg width="17" height="17" viewBox="0 0 48 48" aria-hidden="true">'
        '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85'
        'C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19'
        'C12.43 13.72 17.74 9.5 24 9.5z"/>'
        '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02'
        'h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 '
        '7.09-17.65z"/>'
        '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-'
        '3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 '
        '10.78l7.97-6.19z"/>'
        '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6'
        'c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19'
        'C6.51 42.62 14.62 48 24 48z"/></svg>')
    return f"""
    <a class="btn-google" href="/login/google">{mark}<span>{esc(label)}</span></a>
    <div class="auth-or"><span>or</span></div>"""


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


# --- the Home dashboard's hero row -----------------------------------------
#
# Pure CSS bars, the same "chart" language cash_curve() already established
# for this codebase (hand-rolled, zero external dependencies) - just simpler,
# since a four-stage drop-off needs no curve fitting.

def _source_split(summary: dict) -> str:
    """
    Where the money on this page came from - the gateway, or the simulator.

    A dashboard that mixes both and says neither invites the worst possible
    misreading: a merchant, or a judge, taking generated figures for real
    ones. Shown whenever both are present; a single-source business needs no
    disclaimer and gets none.
    """
    by = summary.get("by_source") or {}
    live = by.get("razorpay") or {}
    demo = by.get("simulator") or {}
    if not (live and demo):
        return ""

    total = (live.get("gross_paise", 0) + demo.get("gross_paise", 0)) or 1
    live_pct = live.get("gross_paise", 0) / total * 100
    return f"""
  <div class="src-split">
    <div class="src-split-bar">
      <span class="seg live" style="width:{live_pct:.1f}%"></span>
      <span class="seg demo" style="width:{100 - live_pct:.1f}%"></span>
    </div>
    <div class="src-split-keys">
      <span><i class="live"></i>Razorpay
        <b>{esc(rupees(live.get("gross_paise", 0)))}</b>
        <em>{live.get("payment_count", 0)} payments</em></span>
      <span><i class="demo"></i>Simulated
        <b>{esc(rupees(demo.get("gross_paise", 0)))}</b>
        <em>{demo.get("payment_count", 0)} payments</em></span>
    </div>
  </div>"""


def dashboard_waterfall(summary: dict, ask_embed: str = "") -> str:
    """
    The hero card: gross sales through gateway deductions to what should
    have landed, then what actually did - the product's own pitch, made
    visible rather than only claimed. `ask_embed` is the settlement-scoped
    ask box, rendered by the caller and slotted in at the bottom exactly
    the way the reference design floats its command bar inside the chart
    card, not a separate one below it.

    Each bar is the RUNNING BALANCE at that stage, not the stage's own
    amount. That matters: gateway fees and GST are ~1.7% of gross, so
    drawing them as their own bars on a shared axis produced two invisible
    slivers and a hole in the middle of the chart. Running balance gives
    every stage a real bar, descends like a funnel, and - most importantly -
    is honest: the gentle slope IS the finding. Almost everything got
    through. The amount removed at each step is stated as a figure beneath
    the bar rather than implied by a height nobody can see.
    """
    gross = summary.get("gross_paise", 0)
    fee = summary.get("fee_paise", 0)
    tax = summary.get("tax_paise", 0)

    # (label, running balance after this stage, amount removed at this stage)
    stages = (
        ("Gross Sales", gross, 0),
        ("After Gateway Fees", gross - fee, fee),
        ("After GST on Fees", gross - fee - tax, tax),
        ("Net Settled", summary.get("net_paise", 0), 0),
    )
    tallest = max((balance for _l, balance, _d in stages), default=0) or 1

    bars = []
    for i, (label, balance, removed) in enumerate(stages):
        height_pct = max(2, round(abs(balance) / tallest * 100))
        last = i == len(stages) - 1
        tone = " net" if last else ""
        # The two middle stages are the deductions; the reference ghosts
        # its non-focal columns the same way, so the eye lands on the
        # start and the end - which is the comparison that matters.
        if 0 < i < len(stages) - 1:
            tone += " ghost"
        delta = (f'<div class="bar-delta">− {esc(rupees(removed))}</div>'
                 if removed else '<div class="bar-delta"></div>')
        bars.append(f"""
      <div class="waterfall-bar{tone}">
        <div class="bar-val">{esc(rupees(balance))}</div>
        <div class="bar-fill" style="height:{height_pct}%"></div>
        <div class="bar-label">{esc(label)}</div>
        {delta}
      </div>""")

    gap = summary.get("net_paise", 0) - summary.get("bank_credited_paise", 0)
    if summary.get("bank_credited_paise"):
        compare = f"""
    <div class="waterfall-compare">
      <span>Actually credited to your bank: <b>{esc(rupees(summary["bank_credited_paise"]))}</b></span>
      {f'<span style="color:var(--danger)">Rs {abs(gap) / 100:,.2f} unexplained</span>'
       if abs(gap) > 100 else '<span style="color:var(--good)">Matches, to the paise</span>'}
    </div>"""
    else:
        compare = ""

    # The method mix strip - one segment per instrument, widths by share of
    # payment count. Real counts off the payments table, no estimation.
    mix = summary.get("method_mix") or []
    total_n = sum(n for _m, n in mix) or 1
    segs = "".join(
        f'<span class="mix-seg mix-{esc(str(method))}"'
        f' style="width:{n / total_n * 100:.2f}%"'
        f' title="{esc(str(method))}: {n}"></span>'
        for method, n in mix)
    keys = "".join(
        f'<span class="mix-key"><i class="mix-{esc(str(method))}"></i>'
        f'{esc(str(method))} <b>{n}</b></span>' for method, n in mix)
    mix_strip = (f'<div class="mix-strip">{segs}</div>'
                 f'<div class="mix-keys">{keys}</div>') if mix else ""

    return f"""
<div class="card dash-card">
  <div class="dash-head">
    <div>
      <h2 style="margin:0 0 4px">Payments</h2>
      <p class="sub" style="margin:0">Sales in, deductions out, what landed.</p>
    </div>
    <span class="pill-count">{summary.get("payment_count", 0)} payments</span>
  </div>
  {_source_split(summary)}
  {mix_strip}
  <div class="waterfall-wrap">
    <div class="waterfall">{"".join(bars)}</div>
    {compare}
    {ask_embed}
  </div>
</div>"""


def dashboard_ask_embed(audited_count: int, findings: int, actionable: int,
                        instrument_count: int) -> str:
    """
    The settlement auditor's real /ask box, embedded - not a rewrite, and
    not pretending it can answer for every agent. The .scope line stays
    exactly as visible here as it is on the standalone /agents/settlement/ask
    page, so Home never implies a wider reach than the box actually has.
    """
    from merchant.ask import SUGGESTIONS

    chips = "".join(
        f'<button type="button" class="chip" data-q="{esc(q)}">{esc(q)}</button>'
        for q in SUGGESTIONS[:4])

    return f"""
<div class="ask-embed">
  <form method="post" action="/ask" id="dash-ask-form">
    <div class="row">
      <div><input name="question" id="dash-ask-input" maxlength="500" required
        autocomplete="off" placeholder="What would you like to explore next?"></div>
      <div style="flex:0"><button id="dash-ask-go">Ask</button></div>
    </div>
  </form>
  <div style="margin-top:10px">{chips}</div>
  <div class="scope" style="margin-top:12px">
    <span>It can see <b>{audited_count}</b> audited settlement(s)</span>
    <span><b>{findings}</b> records, <b>{actionable}</b> with findings</span>
    <span>your rate card (<b>{instrument_count}</b> instruments)</span>
    <span style="color:var(--faint)">settlement questions only, nothing else</span>
  </div>
  <div id="dash-ask-thread"></div>
</div>
<script>
(function () {{
  var form = document.getElementById('dash-ask-form');
  var input = document.getElementById('dash-ask-input');
  var go = document.getElementById('dash-ask-go');
  var thread = document.getElementById('dash-ask-thread');

  document.querySelectorAll('.ask-embed .chip').forEach(function (chip) {{
    chip.onclick = function () {{ input.value = chip.dataset.q; submit(); }};
  }});

  function block(cls, html) {{
    var el = document.createElement('div');
    el.className = cls;
    el.innerHTML = html;
    return el;
  }}

  function submit(ev) {{
    if (ev) ev.preventDefault();
    var question = input.value.trim();
    if (!question) return;

    go.disabled = true;
    input.value = '';

    var pending = block('qa', '');
    var q = block('qa-q', '<span class="qa-who">You</span>' +
                          '<span class="qa-text"></span>');
    q.querySelector('.qa-text').textContent = question;
    var a = block('qa-a', '<span class="qa-thinking">Reading your settlements' +
                          '<span class="qa-dots"></span></span>');
    pending.appendChild(q); pending.appendChild(a);
    thread.insertBefore(pending, thread.firstChild);

    fetch('/ask', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'}},
      body: 'question=' + encodeURIComponent(question)
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{ pending.outerHTML = d.html; }})
      .catch(function () {{
        a.innerHTML = '<p style="color:var(--danger)">Could not reach the ' +
                      'agent. The answer was not lost - try again.</p>';
      }})
      .finally(function () {{ go.disabled = false; input.focus(); }});
  }}

  form.onsubmit = submit;
}})();
</script>"""


def dashboard_side_panel(summary: dict) -> str:
    """
    The gross-volume side card: one big number, then three unrelated
    figures shown together because they're the three worth a glance, not
    because they sum to anything - see .vol-bar-row's own CSS comment.
    """
    rows = [
        ("ITC safe to claim", summary.get("itc_safe_paise", 0), "good"),
        ("ITC at risk", summary.get("itc_at_risk_paise", 0), "danger"),
        ("Recoverable overcharges", summary.get("recoverable_paise", 0), "brand"),
    ]
    tallest = max((v for _l, v, _t in rows), default=0) or 1

    bars = "".join(f"""
    <div class="vol-bar-row">
      <div class="vol-bar-head"><span>{esc(label)}</span><b>{esc(rupees(value))}</b></div>
      <div class="vol-bar-track"><div class="vol-bar-fill {tone}"
        style="width:{max(2, round(value / tallest * 100)) if value else 0}%"></div></div>
    </div>""" for label, value, tone in rows)

    # The reference puts a trend pill beside its big number. There is no
    # prior period to trend against here, so this states the share that
    # survived deductions instead - a real ratio off the same two figures,
    # not a growth number we cannot compute.
    gross = summary.get("gross_paise", 0)
    kept = (f'<span class="trend-pill">{summary.get("net_paise", 0) / gross * 100:.1f}% kept</span>'
            if gross else "")

    return f"""
<div class="card dash-card">
  <h2 style="margin:0 0 2px">Gross Volume</h2>
  <div class="big-figure">
    <span class="figure-num">{esc(rupees(gross))}</span>{kept}
  </div>
  <p class="sub" style="margin:0">across every settlement audited</p>
  {bars}
  <p class="sub" style="margin-top:14px;font-size:11px">Three separate
     figures, not parts of one total.</p>
</div>"""


def stat_card(title: str, value: str, sub: str, dots: int = 0,
              filled: int = 0, tone: str = "brand", href: str = "") -> str:
    """
    One small counting card - Transactions, Customers, Vendors.

    `dots`/`filled` draw the reference's little dot-matrix: `dots` cells,
    the first `filled` of them lit. Both are real counts passed in by the
    caller; when there is nothing to count the card says so plainly rather
    than drawing an empty grid that looks like a loading state.
    """
    matrix = ""
    if dots:
        cells = "".join(
            f'<i class="{"on" if i < filled else ""}"></i>' for i in range(dots))
        matrix = f'<div class="dotgrid tone-{esc(tone)}">{cells}</div>'

    inner = f"""
  <div class="stat-head">{esc(title)}</div>
  <div class="stat-value">{esc(value)}</div>
  {matrix}
  <div class="stat-sub">{esc(sub)}</div>"""

    if href:
        return (f'<a class="card stat-card" href="{esc(href)}"'
                f' style="text-decoration:none;color:inherit">{inner}</a>')
    return f'<div class="card stat-card">{inner}</div>'


def dashboard_bottom(summary: dict, candidates: list) -> str:
    """
    The reference's second row: a few counting cards on the left, and the
    tall glossy Insights panel on the right.

    Every card here is a real count off a real table (see
    Ledger.dashboard_summary). A business that has never run the agent
    owning a table gets a 0 and an explicit "nothing here yet" line -
    never a placeholder number, and never a card implying data we do not
    have.
    """
    txns = summary.get("payment_count", 0)
    methods = summary.get("method_count", 0)
    customers = summary.get("customer_count", 0)
    registered = summary.get("customer_registered", 0)
    vendors = summary.get("vendor_count", 0)
    overbilled = summary.get("vendor_overbilled_paise", 0)

    cards = [
        stat_card(
            "Transactions", f"{txns:,}",
            (f"across {methods} payment method{'s' if methods != 1 else ''}"
             if txns else "no settlements recorded yet"),
            dots=min(txns, 24), filled=min(txns, 24), tone="brand",
            href="/agents/settlement"),
        stat_card(
            "Customers", f"{customers:,}",
            (f"{registered} GST-registered, {customers - registered} not"
             if customers else "no sales invoices loaded yet"),
            dots=min(customers, 24), filled=min(registered, 24), tone="good",
            href="/agents/gst-filing"),
        stat_card(
            "Vendors", f"{vendors:,}",
            (f"{rupees(overbilled)} overbilled" if overbilled
             else ("all within agreed terms" if vendors
                   else "no supplier invoices checked yet")),
            dots=min(vendors, 24), filled=min(vendors, 24),
            tone="danger" if overbilled else "good",
            href="/agents/vendor-terms"),
    ]

    return f"""
<div class="dash-row2">
  <div class="stat-stack">{"".join(cards)}</div>
  {insights_panel(candidates)}
</div>"""


def insights_panel(candidates: list) -> str:
    """
    The tall glossy Insights card - the single most urgent agent finding,
    with the runners-up listed beneath it.

    The headline is still that agent's own real number and its own
    reasoning; the gloss is visual weight earned by rank, never a
    fabricated score standing in for one. With nothing to report the panel
    says exactly that, because "no agent has anything for you" is a real
    and reassuring answer, not an empty state to hide.
    """
    if not candidates:
        return """
  <div class="card insights-panel quiet">
    <div class="insight-agent">Insights</div>
    <div class="insights-quiet-head">Nothing is waiting on you.</div>
    <p class="sub" style="margin:0">No agent has found anything worth your
       attention in what it has been given so far.</p>
  </div>"""

    head, rest = candidates[0], candidates[1:]
    more = "".join(f"""
      <a class="insights-more-row" href="{esc(c["href"])}">
        <span class="im-agent">{esc(c["agent"])}</span>
        <span class="im-head">{esc(c["headline"])}</span>
      </a>""" for c in rest)

    # The panel is a <div>, not an <a>: the runner-up rows below are
    # themselves links, and an <a> inside an <a> is invalid HTML - the
    # parser closes the outer one early and throws the rest of the card
    # out of it. The headline gets its own link instead.
    return f"""
  <div class="card insights-panel tone-{esc(head["tone"])}">
    <a class="insights-lead" href="{esc(head["href"])}">
      <span class="insight-agent">Insights · {esc(head["agent"])}</span>
      <span class="insights-headline">{esc(head["headline"])}</span>
      <span class="insights-sub">{esc(head["subtext"])}</span>
    </a>
    {f'<div class="insights-more">{more}</div>' if more else ""}
  </div>"""


def insight_row(candidates: list) -> str:
    """
    Up to four agents' own headline figures, worst-first - see
    app.py's _insight_candidates() for how the list is built and ranked.
    Renders nothing when there is nothing worth flagging, rather than a
    row of empty placeholders. The single most urgent one gets the big
    gloss treatment (`.insight-hero`); it is still that agent's own real
    number and reasoning, just given the visual weight its rank earns it -
    not a fabricated score standing in for one.
    """
    if not candidates:
        return ""

    head, rest = candidates[0], candidates[1:]

    hero = f"""
    <a class="insight-card insight-hero tone-{head["tone"]}" href="{esc(head["href"])}"
       style="display:block;text-decoration:none;color:inherit">
      <div class="insight-agent">{esc(head["agent"])}</div>
      <div class="insight-headline">{esc(head["headline"])}</div>
      <div class="insight-subtext">{esc(head["subtext"])}</div>
    </a>"""

    cards = "".join(f"""
    <a class="insight-card tone-{c["tone"]}" href="{esc(c["href"])}"
       style="display:block;text-decoration:none;color:inherit">
      <div class="insight-agent">{esc(c["agent"])}</div>
      <div class="insight-headline">{esc(c["headline"])}</div>
      <div class="insight-subtext">{esc(c["subtext"])}</div>
    </a>""" for c in rest)

    return f'<div class="insight-row" style="margin-top:16px">{hero}{cards}</div>'


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
    <div class="src-what">Eight invented suppliers with 36 months of filing
      history. The law and the arithmetic are real; the companies are not.
      <b>Do not act on this against a real supplier.</b></div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">See what this agent does</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:56ch">Scores every
     supplier and opens the results. Nothing leaves this machine.</p>
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
     records with faults planted on purpose &mdash; money that never arrived,
     short credits, lines with no invoice. Because we planted them, the match
     rate on the next screen is <b>measured, not asserted</b>.</p>
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

    # The cross-agent connection to the settlement auditor, made clickable.
    # Before this, "the gateway has no settlement for this" and "the gateway
    # already has this under dispute" read as the same finding - a merchant
    # had no way to tell the two apart without opening the settlement audit
    # by hand and searching for the payment themselves.
    disputed = "".join(
        f'<div style="margin-top:5px;font-size:11.3px;color:var(--danger)">'
        f'Already under dispute in your settlement audit &mdash; '
        f'<b>{esc(f["money_at_stake_display"])}</b> &middot; '
        f'<a href="/agents/settlement/run/{esc(f["run_id"])}" '
        f'style="color:var(--danger);font-weight:600">See it &rarr;</a></div>'
        for f in (row.get("disputed_findings") or []))

    return f"""
      <tr>
        <td style="max-width:44ch">
          <div style="font-weight:560">{esc(row["detail"])}</div>
          <div style="color:var(--muted);font-size:11.3px;margin-top:3px">
            {esc(row["reasoning"] or notes.get(row["finding_type"], ""))}</div>
          {checked}
          {disputed}
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
    """
    Settlements come from the settlement auditor; the other two are uploaded.

    No longer gated on a Razorpay connection: the settlement side is whatever
    this platform already holds, however it got here - a simulated batch or a
    real import - so demanding a gateway connection refused the tab to
    businesses that had settlements ready to match.
    """
    settled = held.get("settlement")
    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Settlements come from the settlement auditor</b>
    <div class="src-what">Whatever this platform already holds, simulated or
      imported. Your invoices and your bank statement are still yours to
      upload &mdash; neither is on the gateway.</div></div>
</div>

<div class="card">
  <h2>Settlements</h2>
  <p class="sub" style="margin:3px 0 12px;max-width:68ch">Taken from the
     settlement auditor &mdash; the batches this platform already holds,
     whichever gateway they came from. Payment lines only: refunds, transfers
     and adjustments belong in a settlement audit rather than in a three-way
     match against sales invoices.</p>
  <form method="post" action="/agents/three-way/use-settlements">
    <button>Use my settlements</button>
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
    "settlement_status": "checked the settlement audit",
}

TOOL_WORDS = {
    "what_if_delayed": "simulated moving a payment",
    "payout_detail": "looked up a payment",
    "movements_on": "opened a day in the forecast",
    "settlement_status": "checked the settlement audit for a dispute",
    "at_risk_input_credit": "checked the GST reconciler for credit at risk",
    "recon_status": "checked the three-way reconciliation",
    "at_risk_output_tax": "checked the GST output-tax reconciler for locked shortfalls",
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

    # The one live cross-agent connection on this platform, made clickable
    # rather than left as a sentence in the reasoning below. Before this, a
    # merchant could read that a receipt was disputed and had no way to open
    # the actual finding - the two agents talked to each other and the
    # merchant was still cut out of the conversation. Computed here, ahead of
    # the CASH_HEALTHY branch below, because a dispute is worth knowing about
    # whether or not the month is otherwise short - a comfortable balance
    # that includes disputed money is not as comfortable as it looks.
    disputed = ""
    receipts = verdict.get("disputed_receipts") or []
    if receipts:
        rows = "".join(
            f'<div style="margin-top:{0 if i == 0 else 10}px">'
            f'<b>{esc(r["payment_id"])}</b> is under an unresolved '
            f'{esc(r["exception_code"].replace("_", " ").lower())} dispute '
            f'&mdash; <b>{esc(r["money_at_stake_display"])}</b> at stake.'
            f'<br><a href="/agents/settlement/run/{esc(r["run_id"])}" '
            f'style="color:var(--danger);font-weight:600">'
            f'See it in your settlement audit &rarr;</a></div>'
            for i, r in enumerate(receipts))
        disputed = (
            f'<div class="draft" style="border-left:3px solid var(--danger)">'
            f'<div style="font-weight:600;margin-bottom:2px">Before counting '
            f'on this money: your settlement audit already found a problem'
            f'</div>{rows}</div>')

    # The third live cross-agent connection: this exact receipt already
    # flagged by the three-way reconciler - never credited by the bank, or
    # credited as a different amount. Grouped with `disputed` above rather
    # than with the credit check below, because both are the same kind of
    # question: is this specific piece of the curve actually real.
    recon_flagged = ""
    flags = verdict.get("recon_flagged") or []
    if flags:
        rows = "".join(
            f'<div style="margin-top:{0 if i == 0 else 10}px">'
            f'<b>{esc(r["payment_id"])}</b> was flagged by your three-way '
            f'reconciliation as '
            f'{esc(r["finding"].replace("_", " ").lower())} '
            f'&mdash; <b>{esc(r["at_stake_display"])}</b> at stake.'
            f'<br><a href="/agents/three-way" '
            f'style="color:var(--danger);font-weight:600">'
            f'See it in your reconciliation &rarr;</a></div>'
            for i, r in enumerate(flags))
        recon_flagged = (
            f'<div class="draft" style="border-left:3px solid var(--danger)">'
            f'<div style="font-weight:600;margin-bottom:2px">Before counting '
            f'on this money: your three-way reconciliation already flagged '
            f'it</div>{rows}</div>')

    # The second live cross-agent connection: claimed input tax credit the
    # GST reconciler found should not have been claimed. Unlike a disputed
    # receipt this is not money the curve is counting as arriving - it is a
    # standing liability sitting outside the thirty days entirely, which is
    # exactly why a balance that looks fine can still be wrong.
    at_risk_credit = ""
    credit = verdict.get("at_risk_credit") or {}
    if credit.get("at_risk_paise"):
        n = credit.get("count", 0)
        at_risk_credit = (
            f'<div class="draft" style="border-left:3px solid var(--warn)">'
            f'<div style="font-weight:600;margin-bottom:2px">Outside this '
            f'curve: {esc(credit.get("at_risk_display", ""))} of claimed '
            f'input credit may have to be repaid</div>'
            f'<div>{n} claim{"" if n == 1 else "s"} your GST reconciler '
            f'found blocked, past deadline, or claimed twice &mdash; left '
            f'unfixed this becomes a demand with interest, not a forecast '
            f'line.<br><a href="/agents/input-credit/reconciliation" '
            f'style="color:var(--warn);font-weight:600">See it in your '
            f'input credit reconciliation &rarr;</a></div></div>')

    # The fourth live cross-agent connection: the mirror of at_risk_credit
    # on the outward side. A locked GST filing period whose GSTR-3B fell
    # short of what GSTR-1 supports is not money the curve is counting as
    # arriving either - it is a tax bill already due, sitting outside the
    # thirty days, with interest accruing whether or not this forecast
    # notices it.
    at_risk_output_tax = ""
    output_tax = verdict.get("at_risk_output_tax") or {}
    if output_tax.get("at_risk_paise"):
        n = output_tax.get("count", 0)
        at_risk_output_tax = (
            f'<div class="draft" style="border-left:3px solid var(--warn)">'
            f'<div style="font-weight:600;margin-bottom:2px">Outside this '
            f'curve: {esc(output_tax.get("at_risk_display", ""))} of '
            f'output tax is already due</div>'
            f'<div>{n} locked filing period{"" if n == 1 else "s"} your '
            f'GST output-tax reconciler found short of what GSTR-3B paid '
            f'&mdash; interest is accruing under s.50 whether or not this '
            f'forecast counts it.<br><a href="/agents/gst-filing/corrections" '
            f'style="color:var(--warn);font-weight:600">See it in your '
            f'output-tax corrections &rarr;</a></div></div>')

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
  {disputed}
  {recon_flagged}
  {at_risk_credit}
  {at_risk_output_tax}
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
  {disputed}
  {recon_flagged}
  {at_risk_credit}
  {at_risk_output_tax}
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

/* --- the Home dashboard's hero row --------------------------------------
   .dash-grid collapses under the same 900px breakpoint SHELL already
   defines for .app, so it needs no media query of its own here. Colors
   throughout this block are still only --brand/--good/--danger/--warn and
   their washes - richer gradients and textures are those same tokens
   layered or darkened with color-mix(), never a new hex value. */
.dash-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr);
  gap:16px }
@media (max-width:900px) { .dash-grid { grid-template-columns:1fr } }

/* The gateway/simulator split on the Home waterfall. Same two colours as the
   source tags below, so one language across the app. */
.src-split { margin:14px 0 4px }
.src-split-bar { display:flex; gap:3px; height:8px; border-radius:999px;
  overflow:hidden }
.src-split-bar .seg { display:block; height:100%; border-radius:999px;
  min-width:3px }
.src-split-bar .seg.live { background:var(--brand) }
.src-split-bar .seg.demo { background:var(--warn) }
.src-split-keys { display:flex; gap:18px; flex-wrap:wrap; margin-top:8px;
  font-size:11.5px; color:var(--muted) }
.src-split-keys span { display:inline-flex; align-items:baseline; gap:5px }
.src-split-keys i { width:8px; height:8px; border-radius:3px;
  align-self:center }
.src-split-keys i.live { background:var(--brand) }
.src-split-keys i.demo { background:var(--warn) }
.src-split-keys b { color:var(--ink); font-variant-numeric:tabular-nums }
.src-split-keys em { font-style:normal; color:var(--faint) }

/* --- where a row's data came from ----------------------------------------
   One glance should answer "is this real?". Blue for the gateway, amber for
   the simulator - the same two colours the source badges already use, so the
   table agrees with the badge above it rather than inventing a third
   language. A left edge rather than a filled row: it must read at a glance
   without competing with the money, which is what the eye is here for. */
.src-live { border-left:3px solid var(--brand) }
.src-demo { border-left:3px solid var(--warn) }
tr.src-live > td:first-child { box-shadow:inset 3px 0 0 var(--brand) }
tr.src-demo > td:first-child { box-shadow:inset 3px 0 0 var(--warn) }
.src-tag { font-size:10.5px; font-weight:650; letter-spacing:.04em;
  padding:2px 7px; border-radius:999px; white-space:nowrap }
.src-tag.live { background:var(--brand-wash); color:var(--brand-ink) }
.src-tag.demo { background:var(--warn-wash); color:var(--warn) }

/* --- Google sign-in ------------------------------------------------------ */
.btn-google { display:flex; align-items:center; justify-content:center;
  gap:10px; width:100%; padding:11px 14px; border-radius:9px;
  border:1px solid var(--line); background:var(--surface); color:var(--ink);
  font-size:13.5px; font-weight:600; text-decoration:none; cursor:pointer }
.btn-google:hover { background:var(--line-2) }
.btn-google svg { flex:none; display:block }
.auth-or { display:flex; align-items:center; gap:10px; margin:16px 0;
  color:var(--faint); font-size:11.5px }
.auth-or::before, .auth-or::after { content:""; flex:1; height:1px;
  background:var(--line) }

.dash-card { padding:26px 28px 22px; border-radius:18px }
.dash-card h2 { font-size:17px; font-weight:700; letter-spacing:-.015em }
.dash-head { display:flex; justify-content:space-between; align-items:flex-start;
  gap:12px }
.pill-count { flex:none; font-size:11.5px; font-weight:650; color:var(--ink-2);
  background:var(--line-2); border-radius:999px; padding:5px 11px;
  font-variant-numeric:tabular-nums }

/* Big number + its ratio pill, the reference's own pairing. */
.big-figure { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  margin:10px 0 4px }
/* Targets the number by class, not by element - .trend-pill is a sibling
   span, and a bare `.big-figure > span` sets its size too. */
.big-figure > .figure-num { font-size:38px; font-weight:780;
  letter-spacing:-.025em; font-variant-numeric:tabular-nums; line-height:1 }
.trend-pill { font-size:11.5px; font-weight:700; color:var(--good);
  background:var(--good-wash); border-radius:999px; padding:4px 9px;
  white-space:nowrap }

/* Instrument mix: one segment per method, width = share of payment count. */
.mix-strip { display:flex; gap:3px; height:8px; margin:16px 0 9px;
  border-radius:999px; overflow:hidden }
.mix-seg { display:block; height:100%; border-radius:999px; min-width:3px;
  background:var(--brand) }
.mix-seg.mix-card { background:color-mix(in srgb, var(--brand) 60%, white) }
.mix-seg.mix-netbanking { background:color-mix(in srgb, var(--brand) 34%, white) }
.mix-seg.mix-wallet { background:color-mix(in srgb, var(--brand) 20%, white) }
.mix-keys { display:flex; gap:14px; flex-wrap:wrap; font-size:11.3px;
  color:var(--ink-2); margin-bottom:2px }
.mix-key { display:inline-flex; align-items:center; gap:5px }
.mix-key i { width:8px; height:8px; border-radius:3px; background:var(--brand) }
.mix-key i.mix-card { background:color-mix(in srgb, var(--brand) 60%, white) }
.mix-key i.mix-netbanking { background:color-mix(in srgb, var(--brand) 34%, white) }
.mix-key i.mix-wallet { background:color-mix(in srgb, var(--brand) 20%, white) }
.mix-key b { font-variant-numeric:tabular-nums; color:var(--ink) }

.waterfall { display:flex; align-items:flex-end; gap:22px; height:210px;
  padding:26px 4px 0 }
.waterfall-bar { flex:1; min-width:0; display:flex; flex-direction:column;
  align-items:center; height:100%; justify-content:flex-end }
.waterfall-bar .bar-val { font-size:17px; font-weight:750; color:var(--ink);
  font-variant-numeric:tabular-nums; margin-bottom:8px; text-align:center;
  letter-spacing:-.01em }
@media (max-width:520px) {
  .waterfall-bar .bar-val { font-size:13px }
  .waterfall { gap:10px }
}
.waterfall-bar .bar-fill { width:100%; max-width:72px; border-radius:10px 10px 3px 3px;
  min-height:3px; position:relative; overflow:hidden;
  background:
    repeating-linear-gradient(-45deg, rgba(255,255,255,.32) 0 3px, transparent 3px 9px),
    linear-gradient(180deg, var(--brand) 0%, var(--brand-ink) 100%);
  box-shadow:0 10px 18px -10px color-mix(in srgb, var(--brand) 70%, transparent) }
.waterfall-bar.net .bar-fill {
  background:
    repeating-linear-gradient(-45deg, rgba(255,255,255,.32) 0 3px, transparent 3px 9px),
    linear-gradient(180deg, var(--good) 0%, color-mix(in srgb, var(--good) 100%, black 20%) 100%);
  box-shadow:0 10px 18px -10px color-mix(in srgb, var(--good) 70%, transparent) }
.waterfall-bar .bar-label { font-size:11.5px; color:var(--muted); margin-top:10px;
  text-align:center; line-height:1.3; font-weight:560 }
/* The amount removed at this stage, stated rather than drawn - the bars
   carry the running balance, and at ~1.7% of gross these deductions have
   no legible height of their own. See dashboard_waterfall()'s docstring. */
.waterfall-bar .bar-delta { font-size:11.5px; font-weight:700; margin-top:3px;
  color:var(--danger); font-variant-numeric:tabular-nums; min-height:15px;
  text-align:center }
/* Non-focal middle stages, ghosted the way the reference ghosts its own,
   so the eye lands on where the money started and where it ended. */
.waterfall-bar.ghost .bar-val { color:var(--muted); font-weight:650 }
.waterfall-bar.ghost .bar-fill { opacity:.42 }
.waterfall-compare { display:flex; justify-content:space-between; gap:12px;
  padding:14px 6px; margin-top:6px; border-top:1px solid var(--line-2);
  font-size:12.5px; color:var(--ink-2) }
.waterfall-compare b { color:var(--ink); font-variant-numeric:tabular-nums }

/* The reference design floats its command bar so it overlaps the base of
   the chart rather than sitting in normal document flow below a divider -
   same idea here, pulled up over the compare line's empty space so the
   bars and their values (the actual data) stay fully readable above it. */
.waterfall-wrap { position:relative }
.ask-embed { position:relative; margin:-14px 2px 0; padding:16px 18px 18px;
  background:color-mix(in srgb, var(--surface) 92%, transparent);
  backdrop-filter:blur(6px); border:1px solid var(--line);
  border-radius:16px; box-shadow:0 14px 28px -14px rgba(16,24,40,.22),
  0 2px 8px rgba(16,24,40,.06); z-index:2 }
.ask-embed .row input { font-size:13.5px }
.ask-embed .row button { border-radius:10px; padding:9px 18px; font-weight:600 }

/* Three unrelated figures shown together, not parts of one whole - see
   dashboard_side_panel()'s own docstring. Track height and pill radius
   deliberately larger than .progress's thin 6px shape: this stack is the
   card's main content, not a background indicator. */
.vol-bar-row { margin-top:18px }
.vol-bar-row .vol-bar-head { display:flex; justify-content:space-between;
  font-size:13px; margin-bottom:6px }
.vol-bar-row .vol-bar-head span { color:var(--ink-2); font-weight:560 }
.vol-bar-row .vol-bar-head b { font-variant-numeric:tabular-nums; font-size:13.5px }
.vol-bar-track { height:10px; background:var(--line-2); border-radius:999px;
  overflow:hidden }
.vol-bar-fill { height:100%; border-radius:999px; min-width:5px }
.vol-bar-fill.good { background:var(--good) }
.vol-bar-fill.danger, .vol-bar-fill.brand {
  background-image:
    repeating-linear-gradient(-45deg, rgba(255,255,255,.4) 0 3px, transparent 3px 7px),
    linear-gradient(90deg, currentColor, currentColor) }
.vol-bar-fill.danger { color:var(--danger) }
.vol-bar-fill.brand { color:var(--brand) }

/* --- the counting cards + the tall Insights panel ----------------------- */
.dash-row2 { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);
  gap:16px; margin-top:16px; align-items:stretch }
@media (max-width:900px) { .dash-row2 { grid-template-columns:1fr } }

.stat-stack { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:16px; align-content:start }
.stat-card { padding:20px 22px; border-radius:18px; display:block }
.stat-head { font-size:11px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--faint); font-weight:700 }
.stat-value { font-size:34px; font-weight:780; letter-spacing:-.025em;
  margin-top:6px; font-variant-numeric:tabular-nums; line-height:1.05 }
.stat-sub { font-size:11.8px; color:var(--ink-2); margin-top:8px; line-height:1.4 }

/* The reference's dot matrix. Cells are a real count, capped at 24 so a
   large batch stays one tidy block rather than an unreadable field. */
.dotgrid { display:flex; flex-wrap:wrap; gap:4px; margin-top:12px; max-width:150px }
.dotgrid i { width:8px; height:8px; border-radius:2.5px; background:var(--line-2) }
.dotgrid.tone-brand i.on { background:var(--brand) }
.dotgrid.tone-good i.on { background:var(--good) }
.dotgrid.tone-danger i.on { background:var(--danger) }

.insights-panel { position:relative; overflow:hidden; border-radius:18px;
  padding:26px 28px; color:#fff; display:flex; flex-direction:column;
  border:none; box-shadow:0 18px 38px -18px rgba(16,24,40,.42);
  background:linear-gradient(135deg, var(--brand-ink), var(--brand)) }
.insights-panel.tone-danger { background:linear-gradient(135deg,
  color-mix(in srgb, var(--danger) 100%, black 30%), var(--danger)) }
.insights-panel.tone-warn { background:linear-gradient(135deg,
  color-mix(in srgb, var(--warn) 100%, black 30%), var(--warn)) }
/* The reference's glossy sweep - a light source in the top-right corner. */
.insights-panel::after { content:""; position:absolute; inset:0;
  pointer-events:none;
  background:radial-gradient(130% 130% at 100% 0%, rgba(255,255,255,.30),
    rgba(255,255,255,.06) 45%, transparent 70%) }
.insights-panel > * { position:relative; z-index:1 }
.insights-panel .insight-agent { font-size:11px; text-transform:uppercase;
  letter-spacing:.07em; color:rgba(255,255,255,.78); font-weight:700 }
.insights-lead { display:block; text-decoration:none; color:inherit }
.insights-headline { display:block; font-size:36px; font-weight:780;
  letter-spacing:-.03em; margin-top:10px; line-height:1.08;
  font-variant-numeric:tabular-nums }
.insights-sub { display:block; font-size:12.8px; line-height:1.5;
  margin:10px 0 0; color:rgba(255,255,255,.9) }
.insights-more { margin-top:auto; padding-top:18px; display:grid; gap:1px }
.insights-more-row { display:flex; justify-content:space-between; gap:10px;
  padding:9px 0; border-top:1px solid rgba(255,255,255,.22);
  text-decoration:none; color:#fff; font-size:12.2px }
.insights-more-row .im-agent { color:rgba(255,255,255,.8) }
.insights-more-row .im-head { font-weight:680; text-align:right;
  font-variant-numeric:tabular-nums }
.insights-panel.quiet { background:var(--surface); color:var(--ink);
  border:1px solid var(--line); box-shadow:var(--shadow) }
.insights-panel.quiet::after { display:none }
.insights-panel.quiet .insight-agent { color:var(--faint) }
.insights-quiet-head { font-size:20px; font-weight:720; margin:10px 0 6px;
  letter-spacing:-.015em }

.insight-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:14px; align-items:stretch }
.insight-card { border-radius:14px; padding:16px 18px; box-shadow:var(--shadow);
  background:var(--surface); border:1px solid var(--line);
  border-top:3px solid var(--line-2) }
.insight-card.tone-danger { border-top-color:var(--danger) }
.insight-card.tone-warn { border-top-color:var(--warn) }
.insight-card .insight-agent { font-size:10.8px; text-transform:uppercase;
  letter-spacing:.06em; color:var(--faint); font-weight:700 }
.insight-card .insight-headline { font-size:23px; font-weight:750; margin-top:5px;
  font-variant-numeric:tabular-nums; letter-spacing:-.01em }
.insight-card .insight-subtext { font-size:12px; color:var(--ink-2);
  margin-top:3px; line-height:1.4 }

/* The one glossy standout - reserved for whichever agent's own number is
   the most urgent this run (see app.py's _insight_candidates ranking).
   Gradient stops are still only the tone's own token, darkened with
   color-mix rather than a hand-picked second hex. */
.insight-hero { border-radius:16px; padding:22px 24px; color:#fff;
  position:relative; overflow:hidden; box-shadow:0 16px 32px -16px rgba(16,24,40,.35);
  grid-column:span 2 }
.insight-hero::after { content:""; position:absolute; inset:0;
  background:radial-gradient(120% 140% at 100% 0%, rgba(255,255,255,.22), transparent 60%) }
.insight-hero.tone-brand { background:linear-gradient(135deg, var(--brand-ink), var(--brand)) }
.insight-hero.tone-danger { background:linear-gradient(135deg,
  color-mix(in srgb, var(--danger) 100%, black 25%), var(--danger)) }
.insight-hero.tone-warn { background:linear-gradient(135deg,
  color-mix(in srgb, var(--warn) 100%, black 25%), var(--warn)) }
.insight-hero .insight-agent { position:relative; font-size:11px; text-transform:uppercase;
  letter-spacing:.07em; color:rgba(255,255,255,.75); font-weight:700 }
.insight-hero .insight-headline { position:relative; font-size:34px; font-weight:780;
  margin-top:8px; font-variant-numeric:tabular-nums; letter-spacing:-.02em }
.insight-hero .insight-subtext { position:relative; font-size:13px;
  color:rgba(255,255,255,.88); margin-top:8px; line-height:1.5; max-width:46ch }
@media (max-width:560px) { .insight-hero { grid-column:span 1 } }
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
    <div class="src-what">A generated month, with a cash crunch on day 14.
      The arithmetic is real; the company is not.</div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">Thirty days forward</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:58ch">Projects thirty
     days, finds the low point, and says which payment to move. Nothing is
     scheduled or paid.</p>
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


# --- vendor invoice auditor -------------------------------------------------

def vendor_terms_demo_screen(latest_link: str = "") -> str:
    """Tab 1. A generated purchase register with a matching rate card, and
    known overbilling planted against it."""
    return f"""
<div class="src demo">
  <span class="src-dot"></span>
  <div><b>Demo mode &mdash; this is a generated purchase register</b>
    <div class="src-what">Forty line items from six invented suppliers, some
      billed above contract. The arithmetic is real; the suppliers are
      not.</div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">Check the batch</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:58ch">Checks every
     line against the rate card and drafts a credit note for each overcharge.
     Nothing is sent.</p>
  <form method="post" action="/agents/vendor-terms/run">
    <input type="hidden" name="tab" value="demo">
    <button style="font-size:14px;padding:12px 26px">Run Demo Mode</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each overbilled supplier is worth pursuing
    </label>
  </form>
  {latest_link}
</div>

<div class="card tint">
  <h2>What counts as overbilled, and what does not</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">A line billed above
     the contracted price, past a small tolerance for rounding, is
     overbilled. A line billed at or below the contracted price is never a
     finding &mdash; being undercharged helps you, not a reason to raise
     anything. An item with no contracted price on file is excluded from
     every total rather than guessed at; add a rate and it can be checked.</p>
</div>"""


def vendor_terms_upload_screen(pending_items: int, sample_csv: str,
                               latest_link: str = "") -> str:
    """Tab 2. A merchant's own purchase register, as a file."""
    import base64

    sample_href = ("data:text/csv;base64,"
                   + base64.b64encode(sample_csv.encode()).decode())

    run_card = f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to check</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {pending_items} billed line item{"" if pending_items == 1 else "s"} on
     file, not yet checked.</p>
  <form method="post" action="/agents/vendor-terms/run">
    <input type="hidden" name="tab" value="without-api">
    <button style="font-size:14px;padding:12px 26px">
      Check against the rate card</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each overbilled supplier is worth pursuing
    </label>
  </form>
  {latest_link}
</div>""" if pending_items else """
<div class="card tint">
  <h2>Nothing to check yet</h2>
  <p class="sub" style="margin:4px 0 0;max-width:68ch">Upload a purchase
     register below - every supplier, item, quantity and rate you were
     billed.</p>
</div>"""

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is sent, claimed or paid.</b> Your file is read and
  checked against your rate card. A credit note request is only ever a
  draft.</span>
</div>

{run_card}

<div class="card" style="margin-top:16px">
  <h2 style="margin:0 0 4px">Upload a purchase register</h2>
  <p class="sub" style="margin:0 0 14px;max-width:64ch">A CSV or Excel
     export from Tally, Zoho, Busy or a spreadsheet &mdash; supplier name,
     GSTIN, invoice number and date, item description, quantity and rate.
     Column names are matched by what they mean, not by an exact header.</p>
  <form method="post" action="/agents/vendor-terms/upload"
        enctype="multipart/form-data"
        style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <input type="file" name="file" accept=".csv,.xlsx,.xlsm" required>
    <button class="btn small">Upload</button>
  </form>
  <p class="sub" style="margin:12px 0 0">
    <a href="{sample_href}" download="sample-purchase-register.csv">
      Download a sample file</a> to see the expected shape.</p>
</div>

<div class="card tint" style="margin-top:16px">
  <h2>Rate card</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">A billed item with no
     contracted price on file is excluded from every check, never guessed
     at. <a href="/agents/vendor-terms/rates">Review and add rates</a>
     before running, or add them inline once a check has run.</p>
</div>"""


def vendor_terms_connected_screen(pending_items: int, zoho_connected: bool,
                                  latest_link: str = "") -> str:
    """Tab 3. Purchases pulled from Zoho Books."""
    if not zoho_connected:
        return """
<div class="banner warn">
  <span><b>No Zoho Books account is connected to this business.</b> Connect
    one in <a href="/data/zoho">Data &amp; integrations</a> and your bills -
    with their line items - can be pulled directly.</span>
</div>

<div class="card tint">
  <h2>What connecting gets you</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">The same Zoho
     connection the GST input credit reconciler already uses. Pulling bills
     also pulls each bill's item, quantity and rate - the detail the ITC
     reconciler never needed and this agent runs on.</p>
</div>"""

    run_card = f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to check</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {pending_items} billed line item{"" if pending_items == 1 else "s"}
     pulled from Zoho, not yet checked.</p>
  <form method="post" action="/agents/vendor-terms/run">
    <input type="hidden" name="tab" value="with-api">
    <button style="font-size:14px;padding:12px 26px">
      Check against the rate card</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each overbilled supplier is worth pursuing
    </label>
  </form>
  {latest_link}
</div>""" if pending_items else """
<div class="card tint">
  <h2>Nothing pulled yet</h2>
  <p class="sub" style="margin:4px 0 0;max-width:68ch">Pull your bills from
     <a href="/data/zoho">Data &amp; integrations</a> first - the same pull
     the GST input credit reconciler uses also brings in each bill's line
     items.</p>
</div>"""

    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Zoho Books connected</b>
    <div class="src-what">Bills are pulled from Data &amp; integrations;
      each one's items, quantities and rates are checked here.</div></div>
</div>

{run_card}"""


def vendor_rate_card_screen(rows) -> str:
    """The merchant's own negotiated prices, one row per supplier per item -
    the only source for this check, since no API for a supplier's agreed
    price exists anywhere."""
    body_rows = "".join(f"""
      <tr>
        <td class="mono">{esc(r["supplier_gstin"])}</td>
        <td>{esc(r["description"])}</td>
        <td class="r">Rs {r["contracted_unit_price_paise"] / 100:,.2f}</td>
        <td>{esc(r["source"] or "")}</td>
      </tr>""" for r in rows)

    table = (f"""
<div class="card" style="padding:0;overflow:hidden"><table>
  <thead><tr><th>Supplier GSTIN</th><th>Item</th><th class="r">Contracted
    price</th><th>Source</th></tr></thead>
  <tbody>{body_rows}</tbody>
</table></div>""" if rows else
        '<div class="card tint"><h2>No rates on file yet</h2>'
        '<p class="sub" style="margin:4px 0 0">Add one below.</p></div>')

    return f"""
<div class="card" style="margin-bottom:16px">
  <h2 style="margin:0 0 10px">Add a rate</h2>
  <form method="post" action="/agents/vendor-terms/rate"
        style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
    <input type="hidden" name="back_to" value="/agents/vendor-terms/rates">
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Supplier GSTIN</label>
      <input name="supplier_gstin" placeholder="27AABCU9603R1ZM" required></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Item description</label>
      <input name="description" placeholder="Steel Rod - 12mm" required></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Contracted price (Rs / unit)</label>
      <input name="price_rupees" type="number" step="0.01" min="0.01"
        required style="width:130px"></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Source (optional)</label>
      <input name="source" placeholder="PO-1042"></div>
    <button class="btn small">Save</button>
  </form>
</div>
{table}"""


def _vendor_terms_supplier_card(supplier_name: str, gstin: str,
                                items: list, credit_note_text: str,
                                decided_by: str, queued: bool) -> str:
    rows = "".join(f"""
      <tr>
        <td>{esc(r["description"])}</td>
        <td class="r">{r["quantity_x100"] / 100:g}</td>
        <td class="r">Rs {r["unit_price_paise"] / 100:,.2f}</td>
        <td class="r">Rs {r["contracted_unit_price_paise"] / 100:,.2f}</td>
        <td class="r" style="color:var(--danger)">
          Rs {r["money_at_stake_paise"] / 100:,.2f}</td>
      </tr>""" for r in items)

    stake = sum(r["money_at_stake_paise"] for r in items)
    note = ""
    if queued:
        note = ('<div class="banner warn" style="margin:10px 0 0">'
               '<span>Sent to a person to decide.</span></div>')

    letter = ""
    if credit_note_text:
        letter = f"""
<details class="working" style="margin-top:12px">
  <summary>Credit note request - ready to send</summary>
  <div class="working-body">
    <pre style="white-space:pre-wrap;font-family:inherit;font-size:12.8px;
      background:var(--raised);border-radius:8px;padding:12px;margin:0">
{esc(credit_note_text)}</pre>
  </div>
</details>"""

    return f"""
<div class="card" style="margin-bottom:14px">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <h2 style="margin:0">{esc(supplier_name)}</h2>
    <b style="color:var(--danger)">Rs {stake / 100:,.2f} at stake</b>
  </div>
  <p class="sub mono" style="margin:2px 0 10px">{esc(gstin)}</p>
  <table>
    <thead><tr><th>Item</th><th class="r">Qty</th><th class="r">Billed</th>
      <th class="r">Contracted</th><th class="r">Over</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {note}
  {letter}
</div>"""


def vendor_terms_results(run, findings) -> str:
    """Overbilled suppliers first, then what could not be checked."""
    from collections import defaultdict

    from merchant.ui import blank_slate

    by_supplier: dict = defaultdict(list)
    unconfigured = []
    for f in findings:
        if f["code"] == "OVERBILLED":
            by_supplier[(f["supplier_gstin"], f["supplier_name"])].append(f)
        elif f["code"] == "RATE_UNCONFIGURED":
            unconfigured.append(f)

    total_stake = sum(f["money_at_stake_paise"] for f in findings)
    n_overbilled = sum(len(v) for v in by_supplier.values())

    stats = f"""
<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  <div class="stats">
    <div class="stat"><b>{run["n_items"]}</b><span>line items checked</span></div>
    <div class="stat"><b>{n_overbilled}</b><span>overbilled</span></div>
    <div class="stat"><b>{len(unconfigured)}</b><span>no rate on file</span></div>
    <div class="stat"><b style="color:var(--danger)">
      Rs {total_stake / 100:,.2f}</b><span>total at stake</span></div>
  </div>
</div>"""

    supplier_cards = "" if by_supplier else blank_slate(
        "Nothing overbilled",
        "Every priced line item matched the rate card, within tolerance.")
    for (gstin, name), items in sorted(
            by_supplier.items(), key=lambda kv: -sum(
                r["money_at_stake_paise"] for r in kv[1])):
        first = items[0]
        supplier_cards += _vendor_terms_supplier_card(
            name, gstin, items, first["credit_note_text"] or "",
            first["decided_by"], bool(first["queued_for_human"]))

    unconfigured_rows = "".join(f"""
      <tr>
        <td>{esc(r["supplier_name"])}</td>
        <td>{esc(r["description"])}</td>
        <td class="r">Rs {r["unit_price_paise"] / 100:,.2f}</td>
        <td>
          <form method="post" action="/agents/vendor-terms/rate"
                style="display:flex;gap:6px;align-items:center">
            <input type="hidden" name="back_to" value="/vendor-terms/{esc(run["run_id"])}">
            <input type="hidden" name="supplier_gstin" value="{esc(r["supplier_gstin"])}">
            <input type="hidden" name="description" value="{esc(r["description"])}">
            <input name="price_rupees" type="number" step="0.01" min="0.01"
              placeholder="Rs / unit" required style="width:100px">
            <button class="ghost small">Set rate</button>
          </form>
        </td>
      </tr>""" for r in unconfigured)

    unconfigured_block = ""
    if unconfigured:
        unconfigured_block = f"""
<div style="margin:22px 0 11px"><h2 style="margin:0">No rate on file</h2></div>
<div class="card" style="padding:0;overflow:hidden"><table>
  <thead><tr><th>Supplier</th><th>Item</th><th class="r">Billed at</th>
    <th>Set the rate</th></tr></thead>
  <tbody>{unconfigured_rows}</tbody>
</table></div>"""

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here has been sent or claimed.</b>
  Every credit note request is a draft waiting for you.</span>
</div>

{stats}
{supplier_cards}
{unconfigured_block}

<div class="card tint" style="margin-top:20px">
  <h2>How this was worked out</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Every billed line is
     compared to your own vendor rate card. A gap of Rs 1 or 0.5% of the
     contracted price, whichever is larger, is treated as rounding, not an
     overcharge. Undercharging is never flagged. An item with no contracted
     price on file is excluded from the totals above rather than guessed
     at.</p>
</div>"""


# --- chargeback defence assembler -------------------------------------------

_CB_EVIDENCE_LABEL: dict[str, str] = {
    "shipping_proof": "Proof of shipment/delivery",
    "billing_proof": "Proof of order/billing",
    "cancellation_proof": "Proof of cancellation",
    "customer_communication": "Customer communication",
    "proof_of_service": "Proof of service",
    "explanation_letter": "Explanation letter",
    "refund_confirmation": "Refund confirmation",
    "access_activity_log": "Access/activity log",
    "refund_cancellation_policy": "Refund/cancellation policy",
    "term_and_conditions": "Terms and conditions",
}


def chargeback_demo_screen(latest_link: str = "") -> str:
    """Tab 1. Generated disputes with generated evidence, planted against a
    known answer key."""
    return f"""
<div class="src demo">
  <span class="src-dot"></span>
  <div><b>Demo mode &mdash; this is a generated dispute batch</b>
    <div class="src-what">Thirty invented disputes across real reason codes,
      with evidence complete, partial or missing by design. The rules are
      real; the disputes are not.</div></div>
</div>

<div class="card" style="text-align:center;padding:34px 24px">
  <h2 style="margin:0">Check the batch</h2>
  <p class="sub" style="margin:7px auto 18px;max-width:58ch">One click
     checks each dispute's evidence against what its reason code actually
     requires, and drafts a reply where there is something to argue with.
     Nothing is submitted.</p>
  <form method="post" action="/agents/chargeback/run">
    <input type="hidden" name="tab" value="demo">
    <button style="font-size:14px;padding:12px 26px">Run Demo Mode</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each dispute is worth contesting
    </label>
  </form>
  {latest_link}
</div>

<div class="card tint">
  <h2>What "evidence complete" actually means</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Every reason code
     here is checked against a real, published table of which evidence
     types a card network expects for it - not a guess. A dispute with
     some but not all of what's required still gets a drafted letter; the
     letter says plainly what's missing rather than arguing around the
     gap. A reason code outside that table is never given a made-up
     checklist - it goes to a person instead.</p>
</div>"""


def _dispute_evidence_form(dispute_id: str, reason_code: str,
                           required: list[str], back_to: str) -> str:
    from engine.chargeback.rules import evidence_types_for

    types = required or list(evidence_types_for(reason_code))
    if not types:
        return ('<p class="sub" style="margin:8px 0 0">No requirement list '
               'for this reason code - nothing to enter yet.</p>')
    fields = "".join(f"""
      <div style="margin-bottom:8px">
        <label class="sub" style="display:block;margin-bottom:3px">
          {esc(_CB_EVIDENCE_LABEL.get(t, t))}</label>
        <input name="evidence_{t}"
          placeholder="what you have - a tracking number, a one-line summary"
          style="width:100%"></div>
      """ for t in types)
    return f"""
    <form method="post" action="/agents/chargeback/evidence" style="margin-top:10px">
      <input type="hidden" name="dispute_id" value="{esc(dispute_id)}">
      <input type="hidden" name="reason_code" value="{esc(reason_code)}">
      <input type="hidden" name="back_to" value="{esc(back_to)}">
      {fields}
      <button class="ghost small">Save evidence</button>
    </form>"""


def _pending_dispute_card(row, back_to: str) -> str:
    from engine.chargeback.rules import evidence_types_for

    required = list(evidence_types_for(row["reason_code"]))
    return f"""
<div class="card" style="margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <b>{esc(row["reason_code"])}</b>
    <span class="sub">Rs {row["amount_paise"] / 100:,.2f}</span>
  </div>
  <p class="sub" style="margin:2px 0 0">{esc(row["reason_description"] or "")}
     &mdash; payment {esc(row["payment_id"] or "")}</p>
  {_dispute_evidence_form(row["dispute_id"], row["reason_code"], required, back_to)}
</div>"""


def chargeback_manual_screen(pending: list, latest_link: str = "") -> str:
    """Tab 2. A merchant typing in a dispute notice themselves - there is
    no register concept for this the way there is for a purchase invoice,
    since a chargeback notice is a one-off event, not a recurring file."""
    run_card = f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to check</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {len(pending)} dispute{"" if len(pending) == 1 else "s"} on file, not
     yet checked.</p>
  <form method="post" action="/agents/chargeback/run">
    <input type="hidden" name="tab" value="without-api">
    <button style="font-size:14px;padding:12px 26px">
      Check against the requirement list</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each dispute is worth contesting
    </label>
  </form>
  {latest_link}
</div>""" if pending else """
<div class="card tint">
  <h2>Nothing to check yet</h2>
  <p class="sub" style="margin:4px 0 0;max-width:68ch">Record a dispute
     below - the reason code, amount and deadline from the notice you
     received.</p>
</div>"""

    pending_cards = "".join(
        _pending_dispute_card(r, "/agents/chargeback/without-api")
        for r in pending)

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here is sent, submitted or claimed.</b> A representment
  letter is only ever a draft.</span>
</div>

{run_card}

<div class="card" style="margin-top:16px">
  <h2 style="margin:0 0 4px">Record a dispute</h2>
  <p class="sub" style="margin:0 0 14px;max-width:64ch">Type in what the
     notice said - the reason code exactly as Razorpay showed it, the
     amount, and the date you must respond by.</p>
  <form method="post" action="/agents/chargeback/manual"
        style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Payment reference</label>
      <input name="payment_id" placeholder="pay_..." required></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Reason code</label>
      <input name="reason_code" placeholder="1064" required style="width:90px"></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Reason (optional)</label>
      <input name="reason_description" placeholder="Goods/Services Not Received"></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Amount (Rs)</label>
      <input name="amount_rupees" type="number" step="0.01" min="0.01"
        required style="width:110px"></div>
    <div><label class="sub" style="display:block;margin-bottom:4px">
      Respond by</label>
      <input name="respond_by_date" type="date" required></div>
    <button class="btn small">Record</button>
  </form>
</div>

{f'<div style="margin:22px 0 11px"><h2 style="margin:0">Not yet checked</h2></div>{pending_cards}' if pending else ""}"""


def chargeback_connected_screen(pending: list, source_kind, has_secret: bool,
                                latest_link: str = "") -> str:
    """Tab 3. Dispute notices pulled from Razorpay; evidence still the
    merchant's to supply, on this tab exactly as on Without API."""
    if source_kind != "razorpay":
        return """
<div class="banner warn">
  <span><b>No Razorpay account is connected to this business.</b> Connect
    one in <a href="/data">Data &amp; integrations</a> and dispute notices
    can be pulled directly - reason code, amount and deadline, straight
    from Razorpay's own Disputes API.</span>
</div>

<div class="card tint">
  <h2>What connecting changes, and what it does not</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">The dispute NOTICE
     itself - reason code, amount, the response deadline - is real and
     fetchable once connected. The evidence behind it is not: no API
     anywhere supplies delivery proof or a customer's chat log, so you
     still enter that yourself, exactly as on the Without API tab.</p>
</div>"""

    field = ('<input name="key_secret" type="password" placeholder="Key secret" required>'
             if not has_secret else
             '<span class="sub">The secret is stored encrypted.</span>')

    pull_card = f"""
<div class="card" style="margin-bottom:16px">
  <h2>Pull disputes from Razorpay</h2>
  <p class="sub" style="margin:6px 0 12px">Reads your real Disputes -
     reason code, amount and the response deadline. Alongside Demo Mode,
     never instead of it.</p>
  <form method="post" action="/agents/chargeback/sync-disputes"
        style="display:flex;gap:10px;align-items:center">
    {field}
    <button class="btn small">Sync</button>
  </form>
</div>"""

    run_card = f"""
<div class="card" style="text-align:center;padding:28px 24px">
  <h2 style="margin:0">Ready to check</h2>
  <p class="sub" style="margin:7px auto 16px;max-width:56ch">
     {len(pending)} dispute{"" if len(pending) == 1 else "s"} pulled, not
     yet checked.</p>
  <form method="post" action="/agents/chargeback/run">
    <input type="hidden" name="tab" value="with-api">
    <button style="font-size:14px;padding:12px 26px">
      Check against the requirement list</button>
    <label style="display:flex;align-items:center;justify-content:center;
      gap:8px;margin-top:14px;font-size:12.4px;color:var(--ink-2)">
      <input type="checkbox" name="use_agent" value="yes" checked
        style="width:auto;margin:0">
      Ask the agent whether each dispute is worth contesting
    </label>
  </form>
  {latest_link}
</div>""" if pending else """
<div class="card tint">
  <h2>Nothing pulled yet</h2>
  <p class="sub" style="margin:4px 0 0">Sync above first.</p>
</div>"""

    pending_cards = "".join(
        _pending_dispute_card(r, "/agents/chargeback/with-api")
        for r in pending)

    return f"""
<div class="src ok">
  <span class="src-dot"></span>
  <div><b>Razorpay connected</b>
    <div class="src-what">Dispute notices are pulled here; evidence is
      still yours to enter below, per dispute.</div></div>
</div>

{pull_card}
{run_card}
{f'<div style="margin:22px 0 11px"><h2 style="margin:0">Not yet checked</h2></div>{pending_cards}' if pending else ""}"""


def _cb_deadline_badge(days: int) -> str:
    from merchant.ui import badge

    if days <= 2:
        return badge(f"{days} day(s) left", "danger")
    if days <= 5:
        return badge(f"{days} day(s) left", "warn")
    return badge(f"{days} day(s) left", "good")


def _chargeback_finding_card(row, now: int) -> str:
    import json

    from merchant.ui import badge

    code = row["code"]
    tone = {"EVIDENCE_COMPLETE": "good", "EVIDENCE_PARTIAL": "warn",
           "EVIDENCE_MISSING": "", "REASON_CODE_UNMAPPED": "danger"}.get(code, "")
    label = {"EVIDENCE_COMPLETE": "Every required document is on file",
            "EVIDENCE_PARTIAL": "Some required documents are on file",
            "EVIDENCE_MISSING": "No evidence on file yet",
            "REASON_CODE_UNMAPPED": "No requirement list for this reason code",
            }.get(code, code)

    days = int((row["respond_by"] - now) // 86_400)

    pack_html = ""
    if row["evidence_pack_json"]:
        pack = json.loads(row["evidence_pack_json"])
        if pack.get("explanation_letter"):
            pack_html = f"""
<details class="working" style="margin-top:12px">
  <summary>Explanation letter - ready to send</summary>
  <div class="working-body">
    <pre style="white-space:pre-wrap;font-family:inherit;font-size:12.8px;
      background:var(--raised);border-radius:8px;padding:12px;margin:0">
{esc(pack["explanation_letter"])}</pre>
  </div>
</details>"""
        if pack.get("summary"):
            pack_html += f"""
<div class="sub" style="margin-top:8px"><b>Submission summary:</b>
  {esc(pack["summary"])}</div>"""

    note = ""
    if row["queued_for_human"]:
        note = ('<div class="banner warn" style="margin:10px 0 0">'
               '<span>Sent to a person to decide.</span></div>')

    evidence_form = ""
    if code in ("EVIDENCE_PARTIAL", "EVIDENCE_MISSING"):
        evidence_form = _dispute_evidence_form(
            row["dispute_id"], row["reason_code"], [],
            f"/chargeback/{row['run_id']}")

    return f"""
<div class="card" style="margin-bottom:14px">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <div>
      <b>{esc(row["reason_code"])}</b>
      <span class="sub" style="margin-left:8px">Rs {row["amount_paise"] / 100:,.2f}</span>
    </div>
    {_cb_deadline_badge(max(days, 0))}
  </div>
  <p class="sub" style="margin:4px 0 8px">{esc(row["reasoning"] or "")}</p>
  {badge(label, tone)}
  {note}
  {pack_html}
  {evidence_form}
</div>"""


def chargeback_results(run, findings) -> str:
    """Worst-deadline-first - there is no natural grouping key the way
    "supplier" was for vendor_terms; each dispute already is the unit."""
    import time as _time

    from merchant.ui import blank_slate

    # Demo deadlines are planted relative to the generator's own fixed
    # AS_OF, not real time - see engine/chargeback/generator.py's own
    # docstring and merchant/agents/chargeback.py's identical reasoning for
    # the classification pass. Real wall-clock time keeps moving further
    # past that fixed anchor, so scoring "days left" against it here would
    # make every demo dispute read as overdue the moment this demo ages.
    if run["source"] == "demo":
        from engine.chargeback.generator import AS_OF

        now = int(AS_OF.timestamp())
    else:
        now = int(_time.time())

    ordered = sorted(findings, key=lambda r: r["respond_by"])
    actionable = [r for r in ordered if r["action"] == "draft_evidence_pack"]
    unmapped = [r for r in ordered if r["code"] == "REASON_CODE_UNMAPPED"]
    total_stake = sum(r["amount_paise"] for r in actionable)

    stats = f"""
<div class="card" style="padding:0;overflow:hidden;margin-bottom:16px">
  <div class="stats">
    <div class="stat"><b>{run["n_disputes"]}</b><span>disputes checked</span></div>
    <div class="stat"><b>{len(actionable)}</b><span>evidence drafted</span></div>
    <div class="stat"><b>{len(unmapped)}</b><span>no requirement list</span></div>
    <div class="stat"><b>Rs {total_stake / 100:,.2f}</b>
      <span>covered by a draft</span></div>
  </div>
</div>"""

    cards = "".join(_chargeback_finding_card(r, now) for r in ordered)
    if not ordered:
        cards = blank_slate("Nothing to show", "Run a check first.")

    return f"""
<div class="banner brand" style="margin-bottom:16px">
  <span><b>Nothing here has been submitted to Razorpay or any network.</b>
  Every draft is a proposal waiting for you.</span>
</div>

{stats}
{cards}

<div class="card tint" style="margin-top:20px">
  <h2>How this was worked out</h2>
  <p class="sub" style="margin:4px 0 0;max-width:70ch">Every dispute's
     evidence checklist comes from a real, published table of what each
     card network expects for that reason code - never a guess. A
     dispute within two days of its deadline is always sent to a person,
     whatever the agent's confidence.</p>
</div>"""

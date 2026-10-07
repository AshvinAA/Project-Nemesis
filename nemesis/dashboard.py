"""Phase 9: the weight-evolution dashboard ("watch jev get smart").

Zero dependencies: stdlib http.server + one embedded HTML page (dark theme,
vanilla JS).  Endpoints:
    /         -> the console
    /state    -> live snapshot (same JSON the agent writes to live_state.json)
    /history  -> tail of the 1 Hz history rows (sparkline + drift fuel)

Runs three ways:
  * wired into the agent  — rl_agent.run() starts it with the in-RAM
    LiveState, so it always serves the freshest snapshot;
  * standalone (files)    — `python -m nemesis.dashboard` reads the files off
    disk (inspect a past session while the agent is down);
  * standalone (--live)   — `python -m nemesis.dashboard --live` CONNECTS to
    the engine itself (observe-only, never sends orders) and drives the
    snapshot live, so the dashboard runs alongside a plain game session with
    no agent.  The engine accepts exactly ONE client: use --live only when
    the agent is NOT running.

Phase 9.17: the "jev pulling the strings" feed — /state carries param_feed,
the list of every learned variable jev changed (tactic weights, weapon
bias, per-type deaths, rank, XP, epsilon), newest first.
Phase 9.18: side-by-side demo mode — `?compact=1` (or the link in the
status bar) squeezes the console to a single ~660 px column so it sits
beside the game's default 640x400 window.
Phase 9.19: "DOCUMENTS" restyle — the console is now a phosphor-green
query terminal. Every learned variable renders as a bracketed question
document ([FIRING] [GOAL] [DODGE] [MOVEMENT]) whose rows are animated
bar meters fed straight from /state (tactic weights, weapon bias, XP,
rank, epsilon, deaths) — you literally watch jev move his numbers, and
a changed variable glows its document. The top block mirrors the
terminal chrome: a `> standing order` line and a DIRECTOR status row.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

HISTORY_TAIL = 900  # rows sent to the UI (~15 min at 1 Hz)

# The 8-order vocabulary (p_nemesis.c NEM_OrderIndex) — heatmap columns.
ORDER_KEYS = ["chase", "hold", "fallback", "flank_left", "flank_right",
              "ambush", "focus_fire", "use_door"]

# One color per order, shared by the heatmap accents and the drift chart.
ORDER_COLORS = {
    "chase": "#46ff7d", "hold": "#34d399", "fallback": "#f87171",
    "flank_left": "#fbbf24", "flank_right": "#fb923c", "ambush": "#a78bfa",
    "focus_fire": "#f472b6", "use_door": "#94a3b8",
}

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEMESIS — weight evolution console</title>
<style>
:root{
  --bg:#040805; --panel:#0a130d; --panel2:#0d1a10; --line:#1c3a26;
  --txt:#a8e6b8; --dim:#4e8a63; --cy:#46ff7d; --gr:#46ff7d; --rd:#ff5d5d;
  --am:#a5ffc6; --mono:ui-monospace,'Cascadia Mono',Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1200px 600px at 70% -10%,#08150c 0%,var(--bg) 60%);
  color:var(--txt);font-family:var(--mono);min-height:100vh;padding:18px}
a{color:var(--cy)}
h1{font-size:15px;letter-spacing:3px;color:var(--cy);text-transform:uppercase;
  text-shadow:0 0 18px #46ff7d44}
h1 .sub{color:var(--dim);letter-spacing:1px;text-transform:none;font-size:11px}
.wrap{max-width:1180px;margin:0 auto}
.bar{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:10px;padding:12px;position:relative;
  box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}
.panel h2{font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  margin-bottom:10px;display:flex;justify-content:space-between}
.panel h2 .r{color:#3d6b4c;letter-spacing:0;text-transform:none}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}
.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
.big{font-size:30px;font-weight:700;color:#e2ffe9;line-height:1.1}
.big .u{font-size:12px;color:var(--dim)}
.gauges{display:flex;gap:10px;flex-wrap:wrap}
.gauge{flex:1;min-width:110px;text-align:center;background:#081209;border:1px solid var(--line);
  border-radius:8px;padding:10px 6px}
.gauge .v{font-size:22px;font-weight:700;color:#e2ffe9}
.gauge .l{font-size:9px;color:var(--dim);letter-spacing:1px;margin-top:3px}
.gauge.ok .v{color:var(--gr)}.gauge.warn .v{color:var(--am)}.gauge.bad .v{color:var(--rd)}
/* skill meter */
.meter{display:flex;gap:6px;margin-top:6px}
.seg{flex:1;height:16px;border-radius:4px;background:#081209;border:1px solid var(--line);
  position:relative;overflow:hidden}
.seg.on{background:linear-gradient(90deg,#0f8a3d,#46ff7d);border-color:#46ff7d;
  box-shadow:0 0 12px #46ff7d66}
.mlabels{display:flex;gap:6px;margin-top:4px}
.mlabels span{flex:1;text-align:center;font-size:9px;letter-spacing:1px;color:var(--dim)}
.mlabels span.cur{color:var(--cy);text-shadow:0 0 8px #46ff7d88}
/* heatmap */
table.heat{border-collapse:separate;border-spacing:4px;width:100%;display:block;overflow-x:auto}
table.heat th{font-size:9px;color:var(--dim);letter-spacing:1px;padding:2px 4px;text-align:center}
table.heat td.type{font-size:11px;color:var(--txt);text-align:left;white-space:nowrap;padding-right:8px}
table.heat td.cell{min-width:64px;height:30px;border-radius:5px;text-align:center;
  font-size:11px;color:#e2ffe9;border:1px solid #0006;text-shadow:0 1px 2px #000c}
table.heat td.cell.na{background:#081209;color:#3d6b4c;border-style:dashed}
.legend{display:flex;gap:14px;margin-top:8px;font-size:9px;color:var(--dim);align-items:center;flex-wrap:wrap}
.chip{width:14px;height:10px;border-radius:3px;display:inline-block;vertical-align:middle}
.deaths{font-size:11px;color:var(--dim)}
.deaths b{color:var(--am);font-size:16px}
/* charts */
.spk{width:100%;height:64px;display:block}
.spk.tall{height:150px}
.spkrow{margin-bottom:8px}
.spkhead{display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:2px}
.leg{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px;font-size:9px;color:var(--dim)}
.leg .chip{width:10px;height:10px}
/* ticker */
ul.tick{list-style:none;max-height:230px;overflow:hidden}
ul.tick li{font-size:10.5px;padding:4px 6px;border-bottom:1px dashed #14291b;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
ul.tick li .t{color:var(--dim)}
ul.tick li.kill{color:var(--rd)} ul.tick li.promo{color:var(--gr)} ul.tick li.spawn{color:var(--cy)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--rd);display:inline-block;
  margin-right:6px;animation:pu 1.2s infinite}
@keyframes pu{0%,100%{opacity:.25}50%{opacity:1}}
.ok .pulse{background:var(--gr);box-shadow:0 0 10px var(--gr)}
.status{font-size:11px;color:var(--dim)}
.status.stale{color:var(--rd)}
.foot{margin-top:12px;font-size:10px;color:#3d6b4c;text-align:center;letter-spacing:1px}
.wbline{font-size:11px;color:var(--dim);white-space:pre-line;line-height:1.7}
.wbline b{color:var(--am)}
/* Phase 9.13: plain-English explainer + XP bar */
.explain-big{font-size:17px;line-height:1.5;color:#e2ffe9;margin:2px 0 8px}
.explain-big b{color:var(--cy);text-shadow:0 0 10px #46ff7d44}
.explain-sub{font-size:11px;color:var(--dim);line-height:1.65}
.xpwrap{position:relative;height:20px;background:#081209;border:1px solid var(--line);
  border-radius:6px;margin-top:10px;overflow:hidden}
.xpbar{height:100%;width:0;background:linear-gradient(90deg,#0f8a3d,#46ff7d);
  box-shadow:0 0 14px #46ff7d66;transition:width .45s ease}
.xpbar.max{background:linear-gradient(90deg,#fbbf24,#f472b6);box-shadow:0 0 14px #fbbf2466}
.xptext{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:10px;color:#e2ffe9;text-shadow:0 1px 2px #000;letter-spacing:1px}
ul.tick li.demote{color:var(--am)}
.seg.cur{animation:segp 1.6s ease-in-out infinite}
@keyframes segp{0%,100%{box-shadow:0 0 6px #46ff7d44}50%{box-shadow:0 0 18px #46ff7dcc}}
/* Phase 9.16: hero ladder stepper, scoreboard, rank-up flash */
.ladder{display:flex;align-items:center;margin:14px 2px 2px}
.lnode{display:flex;flex-direction:column;align-items:center;gap:5px;flex:0 0 auto;width:88px}
.ldot{width:28px;height:28px;border-radius:50%;border:2px solid var(--line);background:#081209;
  display:flex;align-items:center;justify-content:center;font-size:11px;color:#3d6b4c;font-weight:700}
.lnode.done .ldot{border-color:var(--cy);color:var(--cy);box-shadow:0 0 10px #46ff7d44}
.lnode.cur .ldot{border-color:var(--cy);background:linear-gradient(180deg,#0f8a3d,#46ff7d);
  color:#032b12;animation:dotp 1.4s ease-in-out infinite}
.lname{font-size:9px;letter-spacing:1px;color:var(--dim);text-transform:uppercase;text-align:center}
.lnode.cur .lname{color:var(--cy);text-shadow:0 0 8px #46ff7d88}
.lnode.done .lname{color:#9fe8b0}
.lbar{flex:1;height:2px;background:var(--line);margin:0 -8px 18px;position:relative;overflow:hidden}
.lbar.done::after{content:"";position:absolute;inset:0;
  background:linear-gradient(90deg,#0f8a3d,#46ff7d);animation:fillx .8s ease}
@keyframes dotp{0%,100%{box-shadow:0 0 6px #46ff7d55}50%{box-shadow:0 0 24px #46ff7ddd}}
@keyframes fillx{from{width:0}to{width:100%}}
.sb{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sb .tile{background:#081209;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.sb .tile .v{font-size:20px;font-weight:700;color:#e2ffe9;line-height:1.1}
.sb .tile .l{font-size:9px;color:var(--dim);letter-spacing:.5px;margin-top:3px;line-height:1.4}
.sb .tile.hot{border-color:#fbbf2455}.sb .tile.hot .v{color:var(--am)}
.flashscreen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  pointer-events:none;opacity:0;z-index:50}
.flashscreen.go{animation:bigflash 1.9s ease}
.flashscreen .txt{font-size:46px;letter-spacing:8px;font-weight:700;color:var(--cy);
  text-shadow:0 0 30px #46ff7d,0 0 90px #46ff7d88}
.flashscreen .sub{font-size:16px;letter-spacing:3px;color:#9fe8b0;text-align:center;margin-top:8px}
@keyframes bigflash{0%{opacity:0}12%{opacity:1}70%{opacity:.85}100%{opacity:0}}
#hero.flash{animation:heroflash 1.9s ease}
@keyframes heroflash{0%,100%{box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}
  30%{box-shadow:0 0 0 2px #46ff7d inset,0 0 70px #46ff7d66}}
/* Phase 9.19: query-document panels + phosphor bar meters */
.docname{font-size:9px;letter-spacing:3px;color:var(--dim);margin:4px 2px 8px;text-transform:uppercase}
.dochead{display:flex;gap:8px;align-items:baseline;font-size:12px;padding:4px 2px 8px;border-bottom:1px solid var(--line);margin-bottom:10px;line-height:1.5}
.dochead .tag{color:var(--cy);font-weight:700;letter-spacing:1px;white-space:nowrap}
.dochead .q{color:#79d998}
.dochead .r{color:#3d6b4c;font-size:10px;white-space:nowrap}
.qrow{display:grid;grid-template-columns:120px 1fr 60px;gap:10px;align-items:center;padding:4px 2px;font-size:11px}
.qrow .qlabel{color:#7ddb95;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qrow .qbar{height:12px;background:#081209;border:1px solid #14301d;position:relative;overflow:hidden}
.qrow .qfill{height:100%;width:0;background:linear-gradient(90deg,#0f8a3d,#46ff7d);
  box-shadow:0 0 10px #46ff7d55;transition:width .5s ease}
.qrow .qval{text-align:right;color:#d9ffe4;font-size:10px}
.doc.fresh{animation:docglow 1.4s ease}
@keyframes docglow{0%{box-shadow:0 0 0 1px #46ff7d inset,0 0 26px #46ff7d33}
  100%{box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}}
.orderline{font-size:12px;color:#c9ffdb;padding:7px 10px;border:1px solid var(--line);
  background:#081209;margin-bottom:8px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.orderline .cur{width:9px;height:14px;background:var(--cy);animation:blink 1.1s steps(1) infinite;flex:0 0 auto}
@keyframes blink{50%{opacity:0}}
.director{font-size:10px;letter-spacing:1px;color:var(--dim);display:flex;gap:16px;
  padding:2px 2px 8px;border-bottom:1px dashed var(--line);margin-bottom:10px;flex-wrap:wrap}
.director b{color:#d9ffe4;font-weight:700}
/* Phase 9.18: "jev pulling the strings" — parameter-change feed */
#dfeed{max-height:330px;overflow:hidden}
#dfeed li{display:flex;gap:6px;align-items:baseline}
#dfeed li b{color:var(--am)}
#dfeed li i{color:var(--dim);font-style:normal;font-size:9px}
#dfeed li.empty{color:#3d6b4c}
#dfeed li.fresh{animation:dfin 1.6s ease}
@keyframes dfin{0%{background:#46ff7d33}100%{background:transparent}}
#dcount{color:var(--cy);letter-spacing:0;text-transform:none}
</style></head><body class="__QS__"><div class="wrap">
<div class="bar">
  <h1>Nemesis <span class="sub">// DOCUMENTS — every variable jev is pulling right now</span></h1>
  <div class="status" id="status"><span class="pulse"></span>connecting… · <span id="qsline"></span> · <a href="?compact=1" id="qstoggle" title="shrink the console to sit beside the game window (demo side-by-side)">side-by-side</a></div>
</div>
<div class="grid">
  <div class="panel c12 doc" id="hero" title="One sentence a normal person can read: what jev is doing this second, what rank he is, and what will move him up or down the ladder.">
    <div class="dochead"><span class="tag">[INTENT]</span><span class="q">What is jev doing right now?</span><span class="r" id="skpush" style="margin-left:auto"></span></div>
    <div class="orderline"><span>&gt; standing order: <span id="qsorder">awaiting first snapshot…</span></span><span class="cur"></span></div>
    <div class="director"><span>DIRECTOR <b id="dir_tic">–</b></span><span>SPANKED <b id="dir_hits">0</b></span><span>KILLED <b id="dir_kills">0</b></span><span>RANK <b id="dir_rank">–</b></span></div>
    <div class="explain-big" id="explain">waiting for the engine…</div>
    <div class="gauges" style="margin-top:10px">
      <div class="gauge" title="How many training episodes the nemesis squad has finished. One episode = jev spawns, fights, and dies (or you die). More episodes = more learning."><div class="v" id="ep">–</div><div class="l">EPISODE</div><div class="l" id="epsub">waiting for agent</div></div>
      <div class="gauge" title="Epsilon is the share of decisions jev makes at random. High = still experimenting. Low = using what it learned. It only goes down over time."><div class="v" id="eps">–</div><div class="l">EPSILON</div><div class="l" id="epssub">explore vs exploit</div></div>
      <div class="gauge" title="Average number of seconds the nemesis stays alive per episode. Should climb as it gets smarter."><div class="v" id="surv">–<span class="u"> s</span></div><div class="l">SURVIVAL</div><div class="l" id="rsub">last terminal: –</div></div>
      <div class="gauge" title="Your current health, and the weapon you are holding. In hostile training mode you spawn with a shotgun."><div class="v" id="hp">–</div><div class="l">PLAYER HP</div><div class="l" id="act">act: –</div></div>
    </div>
    <div class="ladder" id="ladder"></div>
    <div class="explain-sub" id="explain2" style="margin-top:10px">the training duel: jev hunts you, you rough him up — every 100 damage he absorbs promotes him one rank; if he kills you, he eases off one rank</div>
    <div class="xpwrap" title="XP = the damage YOU have dealt jev since his last rank-up. Fill the bar and he promotes."><div class="xpbar" id="xpbar"></div><span class="xptext" id="xptext"></span></div>
  </div>

  <div class="docname">DOCUMENTS — every variable jev is pulling, live</div>

  <div class="panel c6 doc" id="doc_fire" title="Shooting-pressure weights: focus_fire, chase and hold, averaged over the squad, plus how scared the nemesis is of your shotgun. Bars glide whenever jev re-tunes a weight.">
    <div class="dochead"><span class="tag">[FIRING]</span><span class="q">Should the trigger be held down right now?</span></div>
    <div class="qrow"><span class="qlabel">hold_fire</span><div class="qbar"><div class="qfill" id="qb_focus_fire"></div></div><span class="qval" id="qv_focus_fire">–</span></div>
    <div class="qrow"><span class="qlabel">press_chase</span><div class="qbar"><div class="qfill" id="qb_chase"></div></div><span class="qval" id="qv_chase">–</span></div>
    <div class="qrow"><span class="qlabel">hold_ground</span><div class="qbar"><div class="qfill" id="qb_hold"></div></div><span class="qval" id="qv_hold">–</span></div>
    <div class="qrow"><span class="qlabel">fear_shotgun</span><div class="qbar"><div class="qfill" id="qb_bias_sg"></div></div><span class="qval" id="qv_bias_sg">–</span></div>
    <div class="legend"><span class="deaths">weights learn from squad deaths — you have to take some down before the bars move</span></div>
  </div>

  <div class="panel c6 doc" id="doc_goal" title="The goal document: XP banked toward the next rank, the rank itself, epsilon (jev's randomness) and the session toll. Rank and XP move the instant you hit him or he dies.">
    <div class="dochead"><span class="tag">[GOAL]</span><span class="q">Considering player, enemies, and items, what is the highest-priority goal right now?</span></div>
    <div class="qrow"><span class="qlabel">xp_to_rank</span><div class="qbar"><div class="qfill" id="qb_xp"></div></div><span class="qval" id="qv_xp">–</span></div>
    <div class="qrow"><span class="qlabel">rank</span><div class="qbar"><div class="qfill" id="qb_rank"></div></div><span class="qval" id="qv_rank">–</span></div>
    <div class="qrow"><span class="qlabel">epsilon</span><div class="qbar"><div class="qfill" id="qb_eps"></div></div><span class="qval" id="qv_eps">–</span></div>
    <div class="qrow"><span class="qlabel">squad_deaths</span><div class="qbar"><div class="qfill" id="qb_deaths"></div></div><span class="qval" id="qv_deaths">–</span></div>
    <div class="qrow"><span class="qlabel">rank_ups</span><div class="qbar"><div class="qfill" id="qb_rankups"></div></div><span class="qval" id="qv_rankups">–</span></div>
  </div>

  <div class="panel c6 doc" id="doc_dodge" title="Evasion weights: how much the squad favors sidesteps, break-offs and door retreats. The question line re-writes itself from your health and jev's last action.">
    <div class="dochead"><span class="tag">[DODGE]</span><span class="q" id="dodgeq">The player's current top priority is unknown. What does this exact moment call for?</span></div>
    <div class="qrow"><span class="qlabel">dodge_left</span><div class="qbar"><div class="qfill" id="qb_fl"></div></div><span class="qval" id="qv_fl">–</span></div>
    <div class="qrow"><span class="qlabel">dodge_right</span><div class="qbar"><div class="qfill" id="qb_fr"></div></div><span class="qval" id="qv_fr">–</span></div>
    <div class="qrow"><span class="qlabel">back_off</span><div class="qbar"><div class="qfill" id="qb_fb"></div></div><span class="qval" id="qv_fb">–</span></div>
    <div class="qrow"><span class="qlabel">door_run</span><div class="qbar"><div class="qfill" id="qb_door"></div></div><span class="qval" id="qv_door">–</span></div>
    <div class="legend"><span class="deaths">1.00 = neutral. The dying tactic sheds weight; what kills them feeds fear_shotgun.</span></div>
  </div>

  <div class="panel c6 doc" id="doc_move" title="Movement weights plus the full per-type weight heatmap: green = the squad favors that tactic (it worked), red = it avoids that tactic (it got monsters killed).">
    <div class="dochead"><span class="tag">[MOVEMENT]</span><span class="q">Given the current situation, how should the player move right now?</span></div>
    <div class="qrow"><span class="qlabel">hunt</span><div class="qbar"><div class="qfill" id="qb_hunt"></div></div><span class="qval" id="qv_hunt">–</span></div>
    <div class="qrow"><span class="qlabel">ambush</span><div class="qbar"><div class="qfill" id="qb_amb"></div></div><span class="qval" id="qv_amb">–</span></div>
    <table class="heat" id="heat" style="margin-top:10px"></table>
    <div class="legend">
      <span><span class="chip" style="background:#f87171"></span> punished (&lt;1.0)</span>
      <span><span class="chip" style="background:#1c3a26"></span> neutral 1.0</span>
      <span><span class="chip" style="background:#34d399"></span> rewarded (&gt;1.0)</span>
      <span style="margin-left:auto" class="deaths" id="deaths"></span>
    </div>
    <div class="wbline" id="wbnote" style="margin-top:4px">hunt/ambush learn like every other weight: squad deaths push, your shotgun teaches fear</div>
    <div class="wbline" id="wbias" style="margin-top:8px">no learned bias yet — the nemesis has to die (or kill) first</div>
  </div>

  <div class="panel c12 doc" id="strings" title="Live feed of every variable jev changes: tactic weights and weapon bias (diffed every second), plus rank, XP and epsilon (checked 10x per second). This is the learning, listed line by line as it happens.">
    <div class="dochead"><span class="tag">[STRINGS]</span><span class="q">jev pulling the strings — every changed variable</span><span class="r" id="dcount" style="margin-left:auto">0 changes</span></div>
    <ul class="tick" id="dfeed"><li class="empty">no changes yet — fight jev and his numbers start moving</li></ul>
  </div>

  <div class="panel c8 doc" id="doc_drift" title="Each line is one tactic, averaged over the squad. When the line moves, jev just re-tuned that tactic from what killed his monsters — this is the learning, live.">
    <h2>Weight drift — how jev keeps retuning the tactics
      <span class="r" id="wdrift_r"></span></h2>
    <canvas class="spk tall" id="spk_w"></canvas>
    <div class="leg" id="wleg"></div>
  </div>
  <div class="panel c4 doc" id="doc_record" title="This session's scoreboard and the event transcript: what you and jev have done to each other since the dashboard connected.">
    <h2>Scoreboard <span class="r" id="sbtime"></span></h2>
    <div class="sb" id="sb"><div class="tile"><div class="v">–</div><div class="l">waiting for the engine…</div></div></div>
    <h2 style="margin-top:12px">Transcript</h2>
    <ul class="tick" id="tick"></ul>
  </div>

  <div class="panel c6" title="The agent's own numbers over the last ~15 minutes: chaos level, reward per episode, and how long the nemesis survives."><h2>Telemetry — last 15 min</h2>
    <div class="spkrow"><div class="spkhead"><span>epsilon</span><span id="spk_eps_v"></span></div>
      <canvas class="spk" id="spk_eps"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>episode terminal reward</span><span id="spk_r_v"></span></div>
      <canvas class="spk" id="spk_r"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>survival seconds / player hp</span><span id="spk_surv_v"></span></div>
      <canvas class="spk" id="spk_surv"></canvas></div>
  </div>
  <div class="panel c6 doc" title="jev's rank over time (step chart) — every step up is ~100 of your damage absorbed; every dip is a kill he scored on you."><h2>Rank over time <span class="r">the ladder, 1 Hz</span></h2>
    <canvas class="spk" id="spk_sk" style="height:80px"></canvas>
  </div>
  <div class="foot">PROJECT NEMESIS // phase 9.19 // no stat buffs — the weights are the whole story</div>
</div>
<script>
"use strict";
const ORDERS=__ORDERS__;
const LEVELS=__LEVELS__;
const OCOLORS=__OCOLORS__;
const WEAPONS=["fist","pistol","shotgun","chaingun","rocket","plasma","bfg","chainsaw","ssg"];
const NICE={zombie:"zombie man",shotgun:"shotgun guy",chaingun:"chaingunner",imp:"imp",
  pinky:"pinky",spectre:"spectre",lost:"lost soul",caco:"cacodemon",pain:"pain elemental",
  knight:"hell knight",baron:"baron of hell",revenant:"revenant",mancubus:"mancubus",
  arachnotron:"arachnotron"};
const ACT={chase:"hunting you down",hold:"holding position, watching you",
  fallback:"backing off to regroup",flank_left:"sneaking around your left",
  flank_right:"sneaking around your right",ambush:"lying in ambush",
  focus_fire:"standing his ground and firing",use_door:"working a door to reach you"};
const RANK_BLURB=["barely knows which end of the gun is which",
  "starting to aim before he shoots","fast reactions — he keeps his distance now",
  "punishes every mistake — do not miss","he has seen everything. Good luck."];
let hist=[];
let hasLiveFeed=false;   // /state carries a server-side param_feed?

const $=id=>document.getElementById(id);
// The status bar is rewritten every poll; this keeps the engine-clock slot
// and the side-by-side toggle alive across re-renders.
function statusHTML(msg){
  return '<span class="pulse"></span>'+msg+
    ' · <span id="qsline"></span> · <a href="?compact=1" id="qstoggle" '+
    'title="shrink the console to sit beside the game window (demo side-by-side)">side-by-side</a>';
}
function cellColor(v){
  if(v==null||isNaN(v))return null;
  const d=(v-1.0)/0.6;              // +-0.6 deviation saturates
  if(d>=0){const a=Math.min(1,d);return `rgba(52,211,153,${0.10+0.85*a})`;}
  const a=Math.min(1,-d);return `rgba(248,113,113,${0.10+0.85*a})`;
}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML;}
function fmt(x,p=1){return (x==null||isNaN(x))?"–":Number(x).toFixed(p);}

function drawHeat(rows){
  const t=$("heat");
  let head="<tr><th></th>"+ORDERS.map(o=>`<th>${o.replace('_',' ')}</th>`).join("")+"<th>deaths</th></tr>";
  let body="";
  let dsum="";
  for(const r of (rows||[])){
    body+=`<tr><td class="type">${esc(r.type)}</td>`;
    for(const o of ORDERS){
      const v=(r.tactics||{})[o];
      if(v==null){body+=`<td class="cell na">·</td>`;continue;}
      body+=`<td class="cell" style="background:${cellColor(v)}">${v.toFixed(2)}</td>`;
    }
    body+=`<td style="text-align:center;color:var(--am)">${r.deaths??0}</td></tr>`;
    const wb=r.weapon_bias||{};
    const wbs=Object.entries(wb).map(([k,v])=>`<b>${k}</b>:${v>0?'+':''}${v}`).join("  ");
    if(wbs)dsum+=`${esc(r.type)}  ${wbs}\n`;
  }
  t.innerHTML=head+body;
  $("deaths").textContent=rows&&rows.length?("total deaths: "+rows.reduce((a,r)=>a+(r.deaths||0),0)):"no rows yet";
  const wbEl=$("wbias");
  if(dsum){wbEl.innerHTML=dsum.trim();}
  else{wbEl.textContent="no learned bias yet — the nemesis has to die (or kill) first";}
}
function drawSkill(sk,prev){
  // Phase 9.16: hero ladder stepper (rookie -> legend) with connecting bars.
  const L=$("ladder");
  if(L.children.length!==LEVELS.length*2-1){
    L.innerHTML=LEVELS.map((n,i)=>
      (i?`<div class="lbar" id="lb${i}"></div>`:"")+
      `<div class="lnode" id="ln${i}"><div class="ldot">${i+1}</div><div class="lname">${n}</div></div>`).join("");
  }
  LEVELS.forEach((n,i)=>{
    const el=$("ln"+i);
    el.classList.toggle("done",i<sk);
    el.classList.toggle("cur",i===sk);
  });
  for(let i=1;i<LEVELS.length;i++)$("lb"+i).classList.toggle("done",i<=sk);
}
function drawScoreboard(st,secs){
  const el=$("sb");
  const t=(x)=>x==null?"–":x;
  if(!el.dataset.on){
    el.dataset.on=1;
    el.innerHTML=`
      <div class="tile hot" title="Monsters jev's squad lost this session (the squad dies, jev adapts)."><div class="v" id="sb_kills">0</div><div class="l">squad deaths</div></div>
      <div class="tile" title="Shots you landed on jev's squad."><div class="v" id="sb_hits">0</div><div class="l">hits you landed</div></div>
      <div class="tile" title="Total damage you dealt jev's squad this session."><div class="v" id="sb_dmg">0</div><div class="l">damage dealt</div></div>
      <div class="tile hot" title="Times jev climbed a rank this session."><div class="v" id="sb_rankups">0</div><div class="l">rank-ups</div></div>
      <div class="tile" title="Times jev eased off because he killed you."><div class="v" id="sb_eased">0</div><div class="l">eased off</div></div>
      <div class="tile" title="Times you died to jev this session."><div class="v" id="sb_pdeaths">0</div><div class="l">your deaths</div></div>`;
  }
  if(!st)return;
  $("sb_kills").textContent=t(st.kills);$("sb_hits").textContent=t(st.hits);
  $("sb_dmg").textContent=t(st.dmg);$("sb_rankups").textContent=t(st.rankups);
  $("sb_eased").textContent=t(st.eased);$("sb_pdeaths").textContent=t(st.pdeaths);
  const m=Math.floor((secs||0)/60),s=Math.round((secs||0)%60);
  $("sbtime").textContent=`session ${m}:${String(s).padStart(2,"0")}`;
}
// The rank-up flash: full-screen bloom + big words, once per promotion.
let flashUntil=0,flashRank=-1;
function rankFlash(rank,name){
  const now=Date.now();
  if(now<flashUntil||rank===flashRank)return;
  flashRank=rank;flashUntil=now+2100;
  let ov=$("flash");
  if(!ov){
    ov=document.createElement("div");ov.id="flash";ov.className="flashscreen";
    ov.innerHTML=`<div><div class="txt">RANK UP</div><div class="sub" id="flashsub"></div></div>`;
    document.body.appendChild(ov);
  }
  $("flashsub").textContent=`jev is now ${name.toUpperCase()}`;
  ov.classList.remove("go");void ov.offsetWidth;ov.classList.add("go");
  const hero=$("hero");hero.classList.remove("flash");void hero.offsetWidth;hero.classList.add("flash");
}
// ---- Phase 9.17: "jev pulling the strings" — the parameter-change feed ----
// One row per variable jev changed, newest first. tactic/bias/deaths come
// from the 1 Hz weight diffs (LiveState.param_deltas); rank/xp/epsilon are
// checked 10x/sec server-side. This is the learning, itemized.
function deltaLine(d){
  if(d.kind==="rank")return `<b>rank</b> ${LEVELS[d.old]??d.old} → <b>${LEVELS[d.new]??d.new}</b> <i>(${d.new>d.old?"promoted — your damage taught him":"eased off — he killed you"})</i>`;
  if(d.kind==="xp")return `<b>xp</b> ${d.old} → <b>${d.new}</b> <i>(damage banked toward his next rank)</i>`;
  if(d.kind==="eps")return `<b>epsilon</b> ${Number(d.old).toFixed(3)} → <b>${Number(d.new).toFixed(3)}</b> <i>(learning noise re-tuned)</i>`;
  const nm=d.type==="buddy"?"jev himself":(NICE[d.type]||d.type);
  if(d.kind==="tactic")return `<b>${nm}</b> tactic <b>${d.name}</b>: ${fmt(d.old,2)} → <b>${fmt(d.new,2)}</b> <i>(${d.new>d.old?"rewarded — that worked":"punished — that backfired"})</i>`;
  if(d.kind==="bias")return `<b>${nm}</b> fear of your <b>${d.name}</b>: ${d.old>0?"+":""}${d.old} → <b>${d.new>0?"+":""}${d.new}</b> <i>(weapon bias retuned)</i>`;
  if(d.kind==="deaths")return `<b>${nm}</b> deaths: ${d.old} → <b>${d.new}</b> <i>(the squad paid to learn that)</i>`;
  return esc(JSON.stringify(d));
}
let lastDeltaTop="";
function showDelta(list){
  const el=$("dfeed");if(!el)return;
  const arr=list||[],top=arr.slice(0,20);
  const dc=$("dcount");
  if(dc)dc.textContent=arr.length?(arr.length+(arr.length>=80?"+":" ")+" changes"):"0 changes";
  const key=top.map(d=>[d.kind,d.type||"",d.name||"",d.old,d.new].join("|")).join(";");
  if(key===lastDeltaTop)return;              // nothing new — keep the flash
  const fresh=key.split(";")[0]!==lastDeltaTop.split(";")[0];
  lastDeltaTop=key;
  if(fresh)flashDocs(top[0]);
  if(!top.length){el.innerHTML='<li class="empty">no changes yet — fight jev and his numbers start moving</li>';return;}
  el.innerHTML=top.map((d,i)=>
    `<li class="${esc(d.kind||"")}${i===0&&fresh?" fresh":""}"><span class="t">${new Date((d.ts||0)*1000).toLocaleTimeString()}</span>${deltaLine(d)}</li>`).join("");
}
// File-replay mode has no param_feed in /state — re-derive the same feed
// from the 1 Hz history rows (same diff LiveState does in RAM).
function paramDeltasFromRows(rows){
  const out=[];let prev=null;
  for(const r of (rows||[])){
    const wt=r.wt||{};
    if(prev){
      for(const tn of new Set([...Object.keys(prev),...Object.keys(wt)])){
        const p=prev[tn]||{},c=wt[tn]||{};
        const pt=p.tactics||{},ct=c.tactics||{};
        for(const k of new Set([...Object.keys(pt),...Object.keys(ct)]))
          if(k in pt&&k in ct&&pt[k]!==ct[k])
            out.push({ts:r.ts,kind:"tactic",type:tn,name:k,old:pt[k],new:ct[k]});
        const pb=p.weapon_bias||{},cb=c.weapon_bias||{};
        for(const k of new Set([...Object.keys(pb),...Object.keys(cb)]))
          if(k in pb&&k in cb&&pb[k]!==cb[k])
            out.push({ts:r.ts,kind:"bias",type:tn,name:k,old:pb[k],new:cb[k]});
        if(typeof p.deaths==="number"&&typeof c.deaths==="number"&&p.deaths!==c.deaths)
          out.push({ts:r.ts,kind:"deaths",type:tn,old:p.deaths,new:c.deaths});
      }
    }
    prev=wt;
  }
  return out.slice(-40).reverse();
}
function spark(canvas,vals,color,now,fill){
  const c=$(canvas);if(!c)return;
  const w=c.clientWidth||400,h=c.height;
  const ctx=c.getContext("2d");
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle="#14291b";ctx.strokeRect(0.5,0.5,w-1,h-1);
  if(!vals.length){ctx.fillStyle="#3d6b4c";ctx.font="10px monospace";
    ctx.fillText("no data yet",8,h/2);return;}
  const mn=Math.min(...vals),mx=Math.max(...vals),sp=(mx-mn)||1;
  ctx.beginPath();
  vals.forEach((v,i)=>{
    const x=(i/(vals.length-1||1))*(w-8)+4;
    const y=h-6-((v-mn)/sp)*(h-14);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);
  });
  if(fill){
    ctx.save();
    const lx=(vals.length-1)/(vals.length-1||1)*(w-8)+4;
    ctx.lineTo(lx,h-6);ctx.lineTo(4,h-6);ctx.closePath();
    const gr=ctx.createLinearGradient(0,0,0,h);
    gr.addColorStop(0,color+"55");gr.addColorStop(1,color+"00");
    ctx.fillStyle=gr;ctx.fill();ctx.restore();
    ctx.beginPath();
    vals.forEach((v,i)=>{
      const x=(i/(vals.length-1||1))*(w-8)+4;
      const y=h-6-((v-mn)/sp)*(h-14);
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);
    });
  }
  ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.shadowColor=color;ctx.shadowBlur=6;
  ctx.stroke();ctx.shadowBlur=0;
  const lx=(w-4),ly=h-6-((vals[vals.length-1]-mn)/sp)*(h-14);
  ctx.fillStyle=color;ctx.beginPath();ctx.arc(lx-4,ly,2.5,0,7);ctx.fill();
  if(now!=null)$(now).textContent=`min ${fmt(mn,2)} · max ${fmt(mx,2)} · now ${fmt(vals[vals.length-1],2)}`;
}
// The star of the show: one line per tactic order, mean across nemesis types,
// straight from the 1 Hz weight history — you literally watch the numbers move.
function drawWeightDrift(rows){
  const c=$("spk_w");if(!c)return;
  const w=c.clientWidth||600,h=c.height,ctx=c.getContext("2d");
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle="#14291b";ctx.strokeRect(0.5,0.5,w-1,h-1);
  // series: order -> [mean tactic weight per row that has any]
  const series={};ORDERS.forEach(o=>series[o]=[]);
  let count=0;
  for(const r of rows){
    const wt=r.wt||{};
    const types=Object.values(wt).filter(t=>t&&t.tactics&&Object.keys(t.tactics).length);
    if(!types.length)continue;
    count++;
    for(const o of ORDERS){
      let s=0,n=0;
      for(const t of types){const v=t.tactics[o];if(v!=null){s+=v;n++;}}
      series[o].push(n?s/n:null);
    }
  }
  if(!count){ctx.fillStyle="#3d6b4c";ctx.font="10px monospace";
    ctx.fillText("no weight changes yet — fight something (deaths/kill events move the weights)",8,h/2);
    $("wdrift_r").textContent="";return;}
  // baseline 1.0
  let mn=0.9,mx=1.1;
  for(const o of ORDERS)for(const v of series[o])if(v!=null){mn=Math.min(mn,v);mx=Math.max(mx,v);}
  const pad=0.05;mn=Math.max(0,mn-pad);mx=Math.min(2.2,mx+pad);
  const Y=v=>h-10-((v-mn)/(mx-mn||1))*(h-24);
  const X=i=>(i/(count-1||1))*(w-16)+8;
  ctx.setLineDash([3,4]);ctx.strokeStyle="#3d6b4c";
  ctx.beginPath();ctx.moveTo(6,Y(1.0));ctx.lineTo(w-6,Y(1.0));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="#3d6b4c";ctx.font="9px monospace";
  ctx.fillText("1.0 neutral",w-64,Y(1.0)-4);
  const leg=$("wleg");
  if(!leg.children.length){
    leg.innerHTML=ORDERS.map(o=>`<span><span class="chip" style="background:${OCOLORS[o]}"></span>${o}</span>`).join("");
  }
  for(const o of ORDERS){
    const vals=series[o];if(!vals.some(v=>v!=null))continue;
    ctx.beginPath();let started=false;
    vals.forEach((v,i)=>{
      if(v==null)return;
      const x=X(i),y=Y(v);
      started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true);
    });
    ctx.strokeStyle=OCOLORS[o];ctx.lineWidth=1.6;
    ctx.shadowColor=OCOLORS[o];ctx.shadowBlur=5;
    ctx.stroke();ctx.shadowBlur=0;
  }
  const last={};
  for(const o of ORDERS){const v=[...series[o]].reverse().find(v=>v!=null);if(v!=null)last[o]=v;}
  const movers=ORDERS.filter(o=>last[o]!=null&&Math.abs(last[o]-1)>0.02)
    .sort((a,b)=>Math.abs(last[b]-1)-Math.abs(last[a]-1)).slice(0,3)
    .map(o=>`${o} ${last[o].toFixed(2)}`);
  $("wdrift_r").textContent=movers.length?("biggest movers: "+movers.join(" · ")):
    "hovering at neutral";
}
function drawSkillHist(rows){
  const c=$("spk_sk");if(!c)return;
  const w=c.clientWidth||400,h=c.height,ctx=c.getContext("2d");
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle="#14291b";ctx.strokeRect(0.5,0.5,w-1,h-1);
  const vals=rows.map(r=>r.skill).filter(v=>typeof v==="number");
  if(!vals.length){ctx.fillStyle="#3d6b4c";ctx.font="10px monospace";
    ctx.fillText("no data yet",8,h/2);return;}
  const Y=v=>h-8-(v/(LEVELS.length-1))*(h-18);
  const X=i=>(i/(vals.length-1||1))*(w-12)+6;
  // step line, colored by current level
  ctx.beginPath();
  vals.forEach((v,i)=>{
    const x=X(i),y=Y(v);
    if(!i)ctx.moveTo(x,y);
    else{ctx.lineTo(X(i),Y(vals[i-1]));ctx.lineTo(x,y);}
  });
  const cur=vals[vals.length-1];
  ctx.strokeStyle=OCOLORS.chase;ctx.lineWidth=1.6;
  ctx.shadowColor=OCOLORS.chase;ctx.shadowBlur=5;ctx.stroke();ctx.shadowBlur=0;
  ctx.fillStyle="#4e8a63";ctx.font="9px monospace";
  for(let l=0;l<LEVELS.length;l++){
    ctx.fillText(LEVELS[l],8,Y(l)-2);
    ctx.fillStyle="#14291b";ctx.fillRect(74,Y(l),w-80,1);ctx.fillStyle="#4e8a63";
  }
  $("spk_sk").title=`now: rank ${cur} (${LEVELS[cur]||"?"})`;
}
// Translate an engine event label into a sentence a normal person reads.
// Labels look like "905:hit:shotgun:shotgun:21", "906:kill:imp:pistol",
// "907:rankup:buddy:2", "908:eased:buddy:1" (p_nemesis.c event ring).
function plainify(t){
  const m=/^(\\d+):(hit|kill|rankup|eased):([\\s\\S]+)$/.exec(t);
  if(!m)return t;
  const kind=m[2],rest=m[3].split(":");
  if(kind==="hit"&&rest.length>=3)
    return `you hit the ${NICE[rest[0]]||rest[0]} for ${rest[2]} (${rest[1]})`;
  if(kind==="kill"&&rest.length>=2)
    return `the ${NICE[rest[0]]||rest[0]} died to your ${rest[1]}`;
  if(kind==="rankup"){
    const l=parseInt(rest[rest.length-1]||"0",10);
    return `▲ JEV RANKED UP → ${LEVELS[l]||l} — he trained on the beating you gave him`;
  }
  if(kind==="eased"){
    const l=parseInt(rest[rest.length-1]||"0",10);
    return `▼ jev eased off → ${LEVELS[l]||l} — he killed you, so he is going easier on you`;
  }
  return t;
}
function classify(t){
  const s=String(t).toLowerCase();
  if(s.includes("rankup")||s.includes("promot"))return"promo";
  if(s.includes("eased")||s.includes("demot"))return"demote";
  if(s.includes("killed")&&s.includes("shotgunguy"))return"kill";
  if(s.includes("kill:"))return"kill";
  if(s.includes("spawn"))return"spawn";
  return"";
}
// ---- Phase 9.19: query-document bar meters ----
// One green bar per learned variable, redrawn from every /state poll:
// tactic weights and weapon bias (mean across nemesis types), rank, XP,
// epsilon and the session toll. .qfill carries a CSS width transition,
// so the bars glide whenever jev re-tunes a number.
function meanTactic(rows,name){
  // NEM_921_NEUTRAL: the engine omits weights within +-5% of neutral 1.0
  // (token economy), so a missing key means "still neutral 1.0", not "no
  // data". Only zero rows (no squad seen yet) is a true null ("--").
  let s=0,n=0;
  for(const r of rows||[]){const t=r.tactics||{};const v=(typeof t[name]==="number")?t[name]:1.0;s+=v;n++;}
  return n?s/n:null;
}
function meanBias(rows,name){
  // NEM_921_NEUTRAL: same rule for weapon bias — absent key = neutral 0.
  let s=0,n=0;
  for(const r of rows||[]){const t=r.weapon_bias||{};const v=(typeof t[name]==="number")?t[name]:0;s+=v;n++;}
  return n?s/n:null;
}
function setBar(bar,val,v,mn,mx,txt){
  const b=$(bar),t=$(val);if(!b)return;
  if(v==null||isNaN(v)){b.style.width="0%";if(t)t.textContent="–";return;}
  const p=Math.max(2,Math.min(100,100*(v-mn)/((mx-mn)||1)));
  b.style.width=p+"%";
  if(t)t.textContent=txt!=null?txt:Number(v).toFixed(2);
}
function renderBars(s){
  const rows=s.rows||[],st=s.stats||{};
  // [FIRING] — shooting pressure + fear of your shotgun.
  setBar("qb_focus_fire","qv_focus_fire",meanTactic(rows,"focus_fire"),0,2);
  setBar("qb_chase","qv_chase",meanTactic(rows,"chase"),0,2);
  setBar("qb_hold","qv_hold",meanTactic(rows,"hold"),0,2);
  setBar("qb_bias_sg","qv_bias_sg",meanBias(rows,"shotgun"),-1,1);
  // [GOAL] — the scalar strings jev pulls (checked 10x/sec server-side).
  const nxt=s.buddy_xp_next||100,xp=typeof s.buddy_xp==="number"?s.buddy_xp:0;
  setBar("qb_xp","qv_xp",xp,0,nxt,xp+"/"+nxt);
  const sk=typeof s.buddy_skill==="number"?s.buddy_skill:0;
  setBar("qb_rank","qv_rank",sk,0,LEVELS.length-1,LEVELS[sk]||sk);
  const eps=typeof s.epsilon==="number"?s.epsilon:0;
  setBar("qb_eps","qv_eps",eps,0,1,eps.toFixed(3));
  setBar("qb_deaths","qv_deaths",st.kills??0,0,15,String(st.kills??0));
  setBar("qb_rankups","qv_rankups",st.rankups??0,0,10,String(st.rankups??0));
  // [DODGE] — evasion weights.
  setBar("qb_fl","qv_fl",meanTactic(rows,"flank_left"),0,2);
  setBar("qb_fr","qv_fr",meanTactic(rows,"flank_right"),0,2);
  setBar("qb_fb","qv_fb",meanTactic(rows,"fallback"),0,2);
  setBar("qb_door","qv_door",meanTactic(rows,"use_door"),0,2);
  // [MOVEMENT] — approach weights.
  setBar("qb_hunt","qv_hunt",meanTactic(rows,"chase"),0,2);
  setBar("qb_amb","qv_amb",meanTactic(rows,"ambush"),0,2);
  // Terminal chrome: DIRECTOR row, standing order, dynamic dodge query.
  $("dir_tic").textContent=s.tic?("tic "+s.tic):"–";
  $("dir_hits").textContent=st.hits??0;
  $("dir_kills").textContent=st.kills??0;
  $("dir_rank").textContent=LEVELS[sk]||"–";
  const act=ACT[s.last_action]||"hunting you down";
  $("qsorder").textContent=act+" — rank "+(LEVELS[sk]||"?")+" · eps "+eps.toFixed(3)+" · hp "+(s.player_hp??"–");
  const hp=typeof s.player_hp==="number"?s.player_hp:100;
  const prio=hp<35?"trying to stay alive":hp<70?"looking for a fight he can win":"holding the line and fighting";
  $("dodgeq").textContent="The player's current top priority is "+prio+" (hp "+hp+"). What does this exact moment call for?";
}
// A changed variable glows the document it belongs to (param feed, newest first).
function flashDocs(d){
  if(!d)return;
  const id=d.kind==="bias"?"doc_fire":
    d.kind==="tactic"?(["flank_left","flank_right","fallback","use_door"].includes(d.name)?"doc_dodge":
      d.name==="ambush"?"doc_move":"doc_fire"):"doc_goal";
  const el=$(id);if(!el)return;
  el.classList.remove("fresh");void el.offsetWidth;el.classList.add("fresh");
}
// The plain-English headline: what jev is doing, his rank, and your gear.
function explain(s){
  const el=$("explain");
  if(!s.tic){el.textContent="waiting for the engine…";return;}
  const sk=typeof s.buddy_skill==="number"?s.buddy_skill:0;
  const rank=LEVELS[sk]||("level "+sk);
  const blurb=RANK_BLURB[sk]||"";
  const act=s.last_action?ACT[s.last_action]:null;
  const w=WEAPONS[s.player_weapon];
  const hp=s.player_hp;
  let line;
  if(act)line=`JEV is <b>${act}</b> — current rank: <b>${rank}</b> (${sk}/4), ${blurb}.`;
  else line=`JEV is hunting you — current rank: <b>${rank}</b> (${sk}/4), ${blurb}.`;
  let bits=[];
  if(w)bits.push(`you hold the <b>${w}</b>`);
  if(typeof hp==="number")bits.push(`your HP <b>${hp}</b>`);
  if(bits.length)line+="  "+bits.join(" · ")+".";
  el.innerHTML=line;
  // XP bar: damage YOU dealt jev since his last rank-up (the engine counts it).
  const xp=(typeof s.buddy_xp==="number")?s.buddy_xp:0;
  const nxt=(typeof s.buddy_xp_next==="number")?s.buddy_xp_next:100;
  const bar=$("xpbar"),txt=$("xptext");
  if(sk>=LEVELS.length-1){
    bar.style.width="100%";bar.classList.add("max");
    txt.textContent=`MAX RANK — ${LEVELS[LEVELS.length-1]}. Nothing left to teach him.`;
  }else{
    bar.style.width=Math.min(100,100*xp/nxt)+"%";bar.classList.remove("max");
    txt.textContent=`next rank in ${xp} / ${nxt} XP — XP is the damage you deal him`;
  }
  // The string-pull feed breathes with the panel: shorter in side-by-side mode.
  const df=$("dfeed");
  if(df)df.style.maxHeight=document.body.classList.contains("compact")?"216px":"330px";
}
function render(s){
  if(!s||!s.ts){$("status").innerHTML=statusHTML('waiting for first snapshot…');$("qsline").textContent="";return;}
  // Phase 9.19b: never pretend a frozen snapshot is live — the #1 way this
  // page "looks dead" is a stale --live dashboard that lost the engine.
  const age=(Date.now()/1000)-(s.ts||0);
  if(age>300){
    $("status").className="status";
    $("status").innerHTML=statusHTML('file snapshot from '+new Date(s.ts*1000).toLocaleTimeString()+' — start the game for live data');
  } else if(age>3){
    $("status").className="status stale";
    $("status").innerHTML=statusHTML('STALE — engine data stopped '+Math.round(age)+'s ago (is the game running?)');
  } else {
    $("status").className="status ok";
    $("status").innerHTML=statusHTML('live · tic '+s.tic+' · '+new Date().toLocaleTimeString());
  }
  $("qsline").textContent=s.tic?("engine clock "+(s.tic/35).toFixed(0)+"s"):"no engine";
  $("ep").textContent=s.episode_label;
  $("epsub").textContent=`completed episodes: ${s.episode} · alive: ${s.alive?"yes":"no"}`;
  $("eps").textContent=fmt(s.epsilon,3);
  const e=s.epsilon;
  $("epssub").textContent=e>0.5?"pure exploration (random)":e>0.15?"still curious":"exploiting what it learned";
  $("surv").innerHTML=fmt(s.surv_avg,1)+'<span class="u"> s</span>';
  $("rsub").textContent=`last terminal: ${s.last_terminal_r>=0?"+":""}${fmt(s.last_terminal_r,1)}`;
  $("rsub").style.color=s.last_terminal_r>=0?"var(--gr)":"var(--rd)";
  $("hp").textContent=s.player_hp??"–";
  $("hp").className="big "+((s.player_hp??100)>60?"ok":(s.player_hp??100)>25?"warn":"bad");
  const w=WEAPONS[s.player_weapon];
  $("act").textContent=(w?"you: "+w+" · ":"")+"act: "+(s.last_action??"–");
  if(typeof s.buddy_skill==="number")drawSkill(s.buddy_skill);
  $("skpush").textContent=s.pushes?`${s.pushes} curriculum pushes`:"";
  explain(s);
  renderBars(s);
  drawScoreboard(s.stats,s.session_secs);
  // Only snapshots that actually carry the server-side feed own the panel;
  // older file-replay snapshots (no param_feed key) must not clobber the
  // client-side fallback derived from history rows.
  if(Array.isArray(s.param_feed)){showDelta(s.param_feed);hasLiveFeed=true;}
  scanRankups(s.events);
  drawHeat(s.rows||[]);
  const tick=$("tick");
  const evs=[...(s.agent_events||[]).map(x=>[x.ts,x.text]),
             ...(s.events||[]).map(x=>[s.ts,x])].slice(-24);
  tick.innerHTML=evs.slice().reverse().map(([ts,tx])=>
    `<li class="${classify(tx)}"><span class="t">${new Date(ts*1000).toLocaleTimeString()} </span>${esc(plainify(tx))}</li>`).join("")
    ||'<li><span class="t">no events yet…</span></li>';
}
function renderHist(rows){
  hist=rows||[];
  renderHist._n=(renderHist._n||0)+1;
  if(!hasLiveFeed&&renderHist._n%5===1)showDelta(paramDeltasFromRows(hist));
  spark("spk_eps",hist.map(r=>r.eps),"#46ff7d","spk_eps_v",true);
  spark("spk_r",hist.map(r=>r.r),"#34d399","spk_r_v",true);
  spark("spk_surv",hist.map(r=>r.surv??r.hp),"#fbbf24","spk_surv_v",true);
  drawWeightDrift(hist);
  drawSkillHist(hist);
}
async function poll(){
  try{
    const s=await (await fetch("/state")).json();
    render(s);
  }catch(e){$("status").innerHTML=statusHTML('agent link down — retrying');}
}
async function pollHist(){
  try{const h=await (await fetch("/history")).json();renderHist(h.rows||[]);}
  catch(e){}
}
// Rank-up detection from the event ring (fires the flash + why-line).
function scanRankups(events){
  for(const ev of (events||[])){
    const s=String(ev);
    const m=/rankup:buddy:(\\d+)/.exec(s);
    if(m){
      const l=parseInt(m[1],10);
      rankFlash(l,LEVELS[l]||("level "+l));
    }
    // An ease-off resets the dedupe so re-earning the same rank flashes again.
    if(/eased:buddy:/.test(s))flashRank=-1;
  }
}
// Phase 9.18: side-by-side demo mode. ?compact=1 (or the status-bar link)
// squeezes the whole console into a single ~660 px column — the footprint of
// the game's default 640x400 window — so both fit side by side on one screen.
const QSCSS=`
  body.compact{padding:8px}
  body.compact .wrap{max-width:660px}
  body.compact .grid{gap:8px}
  body.compact .panel{padding:8px;border-radius:8px}
  body.compact .bar{margin-bottom:8px}
  body.compact h1{font-size:11px;letter-spacing:1px}
  body.compact .c3,body.compact .c4,body.compact .c5,body.compact .c6,
  body.compact .c7,body.compact .c8{grid-column:span 12}
  body.compact .big{font-size:22px}
  body.compact .gauge{min-width:88px;padding:6px 4px}
  body.compact .gauge .v{font-size:16px}
  body.compact .explain-big{font-size:13px}
  body.compact .explain-sub{font-size:10px}
  body.compact .lnode{width:52px}
  body.compact .ldot{width:20px;height:20px;font-size:9px}
  body.compact .lname{font-size:7px}
  body.compact .sb{grid-template-columns:1fr 1fr 1fr;gap:6px}
  body.compact .sb .tile{padding:6px 8px}
  body.compact .sb .tile .v{font-size:16px}
  body.compact .spk{height:44px}
  body.compact .spk.tall{height:96px}
  body.compact #spk_sk{height:60px!important}
  body.compact table.heat td.cell{min-width:40px;height:24px;font-size:9px}
  body.compact table.heat td.type{font-size:9px}
  body.compact ul.tick{max-height:110px}
  body.compact #dfeed{max-height:216px}
  body.compact .foot{display:none}
  body.compact .qrow{grid-template-columns:96px 1fr 54px;font-size:10px;gap:6px;padding:3px 0}
  body.compact .qbar{height:9px}
  body.compact .docname{margin:8px 0 4px}
  body.compact .dochead{font-size:11px;padding:2px 2px 6px;margin-bottom:8px}
  body.compact .orderline{font-size:11px;padding:5px 8px}
  body.compact .director{gap:10px;font-size:9px}
  body.compact .flashscreen .txt{font-size:28px;letter-spacing:5px}
  body.compact .flashscreen .sub{font-size:12px}
`;
if(new URLSearchParams(location.search).get("compact")==="1"){
  document.body.classList.add("compact");
  const st=document.createElement("style");st.textContent=QSCSS;document.head.appendChild(st);
}
document.addEventListener("click",e=>{
  const a=e.target.closest("#qstoggle");
  if(!a)return;
  e.preventDefault();
  const u=new URL(location.href);
  u.searchParams.set("compact",document.body.classList.contains("compact")?"0":"1");
  location.href=u;
});
poll();pollHist();scanRankups([]);
setInterval(poll,250);setInterval(pollHist,1000);
</script></body></html>"""


class DashboardState:
    """Shared state the HTTP handlers read from (RAM snapshot or disk)."""

    def __init__(self, live=None) -> None:
        self.live = live  # nemesis.livestate.LiveState or None

    def state_json(self) -> dict:
        if self.live is not None and self.live.snapshot:
            return self.live.snapshot
        try:
            with open(config.LIVE_STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def history_rows(self) -> list:
        if self.live is not None:
            return list(self.live.history)
        rows = []
        try:
            with open(config.STATE_HISTORY_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        return rows[-HISTORY_TAIL:]


def make_handler(state: DashboardState):
    page = (_PAGE
            .replace("__ORDERS__", json.dumps(ORDER_KEYS))
            .replace("__LEVELS__", json.dumps(list(config.SKILL_LEVELS)))
            .replace("__OCOLORS__", json.dumps(ORDER_COLORS))
            .replace("__QS__", ""))  # body class; ?compact=1 adds it client-side

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/state":
                body = json.dumps(state.state_json()).encode("utf-8")
                self._send(200, body, "application/json")
            elif self.path == "/history":
                body = json.dumps({"rows": state.history_rows()}).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def log_message(self, fmt, *args) -> None:  # silence request spam
            pass

    return Handler


class _Server(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, and on Windows that lets a SECOND server
    # silently bind the SAME port - incoming connections then go to an
    # arbitrary listener, which is exactly how a stale --live dashboard
    # "shadows" a fresh one and the browser shows a frozen page while you
    # play.  Disable reuse on nt so a double-bind fails loudly instead.
    allow_reuse_address = os.name != "nt"


def start(live=None, port: int | None = None):
    """Start the dashboard server. Returns (httpd, url)."""
    state = DashboardState(live)
    httpd = _Server(
        ("127.0.0.1", config.DASHBOARD_PORT if port is None else port),
        make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class _LiveShim:
    """Observe-only stand-in for LiveLearner so --live can feed LiveState
    without an agent (no orders, no spawns, no Q updates — just watching)."""

    class _Q:
        episode = 0

        @staticmethod
        def epsilon():
            return config.EPSILON_START

    def __init__(self) -> None:
        self.q = self._Q()
        self.epsilon_override = None
        self.alive = False
        self.rewards_log: list[float] = []
        self.prev_hp = None
        self.last_action_name = None

    def episode_label(self) -> int:
        return self.q.episode

    def _surv_avg(self) -> float:
        return 0.0


def live_loop(live, engine_port=None, max_seconds: float | None = None) -> None:
    """--live: connect to the engine as the ONE client, poll observe, feed
    the dashboard.  Watch-only: nothing is ever sent but `observe`."""
    from .engine import DirectorLink
    from .curriculum import SkillCurriculum

    shim = _LiveShim()
    curriculum = SkillCurriculum(DirectorLink(port=engine_port or config.DIRECTOR_PORT))
    link = curriculum.link
    deadline = time.monotonic() + max_seconds if max_seconds else None
    print(f"[dashboard] live mode: watching the engine on :{link.port} "
          "(observe-only; the agent must NOT be running)")
    while deadline is None or time.monotonic() < deadline:
        if not link.ensure_connected():
            time.sleep(config.RECONNECT_DELAY)
            continue
        obs = link.observe()
        if obs is None or obs.get("nolevel"):
            time.sleep(config.POLL_PERIOD)
            continue
        curriculum.reconcile(obs.get("nemesis", {}).get("buddy_skill"))
        live.update(shim, obs, curriculum)
        time.sleep(config.POLL_PERIOD)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Nemesis dashboard (standalone: reads files, or --live to watch the engine)")
    ap.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    ap.add_argument("--live", action="store_true",
                    help="connect to the engine and stream live snapshots "
                         "(observe-only; exclusive with the agent)")
    ap.add_argument("--max-seconds", type=float, default=None)
    args = ap.parse_args()

    if args.live:
        from .livestate import LiveState
        live = LiveState()
        try:
            httpd, url = start(live=live, port=args.port)
        except OSError as e:
            print(f"[dashboard] cannot bind port {args.port}: {e}\n"
                  "[dashboard] another dashboard is probably running - "
                  "close it first:  Nemesis.bat kill")
            return 1
        print(f"[dashboard] serving {url}")
        try:
            live_loop(live, max_seconds=args.max_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
        return 0

    try:
        httpd, url = start(live=None, port=args.port)
    except OSError as e:
        print(f"[dashboard] cannot bind port {args.port}: {e}\n"
              "[dashboard] another dashboard is probably running - "
              "close it first:  Nemesis.bat kill")
        return 1
    print(f"[dashboard] serving {url}  (reads {config.LIVE_STATE_PATH} + history)")
    print("[dashboard] Ctrl+C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

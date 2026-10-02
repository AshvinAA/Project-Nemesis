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
    "chase": "#22d3ee", "hold": "#34d399", "fallback": "#f87171",
    "flank_left": "#fbbf24", "flank_right": "#fb923c", "ambush": "#a78bfa",
    "focus_fire": "#f472b6", "use_door": "#94a3b8",
}

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEMESIS — weight evolution console</title>
<style>
:root{
  --bg:#07090f; --panel:#0e1420; --panel2:#131b2b; --line:#1d2940;
  --txt:#c9d6e8; --dim:#5c6f8f; --cy:#22d3ee; --gr:#34d399; --rd:#f87171;
  --am:#fbbf24; --mono:ui-monospace,'Cascadia Mono',Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1200px 600px at 70% -10%,#0d1a2e 0%,var(--bg) 60%);
  color:var(--txt);font-family:var(--mono);min-height:100vh;padding:18px}
a{color:var(--cy)}
h1{font-size:15px;letter-spacing:3px;color:var(--cy);text-transform:uppercase;
  text-shadow:0 0 18px #22d3ee44}
h1 .sub{color:var(--dim);letter-spacing:1px;text-transform:none;font-size:11px}
.wrap{max-width:1180px;margin:0 auto}
.bar{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:10px;padding:12px;position:relative;
  box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}
.panel h2{font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  margin-bottom:10px;display:flex;justify-content:space-between}
.panel h2 .r{color:#31415e;letter-spacing:0;text-transform:none}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}
.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
.big{font-size:30px;font-weight:700;color:#eaf4ff;line-height:1.1}
.big .u{font-size:12px;color:var(--dim)}
.gauges{display:flex;gap:10px;flex-wrap:wrap}
.gauge{flex:1;min-width:110px;text-align:center;background:#0a101c;border:1px solid var(--line);
  border-radius:8px;padding:10px 6px}
.gauge .v{font-size:22px;font-weight:700;color:#eaf4ff}
.gauge .l{font-size:9px;color:var(--dim);letter-spacing:1px;margin-top:3px}
.gauge.ok .v{color:var(--gr)}.gauge.warn .v{color:var(--am)}.gauge.bad .v{color:var(--rd)}
/* skill meter */
.meter{display:flex;gap:6px;margin-top:6px}
.seg{flex:1;height:16px;border-radius:4px;background:#0a101c;border:1px solid var(--line);
  position:relative;overflow:hidden}
.seg.on{background:linear-gradient(90deg,#0ea5b7,#22d3ee);border-color:#22d3ee;
  box-shadow:0 0 12px #22d3ee66}
.mlabels{display:flex;gap:6px;margin-top:4px}
.mlabels span{flex:1;text-align:center;font-size:9px;letter-spacing:1px;color:var(--dim)}
.mlabels span.cur{color:var(--cy);text-shadow:0 0 8px #22d3ee88}
/* heatmap */
table.heat{border-collapse:separate;border-spacing:4px;width:100%;display:block;overflow-x:auto}
table.heat th{font-size:9px;color:var(--dim);letter-spacing:1px;padding:2px 4px;text-align:center}
table.heat td.type{font-size:11px;color:var(--txt);text-align:left;white-space:nowrap;padding-right:8px}
table.heat td.cell{min-width:64px;height:30px;border-radius:5px;text-align:center;
  font-size:11px;color:#eaf4ff;border:1px solid #0006;text-shadow:0 1px 2px #000c}
table.heat td.cell.na{background:#0a101c;color:#31415e;border-style:dashed}
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
ul.tick li{font-size:10.5px;padding:4px 6px;border-bottom:1px dashed #16223a;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
ul.tick li .t{color:var(--dim)}
ul.tick li.kill{color:var(--rd)} ul.tick li.promo{color:var(--gr)} ul.tick li.spawn{color:var(--cy)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--rd);display:inline-block;
  margin-right:6px;animation:pu 1.2s infinite}
@keyframes pu{0%,100%{opacity:.25}50%{opacity:1}}
.ok .pulse{background:var(--gr);box-shadow:0 0 10px var(--gr)}
.status{font-size:11px;color:var(--dim)}
.foot{margin-top:12px;font-size:10px;color:#31415e;text-align:center;letter-spacing:1px}
.wbline{font-size:11px;color:var(--dim);white-space:pre-line;line-height:1.7}
.wbline b{color:var(--am)}
/* Phase 9.13: plain-English explainer + XP bar */
.explain-big{font-size:17px;line-height:1.5;color:#eaf4ff;margin:2px 0 8px}
.explain-big b{color:var(--cy);text-shadow:0 0 10px #22d3ee44}
.explain-sub{font-size:11px;color:var(--dim);line-height:1.65}
.xpwrap{position:relative;height:20px;background:#0a101c;border:1px solid var(--line);
  border-radius:6px;margin-top:10px;overflow:hidden}
.xpbar{height:100%;width:0;background:linear-gradient(90deg,#0ea5b7,#22d3ee);
  box-shadow:0 0 14px #22d3ee66;transition:width .45s ease}
.xpbar.max{background:linear-gradient(90deg,#fbbf24,#f472b6);box-shadow:0 0 14px #fbbf2466}
.xptext{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:10px;color:#eaf4ff;text-shadow:0 1px 2px #000;letter-spacing:1px}
ul.tick li.demote{color:var(--am)}
.seg.cur{animation:segp 1.6s ease-in-out infinite}
@keyframes segp{0%,100%{box-shadow:0 0 6px #22d3ee44}50%{box-shadow:0 0 18px #22d3eecc}}
/* Phase 9.16: hero ladder stepper, scoreboard, rank-up flash */
.ladder{display:flex;align-items:center;margin:14px 2px 2px}
.lnode{display:flex;flex-direction:column;align-items:center;gap:5px;flex:0 0 auto;width:88px}
.ldot{width:28px;height:28px;border-radius:50%;border:2px solid var(--line);background:#0a101c;
  display:flex;align-items:center;justify-content:center;font-size:11px;color:#31415e;font-weight:700}
.lnode.done .ldot{border-color:var(--cy);color:var(--cy);box-shadow:0 0 10px #22d3ee44}
.lnode.cur .ldot{border-color:var(--cy);background:linear-gradient(180deg,#0ea5b7,#22d3ee);
  color:#03202b;animation:dotp 1.4s ease-in-out infinite}
.lname{font-size:9px;letter-spacing:1px;color:var(--dim);text-transform:uppercase;text-align:center}
.lnode.cur .lname{color:var(--cy);text-shadow:0 0 8px #22d3ee88}
.lnode.done .lname{color:#8fd8e8}
.lbar{flex:1;height:2px;background:var(--line);margin:0 -8px 18px;position:relative;overflow:hidden}
.lbar.done::after{content:"";position:absolute;inset:0;
  background:linear-gradient(90deg,#0ea5b7,#22d3ee);animation:fillx .8s ease}
@keyframes dotp{0%,100%{box-shadow:0 0 6px #22d3ee55}50%{box-shadow:0 0 24px #22d3eedd}}
@keyframes fillx{from{width:0}to{width:100%}}
.sb{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sb .tile{background:#0a101c;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.sb .tile .v{font-size:20px;font-weight:700;color:#eaf4ff;line-height:1.1}
.sb .tile .l{font-size:9px;color:var(--dim);letter-spacing:.5px;margin-top:3px;line-height:1.4}
.sb .tile.hot{border-color:#fbbf2455}.sb .tile.hot .v{color:var(--am)}
.flashscreen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  pointer-events:none;opacity:0;z-index:50}
.flashscreen.go{animation:bigflash 1.9s ease}
.flashscreen .txt{font-size:46px;letter-spacing:8px;font-weight:700;color:var(--cy);
  text-shadow:0 0 30px #22d3ee,0 0 90px #22d3ee88}
.flashscreen .sub{font-size:16px;letter-spacing:3px;color:#8fd8e8;text-align:center;margin-top:8px}
@keyframes bigflash{0%{opacity:0}12%{opacity:1}70%{opacity:.85}100%{opacity:0}}
#hero.flash{animation:heroflash 1.9s ease}
@keyframes heroflash{0%,100%{box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}
  30%{box-shadow:0 0 0 2px #22d3ee inset,0 0 70px #22d3ee66}}
</style></head><body><div class="wrap">
<div class="bar">
  <h1>Nemesis <span class="sub">// weight-evolution console — watching the jev learn</span></h1>
  <div class="status" id="status"><span class="pulse"></span>connecting…</div>
</div>
<div class="grid">
  <div class="panel c3" title="How many training episodes the nemesis squad has finished. One episode = jev spawns, fights, and dies (or you die). More episodes = more learning."><h2>Episode</h2><div class="big" id="ep">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="epsub">waiting for agent</div></div>
  <div class="panel c3" title="Epsilon is the share of decisions jev makes at random. High = still experimenting. Low = using what it learned. It only goes down over time."><h2>Epsilon (chaos)</h2><div class="big" id="eps">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="epssub">explore vs exploit</div></div>
  <div class="panel c3" title="Average number of seconds the nemesis stays alive per episode. Should climb as it gets smarter."><h2>Survival avg</h2><div class="big" id="surv">–<span class="u"> s</span></div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="rsub">last terminal: –</div></div>
  <div class="panel c3" title="Your current health, and the weapon you are holding. In hostile training mode you spawn with a shotgun."><h2>Player HP</h2><div class="big" id="hp">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="act">act: –</div></div>

  <div class="panel c12" id="hero" title="One sentence a normal person can read: what jev is doing this second, what rank he is, and what will move him up or down the ladder.">
    <h2>What is jev doing right now? <span class="r" id="skpush"></span></h2>
    <div class="explain-big" id="explain">waiting for the engine…</div>
    <div class="ladder" id="ladder"></div>
    <div class="explain-sub" id="explain2" style="margin-top:10px">the training duel: jev hunts you, you rough him up — every 100 damage he absorbs promotes him one rank; if he kills you, he eases off one rank</div>
    <div class="xpwrap" title="XP = the damage YOU have dealt jev since his last rank-up. Fill the bar and he promotes."><div class="xpbar" id="xpbar"></div><span class="xptext" id="xptext"></span></div>
  </div>

  <div class="panel c4" title="This session's scoreboard: what you and jev have done to each other since the dashboard connected."><h2>Scoreboard <span class="r" id="sbtime"></span></h2>
    <div class="sb" id="sb"><div class="tile"><div class="v">–</div><div class="l">waiting for the engine…</div></div></div>
  </div>
  <div class="panel c8" title="What jev's squad has learned, per monster type: green = the squad favors that tactic (it worked), red = it avoids that tactic (it got monsters killed). Numbers are multipliers on the tactic's base chance."><h2>Learned weight heatmap — nemesis rows</h2>
    <table class="heat" id="heat"></table>
    <div class="legend">
      <span><span class="chip" style="background:#f87171"></span> punished (&lt;1.0)</span>
      <span><span class="chip" style="background:#1d2940"></span> neutral 1.0</span>
      <span><span class="chip" style="background:#34d399"></span> rewarded (&gt;1.0)</span>
      <span style="margin-left:auto" class="deaths" id="deaths"></span>
    </div>
  </div>

  <div class="panel c8" title="Each line is one tactic, averaged over the squad. When the line moves, jev just re-tuned that tactic from what killed his monsters — this is the learning, live."><h2>Weight drift — how jev keeps retuning the tactics
      <span class="r" id="wdrift_r"></span></h2>
    <canvas class="spk tall" id="spk_w"></canvas>
    <div class="leg" id="wleg"></div>
  </div>
  <div class="panel c4" title="Everything that just happened, translated to plain English: kills, hits, and jev's rank changes."><h2>Event ticker</h2><ul class="tick" id="tick"></ul></div>

  <div class="panel c6" title="The agent's own numbers over the last ~15 minutes: chaos level, reward per episode, and how long the nemesis survives."><h2>Telemetry — last 15 min</h2>
    <div class="spkrow"><div class="spkhead"><span>epsilon</span><span id="spk_eps_v"></span></div>
      <canvas class="spk" id="spk_eps"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>episode terminal reward</span><span id="spk_r_v"></span></div>
      <canvas class="spk" id="spk_r"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>survival seconds / player hp</span><span id="spk_surv_v"></span></div>
      <canvas class="spk" id="spk_surv"></canvas></div>
  </div>
  <div class="panel c6" title="jev's rank over time (step chart) — every step up is ~100 of your damage absorbed; every dip is a kill he scored on you."><h2>Rank over time <span class="r">the ladder, 1 Hz</span></h2>
    <canvas class="spk" id="spk_sk" style="height:80px"></canvas>
    <h2 style="margin-top:12px" title="Which of YOUR weapons the squad has learned to fear (positive) or shrug off (negative), per monster type.">Weapon bias per type</h2>
    <div class="wbline" id="wbias">no learned bias yet — the nemesis has to die (or kill) first</div>
  </div>
  <div class="foot">PROJECT NEMESIS // phase 9 // no stat buffs — the weights are the whole story</div>
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

const $=id=>document.getElementById(id);
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
function spark(canvas,vals,color,now,fill){
  const c=$(canvas);if(!c)return;
  const w=c.clientWidth||400,h=c.height;
  const ctx=c.getContext("2d");
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle="#16223a";ctx.strokeRect(0.5,0.5,w-1,h-1);
  if(!vals.length){ctx.fillStyle="#31415e";ctx.font="10px monospace";
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
  ctx.strokeStyle="#16223a";ctx.strokeRect(0.5,0.5,w-1,h-1);
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
  if(!count){ctx.fillStyle="#31415e";ctx.font="10px monospace";
    ctx.fillText("no weight changes yet — fight something (deaths/kill events move the weights)",8,h/2);
    $("wdrift_r").textContent="";return;}
  // baseline 1.0
  let mn=0.9,mx=1.1;
  for(const o of ORDERS)for(const v of series[o])if(v!=null){mn=Math.min(mn,v);mx=Math.max(mx,v);}
  const pad=0.05;mn=Math.max(0,mn-pad);mx=Math.min(2.2,mx+pad);
  const Y=v=>h-10-((v-mn)/(mx-mn||1))*(h-24);
  const X=i=>(i/(count-1||1))*(w-16)+8;
  ctx.setLineDash([3,4]);ctx.strokeStyle="#31415e";
  ctx.beginPath();ctx.moveTo(6,Y(1.0));ctx.lineTo(w-6,Y(1.0));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="#31415e";ctx.font="9px monospace";
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
  ctx.strokeStyle="#16223a";ctx.strokeRect(0.5,0.5,w-1,h-1);
  const vals=rows.map(r=>r.skill).filter(v=>typeof v==="number");
  if(!vals.length){ctx.fillStyle="#31415e";ctx.font="10px monospace";
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
  ctx.fillStyle="#5c6f8f";ctx.font="9px monospace";
  for(let l=0;l<LEVELS.length;l++){
    ctx.fillText(LEVELS[l],8,Y(l)-2);
    ctx.fillStyle="#16223a";ctx.fillRect(74,Y(l),w-80,1);ctx.fillStyle="#5c6f8f";
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
}
function render(s){
  if(!s||!s.ts){$("status").innerHTML='<span class="pulse"></span>waiting for first snapshot…';return;}
  $("status").className="status ok";
  $("status").innerHTML='<span class="pulse"></span>live · tic '+s.tic+' · '+new Date().toLocaleTimeString();
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
  drawScoreboard(s.stats,s.session_secs);
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
  spark("spk_eps",hist.map(r=>r.eps),"#22d3ee","spk_eps_v",true);
  spark("spk_r",hist.map(r=>r.r),"#34d399","spk_r_v",true);
  spark("spk_surv",hist.map(r=>r.surv??r.hp),"#fbbf24","spk_surv_v",true);
  drawWeightDrift(hist);
  drawSkillHist(hist);
}
async function poll(){
  try{
    const s=await (await fetch("/state")).json();
    render(s);
  }catch(e){$("status").innerHTML='<span class="pulse"></span>agent link down — retrying';}
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
            .replace("__OCOLORS__", json.dumps(ORDER_COLORS)))

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


def start(live=None, port: int | None = None):
    """Start the dashboard server. Returns (httpd, url)."""
    state = DashboardState(live)
    httpd = ThreadingHTTPServer(
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
        httpd, url = start(live=live, port=args.port)
        print(f"[dashboard] serving {url}")
        try:
            live_loop(live, max_seconds=args.max_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
        return 0

    httpd, url = start(live=None, port=args.port)
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

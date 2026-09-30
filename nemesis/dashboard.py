"""Phase 9: the weight-evolution dashboard ("watch jev get smart").

Zero dependencies: stdlib http.server + one embedded HTML page (dark theme,
vanilla JS).  Endpoints:
    /         -> the console
    /state    -> live snapshot (same JSON the agent writes to live_state.json)
    /history  -> tail of the 1 Hz history rows (sparkline fuel)

Runs two ways:
  * wired into the agent  — rl_agent.run() starts it with the in-RAM
    LiveState, so it always serves the freshest snapshot;
  * standalone            — `python -m nemesis.dashboard` reads the files off
    disk (useful to inspect a past session while the agent is down).
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

HISTORY_TAIL = 900  # rows sent to the UI (~15 min at 1 Hz)

# The 7-order vocabulary (p_nemesis.c NEM_OrderIndex) — the heatmap columns.
ORDER_KEYS = ["chase", "hold", "fallback", "flank_left", "flank_right",
              "ambush", "focus_fire", "use_door"]

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
h1{font-size:15px;letter-spacing:3px;color:var(--cy);text-transform:uppercase}
h1 .sub{color:var(--dim);letter-spacing:1px;text-transform:none;font-size:11px}
.wrap{max-width:1180px;margin:0 auto}
.bar{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:10px;padding:12px;position:relative;
  box-shadow:0 0 0 1px #000 inset,0 8px 24px #0008}
.panel h2{font-size:10px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  margin-bottom:10px}
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
table.heat{border-collapse:separate;border-spacing:4px;width:100%}
table.heat th{font-size:9px;color:var(--dim);letter-spacing:1px;padding:2px 4px;text-align:center}
table.heat td.type{font-size:11px;color:var(--txt);text-align:left;white-space:nowrap;padding-right:8px}
table.heat td.cell{min-width:64px;height:30px;border-radius:5px;text-align:center;
  font-size:11px;color:#eaf4ff;border:1px solid #0006;text-shadow:0 1px 2px #000c}
table.heat td.cell.na{background:#0a101c;color:#31415e;border-style:dashed}
.legend{display:flex;gap:14px;margin-top:8px;font-size:9px;color:var(--dim);align-items:center}
.chip{width:14px;height:10px;border-radius:3px;display:inline-block;vertical-align:middle}
.deaths{font-size:11px;color:var(--dim)}
.deaths b{color:var(--am);font-size:16px}
/* sparklines */
.spk{width:100%;height:64px;display:block}
.spkrow{margin-bottom:8px}
.spkhead{display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:2px}
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
</style></head><body><div class="wrap">
<div class="bar">
  <h1>Nemesis <span class="sub">// weight-evolution console — watching the jev learn</span></h1>
  <div class="status" id="status"><span class="pulse"></span>connecting…</div>
</div>
<div class="grid">
  <div class="panel c3"><h2>Episode</h2><div class="big" id="ep">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="epsub">waiting for agent</div></div>
  <div class="panel c3"><h2>Epsilon (chaos)</h2><div class="big" id="eps">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="epssub">explore vs exploit</div></div>
  <div class="panel c3"><h2>Survival avg</h2><div class="big" id="surv">–<span class="u"> s</span></div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="rsub">last terminal: –</div></div>
  <div class="panel c3"><h2>Player HP</h2><div class="big" id="hp">–</div>
    <div style="font-size:10px;color:var(--dim);margin-top:4px" id="act">act: –</div></div>

  <div class="panel c6"><h2>Hostile-buddy skill (curriculum)</h2>
    <div class="big" id="skname" style="font-size:24px">–</div>
    <div class="meter" id="meter"></div>
    <div class="mlabels" id="mlabels"></div>
    <div style="font-size:10px;color:var(--dim);margin-top:8px" id="sksub">level 0 = almost useless; lessons: +1 per buddy death, −1 when it kills you</div>
  </div>
  <div class="panel c6"><h2>Learned weight heatmap — nemesis rows</h2>
    <table class="heat" id="heat"></table>
    <div class="legend">
      <span><span class="chip" style="background:#f87171"></span> punished (&lt;1.0)</span>
      <span><span class="chip" style="background:#1d2940"></span> neutral 1.0</span>
      <span><span class="chip" style="background:#34d399"></span> rewarded (&gt;1.0)</span>
      <span style="margin-left:auto" class="deaths" id="deaths"></span>
    </div>
  </div>

  <div class="panel c8"><h2>Telemetry — last 15 min</h2>
    <div class="spkrow"><div class="spkhead"><span>epsilon</span><span id="spk_eps_v"></span></div>
      <canvas class="spk" id="spk_eps"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>episode terminal reward</span><span id="spk_r_v"></span></div>
      <canvas class="spk" id="spk_r"></canvas></div>
    <div class="spkrow"><div class="spkhead"><span>survival seconds / player hp</span><span id="spk_surv_v"></span></div>
      <canvas class="spk" id="spk_surv"></canvas></div>
  </div>
  <div class="panel c4"><h2>Event ticker</h2><ul class="tick" id="tick"></ul></div>
  <div class="panel c12"><h2>Weapon bias per type</h2><div id="wbias" style="font-size:11px;color:var(--dim)">no learned bias yet — the nemesis has to die (or kill) first</div></div>
  <div class="foot">PROJECT NEMESIS // phase 9 // no stat buffs — the weights are the whole story</div>
</div>
<script>
"use strict";
const ORDERS=__ORDERS__;
const LEVELS=__LEVELS__;
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
    const wbs=Object.entries(wb).map(([k,v])=>`${k}:${v>0?'+':''}${v}`).join("  ");
    if(wbs)dsum+=`${esc(r.type)}  ${wbs}\n`;
  }
  t.innerHTML=head+body;
  $("deaths").textContent=rows&&rows.length?("total deaths: "+rows.reduce((a,r)=>a+(r.deaths||0),0)):"no rows yet";
  const wbEl=$("wbias");
  if(dsum){wbEl.textContent=dsum.trim();wbEl.style.whiteSpace="pre";}
  else{wbEl.textContent="no learned bias yet — the nemesis has to die (or kill) first";}
}
function drawSkill(sk){
  const m=$("meter"),lb=$("mlabels");
  if(m.children.length!==LEVELS.length){
    m.innerHTML=LEVELS.map(()=>`<div class="seg"></div>`).join("");
    lb.innerHTML=LEVELS.map(n=>`<span>${n}</span>`).join("");
  }
  [...m.children].forEach((el,i)=>el.classList.toggle("on",i<=sk));
  [...lb.children].forEach((el,i)=>el.classList.toggle("cur",i===sk));
  $("skname").textContent=LEVELS[sk]||("level "+sk);
}
function spark(canvas,vals,color,now){
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
  ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.shadowColor=color;ctx.shadowBlur=6;
  ctx.stroke();ctx.shadowBlur=0;
  // last point glow
  const lx=(w-4),ly=h-6-((vals[vals.length-1]-mn)/sp)*(h-14);
  ctx.fillStyle=color;ctx.beginPath();ctx.arc(lx-4,ly,2.5,0,7);ctx.fill();
  if(now!=null)$(now).textContent=`min ${fmt(mn,2)} · max ${fmt(mx,2)} · now ${fmt(vals[vals.length-1],2)}`;
}
function classify(t){
  const s=t.toLowerCase();
  if(s.includes("killed")&&s.includes("shotgunguy"))return"kill";
  if(s.includes("promot"))return"promo";
  if(s.includes("demot"))return"kill";
  if(s.includes("spawn"))return"spawn";
  return"";
}
function render(s){
  if(!s||!s.ts){$("status").innerHTML='<span class="pulse"></span>waiting for first snapshot…';return;}
  $("status").className="status ok";
  $("status").innerHTML='<span class="pulse"></span>live · tic '+s.tic;
  $("ep").textContent=s.episode_label;
  $("epsub").textContent=`completed episodes: ${s.episode} · alive: ${s.alive?"yes":"no"}`;
  $("eps").textContent=fmt(s.epsilon,3);
  const e=s.epsilon;
  $("epssub").textContent=e>0.5?"pure exploration (random)":e>0.15?"still curious":"exploiting what it learned";
  $("eps").className="big "+(e>0.5?"":e>0.15?"":"");
  $("surv").innerHTML=fmt(s.surv_avg,1)+'<span class="u"> s</span>';
  $("rsub").textContent=`last terminal: ${s.last_terminal_r>=0?"+":""}${fmt(s.last_terminal_r,1)}`;
  $("rsub").style.color=s.last_terminal_r>=0?"var(--gr)":"var(--rd)";
  $("hp").textContent=s.player_hp??"–";
  $("hp").className="big "+((s.player_hp??100)>60?"ok":(s.player_hp??100)>25?"warn":"bad");
  $("act").textContent="act: "+(s.last_action??"–");
  if(typeof s.buddy_skill==="number")drawSkill(s.buddy_skill);
  drawHeat(s.rows||[]);
  const tick=$("tick");
  const evs=[...(s.agent_events||[]).map(x=>[x.ts,x.text]),
             ...(s.events||[]).map(x=>[s.ts,x])].slice(-24);
  tick.innerHTML=evs.slice().reverse().map(([ts,tx])=>
    `<li class="${classify(tx)}"><span class="t">${new Date(ts*1000).toLocaleTimeString()} </span>${esc(tx)}</li>`).join("")
    ||'<li><span class="t">no events yet…</span></li>';
}
function renderHist(rows){
  hist=rows||[];
  spark("spk_eps",hist.map(r=>r.eps),"#22d3ee","spk_eps_v");
  spark("spk_r",hist.map(r=>r.r),"#34d399","spk_r_v");
  spark("spk_surv",hist.map(r=>r.surv??r.hp),"#fbbf24","spk_surv_v");
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
poll();pollHist();
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
            .replace("__LEVELS__", json.dumps(list(config.SKILL_LEVELS))))

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


def start(live=None, port: int = 0):
    """Start the dashboard server. Returns (httpd, url)."""
    state = DashboardState(live)
    httpd = ThreadingHTTPServer(("127.0.0.1", port or config.DASHBOARD_PORT),
                                make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Nemesis dashboard (standalone: reads files from disk)")
    ap.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    args = ap.parse_args()
    httpd, url = start(live=None, port=args.port)
    print(f"[dashboard] serving {url}  (reads {config.LIVE_STATE_PATH} + history)")
    print("[dashboard] Ctrl+C to stop")
    try:
        while True:
            import time as _t
            _t.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

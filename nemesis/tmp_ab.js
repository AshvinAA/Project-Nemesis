// Phase 9 A/B probe: push buddy skill=N, sample player HP for S seconds.
// usage: node nemesis/tmp_ab.js <skill> <seconds>
const net = require('net');
const lvl = parseInt(process.argv[2] || '0', 10);
const secs = parseInt(process.argv[3] || '12', 10);
const s = net.connect(31666, '127.0.0.1');
let buf = '';
s.setNoDelay(true);
const send = (x) => s.write(x + '\n');

let samples = [];
let t0 = null;
let started = false;
let done = false;

function onObs(o) {
  if (!o || !o.player) return;
  samples.push({
    t: (Date.now() - t0) / 1000,
    tic: o.tic,
    hp: o.player.health,
    skill: o.nemesis && o.nemesis.buddy_skill,
    rows: o.nemesis && o.nemesis.rows ? o.nemesis.rows.length : 0,
  });
}

function start() {
  if (started) return;
  started = true;
  t0 = Date.now();
  const iv = setInterval(() => send('observe'), 200);
  setTimeout(() => { clearInterval(iv); finish(); }, secs * 1000);
}

function finish() {
  if (done) return;
  done = true;
  const hp0 = samples.length ? samples[0].hp : null;
  const hpN = samples.length ? samples[samples.length - 1].hp : null;
  let firstDmg = null;
  for (const sm of samples) {
    if (hp0 != null && sm.hp < hp0) { firstDmg = sm.t; break; }
  }
  const tics = samples.length ? [samples[0].tic, samples[samples.length - 1].tic] : null;
  console.log(JSON.stringify({
    skill: lvl, hp_start: hp0, hp_end: hpN,
    dmg: (hp0 != null && hpN != null) ? hp0 - hpN : null,
    first_dmg_s: firstDmg, tic_range: tics, samples: samples.length,
  }));
  s.destroy();
  process.exit(0);
}

s.on('connect', () => send('buddy skill=' + lvl));
s.on('data', (d) => {
  buf += d.toString();
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    if (!line.startsWith('{')) { console.error('reply:', line); start(); continue; }
    try { onObs(JSON.parse(line)); } catch (e) { console.error('bad json:', line.slice(0, 80)); }
  }
});
s.on('error', (e) => { console.error('conn err', e.message); process.exit(2); });
setTimeout(() => finish(), (secs + 8) * 1000); // fail-safe

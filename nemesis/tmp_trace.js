// Trace player.health via observe to confirm the hostile buddy is fighting.
const net = require('net');

function observe() {
    return new Promise((resolve) => {
        const s = net.connect(31666, '127.0.0.1', () => s.write('observe\n'));
        let buf = '';
        s.setTimeout(4000, () => { s.destroy(); resolve(null); });
        s.on('data', (d) => {
            buf += d;
            if (buf.includes('\n')) {
                s.end();
                try { resolve(JSON.parse(buf)); } catch { resolve(null); }
            }
        });
        s.on('error', () => resolve(null));
    });
}

(async () => {
    for (let i = 0; i < 7; i++) {
        const o = await observe();
        if (!o) { console.log('no response'); continue; }
        const p = o.player || {};
        const bud = o.buddy ? ` buddy=${JSON.stringify(o.buddy)}` : '';
        console.log(`tic=${o.tic} hp=${p.health} region=${p.region} monsters=${(o.monsters||[]).length}${bud}`);
        await new Promise(r => setTimeout(r, 2000));
    }
    process.exit(0);
})();

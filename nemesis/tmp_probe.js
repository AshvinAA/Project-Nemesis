// Phase 8 live verification: connect, observe, ABORT hard (no clean close),
// reconnect, observe again.  Verifies the p_ai_llm.c AI_PollSocket fix AND
// that the :31666 listener answers on the fresh MinGW build.
const net = require('net');

function probe(label, hard) {
    return new Promise((resolve) => {
        const s = net.connect(31666, '127.0.0.1', () => s.write('observe\n'));
        let buf = '';
        const done = (ok, note) => {
            console.log(`[${label}] ok=${ok} ${note || ''}`);
            resolve(ok);
        };
        s.setTimeout(5000, () => { s.destroy(); done(false, 'timeout'); });
        s.on('data', (d) => {
            buf += d;
            if (buf.includes('\n')) {
                let head = buf.split('\n')[0].slice(0, 200);
                let ok = buf.includes('"nolevel"') || buf.includes('"monsters"');
                done(ok, head);
                if (hard) s.resetAndDestroy ? s.resetAndDestroy() : s.destroy(); // RST, no FIN
                else s.end();
            }
        });
        s.on('error', (e) => done(false, 'conn error: ' + e.code));
    });
}

(async () => {
    await probe('connect#1', true);   // hard-abort this one
    await new Promise(r => setTimeout(r, 1500));
    const ok = await probe('connect#2', false);  // listener must still answer
    console.log(ok ? 'LISTENER_SURVIVED_ABRUPT_DISCONNECT' : 'LISTENER_STILL_WEDGED');
    process.exit(0);
})();

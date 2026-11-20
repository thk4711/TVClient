/* Opt-in TV-code pairing. Never render remote strings as HTML. */
(() => {
    const element = id => document.getElementById(id);
    const status = element('youtube-status'), code = element('youtube-code');
    const help = element('youtube-pairing-help');
    if (!status || !code || !help) return;
    const qr = element('youtube-qr'), canvas = element('youtube-qr-image');
    const link = element('youtube-pairing-link');
    let renderedCode = '', generation = 0;
    function clearCode() {
        code.hidden = true; code.textContent = '';
        renderedCode = '';
        if (qr) qr.hidden = true;
        if (link) link.removeAttribute('href');
        if (canvas) canvas.width = canvas.width; // Erase the old pairing secret.
    }
    function renderCode(value) {
        if (!pairing || typeof value !== 'string' || !/^[0-9]{12}$/.test(value)) {
            clearCode(); return;
        }
        code.hidden = false; code.textContent = value.match(/.{3}/g).join(' ');
        if (!qr || !canvas || !link || renderedCode === value) return;
        try {
            // App handoff is experimental; /pair falls back to YouTube help in browsers.
            const url = 'https://www.youtube.com/pair?code=' + value;
            const matrix = qrcode(0, 'M');
            matrix.addData(url, 'Byte'); matrix.make();
            const count = matrix.getModuleCount(), border = 4, scale = 6;
            canvas.width = canvas.height = (count + border * 2) * scale;
            const context = canvas.getContext('2d');
            context.fillStyle = '#fff';
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.fillStyle = '#000';
            for (let y = 0; y < count; y++) {
                for (let x = 0; x < count; x++) {
                    if (matrix.isDark(y, x)) context.fillRect((x + border) * scale, (y + border) * scale, scale, scale);
                }
            }
            link.href = url; qr.hidden = false; renderedCode = value;
        } catch (_) {
            // A missing/failed encoder must never break numeric pairing.
            qr.hidden = true; link.removeAttribute('href');
            canvas.width = canvas.width; renderedCode = '';
        }
    }
    let pending = false, polling = false, pairing = false;
    const controls = ['pair', 'disable', 'unlink'].map(action => element('youtube-' + action));
    async function refresh() {
        if (polling || document.hidden || element('page-settings').hidden) return;
        polling = true;
        const currentGeneration = generation;
        try {
            const data = await receiverRequest('/api/youtube', 'GET', undefined, new AbortController(), 5000);
            if (currentGeneration !== generation) return;
            status.textContent = data.error || (data.active ? 'YouTube video selected · ' + (data.video_id || '') :
                data.state === 'ready' ? 'Receiver ready. Select a video in your linked YouTube app.' :
                data.enabled ? 'Connecting to YouTube…' : 'YouTube disabled.');
            status.hidden = !pairing && !data.error;
            renderCode(data.enabled === false || data.error ? null : data.pairing_code);
        } catch (_) {
            if (currentGeneration !== generation) return;
            clearCode();
            status.hidden = false;
            status.textContent = 'YouTube status unavailable. Reopen Settings to retry.';
        } finally { polling = false; }
    }
    for (const action of ['pair', 'disable', 'unlink']) {
        element('youtube-' + action).onclick = async () => {
            if (pending) return;
            if (action === 'unlink' && !confirm('Forget all linked phones and disable YouTube?')) return;
            pending = true; controls.forEach(control => { control.disabled = true; });
            generation++;
            pairing = action === 'pair';
            help.hidden = !pairing;
            status.hidden = !pairing;
            clearCode();
            try {
                await receiverRequest('/api/youtube', 'POST', {action});
                status.textContent = action === 'pair' ? 'Requesting a TV code…' : 'Request accepted. Waiting for the receiver…';
                await refresh();
            } catch (_) {
                status.hidden = false;
                status.textContent = 'Could not confirm request. Check receiver status before retrying.';
            } finally { pending = false; controls.forEach(control => { control.disabled = false; }); }
        };
    }
    window.addEventListener('hashchange', refresh);
    document.addEventListener('visibilitychange', refresh);
    setInterval(refresh, 2000);
    void refresh();
})();

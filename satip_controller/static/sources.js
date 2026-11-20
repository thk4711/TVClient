/* Shared source discovery/bouquet browser. Remote names are rendered as text only. */
(() => {
    const $ = id => document.getElementById('source-' + id);
    if (!$('kind')) return;
    const setup = document.body.classList.contains('setup-page');
    let data = null, loaded = false, pending = false, bouquets = [], selection = new Set();
    let receiverKey = '', initialized = false, timer;
    const active = new Set(['starting', 'pausing', 'running', 'stopping']);
    const kind = () => $('kind').value;
    const receiver = () => ({address: $('address').value.trim(), web_port: Number($('web-port').value), stream_port: Number($('stream-port').value)});
    const api = (path, payload) => receiverRequest('/api/sources' + path, payload === undefined ? 'GET' : 'POST', payload, new AbortController(), 30000);
    function renderConfiguration() {
        if (setup || !data) return;
        const satip = data.satip || {}, current = data.active === 'satip', info = data.receiver;
        const identity = [satip.name, satip.address].filter(Boolean).join(' — ') || 'Not configured';
        $('current-device').textContent = current ? 'Server: ' + identity :
            'Receiver: ' + (info ? info.address + ' · OpenWebif ' + info.web_port + ' · stream port ' + info.stream_port : 'Unavailable');
        const configured = satip.tuners?.find(t => t.uuid === satip.configured_frontend);
        $('current-tuner').textContent = current ? 'Configured tuner: ' + (configured ? '#' + configured.number : 'Unavailable') :
            'Physical tuner: allocated by the Enigma2 receiver (not selectable on the Pi).';
        $('receiving').textContent = current ? satip.error || (!satip.connected ? 'Server unavailable; showing saved configuration.' :
            satip.receiving?.length ? 'Currently receiving: ' + satip.receiving.join(' · ') : satip.tuning?.length ? 'Tuning, but no stream data received: ' + satip.tuning.join(' · ') + '. Check the satellite cable and signal.' : 'Server registered; no SAT-IP stream is currently receiving.') :
            'The saved receiver address is configuration, not a live connection check.';
        const previous = $('tuner').value, tuners = satip.tuners || [];
        $('tuner').replaceChildren();
        for (const tuner of tuners) {
            const option = document.createElement('option'); option.value = tuner.uuid;
            option.textContent = 'Tuner #' + tuner.number + (tuner.configured ? ' — configured' : '');
            $('tuner').append(option);
        }
        if (!tuners.length) {
            const option = document.createElement('option'); option.value = ''; option.textContent = 'No tuner available'; $('tuner').append(option);
        }
        $('tuner').value = tuners.some(t => t.uuid === previous) ? previous : satip.configured_frontend || '';
        window.sourceConfiguration = {active: data.active, available: data.available, satip};
        window.sourceTunerControls?.();
    }
    function controls() {
        const busy = active.has(data?.import?.state), blocked = pending || !loaded || busy || !!window.sourceTunerPending;
        const locked = setup && (window.sourceSetupLocked || false);
        $('kind').disabled = blocked || locked;
        $('enigma').hidden = kind() !== 'enigma2';
        if (!setup) {
            $('satip-panel').hidden = kind() !== 'satip';
            $('discovery-controls').hidden = kind() !== 'enigma2';
            $('enigma-help').hidden = kind() !== 'enigma2';
            $('load-bouquets').textContent = 'Load Enigma2 TV bouquets';
            $('discover').textContent = kind() === 'satip' ? 'Find SAT-IP servers' : 'Find Enigma2 receivers';
        }
        $('bouquet-panel').hidden = kind() !== 'enigma2';
        $('discover').disabled = blocked || data?.discovery?.[kind()]?.state === 'searching';
        $('server').disabled = blocked;
        for (const id of ['address', 'web-port', 'stream-port', 'load-bouquets']) $(id).disabled = blocked;
        for (const input of $('bouquets').querySelectorAll('input')) input.disabled = blocked;
        $('import').disabled = blocked || !selection.size || !!data?.import?.recovery_pending;
        $('cancel').hidden = !busy; $('cancel').disabled = pending;
        $('recover').hidden = !data?.import?.recovery_pending; $('recover').disabled = blocked;
        const source = document.getElementById('device-source');
        if (source && loaded) source.disabled = source.disabled || busy;
        window.sourceImportBusy = busy;
        if (setup && window.sourceSetupControls) window.sourceSetupControls();
        if (!setup) window.sourceTunerControls?.();
    }
    async function action(callback) {
        if (pending) return;
        pending = true; $('message').textContent = ''; controls();
        try { await callback(); }
        catch (error) { $('message').textContent = error.message; }
        finally { pending = false; controls(); }
    }
    function renderBouquets() {
        $('bouquets').replaceChildren(...bouquets.map(b => {
            const label = document.createElement('label'), input = document.createElement('input');
            label.className = 'option'; input.type = 'checkbox'; input.value = b.name;
            input.checked = selection.has(b.name);
            input.onchange = () => { input.checked ? selection.add(b.name) : selection.delete(b.name); controls(); };
            label.append(input, document.createTextNode(b.name)); return label;
        }));
    }
    async function loadBouquets() {
        const current = receiver(), key = JSON.stringify(current);
        const response = await api('/bouquets', current);
        if (key !== receiverKey) selection = new Set(data?.receiver?.address === current.address ? data.receiver.bouquets : []);
        receiverKey = key; bouquets = response.bouquets;
        selection = new Set([...selection].filter(name => bouquets.some(b => b.name === name)));
        renderBouquets();
        $('message').textContent = response.receiver.model + ' · ' + bouquets.length + ' TV bouquets. Select genuinely free-to-air bouquets.';
    }
    $('load-bouquets').onclick = () => action(loadBouquets);
    $('discover').onclick = () => action(async () => {
        await api('/discover', {source: kind()});
        data.discovery[kind()].state = 'searching';
        $('discovery-status').textContent = 'Searching the local network…';
    });
    $('kind').onchange = () => {
        const device = document.getElementById('device-source');
        if (device) device.value = kind();
        if (setup) window.sourceSetupKind = kind();
        renderServers(); controls();
    };
    const device = document.getElementById('device-source');
    if (device) device.addEventListener('change', () => { $('kind').value = device.value; $('kind').onchange(); });
    $('server').onchange = () => action(async () => {
        const server = data.discovery[kind()].servers.find(s => s.address === $('server').value);
        if (server && kind() === 'enigma2') { $('address').value = server.address; await loadBouquets(); }
        else if (server) $('message').textContent = (server.model || server.name) + ' — ' + server.address + ' · ' + server.tuners + ' DVB-S/S2 tuners. Discovery only: playback and the configured server are unchanged. Use the tuner selector for the configured server below.';
    });
    $('import').onclick = () => action(async () => {
        if (typeof dirty !== 'undefined' && dirty) throw new Error('Save or reload your favourite edits before importing bouquets.');
        if (!window.confirm('Import selected bouquets? Pi TV playback pauses while streams are validated. The Enigma2 receiver will not be zapped or reconfigured.')) return;
        await api('/import', {...receiver(), bouquets: [...selection], confirm_pause: true});
        data.import = {state: 'starting'};
        if (setup) window.sourceSetupKind = 'enigma2';
        $('import-status').textContent = 'Starting bouquet import…';
    });
    $('cancel').onclick = () => action(async () => { await api('/cancel', {}); await poll(false); });
    $('recover').onclick = () => action(async () => { await api('/recover', {}); await poll(false); });
    function renderServers() {
        if (!data) return;
        const result = data.discovery[kind()], previous = $('server').value;
        if (!setup) $('server-results').hidden = !result.servers.length;
        $('server').replaceChildren();
        for (const server of [{address: '', name: 'Choose a server…'}, ...result.servers]) {
            const option = document.createElement('option'); option.value = server.address;
            option.textContent = server.address ? (server.model || server.name) + ' — ' + server.address : server.name;
            $('server').append(option);
        }
        if (result.servers.some(s => s.address === previous)) $('server').value = previous;
        $('discovery-status').textContent = result.state === 'searching' ? 'Searching the local network…' :
            result.state === 'error' ? result.error : result.state === 'complete' ?
            (result.servers.length ? result.servers.length + ' server(s) found. Discovery does not change playback.' : kind() === 'enigma2' ? 'No Enigma2 receiver replied. Enter its private LAN IPv4 address and OpenWebif port manually.' : 'No SAT-IP server replied. Check server power and the local network; the saved configuration is unchanged.') : setup ? 'Search for the selected TV source.' : '';
    }
    async function poll(reschedule = true) {
        try {
            const oldState = data?.import?.state;
            data = await api(''); loaded = true;
            if ($('active')) $('active').textContent = 'Currently active: ' + (data.active === 'enigma2' ? 'Enigma2 receiver' : 'SAT-IP server');
            if (!initialized) {
                $('kind').value = setup ? 'satip' : data.active;
                if (data.receiver) {
                    $('address').value = data.receiver.address; $('web-port').value = data.receiver.web_port;
                    $('stream-port').value = data.receiver.stream_port;
                }
                initialized = true;
                if (setup) window.sourceSetupKind = 'satip';
            }
            renderServers(); renderConfiguration();
            const scan = data.import;
            $('import-status').textContent = active.has(scan.state) ?
                'Importing: ' + (scan.completed || 0) + ' / ' + (scan.total || 0) + ' streams checked · ' + (scan.found || 0) + ' supported channels found' :
                scan.error || scan.message || '';
            const progress = $('progress'); progress.hidden = !active.has(scan.state);
            if (scan.total) { progress.max = scan.total; progress.value = scan.completed || 0; }
            else progress.removeAttribute('value');
            if (oldState && active.has(oldState) && scan.state === 'complete' && !setup && typeof load === 'function') await load();
        } catch (error) { loaded = false; $('discovery-status').textContent = 'Source controls unavailable. Retrying…'; }
        controls();
        if (reschedule) timer = setTimeout(poll, 2000);
    }
    window.addEventListener('pagehide', () => clearTimeout(timer));
    controls(); poll();
})();

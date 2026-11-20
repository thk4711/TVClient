/* Application navigation and read-only dashboard; tuner actions remain in ui.js. */
(() => {
    const pages = ['status', 'live', 'channels', 'scan', 'settings'];
    let settingsPending = false;
    let page = 'status', telemetryPending = false, telemetryData = null;
    let lastActivity = '', watchSignature = '', watchSwitching = false;
    const log = [];
    const byId = id => document.getElementById(id);
    const put = (id, value) => { byId(id).textContent = value; };
    const value = (n, unit = '') => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString(undefined, {maximumFractionDigits: 1}) + unit : 'Not reported';
    const bytes = n => typeof n === 'number' ? value(n / 1048576, ' MiB') : 'Not reported';
    const rate = n => typeof n === 'number' ? value(n / 1000000, ' Mbit/s') : 'Not reported';
    const yes = n => n === true ? 'Yes' : n === false ? 'No' : 'Not reported';
    const uptime = n => typeof n === 'number' ? (n >= 86400 ? Math.floor(n / 86400) + 'd ' : '') + Math.floor(n % 86400 / 3600) + 'h ' + Math.floor(n % 3600 / 60) + 'm' : '—';
    function node(tag, text, className) {
        const result = document.createElement(tag);
        if (text !== undefined) result.textContent = text;
        if (className) result.className = className;
        return result;
    }
    function facts(target, entries) {
        target.replaceChildren();
        for (const [label, text] of entries) target.append(node('dt', label), node('dd', text ?? 'Not reported'));
    }
    function addLog(text) {
        if (!text || text === lastActivity) return;
        lastActivity = text; log.unshift({time: new Date(), text}); log.length = Math.min(log.length, 12);
        const list = byId('activity-log'); list.replaceChildren();
        for (const event of log) {
            const item = node('li'); item.append(node('time', event.time.toLocaleTimeString()), node('span', event.text)); list.append(item);
        }
    }
    function navigate() {
        const requested = location.hash.slice(1);
        if (requested === 'main') return;
        const next = pages.includes(requested) ? requested : 'status';
        if (next !== 'status' && byId('performance-dialog').open) byId('performance-dialog').close();
        if (next !== page) window.scrollTo(0, 0);
        page = next;
        for (const name of pages) byId('page-' + name).hidden = name !== page;
        document.querySelectorAll('[data-page]').forEach(link => {
            if (link.dataset.page === page) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
        document.title = 'SAT-IP · ' + ({status: 'Status', live: 'Live TV', channels: 'Channels', scan: 'Channel search', settings: 'Settings'})[page];
        if (page === 'status') void refreshTelemetry();
        if (page === 'settings' || page === 'channels') void loadSettings();
    }
    async function loadSettings() {
        if (settingsPending) return;
        settingsPending = true;
        byId('osd-language').disabled = byId('osd-timeout').disabled = byId('save-settings').disabled = true;
        put('settings-status', 'Loading settings…');
        try {
            const data = await api('/api/settings');
            byId('osd-timeout').value = data.osd_timeout_ms / 1000;
            byId('osd-language').value = data.osd_language || 'en';
            if (window.loadDeviceSettings) void window.loadDeviceSettings(data.device_settings === true);
            byId('osd-language').disabled = byId('osd-timeout').disabled = byId('save-settings').disabled = false;
            put('settings-status', '');
        } catch (_) {
            put('settings-status', 'Cannot load settings. Reopen this page to retry.');
        } finally { settingsPending = false; }
    }
    byId('osd-settings').onsubmit = async event => {
        event.preventDefault();
        if (settingsPending || !byId('osd-settings').reportValidity()) return;
        settingsPending = true;
        const timeout = Math.round(Number(byId('osd-timeout').value) * 1000);
        byId('osd-language').disabled = byId('osd-timeout').disabled = byId('save-settings').disabled = true;
        put('settings-status', 'Saving settings…');
        try {
            const data = await api('/api/settings', 'PUT', {osd_timeout_ms: timeout, osd_language: byId('osd-language').value});
            byId('osd-timeout').value = data.osd_timeout_ms / 1000;
            byId('osd-language').value = data.osd_language || 'en';
            put('settings-status', 'Saved.');
        } catch (_) {
            put('settings-status', 'Could not confirm save. Reopen this page to check before retrying.');
        } finally {
            settingsPending = false;
            byId('osd-language').disabled = byId('osd-timeout').disabled = byId('save-settings').disabled = false;
        }
    };
    window.navigateReceiver = name => {
        if (!pages.includes(name)) return;
        location.hash = name; navigate();
    };
    window.addEventListener('hashchange', () => { navigate(); byId('main').focus({preventScroll: true}); });
    byId('header-stop').onclick = () => stopPreview();
    byId('watch-search').oninput = () => window.renderWatchChannels();
    window.renderWatchChannels = () => {
        const query = byId('watch-search').value.toLowerCase();
        const channels = catalogue.filter(c => previewable.includes(c.service_uuid) && c.name.toLowerCase().includes(query));
        channels.sort((a, b) => {
            const ai = favourites.indexOf(a.service_uuid), bi = favourites.indexOf(b.service_uuid);
            return (ai < 0 ? 10000 : ai) - (bi < 0 ? 10000 : bi) || a.name.localeCompare(b.name);
        });
        const signature = JSON.stringify([channels, favourites]);
        if (signature !== watchSignature) {
            watchSignature = signature;
            const list = byId('watch-channels'); list.replaceChildren();
            for (const channel of channels) {
                const item = node('li'), control = node('button');
                control.dataset.service = channel.service_uuid;
                control.dataset.name = channel.name;
                control.append(node('span', channel.name), node('small', favourites.includes(channel.service_uuid) ? '★' : '▷'));
                control.setAttribute('aria-label', 'Watch live: ' + channel.name);
                control.onclick = async () => {
                    if (watchSwitching || requesting || busy || recovering || !connected) return;
                    watchSwitching = true; window.receiverControls();
                    try {
                        if (previewSession) await stopPreview();
                        await startPreview(channel);
                    } finally { watchSwitching = false; window.receiverControls(); }
                };
                item.append(control); list.append(item);
            }
        }
        byId('watch-empty').hidden = channels.length > 0;
        window.receiverControls();
    };
    window.receiverControls = () => {
        const active = ['starting', 'ready'].includes(previewState.state);
        byId('stream-indicator').hidden = !active;
        byId('header-stop').disabled = !previewSession || requesting || !connected;
        byId('video-placeholder').hidden = !byId('preview-video').hidden;
        put('watch-channel-name', active ? previewState.channel_name || '' : '');
        document.querySelectorAll('#watch-channels button').forEach(control => {
            control.disabled = !connected || !previewState.enabled || busy || recovering || requesting || watchSwitching || (active && !previewSession);
            control.setAttribute('aria-current', String(active && control.dataset.name === previewState.channel_name));
        });
    };
    function programmes(target, rows) {
        target.replaceChildren();
        const now = Date.now() / 1000;
        for (const event of rows || []) {
            const line = node('p', undefined, 'programme');
            const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
            line.append(node('small', (event.start <= now ? 'NOW' : 'NEXT') + ' · ' + clock(event.start) + '–' + clock(event.stop)), node('span', event.title));
            target.append(line);
        }
    }
    window.receiverStatus = data => {
        put('connection-state', 'Connected');
        const player = data.player || {}, scan = data.scan || {};
        const active = ['starting', 'ready'].includes(previewState.state);
        const activity = recovering ? 'Recovery needed' : busy ? 'Channel search' : active ? 'Browser streaming' : player.error ? 'Playback unavailable' : player.playing ? (player.timeshift?.paused ? 'TV paused' : player.timeshift?.behind_live_seconds > 5 ? 'Time-shifted TV' : 'Watching TV') : player.loading ? 'Changing channel' : 'Idle';
        put('activity-value', activity);
        put('activity-detail', active ? previewState.channel_name || 'Preparing stream' : player.channel_name || (player.error ? 'Check receiver and reception' : 'HDMI output'));
        if (!active && player.timeshift?.enabled) put('activity-detail', (player.channel_name || 'TV') + ' · ' + Math.round(player.timeshift.behind_live_seconds) + 's behind live · ' + Math.round(player.timeshift.available_seconds) + 's buffered' + (player.timeshift.warning ? ' · ' + player.timeshift.warning : '') + (player.timeshift.paused || player.timeshift.behind_live_seconds > 5 ? ' · Programme information is live' : ''));
        programmes(byId('watch-programmes'), active ? data.preview?.programmes : []);
        const description = activity + ((active ? previewState.channel_name : player.channel_name) ? ' · ' + (active ? previewState.channel_name : player.channel_name) : '');
        addLog(description);
        if (data.preview?.reason && data.preview.state === 'stopped') {
            put('preview-status', data.preview.reason);
        }
        put('last-search', ['complete', 'cancelled', 'error'].includes(scan.state) ? 'Last search: ' + scan.state + ' · ' + (scan.found || 0) + ' channels found' : '');
        window.receiverControls();
    };
    window.receiverOffline = () => {
        put('connection-state', 'Reconnecting…'); put('activity-value', 'Disconnected');
        put('activity-detail', 'Trying to reconnect to the receiver');
        addLog('Connection lost. Reconnecting…');
    };
    function stateBadge(id, section) {
        const target = byId(id), age = section.sampled_at ? Math.max(0, Math.floor(Date.now() / 1000 - section.sampled_at)) : null;
        const stale = section.state !== 'ok' || age > 15;
        target.textContent = section.state === 'unavailable' ? 'Unavailable' : stale ? 'Stale' + (age === null ? '' : ' · ' + age + 's ago') : 'Updated ' + age + 's ago';
        target.className = 'badge ' + (stale ? 'warn' : 'good');
    }
    const performanceHistory = new Map();
    const finite = n => typeof n === 'number' && Number.isFinite(n);
    function performanceChart(target, key, label, reading, stamp, unit, maximum = null, fresh = true) {
        const now = Date.now() / 1000;
        let history = performanceHistory.get(key) || [];
        history = history.filter(point => point.time >= now - 300);
        if (fresh && finite(stamp) && stamp >= now - 300 && (!history.length || stamp > history.at(-1).time)) {
            history.push({time: stamp, value: finite(reading) ? reading : null});
        }
        history = history.slice(-100); performanceHistory.set(key, history);
        // Keep bounded history on Status, but build graph DOM only on demand.
        if (!byId('performance-dialog').open) return;
        const card = node('div', undefined, 'performance-metric');
        card.append(node('span', label, 'label'), node('strong', value(reading, unit)));
        if (maximum === 100 && finite(reading)) {
            const meter = node('meter'); meter.min = 0; meter.max = 100; meter.value = reading;
            meter.setAttribute('aria-label', label); card.append(meter);
        }
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 300 64'); svg.setAttribute('preserveAspectRatio', 'none');
        svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', label + ' history, last five minutes');
        svg.classList.add('performance-graph');
        const scale = maximum || Math.max(1, ...history.map(point => point.value || 0));
        let path = '', previous = null;
        for (const point of history) {
            if (point.value === null) { previous = null; continue; }
            const x = Math.max(0, Math.min(300, 300 - (now - point.time)));
            const y = 62 - Math.max(0, Math.min(1, point.value / scale)) * 60;
            path += (previous && point.time - previous.time <= 15 ? ' L' : ' M') + x.toFixed(1) + ',' + y.toFixed(1);
            previous = point;
        }
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        line.setAttribute('d', path); svg.append(line); card.append(svg);
        card.append(node('small', maximum ? 'Scale: 0–100%' : 'Scale: 0–' + value(scale, unit), 'hint'));
        target.append(card);
    }
    function renderPerformance(data) {
        const performance = data.performance || {}, network = data.network || {};
        stateBadge('performance-state', performance);
        const open = byId('performance-dialog').open;
        if (open) stateBadge('performance-dialog-state', performance);
        const target = byId('performance-content');
        if (open) target.replaceChildren();
        const chart = (key, label, reading, unit = '%', maximum = 100) => performanceChart(
            target, key, label, reading, performance.sampled_at, unit, maximum, performance.state === 'ok');
        chart('cpu', 'Total CPU', performance.cpu_percent);
        for (const core of performance.cores || []) {
            chart(core.name, core.name.toUpperCase() + ' · ' + value(core.frequency_mhz, ' MHz'), core.percent);
        }
        const memoryPercent = finite(performance.memory_used) && performance.memory_total > 0 ? 100 * performance.memory_used / performance.memory_total : null;
        chart('memory', 'RAM used · ' + bytes(performance.memory_used) + ' / ' + bytes(performance.memory_total), memoryPercent);
        chart('swap', 'Swap used · ' + bytes(performance.swap_used) + ' / ' + bytes(performance.swap_total),
            performance.swap_total === 0 ? 0 : performance.swap_total > 0 ? 100 * performance.swap_used / performance.swap_total : null);
        for (const item of network.interfaces || []) {
            for (const [key, label] of [['rx_bps', 'Receive'], ['tx_bps', 'Send']]) performanceChart(
                target, 'net-' + item.name + key, item.name + ' · ' + label,
                finite(item[key]) ? item[key] / 1000000 : null, network.sampled_at, ' Mbit/s', null, network.state === 'ok');
        }
        for (const disk of performance.disks || []) {
            for (const [key, label] of [['read_bytes_per_second', 'Read'], ['write_bytes_per_second', 'Write']]) chart(
                'disk-' + disk.name + key, disk.name + ' · ' + label,
                finite(disk[key]) ? disk[key] / 1048576 : null, ' MiB/s', null);
        }
        // Remove histories for vanished interfaces/devices so memory remains bounded.
        const activeKeys = new Set(['cpu', 'memory', 'swap', ...(performance.cores || []).map(c => c.name),
            ...(network.interfaces || []).flatMap(i => ['net-' + i.name + 'rx_bps', 'net-' + i.name + 'tx_bps']),
            ...(performance.disks || []).flatMap(d => ['disk-' + d.name + 'read_bytes_per_second', 'disk-' + d.name + 'write_bytes_per_second'])]);
        for (const key of performanceHistory.keys()) if (!activeKeys.has(key)) performanceHistory.delete(key);
        if (!open) return;
        const rows = byId('performance-processes'); rows.replaceChildren();
        for (const process of performance.processes || []) {
            const row = node('tr');
            for (const reading of [process.pid, process.name, value(process.cpu_percent, '%'), bytes(process.memory_bytes)]) row.append(node('td', reading));
            rows.append(row);
        }
        const notes = ['Load average (1 / 5 / 15 min): ' + ((performance.load_averages || []).map(n => value(n)).join(' / ') || 'Not reported'),
            'Available RAM: ' + bytes(performance.memory_available), 'File cache: ' + bytes(performance.memory_cached)];
        if (!performance.processes?.length) notes.push('Process measurements unavailable');
        if (performance.processes_truncated) notes.push('Process scan limited; list may be incomplete');
        put('performance-note', notes.join(' · '));
    }
    function renderTelemetry(data) {
        renderPerformance(data);
        const system = data.system || {}, network = data.network || {}, reception = data.reception || {};
        stateBadge('system-state', system); stateBadge('network-state', network); stateBadge('reception-state', reception);
        put('receiver-name', system.hostname || 'Your receiver');
        put('temperature-value', value(system.temperature_c, ' °C'));
        put('cpu-value', 'CPU utilization · ' + value(system.cpu_percent, '%'));
        put('uptime-value', uptime(system.uptime_seconds)); put('model-value', system.model || 'Hardware not reported');
        facts(byId('system-content'), [
            ['Hostname', system.hostname], ['Memory available', bytes(system.memory_available) + ' / ' + bytes(system.memory_total)],
            ['Storage free', bytes(system.storage_free) + ' / ' + bytes(system.storage_total)],
            ['Undervoltage now', yes(system.undervoltage)], ['Throttling now', yes(system.throttled)], ['Undervoltage since boot', yes(system.past_undervoltage)],
            ['Operating system', system.os], ['Image', system.build?.build_inputs_sha256?.slice(0, 16) || 'Build identity not reported']
        ]);
        const receptionTarget = byId('reception-content'); receptionTarget.replaceChildren(); receptionTarget.className = '';
        if (!reception.streams?.length) receptionTarget.append(node('p', reception.state === 'ok' ? 'Tuner idle. Signal measurements appear when receiving a channel or searching.' : 'Tuner measurements are unavailable.', 'empty'));
        for (const stream of reception.streams || []) {
            const container = node('div', undefined, 'tuner-stream');
            container.append(node('h3', stream.input), node('p', stream.transponder || 'Transponder not reported', 'hint'));
            const signals = node('div', undefined, 'signal-grid');
            for (const [label, reading] of [['Signal strength', stream.signal], ['Quality / SNR', stream.quality]]) {
                const measurement = node('div');
                measurement.append(node('span', label, 'signal-label'), node('strong', value(reading?.value, ' ' + (reading?.unit || '')), 'signal-reading'));
                if (reading?.unit === '%') {
                    const meter = node('meter'); meter.min = 0; meter.max = 100; meter.value = reading.value; meter.setAttribute('aria-label', label); measurement.append(meter);
                }
                signals.append(measurement);
            }
            container.append(signals);
            const errors = stream.errors || {}, changes = stream.error_changes || {};
            const counter = key => value(errors[key]) + (typeof changes[key] === 'number' ? ' (+' + changes[key] + ' since sample)' : '');
            const details = node('dl', undefined, 'facts');
            facts(details, [['Stream bitrate', rate(stream.bitrate_bps)], ['Subscriptions', value(stream.subscriptions)], ['Continuity errors', counter('cc')], ['Transport errors', counter('te')], ['Uncorrected blocks', counter('unc')]]);
            container.append(details); receptionTarget.append(container);
        }
        const networkTarget = byId('network-content'); networkTarget.replaceChildren(); networkTarget.className = '';
        if (!network.interfaces?.length) networkTarget.append(node('p', 'Network interfaces are not reported.', 'empty'));
        for (const item of network.interfaces || []) {
            const container = node('div', undefined, 'network-interface');
            const heading = node('h3', item.name + (item.default_route ? ' · Default route' : ''));
            heading.append(node('span', item.link, 'badge')); container.append(heading);
            const entries = [['IP addresses', item.addresses?.join(' · ') || 'No address'], ['Link speed', value(item.speed_mbps, ' Mbit/s')], ['Receive / send', rate(item.rx_bps) + ' / ' + rate(item.tx_bps)], ['RX / TX errors', value(item.rx_errors) + ' / ' + value(item.tx_errors)], ['RX / TX dropped', value(item.rx_dropped) + ' / ' + value(item.tx_dropped)]];
            if (item.wifi) entries.push(['Wi-Fi', item.wifi.ssid || item.wifi.state], ['Wi-Fi signal', value(item.wifi.signal_dbm, ' dBm')], ['Frequency', value(item.wifi.frequency_mhz, ' MHz')], ['Wi-Fi TX link rate', value(item.wifi.tx_rate_mbps, ' Mbit/s')]);
            const details = node('dl', undefined, 'facts'); facts(details, entries); container.append(details); networkTarget.append(container);
        }
    }
    byId('open-performance').onclick = () => {
        const dialog = byId('performance-dialog');
        if (dialog.open) return;
        dialog.showModal();
        renderPerformance(telemetryData || {});
        void refreshTelemetry();
    };
    byId('close-performance').onclick = () => byId('performance-dialog').close();
    byId('performance-dialog').onclose = () => {
        byId('performance-content').replaceChildren();
        byId('performance-processes').replaceChildren();
        put('performance-note', '');
        if (page === 'status') byId('open-performance').focus({preventScroll: true});
    };
    async function refreshTelemetry() {
        if (telemetryPending || document.hidden || page !== 'status') return;
        telemetryPending = true;
        try {
            telemetryData = await api('/api/telemetry', 'GET', undefined, new AbortController(), 15000);
            renderTelemetry(telemetryData); put('telemetry-updated', 'Auto-refresh · every 5 seconds');
        } catch (_) {
            put('telemetry-updated', 'Measurements unavailable · retrying');
            if (telemetryData) {
                for (const name of ['system', 'network', 'reception', 'performance']) if (telemetryData[name]) telemetryData[name].state = 'stale';
                renderTelemetry(telemetryData);
            }
        } finally { telemetryPending = false; }
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshTelemetry(); });
    setInterval(() => { if (telemetryData && page === 'status') renderTelemetry(telemetryData); void refreshTelemetry(); }, 5000);
    navigate(); window.renderWatchChannels();
})();

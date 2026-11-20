let catalogue = [], favourites = [], dirty = false;
let busy = false, requesting = false, recovering = false, connected = false;
let baseRevision = null, lastLibraryRevision = null, favouritesConflict = false;
let lastRevision = null, lastState = '', lastScan = {state: 'idle'};
let drag = null, saving = false, pendingSave = null;
let loadSequence = 0, editRevision = 0;
let previewable = [], previewSession = null, previewUrl = null, hls = null, previewAttached = false;
let previewState = {state: 'idle', enabled: false};
const element = id => document.getElementById(id);
const activeStates = new Set(['starting', 'pausing', 'running', 'stopping']);

function message(text) { element('message').textContent = text; }
function saveMessage(text) { message(text); element('save-status').textContent = text; }
function previewButton(channel) {
    const node = button('', () => startPreview(channel), `Watch live: ${channel.name}`);
    node.classList.add('preview-channel'); node.dataset.service = channel.service_uuid;
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M8 2l4 4 4-4M3 6h18v14H3zM9 10l6 3-6 3zM8 23h8M12 20v3');
    icon.append(path); node.append(icon);
    if (!previewable.includes(channel.service_uuid)) node.title = 'Save favourites before previewing this channel';
    return node;
}
function detachPreview() {
    if (hls) { hls.destroy(); hls = null; }
    const video = element('preview-video');
    video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
    previewAttached = false;
}
async function stopPreview() {
    const session = previewSession;
    detachPreview(); previewSession = null; previewUrl = null;
    if (!session) return;
    requesting = true; controls();
    try {
        await api('/api/preview/stop', 'POST', {session});
        previewState = {state: 'stopped', enabled: previewState.enabled};
        element('preview-status').textContent = 'Streaming stopped. HDMI playback resumes automatically.';
    }
    catch (error) { message(error.message + ' An abandoned preview stops automatically within 20 seconds.'); }
    finally { requesting = false; controls(); }
}
async function startPreview(channel) {
    if (busy || requesting || recovering || !connected || previewSession) return;
    const video = element('preview-video');
    if (!video.canPlayType('application/vnd.apple.mpegurl') && !(window.Hls && Hls.isSupported())) {
        message('This browser does not support Live TV. Try a current Safari, Chrome, Edge or Firefox.'); return;
    }
    requesting = true; controls(); message('');
    try {
        const data = await api('/api/preview', 'POST', {service: channel.service_uuid, confirm_pause: true});
        previewSession = data.session; previewUrl = data.url;
        previewState = {state: 'starting', enabled: true, channel_name: channel.name};
        if (window.navigateReceiver) window.navigateReceiver('live');
        element('preview-section').scrollIntoView({behavior: 'smooth', block: 'start'});
        element('preview-status').textContent = `Preparing ${channel.name}…`;
        void previewHeartbeat(previewSession);
    } catch (error) { message(error.message); }
    finally { requesting = false; controls(); }
}
async function showPreview(state) {
    previewState = state;
    const active = ['starting', 'ready'].includes(state.state);
    element('preview-status').textContent = !state.enabled ? 'Live TV is not configured.' :
        state.state === 'starting' ? `Preparing ${state.channel_name}…` :
        state.state === 'ready' ? '' :
        state.error || state.reason || '';
    if (active) element('tv-status').textContent = 'TV paused for browser streaming.';
    if (!active && previewSession) { detachPreview(); previewSession = null; previewUrl = null; }
    if (state.state === 'ready' && previewSession && !previewAttached) {
        previewAttached = true;
        const video = element('preview-video'); video.hidden = false;
        // Some desktop Chrome builds advertise native HLS but never load it.
        // Prefer the tested MediaSource path; native HLS remains the fallback.
        if (window.Hls && Hls.isSupported()) {
            hls = new Hls({maxBufferLength: 10, backBufferLength: 10});
            hls.on(Hls.Events.ERROR, (_event, data) => {
                if (data.fatal) { message('Live TV playback failed. Try starting it again.'); void stopPreview(); }
            });
            hls.loadSource(previewUrl); hls.attachMedia(video);
        } else video.src = previewUrl;
        // Browsers may reject unmuted autoplay after asynchronous preparation.
        video.play().catch(() => {});
    }
    controls();
}
async function previewHeartbeat(session) {
    if (session !== previewSession) return;
    try {
        const state = await api('/api/preview/heartbeat', 'POST', {session}, new AbortController(), 5000);
        if (session === previewSession) await showPreview(state);
    } catch (error) {
        if (session === previewSession && error.status === 410) {
            detachPreview(); previewSession = null; previewUrl = null;
            element('preview-status').textContent = 'Streaming stopped. Check receiver activity or choose a channel to watch again.';
            controls();
        }
    } finally {
        if (session === previewSession) setTimeout(() => previewHeartbeat(session), 3000);
    }
}
const api = receiverRequest;
async function load(resetFavourites = false) {
    const sequence = ++loadSequence, revision = editRevision;
    const data = await api('/api/channels');
    // A poll started before Save must not replace the newer permissions/order.
    if (sequence !== loadSequence) return;
    previewable = data.previewable || [];
    // Keep names and local edits if a refreshed catalogue omits an old favourite.
    const selected = new Set(favourites);
    catalogue = [...data.channels, ...catalogue.filter(c => selected.has(c.service_uuid) && !data.channels.some(n => n.service_uuid === c.service_uuid))];
    if ((resetFavourites || (!dirty && !saving)) && revision === editRevision && !drag) {
        favourites = data.favourites.slice(); dirty = false;
        baseRevision = data.library_revision || baseRevision; favouritesConflict = false;
    } else if (!baseRevision) baseRevision = data.library_revision || null;
    else if (!saving && data.library_revision && data.library_revision !== baseRevision) {
        favouritesConflict = true;
        saveMessage('Favourites changed in another browser. Your edits are kept. Reload saved favourites before editing again.');
    }
    lastLibraryRevision = data.library_revision || lastLibraryRevision;
    render();
}
function button(label, action, title) {
    const node = document.createElement('button');
    node.textContent = label; node.onclick = action;
    if (title) { node.title = title; node.setAttribute('aria-label', title); }
    return node;
}
function row(name) {
    const node = document.createElement('li'), label = document.createElement('span');
    label.textContent = name; node.append(label); return node;
}
function removeButton(name, action) {
    const node = button('', action, `Remove ${name} from favourites`);
    node.classList.add('remove-channel'); node.classList.add('channel-edit');
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7');
    icon.append(path); node.append(icon); return node;
}
function editsBlocked() {
    return requesting || recovering || !connected || ['starting', 'ready'].includes(previewState.state);
}
function edit(action) { if (drag || editsBlocked()) return; action(); editRevision++; dirty = true; render(); }
function beginDrag(event, node) {
    if (event.button !== 0 || drag || editsBlocked()) return;
    // Capturing on the moved handle loses capture when insertBefore/append
    // reparents its row. The list stays connected throughout the gesture.
    const captureTarget = element('favourites');
    drag = {node, captureTarget, pointer: event.pointerId, startY: event.clientY, moved: false};
    captureTarget.setPointerCapture(event.pointerId);
    controls();
}
function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (!drag.moved && Math.abs(event.clientY - drag.startY) < 5) return;
    drag.moved = true;
    drag.node.classList.add('dragging');
    event.preventDefault();
    const list = element('favourites');
    for (const target of list.children) {
        if (target === drag.node) continue;
        const bounds = target.getBoundingClientRect();
        if (event.clientY < bounds.top + bounds.height / 2) {
            list.insertBefore(drag.node, target);
            return;
        }
    }
    list.append(drag.node);
}
function endDrag(event, cancelled = false) {
    if (!drag || event.pointerId !== drag.pointer) return;
    const current = drag; drag = null;
    current.node.classList.remove('dragging');
    if (current.captureTarget.hasPointerCapture(current.pointer)) current.captureTarget.releasePointerCapture(current.pointer);
    if (!cancelled && current.moved) {
        const order = Array.from(element('favourites').children, node => node.dataset.service);
        if (JSON.stringify(order) !== JSON.stringify(favourites)) {
            favourites = order; editRevision++; dirty = true;
            message('Favourites reordered. Click “Save favourites” to apply the new order.');
        }
    }
    render();
}
function render() {
    // Keep the dragged row and its visual position intact during live updates.
    if (drag) return;
    const available = element('available'), ordered = element('favourites');
    available.replaceChildren(); ordered.replaceChildren();
    const query = element('search').value.toLowerCase();
    for (const channel of catalogue) {
        if (!favourites.includes(channel.service_uuid) && channel.name.toLowerCase().includes(query)) {
            const node = row(channel.name);
            if (previewable.includes(channel.service_uuid)) node.append(previewButton(channel));
            const add = button('Add', () => edit(() => favourites.push(channel.service_uuid)));
            add.classList.add('channel-edit'); node.append(add); available.append(node);
        }
    }
    favourites.forEach((id, index) => {
        const channel = catalogue.find(c => c.service_uuid === id);
        const name = channel ? channel.name : 'Previously selected channel';
        const node = row(`${index + 1}. ${name}`);
        if (channel) node.append(previewButton(channel));
        node.dataset.service = id;
        const handle = button('⋮⋮', () => {}, `Drag ${name} to reorder`);
        handle.classList.add('drag-handle'); handle.classList.add('channel-edit');
        handle.onpointerdown = event => beginDrag(event, node);
        node.prepend(handle);
        const up = button('↑', () => edit(() => { [favourites[index - 1], favourites[index]] = [favourites[index], favourites[index - 1]]; }), `Move ${name} up`);
        const down = button('↓', () => edit(() => { [favourites[index + 1], favourites[index]] = [favourites[index], favourites[index + 1]]; }), `Move ${name} down`);
        up.classList.add('channel-edit'); down.classList.add('channel-edit');
        up.dataset.limit = String(index === 0); down.dataset.limit = String(index + 1 === favourites.length);
        node.append(up, down, removeButton(name, () => edit(() => favourites.splice(index, 1)))); ordered.append(node);
    });
    element('channel-count').textContent = `(${catalogue.length} known)`;
    element('empty-channels').hidden = available.children.length > 0;
    element('empty-channels').textContent = query ? 'No channels match your filter.' : catalogue.length ? 'All known channels are already in your favourites.' : 'No channels found yet. Start a search to find channels.';
    element('unsaved').hidden = !dirty;
    if (window.renderWatchChannels) window.renderWatchChannels();
    controls();
}
function controls() {
    const previewActive = ['starting', 'ready'].includes(previewState.state);
    const blocked = busy || requesting || recovering || !connected || !!drag || previewActive;
    document.querySelectorAll('.preview-channel').forEach(node => {
        node.disabled = blocked || !previewState.enabled || !previewable.includes(node.dataset.service);
    });
    document.querySelectorAll('.channel-edit').forEach(node => {
        node.disabled = editsBlocked() || node.dataset.limit === 'true';
    });
    element('stop-preview').disabled = !previewSession || requesting || !connected;
    element('start-scan').disabled = blocked;
    element('short').disabled = blocked;
    element('cancel-scan').disabled = !busy || requesting || lastScan.state === 'stopping' || !connected;
    element('recover-scan').hidden = !recovering;
    element('recover-scan').disabled = busy || requesting || !connected;
    element('recovery-help').hidden = !recovering;
    element('refresh-channels').disabled = blocked;
    element('save-favourites').disabled = blocked || !favourites.length || favouritesConflict;
    element('reload-favourites').hidden = !favouritesConflict;
    element('reload-favourites').disabled = blocked;
    element('save-favourites').textContent = saving ? 'Saving…' : 'Save favourites';
    if (window.receiverControls) window.receiverControls();
}
function showStatus(scan, player) {
    lastScan = scan; busy = activeStates.has(scan.state); recovering = !!scan.recovery_pending;
    const found = scan.found || 0, completed = scan.completed || 0, total = scan.total || 0;
    const failed = scan.failed_muxes || 0;
    const partial = scan.partial_muxes || 0;
    const labels = {idle: 'Ready to search for channels.', starting: 'Starting channel search…',
        pausing: 'Pausing TV playback before the search…', stopping: 'Stopping search and restoring TV playback…',
        running: `Searching transponder ${Math.min(completed + 1, total)} of ${total} · ${found} channels found`,
        complete: `Search finished · ${found} supported channels found.`,
        cancelled: `Search stopped · ${found} channels found so far.`,
        error: recovering ? 'Search interrupted. TV playback needs to be restored.' : 'The channel search could not finish. Check the satellite receiver and try again.'};
    element('scan-status').textContent = labels[scan.state] || 'Checking channel search…';
    const progress = element('scan-progress');
    progress.hidden = !busy;
    if (scan.state === 'running' && total > 0) { progress.max = total; progress.value = completed; }
    else { progress.removeAttribute('value'); }
    element('scan-detail').textContent = busy && total ? `${completed} of ${total} transponders searched. New channels appear on the Channels page after each transponder finishes.` :
        scan.state === 'complete' || scan.state === 'cancelled' ?
            `${completed} of ${total} transponders searched.${partial ? ` ${partial} completed partially; usable channels were kept.` : ''}${failed ? ` ${failed} could not be scanned.` : ''} Channels found so far have been kept; your saved favourites are unchanged.` : '';
    element('tv-status').textContent = recovering ? 'TV paused — tuner settings need to be restored.' :
        busy ? 'TV paused for channel search.' : player.error ? 'TV playback is unavailable. Check the receiver or restart the TV service.' :
        player.paused ? 'TV paused. Waiting for playback to resume…' : player.loading ? 'TV is changing channel…' :
        player.playing ? `TV playing${player.channel_name ? ': ' + player.channel_name : ''}` : 'Waiting for TV playback…';
    controls();
}
async function scan() {
    if (busy || requesting || recovering || !connected) return;
    if (!confirm('TV playback will pause while searching for channels. A full search can take many minutes. Continue?')) return;
    requesting = true; message(''); showStatus({state: 'starting'}, {});
    try { await api('/api/scan', 'POST', {enabled_only: element('short').checked, confirm_pause: true}); }
    catch (error) { busy = false; message(error.message); }
    finally { requesting = false; controls(); }
}
async function stop() {
    requesting = true; showStatus({...lastScan, state: 'stopping'}, {});
    try { await api('/api/scan/cancel', 'POST', {}); message('Search stopped. Channels already found have been kept.'); }
    catch (error) { message(error.message); } finally { requesting = false; controls(); }
}
async function recover() {
    requesting = true; controls(); message('Restoring tuner settings and TV playback…');
    try { await api('/api/scan/recover', 'POST', {}); message('Tuner settings restored. TV playback will resume automatically.'); }
    catch (_) { message('TV playback could not be restored. Check the satellite receiver connection, then try again.'); }
    finally { requesting = false; controls(); }
}
async function refresh() {
    requesting = true; controls(); message('Reloading saved channels…');
    try { await api('/api/refresh', 'POST', {}); await load(); message('Saved channels reloaded. Your unsaved favourites edits have been kept.'); }
    catch (error) { message(error.message); } finally { requesting = false; controls(); }
}
async function save() {
    if (busy || editsBlocked() || drag || !favourites.length || favouritesConflict) return;
    // getRandomValues also works on the login-free HTTP LAN interface.
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    const submitted = favourites.slice();
    const operation = {id, submitted, acknowledged: false, controller: new AbortController()};
    pendingSave = operation;
    requesting = true; saving = true; controls();
    saveMessage('Saving favourites… Waiting for the receiver to confirm.');
    let saved = false;
    try {
        const result = await api('/api/favourites', 'PUT', {services: submitted, save_id: id, base_revision: baseRevision}, operation.controller);
        baseRevision = result.library_revision || baseRevision;
        saved = true;
        if (pendingSave === operation) pendingSave = null;
        dirty = JSON.stringify(submitted) !== JSON.stringify(favourites);
        // Mapping a new favourite changes preview permissions. Refresh metadata
        // without resetting the displayed order or any local edits.
        await load();
        saveMessage(dirty ? 'Favourites saved. You have additional unsaved changes.' : 'Favourites saved. TV will use the new order automatically.');
    }
    catch (error) {
        // The status poll may have confirmed this save and cancelled its stuck
        // response. Do not replace that confirmation with a timeout message.
        if (error.status === 409) { favouritesConflict = true; }
        if (!operation.acknowledged) saveMessage(saved ? 'Favourites saved, but channel details could not be refreshed. Click “Reload saved channels” to retry.' : error.message + ' Your local order has been kept. If the connection failed, the save may still have reached the receiver.');
    }
    finally { requesting = false; saving = false; render(); }
}
async function poll() {
    try {
        const sessionAtRequest = previewSession;
        const data = await api('/api/status');
        const reconnected = !connected; connected = true;
        if (pendingSave && data.last_favourites_save_id === pendingSave.id) {
            const operation = pendingSave; pendingSave = null;
            operation.acknowledged = true;
            baseRevision = data.library_revision || baseRevision;
            dirty = JSON.stringify(operation.submitted) !== JSON.stringify(favourites);
            saveMessage(dirty ? 'Favourites saved. You have additional unsaved changes.' : 'Favourites saved. TV will use the new order automatically.');
            operation.controller.abort();
            render();
        }
        if (data.catalogue_revision !== lastRevision || data.scan.state !== lastState || data.library_revision !== lastLibraryRevision || reconnected) {
            await load(lastRevision === null && !dirty);
            lastRevision = data.catalogue_revision; lastState = data.scan.state;
        }
        showStatus(data.scan, data.player);
        // Ignore preview status requested before a concurrent Start/Stop.
        if (previewSession === sessionAtRequest) await showPreview(data.preview || {state: 'idle', enabled: false});
        if (window.receiverStatus) window.receiverStatus(data);
    } catch (_) {
        connected = false; controls();
        element('scan-status').textContent = 'Connection to the receiver lost. Reconnecting…';
        element('tv-status').textContent = 'TV status is temporarily unavailable.';
        if (window.receiverOffline) window.receiverOffline();
    } finally { setTimeout(poll, 1500); }
}
element('start-scan').onclick = scan; element('cancel-scan').onclick = stop;
element('stop-preview').onclick = stopPreview;
element('preview-video').onerror = () => {
    if (previewSession) { message('The browser could not play this stream.'); void stopPreview(); }
};
window.addEventListener('pagehide', () => {
    if (previewSession) fetch('/api/preview/stop', {method: 'POST', keepalive: true,
        headers: {'Content-Type': 'application/json', 'X-Satip-Action': '1'},
        body: JSON.stringify({session: previewSession})}).catch(() => {});
});
element('recover-scan').onclick = recover; element('refresh-channels').onclick = refresh;
element('reload-favourites').onclick = async () => {
    if (dirty && !confirm('Discard your local edits and load the latest saved favourites?')) return;
    requesting = true; controls();
    try { await load(true); saveMessage('Latest saved favourites loaded.'); }
    catch (error) { saveMessage(error.message); }
    finally { requesting = false; controls(); }
};
element('save-favourites').onclick = save; element('search').oninput = render;
element('favourites').onpointermove = moveDrag;
element('favourites').onpointerup = event => endDrag(event);
element('favourites').onpointercancel = event => endDrag(event, true);
element('favourites').onlostpointercapture = event => {
    // Ignore capture changes bubbling from a child (e.g. implicit touch capture).
    if (event.target !== element('favourites') || !drag || event.pointerId !== drag.pointer) return;
    // A handoff artifact can drop capture before the first move; re-acquire so the
    // gesture survives on slow or touch devices instead of cancelling prematurely.
    if (!drag.moved) { drag.captureTarget.setPointerCapture(drag.pointer); return; }
    endDrag(event, true);
};
controls(); poll();

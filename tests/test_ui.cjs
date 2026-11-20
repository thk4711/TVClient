// Lightweight DOM/API fakes: no browser packages, receiver, or tuner required.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
const source = fs.readFileSync('satip_controller/static/ui.js', 'utf8');

async function fixture() {
    const nodes = new Map();
    function node() {
        const classes = new Set(), captures = new Set();
        return {children: [], dataset: {}, value: '', textContent: '', hidden: false, disabled: false,
            classList: {add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value)},
            setPointerCapture: id => captures.add(id), hasPointerCapture: id => captures.has(id), releasePointerCapture: id => captures.delete(id),
            append(...items) { for (const item of items) { this.children = this.children.filter(c => c !== item); this.children.push(item); item.parent = this; } },
            prepend(item) { this.children.unshift(item); item.parent = this; },
            insertBefore(item, target) { this.children = this.children.filter(c => c !== item); this.children.splice(this.children.indexOf(target), 0, item); },
            getBoundingClientRect() { return {top: this.parent.children.indexOf(this) * 50, height: 50}; },
            replaceChildren() { this.children = []; },
            pause() {}, load() {}, play() { return Promise.resolve(); },
            canPlayType() { return 'maybe'; }, scrollIntoView() {},
            setAttribute(key, value) { this[key] = value; },
            removeAttribute(key) { delete this[key]; }};
    }
    for (const match of html.matchAll(/id="([^"]+)"/g)) nodes.set(match[1], node());
    const state = {scan: {state: 'idle'}, player: {playing: 'a', channel_name: 'Das Erste HD'}, catalogue_revision: 0};
    const channels = {channels: [{service_uuid: 'a', name: 'Das Erste HD'}], favourites: ['a']};
    const calls = [];
    const context = vm.createContext({document: {
        getElementById(id) { assert.ok(nodes.has(id), 'Missing HTML element: ' + id); return nodes.get(id); },
        querySelectorAll(selector) {
            return [...nodes.values()].flatMap(n => n.children).flatMap(n => n.children || []).filter(n => n.classList.contains(selector.slice(1)));
        },
        createElement: node, createElementNS: node}, window: {addEventListener() {}}, confirm: () => true, crypto: require('node:crypto').webcrypto, Uint8Array, AbortController, setTimeout: () => {}, clearTimeout: () => {},
        fetch: async (path, options) => {
            calls.push({path, options});
            return {ok: true, json: async () => path === '/api/status' ? state : path === '/api/channels' ? channels :
                path === '/api/preview' ? {session: 'a'.repeat(32), url: '/api/preview/' + 'a'.repeat(32) + '/index.m3u8'} :
                path === '/api/preview/heartbeat' ? {state:'starting', enabled:true, channel_name:'Das Erste HD'} : {}};
        }});
    vm.runInContext(fs.readFileSync('satip_controller/static/request.js', 'utf8'), context);
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    return {context, state, channels, calls, get: id => nodes.get(id), run: code => vm.runInContext(code, context)};
}

test('saving an added and dragged channel refreshes preview permissions without reloading', async () => {
    const f = await dragFixture();
    f.channels.previewable = ['a','b','c'];
    f.channels.channels.push({service_uuid:'sat1',name:'SAT.1'});
    await f.run('load()');
    await f.run('showPreview({state:"idle",enabled:true})');
    f.get('available').children[0].children.at(-1).onclick();
    const list = f.get('favourites');
    list.children[3].children[0].onpointerdown(pointer(170));
    list.onpointermove(pointer(5)); list.onpointerup(pointer(5));
    assert.deepEqual(list.children.map(n => n.dataset.service), ['sat1','a','b','c']);
    const original = f.context.fetch;
    f.context.fetch = async (path, options) => {
        if (path === '/api/favourites') {
            f.channels.favourites = JSON.parse(options.body).services;
            f.channels.previewable = f.channels.favourites.slice();
        }
        return original(path, options);
    };
    await f.run('save()');
    assert.deepEqual(list.children.map(n => n.dataset.service), ['sat1','a','b','c']);
    assert.equal(f.get('unsaved').hidden, true);
    for (const row of list.children) assert.equal(row.children.find(n => n.classList.contains('preview-channel')).disabled, false);
    await f.run('poll()');
    assert.deepEqual(list.children.map(n => n.dataset.service), ['sat1','a','b','c']);
});

test('save prevents duplicate submissions and dragging until completion', async () => {
    const f = await dragFixture();
    let resolveSave; const original = f.context.fetch;
    f.context.fetch = async (path, options) => path === '/api/favourites' ?
        new Promise(resolve => { resolveSave = resolve; }) : original(path, options);
    const saving = f.run('save()');
    const handle = f.get('favourites').children[0].children[0];
    handle.onpointerdown(pointer(10));
    assert.equal(f.run('!!drag'), false);
    const duplicate = f.run('save()');
    resolveSave({ok:true,json:async()=>({})});
    await Promise.all([saving, duplicate]);
    assert.equal(f.run('requesting'), false);
    assert.equal(f.get('save-favourites').disabled, false);
});

test('failed save retains edits and restores preview and edit controls', async () => {
    const f = await dragFixture();
    f.channels.previewable = ['a','b','c'];
    await f.run('load()'); await f.run('showPreview({state:"idle",enabled:true})');
    f.run('edit(() => favourites.reverse())');
    const original = f.context.fetch;
    f.context.fetch = async (path, options) => path === '/api/favourites' ?
        {ok:false,json:async()=>({detail:'Save failed'})} : original(path, options);
    await f.run('save()');
    assert.deepEqual(f.get('favourites').children.map(n=>n.dataset.service), ['c','b','a']);
    assert.equal(f.get('unsaved').hidden, false);
    assert.equal(f.get('save-favourites').disabled, false);
    for (const row of f.get('favourites').children) assert.equal(row.children.find(n=>n.classList.contains('preview-channel')).disabled, false);
});

test('metadata refresh failure is distinguished from a failed save', async () => {
    const f = await dragFixture();
    f.run('edit(() => favourites.reverse())');
    const original = f.context.fetch;
    f.context.fetch = async (path, options) => {
        if (path === '/api/channels') throw Error('offline');
        return original(path, options);
    };
    await f.run('save()');
    assert.match(f.get('message').textContent, /Favourites saved, but channel details/);
    assert.equal(f.get('unsaved').hidden, true);
    assert.equal(f.get('save-favourites').disabled, false);
});

test('a stalled save times out and restores controls without a reload', async () => {
    const f = await dragFixture();
    f.channels.previewable = ['a','b','c'];
    await f.run('load()'); await f.run('showPreview({state:"idle",enabled:true})');
    f.run('edit(() => favourites.reverse())');
    let expire, cleared = false;
    f.context.setTimeout = (callback, milliseconds) => { assert.equal(milliseconds,30000); expire=callback; return 1; };
    f.context.clearTimeout = () => { cleared=true; };
    f.context.fetch = async (_path, options) => new Promise((_resolve,reject) => {
        options.signal.addEventListener('abort',()=>reject(Error('aborted')));
    });
    const saving = f.run('save()');
    assert.equal(f.get('save-favourites').textContent, 'Saving…');
    assert.equal(f.get('save-favourites').disabled, true);
    assert.match(f.get('save-status').textContent, /Saving favourites/);
    expire(); await saving;
    assert.equal(cleared,true);
    assert.equal(f.run('requesting'),false);
    assert.equal(f.get('save-favourites').disabled,false);
    assert.equal(f.get('save-favourites').textContent,'Save favourites');
    assert.match(f.get('save-status').textContent, /timed out/);
    assert.equal(f.get('unsaved').hidden,false);
    assert.deepEqual(f.get('favourites').children.map(n=>n.dataset.service), ['c','b','a']);
    for (const row of f.get('favourites').children) assert.equal(row.children.find(n=>n.classList.contains('preview-channel')).disabled,false);
});

test('status confirms a committed save whose original response stalls', async () => {
    const f=await dragFixture();
    f.state.preview={state:'idle',enabled:true};
    await f.run('showPreview({state:"idle",enabled:true})');
    f.run('edit(() => favourites.reverse())');
    let token; const original=f.context.fetch;
    f.context.fetch=async (path,options)=>{
        if(path==='/api/favourites') {
            const payload=JSON.parse(options.body); token=payload.save_id;
            assert.match(token,/^[0-9a-f]{32}$/);
            f.channels.favourites=payload.services; f.channels.previewable=payload.services;
            return new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('aborted'))));
        }
        return original(path,options);
    };
    const saving=f.run('save()');
    f.state.last_favourites_save_id='f'.repeat(32);
    await f.run('poll()');
    assert.equal(f.run('requesting'),true, 'Another request acknowledgement must not confirm this save');
    f.state.last_favourites_save_id=token; f.state.catalogue_revision++;
    await f.run('poll()'); await saving;
    assert.equal(f.run('requesting'),false);
    assert.equal(f.get('unsaved').hidden,true);
    assert.equal(f.get('save-favourites').disabled,false);
    assert.match(f.get('save-status').textContent,/Favourites saved/);
    assert.doesNotMatch(f.get('save-status').textContent,/timed out/);
    assert.deepEqual(f.get('favourites').children.map(n=>n.dataset.service),['c','b','a']);
    for(const row of f.get('favourites').children) assert.equal(row.children.find(n=>n.classList.contains('preview-channel')).disabled,false);
});

test('late save acknowledgement retains newer unsaved edits', async () => {
    const f=await dragFixture();
    let expire, token; const original=f.context.fetch;
    f.context.setTimeout=(callback)=>{expire=callback;};
    f.context.fetch=async(path,options)=>{
        if(path==='/api/favourites') {
            token=JSON.parse(options.body).save_id;
            return new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('aborted'))));
        }
        return original(path,options);
    };
    const saving=f.run('save()'); expire(); await saving;
    f.run('edit(() => favourites.reverse())');
    f.state.last_favourites_save_id=token;
    await f.run('poll()');
    assert.deepEqual(f.get('favourites').children.map(n=>n.dataset.service),['c','b','a']);
    assert.equal(f.get('unsaved').hidden,false);
    assert.match(f.get('save-status').textContent,/additional unsaved changes/);
});

test('available channels show preview only when already mapped', async () => {
    const f = await fixture();
    f.channels.channels.push({service_uuid:'b',name:'New channel'}, {service_uuid:'c',name:'Previously saved'});
    f.channels.previewable=['a','c'];
    await f.run('load()'); await f.run('showPreview({state:"idle",enabled:true})');
    const rows=f.get('available').children;
    assert.equal(rows[0].children.some(n=>n.classList.contains('preview-channel')),false);
    assert.equal(rows[1].children.find(n=>n.classList.contains('preview-channel')).disabled,false);
    assert.match(html,/Save a channel as a favourite before watching/);
});

test('late channel response cannot remove newly refreshed preview permissions', async () => {
    const f = await fixture();
    f.channels.previewable = ['a'];
    let resolveOld; const original = f.context.fetch;
    f.context.fetch = async (path, options) => path === '/api/channels' ?
        new Promise(resolve => { resolveOld = resolve; }) : original(path, options);
    const oldLoad = f.run('load()');
    f.context.fetch = original;
    await f.run('load()');
    await f.run('showPreview({state:"idle",enabled:true})');
    resolveOld({ok:true,json:async()=>({channels:f.channels.channels,favourites:['a'],previewable:[]})});
    await oldLoad;
    assert.equal(f.get('favourites').children[0].children.find(n=>n.classList.contains('preview-channel')).disabled, false);
});

test('initial channel reload cannot overwrite edits made while it is pending', async () => {
    const f = await dragFixture();
    let resolveLoad; const original = f.context.fetch;
    f.context.fetch = async (path, options) => path === '/api/channels' ?
        new Promise(resolve => { resolveLoad = resolve; }) : original(path, options);
    const loading = f.run('load(true)');
    f.run('edit(() => favourites.reverse())');
    resolveLoad({ok:true,json:async()=>f.channels});
    await loading;
    assert.deepEqual(f.get('favourites').children.map(n=>n.dataset.service), ['c','b','a']);
    assert.equal(f.get('unsaved').hidden, false);
});

test('partial scans are distinguished from failures and keep usable channels', async () => {
    const f = await fixture();
    await f.run('showStatus({state:"complete",completed:87,total:87,found:120,partial_muxes:4,failed_muxes:1},{playing:"a"})');
    assert.match(f.get('scan-detail').textContent, /4 completed partially; usable channels were kept/);
    assert.match(f.get('scan-detail').textContent, /1 could not be scanned/);
    assert.match(f.get('scan-detail').textContent, /saved favourites are unchanged/);
});

test('idle status is readable, with safe controls and no raw JSON', async () => {
    const f = await fixture();
    assert.equal(f.get('tv-status').textContent, 'TV playing: Das Erste HD');
    assert.equal(f.get('scan-status').textContent, 'Ready to search for channels.');
    assert.equal(f.get('cancel-scan').disabled, true);
    assert.equal(f.get('recover-scan').hidden, true);
    assert.equal(f.get('scan-progress').hidden, true);
});

test('Live TV omits decorative idle controls and places Stop before player details', () => {
    const player = html.slice(html.indexOf('id="preview-section"'), html.indexOf('<article class="card watch-list"'));
    assert.doesNotMatch(player, /play-symbol|Ready when you are/);
    assert.match(player, /Choose a saved channel to start watching/);
    assert.ok(player.indexOf('id="stop-preview"') < player.indexOf('class="player-info"'));
});

test('ready preview removes redundant status while preparation and errors remain useful', async () => {
    const f = await fixture();
    await f.run('showPreview({state:"idle",enabled:true})');
    assert.equal(f.get('preview-status').textContent, '');
    await f.run('showPreview({state:"starting",enabled:true,channel_name:"VOX"})');
    assert.equal(f.get('preview-status').textContent, 'Preparing VOX…');
    await f.run('showPreview({state:"ready",enabled:true,channel_name:"VOX"})');
    assert.equal(f.get('preview-status').textContent, '');
    await f.run('showPreview({state:"failed",enabled:true,error:"Tuner unavailable"})');
    assert.equal(f.get('preview-status').textContent, 'Tuner unavailable');
});

test('preview starts without a popup, keeps a heartbeat and blocks search', async () => {
    const f = await fixture();
    f.context.confirm = () => { throw Error('Preview must not show a confirmation popup'); };
    f.channels.previewable = ['a'];
    f.state.preview = {state: 'idle', enabled: true};
    await f.run('load(true); showPreview({state:"idle",enabled:true})');
    await f.run('startPreview(catalogue[0])');
    const start = f.calls.find(call => call.path === '/api/preview');
    assert.equal(JSON.parse(start.options.body).confirm_pause, true);
    assert.equal(f.get('start-scan').disabled, true);
    assert.equal(f.get('stop-preview').disabled, false);
    f.state.preview = {state: 'ready', enabled: true, channel_name: 'Das Erste HD'};
    await f.run('poll()');
    assert.equal(f.get('preview-video').hidden, false);
    assert.match(f.get('preview-video').src, /index.m3u8$/);
    assert.ok(f.calls.some(call => call.path === '/api/preview/heartbeat'));
    await f.run('stopPreview()');
    assert.equal(f.get('preview-video').hidden, true);
    assert.ok(f.calls.some(call => call.path === '/api/preview/stop'));
});

test('expired preview clears the video and enables search again', async () => {
    const f = await fixture();
    await f.run('startPreview(catalogue[0])');
    await f.run('showPreview({state:"stopped",enabled:true,reason:"Timed out"})');
    assert.equal(f.get('preview-video').hidden, true);
    assert.equal(f.get('stop-preview').disabled, true);
    assert.equal(f.get('start-scan').disabled, false);
});

test('heartbeat is independent of a stalled status request and ignores an obsolete session', async () => {
    const f = await fixture();
    await f.run('startPreview(catalogue[0])');
    const original = f.context.fetch;
    let resolveHeartbeat, resolveStatus;
    f.context.fetch = async (path, options) => path === '/api/status' ?
        new Promise(resolve => { resolveStatus = resolve; }) : path === '/api/preview/heartbeat' ?
        new Promise(resolve => { resolveHeartbeat = resolve; }) : original(path, options);
    const polling = f.run('poll()');
    const heartbeat = f.run('previewHeartbeat(previewSession)');
    assert.equal(typeof resolveHeartbeat, 'function');
    f.run("previewSession = 'b'.repeat(32)");
    resolveHeartbeat({ok:false,status:410,json:async()=>({detail:'Expired'})});
    await heartbeat;
    assert.equal(f.run('previewSession'), 'b'.repeat(32));
    resolveStatus({ok:true,json:async()=>f.state});
    await polling;
    assert.equal(f.run('previewSession'), 'b'.repeat(32));
});

test('late status response cannot discard a newly started preview', async () => {
    const f = await fixture();
    let resolveStatus;
    const originalFetch = f.context.fetch;
    f.context.fetch = async (path, options) => path === '/api/status' ?
        new Promise(resolve => { resolveStatus = resolve; }) : originalFetch(path, options);
    const polling = f.run('poll()');
    await f.run('startPreview(catalogue[0])');
    resolveStatus({ok: true, json: async () => f.state});
    await polling;
    assert.equal(f.run('previewSession'), 'a'.repeat(32));
    assert.equal(f.get('stop-preview').disabled, false);
});

test('MediaSource is preferred even when the browser advertises native HLS', async () => {
    const f = await fixture();
    f.run('window.Hls = Hls = class { static isSupported() { return true; } static Events = {ERROR:"error"}; on() {} loadSource(url) { this.url=url; } attachMedia(video) { this.video=video; } destroy() {} }');
    await f.run('startPreview(catalogue[0])');
    await f.run('showPreview({state:"ready",enabled:true,channel_name:"Das Erste HD"})');
    assert.equal(f.run('!!hls'), true);
    assert.equal(f.get('preview-video').src, undefined);
    assert.equal(f.get('preview-video').hidden, false);
});

test('starting/pausing show indeterminate progress and running shows counters', async () => {
    const f = await fixture();
    for (const state of ['starting', 'pausing']) {
        f.run("showStatus({state:'" + state + "'}, {})");
        assert.equal(f.get('scan-progress').hidden, false);
        assert.equal(f.get('scan-progress').value, undefined);
        assert.equal(f.get('start-scan').disabled, true);
    }
    f.run("showStatus({state:'running',completed:7,total:87,found:24}, {})");
    assert.match(f.get('scan-status').textContent, /8 of 87 · 24 channels found/);
    assert.equal(f.get('scan-progress').value, 7);
    assert.equal(f.get('scan-progress').max, 87);
    assert.equal(f.get('cancel-scan').disabled, false);
});

test('progress poll grows catalogue without resetting local favourites edits', async () => {
    const f = await fixture();
    f.run("edit(() => favourites.splice(0,1))");
    f.channels.channels.push({service_uuid: 'b', name: 'ZDF HD'});
    f.state.catalogue_revision++;
    f.state.scan = {state: 'running', completed: 1, total: 3, found: 1};
    await f.run('poll()');
    assert.equal(f.get('available').children.length, 2);
    assert.equal(f.get('favourites').children.length, 0);
    assert.equal(f.get('unsaved').hidden, false);
    assert.equal(f.get('save-favourites').disabled, true);
});

test('reload during an active scan retains progress and saved favourites', async () => {
    const f = await fixture();
    f.run('lastRevision = null; lastState = ""');
    f.state.scan = {state: 'running', completed: 3, total: 8, found: 10};
    await f.run('poll()');
    assert.match(f.get('scan-status').textContent, /4 of 8/);
    assert.equal(f.get('favourites').children.length, 1);
    assert.equal(f.get('cancel-scan').disabled, false);
});

test('completion and cancellation show a retained-results summary', async () => {
    const f = await fixture();
    for (const state of ['complete', 'cancelled']) {
        f.run("showStatus({state:'" + state + "',completed:2,total:3,found:5,failed_muxes:1}, {})");
        assert.match(f.get('scan-status').textContent, /5/);
        assert.match(f.get('scan-detail').textContent, /1 could not be scanned/);
        assert.match(f.get('scan-detail').textContent, /have been kept/);
        assert.equal(f.get('scan-progress').hidden, true);
        assert.equal(f.get('cancel-scan').disabled, true);
    }
});

test('recovery is explained and offered only when needed', async () => {
    const f = await fixture();
    f.run("showStatus({state:'error',recovery_pending:true}, {})");
    assert.equal(f.get('recover-scan').hidden, false);
    assert.equal(f.get('recovery-help').hidden, false);
    assert.equal(f.get('start-scan').disabled, true);
    assert.match(f.get('tv-status').textContent, /need to be restored/);
    f.run("showStatus({state:'idle'}, {})");
    assert.equal(f.get('recover-scan').hidden, true);
});

test('connection errors have friendly text and disable operations', async () => {
    const f = await fixture();
    f.context.fetch = async () => { throw Error('technical detail'); };
    await f.run('poll()');
    assert.match(f.get('scan-status').textContent, /Reconnecting/);
    assert.equal(f.get('start-scan').disabled, true);
    assert.doesNotMatch(f.get('scan-status').textContent, /technical detail/);
});

test('starting gives immediate feedback, sends confirmation and prevents duplicates', async () => {
    const f = await fixture();
    await f.run('scan()');
    assert.match(f.get('scan-status').textContent, /Starting channel search/);
    assert.equal(f.get('start-scan').disabled, true);
    await f.run('scan()');
    const starts = f.calls.filter(call => call.path === '/api/scan');
    assert.equal(starts.length, 1);
    assert.equal(JSON.parse(starts[0].options.body).confirm_pause, true);
});

async function dragFixture() {
    const f = await fixture();
    f.channels.channels = ['a', 'b', 'c'].map(id => ({service_uuid: id, name: id.toUpperCase()}));
    f.channels.favourites = ['a', 'b', 'c'];
    await f.run('load(true)');
    return f;
}
function pointer(y, type = 'mouse', id = 1) {
    return {button: 0, pointerId: id, pointerType: type, clientY: y, preventDefault() {}};
}

test('mouse drag reorders downward, marks dirty and saves the displayed order', async () => {
    const f = await dragFixture();
    const handle = f.get('favourites').children[0].children[0];
    handle.onpointerdown(pointer(10));
    f.get('favourites').onpointermove(pointer(140));
    f.get('favourites').onpointerup(pointer(140));
    assert.deepEqual(f.get('favourites').children.map(n => n.dataset.service), ['b', 'c', 'a']);
    assert.equal(f.get('unsaved').hidden, false);
    await f.run('save()');
    const saved = f.calls.find(call => call.path === '/api/favourites');
    assert.deepEqual(JSON.parse(saved.options.body).services, ['b', 'c', 'a']);
});

test('touch drag moves upward and polling does not replace the captured row', async () => {
    const f = await dragFixture();
    const row = f.get('favourites').children[2], handle = row.children[0];
    handle.onpointerdown(pointer(120, 'touch'));
    f.state.catalogue_revision++;
    await f.run('poll()');
    assert.equal(f.get('favourites').children[2], row);
    f.get('favourites').onpointermove(pointer(5, 'touch'));
    f.get('favourites').onpointerup(pointer(5, 'touch'));
    assert.deepEqual(f.get('favourites').children.map(n => n.dataset.service), ['c', 'a', 'b']);
});

test('cancelled drag restores the original order without creating an unsaved edit', async () => {
    const f = await dragFixture();
    const handle = f.get('favourites').children[0].children[0];
    handle.onpointerdown(pointer(10));
    f.get('favourites').onpointermove(pointer(140));
    f.get('favourites').onpointercancel(pointer(140));
    assert.deepEqual(f.get('favourites').children.map(n => n.dataset.service), ['a', 'b', 'c']);
    assert.equal(f.get('unsaved').hidden, true);
});

test('small movements and unrelated pointers do not reorder favourites', async () => {
    const f = await dragFixture();
    const handle = f.get('favourites').children[0].children[0];
    handle.onpointerdown(pointer(10));
    f.get('favourites').onpointermove(pointer(140, 'touch', 2));
    f.get('favourites').onpointermove(pointer(12));
    f.get('favourites').onpointerup(pointer(12));
    assert.deepEqual(f.get('favourites').children.map(n => n.dataset.service), ['a', 'b', 'c']);
    assert.equal(f.get('unsaved').hidden, true);
});

test('capture stays on the stationary list across repeated row moves', async () => {
    const f = await dragFixture();
    const list = f.get('favourites'), row = list.children[0], handle = row.children[0];
    handle.onpointerdown(pointer(10));
    assert.equal(list.hasPointerCapture(1), true);
    assert.equal(handle.hasPointerCapture(1), false);
    list.onpointermove(pointer(140));
    assert.equal(list.hasPointerCapture(1), true);
    assert.equal(row.classList.contains('dragging'), true);
    list.onlostpointercapture({pointerId: 1, target: handle});
    assert.equal(row.classList.contains('dragging'), true);
    list.onpointermove(pointer(5));
    assert.equal(row.classList.contains('dragging'), true);
    list.onpointermove(pointer(140));
    list.onpointerup(pointer(140));
    assert.deepEqual(list.children.map(n => n.dataset.service), ['b', 'c', 'a']);
    assert.equal(list.hasPointerCapture(1), false);
});

test('trash icon has an accessible label and removes only the selected favourite', async () => {
    const f = await dragFixture();
    const remove = f.get('favourites').children[1].children.at(-1);
    assert.equal(remove.textContent, '');
    assert.equal(remove.title, 'Remove B from favourites');
    assert.equal(remove['aria-label'], 'Remove B from favourites');
    assert.equal(remove.children[0]['aria-hidden'], 'true');
    remove.onclick();
    assert.deepEqual(f.get('favourites').children.map(n => n.dataset.service), ['a', 'c']);
    assert.equal(f.get('unsaved').hidden, false);
});

test('TV preview icon retains channel tooltip, accessible name and action', async () => {
    const f = await fixture();
    f.channels.previewable = ['a'];
    await f.run('load(true)');
    await f.run('showPreview({state:"idle",enabled:true})');
    const preview = f.get('favourites').children[0].children.find(n => n.classList.contains('preview-channel'));
    assert.equal(preview.textContent, '');
    assert.equal(preview.title, 'Watch live: Das Erste HD');
    assert.equal(preview['aria-label'], 'Watch live: Das Erste HD');
    assert.equal(preview.children[0]['aria-hidden'], 'true');
    assert.equal(preview.children[0]['focusable'], 'false');
    assert.equal(preview.disabled, false);
    await preview.onclick();
    assert.ok(f.calls.some(call => call.path === '/api/preview'));
});

test('clean editor follows another browser save even across web restart', async () => {
    const f = await fixture();
    f.channels.library_revision = '1'.repeat(64);
    f.state.library_revision = f.channels.library_revision;
    await f.run('poll()');
    f.channels.channels.push({service_uuid:'b', name:'Second'});
    f.channels.favourites = ['b','a'];
    f.channels.library_revision = '2'.repeat(64);
    f.state.library_revision = f.channels.library_revision;
    await f.run('poll()');
    assert.equal(f.run('JSON.stringify(favourites)'), JSON.stringify(['b','a']));
    assert.equal(f.run('dirty'), false);
    assert.equal(f.run('baseRevision'), f.channels.library_revision);
});

test('dirty editor retains its base revision and requires explicit conflict reload', async () => {
    const f = await fixture();
    f.channels.library_revision = '1'.repeat(64);
    f.state.library_revision = f.channels.library_revision;
    await f.run('poll()');
    await f.run('edit(() => favourites.reverse())');
    f.channels.library_revision = '2'.repeat(64);
    f.state.library_revision = f.channels.library_revision;
    f.channels.favourites = [];
    await f.run('poll()');
    assert.equal(f.run('favouritesConflict'), true);
    assert.equal(f.run('baseRevision'), '1'.repeat(64));
    assert.equal(f.get('save-favourites').disabled, true);
    const writes = f.calls.filter(c => c.path === '/api/favourites').length;
    await f.run('save()');
    assert.equal(f.calls.filter(c => c.path === '/api/favourites').length, writes);
    await f.get('reload-favourites').onclick();
    assert.equal(f.run('favouritesConflict'), false);
    assert.equal(f.run('baseRevision'), '2'.repeat(64));
});

test('save sends the revision on which the local edits were based', async () => {
    const f = await fixture();
    f.channels.library_revision = 'a'.repeat(64);
    await f.run('load()');
    await f.run('save()');
    const request = f.calls.find(c => c.path === '/api/favourites');
    assert.equal(JSON.parse(request.options.body).base_revision, 'a'.repeat(64));
});

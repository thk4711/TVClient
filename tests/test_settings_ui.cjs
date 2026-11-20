const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(failLoad = false) {
    const nodes = new Map();
    for (const match of fs.readFileSync('satip_controller/static/index.html', 'utf8').matchAll(/id="([^"]+)"/g)) {
        nodes.set(match[1], {value: '', disabled: false, hidden: false, textContent: '',
            replaceChildren() {}, reportValidity() { return true; }});
    }
    const calls = [];
    let saved = 5000, language = 'en';
    const window = {addEventListener() {}, scrollTo() {}};
    const context = vm.createContext({window, document: {
        getElementById: id => { assert.ok(nodes.has(id), id); return nodes.get(id); },
        querySelectorAll: () => [], addEventListener() {}
    }, location: {hash: '#settings'}, setInterval() {}, AbortController,
        catalogue: [], favourites: [], previewable: [], previewState: {state: 'idle'},
        previewSession: null, requesting: false, busy: false, recovering: false, connected: true,
        api: async (path, method = 'GET', data) => {
            calls.push({path, method, data});
            if (failLoad) throw Error('offline');
            if (method === 'PUT') { saved = data.osd_timeout_ms; language = data.osd_language; }
            return {version: 1, osd_timeout_ms: saved, osd_language: language};
        }
    });
    vm.runInContext(fs.readFileSync('satip_controller/static/app.js', 'utf8'), context);
    return {nodes, calls, window};
}

test('settings navigation loads seconds and saves milliseconds without player actions', async () => {
    const f = fixture();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.nodes.get('page-settings').hidden, false);
    assert.equal(f.nodes.get('page-status').hidden, true);
    assert.equal(f.nodes.get('osd-timeout').value, 5);
    assert.equal(f.nodes.get('osd-language').value, 'en');
    f.nodes.get('osd-language').value = 'de';
    f.nodes.get('osd-timeout').value = '8.5';
    await f.nodes.get('osd-settings').onsubmit({preventDefault() {}});
    assert.equal(f.calls.at(-1).method, 'PUT');
    assert.equal(f.calls.at(-1).data.osd_timeout_ms, 8500);
    assert.equal(f.calls.at(-1).data.osd_language, 'de');
    assert.equal(f.nodes.get('osd-language').value, 'de');
    assert.equal(f.nodes.get('osd-language').disabled, false);
    assert.match(f.nodes.get('settings-status').textContent, /Saved/);
    assert.equal(f.nodes.get('save-settings').disabled, false);
    assert.ok(f.calls.every(call => call.path === '/api/settings'));
});

test('settings heading has no eyebrow or subtitle', () => {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    const settings = html.match(/<section[^>]+id="page-settings"[\s\S]*?<\/section>/)[0];
    assert.ok(!settings.includes('MAKE IT YOURS'));
    assert.ok(!settings.includes('On-screen display preferences.'));
    assert.ok(settings.includes('<h1 id="settings-title">Settings</h1>'));
});

test('settings timeout control has no explanatory hint or dangling reference', () => {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    assert.ok(!html.includes('osd-timeout-help'));
    assert.ok(!html.includes('Applies to channel/programme and volume bars'));
    assert.match(html, /<input id="osd-timeout" type="number" min="0.5" max="30" step="0.5" required disabled>/);
});

test('failed settings load keeps the save control disabled', async () => {
    const f = fixture(true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.nodes.get('save-settings').disabled, true);
    assert.equal(f.nodes.get('osd-language').disabled, true);
    assert.match(f.nodes.get('settings-status').textContent, /Cannot load/);
});

test('settings has no network startup section or polling', async () => {
    const f = fixture();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.nodes.has('network-address-status'), false);
    assert.ok(!fs.readFileSync('satip_controller/static/index.html', 'utf8').includes('Network startup'));
    assert.equal(f.nodes.has('network-mode'), false);
    assert.equal(f.nodes.has('network-risk'), false);
    assert.equal(f.nodes.has('save-network'), false);
    assert.ok(f.calls.every(call => call.path === '/api/settings'));
    assert.ok(!fs.readFileSync('satip_controller/static/app.js', 'utf8').includes('/api/network'));
});

async function youtubeFixture() {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    const nodes = new Map();
    for (const match of html.matchAll(/<[^>]+id="([^"]+)"[^>]*>/g)) {
        nodes.set(match[1], {hidden: /\bhidden\b/.test(match[0]), disabled: false, textContent: '',
            removeAttribute(name) { delete this[name]; }});
    }
    nodes.get('page-settings').hidden = false;
    const fills = [];
    nodes.get('youtube-qr-image').getContext = () => ({fillRect(...args) { fills.push(args); }});
    const f = {nodes, calls: [], data: {enabled: true, state: 'ready', pairing_code: '123456789012'},
        fail: false, confirmed: true, fills, context: null};
    const context = vm.createContext({
        document: {hidden: false, getElementById: id => nodes.get(id), addEventListener() {}},
        window: {addEventListener() {}}, AbortController, confirm: () => f.confirmed,
        setInterval: callback => { f.refresh = callback; },
        receiverRequest: async (path, method, data) => {
            f.calls.push({path, method, data});
            if (f.fail) throw Error('offline');
            return f.data;
        }
    });
    f.context = context;
    vm.runInContext(fs.readFileSync('satip_controller/static/qrcode.js', 'utf8'), context);
    vm.runInContext(fs.readFileSync('satip_controller/static/youtube.js', 'utf8'), context);
    await new Promise(resolve => setImmediate(resolve));
    return f;
}

test('YouTube link shows only buttons before a pairing request, even with an existing code', async () => {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    assert.ok(html.includes('<h2>YouTube link</h2>'));
    assert.ok(!html.includes('YouTube on HDMI'));
    assert.ok(!html.includes('Public H.264/AAC'));
    const f = await youtubeFixture();
    for (const id of ['youtube-status', 'youtube-code', 'youtube-pairing-help', 'youtube-qr']) {
        assert.equal(f.nodes.get(id).hidden, true, id);
    }
    assert.ok(f.calls.every(call => call.method === 'GET'));
});

test('YouTube pairing reveals instructions and formatted code; disable/unlink hide them', async () => {
    const f = await youtubeFixture();
    for (const action of ['disable', 'unlink']) {
        await f.nodes.get('youtube-pair').onclick();
        assert.equal(f.calls.findLast(call => call.method === 'POST').data.action, 'pair');
        assert.equal(f.nodes.get('youtube-pairing-help').hidden, false);
        assert.equal(f.nodes.get('youtube-status').hidden, false);
        assert.equal(f.nodes.get('youtube-code').hidden, false);
        assert.equal(f.nodes.get('youtube-code').textContent, '123 456 789 012');
        assert.equal(f.nodes.get('youtube-qr').hidden, false);
        assert.equal(f.nodes.get('youtube-pairing-link').href, 'https://www.youtube.com/pair?code=123456789012');
        assert.ok(f.fills.length > 100);
        await f.nodes.get('youtube-' + action).onclick();
        await f.refresh();
        for (const id of ['youtube-status', 'youtube-code', 'youtube-pairing-help', 'youtube-qr']) {
            assert.equal(f.nodes.get(id).hidden, true, id);
        }
        assert.equal(f.nodes.get('youtube-pairing-link').href, undefined);
    }
});

test('cancelled YouTube unlink leaves pairing instructions visible and sends no request', async () => {
    const f = await youtubeFixture();
    await f.nodes.get('youtube-pair').onclick();
    f.confirmed = false;
    const count = f.calls.length;
    await f.nodes.get('youtube-unlink').onclick();
    assert.equal(f.calls.length, count);
    assert.equal(f.nodes.get('youtube-pairing-help').hidden, false);
});

test('YouTube hides invalid codes and keeps request/status errors visible', async () => {
    const f = await youtubeFixture();
    f.data.pairing_code = '<script>bad</script>';
    await f.nodes.get('youtube-pair').onclick();
    assert.equal(f.nodes.get('youtube-code').hidden, true);
    assert.equal(f.nodes.get('youtube-code').textContent, '');
    f.fail = true;
    await f.nodes.get('youtube-disable').onclick();
    assert.equal(f.nodes.get('youtube-pairing-help').hidden, true);
    assert.equal(f.nodes.get('youtube-status').hidden, false);
    assert.match(f.nodes.get('youtube-status').textContent, /Could not confirm/);
    assert.equal(f.nodes.get('youtube-pair').disabled, false);
    await f.refresh();
    assert.match(f.nodes.get('youtube-status').textContent, /unavailable/);
    f.fail = false;
    f.data.error = 'Receiver unavailable';
    await f.refresh();
    assert.equal(f.nodes.get('youtube-status').hidden, false);
    assert.equal(f.nodes.get('youtube-status').textContent, 'Receiver unavailable');
});

test('YouTube QR changes only with the code and clears on expiry, disabled state and errors', async () => {
    const f = await youtubeFixture();
    await f.nodes.get('youtube-pair').onclick();
    const drawn = f.fills.length;
    await f.refresh();
    assert.equal(f.fills.length, drawn);
    f.data.pairing_code = '000000000001';
    await f.refresh();
    assert.equal(f.nodes.get('youtube-pairing-link').href, 'https://www.youtube.com/pair?code=000000000001');
    assert.equal(f.nodes.get('youtube-code').textContent, '000 000 000 001');
    assert.ok(f.fills.length > drawn);
    for (const change of [{pairing_code: null}, {enabled: false}, {error: 'Failed'}]) {
        f.data = {enabled: true, state: 'ready', pairing_code: '123456789012', ...change};
        await f.refresh();
        assert.equal(f.nodes.get('youtube-qr').hidden, true);
        assert.equal(f.nodes.get('youtube-code').hidden, true);
        assert.equal(f.nodes.get('youtube-code').textContent, '');
        assert.equal(f.nodes.get('youtube-pairing-link').href, undefined);
    }
    f.data = {enabled: true, state: 'ready', pairing_code: '123456789012'};
    await f.refresh();
    f.fail = true;
    await f.refresh();
    assert.equal(f.nodes.get('youtube-qr').hidden, true);
    assert.equal(f.nodes.get('youtube-pairing-link').href, undefined);
});

test('YouTube numeric pairing survives a missing QR encoder or canvas failure', async () => {
    for (const fail of ['encoder', 'canvas']) {
        const f = await youtubeFixture();
        if (fail === 'encoder') vm.runInContext('qrcode = undefined', f.context);
        else f.nodes.get('youtube-qr-image').getContext = () => null;
        await f.nodes.get('youtube-pair').onclick();
        assert.equal(f.nodes.get('youtube-code').textContent, '123 456 789 012');
        assert.equal(f.nodes.get('youtube-code').hidden, false);
        assert.equal(f.nodes.get('youtube-qr').hidden, true);
        assert.equal(f.nodes.get('youtube-pairing-link').href, undefined);
    }
});

test('an old YouTube status request cannot restore codes after Disable', async () => {
    const f = await youtubeFixture();
    await f.nodes.get('youtube-pair').onclick();
    let finish;
    f.context.receiverRequest = async (path, method) => method === 'GET' ?
        new Promise(resolve => { finish = resolve; }) : {};
    const oldRequest = f.refresh();
    await f.nodes.get('youtube-disable').onclick();
    finish({enabled: true, state: 'ready', pairing_code: '123456789012'});
    await oldRequest;
    assert.equal(f.nodes.get('youtube-qr').hidden, true);
    assert.equal(f.nodes.get('youtube-code').textContent, '');
    assert.equal(f.nodes.get('youtube-pairing-link').href, undefined);
});

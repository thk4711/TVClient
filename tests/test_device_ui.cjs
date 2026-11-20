const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('settings sections use one column at every viewport width', () => {
    const css = fs.readFileSync('satip_controller/static/app.css', 'utf8');
    assert.ok(!css.includes('(line truncated'), 'stylesheet must not contain truncated tool output');
    const layouts = [...css.matchAll(/#device-settings{([^}]+)}/g)].map(match => match[1]);
    assert.equal(layouts.length, 1);
    assert.ok(layouts[0].includes('grid-template-columns:minmax(0,1fr)'));
});

test('storage highlights main non-destructive actions and keeps alternatives neutral', () => {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    for (const name of ['choose', 'save', 'mount', 'format', 'eject', 'cancel']) {
        const button = html.match(new RegExp('<button\\b[^>]*id="device-storage-' + name + '"[^>]*>'));
        assert.ok(button, 'Storage button exists: ' + name);
        assert.equal(/class="primary"/.test(button[0]), ['choose', 'save', 'mount'].includes(name), name);
    }
});

async function fixture(extra = {}) {
    const nodes = new Map();
    for (const match of fs.readFileSync('satip_controller/static/index.html', 'utf8').matchAll(/id="([^"]+)"/g)) {
        nodes.set(match[1], {value: '', checked: false, disabled: false, hidden: false, textContent: '', children: [],
            append(node) { this.children.push(node); }, replaceChildren() { this.children = []; },
            reportValidity() { return true; }, showModal() { this.open = true; }, close() { this.open = false; }, focus() {}});
    }
    const get = name => nodes.get('device-' + name);
    let poll;
    const calls = [];
    const state = {hostname: 'satip-dev', timezone: 'Europe/Berlin', timezones: ['Europe/Berlin', 'Etc/UTC'],
        wifi_ssid: 'home', wifi_country: 'DE', ssh_enabled: true, wifi_enabled: true, trial: null,
        storage: {disks: [], mounted: false}};
    const context = vm.createContext({document: {getElementById: id => nodes.get(id),
        createElement: () => ({value: '', textContent: ''}), querySelectorAll: selector => selector.startsWith('#device-storage-dialog') ?
            ['disk', 'mount', 'format', 'cancel'].map(name => get('storage-' + name)) : [...nodes.values()]},
        location: {hash: '#status'}, setInterval(callback) { poll = callback; }, window: {confirm: () => true},
        receiverRequest: async (path, method = 'GET', payload) => {
            calls.push({path, method, payload});
            if (method === 'GET') return path === '/api/device/storage' ? {...state.storage} : {...state};
            if (state.error) throw new Error(state.error);
            if (payload.action === 'wifi') {
                state.trial = {id: 'trial', ssid: payload.args.ssid, seconds_left: 90};
                return {trial_id: 'trial', message: 'Trying Wi-Fi settings.'};
            }
            if (payload.action === 'wifi_confirm') { state.trial = null; state.message = 'Wi-Fi settings saved.'; }
            return {message: 'Saved.'};
        }
    });
    Object.assign(context, extra);
    vm.runInContext(fs.readFileSync('satip_controller/static/device.js', 'utf8'), context);
    await context.window.loadDeviceSettings(true);
    return {nodes, get, calls, state, context, poll, event: {preventDefault() {}}};
}

test('device page loads safe values and polling preserves unsaved edits', async () => {
    const f = await fixture();
    assert.equal(f.nodes.get('device-settings').hidden, false);
    assert.equal(f.get('hostname').value, 'satip-dev');
    assert.equal(f.get('wifi-password').value, '');
    f.get('hostname').value = 'unsaved';
    await f.poll();
    assert.equal(f.get('hostname').value, 'unsaved');
    await f.context.window.loadDeviceSettings(false);
    assert.equal(f.nodes.get('device-settings').hidden, true);
});

test('device save sends only hostname and timezone', async () => {
    const f = await fixture();
    f.get('hostname').value = 'living-room';
    await f.get('form').onsubmit(f.event);
    const call = f.calls.find(call => call.method === 'POST');
    assert.equal(call.payload.action, 'device');
    assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)), {hostname: 'living-room', timezone: 'Europe/Berlin'});
});

test('Wi-Fi trial clears password and offers keep/revert; completion hides trial', async () => {
    const f = await fixture();
    f.get('ssid').value = 'new-home'; f.get('wifi-password').value = 'test-password';
    await f.get('wifi-form').onsubmit(f.event);
    assert.equal(f.get('wifi-password').value, '');
    assert.equal(f.get('wifi-trial').hidden, false);
    assert.match(f.get('trial-message').textContent, /90 seconds/);
    await f.get('wifi-keep').onclick();
    assert.equal(f.get('wifi-trial').hidden, true);
    assert.match(f.get('wifi-message').textContent, /saved/);
});

test('access card has only SSH and password change buttons, with fields in a dialog', () => {
    const html = fs.readFileSync('satip_controller/static/index.html', 'utf8');
    const card = html.split('<h2>Passwords &amp; access</h2>')[1].split('</article>')[0];
    assert.equal([...card.matchAll(/<input /g)].length, 1);
    assert.match(card, /id="device-ssh" type="checkbox"/);
    assert.match(card, /id="device-change-web-password"/);
    assert.match(card, /id="device-change-root-password"/);
    assert.ok(!card.includes('type="password"'));
    assert.match(html, /<dialog id="device-change-password-dialog" aria-labelledby="device-change-password-title">/);
    assert.ok(!html.includes('id="device-change-current"'));
    for (const id of ['new', 'confirm']) assert.match(html, new RegExp('id="device-change-' + id + '" type="password"'));
});

test('password dialog opens for either account; cancel and Escape clear secrets without requests', async () => {
    const f = await fixture();
    for (const kind of ['web', 'root']) {
        f.get('change-' + kind + '-password').onclick();
        assert.equal(f.get('change-password-dialog').open, true);
        assert.equal(f.get('change-password-title').textContent, 'Change ' + kind + ' password');
        for (const id of ['new', 'confirm']) f.get('change-' + id).value = 'secret';
        if (kind === 'web') f.get('change-password-cancel').onclick();
        else f.get('change-password-dialog').oncancel(f.event);
        assert.equal(f.get('change-password-dialog').open, false);
        for (const id of ['new', 'confirm']) assert.equal(f.get('change-' + id).value, '');
    }
    assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
});

test('password mismatch stays local inside the change dialog', async () => {
    const f = await fixture();
    f.get('change-root-password').onclick();
    f.get('change-new').value = 'one'; f.get('change-confirm').value = 'two';
    await f.get('change-password-form').onsubmit(f.event);
    assert.equal(f.calls.filter(call => call.method === 'POST').length, 0);
    assert.match(f.get('change-password-message').textContent, /do not match/);
    assert.equal(f.get('change-password-dialog').open, true);
});

test('password changes use one dialog without current password and clear all secrets', async () => {
    for (const kind of ['web', 'root']) {
        const f = await fixture();
        f.get('change-' + kind + '-password').onclick();
        f.get('change-new').value = f.get('change-confirm').value = 'new-secret';
        const pending = f.get('change-password-form').onsubmit(f.event);
        assert.equal(f.get('change-password-save').disabled, true);
        assert.notEqual(f.get('password-dialog').open, true);
        f.get('change-password-dialog').oncancel(f.event);
        assert.equal(f.get('change-password-dialog').open, true);
        await pending;
        const call = f.calls.find(call => call.method === 'POST');
        assert.ok(!('current_password' in call.payload));
        assert.equal(call.payload.action, kind + '_password');
        assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)), {password: 'new-secret', confirm: 'new-secret'});
        for (const id of ['new', 'confirm']) assert.equal(f.get('change-' + id).value, '');
        assert.equal(f.get('change-password-dialog').open, false);
        if (kind === 'web') assert.match(f.get('access-message').textContent, /Sign in again/);
    }
});

test('failed password change stays open with feedback and clears secrets for retry', async () => {
    const f = await fixture();
    f.state.error = 'Password change is unavailable.';
    f.get('change-root-password').onclick();
    for (const id of ['new', 'confirm']) f.get('change-' + id).value = 'secret';
    await f.get('change-password-form').onsubmit(f.event);
    assert.equal(f.get('change-password-dialog').open, true);
    assert.match(f.get('change-password-message').textContent, /unavailable/);
    assert.equal(f.get('change-password-save').disabled, false);
    for (const id of ['new', 'confirm']) assert.equal(f.get('change-' + id).value, '');
});

test('Wi-Fi trials block opening password dialogs', async () => {
    const f = await fixture();
    f.state.trial = {id: 'trial', ssid: 'new-home', seconds_left: 80};
    await f.poll();
    for (const kind of ['web', 'root']) {
        assert.equal(f.get('change-' + kind + '-password').disabled, true);
        f.get('change-' + kind + '-password').onclick();
        assert.notEqual(f.get('change-password-dialog').open, true);
    }
});

async function storageFixture() {
    const f = await fixture();
    f.state.storage.disks = [{id: 'disk-one', name: 'sda', model: 'Test SSD', serial: '123',
        bytes: 256 * 1024**3, busy: false, filesystems: [{type: 'ext4'}]}];
    await f.poll();
    f.get('storage-choose').onclick();
    f.get('storage-disk').value = 'disk-one'; f.get('storage-disk').onchange();
    return f;
}

test('empty storage hides controls and requests a USB device', async () => {
    const f = await fixture();
    assert.match(f.get('storage-status').textContent, /Please connect a USB storage device/);
    for (const id of ['storage-choose', 'storage-eject', 'storage-limit-form']) assert.equal(f.get(id).hidden, true);
});

test('choose device opens a cancellable dialog without showing a storage limit', async () => {
    const f = await storageFixture();
    assert.equal(f.get('storage-choose').hidden, false);
    assert.equal(f.get('storage-dialog').open, true);
    assert.equal(f.get('storage-limit-form').hidden, true);
    f.get('storage-dialog').oncancel(f.event);
    assert.equal(f.get('storage-dialog').open, false);
    assert.equal(f.calls.filter(c => c.method === 'POST').length, 0);
});

test('formatting warns about all data and requires only a second confirmation', async () => {
    const f = await storageFixture();
    let warning;
    f.context.window.confirm = text => { warning = text; return false; };
    await f.get('storage-format').onclick();
    assert.match(warning, /ALL data on sda.*Test SSD.*permanently lost/);
    assert.equal(f.calls.filter(c => c.method === 'POST').length, 0);
    f.context.window.confirm = () => true;
    await f.get('storage-format').onclick();
    const call = f.calls.find(c => c.method === 'POST');
    assert.equal(call.payload.action, 'storage_format');
    assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)), {id: 'disk-one', minutes: 0, gib: 8, confirm: true});
    assert.ok(!('current_password' in call.payload));
    assert.notEqual(f.get('password-dialog').open, true);
    assert.equal(f.get('storage-dialog').open, false);
});

test('format failure stays visible in the dialog for retry', async () => {
    const f = await storageFixture(); f.state.error = 'Formatter unavailable';
    await f.get('storage-format').onclick();
    assert.equal(f.get('storage-dialog').open, true);
    assert.match(f.get('storage-dialog-message').textContent, /Formatter unavailable/);
});

test('existing filesystem never formats and device replacement clears selection', async () => {
    const f = await storageFixture();
    await f.get('storage-form').onsubmit(f.event);
    assert.equal(f.calls.find(c => c.method === 'POST').payload.action, 'storage_mount');
    f.state.storage.disks[0].id = 'replacement';
    await f.poll();
    assert.equal(f.get('storage-disk').value, '');
    assert.equal(f.get('storage-format').disabled, true);
});

test('mounted storage shows identity, capacity, free space and a capacity-sized limit', async () => {
    const f = await storageFixture();
    Object.assign(f.state.storage, {mounted: true, ready: true, device: f.state.storage.disks[0],
        device_id: 'disk-one', total_bytes: 256 * 1024**3, free_bytes: 200 * 1024**3, max_bytes: 8 * 1024**3});
    await f.poll();
    assert.match(f.get('storage-status').textContent, /sda · Test SSD · 256.0 GiB capacity · 200.0 GiB free/);
    assert.equal(f.get('storage-choose').hidden, true);
    assert.equal(f.get('storage-limit-form').hidden, false);
    assert.equal(f.get('storage-eject').hidden, false);
    assert.equal(f.get('storage-gib').max, '256');
    assert.equal(f.get('storage-gib').value, '8');
    f.get('storage-gib').value = '256';
    await f.poll(); assert.equal(f.get('storage-gib').value, '256');
    await f.get('storage-limit-form').onsubmit(f.event);
    const call = f.calls.find(c => c.method === 'POST');
    assert.equal(call.payload.action, 'storage_limit');
    assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)), {id: 'disk-one', gib: 256});
});

test('mounted or busy disks cannot be formatted', async () => {
    const f = await storageFixture();
    f.state.storage.disks[0].busy = true;
    await f.poll();
    assert.equal(f.get('storage-format').disabled, true);
    assert.equal(f.get('storage-choose').hidden, true);
    f.state.storage.mounted = true; await f.poll();
    assert.equal(f.get('storage-mount').disabled, true);
    assert.equal(f.get('storage-eject').disabled, false);
});


function backupEnvironment(fetch) {
    return {crypto: {getRandomValues(bytes) { bytes.fill(7); }}, AbortController,
        setTimeout(callback, delay) { if (delay === 2000) queueMicrotask(callback); return 1; },
        clearTimeout() {}, fetch};
}

test('backup downloads a binary file and releases its object URL', async () => {
    const revoked = [], links = [];
    const f = await fixture(backupEnvironment(async path => {
        assert.match(path, /^\/api\/device\/backup\/download\/[0-9a-f]{32}$/);
        return {ok: true, blob: async () => 'private archive'};
    }));
    f.context.URL = {createObjectURL(blob) { assert.equal(blob, 'private archive'); return 'blob:backup'; }, revokeObjectURL(url) { revoked.push(url); }};
    f.context.document.body = {append(link) { links.push(link); }};
    f.context.document.createElement = () => ({click() { this.clicked = true; }, remove() { this.removed = true; }});
    f.context.setTimeout = (callback, delay) => { if (delay === 60000) callback(); return 1; };
    f.context.receiverRequest = async (path, method, payload) => {
        f.calls.push({path, method, payload});
        return {state: payload.action === 'backup_status' ? 'ready' : 'working'};
    };
    await f.get('backup-download').onclick();
    assert.equal(links.length, 1);
    assert.equal(links[0].clicked, true);
    assert.equal(links[0].removed, true);
    assert.match(links[0].download, /^satip-backup-.*\.tar$/);
    assert.deepEqual(revoked, ['blob:backup']);
    assert.match(f.get('backup-message').textContent, /Keep.*private/);
    assert.ok(f.calls.some(call => call.payload?.action === 'backup_create'));
});

test('restore validates upload, supports cancellation, and never sends file contents as JSON', async () => {
    let uploaded;
    const f = await fixture(backupEnvironment(async (path, options) => {
        uploaded = {path, options};
        return {ok: true, json: async () => ({created_at: '2026-11-13', state: 'validated'})};
    }));
    f.context.window.confirm = () => false;
    f.get('backup-file').files = [{size: 100, name: 'private.tar'}];
    await f.get('backup-file').onchange();
    assert.equal(uploaded.options.headers['Content-Type'], 'application/octet-stream');
    assert.equal(uploaded.options.body.name, 'private.tar');
    assert.match(f.get('backup-message').textContent, /cancelled.*No settings changed/);
    assert.equal(f.calls.filter(call => call.payload?.action === 'backup_restore').length, 0);
    assert.equal(f.get('backup-file').value, '');
});

test('restore requires password confirmation, clears secrets, and disables controls after acceptance', async () => {
    const f = await fixture(backupEnvironment(async () => ({ok: true, json: async () => ({created_at: '2026-11-13'})})));
    f.get('backup-file').files = [{size: 100}];
    const task = f.get('backup-file').onchange();
    await new Promise(setImmediate);
    assert.equal(f.get('password-dialog').open, true);
    f.get('current-password').value = 'fake-web-password';
    f.get('password-prompt').onsubmit(f.event);
    await task;
    const call = f.calls.find(call => call.payload?.action === 'backup_restore');
    assert.equal(call.payload.current_password, 'fake-web-password');
    assert.equal(f.get('current-password').value, '');
    assert.equal(f.get('backup-restore').disabled, true);
    assert.match(f.get('backup-message').textContent, /rebooting/);
});

test('oversized or empty backup files fail locally and Wi-Fi trials block both backup buttons', async () => {
    let uploaded = false;
    const f = await fixture(backupEnvironment(async () => { uploaded = true; }));
    for (const size of [0, 32 * 1024**2 + 1]) {
        f.get('backup-file').files = [{size}];
        await f.get('backup-file').onchange();
        assert.match(f.get('backup-message').textContent, /32 MiB/);
    }
    assert.equal(uploaded, false);
    f.state.trial = {id: 'trial', ssid: 'home', seconds_left: 90};
    await f.poll();
    assert.equal(f.get('backup-download').disabled, true);
    assert.equal(f.get('backup-restore').disabled, true);
});


test('safe removal and forgetting are separate actions with correct availability', async () => {
    const f = await fixture();
    f.state.require_auth = false;
    f.state.storage = {disks: [], mounted: false, remembered: true, safely_removed: true};
    await f.poll();
    assert.equal(f.get('storage-forget').hidden, false);
    assert.equal(f.get('storage-forget').disabled, false);
    assert.equal(f.get('storage-eject').hidden, true);
    await f.get('storage-forget').onclick();
    assert.equal(f.calls.filter(c => c.method === 'POST').at(-1).payload.action, 'storage_forget');
    f.context.location.hash = '#settings';
    f.state.storage.mounted = true;
    f.state.storage.total_bytes = 32 * 1024**3;
    f.state.storage.free_bytes = 30 * 1024**3;
    f.state.storage.max_bytes = 8 * 1024**3;
    await f.poll();
    assert.equal(f.get('storage-forget').hidden, true);
    assert.equal(f.get('storage-forget').disabled, true);
    assert.equal(f.get('storage-eject').hidden, false);
});


test('channel backup is appliance-only and uses a separate download', async () => {
    const f = await fixture(backupEnvironment(async path => {
        assert.match(path, /^\/api\/device\/channels\/download\/[0-9a-f]{32}$/);
        return {ok: true, blob: async () => 'channels'};
    }));
    const links = [];
    f.context.document.body = {append(link) { links.push(link); }};
    f.context.document.createElement = () => ({click() {}, remove() {}});
    f.context.URL = {createObjectURL() { return 'blob:channels'; }, revokeObjectURL() {}};
    f.context.receiverRequest = async (path, method, payload) => {
        f.calls.push({path, method, payload});
        return {state: payload.action === 'backup_status' ? 'ready' : 'working'};
    };
    assert.equal(f.get('channel-backup').hidden, false);
    await f.get('channels-download').onclick();
    assert.match(links[0].download, /^satip-channels-.*\.tar$/);
    assert.ok(f.calls.some(call => call.payload?.action === 'backup_channels_create'));
    assert.match(f.get('channels-message').textContent, /not passwords/);
    await f.context.window.loadDeviceSettings(false);
    assert.equal(f.get('channel-backup').hidden, true);
});

test('channel restore shows counts, preserves credentials and reloads after service recovery', async () => {
    const f = await fixture(backupEnvironment(async path => {
        assert.match(path, /channels\/upload/);
        return {ok: true, json: async () => ({created_at: '2026-11-16', available: 202, favourites: 38})};
    }));
    let prompt, reloaded = false;
    f.context.window.confirm = text => { prompt = text; return true; };
    f.context.location.reload = () => { reloaded = true; };
    f.context.receiverRequest = async (path, method, payload) => {
        f.calls.push({path, method, payload});
        return {state: payload.action === 'backup_status' ? 'restored' : 'working'};
    };
    f.get('channels-file').files = [{size: 100}];
    const task = f.get('channels-file').onchange();
    for (let i = 0; i < 10 && !f.get('password-dialog').open; i++) await Promise.resolve();
    assert.equal(f.get('channels-download').disabled, true);
    f.get('current-password').value = 'current-web-password';
    f.get('password-prompt').onsubmit(f.event);
    await task;
    assert.match(prompt, /202 available channels and 38 favourites/);
    assert.match(prompt, /device settings and passwords stay unchanged/);
    assert.equal(f.get('current-password').value, '');
    assert.ok(f.calls.some(call => call.payload?.action === 'backup_channels_restore'));
    assert.ok(!f.calls.some(call => call.payload?.action === 'backup_restore'));
    assert.equal(reloaded, true);
    assert.equal(f.get('channels-file').value, '');
});


test('TV source switch is queued, blocks controls and reloads after activation', async () => {
    let reloads = 0;
    const f = await fixture({location: {hash: '#status', reload() { reloads++; }}});
    f.state.require_auth = false;
    f.state.tv_source = {active: 'satip', available: ['satip', 'enigma2']};
    await f.context.window.loadDeviceSettings(true);
    assert.equal(f.get('source-card').hidden, false);
    assert.equal(f.get('source').value, 'satip');
    f.get('source').value = 'enigma2';
    f.context.window.sourceTunerControls();
    await f.get('source-form').onsubmit(f.event);
    const call = f.calls.find(c => c.payload?.action === 'source_select');
    assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)), {source: 'enigma2'});
    assert.equal(f.get('hostname').disabled, true);
    assert.equal(reloads, 0);
    f.state.tv_source.active = 'enigma2';
    await f.poll();
    assert.equal(reloads, 1);
});

test('tuner apply is explicit, blocks pending changes and reloads on completion', async () => {
    let reloads=0;
    const f=await fixture({location:{hash:'#settings',reload(){reloads++;}}});
    f.state.require_auth=false;
    f.state.tv_source={active:'satip',available:['satip','enigma2']};
    await f.context.window.loadDeviceSettings(true);
    f.context.window.sourceConfiguration.satip={connected:true,configured_frontend:'b'.repeat(32)};
    const select=f.nodes.get('source-tuner');
    select.value='b'.repeat(32);f.context.window.sourceTunerControls();
    assert.equal(f.nodes.get('source-apply-tuner').disabled,true);
    assert.equal(f.nodes.get('source-activate').disabled,true);
    select.value='a'.repeat(32);select.selectedOptions=[{textContent:'Tuner #1'}];
    f.context.window.sourceTunerControls();
    assert.equal(f.nodes.get('source-apply-tuner').disabled,false);
    assert.equal(f.calls.some(c=>c.method==='POST'),false);
    await f.get('tuner-form').onsubmit(f.event);
    const call=f.calls.find(c=>c.payload?.action==='satip_tuner');
    assert.deepEqual(JSON.parse(JSON.stringify(call.payload.args)),{frontend:'a'.repeat(32)});
    assert.equal(f.get('hostname').disabled,true);
    assert.equal(select.disabled,true);
    f.state.message='SAT-IP tuner applied.';await f.poll();
    assert.equal(reloads,1);
});

test('tuner apply disables unavailable and inactive SAT-IP; unconfigured activation explains why', async () => {
    const f=await fixture();
    f.state.tv_source={active:'satip',available:['satip']};
    await f.context.window.loadDeviceSettings(true);
    f.context.window.sourceConfiguration.satip={connected:false};
    f.context.window.sourceTunerControls();
    assert.equal(f.nodes.get('source-apply-tuner').disabled,true);
    assert.match(f.nodes.get('source-tuner-help').textContent,/unavailable/);
    f.get('source').value='enigma2';f.context.window.sourceTunerControls();
    assert.equal(f.nodes.get('source-activate').disabled,true);
    assert.match(f.nodes.get('source-activation-help').textContent,/import/);
    f.context.window.sourceConfiguration.active='enigma2';f.context.window.sourceTunerControls();
    assert.match(f.nodes.get('source-tuner-help').textContent,/Activate SAT-IP/);
});

// Isolated Chrome regression: fake device API; never changes receiver credentials.
// Requires Node 22+, CHROME_BINARY and a fresh CHROME_USER_DATA_DIR.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {test} = require('node:test');

test('device dialogs and storage states support desktop and mobile layout', {timeout: 30000}, async () => {
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR, 'Set Chrome binary and an isolated profile directory');
    const calls = [];
    let fail = false;
    let osd = {device_settings: true, osd_timeout_ms: 5000, osd_language: 'en'};
    let storage = {disks: [], mounted: false};
    const server = http.createServer(async (request, response) => {
        response.setHeader('Cache-Control', 'no-store');
        if (request.url === '/' || /^\/[a-z.]+\.(js|css)$/.test(request.url)) {
            const path = 'satip_controller/static/' + (request.url === '/' ? 'index.html' : request.url.slice(1));
            if (!fs.existsSync(path)) { response.writeHead(404); response.end(); return; }
            response.setHeader('Content-Type', request.url === '/' ? 'text/html' : request.url.endsWith('.css') ? 'text/css' : 'text/javascript');
            response.end(fs.readFileSync(path));
            return;
        }
        response.setHeader('Content-Type', 'application/json');
        if (request.url === '/api/device' && request.method === 'POST') {
            let body = ''; for await (const chunk of request) body += chunk;
            calls.push(JSON.parse(body));
            if (fail) { response.writeHead(503); response.end(JSON.stringify({detail: 'Password change is unavailable'})); }
            else response.end(JSON.stringify({message: 'Password changed.'}));
        } else if (request.url === '/api/device') response.end(JSON.stringify({hostname: 'satip-test', timezone: 'Etc/UTC', timezones: ['Etc/UTC'], wifi_ssid: 'home', wifi_country: 'DE', ssh_enabled: true, wifi_enabled: true, trial: null}));
        else if (request.url === '/api/device/storage') response.end(JSON.stringify(storage));
        else if (request.url === '/api/settings') {
            if (request.method === 'PUT') {
                let body = ''; for await (const chunk of request) body += chunk;
                osd = {...osd, ...JSON.parse(body)};
            }
            response.end(JSON.stringify(osd));
        }
        else if (request.url === '/api/status') response.end(JSON.stringify({scan: {state: 'idle'}, preview: {state: 'idle', enabled: true}}));
        else if (request.url === '/api/channels') response.end(JSON.stringify({channels: [], favourites: [], previewable: []}));
        else response.end('{}');
    });
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--user-data-dir=' + process.env.CHROME_USER_DATA_DIR, 'about:blank']);
    let socket;
    try {
        const debuggerUrl = await new Promise((resolve, reject) => {
            let text = '';
            chrome.stderr.on('data', data => { text += data; const match = text.match(/DevTools listening on (.+)/); if (match) resolve(match[1].trim()); });
            chrome.on('error', reject); chrome.on('exit', code => reject(Error('Chrome exited: ' + code)));
        });
        const tabs = await (await fetch(new URL('/json/list', debuggerUrl.replace('ws:', 'http:')))).json();
        socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
        await once(socket, 'open');
        let sequence = 0; const pending = new Map();
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data), request = pending.get(data.id);
            if (request) { pending.delete(data.id); data.error ? request.reject(Error(data.error.message)) : request.resolve(data.result); }
        });
        const send = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; pending.set(id, {resolve, reject}); socket.send(JSON.stringify({id, method, params})); });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
            assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails)); return result.result.value;
        };
        await send('Page.navigate', {url: 'http://127.0.0.1:' + server.address().port + '/#settings'});
        const waitFor = async expression => {
            for (let attempt = 0; attempt < 100; attempt++) {
                if (await evaluate(expression)) return;
                await new Promise(resolve => setTimeout(resolve, 25));
            }
            assert.fail('Timed out: ' + expression);
        };
        await waitFor("document.getElementById('device-change-root-password')?.disabled === false");
        await waitFor("document.getElementById('device-storage-status').textContent.includes('Please connect')");
        assert.equal(await evaluate("getComputedStyle(document.getElementById('device-storage-limit-form')).display"), 'none');
        assert.match(await evaluate("document.getElementById('device-storage-status').textContent"), /Please connect/);
        await waitFor("document.getElementById('osd-language')?.disabled === false");
        assert.equal(await evaluate("document.getElementById('osd-language').value"), 'en');
        assert.equal(await evaluate("document.getElementById('osd-language').labels[0].textContent"), 'Language');
        for (const language of ['de', 'en']) {
            await evaluate("document.getElementById('osd-language').value = '" + language + "'; document.getElementById('save-settings').click()");
            await waitFor("document.getElementById('settings-status').textContent === 'Saved.'");
            assert.equal(osd.osd_language, language);
            assert.equal(osd.osd_timeout_ms, 5000);
            await evaluate("window.navigateReceiver('status'); window.navigateReceiver('settings')");
            await waitFor("document.getElementById('osd-language').disabled === false");
            assert.equal(await evaluate("document.getElementById('osd-language').value"), language);
        }
        const disk = {id: 'disk-one', name: 'sda', model: 'Test SSD', serial: '123', bytes: 256 * 1024**3, busy: false, filesystems: [{type: 'ext4'}]};
        storage = {disks: [disk], mounted: false};
        await evaluate('window.loadDeviceSettings(true)');
        for (const [width, height] of [[1200, 1000], [390, 844]]) {
            await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width < 500});
            assert.deepEqual(await evaluate(
                "(() => { const status = document.getElementById('device-storage-status'); const heading = status.previousElementSibling; const choose = document.getElementById('device-storage-choose'); return [status.getBoundingClientRect().top - heading.getBoundingClientRect().bottom, choose.getBoundingClientRect().top - status.getBoundingClientRect().bottom]; })()"
            ), [16, 16]);
            for (const id of ['device-form', 'device-wifi-form', 'osd-settings']) {
                assert.deepEqual(await evaluate(
                    "(() => { const form = document.getElementById('" + id + "'); const style = getComputedStyle(form); const label = form.querySelector('label'); const input = label.nextElementSibling; return {display: style.display, gap: style.rowGap, stacked: input.getBoundingClientRect().top >= label.getBoundingClientRect().bottom}; })()"
                ), {display: 'grid', gap: '10px', stacked: true});
            }
            assert.deepEqual(await evaluate(
                "(() => { const style = id => getComputedStyle(document.getElementById(id)); const osd = style('osd-timeout'), device = style('device-hostname'); return ['backgroundColor', 'borderRadius', 'paddingTop', 'paddingLeft'].map(key => osd[key] === device[key]); })()"
            ), [true, true, true, true]);
            assert.equal(await evaluate(
                "(() => { const input = document.getElementById('osd-timeout').getBoundingClientRect(); const form = document.getElementById('osd-settings').getBoundingClientRect(); return input.width === form.width; })()"
            ), true);
            assert.equal(await evaluate(
                "(() => { const card = document.getElementById('osd-settings-card'); return card.nextElementSibling.getBoundingClientRect().top - card.getBoundingClientRect().bottom; })()"
            ), 22);
            await evaluate("document.getElementById('device-storage-choose').click()");
            assert.equal(await evaluate("document.getElementById('device-storage-dialog').open"), true);
            assert.equal(await evaluate('document.activeElement.id'), 'device-storage-disk');
            assert.equal(await evaluate("document.getElementById('device-storage-dialog').getBoundingClientRect().right <= innerWidth"), true);
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
            await evaluate("document.getElementById('device-storage-cancel').click()");
        }
        storage = {disks: [{...disk, busy: true}], mounted: true, ready: true, device: disk,
            device_id: disk.id, total_bytes: disk.bytes, free_bytes: 200 * 1024**3, max_bytes: 8 * 1024**3};
        await evaluate('window.loadDeviceSettings(true)');
        assert.match(await evaluate("document.getElementById('device-storage-status').textContent"), /sda · Test SSD · 256.0 GiB capacity · 200.0 GiB free/);
        assert.equal(await evaluate("document.getElementById('device-storage-choose').hidden"), true);
        assert.equal(await evaluate("document.getElementById('device-storage-gib').max"), '256');
        assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
        for (const [width, height] of [[1200, 1000], [390, 844]]) {
            await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width < 500});
            assert.deepEqual(await evaluate(
                "(() => { const rect = id => document.getElementById('device-storage-' + id).getBoundingClientRect(); const status = rect('status'), eject = rect('eject'), form = rect('limit-form'), input = rect('gib'), save = rect('save'); const label = document.querySelector('#device-storage-limit-form label').getBoundingClientRect(); return [eject.top - status.bottom, form.top - eject.bottom, input.top - label.bottom, save.top - input.bottom]; })()"
            ), [16, 16, 10, 10]);
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
        }
        const dialogOpen = "document.getElementById('device-change-password-dialog').open";
        const fill = "for (const id of ['new', 'confirm']) document.getElementById('device-change-' + id).value = 'test-secret'";
        for (const [width, height] of [[1200, 1000], [390, 844]]) {
            await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: width < 500});
            for (const kind of ['web', 'root']) {
                await evaluate("document.getElementById('device-change-" + kind + "-password').click()");
                assert.equal(await evaluate(dialogOpen), true);
                assert.equal(await evaluate('document.activeElement.id'), 'device-change-new');
                assert.equal(await evaluate("document.getElementById('device-change-password-dialog').getBoundingClientRect().right <= innerWidth"), true);
                assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
                await evaluate(fill);
                if (kind === 'web') await evaluate("document.getElementById('device-change-password-cancel').click()");
                else {
                    await send('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
                    await send('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27});
                }
                await waitFor('!' + dialogOpen);
                assert.equal(await evaluate("['new', 'confirm'].every(id => document.getElementById('device-change-' + id).value === '')"), true);
                assert.equal(await evaluate('document.activeElement.id'), 'device-change-' + kind + '-password');
            }
        }
        assert.equal(calls.length, 0);
        await evaluate("document.getElementById('device-change-root-password').click(); " + fill + "; document.getElementById('device-change-confirm').value = 'different'; document.getElementById('device-change-password-save').click()");
        assert.equal(calls.length, 0);
        assert.match(await evaluate("document.getElementById('device-change-password-message').textContent"), /do not match/);
        fail = true;
        await evaluate(fill + "; document.getElementById('device-change-password-save').click()");
        await waitFor("document.getElementById('device-change-password-message').textContent.includes('unavailable')");
        assert.equal(await evaluate(dialogOpen), true);
        assert.equal(await evaluate("document.getElementById('device-change-new').value"), '');
        fail = false;
        await evaluate(fill + "; document.getElementById('device-change-password-save').click()");
        await waitFor('!' + dialogOpen);
        assert.equal(calls.at(-1).action, 'root_password');
        assert.ok(!('current_password' in calls.at(-1)));
        assert.equal(await evaluate("document.getElementById('device-password-dialog').open"), false);
        await evaluate("document.getElementById('device-change-web-password').click(); " + fill + "; document.getElementById('device-change-password-save').click()");
        await waitFor('!' + dialogOpen);
        assert.equal(calls.at(-1).action, 'web_password');
        assert.ok(!('current_password' in calls.at(-1)));
        assert.match(await evaluate("document.getElementById('device-access-message').textContent"), /Sign in again/);
    } finally {
        socket?.close();
        chrome.kill('SIGTERM');
        server.closeAllConnections();
        await new Promise(resolve => server.close(resolve));
    }
});

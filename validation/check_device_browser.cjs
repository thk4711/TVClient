// Read-only appliance layout/password-dialog check. No device POST is sent.
// Requires Node 22+, Chrome and a new disposable profile. Login is read over SSH
// into process memory, never argv, console output or browser storage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');

async function main() {
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR, 'Set Chrome binary and a disposable profile');
    assert.ok(process.env.SATIP_DEVICE_URL, 'Set SATIP_DEVICE_URL explicitly');
    const url = new URL(process.env.SATIP_DEVICE_URL);
    assert.ok(url.protocol === 'http:' && require('node:net').isIP(url.hostname) === 4 && !url.username && !url.password);
    const login = JSON.parse(execFileSync('ssh', ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        'root@' + url.hostname, 'cat /etc/satip-web/login.json'], {encoding: 'utf8', timeout: 10000}));
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless', '--no-first-run', '--no-default-browser-check',
        '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--user-data-dir=' + process.env.CHROME_USER_DATA_DIR, 'about:blank']);
    let socket;
    try {
        const debuggerUrl = await new Promise((resolve, reject) => {
            let text = '';
            chrome.stderr.on('data', data => { text += data; const match = text.match(/DevTools listening on (.+)/); if (match) resolve(match[1].trim()); });
            chrome.on('error', reject); chrome.on('exit', () => reject(Error('Chrome exited')));
        });
        const tabs = await (await fetch(new URL('/json/list', debuggerUrl.replace('ws:', 'http:')))).json();
        socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
        await once(socket, 'open');
        let sequence = 0; const pending = new Map(); let posts = 0;
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data), request = pending.get(data.id);
            if (request) { pending.delete(data.id); data.error ? request.reject(Error(data.error.message)) : request.resolve(data.result); }
            if (data.method === 'Network.requestWillBeSent' && data.params.request.method !== 'GET') posts++;
        });
        const send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++sequence; pending.set(id, {resolve, reject}); socket.send(JSON.stringify({id, method, params}));
        });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
            assert.ok(!result.exceptionDetails, 'Browser evaluation failed'); return result.result.value;
        };
        await send('Network.enable');
        await send('Network.setExtraHTTPHeaders', {headers: {Authorization: 'Basic ' + Buffer.from(login.username + ':' + login.password).toString('base64')}});
        login.password = '';
        await send('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
        await send('Page.navigate', {url: url.origin + '/#settings'});
        for (let attempt = 0; attempt < 150; attempt++) {
            if (await evaluate("!!document.getElementById('device-timezone')?.options.length && !document.getElementById('device-hostname').disabled")) break;
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.equal(await evaluate("document.getElementById('device-settings').hidden"), false);
        assert.ok(await evaluate("document.getElementById('device-timezone').options.length > 50"));
        const screenshot = async name => {
            if (!process.env.DEVICE_SCREENSHOT_DIR) return;
            fs.mkdirSync(process.env.DEVICE_SCREENSHOT_DIR, {recursive: true, mode: 0o700});
            fs.writeFileSync(process.env.DEVICE_SCREENSHOT_DIR + '/' + name + '.png',
                Buffer.from((await send('Page.captureScreenshot', {captureBeyondViewport:true})).data, 'base64'), {mode:0o600});
        };
        for (const width of [1440, 390]) {
            await send('Emulation.setDeviceMetricsOverride', {width,height:1000,deviceScaleFactor:1,mobile:width===390});
            await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'Settings overflow');
            assert.equal(await evaluate("(() => { const cards = Array.from(document.querySelectorAll('#page-settings .card'), n => n.getBoundingClientRect()).filter(r => r.width); return cards.every((r, i) => Math.abs(r.left - cards[0].left) < 1 && Math.abs(r.width - cards[0].width) < 1 && (!i || r.top >= cards[i - 1].bottom)); })()"), true, 'Settings sections must share one column');
            const fields = await evaluate("Array.from(document.querySelectorAll('#device-settings input:not([type=checkbox]), #device-settings select')).filter(n => n.getClientRects().length).map(n => ({id: n.id, width: n.clientWidth, scroll: n.scrollWidth}))");
            assert.ok(fields.every(n => n.width > 100 && n.scroll <= n.width), 'Settings field overflow: ' + JSON.stringify(fields));
            await screenshot(width === 1440 ? 'settings-desktop' : 'settings-mobile');
            // Open each real dialog, set only dummy fields, then cancel without
            // submitting the form or issuing any password/device action.
            for (const account of ['root', 'web']) {
                await evaluate("document.getElementById('device-change-" + account + "-password').click()");
                assert.equal(await evaluate("document.getElementById('device-change-password-dialog').open"), true);
                assert.equal(await evaluate("document.getElementById('device-change-new').type"), 'password');
                assert.equal(await evaluate("document.getElementById('device-change-confirm').type"), 'password');
                assert.equal(await evaluate("document.activeElement.id"), 'device-change-new');
                await evaluate("document.getElementById('device-change-new').value = document.getElementById('device-change-confirm').value = 'dummy-test'");
                await screenshot('password-' + account + '-' + width);
                await evaluate("document.getElementById('device-change-password-cancel').click(); new Promise(resolve => setTimeout(resolve, 30))");
                assert.equal(await evaluate("document.getElementById('device-change-new').value"), '');
                assert.equal(await evaluate("document.getElementById('device-change-confirm').value"), '');
                assert.equal(await evaluate("document.getElementById('device-change-password-dialog').open"), false);
            }
        }
        assert.equal(posts, 0, 'Read-only browser check must not send actions');
        console.log('PASS: live desktop/mobile settings layout, masked focused password dialog, cancellation; no actions sent');
    } finally {
        if (socket) socket.close(); chrome.kill();
    }
}
main().catch(error => {
    console.error(error.code === 'ERR_ASSERTION' ? error.message : 'Device browser check failed (credentials suppressed)');
    process.exitCode = 1;
});

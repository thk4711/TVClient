// Explicit live-device acceptance: briefly pauses HDMI, then restores it.
// Use direct trusted-LAN access, SATIP_PREVIEW_URL, SATIP_PREVIEW_SERVICES
// (comma-separated UUIDs), SATIP_CONFIRM_INTERRUPT=yes, CHROME_BINARY and a fresh
// CHROME_USER_DATA_DIR. Login is read over root SSH into memory, not argv/storage.
// Never run this as part of the ordinary unit suite.
const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');

async function main() {
    assert.equal(process.env.SATIP_CONFIRM_INTERRUPT, 'yes', 'Set SATIP_CONFIRM_INTERRUPT=yes to interrupt HDMI');
    assert.ok(process.env.SATIP_PREVIEW_URL && process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR,
        'Set SATIP_PREVIEW_URL, CHROME_BINARY and a fresh CHROME_USER_DATA_DIR');
    const target = new URL(process.env.SATIP_PREVIEW_URL);
    assert.ok(target.protocol === 'http:' && require('node:net').isIP(target.hostname) === 4 && !target.username && !target.password);
    const base = target.origin;
    const services = (process.env.SATIP_PREVIEW_SERVICES || '').split(',');
    assert.ok(services.length > 0 && services.every(s => /^[0-9a-f]{32}$/.test(s)), 'Set SATIP_PREVIEW_SERVICES to target service UUIDs');
    const login = JSON.parse(execFileSync('ssh', ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        'root@' + target.hostname, 'cat /etc/satip-web/login.json'], {encoding: 'utf8', timeout: 10000}));
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless', '--no-first-run',
        '--no-default-browser-check', '--autoplay-policy=no-user-gesture-required',
        '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
        '--user-data-dir=' + process.env.CHROME_USER_DATA_DIR, 'about:blank']);
    let socket, send, evaluate;
    try {
        const debuggerUrl = await new Promise((resolve, reject) => {
            let text = '';
            chrome.stderr.on('data', data => {
                text += data;
                const match = text.match(/DevTools listening on (.+)/);
                if (match) resolve(match[1].trim());
            });
            chrome.on('error', reject);
            chrome.on('exit', code => reject(Error('Chrome exited: ' + code)));
        });
        const tabs = await (await fetch(new URL('/json/list', debuggerUrl.replace('ws:', 'http:')))).json();
        socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
        await once(socket, 'open');
        let sequence = 0;
        const pending = new Map();
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data), request = pending.get(data.id);
            if (request) {
                pending.delete(data.id);
                data.error ? request.reject(Error(data.error.message)) : request.resolve(data.result);
            }
        });
        send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++sequence; pending.set(id, {resolve, reject});
            socket.send(JSON.stringify({id, method, params}));
        });
        evaluate = async expression => {
            const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
            assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
            return result.result.value;
        };
        const wait = async (expression, timeout = 60000) => {
            const end = Date.now() + timeout;
            while (Date.now() < end) {
                if (await evaluate(expression)) return;
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            const diagnostic = await evaluate('(() => { const v=document.getElementById("preview-video"); return {state:previewState,sessionActive:!!previewSession,message:element("message").textContent,readyState:v.readyState,paused:v.paused,time:v.currentTime,src:v.currentSrc,nativeHls:v.canPlayType("application/vnd.apple.mpegurl"),hlsSupported:window.Hls && Hls.isSupported(),error:v.error && v.error.code}; })()');
            throw Error('Timed out: ' + expression + ' ' + JSON.stringify(diagnostic));
        };
        await send('Network.enable');
        await send('Network.setExtraHTTPHeaders', {headers: {Authorization: 'Basic ' + Buffer.from(login.username + ':' + login.password).toString('base64')}});
        login.password = '';
        await send('Page.navigate', {url: base + '/'});
        await wait('typeof connected !== "undefined" && connected && previewState.enabled');
        await evaluate('window.confirm = () => true');
        for (const service of services) {
            assert.ok(await evaluate('catalogue.some(c => c.service_uuid === ' + JSON.stringify(service) + ')'), 'Selected service is not in the catalogue');
            await evaluate('startPreview(catalogue.find(c => c.service_uuid === ' + JSON.stringify(service) + '))');
            await wait('document.getElementById("preview-video").currentTime > 3');
            const sample = await evaluate('(() => { const v=element("preview-video"); return {channel:previewState.channel_name, time:v.currentTime, width:v.videoWidth, height:v.videoHeight, frames:v.getVideoPlaybackQuality().totalVideoFrames, audioDecodedBytes:v.webkitAudioDecodedByteCount, muted:v.muted, volume:v.volume, error:v.error && v.error.code}; })()');
            assert.ok(sample.frames > 0 && sample.width > 0 && !sample.error);
            assert.equal(sample.muted, false);
            assert.ok(sample.volume > 0);
            if (sample.audioDecodedBytes !== undefined) assert.ok(sample.audioDecodedBytes > 0);
            console.log(JSON.stringify(sample));
            await evaluate('stopPreview()');
            await wait('!previewSession && !busy && !element("start-scan").disabled', 30000);
        }
        console.log('Chrome displayed selected previews and decoded audio; sessions stopped. Physical output remains unverified.');
    } finally {
        if (evaluate) {
            try { await evaluate('typeof stopPreview === "function" && stopPreview()'); } catch (_) {}
        }
        if (socket) socket.close();
        chrome.kill();
    }
}
main().catch(error => { console.error(error); process.exitCode = 1; });

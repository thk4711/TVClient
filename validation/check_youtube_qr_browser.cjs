// Read-only test of deployed assets: replace YouTube API calls with synthetic
// responses before navigation. No real pairing, disable or playback requests.
// Set SATIP_QR_URL, CHROME_BINARY, CHROME_USER_DATA_DIR; optional credentials
// SATIP_QR_USERNAME/PASSWORD and SATIP_QR_DECODER (path to a jsQR decoder).
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

async function main() {
    const base = process.env.SATIP_QR_URL;
    assert.ok(base && process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR,
        'Set SATIP_QR_URL, CHROME_BINARY and a disposable CHROME_USER_DATA_DIR');
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless', '--no-first-run',
        '--no-default-browser-check', '--remote-debugging-port=0',
        '--remote-debugging-address=127.0.0.1',
        '--user-data-dir=' + process.env.CHROME_USER_DATA_DIR, 'about:blank']);
    let socket;
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
        const send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++sequence; pending.set(id, {resolve, reject});
            socket.send(JSON.stringify({id, method, params}));
        });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
            assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
            return result.result.value;
        };
        const wait = async expression => {
            const end = Date.now() + 15000;
            while (Date.now() < end) {
                if (await evaluate(expression)) return;
                await new Promise(resolve => setTimeout(resolve, 100));
            }
            throw Error('Timed out: ' + expression);
        };
        let fixture = {enabled:true,state:'ready',pairing_code:'123456789012'};
        // Intercept at the network boundary; never let a YouTube action reach the Pi.
        // Handle authentication only for this receiver, never another origin.
        await send('Fetch.enable', {handleAuthRequests: true});
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data);
            if (data.method === 'Fetch.requestPaused') {
                const url = new URL(data.params.request.url);
                if (url.origin === new URL(base).origin && url.pathname === '/api/youtube') {
                    void send('Fetch.fulfillRequest', {requestId:data.params.requestId, responseCode:200,
                        responseHeaders:[{name:'Content-Type',value:'application/json'}],
                        body:Buffer.from(JSON.stringify(fixture)).toString('base64')});
                } else {
                    void send('Fetch.continueRequest', {requestId:data.params.requestId});
                }
            } else if (data.method === 'Fetch.authRequired') {
                const own = new URL(data.params.request.url).origin === new URL(base).origin;
                void send('Fetch.continueWithAuth', {requestId:data.params.requestId,
                    authChallengeResponse: own && process.env.SATIP_QR_USERNAME ?
                        {response:'ProvideCredentials', username:process.env.SATIP_QR_USERNAME,
                            password:process.env.SATIP_QR_PASSWORD} : {response:'CancelAuth'}});
            }
        });
        await send('Page.enable');
        await send('Page.navigate', {url: base + '/'});
        await wait("document.getElementById('youtube-pair') && typeof qrcode === 'function' && !!document.getElementById('youtube-pair').onclick");
        await evaluate("location.hash = '#settings'");
        await wait("!document.getElementById('page-settings').hidden");
        assert.equal(await evaluate("document.getElementById('youtube-qr').hidden"), true);
        await evaluate("document.getElementById('youtube-pair').onclick()");
        await wait("!document.getElementById('youtube-qr').hidden");
        assert.equal(await evaluate("document.getElementById('youtube-code').textContent"), '123 456 789 012');
        const pixels = await evaluate("(() => { const c=document.getElementById('youtube-qr-image'); return {width:c.width,height:c.height,data:Array.from(c.getContext('2d').getImageData(0,0,c.width,c.height).data)}; })()");
        assert.ok(pixels.width >= 200 && pixels.width === pixels.height);
        assert.deepEqual(pixels.data.slice(0, 4), [255,255,255,255]);
        assert.ok(pixels.data.some((value, index) => index % 4 !== 3 && value === 0));
        if (process.env.SATIP_QR_DECODER) {
            const decode = require(process.env.SATIP_QR_DECODER);
            const result = decode(new Uint8ClampedArray(pixels.data), pixels.width, pixels.height);
            assert.equal(result && result.data, 'https://www.youtube.com/pair?code=123456789012');
            console.log('Independent decoder verified the QR payload (synthetic code).');
        }
        await send('Emulation.setDeviceMetricsOverride', {width:375,height:812,deviceScaleFactor:2,mobile:true});
        assert.ok(await evaluate("document.getElementById('youtube-qr-image').getBoundingClientRect().right <= innerWidth"));
        fixture.pairing_code = null;
        await wait("document.getElementById('youtube-qr').hidden");
        assert.equal(await evaluate("document.getElementById('youtube-pairing-link').hasAttribute('href')"), false);
        fixture.pairing_code = '000000000001';
        await wait("!document.getElementById('youtube-qr').hidden");
        assert.equal(await evaluate("document.getElementById('youtube-code').textContent"), '000 000 000 001');
        await evaluate("document.getElementById('youtube-disable').onclick()");
        assert.equal(await evaluate("document.getElementById('youtube-qr').hidden"), true);
        console.log('Deployed QR rendering, mobile layout, replacement and clearing passed. No real YouTube actions sent.');
    } finally {
        if (socket) socket.close();
        chrome.kill();
    }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });

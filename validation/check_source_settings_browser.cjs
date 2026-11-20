// Read-only live Settings check. Node 22+, Chrome, disposable browser profile.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

async function main() {
    const url = new URL(process.env.SATIP_SETTINGS_URL);
    assert.equal(url.protocol, 'http:');
    assert.ok(require('node:net').isIP(url.hostname) === 4 && !url.username && !url.password);
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR);
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless', '--no-first-run', '--no-default-browser-check',
        '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
        '--user-data-dir=' + process.env.CHROME_USER_DATA_DIR, 'about:blank']);
    let socket;
    try {
        const debug = await new Promise((resolve, reject) => {
            let text = '';
            chrome.stderr.on('data', chunk => { text += chunk; const m = text.match(/DevTools listening on (.+)/); if (m) resolve(m[1].trim()); });
            chrome.on('error', reject);
        });
        const tabs = await (await fetch(new URL('/json/list', debug.replace('ws:', 'http:')))).json();
        socket = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
        await once(socket, 'open');
        let sequence = 0;
        const pending = new Map(), errors = [];
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data), task = pending.get(data.id);
            if (task) { pending.delete(data.id); data.error ? task.reject(Error(data.error.message)) : task.resolve(data.result); }
            if (data.method === 'Runtime.exceptionThrown') errors.push('Uncaught browser exception');
            if (data.method === 'Fetch.requestPaused') {
                const allowed = data.params.request.method === 'GET';
                if (!allowed) errors.push('Unexpected non-GET request blocked');
                send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', allowed ?
                    {requestId:data.params.requestId} : {requestId:data.params.requestId,errorReason:'BlockedByClient'}).catch(e=>errors.push(e.message));
            }
        });
        const send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++sequence; pending.set(id, {resolve,reject}); socket.send(JSON.stringify({id,method,params}));
        });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate', {expression,returnByValue:true,awaitPromise:true});
            assert.ok(!result.exceptionDetails, 'Browser evaluation failed'); return result.result.value;
        };
        const wait = async expression => {
            for (let i=0;i<150;i++) { if (await evaluate(expression)) return; await new Promise(r=>setTimeout(r,100)); }
            assert.fail('Settings did not load: ' + expression);
        };
        await send('Page.enable'); await send('Runtime.enable');
        await send('Fetch.enable', {patterns:[{urlPattern:'*'}]});
        await send('Page.navigate', {url:url.href});
        await wait('window.sourceConfiguration?.satip?.configured_frontend && !document.getElementById("device-settings").hidden');
        const status = await evaluate('({name:document.getElementById("source-current-device").textContent,tuner:document.getElementById("source-current-tuner").textContent,receiving:document.getElementById("source-receiving").textContent})');
        console.log(status);
        assert.match(status.name, /Server:/); assert.match(status.tuner, /#2/); assert.match(status.receiving, /Currently receiving:/);
        assert.equal(await evaluate('document.getElementById("source-apply-tuner").disabled'), true);
        assert.equal(await evaluate('document.getElementById("source-activate").disabled'), true);
        assert.equal(await evaluate('document.getElementById("source-enigma-help").hidden'), true);
        assert.equal(await evaluate('document.getElementById("source-discovery-controls").hidden'), true);
        assert.equal(await evaluate('document.getElementById("source-satip-device")'), null);
        assert.equal(await evaluate('document.querySelector("label[for=source-tuner]").textContent'), 'Change tuner');
        await evaluate('document.getElementById("source-tuner").value=window.sourceConfiguration.satip.tuners.find(t=>!t.configured).uuid;document.getElementById("source-tuner").dispatchEvent(new Event("change"))');
        assert.equal(await evaluate('document.getElementById("source-apply-tuner").disabled'), false);
        await evaluate('document.getElementById("source-kind").value="enigma2";document.getElementById("source-kind").dispatchEvent(new Event("change"))');
        assert.equal(await evaluate('document.getElementById("source-satip-panel").hidden'), true);
        assert.equal(await evaluate('document.getElementById("source-enigma-help").hidden'), false);
        assert.equal(await evaluate('document.getElementById("source-discovery-controls").hidden'), false);
        assert.equal(await evaluate('document.getElementById("source-activate").textContent'), 'Activate Enigma2 source');
        // Browsing Enigma2 did not change the current SAT-IP summary.
        assert.match(await evaluate('document.getElementById("source-active").textContent'), /SAT-IP/);
        for (const width of [1280,390]) {
            await send('Emulation.setDeviceMetricsOverride', {width,height:900,deviceScaleFactor:1,mobile:width===390});
            await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
            assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true, 'Horizontal overflow');
        }
        assert.deepEqual(errors, []);
        console.log('PASS: live configuration, explicit tuner/activation controls, source-specific help and desktop/mobile layout; GET-only.');
    } finally { if (socket) socket.close(); chrome.kill(); }
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});

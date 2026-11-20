// Real-browser setup recovery against an isolated localhost API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {test} = require('node:test');

test('setup recovers requests, retains errors and selections, and enters management', {timeout: 30000}, async () => {
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR);
    let stall = true, offline = false, configured = false, selections = 0, expectedFrontend = '3'.repeat(32), restoreFails = false, restores = 0;
    let channelFault = '', channelPrepares = 0, channelUploads = 0, channelRestores = 0, channelStatuses = 0;
    const device = {url:'http://192.168.1.10:38400/description.xml', udn:'uuid:28146e70-1dd2-11b2-8021-c19f52d1fd53',
        name:'Digibit Twin', address:'192.168.1.10', tuners:2};
    let state = {state:'unconfigured', scan:{state:'idle'}, candidates:[], prepared:false, recovery_pending:false,
        discovery:{state:'idle',servers:[],interfaces:['wlan0'],warnings:[]},registration:{state:'idle'},tuners:[],scanfiles:[]};
    const server = http.createServer(async (request, response) => {
        response.setHeader('Cache-Control', 'no-store');
        if (request.url.startsWith('/api/device')) {
            assert.equal(request.headers.authorization, undefined);
            assert.equal(request.headers['x-satip-action'], '1');
            response.setHeader('Content-Type', 'application/json');
            if (request.url.startsWith('/api/device/channels/upload/')) {
                assert.equal(request.headers['content-type'], 'application/octet-stream');
                assert.match(request.url, /\/[0-9a-f]{32}$/);
                let size = 0; for await (const chunk of request) size += chunk.length;
                assert.equal(size, 40000); channelUploads++;
                if (channelFault === 'upload_timeout') return;
                if (channelFault === 'validation') {
                    response.writeHead(409); response.end(JSON.stringify({detail:'Wrong satellite fixture'})); return;
                }
                response.end(JSON.stringify({available:3,favourites:2,created_at:'2026-11-20'})); return;
            }
            let body = ''; for await (const chunk of request) body += chunk;
            const payload = JSON.parse(body);
            assert.match(payload.args.id, /^[0-9a-f]{32}$/);
            if (payload.action === 'backup_channels_restore') {
                channelRestores++; channelStatuses = 0;
                response.end(JSON.stringify({state:'working'})); return;
            }
            assert.equal(payload.action, 'backup_status'); channelStatuses++;
            if (channelFault === 'reconnect' && channelStatuses === 1) { response.destroy(); return; }
            if (channelFault === 'reconnect') configured = true;
            response.end(JSON.stringify({state:channelFault === 'reconnect' ? 'restored' : 'working'})); return;
        }
        if (request.url.startsWith('/api/setup/')) {
            response.setHeader('Content-Type', 'application/json');
            const action = request.url.slice('/api/setup/'.length);
            assert.equal(request.headers.authorization, undefined);
            if (action === 'inputs') {
                response.end(JSON.stringify({devices:[{id:'1234:abcd',name:'Living room keyboard',keyboard:true},
                    {id:'ir',name:'Infrared receiver connected to GPIO',remote:true},
                    {id:'5678:ef01',name:'Additional USB remote'}],selected:'1234:abcd',
                    test:{id:'1234:abcd',device:'Living room keyboard',code:103,time:1}})); return;
            }
            if (action === 'preferences' && request.method === 'GET') {
                response.end(JSON.stringify({hostname:'satip',timezone:'Etc/UTC',timezones:['Etc/UTC','Europe/Berlin']})); return;
            }
            if (action === 'input') {
                let body=''; for await (const chunk of request) body+=chunk;
                assert.deepEqual(JSON.parse(body), {id:'5678:ef01'});
                response.end(JSON.stringify({message:'Control saved.'})); return;
            }
            if (action === 'status') {
                if (offline) { response.writeHead(503); response.end('{}'); return; }
                response.end(JSON.stringify(configured ? {state:'configured'} : state)); return;
            }
            if (action === 'discover') {
                if (stall) return;
                state.discovery.state = 'complete';
                response.end(JSON.stringify({state:'searching'})); return;
            }
            if (action === 'config/restore') {
                restores++;
                assert.equal(request.headers['x-satip-action'], '1');
                if (restoreFails) { response.writeHead(409); response.end(JSON.stringify({detail:'Restore failed fixture'})); return; }
                response.end(JSON.stringify({state:'restored'})); return;
            }
            if (action === 'server/add') {
                state.discovery.servers = [device];
                response.end(JSON.stringify(device)); return;
            }
            if (action === 'server/select') {
                let body = ''; for await (const chunk of request) body += chunk;
                const selection = JSON.parse(body);
                state.selected_server = state.discovery.servers.find(server => server.udn === selection.udn);
                assert.ok(state.selected_server); selections++; state.registration = {state:'ready'};
                state.tuners = [1,2].map(n=>({server_uuid:'1'.repeat(32), frontend_uuid:String(n+1).repeat(32), server:device.address, tuner_number:n}));
                state.scanfiles = [{key:'astra',val:'Astra'}];
                response.end(JSON.stringify({state:'connecting'})); return;
            }
            if (action === 'channels/prepare') {
                let body = ''; for await (const chunk of request) body += chunk;
                assert.deepEqual(JSON.parse(body), {server_uuid:'1'.repeat(32),frontend_uuid:expectedFrontend});
                assert.equal(state.scan.state, 'idle'); channelPrepares++; state.prepared = true;
                response.end(JSON.stringify({state:'prepared'})); return;
            }
            if (action === 'scan') {
                let body = ''; for await (const chunk of request) body += chunk;
                assert.deepEqual(JSON.parse(body), {server_uuid:'1'.repeat(32), frontend_uuid:expectedFrontend});
                state = {...state, scan:{state:'running',total:2,completed:1,found:1,multiplex:'10773H'}, recovery_pending:true,
                    prepared:true, selection_locked:true, frontend_uuid:expectedFrontend, candidates:[{service_uuid:'3'.repeat(32),name:'Fixture TV'}]};
            }
            if (action === 'cancel') { state.scan = {state:'cancelled'}; state.recovery_pending = false; }
            if (action === 'recover') state.recovery_pending = false;
            if (action === 'finish') {
                let body=''; for await (const chunk of request) body+=chunk;
                assert.deepEqual(JSON.parse(body), {services:['4'.repeat(32),'3'.repeat(32)]});
                state.completion_pending = true;
            }
            response.end(JSON.stringify({message:'Starting management…'})); return;
        }
        if (configured && request.url === '/') { response.end('<h1>Management ready</h1>'); return; }
        const file = request.url === '/' ? 'setup.html' : request.url.slice(1);
        if (!['setup.html','setup.js','request.js','app.css'].includes(file)) { response.writeHead(404); response.end(); return; }
        response.setHeader('Content-Type', file.endsWith('.html') ? 'text/html' : file.endsWith('.js') ? 'text/javascript' : 'text/css');
        response.end(fs.readFileSync('satip_controller/static/' + file));
    });
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const chrome = spawn(process.env.CHROME_BINARY, ['--headless','--no-first-run','--no-default-browser-check',
        '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+process.env.CHROME_USER_DATA_DIR,'about:blank']);
    let socket;
    try {
        const debuggerUrl = await new Promise((resolve, reject) => {
            let text = '';
            chrome.stderr.on('data', data => { text += data; const match = text.match(/DevTools listening on (.+)/); if (match) resolve(match[1].trim()); });
            chrome.on('error', reject); chrome.on('exit', code => reject(Error('Chrome exited: '+code)));
        });
        const tabs = await (await fetch(new URL('/json/list', debuggerUrl.replace('ws:', 'http:')))).json();
        socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl); await once(socket, 'open');
        let sequence = 0; const pending = new Map();
        socket.addEventListener('message', event => {
            const data = JSON.parse(event.data), task = pending.get(data.id);
            if (task) { pending.delete(data.id); data.error ? task.reject(Error(data.error.message)) : task.resolve(data.result); }
        });
        const send = (method, params={}) => new Promise((resolve,reject) => { const id=++sequence; pending.set(id,{resolve,reject}); socket.send(JSON.stringify({id,method,params})); });
        const evaluate = async expression => {
            const result = await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
            assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails)); return result.result.value;
        };
        const wait = async expression => {
            for (let n=0;n<150;n++) { if (await evaluate(expression)) return; await new Promise(resolve=>setTimeout(resolve,20)); }
            assert.fail('Timed out: '+expression+' · '+JSON.stringify(await evaluate('({body:document.body.innerText, pending:typeof pending==="undefined"?null:pending, timer:window.setTimeout.toString()})')));
        };
        // Exercise actual AbortController handling without waiting 30 seconds.
        await send('Page.enable');
        await send('Page.addScriptToEvaluateOnNewDocument',{source:'const nativeTimeout=window.setTimeout; window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,ms===30000?100:ms===2000?50:ms,...args);'});
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
        await wait('typeof connected !== "undefined" && connected && !document.getElementById("discover").disabled');
        assert.equal(await evaluate('document.getElementById("restore-config").textContent'), 'Restore default satip-config.txt');
        restoreFails = true;
        await evaluate('document.getElementById("restore-config").click()');
        await wait('document.getElementById("message").textContent.includes("Restore failed fixture") && !document.getElementById("restore-config").disabled');
        restoreFails = false;
        await evaluate('document.getElementById("restore-config").click()');
        await wait('document.getElementById("restore-config-status").textContent.includes("Default satip-config.txt restored") && !document.getElementById("restore-config").disabled');
        assert.equal(restores, 2);
        await evaluate('document.getElementById("discover").click()');
        await wait('document.getElementById("message").textContent.includes("timed out") && !document.getElementById("discover").disabled');
        await new Promise(resolve=>setTimeout(resolve,100));
        assert.match(await evaluate('document.getElementById("message").textContent'), /timed out/);
        stall = false;
        await wait('!document.getElementById("save-input").disabled');
        assert.equal(await evaluate('document.getElementById("save-input").hidden'), true);
        assert.match(await evaluate('document.getElementById("input-device").textContent'), /Infrared receiver connected to GPIO/);
        await evaluate('document.getElementById("input-device").value="5678:ef01"; document.getElementById("input-device").dispatchEvent(new Event("change"))');
        assert.equal(await evaluate('document.getElementById("save-input").hidden'), false);
        await evaluate('document.getElementById("save-input").click()');
        await wait('document.getElementById("input-status").textContent.includes("Control saved")');
        assert.match(await evaluate('document.getElementById("input-test").textContent'), /Up.*Living room keyboard/);
        await evaluate('document.getElementById("discover").click()');
        await wait('document.getElementById("discovery-status").textContent.includes("No SAT-IP server replied")');
        assert.equal(await evaluate('document.getElementById("scan").disabled'), true);
        await evaluate('document.querySelector("details").open=true; document.getElementById("server-url").value="http://192.168.1.10:38400/description.xml"; document.getElementById("add-server").click()');
        await wait('document.querySelectorAll("#tuner option").length===2 && !document.getElementById("scan").disabled');
        assert.equal(selections, 1);
        assert.equal(await evaluate('document.getElementById("select-server")'), null);
        // A single discovered server connects automatically, without another button.
        state = {...state, selected_server:null, registration:{state:'idle'}, tuners:[], scanfiles:[]};
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
        await wait('typeof connected!=="undefined" && connected && !document.getElementById("scan").disabled');
        assert.equal(selections, 2);
        // Multiple servers require just changing the dropdown, not confirming it.
        const second = {...device, udn:'uuid:second-fixture', name:'Second fixture'};
        state = {...state, selected_server:null, registration:{state:'idle'}, tuners:[], scanfiles:[],
            discovery:{...state.discovery,servers:[device,second],state:'complete'}};
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
        await wait('typeof connected!=="undefined" && connected && document.querySelectorAll("#server option").length===3 && !document.getElementById("server").disabled');
        assert.equal(selections, 2);
        await evaluate('document.getElementById("server").value="uuid:second-fixture"; document.getElementById("server").dispatchEvent(new Event("change"))');
        await wait('!document.getElementById("scan").disabled');
        assert.equal(selections, 3); assert.equal(state.selected_server.udn, second.udn);
        state.registration = {state:'error',error:'Fixture connection interrupted'};
        await wait('!document.getElementById("retry-server").hidden');
        await evaluate('document.getElementById("retry-server").click()');
        await wait('!document.getElementById("scan").disabled');
        assert.equal(selections, 4);
        assert.equal(await evaluate('document.getElementById("confirm") || document.getElementById("scanfile")'), null);
        for (const width of [1280,390]) {
            await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
            const sizing = await evaluate('({font:getComputedStyle(document.getElementById("tuner")).fontSize,height:document.getElementById("tuner").getBoundingClientRect().height,gap:document.getElementById("tuner").getBoundingClientRect().top-document.querySelector("label[for=tuner]").getBoundingClientRect().bottom})');
            assert.equal(sizing.font, '16px'); assert.ok(sizing.height >= 40 && sizing.height <= 46); assert.ok(sizing.gap >= 8);
        }
        await evaluate('document.getElementById("tuner").value="3".repeat(32)');
        await new Promise(resolve=>setTimeout(resolve,150));
        assert.equal(await evaluate('document.getElementById("tuner").value'), '3'.repeat(32));
        // Channel restore prepares the tuner but never starts a search.
        const chooseChannelBackup = async confirm => {
            await wait('!document.getElementById("setup-channels-restore").disabled');
            await evaluate(`window.confirm=()=>${confirm}; var transfer=new DataTransfer(); transfer.items.add(new File([new Uint8Array(40000)], 'channels.tar')); document.getElementById("setup-channels-file").files=transfer.files; document.getElementById("setup-channels-file").dispatchEvent(new Event("change"));`);
        };
        await chooseChannelBackup(false);
        await wait('!pending && document.getElementById("setup-channels-message").textContent.includes("Restore cancelled")');
        assert.equal(channelPrepares, 1); assert.equal(channelUploads, 1); assert.equal(channelRestores, 0);
        assert.equal(state.scan.state, 'idle');
        assert.equal(await evaluate('document.getElementById("setup-channels-file").value'), '');
        channelFault = 'validation';
        await chooseChannelBackup(true);
        await wait('!pending && document.getElementById("setup-channels-message").textContent.includes("Wrong satellite fixture")');
        assert.equal(channelRestores, 0);
        channelFault = 'upload_timeout';
        await evaluate('window.nativeSignalTimeout=AbortSignal.timeout; AbortSignal.timeout=ms=>window.nativeSignalTimeout(ms===120000?100:ms)');
        await chooseChannelBackup(true);
        await wait('!pending && document.getElementById("setup-channels-message").textContent.length>0');
        assert.equal(channelRestores, 0);
        await evaluate('AbortSignal.timeout=window.nativeSignalTimeout');
        channelFault = 'restore_timeout';
        await evaluate('window.nativeNow=Date.now; let elapsed=0; Date.now=()=>window.nativeNow()+(elapsed+=500000)');
        await chooseChannelBackup(true);
        await wait('!pending && document.getElementById("setup-channels-message").textContent.includes("taking longer than expected")');
        assert.equal(channelRestores, 1);
        await evaluate('Date.now=window.nativeNow');
        channelFault = '';
        await evaluate('document.getElementById("scan").click()');
        await wait('document.querySelectorAll("#channels input").length===1 && !document.getElementById("cancel").disabled');
        assert.match(await evaluate('document.getElementById("status").textContent'), /1 \/ 2 transponders scanned — 1 supported channels found/);
        assert.equal(await evaluate('document.getElementById("recover").hidden'), true);
        assert.deepEqual(await evaluate('({value:document.getElementById("scan-progress").value,max:document.getElementById("scan-progress").max,hidden:document.getElementById("scan-progress").hidden})'), {value:1,max:2,hidden:false});
        assert.equal(await evaluate('document.getElementById("scan-detail").textContent'), "Current transponder: 10773H.");
        await evaluate('document.querySelector("#channels input").click()');
        offline = true;
        await wait('document.getElementById("status").textContent.includes("Connection unavailable")');
        assert.equal(await evaluate('document.getElementById("confirm-favourites").disabled'), true);
        offline = false;
        await wait('!document.getElementById("cancel").disabled');
        await evaluate('document.getElementById("cancel").click()');
        await wait('!document.getElementById("confirm-favourites").disabled');
        assert.equal(await evaluate('document.querySelector("#channels input").checked'), true);
        assert.equal(await evaluate('document.getElementById("tuner").disabled'), false);
        assert.equal(await evaluate('document.getElementById("server-field").hidden'), true);
        await evaluate('document.getElementById("tuner").value="2".repeat(32); document.getElementById("tuner").dispatchEvent(new Event("change"))');
        await new Promise(resolve=>setTimeout(resolve,150));
        assert.equal(await evaluate('document.getElementById("tuner").value'), '2'.repeat(32));
        assert.equal(await evaluate('document.getElementById("confirm-favourites").disabled'), true);
        expectedFrontend = '2'.repeat(32);
        await evaluate('document.getElementById("scan").click()');
        await wait('setupState.scan.state==="running" && !pending');
        assert.equal(await evaluate('document.getElementById("tuner").disabled'), true);
        await evaluate('document.getElementById("cancel").click()');
        await wait('!document.getElementById("confirm-favourites").disabled');
        state.recovery_pending = true;
        await wait('!document.getElementById("recover").disabled');
        assert.equal(await evaluate('document.getElementById("confirm-favourites").disabled'), true);
        await evaluate('document.getElementById("recover").click()');
        await wait('!document.getElementById("confirm-favourites").disabled');
        state.candidates.push({service_uuid:'4'.repeat(32),name:'Starter HD'});
        state.starter=['4'.repeat(32)];
        await wait('document.querySelectorAll("#channels input:checked").length===2');
        assert.equal(await evaluate('document.querySelectorAll("#channels button").length'), 0);
        assert.equal(await evaluate('document.getElementById("setup-sorting").hidden'), true);
        await evaluate('document.getElementById("confirm-favourites").click()');
        await wait('!pending && !document.getElementById("setup-sorting").hidden');
        assert.equal(await evaluate('document.getElementById("setup-selection").hidden'), true);
        assert.deepEqual(await evaluate('[...document.querySelectorAll("#setup-favourites li")].map(n=>n.dataset.service)'), ['4'.repeat(32),'3'.repeat(32)]);
        await evaluate('document.getElementById("setup-favourites").scrollIntoView({block:"center"})');
        const positions = await evaluate('[...document.querySelectorAll("#setup-favourites .drag-handle")].map(n=>{const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})');
        await send('Input.dispatchMouseEvent',{type:'mousePressed',...positions[0],button:'left',clickCount:1});
        await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:positions[0].x,y:positions[1].y+20,button:'left',buttons:1});
        await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:positions[0].x,y:positions[1].y+20,button:'left',clickCount:1});
        assert.deepEqual(await evaluate('lineupOrder'), ['3'.repeat(32),'4'.repeat(32)]);
        // Polling must neither add newly discovered channels nor reset the confirmed order.
        state.candidates.push({service_uuid:'5'.repeat(32),name:'Another TV'});
        state.starter.push('5'.repeat(32));
        await new Promise(resolve=>setTimeout(resolve,150));
        assert.deepEqual(await evaluate('lineupOrder'), ['3'.repeat(32),'4'.repeat(32)]);
        assert.equal(await evaluate('document.querySelectorAll("#setup-favourites li").length'), 2);
        // Touch uses the same stationary-list capture path and cancellation restores order.
        const touch = await evaluate('[...document.querySelectorAll("#setup-favourites .drag-handle")].map(n=>{const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})');
        await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[touch[1]]});
        await send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:touch[1].x,y:touch[0].y-20}]});
        assert.deepEqual(await evaluate('[...document.querySelectorAll("#setup-favourites li")].map(n=>n.dataset.service)'), ['4'.repeat(32),'3'.repeat(32)]);
        await send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]});
        assert.deepEqual(await evaluate('lineupOrder'), ['3'.repeat(32),'4'.repeat(32)]);
        await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[touch[1]]});
        await send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:touch[1].x,y:touch[0].y-20}]});
        await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
        assert.deepEqual(await evaluate('lineupOrder'), ['4'.repeat(32),'3'.repeat(32)]);
        await evaluate('document.getElementById("change-selection").click()');
        await wait('!pending && !document.getElementById("setup-selection").hidden');
        assert.equal(await evaluate('document.querySelectorAll("#channels input").length'), 3);
        assert.equal(await evaluate('document.querySelectorAll("#channels input:checked").length'), 2);
        await evaluate('[...document.querySelectorAll("#channels input")].find(i=>i.value==="4".repeat(32)).click()');
        await evaluate('document.getElementById("confirm-favourites").click()');
        await wait('!pending && !document.getElementById("finish").disabled');
        assert.deepEqual(await evaluate('lineupOrder'), ['3'.repeat(32)]);
        await evaluate('document.getElementById("change-selection").click()');
        await wait('!pending && !document.getElementById("setup-selection").hidden');
        await evaluate('[...document.querySelectorAll("#channels input")].find(i=>i.value==="4".repeat(32)).click()');
        await evaluate('document.getElementById("confirm-favourites").click()');
        await wait('!pending && !document.getElementById("finish").disabled');
        await evaluate('document.querySelectorAll("#setup-favourites button")[1].dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowUp",bubbles:true}))');
        assert.deepEqual(await evaluate('lineupOrder'), ['4'.repeat(32),'3'.repeat(32)]);
        await evaluate('document.getElementById("finish").click()');
        await wait('document.getElementById("status").textContent.includes("Starting TV")');
        offline = true; await new Promise(resolve=>setTimeout(resolve,100)); offline = false; configured = true;
        await wait('document.body?.textContent.includes("Management ready")');
        assert.equal(await evaluate('location.hash'), '#status');
        // A separate fresh setup restores through a dropped status request and reloads management.
        configured = false; expectedFrontend = '3'.repeat(32); channelFault = 'reconnect';
        state = {...state,scan:{state:'idle'},prepared:false,completion_pending:false,
            recovery_pending:false,selection_locked:false,frontend_uuid:undefined,candidates:[],starter:[]};
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
        await wait('typeof connected!=="undefined" && connected && !document.getElementById("setup-channels-restore").disabled');
        await evaluate('document.getElementById("tuner").value="3".repeat(32)');
        await chooseChannelBackup(true);
        await wait('document.body?.textContent.includes("Management ready")');
        assert.equal(channelRestores, 2); assert.equal(channelStatuses, 2);
        assert.equal(state.scan.state, 'idle');
        assert.equal(await evaluate('location.hash'), '#status');
    } finally {
        if (socket) socket.close();
        chrome.kill('SIGTERM');
        await new Promise(resolve => { if (chrome.exitCode !== null) resolve(); else chrome.once('exit',resolve); });
        server.closeAllConnections(); await new Promise(resolve=>server.close(resolve));
    }
});

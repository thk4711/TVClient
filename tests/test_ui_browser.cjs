// Real pointer input via Chrome DevTools; only a local fake backend is used.
// Requires Node 22+, CHROME_BINARY and a fresh CHROME_USER_DATA_DIR.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {test} = require('node:test');

test('real browser keeps dragging after DOM moves, with mouse and touch', {timeout: 30000}, async () => {
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR, 'Set Chrome binary and an isolated profile directory');
    let saved, stallSave = false, loseSaveResponse = false, lastSaveId = null, revision = 0;
    let preview = {state:'idle',enabled:true};
    let streamCount = 0;
    const server = http.createServer(async (request, response) => {
        response.setHeader('Cache-Control', 'no-store');
        if (['/', '/request.js', '/ui.js', '/app.js', '/app.css', '/hls.min.js'].includes(request.url)) {
            const path = request.url === '/' ? 'satip_controller/static/index.html' : request.url === '/ui.js' && process.env.UI_SOURCE ? process.env.UI_SOURCE : 'satip_controller/static' + request.url;
            response.setHeader('Content-Type', request.url === '/' ? 'text/html' : request.url.endsWith('.css') ? 'text/css' : 'text/javascript');
            response.end(fs.readFileSync(path));
        } else {
            response.setHeader('Content-Type', 'application/json');
            if (request.url === '/api/status') response.end(JSON.stringify({scan: {state: 'idle'}, player: {playing:'a',channel_name:'Das Erste HD'}, catalogue_revision: revision, last_favourites_save_id: lastSaveId, preview}));
            else if (request.url === '/api/telemetry') response.end(JSON.stringify({version:1,
                system:{state:'ok',sampled_at:Date.now()/1000,hostname:'living-room',model:'Raspberry Pi 4 Model B',temperature_c:48.2,cpu_percent:14,uptime_seconds:125800,memory_available:1500000000,memory_total:2000000000,storage_free:180000000,storage_total:268435456,os:'Linux',build:{}},
                performance:{state:'ok',sampled_at:Date.now()/1000,cpu_percent:14,cores:[{name:'cpu0',percent:20,frequency_mhz:1200},{name:'cpu1',percent:10,frequency_mhz:1200}],load_averages:[0.5,0.4,0.3],memory_used:500000000,memory_available:1500000000,memory_total:2000000000,memory_cached:200000000,swap_used:0,swap_total:0,disks:[{name:'mmcblk0',read_bytes_per_second:1048576,write_bytes_per_second:0}],processes:[{pid:42,name:'mpv',cpu_percent:50,memory_bytes:80000000}]},
                network:{state:'ok',sampled_at:Date.now()/1000,interfaces:[{name:'eth0',link:'up',default_route:true,addresses:['192.0.2.10'],speed_mbps:1000,rx_bps:8500000,tx_bps:100000,rx_errors:0,tx_errors:0}]},
                reception:{state:'ok',sampled_at:Date.now()/1000,streams:[{input:'SAT-IP tuner · DVB-S #1',transponder:'Astra 19.2°E · 11494 H',signal:{value:82,unit:'%'},quality:{value:91,unit:'%'},bitrate_bps:8500000,subscriptions:1,errors:{cc:0,te:0,unc:0},error_changes:{cc:0,te:0,unc:0}}]}}));
            else if (request.url === '/api/preview') {
                let body = ''; for await (const chunk of request) body += chunk;
                preview = {state:'starting',enabled:true,channel_name:JSON.parse(body).service.toUpperCase()};
                const session = (++streamCount === 1 ? 'a' : 'b').repeat(32);
                response.end(JSON.stringify({session,url:'/api/preview/'+session+'/index.m3u8'}));
            } else if (request.url === '/api/preview/heartbeat') response.end(JSON.stringify(preview));
            else if (request.url === '/api/preview/stop') { preview = {state:'stopped',enabled:true}; response.end(JSON.stringify(preview)); }
            else if (request.url === '/api/channels') response.end(JSON.stringify({channels: ['a','b','c','sat1'].map(id => ({service_uuid: id, name: id === 'sat1' ? 'SAT.1' : id.toUpperCase()})), favourites: saved || ['a','b','c'], previewable: saved || ['a','b','c']}));
            else if (request.url === '/api/favourites') {
                let body = ''; for await (const chunk of request) body += chunk;
                if (stallSave) return; // Deliberately leave the browser request pending.
                const payload = JSON.parse(body);
                saved = payload.services; lastSaveId = payload.save_id; revision++;
                if (loseSaveResponse) return; // Commit succeeded, only its response stalls.
                response.end(JSON.stringify({saved:saved.length,save_id:lastSaveId}));
            } else response.end('{}');
        }
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
        await send('Emulation.setDeviceMetricsOverride', {width:1200,height:1000,deviceScaleFactor:1,mobile:false});
        await send('Page.navigate', {url: 'http://127.0.0.1:' + server.address().port + '/#channels'});
        for (let attempt = 0; attempt < 100; attempt++) {
            if (await evaluate("document.querySelectorAll('#favourites li').length === 3 && connected && previewState.enabled")) break;
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        const coordinates = async () => evaluate("document.querySelector('#favourites').scrollIntoView(); Array.from(document.querySelectorAll('#favourites .drag-handle'), n => { const r=n.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })");
        const order = () => evaluate("Array.from(document.querySelectorAll('#favourites li'), n => n.dataset.service)");
        const settle = () => evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
        await evaluate("document.querySelector('#favourites').scrollIntoView()");
        await settle();
        const points = await coordinates(); assert.equal(points.length, 3);
        await evaluate("window.pointerLog=[]; for(const type of ['pointerdown','pointermove','pointerup','pointercancel','lostpointercapture']) document.addEventListener(type,e=>pointerLog.push({type,target:e.target.tagName,id:e.pointerId,x:e.clientX,y:e.clientY}),true)");
        await send('Input.dispatchMouseEvent', {type: 'mousePressed', ...points[0], button: 'left', buttons: 1, clickCount: 1});
        // Move in steps like a real drag: a single large jump can race pointer capture on slow runners.
        await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: points[0].x, y: points[0].y + 6, buttons: 1});
        await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: points[0].x, y: points[2].y + 10, buttons: 1});
        await settle();
        assert.equal(await evaluate("document.querySelectorAll('#favourites li.dragging').length"), 1, 'Drag was cancelled: ' + JSON.stringify(await evaluate('({points:' + JSON.stringify(points) + ',height:innerHeight,events:pointerLog,drag:!!drag})')));
        await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: points[0].x, y: points[0].y - 10, buttons: 1});
        await send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: points[0].x, y: points[2].y + 10, buttons: 1});
        await send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: points[0].x, y: points[2].y + 10, button: 'left', buttons: 0, clickCount: 1});
        await settle();
        assert.deepEqual(await order(), ['b','c','a']);
        await evaluate('save()'); assert.deepEqual(saved, ['b','c','a']);
        const touch = await coordinates();
        await send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{...touch[2], id: 1}]});
        await send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: touch[2].x, y: touch[0].y - 10, id: 1}]});
        await settle();
        assert.equal(await evaluate("document.querySelectorAll('#favourites li.dragging').length"), 1);
        await send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
        await settle();
        assert.deepEqual(await order(), ['a','b','c']);
        assert.equal(await evaluate("document.querySelectorAll('#favourites .remove-channel svg').length"), 3);
        await evaluate("document.querySelector('[aria-label=\"Remove B from favourites\"]').click()");
        assert.deepEqual(await order(), ['a','c']);
        // Reproduce Add SAT.1 -> drag once -> Save, without a page reload.
        await evaluate("Array.from(document.querySelectorAll('#available li')).find(n=>n.textContent.includes('SAT.1')).querySelector('button:last-child').click()");
        assert.deepEqual(await order(), ['a','c','sat1']);
        const added = await coordinates();
        await send('Input.dispatchMouseEvent', {type:'mousePressed', ...added[2], button:'left',buttons:1,clickCount:1});
        await send('Input.dispatchMouseEvent', {type:'mouseMoved', x:added[2].x,y:added[0].y-10,buttons:1});
        await send('Input.dispatchMouseEvent', {type:'mouseReleased', x:added[2].x,y:added[0].y-10,button:'left',buttons:0,clickCount:1});
        await settle();
        assert.deepEqual(await order(), ['sat1','a','c']);
        await evaluate('save()');
        assert.deepEqual(saved, ['sat1','a','c']);
        assert.deepEqual(await order(), saved);
        assert.equal(await evaluate("Array.from(document.querySelectorAll('#favourites .preview-channel')).every(n=>!n.disabled)"), true, 'Preview buttons must re-enable after Save');
        assert.equal(await evaluate("document.getElementById('save-favourites').disabled"), false);
        await evaluate('poll()');
        assert.deepEqual(await order(), saved);
        await send('Page.reload');
        for (let attempt=0; attempt<100; attempt++) {
            if (await evaluate("document.querySelector('#favourites li')?.dataset.service === 'sat1'")) break;
            await new Promise(resolve=>setTimeout(resolve,50));
        }
        assert.deepEqual(await order(), saved, 'One drag and one save persist across reload');
        await evaluate('poll()');
        loseSaveResponse = true;
        const confirmationStarted = Date.now();
        await evaluate('save()'); // Normal periodic polling must confirm the commit.
        assert.ok(Date.now()-confirmationStarted < 10000, 'Lost response must be confirmed before the timeout');
        assert.equal(await evaluate("document.getElementById('unsaved').hidden"), true);
        assert.equal(await evaluate("document.getElementById('save-favourites').disabled"), false);
        assert.equal(await evaluate("document.getElementById('save-status').textContent.startsWith('Favourites saved')"), true);
        assert.equal(await evaluate("Array.from(document.querySelectorAll('#favourites .preview-channel')).every(n=>!n.disabled)"), true);
        // Navigation retains edits and the stream owner; physical takeover clears it.
        stallSave = false;
        await evaluate("edit(() => favourites.reverse()); navigateReceiver('status')");
        for (let attempt=0; attempt<100; attempt++) {
            if (await evaluate("document.getElementById('temperature-value').textContent.includes('48')")) break;
            await new Promise(resolve=>setTimeout(resolve,50));
        }
        assert.equal(await evaluate('dirty'), true);
        assert.equal(await evaluate("document.getElementById('page-status').hidden"), false);
        assert.equal(await evaluate("document.getElementById('performance-dialog').open"), false);
        assert.equal(await evaluate("document.querySelectorAll('#performance-content svg').length"), 0);
        await evaluate("document.getElementById('open-performance').click()"); await settle();
        assert.equal(await evaluate("document.getElementById('performance-dialog').open"), true);
        assert.equal(await evaluate("document.activeElement.id"), 'close-performance');
        assert.equal(await evaluate("document.querySelectorAll('#performance-content svg').length"), 9);
        assert.match(await evaluate("document.getElementById('performance-processes').textContent"), /mpv/);
        assert.match(await evaluate("document.getElementById('performance-note').textContent"), /0.5/);
        assert.match(await evaluate("document.getElementById('network-content').textContent"), /192.0.2.10/);
        await evaluate("document.getElementById('close-performance').click()"); await settle();
        assert.equal(await evaluate("document.getElementById('performance-dialog').open"), false);
        assert.equal(await evaluate("document.activeElement.id"), 'open-performance');
        assert.equal(await evaluate("document.querySelectorAll('#performance-content svg').length"), 0);
        await evaluate("document.getElementById('open-performance').click()"); await settle();
        assert.equal(await evaluate("document.querySelectorAll('#performance-content svg').length"), 9);
        await send('Input.dispatchKeyEvent', {type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
        await send('Input.dispatchKeyEvent', {type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
        await settle();
        assert.equal(await evaluate("document.getElementById('performance-dialog').open"), false);
        assert.equal(await evaluate("document.activeElement.id"), 'open-performance');
        if (process.env.UI_SCREENSHOT_DIR) {
            fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, {recursive:true});
            fs.writeFileSync(process.env.UI_SCREENSHOT_DIR + '/status-desktop.png', Buffer.from((await send('Page.captureScreenshot', {captureBeyondViewport:true})).data, 'base64'));
        }
        assert.equal(await evaluate("getComputedStyle(document.getElementById('save-favourites')).minHeight"), '36px');
        assert.equal(await evaluate("getComputedStyle(document.getElementById('save-favourites')).paddingTop"), '6px');
        await evaluate("document.getElementById('open-performance').click(); navigateReceiver('live')"); await settle();
        assert.equal(await evaluate("document.getElementById('performance-dialog').open"), false);
        assert.equal(await evaluate("getComputedStyle(document.getElementById('watch-channel-name')).display"), 'none');
        assert.equal(await evaluate("getComputedStyle(document.getElementById('preview-status')).display"), 'none');
        assert.equal(await evaluate("document.querySelector('#video-placeholder').textContent.trim()"), 'Choose a saved channel to start watching.');
        const checkStopPosition = () => evaluate("(() => { const stage=document.querySelector('.video-stage').getBoundingClientRect(), stop=document.getElementById('stop-preview').getBoundingClientRect(); return stop.top-stage.bottom; })()");
        assert.equal(await checkStopPosition(), 12, 'Stop belongs directly below the video');
        const checkProgrammeColumns = async () => {
            const cells = await evaluate("(() => { const original=previewState, now=Date.now()/1000; previewState={state:'starting',enabled:true}; receiverStatus({preview:{programmes:[{start:now-600,stop:now+600,title:'Current programme with a long descriptive title'},{start:now+600,stop:now+1200,title:'NextProgrammeWithAVeryLongUnbrokenTitleThatMustWrapWithoutOverflow'}]}}); previewState=original; return Array.from(document.querySelectorAll('#watch-programmes .programme'), n => { const r=n.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,label:n.querySelector('small').textContent,overflow:n.scrollWidth>n.clientWidth}; }); })()");
            assert.equal(cells.length, 2);
            assert.match(cells[0].label, /^NOW/); assert.match(cells[1].label, /^NEXT/);
            assert.equal(cells[0].y, cells[1].y, 'Now and Next share a horizontal row');
            assert.ok(cells[1].x > cells[0].x);
            assert.ok(Math.abs(cells[0].width-cells[1].width) <= 1, 'Equal-width programme columns');
            assert.equal(cells.some(cell => cell.overflow), false, 'Long titles wrap inside their cells');
        };
        await checkProgrammeColumns();
        await evaluate("showPreview({state:'ready',enabled:true,channel_name:'VOX'})");
        assert.equal(await evaluate("getComputedStyle(document.getElementById('preview-status')).display"), 'none');
        await evaluate("showPreview({state:'idle',enabled:true}); document.querySelector('#watch-channels button').onclick()");
        assert.equal(await evaluate('previewSession'), 'a'.repeat(32));
        await evaluate("document.querySelectorAll('#watch-channels button')[1].onclick()");
        assert.equal(await evaluate('previewSession'), 'b'.repeat(32));
        await evaluate("navigateReceiver('status')");
        assert.equal(await evaluate('previewSession'), 'b'.repeat(32));
        assert.equal(await evaluate("document.getElementById('header-stop').disabled"), false);
        preview = {state:'stopped',enabled:true,reason:'Streaming stopped because the receiver was used locally.'};
        await evaluate('poll()');
        assert.equal(await evaluate('previewSession'), null);
        assert.equal(await evaluate("document.getElementById('preview-video').hidden"), true);
        assert.match(await evaluate("document.getElementById('preview-status').textContent"), /used locally/);
        await send('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
        await send('Emulation.setTouchEmulationEnabled', {enabled:true});
        assert.equal(await evaluate("getComputedStyle(document.getElementById('stop-preview')).minHeight"), '44px');
        for (const page of ['status','live','channels','scan']) {
            await evaluate("navigateReceiver('" + page + "')"); await settle();
            if (page === 'live') { assert.equal(await checkStopPosition(), 12); await checkProgrammeColumns(); }
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'Mobile overflow on ' + page);
            if (page === 'status') {
                await evaluate("document.getElementById('open-performance').click()"); await settle();
                assert.equal(await evaluate("document.getElementById('performance-dialog').open"), true);
                assert.equal(await evaluate("(() => { const d=document.getElementById('performance-dialog'), r=d.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight && d.scrollWidth<=d.clientWidth && d.scrollHeight>d.clientHeight; })()"), true, 'Mobile monitor fits and scrolls vertically');
                if (process.env.UI_SCREENSHOT_DIR) fs.writeFileSync(process.env.UI_SCREENSHOT_DIR + '/performance-mobile.png', Buffer.from((await send('Page.captureScreenshot')).data, 'base64'));
                await evaluate("document.getElementById('close-performance').click()"); await settle();
            }
            if (process.env.UI_SCREENSHOT_DIR) fs.writeFileSync(process.env.UI_SCREENSHOT_DIR + '/' + page + '-mobile.png', Buffer.from((await send('Page.captureScreenshot', {captureBeyondViewport:true})).data, 'base64'));
        }
        loseSaveResponse = false;
        // Accelerate only the 30-second API deadline; use a real pending fetch.
        await evaluate('window.originalTimeout = window.setTimeout; window.setTimeout = (callback, ms) => originalTimeout(callback, ms === 30000 ? 100 : ms)');
        stallSave = true;
        await evaluate('save()');
        assert.equal(await evaluate('requesting'), false);
        assert.equal(await evaluate("document.getElementById('save-favourites').disabled"), false);
        assert.equal(await evaluate("document.getElementById('save-status').textContent.includes('timed out')"), true);
        assert.equal(await evaluate("Array.from(document.querySelectorAll('#favourites .preview-channel')).every(n=>!n.disabled)"), true);
    } finally {
        if (socket) socket.close(); chrome.kill(); server.closeAllConnections(); server.close();
    }
});

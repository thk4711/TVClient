// Isolated real-Chrome contract for source discovery and bouquet import controls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {test} = require('node:test');

test('source UI dispatches discovery, selects bouquets safely and blocks imports while running', {timeout:30000}, async () => {
    assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR);
    let imports = [], requests = [], fail = false;
    const discovery = {state:'idle',servers:[],interfaces:['eth0'],warnings:[]};
    const state = {active:'satip',available:['satip'],satip:{name:'Fixture Twin',address:'192.168.1.3',connected:true,configured_frontend:'b'.repeat(32),tuners:[{uuid:'a'.repeat(32),number:1,configured:false},{uuid:'b'.repeat(32),number:2,configured:true}],receiving:['Tuner #2 (192.168.1.3)'],tuning:[]},discovery:{satip:{...discovery},enigma2:{...discovery}},import:{state:'idle'}};
    const server = http.createServer(async (req,res) => {
        res.setHeader('Cache-Control','no-store');
        if (req.url.startsWith('/api/setup/')) {
            res.setHeader('Content-Type','application/json');
            let text=''; for await(const chunk of req) text+=chunk;
            const body=text?JSON.parse(text):null;
            requests.push({path:req.url,body});
            const complete=state.import.state==='complete';
            const replies={
                status:{state:'unconfigured',source:complete?'enigma2':null,prepared:complete,
                    selection_locked:complete,scan:state.import,discovery,tuners:[],scanfiles:[],registration:{state:'idle'},
                    candidates:complete?[{service_uuid:'a'.repeat(32),name:'Fixture TV'}]:[],starter:['a'.repeat(32)]},
                inputs:{devices:[],selected:''},
                preferences:{hostname:'satip',timezone:'Etc/UTC',timezones:['Etc/UTC']},
                finish:{message:'Starting management fixture'},
            };
            res.end(JSON.stringify(replies[req.url.slice('/api/setup/'.length)]||{}));return;
        }
        if (req.url.startsWith('/api/sources')) {
            res.setHeader('Content-Type','application/json');
            let text=''; for await (const chunk of req) text+=chunk;
            const body=text?JSON.parse(text):null;
            requests.push({path:req.url,body});
            if (body) assert.equal(req.headers['x-satip-action'],'1');
            if (req.url.endsWith('/discover')) state.discovery[body.source]={...discovery,state:'complete',servers:[{address:'192.168.1.2',model:'Fixture receiver'}]};
            if (req.url.endsWith('/bouquets')) {
                if (fail) { res.writeHead(400); res.end(JSON.stringify({detail:'OpenWebif unavailable fixture'})); return; }
                res.end(JSON.stringify({receiver:{model:'Fixture receiver'},bouquets:[{name:'Free HD'},{name:'Free SD'},{name:'<img src=x onerror=alert(1)>'}]})); return;
            }
            if (req.url.endsWith('/import')) { imports.push(body); state.import={state:'running',completed:1,total:3,found:1}; }
            if (req.url.endsWith('/cancel')) state.import={state:'cancelled',error:'Import stopped; previous lineup retained'};
            if (req.url.endsWith('/recover')) state.import={state:'idle'};
            res.end(JSON.stringify(req.url==='/api/sources'?state:{state:'accepted'})); return;
        }
        if (req.url==='/' || req.url==='/setup') {
            const setup=req.url==='/setup';
            let html=fs.readFileSync('satip_controller/static/'+(setup?'setup.html':'index.html'),'utf8');
            html=html.replace(/<script[^>]*>[\s\S]*?<\/script>/g,'');
            html=html.replace('</body>','<script src="/request.js"></script>'+(setup?'<script src="/setup.js"></script>':'')+'<script src="/sources.js"></script></body>');
            res.end(html); return;
        }
        const file=req.url.slice(1);
        if (!['sources.js','setup.js','request.js','app.css'].includes(file)) { res.writeHead(404); res.end(); return; }
        res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css');
        res.end(fs.readFileSync('satip_controller/static/'+file));
    });
    server.listen(0,'127.0.0.1'); await once(server,'listening');
    const chrome=spawn(process.env.CHROME_BINARY,['--headless','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+process.env.CHROME_USER_DATA_DIR,'about:blank']);
    let socket;
    try {
        const debuggerUrl=await new Promise((resolve,reject)=>{let text='';chrome.stderr.on('data',chunk=>{text+=chunk;const match=text.match(/DevTools listening on (.+)/);if(match)resolve(match[1].trim());});chrome.on('error',reject);});
        const tabs=await(await fetch(new URL('/json/list',debuggerUrl.replace('ws:','http:')))).json();
        socket=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await once(socket,'open');
        let seq=0;const pending=new Map();
        socket.addEventListener('message',event=>{const data=JSON.parse(event.data),task=pending.get(data.id);if(task){pending.delete(data.id);data.error?task.reject(Error(data.error.message)):task.resolve(data.result);}});
        const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
        const evaluate=async expression=>{const data=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});assert.ok(!data.exceptionDetails,JSON.stringify(data.exceptionDetails));return data.result.value;};
        const wait=async expression=>{for(let n=0;n<150;n++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,20));}assert.fail('Timed out: '+expression+' · '+await evaluate('document.body.innerText'));};
        await send('Page.enable');
        await send('Page.addScriptToEvaluateOnNewDocument',{source:'window.confirm=()=>true; const nativeTimeout=window.setTimeout;window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,ms===2000?50:ms,...args);'});
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/'});
        await wait('document.getElementById("source-current-device")?.textContent.includes("Fixture Twin") && !document.getElementById("source-kind").disabled');
        await evaluate('document.getElementById("page-settings").hidden=false;document.getElementById("device-source-card").hidden=false');
        assert.match(await evaluate('document.getElementById("source-current-device").textContent'), /Fixture Twin.*192.168.1.3/);
        assert.match(await evaluate('document.getElementById("source-current-tuner").textContent'), /#2/);
        assert.equal(await evaluate('document.getElementById("source-enigma-help").hidden'), true);
        await evaluate('document.getElementById("source-tuner").value="' + 'a'.repeat(32) + '"');
        await new Promise(r=>setTimeout(r,150));
        assert.equal(await evaluate('document.getElementById("source-tuner").value'), 'a'.repeat(32));
        state.satip.receiving=[];state.satip.tuning=['Tuner #2'];
        await wait('document.getElementById("source-receiving").textContent.includes("no stream data")');
        state.satip.connected=false;state.satip.tuning=[];
        await wait('document.getElementById("source-receiving").textContent.includes("unavailable")');
        state.satip.connected=true;state.satip.receiving=['Tuner #2 (192.168.1.3)'];
        assert.equal(await evaluate('document.getElementById("source-discovery-controls").hidden'), true);
        assert.equal(await evaluate('document.getElementById("source-server-results").hidden'), true);
        assert.equal(await evaluate('document.getElementById("source-satip-device")'), null);
        assert.equal(requests.some(r=>r.path.endsWith('/discover')), false);
        await evaluate('document.getElementById("source-kind").value="enigma2";document.getElementById("source-kind").dispatchEvent(new Event("change"));document.getElementById("source-discover").click()');
        await wait('document.querySelectorAll("#source-server option").length===2 && !document.getElementById("source-server").disabled');
        assert.equal(requests.filter(r=>r.path.endsWith('/discover')).at(-1).body.source,'enigma2');
        assert.equal(await evaluate('document.getElementById("source-discovery-controls").hidden'), false);
        assert.equal(await evaluate('document.getElementById("source-server-results").hidden'), false);
        await evaluate('document.getElementById("source-server").value="192.168.1.2";document.getElementById("source-server").dispatchEvent(new Event("change"))');
        await wait('document.querySelectorAll("#source-bouquets input").length===3 && !document.getElementById("source-load-bouquets").disabled');
        assert.equal(await evaluate('document.querySelectorAll("#source-bouquets img").length'),0);
        assert.equal(await evaluate('document.getElementById("source-import").disabled'),true);
        await evaluate('document.getElementById("page-channels").hidden=false;document.querySelectorAll("#source-bouquets input")[0].click();document.querySelectorAll("#source-bouquets input")[1].click();document.getElementById("source-import").click()');
        await wait('!document.getElementById("source-cancel").hidden');
        assert.deepEqual(imports[0],{address:'192.168.1.2',web_port:80,stream_port:8001,bouquets:['Free HD','Free SD'],confirm_pause:true});
        assert.equal(await evaluate('document.getElementById("source-kind").disabled'),true);
        await evaluate('document.getElementById("source-cancel").click()');
        await wait('document.getElementById("source-cancel").hidden && !document.getElementById("source-import").disabled');
        assert.equal(await evaluate('document.querySelectorAll("#source-bouquets input:checked").length'),2);
        fail=true;await evaluate('document.getElementById("source-load-bouquets").click()');
        await wait('document.getElementById("source-message").textContent.includes("OpenWebif unavailable fixture")');
        for(const width of [1280,390]){
            await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
            assert.equal(await evaluate('["source-kind","source-address","source-bouquet-panel"].every(id=>document.getElementById(id).getBoundingClientRect().right<=innerWidth)'),true);
        }
        // The same component drives first setup without SAT-IP tuners.
        fail=false;state.import={state:'idle'};
        await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/setup'});
        await wait('window.sourceSetupKind === "satip" && connected && !document.getElementById("source-kind").disabled');
        await evaluate('document.getElementById("source-kind").value="enigma2";document.getElementById("source-kind").dispatchEvent(new Event("change"));document.getElementById("source-address").value="192.168.1.2";document.getElementById("source-load-bouquets").click()');
        await wait('document.querySelectorAll("#source-bouquets input").length===3 && !document.getElementById("source-load-bouquets").disabled');
        assert.equal(await evaluate('document.getElementById("setup-satip").hidden'),true);
        await evaluate('document.querySelector("#source-bouquets input").click();document.getElementById("source-import").click()');
        await wait('!document.getElementById("source-cancel").hidden');
        state.import={state:'complete',found:1,message:'Imported fixture'};
        await wait('!document.getElementById("confirm-favourites").disabled');
        assert.equal(await evaluate('document.querySelectorAll("#tuner option").length'),0);
        await evaluate('document.getElementById("confirm-favourites").click()');
        await wait('!document.getElementById("finish").disabled');
        await evaluate('document.getElementById("finish").click()');
        await wait('setupState.completion_pending===true');
        assert.deepEqual(requests.find(r=>r.path==='/api/setup/finish').body,{services:['a'.repeat(32)]});
    } finally {
        if(socket)socket.close();chrome.kill();await once(chrome,'exit').catch(()=>{});server.closeAllConnections();await new Promise(r=>server.close(r));
    }
});

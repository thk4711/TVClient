// Public manual screenshots: real receiver GET-only; synthetic first setup on localhost.
// Requires Node 22+, Chrome and a new private CHROME_USER_DATA_DIR.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');

async function main() {
  assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR);
  assert.ok(process.env.MANUAL_SCREENSHOT_DIR && process.env.SATIP_DEVICE_URL);
  const device = new URL(process.env.SATIP_DEVICE_URL);
  assert.equal(device.protocol, 'http:');
  assert.equal(require('node:net').isIP(device.hostname), 4);
  assert.ok(!device.username && !device.password);
  const output = process.env.MANUAL_SCREENSHOT_DIR;
  fs.mkdirSync(output, {recursive:true, mode:0o700});
  const login = JSON.parse(execFileSync('ssh', ['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    'root@'+device.hostname, 'cat /etc/satip-web/login.json'], {encoding:'utf8',timeout:10000}));
  const example = {name:'Example SAT-IP server',address:'192.168.1.20',tuners:2,
    udn:'uuid:example',url:'http://192.168.1.20/description.xml'};
  const candidates = ['Das Erste HD','ZDF HD','arte HD','3sat HD','SAT.1','ProSieben','kabel eins'].map((name,i)=>
    ({name,service_uuid:String(i+1).repeat(32)}));
  const state = {state:'unconfigured',source:'satip',prepared:true,recovery_pending:false,
    registration:{state:'ready'},discovery:{state:'complete',servers:[example],interfaces:['eth0'],warnings:[]},
    selected_server:example,tuners:[1,2].map(n=>({server_uuid:'a'.repeat(32),frontend_uuid:String(n).repeat(32),server:example.address,tuner_number:n})),
    frontend_uuid:'1'.repeat(32),scanfiles:[{key:'astra',val:'Astra 19.2E'}],
    scan:{state:'complete',total:112,completed:112,found:candidates.length},candidates,starter:candidates.slice(0,4).map(c=>c.service_uuid)};
  const fixtures = http.createServer((request,response)=>{
    response.setHeader('Cache-Control','no-store');
    if(request.url.startsWith('/api/')) {
      response.setHeader('Content-Type','application/json');
      const data = request.url==='/api/setup/status' ? state :
        request.url==='/api/setup/inputs' ? {devices:[{id:'keyboard',name:'USB keyboard',keyboard:true},{id:'ir',name:'Infrared receiver connected to GPIO',remote:true}],selected:'keyboard',test:{device:'USB keyboard',code:103,time:1}} :
        request.url==='/api/setup/preferences' ? {hostname:'living-room-tv',timezone:'Europe/Berlin',timezones:['Etc/UTC','Europe/Berlin']} :
        request.url==='/api/sources/bouquets' ? {receiver:{model:'Example Enigma2 box'},bouquets:[{name:'Free HD TV'},{name:'Free SD TV'},{name:'Regional TV'}]} :
        {active:'satip',discovery:{satip:{state:'complete',servers:[example]},enigma2:{state:'complete',servers:[{address:'192.168.1.30',model:'Example Enigma2 box'}]}},import:{state:'idle'},receiver:null};
      response.end(JSON.stringify(data)); return;
    }
    const file=request.url==='/'?'setup.html':request.url.slice(1);
    if(!['setup.html','setup.js','sources.js','request.js','app.css'].includes(file)) {response.writeHead(404);response.end();return;}
    response.setHeader('Content-Type',file.endsWith('.html')?'text/html':file.endsWith('.js')?'text/javascript':'text/css');
    response.end(fs.readFileSync(path.join(__dirname,'../satip_controller/static',file)));
  });
  fixtures.listen(0,'127.0.0.1'); await once(fixtures,'listening');
  const chrome=spawn(process.env.CHROME_BINARY,['--headless','--no-first-run','--no-default-browser-check',
    '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+process.env.CHROME_USER_DATA_DIR,'about:blank']);
  let socket,blockedActions=0;
  try {
    const debug=await new Promise((resolve,reject)=>{
      let text='';chrome.stderr.on('data',chunk=>{text+=chunk;const match=text.match(/DevTools listening on (.+)/);if(match)resolve(match[1].trim());});
      chrome.on('error',reject);chrome.on('exit',()=>reject(Error('Chrome exited')));
    });
    const tabs=await (await fetch(new URL('/json/list',debug.replace('ws:','http:')))).json();
    socket=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await once(socket,'open');
    let sequence=0;const pending=new Map();
    const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    socket.addEventListener('message',event=>{
      const data=JSON.parse(event.data),request=pending.get(data.id);
      if(request){pending.delete(data.id);data.error?request.reject(Error(data.error.message)):request.resolve(data.result);}
      if(data.method==='Fetch.requestPaused') {
        const p=data.params;
        if(new URL(p.request.url).origin===device.origin && p.request.method!=='GET') {
          blockedActions++;send('Fetch.failRequest',{requestId:p.requestId,errorReason:'BlockedByClient'}).catch(()=>{});
        } else send('Fetch.continueRequest',{requestId:p.requestId}).catch(()=>{});
      }
    });
    const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert.ok(!r.exceptionDetails,'Browser evaluation failed');return r.result.value;};
    const wait=async expression=>{for(let i=0;i<150;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,100));}throw Error('Timed out: '+expression);};
    await send('Page.enable');await send('Network.enable');
    await send('Fetch.enable',{patterns:[{urlPattern:device.origin+'/*',requestStage:'Request'}]});
    await send('Network.setExtraHTTPHeaders',{headers:{Authorization:'Basic '+Buffer.from(login.username+':'+login.password).toString('base64')}});login.password='';
    const dimensions=(width,height=900)=>send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<600});
    const redact=()=>{
      for(const id of ['receiver-name','device-hostname']) {const n=document.getElementById(id);if(n){if(n.tagName==='INPUT')n.value='living-room-tv';else n.textContent='Living room receiver';}}
      const ssid=document.getElementById('device-ssid');if(ssid)ssid.value='Home Wi-Fi';
      const nodes=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;
      while(n=nodes.nextNode()){if(['SCRIPT','STYLE'].includes(n.parentElement.tagName))continue;n.textContent=n.textContent.replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g,'192.168.1.42').replace(/\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b/gi,'00:00:00:00:00:00').replace(/satip-dev/g,'living-room-tv');}
      document.querySelectorAll('input[type=password]').forEach(n=>n.value='');
    };
    const shot=async name=>{
      await evaluate('document.fonts.ready');await evaluate('('+redact.toString()+')()');
      assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true,'Horizontal overflow');
      fs.writeFileSync(path.join(output,name+'.png'),Buffer.from((await send('Page.captureScreenshot',{captureBeyondViewport:false})).data,'base64'),{mode:0o644});
    };
    await dimensions(1200);
    for(const page of ['status','channels','scan','live']) {
      await send('Page.navigate',{url:device.origin+'/#'+page});
      await wait("document.readyState==='complete' && !!document.getElementById('page-"+page+"') && !document.getElementById('page-"+page+"').hidden");
      await evaluate('new Promise(r=>setTimeout(r,2200))');
      if(page==='channels') {
        await dimensions(1200,1000);
        await evaluate("document.querySelector('#page-channels .columns').scrollIntoView({block:'start'})");
      }
      await shot('manual-'+(page==='scan'?'search':page));
    }
    await dimensions(390,1100);await send('Page.navigate',{url:device.origin+'/#settings'});
    await wait("document.getElementById('device-timezone')?.options.length>0");
    await wait("!document.getElementById('device-source-card').hidden");
    const cropHeight=await evaluate("Math.ceil(document.getElementById('device-source-card').getBoundingClientRect().top)-12");
    await dimensions(390,cropHeight);await shot('manual-settings-mobile');
    assert.equal(blockedActions,0,'Receiver attempted an action; capture blocked it');
    await send('Network.setExtraHTTPHeaders',{headers:{}});await dimensions(1000,1000);
    await send('Page.navigate',{url:'http://127.0.0.1:'+fixtures.address().port+'/'});
    await wait("document.getElementById('channels')?.querySelectorAll('input').length===7 && !document.getElementById('confirm-favourites').disabled");
    await dimensions(1000,780);await shot('manual-setup');
    await dimensions(1000,1000);
    await evaluate("document.getElementById('setup-source-browser').scrollIntoView({block:'start'})");await shot('manual-tuner');
    await evaluate("document.getElementById('selection-heading').scrollIntoView({block:'start'})");await shot('manual-selection');
    await evaluate("document.getElementById('confirm-favourites').click()");await wait("!document.getElementById('setup-sorting').hidden");
    await evaluate('window.scrollTo(0,0)');await shot('manual-order');
    await evaluate("document.getElementById('change-selection').click()");
    state.source='enigma2';
    await wait("document.getElementById('source-kind').value==='enigma2'");
    await evaluate("document.getElementById('source-kind').onchange(); document.getElementById('source-address').value='192.168.1.30'; document.getElementById('setup-source-browser').scrollIntoView({block:'start'})");
    await shot('manual-enigma');
    await evaluate("document.getElementById('source-load-bouquets').click()");
    await wait("document.querySelectorAll('#source-bouquets input').length===3");
    await evaluate("Array.from(document.querySelectorAll('#source-bouquets input')).slice(0,2).forEach(n=>{n.checked=true;n.dispatchEvent(new Event('change',{bubbles:true}));}); document.getElementById('source-bouquet-panel').scrollIntoView({block:'start'})");
    await wait("!document.getElementById('source-import').disabled");
    await shot('manual-bouquets');
    console.log('PASS: eleven browser screenshots; receiver GET-only; SAT-IP and Enigma2 setup on an isolated actual renderer; addresses/names redacted.');
  } finally {if(socket)socket.close();chrome.kill();fixtures.close();}
}
main().catch(error=>{console.error(error.code==='ERR_ASSERTION'?error.message:'Manual capture failed (credentials suppressed): '+error.message);process.exitCode=1;});

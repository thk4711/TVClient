// Read-only offline/live manual check: Node 22+, Chrome, a disposable profile.
// Set SATIP_MANUAL_URL (file:// or private-LAN http://) and MANUAL_EVIDENCE_DIR.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn,execFileSync} = require('node:child_process');
const {once} = require('node:events');

async function main() {
  assert.ok(process.env.CHROME_BINARY && process.env.CHROME_USER_DATA_DIR);
  const url=new URL(process.env.SATIP_MANUAL_URL);
  assert.ok(url.protocol==='file:' || (url.protocol==='http:' && require('node:net').isIP(url.hostname)===4));
  assert.ok(!url.username && !url.password);
  const expectedImages=(fs.readFileSync(path.join(__dirname,'../doc/user-manual.md'),'utf8').match(/^!\[/gm)||[]).length;
  assert.ok(expectedImages>=17);
  let authorization;
  if(url.protocol==='http:') {
    const login=JSON.parse(execFileSync('ssh',['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','root@'+url.hostname,
      'cat /etc/satip-web/login.json'],{encoding:'utf8',timeout:10000}));
    authorization='Basic '+Buffer.from(login.username+':'+login.password).toString('base64');login.password='';
  }
  const chrome=spawn(process.env.CHROME_BINARY,['--headless','--no-first-run','--no-default-browser-check',
    '--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--user-data-dir='+process.env.CHROME_USER_DATA_DIR,'about:blank']);
  let socket;
  try {
    const debug=await new Promise((resolve,reject)=>{
      let text='';chrome.stderr.on('data',chunk=>{text+=chunk;const m=text.match(/DevTools listening on (.+)/);if(m)resolve(m[1].trim());});
      chrome.on('error',reject);chrome.on('exit',()=>reject(Error('Chrome exited')));
    });
    const tabs=await (await fetch(new URL('/json/list',debug.replace('ws:','http:')))).json();
    socket=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await once(socket,'open');
    let sequence=0;const pending=new Map(),violations=[];
    socket.addEventListener('message',event=>{
      const data=JSON.parse(event.data),request=pending.get(data.id);
      if(request){pending.delete(data.id);data.error?request.reject(Error(data.error.message)):request.resolve(data.result);}
      if(data.method==='Network.requestWillBeSent') {
        assert.equal(data.params.request.method,'GET','Manual must never send actions');
        const target=new URL(data.params.request.url);
        if(url.protocol==='http:' && target.origin!==url.origin) violations.push('Remote request');
      }
      if(data.method==='Log.entryAdded' && data.params.entry.level==='error') {
        const entry=data.params.entry;
        violations.push('Browser error at '+(entry.url?new URL(entry.url).pathname:entry.source));
      }
    });
    const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert.ok(!r.exceptionDetails,'Browser evaluation failed');return r.result.value;};
    await send('Page.enable');await send('Network.enable');await send('Log.enable');
    if(authorization) await send('Network.setExtraHTTPHeaders',{headers:{Authorization:authorization}});
    await send('Page.navigate',{url:url.href});
    let ready=false;
    for(let i=0;i<100;i++) {
      ready=await evaluate("document.readyState==='complete' && document.querySelectorAll('figure img').length==="+expectedImages+" && Array.from(document.images).every(n=>n.complete && n.naturalWidth>0)");
      if(ready)break;await new Promise(r=>setTimeout(r,100));
    }
    assert.ok(ready,'Manual/images did not load');
    const links=await evaluate("Array.from(document.querySelectorAll('a[href],link[href]'),n=>n.getAttribute('href'))");
    const anchors=await evaluate("Array.from(document.querySelectorAll('[id]'),n=>n.id)");
    for(const link of links) {
      if(link.startsWith('#')) assert.ok(anchors.includes(link.slice(1)),'Broken contents anchor');
      else if(url.protocol==='http:') {
        const target=new URL(link,url);
        if(target.origin!==url.origin) {
          assert.ok(['https://www.raspberrypi.com/software/','https://argon40.com/'].includes(target.href),'Unreviewed external reference');
          continue; // Optional reference links are not assets and need not be fetched to read offline.
        }
        const response=await fetch(target,{headers:authorization?{Authorization:authorization}:{}});
        assert.equal(response.status,200,'Manual linked asset/reference missing');
      }
    }
    for(const width of [1200,390]) {
      await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width===390});
      await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
      assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true,'Manual horizontal overflow');
      assert.equal(await evaluate("getComputedStyle(document.querySelector('main')).backgroundColor"),'rgb(255, 255, 255)','Stylesheet failed');
    }
    await send('Emulation.setEmulatedMedia',{media:'print'});
    assert.equal(await evaluate("getComputedStyle(document.querySelector('.manual-header')).display"),'none');
    assert.equal(await evaluate("getComputedStyle(document.querySelector('h2')).breakBefore"),'page');
    const pdf=Buffer.from((await send('Page.printToPDF',{printBackground:true,preferCSSPageSize:true})).data,'base64');
    assert.equal(pdf.subarray(0,5).toString(),'%PDF-');assert.ok(pdf.length>100000);
    if(process.env.MANUAL_EVIDENCE_DIR) {
      fs.mkdirSync(process.env.MANUAL_EVIDENCE_DIR,{recursive:true,mode:0o700});
      fs.writeFileSync(path.join(process.env.MANUAL_EVIDENCE_DIR,'manual-print.pdf'),pdf,{mode:0o600});
    }
    assert.deepEqual(violations,[],'Manual requested remote content or logged a browser/CSP error');
    console.log('PASS: '+expectedImages+' captioned images, local contents/reference links, desktop/mobile without overflow, print CSS and PDF; GET-only, optional external references not fetched.');
  } finally {if(socket)socket.close();chrome.kill();}
}
main().catch(error=>{console.error(error.code==='ERR_ASSERTION'?error.message:'Manual browser check failed (credentials suppressed).');process.exitCode=1;});

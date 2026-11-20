const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
    class Element {
        constructor() { this.open = true; this.children = []; this.attrs = {}; this.textContent = ''; this.classList = {add() {}}; }
        append(...children) { this.children.push(...children); }
        replaceChildren() { this.children = []; }
        setAttribute(key, value) { this.attrs[key] = value; }
    }
    const nodes = new Map();
    const byId = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
    let now = 1000;
    const context = vm.createContext({document: {createElement: () => new Element(), createElementNS: () => new Element()},
        Date: {now: () => now * 1000}, byId, put: (id, text) => { byId(id).textContent = text; },
        stateBadge: (id, data) => { byId(id).textContent = data.state; },
        value: (n, unit = '') => typeof n === 'number' ? n.toFixed(1) + unit : 'Not reported',
        bytes: n => typeof n === 'number' ? n + ' bytes' : 'Not reported',
        node: (tag, text) => { const element = new Element(); element.textContent = text; return element; }});
    const source = fs.readFileSync('satip_controller/static/app.js', 'utf8');
    vm.runInContext(source.slice(source.indexOf('    const performanceHistory'), source.indexOf('    function renderTelemetry')), context);
    return {nodes, open: value => { byId('performance-dialog').open = value; }, now: value => { now = value; }, render: data => {
        context.data = data; vm.runInContext('renderPerformance(data)', context);
    }, history: key => vm.runInContext('performanceHistory.get(' + JSON.stringify(key) + ')', context)};
}
const sample = stamp => ({performance:{state:'ok',sampled_at:stamp,cpu_percent:20,cores:[],memory_total:100,memory_used:30,
    swap_total:0,swap_used:0,processes:[{pid:42,name:'<script>secret?</script>',cpu_percent:100,memory_bytes:100}]},
    network:{state:'ok',sampled_at:stamp,interfaces:[]}});

test('closed monitor collects history without creating graphs or process rows', () => {
    const f = fixture(); f.open(false); f.render(sample(1000));
    assert.equal(f.history('cpu').length, 1);
    assert.equal(f.nodes.get('performance-content').children.length, 0);
    assert.equal(f.nodes.has('performance-processes'), false);
    f.now(1005); f.render(sample(1005));
    assert.equal(f.history('cpu').length, 2);
    f.open(true); f.render(sample(1005));
    assert.equal(f.history('cpu').length, 2, 'Opening does not duplicate cached samples');
    assert.equal(f.nodes.get('performance-content').children.length, 3);
    assert.equal(f.nodes.get('performance-processes').children.length, 1);
});

test('history deduplicates cached samples, bounds age and breaks across gaps', () => {
    const f = fixture(); f.render(sample(1000)); f.render(sample(1000));
    assert.equal(f.history('cpu').length, 1);
    f.now(1005); f.render(sample(1005)); assert.equal(f.history('cpu').length, 2);
    f.now(1100); f.render(sample(1100));
    const graph = f.nodes.get('performance-content').children[0].children.find(n => n.children.some(c => c.attrs.d));
    assert.equal((graph.children[0].attrs.d.match(/M/g) || []).length, 2);
    f.now(1401); f.render(sample(1401)); assert.equal(f.history('cpu').length, 1);
    assert.equal(f.nodes.get('performance-processes').children[0].children[1].textContent, '<script>secret?</script>');
});

test('stale samples are not added; missing readings and vanished interfaces remain safe', () => {
    const f = fixture(), data = sample(1000);
    data.network.interfaces = [{name:'wlan0',rx_bps:1000,tx_bps:2000}]; f.render(data);
    assert.equal(f.history('net-wlan0rx_bps').length, 1);
    f.now(1005); data.performance.state = 'stale'; data.performance.sampled_at = 1005; f.render(data);
    assert.equal(f.history('cpu').length, 1);
    assert.equal(f.nodes.get('performance-state').textContent, 'stale');
    f.render({}); assert.equal(f.history('net-wlan0rx_bps'), undefined);
    assert.match(f.nodes.get('performance-note').textContent, /unavailable/);
});

// Menjalankan <script> dashboard di Node dengan DOM tiruan terhadap server nyata. BUKAN uji visual/browser.
// Pakai: node dashboard_dom_check.js http://127.0.0.1:PORT
const base = process.argv[2];
const registry = {};
class El {
  constructor(tag) { this.tag = tag; this.children = []; this._id = ''; this.textContent = ''; this.className = ''; this.checked = false; this.value = ''; this.selected = false; }
  set id(v) { this._id = v; registry[v] = this; }
  get id() { return this._id; }
  appendChild(c) {
    this.children.push(c);
    if (this.tag === 'select') {  // perilaku browser: value = opsi terpilih, atau opsi pertama
      const sel = this.children.find((x) => x.selected) || this.children[0];
      this.value = sel ? sel.value : '';
    }
    return c;
  }
}
for (const id of ['scan-ai', 'scan-manual', 'msg', 'panels']) { const e = new El('x'); e.id = id; }
registry['scan-manual'].checked = true;
let html = '';
const errors = [];
(async () => {
  html = await (await fetch(base + '/')).text();
  const token = /name="dash-token" content="([^"]+)"/.exec(html)[1];
  const script = /<script>([\s\S]*)<\/script>/.exec(html)[1];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (p, o) => realFetch(base + p, o);
  globalThis.document = {
    querySelector: (s) => (s.includes('dash-token') ? { content: token } : null),
    getElementById: (id) => registry[id],
    createElement: (t) => new El(t),
  };
  process.on('unhandledRejection', (e) => errors.push('unhandledRejection: ' + e));
  try { new Function(script)(); } catch (e) { errors.push('script: ' + e.message); }
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  await wait(700);
  const need = [];
  for (const tf of ['M15', 'H1']) {
    for (const k of ['coin', 'apply', 'acc', 'disarm', 'step', 'status', 'balance', 'trades', 'winrate', 'profit', 'loss', 'net', 'dd', 'pos', 'sg', 'halt', 'rej'])
      if (!registry[k + '-' + tf]) need.push(k + '-' + tf);
  }
  const out = { errors, missing: need, status: {}, steps: [] };
  if (!need.length) {
    out.status.M15 = registry['status-M15'].textContent;
    for (const tf of ['H1']) {
      await registry['apply-' + tf].onclick(); await wait(300); out.steps.push(['apply', registry.msg.textContent]);
      await registry['acc-' + tf].onclick(); await wait(300); out.steps.push(['acc', registry.msg.textContent]);
      await registry['step-' + tf].onclick(); await wait(1500); out.steps.push(['step', registry.msg.textContent]);
      out.status[tf] = registry['status-' + tf].textContent;
    }
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ errors: ['fatal: ' + e.message], missing: [] })); process.exit(0); });

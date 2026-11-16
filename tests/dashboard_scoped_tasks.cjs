// Exercise the shipped dashboard bundle with the host SDK contract, without dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const bundle = fs.readFileSync(path.join(__dirname, '../dashboard/dist/index.js'), 'utf8');

async function harness(cards) {
  let component, current, index;
  const states = new Map(), effects = new Map(), requests = [];
  const h = (type, props, ...children) => ({ type, props: props || {}, children: children.flat(Infinity) });
  const SDK = {
    React: { createElement: h },
    hooks: {
      useState(initial) {
        const owner = current, slot = index++;
        const values = states.get(owner) || [];
        states.set(owner, values);
        if (!(slot in values)) values[slot] = initial;
        return [values[slot], value => { values[slot] = value; }];
      },
      useEffect(fn) {
        const key = current.name + ':' + index++;
        if (!effects.has(key)) { effects.set(key, true); fn(); }
      },
    },
    components: Object.fromEntries(['Card', 'CardHeader', 'CardTitle', 'CardContent'].map(name => [name, name])),
    utils: {},
    async fetchJSON(url, options) {
      requests.push({ url, options });
      if (options) return { ok: true };
      if (url.endsWith('/grants')) return { ok: true, grants: cards };
      return { ok: true, app: { state: 'never' }, push: { state: 'not_paired' } };
    },
  };
  vm.runInNewContext(bundle, { window: {
    __HERMES_PLUGINS__: { register() {}, registerSlot(_plugin, _slot, fn) { component = fn; } },
    __HERMES_PLUGIN_SDK__: SDK,
  }, setInterval() { return 1; }, clearInterval() {} });
  function expand(node) {
    if (node == null || node === false || typeof node !== 'object') return node;
    if (typeof node.type === 'function') {
      current = node.type; index = 0;
      return expand(node.type(node.props));
    }
    return { ...node, children: node.children.map(expand).filter(value => value != null && value !== false) };
  }
  function render() { return expand(h(component)); }
  render();
  await new Promise(resolve => setImmediate(resolve));
  return { render, requests, SDK };
}
function nodes(tree) {
  return tree && typeof tree === 'object' ? [tree, ...tree.children.flatMap(nodes)] : [];
}
const card = { id: 'Gtest', digest: 'displayed-digest', status: 'pending', snapshot: {
  installation: 'installation-1', sender: { profile: 'sender', chat: 'origin-chat' },
  recipient: { profile: 'recipient', chat: 'recipient-chat' }, task: '<script>task</script>', scope: 'Only this exact scope',
} };
(async () => {
  const empty = await harness([]);
  assert.equal(nodes(empty.render()).filter(node => node.type === 'Card').length, 0,
    'Do not add an empty Conduit card on an unused installation');
  const app = await harness([card]);
  const tree = app.render();
  const preview = nodes(tree).find(node => node.type === 'pre');
  assert.deepEqual(JSON.parse(preview.children[0]), { id: card.id, digest: card.digest, snapshot: card.snapshot });
  assert.equal(preview.props.dangerouslySetInnerHTML, undefined);
  const approve = nodes(tree).find(node => node.type === 'button' && node.children[0] === 'Approve once');
  assert.ok(approve);
  approve.props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  let writes = app.requests.filter(request => request.options);
  assert.equal(writes.length, 1);
  assert.ok(writes[0].url.endsWith('/Gtest/grant_approve'));
  assert.deepEqual(JSON.parse(writes[0].options.body), { digest: 'displayed-digest' });
  assert.equal(writes[0].options.method, 'POST');
  const failed = await harness([card]);
  failed.SDK.fetchJSON = async (url, options) => {
    if (options) { failed.requests.push({ url, options }); throw new Error('lost acknowledgement'); }
    return { grants: [card] };
  };
  nodes(failed.render()).find(node => node.type === 'button' && node.children[0] === 'Approve once').props.onClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(failed.requests.filter(request => request.options).length, 1, 'Never retry uncertain approval');
  assert.ok(nodes(failed.render()).some(node => node.props.role === 'alert'));
  console.log('Dashboard scoped card: full escaped snapshot, exact approve payload, no automatic retry, no empty card');
})().catch(error => { console.error(error); process.exitCode = 1; });

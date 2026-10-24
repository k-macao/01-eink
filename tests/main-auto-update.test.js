const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');

function loadMain() {
  const values = {
    'chk-auto-update': { checked: false },
    'time-auto-update-am': { value: '09:15' },
    'time-auto-update-pm': { value: '17:15' },
    'auto-status': { textContent: '' },
  };
  const generic = id => values[id] || (values[id] = {
    id, value: '', checked: false, disabled: null, innerHTML: '', textContent: '',
    childNodes: [], style: {}, scrollTop: 0, scrollHeight: 0,
    appendChild(node) { this.childNodes.push(node); },
    removeChild() { this.childNodes.shift(); },
  });
  const store = {};
  const sandbox = {
    console,
    Date,
    Math,
    Uint8Array,
    TextDecoder,
    setTimeout: fn => { /* connection timers are not run in schedule tests */ },
    setInterval: () => 11,
    clearInterval: () => {},
    localStorage: {
      getItem: key => (key in store ? store[key] : null),
      setItem: (key, value) => { store[key] = String(value); },
    },
    navigator: {},
    document: {
      body: {},
      getElementById: generic,
      createElement: tag => generic('made-' + tag + '-' + Math.random()),
      createTextNode: text => ({ textContent: text }),
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'js', 'main.js'), 'utf8'), sandbox);
  // Schedule tests don't need DOM log construction or a real push.
  vm.runInContext('addLog = function () {}; runAutoUpdate = async function () { return true; };', sandbox);
  return { sandbox, values, store };
}

const expr = (box, code) => vm.runInContext(code, box.sandbox);

test('默认配置为每天 09:15 / 17:15 两次', () => {
  const box = loadMain();
  assert.strictEqual(expr(box, 'autoUpdateTimes.join("/")'), '09:15/17:15');
  assert.strictEqual(expr(box, 'autoStatusText()'), '自动推送：已停止');
});

test('早报和晚报在到点后十分钟内各触发一次', () => {
  const box = loadMain();
  expr(box, 'autoUpdateEnabled = true');
  assert.strictEqual(expr(box, 'checkAutoUpdate(new Date(2026, 9, 24, 9, 15, 2))'), true);
  assert.strictEqual(expr(box, 'lastAutoUpdateSlot'), '2026-10-24@09:15');
  assert.strictEqual(expr(box, 'checkAutoUpdate(new Date(2026, 9, 24, 9, 16, 0))'), false);

  // Finish the mocked first run and move to the second daily slot.
  expr(box, 'autoUpdateRunning = false');
  assert.strictEqual(expr(box, 'checkAutoUpdate(new Date(2026, 9, 24, 17, 18, 0))'), true);
  assert.strictEqual(expr(box, 'lastAutoUpdateSlot'), '2026-10-24@17:15');
});

test('超过十分钟不补推，避免打开旧页面后误发过期版次', () => {
  const box = loadMain();
  expr(box, 'autoUpdateEnabled = true');
  assert.strictEqual(expr(box, 'checkAutoUpdate(new Date(2026, 9, 24, 9, 25, 0))'), false);
  assert.strictEqual(expr(box, 'checkAutoUpdate(new Date(2026, 9, 24, 17, 40, 0))'), false);
});

test('两个时刻与启用状态会保存并恢复', () => {
  const box = loadMain();
  box.values['time-auto-update-am'].value = '08:50';
  box.values['time-auto-update-pm'].value = '18:05';
  expr(box, 'setAutoUpdateTimes()');
  expr(box, 'autoUpdateEnabled = true; saveAutoPrefs()');

  assert.strictEqual(box.store['epd.auto.morning'], '08:50');
  assert.strictEqual(box.store['epd.auto.evening'], '18:05');
  assert.strictEqual(box.store['epd.auto.enabled'], '1');

  expr(box, 'autoUpdateEnabled = false; autoUpdateTimes = ["00:00", "00:00"]; loadAutoPrefs()');
  assert.strictEqual(expr(box, 'autoUpdateEnabled'), true);
  assert.strictEqual(expr(box, 'autoUpdateTimes.join("/")'), '08:50/18:05');
});

test('非法时间不会污染自动推送配置', () => {
  const box = loadMain();
  box.values['time-auto-update-am'].value = '99:99';
  box.values['time-auto-update-pm'].value = 'bad';
  expr(box, 'setAutoUpdateTimes()');
  assert.strictEqual(expr(box, 'autoUpdateTimes.join("/")'), '09:15/17:15');
});

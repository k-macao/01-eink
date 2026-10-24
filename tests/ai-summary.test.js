// 007 每日研报卡片：纯 JS 测试（不依赖浏览器 canvas 或第三方包）
//
//   node --test tests/ai-summary.test.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');

function measuredWidth(text, font) {
  const m = /(\d+(?:\.\d+)?)px/.exec(font || '16px');
  const size = m ? parseFloat(m[1]) : 16;
  let width = 0;
  for (const ch of String(text)) {
    const code = ch.codePointAt(0);
    if (code > 0x2e80) width += size;
    else if (/[ilj.,;:'!\s]/.test(ch)) width += size * 0.3;
    else if (/[A-Z0-9]/.test(ch)) width += size * 0.62;
    else width += size * 0.5;
  }
  return width;
}

/** Only the Canvas APIs used by ai-summary.js. */
function createStubContext(w, h) {
  const ctx = {
    font: '16px sans-serif',
    fillStyle: '#000',
    textBaseline: 'top',
    textAlign: 'left',
    texts: [],
    rects: [],
    _w: w,
    _h: h,
    measureText(s) { return { width: measuredWidth(s, ctx.font) }; },
    fillText(t, x, y) { ctx.texts.push({ text: String(t), x, y, font: ctx.font }); },
    fillRect(x, y, rw, rh) { ctx.rects.push({ x, y, w: rw, h: rh }); },
    getImageData() { return { data: new Uint8ClampedArray(w * h * 4).fill(255) }; },
    save() {}, restore() {}, translate() {}, rotate() {}, drawImage() {},
  };
  return ctx;
}

function loadModule(fetchImpl) {
  const ui = {
    'sum-size': '400x300',
    'sum-font': 'hei',
    'sum-size-scale': 'normal',
    'sum-zoom': '1',
  };
  const elements = new Map();
  const makeElement = id => {
    if (elements.has(id)) return elements.get(id);
    const el = {
      id,
      value: ui[id] !== undefined ? ui[id] : '',
      checked: false,
      disabled: null,
      style: {},
      textContent: '',
      options: ['400x300', '296x128', '250x122', 'hei', 'song', 'kai', 'mono',
        'small', 'normal', 'large', '1', '2', '3'].map(value => ({ value })),
      addEventListener() {},
      getContext() { return createStubContext(400, 300); },
    };
    elements.set(id, el);
    return el;
  };

  const store = {};
  const sandbox = {
    console,
    Math,
    Date,
    JSON,
    Uint8Array,
    Uint8ClampedArray,
    setTimeout: () => {},
    fetch: fetchImpl || (async () => { throw new Error('测试环境不联网'); }),
    localStorage: {
      getItem: key => (key in store ? store[key] : null),
      setItem: (key, value) => { store[key] = String(value); },
    },
    document: {
      readyState: 'loading',
      addEventListener() {},
      getElementById: makeElement,
      createElement: () => makeElement('tmp-' + Math.random()),
    },
    addLog() {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'js', 'ai-summary.js'), 'utf8'), sandbox);
  return { sandbox, makeElement, store };
}

const box = loadModule();
const { sandbox, makeElement } = box;
const get = name => vm.runInContext(name, sandbox);
const call = (name, ...args) => get(name)(...args);
const REPORT_TEXT = fs.readFileSync(path.join(ROOT, 'data', '007-report.json'), 'utf8');
const REPORT = JSON.parse(REPORT_TEXT);

function parsedCard() {
  return call('buildCard', call('parse007Report', REPORT_TEXT));
}

test('读取真实 007 JSON，并保留来源与互补多空概率', () => {
  const report = call('parse007Report', REPORT_TEXT);
  assert.strictEqual(report.source.repository, 'k-macao/007');
  assert.strictEqual(report.probability.long + report.probability.short, 100);
  assert.strictEqual(report.probability.long, 62);
  assert.ok(report.topics.length >= 3);
  assert.ok(report.risks.length >= 2);
  assert.ok(report.opportunities.length >= 2);
});

test('卡片只显示附图指定的四个栏目', () => {
  const card = parsedCard();
  assert.strictEqual(card.lines.map(line => line.label).join('/'),
    'AI 多空概率/热点话题/风险点/机会点');
  assert.strictEqual(card.lines.length, 4);
  assert.match(card.date, /^2026-10-24 21:01 晚报$/);
  assert.strictEqual(card.section, '007 · 晚报');
  assert.ok(!card.lines.some(line => /今日盘点|明日关注|窗口摘要|AI 结论/.test(line.label)));
});

test('标题按要求精确改为「章鱼 AI.每日研报」', () => {
  assert.strictEqual(get('REPORT_TITLE'), '章鱼 AI.每日研报');
  const ctx = createStubContext(400, 300);
  call('drawCard', ctx, parsedCard(), 400, 300, {});
  const shown = ctx.texts.map(item => item.text);
  assert.ok(shown.includes('章鱼 AI.每日研报'));
  assert.ok(!shown.some(text => text.includes('上水日报')));
});

test('小屏候选是完整栏目内容，不依赖硬截断', () => {
  const card = parsedCard();
  for (const line of card.lines) {
    assert.ok(Array.isArray(line.alts) && line.alts.length >= 1, line.label + ' 缺少候选');
    assert.strictEqual(line.alts[0], line.text);
    assert.ok(line.alts.every(Boolean));
  }
  const topics = card.lines[1];
  assert.ok(topics.alts.length >= 4, '热点应能逐级缩到 1 条');
  assert.ok(topics.alts.at(-1).includes('中东地缘'));
  assert.ok(!topics.alts.at(-1).endsWith('…'));
});

test('无效 JSON、错误来源、缺栏目均会显式拒绝', () => {
  assert.throws(() => call('parse007Report', '{bad'), /有效 JSON/);
  assert.throws(() => call('parse007Report', {
    source: { repository: 'other/repo' }, probability: { long: 50 },
    topics: ['a'], risks: ['b'], opportunities: ['c'],
  }), /来源不是/);
  assert.throws(() => call('parse007Report', { source: {} }), /来源不是/);
  const noProbability = JSON.parse(REPORT_TEXT);
  noProbability.probability = { long: null, short: null };
  assert.throws(() => call('parse007Report', noProbability), /缺少多空概率/);
  const missing = JSON.parse(REPORT_TEXT);
  missing.risks = [];
  assert.throws(() => call('parse007Report', missing), /缺少风险点/);
});

test('只给一边概率时自动补齐；不一致时以多头为准归一', () => {
  const base = {
    source: { repository: 'k-macao/007' }, topics: ['热点'], risks: ['风险'], opportunities: ['机会'],
  };
  const one = call('parse007Report', { ...base, probability: { short: 61 } });
  assert.strictEqual(one.probability.long, 39);
  const badSum = call('parse007Report', { ...base, probability: { long: 72, short: 72 } });
  assert.strictEqual(badSum.probability.short, 28);
});

test('三种屏幕尺寸都不越界，且四个栏目全部保留', () => {
  const card = parsedCard();
  const opts = call('sumDisplayOptions');
  for (const [w, h] of [[400, 300], [296, 128], [250, 122]]) {
    const ctx = createStubContext(w, h);
    const layout = call('drawCard', ctx, card, w, h, opts);
    assert.ok(layout.bodySize >= 10, `${w}x${h} 字号过小：${layout.bodySize}`);
    assert.strictEqual(layout.blocks.length, 4, `${w}x${h} 应保留四栏目`);
    layout.blocks.forEach((block, index) => {
      assert.ok(card.lines[index].alts.includes(block.item.text),
        `${w}x${h} 的${card.lines[index].label}不是完整候选：${block.item.text}`);
      assert.ok(!block.item.text.includes('…'), `${w}x${h} 不应显示硬截断省略号`);
    });
    for (const item of ctx.texts) {
      assert.ok(item.y >= 0 && item.y < h, '文字纵向越界：' + JSON.stringify(item));
      assert.ok(item.x >= 0 && item.x < w, '文字横向越界：' + JSON.stringify(item));
      assert.ok(item.x + measuredWidth(item.text, item.font) <= w + 0.5,
        `文字横向越界：${item.text}`);
    }
  }
});

test('400×300 的四栏均使用某档完整候选，不把句子腰斩', () => {
  const card = parsedCard();
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, {});
  layout.blocks.forEach((block, index) => {
    assert.ok(card.lines[index].alts.includes(block.item.text),
      `${card.lines[index].label} 不是完整候选：${block.item.text}`);
    assert.ok(!block.item.text.endsWith('…'));
  });
});

test('折行不把标点甩到行首，也不拆英文和百分比', () => {
  const ctx = createStubContext(230, 120);
  ctx.font = '16px sans-serif';
  const lines = call('wrapLines', ctx,
    'AI算力硬件产能锁定 · DeepSeek融资回报审视 · 多头概率62%与空头概率38%', 210);
  const noStart = '，。、；：？！）】》」』…·,.;:!?)]}';
  for (const line of lines.slice(1)) {
    assert.ok(!noStart.includes(line[0]), '标点出现在行首：' + line);
  }
  assert.ok(lines.some(line => line.includes('DeepSeek')), JSON.stringify(lines));
  assert.ok(lines.some(line => line.includes('62%') || line.includes('38%')), JSON.stringify(lines));
});

test('字号档位单调：小 ≤ 标准 ≤ 大', () => {
  const card = parsedCard();
  const base = call('sumDisplayOptions');
  const sizeOf = offset => call('drawCard', createStubContext(400, 300), card, 400, 300,
    Object.assign({}, base, { offset })).bodySize;
  const small = sizeOf(-1), normal = sizeOf(0), large = sizeOf(2);
  assert.ok(small <= normal, `${small} > ${normal}`);
  assert.ok(normal <= large, `${normal} > ${large}`);
});

test('1bpp 位平面：1=白 0=黑；红层与非 8 倍数行尾正确', () => {
  const w = 10, h = 2;
  const data = new Uint8ClampedArray(w * h * 4).fill(255);
  const setPx = (x, y, r, g, b) => {
    const i = (y * w + x) * 4;
    data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = 255;
  };
  setPx(0, 0, 0, 0, 0);
  setPx(7, 0, 0, 0, 0);
  setPx(8, 0, 0, 0, 0);
  setPx(2, 1, 220, 30, 30);
  const first = call('canvasToLayers', { getImageData: () => ({ data }) }, w, h);
  assert.strictEqual(first.byteWidth, 2);
  assert.strictEqual(first.bw[0], 0b01111110);
  assert.strictEqual(first.bw[1], 0b01111111);
  assert.strictEqual(first.bw[2], 0b11011111);
  assert.strictEqual(first.red[2], 0b11011111);

  setPx(3, 1, 30, 30, 30);
  const second = call('canvasToLayers', { getImageData: () => ({ data }) }, w, h);
  assert.strictEqual(second.bw[2], 0b11001111);
  assert.strictEqual(second.red[2], 0b11011111);
});

test('字体样张能画满且不越界', () => {
  const opts = call('sumDisplayOptions');
  for (const [w, h] of [[400, 300], [296, 128]]) {
    const ctx = createStubContext(w, h);
    const info = call('drawFontSample', ctx, w, h, opts);
    assert.ok(info.kept >= 2);
    for (const item of ctx.texts) {
      assert.ok(item.y >= 0 && item.y < h);
      assert.ok(item.x + measuredWidth(item.text, item.font) <= w + 0.5,
        '样张文字横向越界：' + item.text);
    }
  }
});

test('字体/字号偏好只恢复下拉中存在的值', () => {
  const fresh = loadModule();
  fresh.store['epd.sum-font'] = 'mono';
  fresh.store['epd.sum-size-scale'] = 'large';
  vm.runInContext('restoreSumPrefs()', fresh.sandbox);
  assert.strictEqual(fresh.makeElement('sum-font').value, 'mono');
  assert.strictEqual(fresh.makeElement('sum-size-scale').value, 'large');

  fresh.store['epd.sum-font'] = 'not-a-font';
  fresh.makeElement('sum-font').value = 'hei';
  vm.runInContext('restoreSumPrefs()', fresh.sandbox);
  assert.strictEqual(fresh.makeElement('sum-font').value, 'hei');
});

test('loadSummaryCard 只读取同源 data/007-report.json', async () => {
  let requested = '';
  const net = loadModule(async url => {
    requested = String(url);
    return { ok: true, text: async () => REPORT_TEXT };
  });
  const card = await vm.runInContext('loadSummaryCard()', net.sandbox);
  assert.match(requested, /^data\/007-report\.json\?t=\d+$/);
  assert.strictEqual(card.lines.length, 4);
});

test('页面定时检查只在 007 报告 id 变化时自动重绘', async () => {
  let current = JSON.parse(REPORT_TEXT);
  const fresh = loadModule(async () => ({ ok: true, text: async () => JSON.stringify(current) }));
  const first = await vm.runInContext('refreshSummaryIfChanged()', fresh.sandbox);
  assert.strictEqual(first, true);
  assert.strictEqual(vm.runInContext('sumCardCache.id', fresh.sandbox), current.id);

  const unchanged = await vm.runInContext('refreshSummaryIfChanged()', fresh.sandbox);
  assert.strictEqual(unchanged, false);

  current = { ...current, id: current.id + '-next', time: '21:02' };
  const changed = await vm.runInContext('refreshSummaryIfChanged()', fresh.sandbox);
  assert.strictEqual(changed, true);
  assert.strictEqual(vm.runInContext('sumCardCache.id', fresh.sandbox), current.id);
});

test('buildCard(null) 安全返回空卡，页头仍使用新标题', () => {
  const card = call('buildCard', null);
  assert.strictEqual(card.lines.length, 0);
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, {});
  assert.strictEqual(layout.blocks.length, 0);
  assert.ok(ctx.texts.some(item => item.text === '章鱼 AI.每日研报'));
  assert.ok(ctx.texts.some(item => item.text.includes('仅供参考')));
});

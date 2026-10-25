// 007 每日研报卡片：纯 JS 测试（不依赖浏览器 canvas 或第三方包）
//
//   node --test tests/ai-summary.test.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const crypto = require('node:crypto');
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
    'sum-font': 'puhui',
    'sum-size-scale': 'fixed16',
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
      options: ['400x300', '296x128', '250x122', 'puhui', 'fixed16', '1', '2', '3']
        .map(value => ({ value })),
      addEventListener() {},
      getContext() { return createStubContext(400, 300); },
    };
    elements.set(id, el);
    return el;
  };

  const store = {};
  const fontRequests = [];
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
      fonts: {
        load: async (...args) => { fontRequests.push(args); return [{}]; },
      },
      addEventListener() {},
      getElementById: makeElement,
      createElement: () => makeElement('tmp-' + Math.random()),
    },
    addLog() {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'js', 'ai-summary.js'), 'utf8'), sandbox);
  return { sandbox, makeElement, store, fontRequests };
}

const box = loadModule();
const { sandbox, makeElement } = box;
const get = name => vm.runInContext(name, sandbox);
const call = (name, ...args) => get(name)(...args);
const REPORT_TEXT = fs.readFileSync(path.join(ROOT, 'data', '007-report.json'), 'utf8');
const REPORT = JSON.parse(REPORT_TEXT);
const INDEX_HTML = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const MAIN_CSS = fs.readFileSync(path.join(ROOT, 'css', 'main.css'), 'utf8');

function parsedCard() {
  return call('buildCard', call('parse007Report', REPORT_TEXT));
}

test('读取真实 007 JSON，并保留来源与互补多空概率', () => {
  const report = call('parse007Report', REPORT_TEXT);
  assert.strictEqual(report.source.repository, 'k-macao/007');
  assert.strictEqual(report.probability.long + report.probability.short, 100);
  assert.strictEqual(report.probability.long, REPORT.probability.long);
  assert.ok(report.topics.length >= 3);
  assert.ok(report.risks.length >= 2);
  assert.ok(report.opportunities.length >= 2);
});

test('卡片只显示附图指定的四个栏目', () => {
  const card = parsedCard();
  assert.strictEqual(card.lines.map(line => line.label).join('/'),
    'AI 多空概率/热点话题/风险点/机会点');
  assert.strictEqual(card.lines.length, 4);
  assert.strictEqual(card.date, [REPORT.date, REPORT.time, REPORT.edition].join(' '));
  assert.strictEqual(card.section, '007 · ' + REPORT.edition);
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
  assert.strictEqual(call('completeClause', '【某机构：A股预期改善'), 'A股预期改善');
  const card = parsedCard();
  for (const line of card.lines) {
    assert.ok(Array.isArray(line.alts) && line.alts.length >= 1, line.label + ' 缺少候选');
    assert.strictEqual(line.alts[0], line.text);
    assert.ok(line.alts.every(Boolean));
  }
  const topics = card.lines[1];
  assert.ok(topics.alts.length >= 4, '热点应能逐级缩到 1 条');
  assert.ok(topics.alts.includes(call('completeClause', REPORT.topics[0].text)));
  assert.ok(topics.alts.includes(call('completeClause', REPORT.topics[1].text)),
    '窄屏应可选择下一条完整短句');
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

test('三种屏幕正文都严格为 16px、不越界，且四个栏目全部保留', () => {
  const card = parsedCard();
  const opts = call('sumDisplayOptions');
  for (const [w, h] of [[400, 300], [296, 128], [250, 122]]) {
    const ctx = createStubContext(w, h);
    const layout = call('drawCard', ctx, card, w, h, opts);
    assert.strictEqual(layout.bodySize, 16, `${w}x${h} 必须严格使用 16px`);
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

/** 网页版完整四栏的量级：热点 6 条带情绪与权重、风险 5 条、机会 5 条。 */
function heavyPayload() {
  return {
    schema: 1, title: '章鱼 AI.每日研报', id: 'heavy-1',
    date: '2026-10-25', time: '17:10', edition: '晚报',
    source: { repository: 'k-macao/007' }, sample_count: 50, source_count: 13,
    probability: { long: 38, short: 62, label: '谨慎悲观', note: '基于 50 条内容' },
    topics: [
      { text: '费半狂泻近7%、台积电ADR跌超4.5%', sentiment: '谨慎悲观', weight: 11.8 },
      { text: '空头血洗美股、多头投降?', sentiment: '谨慎悲观', weight: 5.4 },
      { text: '半导体SOXX成分股全阵亡', sentiment: '谨慎悲观', weight: 5.3 },
      { text: '一下子跌入极度恐慌', sentiment: '极度恐慌', weight: 4.7 },
      { text: '全球资金大逃亡、AI泡沫要破?', sentiment: '极度恐慌', weight: 1.5 },
      { text: '恒指全周跌1093点、北水连8日流出', sentiment: '极度恐慌', weight: 1.4 },
    ],
    risks: [
      '谨慎悲观档占流量61%,悲观叙事主导易形成自我强化',
      '极度恐慌标题已现(12条),恐慌若加速或快速逼近下轨',
      'AI泡沫、AI融资与巨头资本支出担忧被反复放大',
      '美债息急跌、日元急升等避险信号叠加,波动或加剧',
      '情绪过热上轨未至,但反弹时需防乐观情绪快速冲高后止盈',
    ],
    opportunities: [
      '情绪指数38处中枢偏下,距下轨25尚有13点缓冲空间',
      '若恐慌放大触及≤25极度恐慌区,可寻找左侧买点',
      '部分博主提示最绝望时买入将来最赚,逆向线索浮现',
      '逢低加码与资产检视类内容出现,显示理性配置需求',
      '财报错杀、AI算力布局等乐观催化仍被部分高流量视频提及',
    ],
  };
}

test('内容尽量一屏：完整四栏在 400×300 用简压缩装进一屏，无省略号且条数足量', () => {
  const card = call('buildCard', call('parse007Report', heavyPayload()));
  const opts = call('sumDisplayOptions');
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, opts);

  assert.strictEqual(layout.bodySize, 16);
  assert.strictEqual(layout.blocks.length, 4);
  assert.strictEqual(layout.clean, true, '应找到完整装下一屏的方案');
  assert.strictEqual(layout.hardTrimmed, 0);
  layout.blocks.forEach((block, index) => {
    assert.ok(card.lines[index].alts.includes(block.item.text),
      `${card.lines[index].label} 不是完整候选：${block.item.text}`);
    assert.ok(!block.item.text.includes('…'), '不应出现省略号：' + block.item.text);
  });

  // 用满一屏（旧逻辑 7 行收工并腰斩内容），且每栏保留多条信息而不是只剩一条
  const totalLines = layout.blocks.reduce((a, b) => a + b.wrapped.length, 0);
  assert.ok(totalLines >= 8, '400×300 应至少排 8 行，实际 ' + totalLines);
  const hits = (text, keys) => keys.filter(k => text.includes(k)).length;
  assert.ok(hits(layout.blocks[1].item.text, ['费半', '空头血洗', 'SOXX', '极度恐慌', '全球资金', '恒指']) >= 3,
    '热点至少保留 3 条线索：' + layout.blocks[1].item.text);
  assert.ok(hits(layout.blocks[2].item.text, ['流量61%', '恐慌标题', 'AI泡沫', '美债', '上轨']) >= 2,
    '风险至少保留 2 条线索：' + layout.blocks[2].item.text);
  assert.ok(hits(layout.blocks[3].item.text, ['情绪指数38', '极度恐慌区', '最绝望时', '逢低加码', 'AI算力']) >= 2,
    '机会至少保留 2 条线索：' + layout.blocks[3].item.text);
  assert.ok(layout.keptRatio >= 0.25, '保留率不应低于 25%，实际 ' + layout.keptRatio);

  // 状态栏应说清是“一屏压缩”，而不是把完整短版误报成截短
  const desc = call('describeLayout', layout, opts);
  assert.match(desc, /已压缩（一屏完整）/);
  assert.ok(!desc.includes('已截短'), desc);

  for (const item of ctx.texts) {
    assert.ok(item.y >= 0 && item.y < 300, '文字纵向越界：' + JSON.stringify(item));
    assert.ok(item.x + measuredWidth(item.text, item.font) <= 400 + 0.5,
      '文字横向越界：' + item.text);
  }
});

test('上游自带的省略号断尾与未闭合【不上屏', () => {
  const report = call('parse007Report', {
    source: { repository: 'k-macao/007' },
    probability: { long: 55, short: 45 },
    topics: ['【前三季度深圳锂电池出口至200多个国家和地区',
      "【Trump attacks Obama as 'extremely incompet…"],
    risks: ['【QDII segregated-account products being wound down; ind…'],
    opportunities: ["【Trump attacks Obama as 'extremely incompetent', touts…"],
  });
  assert.strictEqual(report.topics[0].text, '前三季度深圳锂电池出口至200多个国家和地区');
  // 截断在引号内部：未闭合引号与悬空虚词一并去掉
  assert.strictEqual(report.topics[1].text, 'Trump attacks Obama');
  assert.strictEqual(report.risks[0].text, 'QDII segregated-account products being wound down');
  assert.strictEqual(report.opportunities[0].text, "Trump attacks Obama as 'extremely incompetent'");

  const card = call('buildCard', report);
  const opts = call('sumDisplayOptions');
  // 400×300 装得下清理后的全文：任何一栏都不能再带上游的断尾省略号。
  // （小屏上若某栏只有一条无标点长标题，不存在更短的完整版本，
  //  省略号仍是最后兜底，故小屏这里只断言不越界；真实 007 数据每栏多条，
  //  总有完整的短句可选，见「三种屏幕…」用例。）
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, opts);
  assert.strictEqual(layout.clean, true);
  layout.blocks.forEach(block => {
    assert.ok(!block.item.text.includes('…'), '400x300 出现了断尾：' + block.item.text);
  });
  for (const [w, h] of [[296, 128], [250, 122]]) {
    const sctx = createStubContext(w, h);
    call('drawCard', sctx, card, w, h, opts);
    for (const item of sctx.texts) {
      assert.ok(item.y >= 0 && item.y < h, '文字纵向越界：' + JSON.stringify(item));
      assert.ok(item.x + measuredWidth(item.text, item.font) <= w + 0.5,
        '文字横向越界：' + item.text);
    }
  }
});

test('内容轻的日子不压缩：全文原样上屏', () => {
  const card = call('buildCard', call('parse007Report', {
    source: { repository: 'k-macao/007' },
    date: '2026-10-25', time: '09:10', edition: '早报',
    probability: { long: 60, short: 40, label: '谨慎乐观' },
    topics: [{ text: '锂电排产回暖', sentiment: '积极乐观', weight: 3.2 }],
    risks: ['波动仍大'],
    opportunities: ['逢低关注'],
  }));
  const opts = call('sumDisplayOptions');
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, opts);
  assert.strictEqual(layout.trimmed, 0);
  layout.blocks.forEach((block, index) => {
    assert.strictEqual(block.item.text, card.lines[index].text);
  });
  assert.ok(layout.blocks[1].item.text.includes('权重3.2%'), '装得下时应保留完整标注');
});

test('completeClause 处理短来源前缀与数字时间', () => {
  assert.strictEqual(call('completeClause', '野村：维持韩国央行3.50%终端利率预测'),
    '维持韩国央行3.50%终端利率预测');
  assert.strictEqual(call('completeClause', '09:30开盘走高'), '09:30开盘走高');
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

test('页面自托管阿里巴巴普惠体 Regular，并锁定字体与字号控件', () => {
  const fontPath = path.join(ROOT, 'fonts', 'AlibabaPuHuiTi-3-55-Regular.woff2');
  const font = fs.readFileSync(fontPath);
  assert.strictEqual(font.subarray(0, 4).toString('ascii'), 'wOF2');
  assert.strictEqual(crypto.createHash('sha256').update(font).digest('hex'),
    '287046cd518a90d7bbf3a7fddb509df5912aedb98a6055d2d96975f2495122fc');
  assert.ok(font.length > 1_000_000, '字体文件异常过小');
  assert.match(MAIN_CSS, /@font-face[\s\S]*font-family:\s*"Alibaba PuHuiTi 3"/);
  assert.match(MAIN_CSS, /font-weight:\s*400/);
  assert.match(MAIN_CSS, /AlibabaPuHuiTi-3-55-Regular\.woff2/);
  assert.match(INDEX_HTML, /rel="preload"[^>]*AlibabaPuHuiTi-3-55-Regular\.woff2[^>]*as="font"/);
  assert.match(INDEX_HTML, /id="sum-font" disabled[^>]*>[\s\S]*value="puhui" selected/);
  assert.match(INDEX_HTML, /id="sum-size-scale" disabled[^>]*>[\s\S]*value="fixed16" selected>16px/);
});

test('默认严格使用阿里巴巴普惠体、三款屏幕正文 16px、非加粗', () => {
  const card = parsedCard();
  const opts = call('sumDisplayOptions');
  assert.strictEqual(opts.fontKey, 'puhui');
  assert.strictEqual(opts.fontLabel, '阿里巴巴普惠体 · 非加粗');
  assert.strictEqual(opts.bodySize, 16);
  assert.strictEqual(opts.boldBody, false);
  assert.match(call('cardFont', 16, opts, true), /^400 16px .*Alibaba PuHuiTi 3/);
  assert.ok(!call('cardFont', 16, opts, true).includes('bold'));

  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, opts);
  assert.strictEqual(layout.bodySize, 16);
  assert.ok(ctx.texts.every(item => !item.font.includes('bold')));
  assert.ok(ctx.texts.every(item => item.font.includes('Alibaba PuHuiTi 3')));
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

test('旧字体偏好不能覆盖固定的普惠体 16px 配置', () => {
  const fresh = loadModule();
  fresh.store['epd.sum-font'] = 'mono';
  fresh.store['epd.sum-size-scale'] = 'large';
  vm.runInContext('restoreSumPrefs()', fresh.sandbox);
  assert.strictEqual(fresh.makeElement('sum-font').value, 'puhui');
  assert.strictEqual(fresh.makeElement('sum-size-scale').value, 'fixed16');
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

test('Canvas 绘制前等待阿里巴巴普惠体 WebFont，且只加载一次', async () => {
  const fresh = loadModule();
  await vm.runInContext('ensureSummaryFont()', fresh.sandbox);
  await vm.runInContext('ensureSummaryFont()', fresh.sandbox);
  assert.strictEqual(fresh.fontRequests.length, 1);
  assert.match(fresh.fontRequests[0][0], /^400 16px "Alibaba PuHuiTi 3"$/);
  assert.match(fresh.fontRequests[0][1], /章鱼研报/);
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

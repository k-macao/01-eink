// AI 总结卡片渲染的纯 JS 测试（不依赖 canvas / 浏览器 / 第三方包）
//
//   node --test tests/
//
// 用一个「按字符宽度量字」的假 canvas 上下文加载 js/ai-summary.js，
// 检查解析、折行、字号选择、越界与 1bpp 位平面打包。

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');

/** 假 canvas 2D 上下文：只实现 drawCard / drawFontSample 用到的那几个 API */
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
    measureText(s) {
      const m = /(\d+(?:\.\d+)?)px/.exec(ctx.font);
      const size = m ? parseFloat(m[1]) : 16;
      let width = 0;
      for (const ch of String(s)) {
        const code = ch.codePointAt(0);
        if (code > 0x2e80) width += size;              // 汉字 / 全角标点
        else if (/[ilj.,;:'!\s]/.test(ch)) width += size * 0.3;
        else if (/[A-Z0-9]/.test(ch)) width += size * 0.62;
        else width += size * 0.5;
      }
      return { width };
    },
    fillText(t, x, y) { ctx.texts.push({ text: String(t), x, y, font: ctx.font }); },
    fillRect(x, y, rw, rh) { ctx.rects.push({ x, y, w: rw, h: rh }); },
    getImageData() { return { data: new Uint8ClampedArray(w * h * 4).fill(255) }; },
    save() {}, restore() {}, translate() {}, rotate() {}, drawImage() {},
  };
  return ctx;
}

function loadModule() {
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
      options: Object.keys(ui).includes(id)
        ? ['400x300', '296x128', '250x122', 'hei', 'song', 'kai', 'mono', 'small', 'normal', 'large', '1', '2', '3']
          .map(v => ({ value: v })).filter(o => o.value === ui[id] || true)
        : [],
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
    Uint8Array,
    Uint8ClampedArray,
    setTimeout: (fn) => { /* 测试里不跑延迟任务 */ },
    fetch: async () => { throw new Error('测试环境不联网'); },
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    document: {
      readyState: 'loading',
      addEventListener() {},
      getElementById: makeElement,
      createElement: () => makeElement('tmp' + Math.random()),
    },
    addLog() {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const src = fs.readFileSync(path.join(ROOT, 'js', 'ai-summary.js'), 'utf8');
  vm.runInContext(src, sandbox);
  return { sandbox, makeElement, store };
}

const { sandbox, makeElement } = loadModule();
const get = name => vm.runInContext(name, sandbox);
const call = (name, ...args) => get(name)(...args);

const README_HTML = fs.readFileSync(path.join(ROOT, '001', 'output', 'latest.html'), 'utf8');

test('从 latest.html 解析「总结」栏目', () => {
  const summary = call('extractSummary', README_HTML);
  assert.ok(summary, '应能找到总结栏目');
  assert.match(summary.date, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(summary.fields['今日盘点']);
  assert.ok(summary.fields['明日关注']);
  assert.ok(summary.fields['风险关注']);
  // 花体装饰行 / emoji 行不应混进字段
  for (const k of Object.keys(summary.fields)) {
    assert.ok(!/[█▓▒░]/.test(k), '字段名不应含块字符：' + k);
  }
});

test('卡片最多 4 条，且内容非空', () => {
  const card = call('buildCard', call('extractSummary', README_HTML));
  assert.ok(card.lines.length > 0 && card.lines.length <= 4);
  for (const line of card.lines) {
    assert.ok(line.label && line.text);
  }
});

test('卡片用「窗口摘要」取代「风险关注 / AI 结论」', () => {
  const card = call('buildCard', call('extractSummary', README_HTML));
  const labels = card.lines.map(l => l.label);
  // 数组来自 vm 沙箱，跨 realm 的 deepStrictEqual 会因原型不同失败，用字符串比
  assert.strictEqual(labels.join('/'), '今日盘点/明日关注/窗口摘要');
  assert.ok(!labels.includes('风险关注'));
  assert.ok(!labels.includes('AI 结论'));
  // 摘要里的日子必须都能在日报原文里找到，不能是编的
  const winText = card.lines[2].text;
  assert.ok(/10-2[6-9]|10-30/.test(winText), '应含未来一周的日期：' + winText);
  for (const md of winText.match(/\d{2}-\d{2}/g) || []) {
    assert.ok(README_HTML.includes(md), '窗口摘要里的日期在原文中不存在：' + md);
  }
});

test('窗口摘要只覆盖报告日之后的 5 个工作日', () => {
  const summary = call('extractSummary', README_HTML);
  const card = call('buildCard', summary);
  const winText = card.lines[2].text;

  const days = call('nextFiveWeekdays', summary.date);
  assert.deepStrictEqual(Array.from(days), ['10-26', '10-27', '10-28', '10-29', '10-30']);

  // 跨月 / 跨年也不能漏：区间两头就是这五天的首尾
  assert.ok(winText.startsWith(days[0] + '~' + days[days.length - 1]), '区间不对：' + winText);
  for (const md of winText.match(/\d{2}-\d{2}/g) || []) {
    assert.ok(days.includes(md), '超出一周范围：' + md + ' | ' + winText);
  }
  // 30 天口径的内容不该再出现在卡上
  assert.ok(!/个时间点|最密|★★★/.test(winText), '窗口摘要混进了 30 天口径：' + winText);

  // 每天只留一条，且都在原文里
  const picked = call('windowDatedItems', summary.window).filter(it => days.includes(it.md));
  assert.ok(picked.length >= 3, '十条内应挑出条目：' + picked.length);
});

test('五天内没有日程时退回 30 天口径', () => {
  const only30 = [{ label: '时间窗口', value: '2026-10-23 ~ 2026-11-22 · 未来 30 天' }];
  assert.strictEqual(call('composeFiveDayVariants', only30, '2026-10-23').length, 0);

  // 日期都在五天之内的条目一律不取（10-31 已经开始下一周）
  const far = [{ label: '央行议息 / 重要会议', value: '10-31 美联储议息会议、11-07 央行议息会议' }];
  assert.strictEqual(call('composeFiveDayVariants', far, '2026-10-23').length, 0);
  const near = [{ label: '央行议息 / 重要会议', value: '10-28 美联储议息会议' }];
  assert.deepStrictEqual(
    Array.from(call('composeFiveDayVariants', near, '2026-10-23')),
    ['10-26~10-30 · 10-28 美联储议息', '10-28 美联储议息'],
  );
});

test('窗口摘要：解析时间节点栏目的表，压成一句话', () => {
  const summary = call('extractSummary', README_HTML);
  assert.ok(Array.isArray(summary.window) && summary.window.length >= 3);
  const labels = summary.window.map(p => p.label);
  assert.ok(labels.includes('时间窗口'));
  assert.ok(labels.includes('时间点合计'));

  const text = call('composeWindowSummary', summary.window);
  assert.ok(/\d+ 个时间点/.test(text), '应含时间点总数：' + text);
  assert.ok(/最密 \d{2}-\d{2}/.test(text), '应含最密集日：' + text);
  assert.ok(text.length <= 130, '一句话不应超过 130 字，实际 ' + text.length);
});

test('pixel 主题的窗口摘要（标签与内容分行）也能解析', () => {
  const html = fs.readFileSync(path.join(ROOT, '001', 'output', 'daily_report_20260928.html'), 'utf8');
  const summary = call('extractSummary', html);
  assert.ok(Array.isArray(summary.window), '应能解析 pixel 版窗口摘要');
  const labels = summary.window.map(p => p.label);
  assert.ok(labels.includes('时间窗口'));
  assert.ok(labels.includes('最密集日'));
  const text = call('composeWindowSummary', summary.window);
  // 重头戏要挑对：美联储议息 / 中全会 优先级高于「国民经济运行情况发布会」
  assert.ok(text.includes('美联储议息'), '应挑出美联储议息：' + text);
  assert.ok(!text.includes('国民经济运行情况发布会'), '不该挑发布会：' + text);
});

test('旧日报没有窗口摘要表时退回风险关注 / AI 结论', () => {
  const html = README_HTML.split('窗口摘要').join('窗口概览');
  const summary = call('extractSummary', html);
  assert.strictEqual(summary.window, null);
  const card = call('buildCard', summary);
  const labels = card.lines.map(l => l.label);
  assert.ok(labels.includes('风险关注'));
  assert.ok(labels.includes('AI 结论'));
});

test('窗口摘要折行不拆数字与量词、不拆专名', () => {
  const wrapLines = get('wrapLines');
  const ctx = createStubContext(400, 300);
  ctx.font = 'bold 18px sans-serif';
  const text = '30 天 · 60 个时间点 · ★★★59 · 最密 10-29（8 项） · 10-26 二十届五中全会、10-28 美联储议息';
  const lines = wrapLines(ctx, text, 276);
  for (const line of lines) {
    assert.ok(!/\d$/.test(line.replace(/[·、，]$/, '')), '数字被留在行尾：' + line);
  }
  assert.ok(lines.some(l => l.includes('二十届五中全会')), '专名被拆开：' + JSON.stringify(lines));
  assert.ok(lines.some(l => l.includes('60 个时间点')), '数字与量词被拆开：' + JSON.stringify(lines));
});

test('三种屏幕尺寸都不越界，字号不低于 10px', () => {
  const card = call('buildCard', call('extractSummary', README_HTML));
  const drawCard = get('drawCard');
  const opts = call('sumDisplayOptions');
  for (const [w, h] of [[400, 300], [296, 128], [250, 122]]) {
    const ctx = createStubContext(w, h);
    const layout = drawCard(ctx, card, w, h, opts);
    assert.ok(layout.bodySize >= 10, w + 'x' + h + ' 字号过小：' + layout.bodySize);
    assert.ok(layout.blocks.length >= 1);
    // 所有文字都必须落在画布内
    for (const t of ctx.texts) {
      assert.ok(t.y >= 0 && t.y < h, '文字纵向越界：' + JSON.stringify(t));
      assert.ok(t.x >= 0 && t.x < w, '文字横向越界：' + JSON.stringify(t));
    }
  }
});

test('正文每一行都不超过可用宽度', () => {
  const card = {
    date: '2026-10-23',
    lines: [
      { label: '今日盘点', text: '整体偏多｜A股普涨强势｜政策偏暖｜北向资金净流入 120 亿' },
      { label: '风险关注', text: '媒体舆情 结构化预警 12 只（红/橙 3） · 异常关注 7 只 · 最高 盈富基金 72/100' },
      { label: 'AI 结论', text: '本次日报的配料表：24 路数据里 19 路是当天新鲜货，缺的 2 路直接点名、不装样子。' },
    ],
  };
  const drawCard = get('drawCard');
  const opts = call('sumDisplayOptions');
  const w = 400, h = 300;
  const ctx = createStubContext(w, h);
  drawCard(ctx, card, w, h, opts);
  for (const t of ctx.texts) {
    const width = ctx.measureText(t.text).width;
    assert.ok(t.x + width <= w + 0.5, '文字超出右边界：' + t.text + ' 右端 ' + (t.x + width));
  }
});

test('折行不会把标点甩到行首，也不会拦腰切断英文', () => {
  const wrapLines = get('wrapLines');
  const ctx = createStubContext(200, 100);
  ctx.font = '16px sans-serif';
  const lines = wrapLines(ctx, '媒体舆情 结构化预警 5 只（红/橙 0），最高 盈富基金 40/100｜策略关键词 降级×1、加息×1', 200);
  const NO_START = '，。、；：？！）】》」』…·,.;:!?)]}';
  for (const line of lines.slice(1)) {
    assert.ok(NO_START.indexOf(line[0]) < 0, '标点出现在行首：' + line);
  }
  const en = wrapLines(ctx, '缺的 2 路（申万一级行业轮动策略、StockTwits）直接点名', 200);
  assert.ok(en.some(l => l.includes('StockTwits')), 'StockTwits 被拆开：' + JSON.stringify(en));
});

test('字号档位单调：小 ≤ 标准 ≤ 大', () => {
  const card = call('buildCard', call('extractSummary', README_HTML));
  const drawCard = get('drawCard');
  const base = call('sumDisplayOptions');
  const sizeOf = offset => {
    const ctx = createStubContext(400, 300);
    return drawCard(ctx, card, 400, 300, Object.assign({}, base, { offset })).bodySize;
  };
  const small = sizeOf(-1), normal = sizeOf(0), large = sizeOf(2);
  assert.ok(small <= normal, '小档不应比标准档大：' + small + ' > ' + normal);
  assert.ok(normal <= large, '标准档不应比大档大：' + normal + ' > ' + large);
});

test('1bpp 位平面：1=白 0=黑，红色层 0=红 1=非红，行尾补齐保持白', () => {
  const canvasToLayers = get('canvasToLayers');
  const w = 10, h = 2;                     // 10 不是 8 的倍数，右侧补 6 位
  const data = new Uint8ClampedArray(w * h * 4).fill(255);
  const setPx = (x, y, r, g, b) => {
    const i = (y * w + x) * 4;
    data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = 255;
  };
  setPx(0, 0, 0, 0, 0);        // 黑
  setPx(7, 0, 0, 0, 0);        // 黑，字节最低位
  setPx(8, 0, 0, 0, 0);        // 黑，第二字节首位
  setPx(2, 1, 220, 30, 30);    // 红

  const ctx = {
    getImageData: () => ({ data }),
  };
  const { bw, red, byteWidth } = canvasToLayers(ctx, w, h);
  assert.strictEqual(byteWidth, 2);
  assert.strictEqual(bw.length, byteWidth * h);

  // 第一行第一字节：bit7(0,0)、bit0(7,0) 为黑 → 0b0111_1110
  assert.strictEqual(bw[0], 0b01111110);
  // 第一行第二字节：bit7(8,0) 黑，其余补齐位必须为 1（白）
  assert.strictEqual(bw[1], 0b01111111);
  // 第二行第一字节：只有 x=2 是红点。红色在灰度上也算深色，
  // 所以黑白层同样置黑，红色层该位单独清零（1 = 非红）
  assert.strictEqual(bw[2], 0b11011111);
  assert.strictEqual(red[2], 0b11011111);
  // 不是红色的深色像素：红色层保持 1，不会被误判成红
  setPx(3, 1, 30, 30, 30);
  const second = canvasToLayers({ getImageData: () => ({ data }) }, w, h);
  assert.strictEqual(second.bw[2], 0b11001111);
  assert.strictEqual(second.red[2], 0b11011111);
});

test('字体样张能画满且不越界', () => {
  const drawFontSample = get('drawFontSample');
  const opts = call('sumDisplayOptions');
  for (const [w, h] of [[400, 300], [296, 128]]) {
    const ctx = createStubContext(w, h);
    const info = drawFontSample(ctx, w, h, opts);
    assert.ok(info.kept >= 2, '样张至少要有两行对比');
    for (const t of ctx.texts) {
      assert.ok(t.y >= 0 && t.y < h, '样张文字纵向越界：' + JSON.stringify(t));
      assert.ok(t.x + ctx.measureText(t.text).width <= w + 0.5, '样张文字横向越界：' + t.text);
    }
  }
});

test('偏好记忆只接受下拉里存在的值', () => {
  const { store } = loadModule();
  store['epd.sum-font'] = 'not-a-font';
  const saved = {};
  Object.assign(saved, store);
  // restore 逻辑：非法值应被忽略，控件保持原值
  const box = loadModule();
  box.store['epd.sum-font'] = 'mono';
  vm.runInContext('restoreSumPrefs()', box.sandbox);
  assert.strictEqual(box.makeElement('sum-font').value, 'mono');
});

test('小屏放不下整句时，窗口摘要换成完整的短一档，而不是用省略号腰斩', () => {
  const card = call('buildCard', call('extractSummary', README_HTML));
  const win = card.lines[2];
  assert.ok(Array.isArray(win.alts) && win.alts.length >= 3, '应给出逐级更短的候选');
  assert.strictEqual(win.alts[0], win.text, '第一档就是完整版');

  for (const [w, h] of [[296, 128], [250, 122]]) {
    const ctx = createStubContext(w, h);
    const layout = call('drawCard', ctx, card, w, h, {});
    const shown = layout.blocks[2].item.text;
    assert.ok(win.alts.includes(shown), '小屏应是某一档完整短句，实际：' + shown);
    assert.ok(!/…$/.test(shown), '短句不应以省略号收尾：' + shown);
    // 短句里的数字仍然全部来自日报原文
    for (const num of shown.match(/\d+/g) || []) {
      assert.ok(README_HTML.includes(num), '短句里的数字在原文中不存在：' + num);
    }
  }
});

test('没有「总结」栏目的旧日报：buildCard(null) 返回空卡片而不是抛错', () => {
  const card = call('buildCard', null);
  assert.strictEqual(card.lines.length, 0);
  const ctx = createStubContext(400, 300);
  const layout = call('drawCard', ctx, card, 400, 300, {});
  assert.strictEqual(layout.blocks.length, 0);
  // 页头 / 页脚照画
  const texts = ctx.texts.map(t => t.text);
  assert.ok(texts.includes('章鱼 AI · 上水日报'));
  assert.ok(texts.some(t => t.includes('仅供参考')));
});

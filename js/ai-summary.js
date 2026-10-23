// ---------- AI 总结：抓取日报 → 渲染成墨水屏位图 → 蓝牙推送 ----------
// 与 main.js 的耦合刻意保持最小：
//   - 只读取 addLog / epdCharacteristic / gattServer（旧版 main.js 也有）
//   - 写入走本文件的 writeBytes()，main.js 有 writePayload 就用，没有就自己写
//     这样即使浏览器缓存了旧版 main.js，推送功能也不会直接报 not defined
//   - 指令码直接写常量（属于协议本身，不依赖 main.js 的 EpdCmd）

const SUMMARY_PATH = '001/output/latest.html';

const SUM_CMD = {
  INIT:        0x01,
  REFRESH:     0x05,
  WRITE_IMAGE: 0x30,
};

/**
 * 统一写入入口。
 * main.js（新版）提供 writePayload(bytes, withResponse, silent)；
 * 若为旧缓存版本则没有该函数，这里退化成直接写特征值。
 */
async function writeBytes(bytes, withResponse) {
  const data = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes);

  if (typeof writePayload === 'function') {
    return writePayload(data, withResponse, true);
  }

  if (typeof epdCharacteristic === 'undefined' || epdCharacteristic == null) {
    if (typeof addLog === 'function') addLog('服务不可用，请检查蓝牙连接');
    return false;
  }

  try {
    if (withResponse) await epdCharacteristic.writeValueWithResponse(data);
    else await epdCharacteristic.writeValueWithoutResponse(data);
  } catch (e) {
    console.error(e);
    if (e.message && typeof addLog === 'function') addLog('write: ' + e.message);
    return false;
  }
  try { lastWriteTime = Date.now(); } catch (_) { /* 旧版没有该变量也不影响 */ }
  return true;
}

function isDeviceConnected() {
  if (typeof gattServer === 'undefined' || gattServer == null) return false;
  return gattServer.connected === true;
}

// MTU / RLE 由 main.js 在收到设备 notification 时写入
const EpdLink = globalThis.EpdLink || (globalThis.EpdLink = { mtu: 240, rle: false, reported: false });

let pushingSummary = false;

// ---------- 解析 ----------

function stripTags(s) {
  return String(s).replace(/<[^>]*>/g, ' ');
}

function decodeEntities(s) {
  return String(s)
    .replace(/&#(\d+);/g, (_, d) => String.fromCharCode(parseInt(d, 10)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function normalizeText(s) {
  return decodeEntities(stripTags(s)).replace(/\s+/g, ' ').trim();
}

/**
 * 从日报 HTML 中取出「总结」栏目的 标签 → 内容 映射。
 * 结构固定为 <h2>总结</h2> ... <tr><td>标签</td><td>内容</td></tr>
 */
function extractSummary(html) {
  const h2Re = /<h2[^>]*>([\s\S]*?)<\/h2>/g;
  const heads = [];
  let m;
  while ((m = h2Re.exec(html)) !== null) {
    heads.push({ index: m.index, end: m.index + m[0].length, text: normalizeText(m[1]) });
  }

  const idx = heads.findIndex(h => h.text.indexOf('总结') >= 0);
  if (idx < 0) return null;

  const start = heads[idx].end;
  const end = idx + 1 < heads.length ? heads[idx + 1].index : html.length;
  const section = html.slice(start, end);

  const fields = {};
  const trRe = /<tr[^>]*>([\s\S]*?)<\/tr>/g;
  let tr;
  while ((tr = trRe.exec(section)) !== null) {
    const tds = [];
    const tdRe = /<td[^>]*>([\s\S]*?)<\/td>/g;
    let td;
    while ((td = tdRe.exec(tr[1])) !== null) tds.push(normalizeText(td[1]));
    if (tds.length < 2) continue;
    const label = tds[0];
    const value = tds[1];
    if (!label || !value) continue;
    // 跳过花体装饰行（█▓▒░ 等块字符 / emoji）与过长的伪标签
    if (/[█▓▒░▀▄■□●]/.test(label)) continue;
    if (/[\u{1F300}-\u{1FAFF}]/u.test(label)) continue;
    if (label.length > 12) continue;
    if (!(label in fields)) fields[label] = value;
  }

  if (Object.keys(fields).length === 0) return null;

  // 报告日期
  const dateMatch = /<meta[^>]*name=["']octopus-report-date["'][^>]*content=["'](\d{8})["']/i.exec(html);
  let date = '';
  if (dateMatch) {
    const d = dateMatch[1];
    date = d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8);
  }

  return { date, fields, window: extractWindowSummary(html) };
}

/** 取第一句（到句号/感叹号），最多 maxLen 字 */
function firstSentence(text, maxLen) {
  if (!text) return '';
  const m = /^[^。！？!?]{1,200}[。！？!?]?/.exec(text);
  let s = m ? m[0] : text;
  if (s.length > maxLen) s = s.slice(0, maxLen - 1) + '…';
  return s;
}

function clampText(text, maxLen) {
  if (!text) return '';
  return text.length > maxLen ? text.slice(0, maxLen - 1) + '…' : text;
}

/**
 * 「窗口摘要」的字段名（见 001/output/pipeline.py 的 _cal_digest）。
 * 精简版只有前 3 条，全量版还有中国/美国关键读数、最密集日、筛选口径。
 */
const WINDOW_LABELS = [
  '时间窗口', '时间点合计', '央行议息 / 重要会议', '央行议息/重要会议',
  '中国关键读数', '美国关键读数', '最密集日', '筛选口径',
];

const squashLabel = s => String(s).replace(/\s+/g, '').replace(/／/g, '/');

const WINDOW_LABEL_SET = new Set(WINDOW_LABELS.map(squashLabel));

/**
 * 取出「时间节点量化分析」栏目里的「窗口摘要」表。
 *
 * 两个主题的结构不一样：研报版是一行两列（标签 / 内容），pixel 版是
 * 标签、内容各占一行。所以这里不按 <tr><td> 结构硬解，而是把标签和
 * 单元格边界都压成分隔符，再按「已知字段名 → 下一段」配对，两种版式通吃。
 *
 * 返回 [{ label, value }]（按原顺序）；老日报没有这张表时返回 null。
 */
function extractWindowSummary(html) {
  const head = /(^|>)\s*窗口摘要\s*</.exec(html);
  if (!head) return null;

  const at = head.index + head[0].length;
  let end = html.indexOf('<!--SPLIT-->', at);
  if (end < 0) end = Math.min(html.length, at + 12000);
  const seg = html.slice(at, end);

  // 单元格 / 行 / 区块边界统一换成 ，便于按段切
  const flat = decodeEntities(
    seg.replace(/<\/?(td|th|tr|div|table|tbody|p|br)\b[^>]*>/gi, '\u0001')
       .replace(/<[^>]*>/g, ' ')
  );
  const tokens = flat.split('\u0001').map(t => t.replace(/\s+/g, ' ').trim()).filter(Boolean);

  const pairs = [];
  for (let i = 0; i < tokens.length; i++) {
    if (!WINDOW_LABEL_SET.has(squashLabel(tokens[i]))) continue;
    // 值 = 这一段之后、下一个「字段名」之前的第一段非空文本
    let value = '';
    for (let j = i + 1; j < tokens.length; j++) {
      if (WINDOW_LABEL_SET.has(squashLabel(tokens[j]))) break;
      if (tokens[j].trim()) { value = tokens[j].trim(); break; }
    }
    if (!value) continue;
    pairs.push({ label: tokens[i].replace(/\s+/g, ' ').trim(), value });
  }

  return pairs.length ? pairs : null;
}

/** 去掉日期前缀、把长名字压短：「中国共产党第二十届…第五次全体会议」→「五中全会」 */
function compressEventName(text) {
  let s = String(text || '').trim();
  if (!s || s.indexOf('窗口内暂无') >= 0) return '';
  s = s
    .replace(/^\d{1,2}-\d{1,2}\s*/, '')
    .replace(/中国共产党/g, '')
    // 「二十届中央委员会第五次全体会议」→「五中全会」：屏上只留最认得出的部分
    .replace(/第([一二三四五六七八九十\d]+)届中央委员会第([一二三四五六七八九十\d]+)次全体会议/g, '$2中全会')
    .replace(/议息会议/g, '议息')
    .replace(/全体会议/g, '全会')
    // 经济读数常带一串口径：「GDP:不变价:环比折年率:季调」→「GDP」，
    // 「ADP就业人数(万人)」→「ADP就业人数」
    .replace(/[:：（(].*$/, '')
    .replace(/\s+/g, ' ')
    .trim();
  // 截断时别把行尾留成半个括号 / 冒号
  return s.length > 12 ? s.slice(0, 11).replace(/[\s([（:：,，、·]+$/, '') + '…' : s;
}

/** 「10-26 中国共产党第二十届中央委员会第五次全体会议」→「10-26 五中全会」 */
function shortEventName(text) {
  const md = /^(\d{1,2}-\d{1,2})/.exec(String(text || '').trim());
  const name = compressEventName(text);
  if (!name) return '';
  return md ? md[1] + ' ' + name : name;
}

/**
 * 事件重要度打分：屏幕只放得下 1～2 个，要挑真正影响市场的。
 * 纯关键词加权，只用于「排序」，不改变原文事实。
 */
function eventWeight(text) {
  const t = String(text);
  let w = 0;
  if (/美联储/.test(t)) w += 5;
  if (/中央委员会|中全会/.test(t)) w += 5;
  if (/政治局|国务院/.test(t)) w += 4;
  if (/央行|议息/.test(t)) w += 3;
  if (/非农|CPI|PCE|GDP/.test(t)) w += 2;
  if (/发布会|论坛|博览会|展/.test(t)) w -= 2;
  return w;
}

/**
 * 把「窗口摘要」压成屏幕上读得完的一句话：
 * 窗口天数 · 时间点总数 · 最密集日 · 重点会议（前 2 个）。
 * 只做压缩，不加新数字——每个数字都来自日报原文。
 */
function windowSegments(pairs) {
  const f = {};
  (pairs || []).forEach(p => { if (!(p.label in f)) f[p.label] = p.value; });

  const seg = { span: '', total: '', star: '', dense: '', meets: [] };

  // 时间窗口：2026-10-23 ~ 2026-11-22 · 未来 30 天 → 10-23~11-22 · 30 天
  const span = f['时间窗口'] || '';
  const sm = /(\d{4})-(\d{2})-(\d{2})\s*[~～]\s*(\d{4})-(\d{2})-(\d{2})/.exec(span);
  const days = /未来\s*(\d+)\s*天/.exec(span);
  if (sm) seg.span = sm[2] + '-' + sm[3] + '~' + sm[5] + '-' + sm[6] + (days ? ' · ' + days[1] + ' 天' : '');
  else if (span) seg.span = clampText(span, 24);

  // 时间点总数：60 个 · ★★★ 59 / ★★ 1
  const total = f['时间点合计'] || '';
  const tm = /^(\d+)\s*个/.exec(total);
  const star = /★★★\s*(\d+)/.exec(total);
  if (tm) {
    seg.total = tm[1] + ' 个时间点';
    if (star) seg.star = '★★★' + star[1];
  }

  // 最密集日：10-29 周四（8 项 · T+6）→ 最密 10-29（8 项）
  const dense = f['最密集日'] || '';
  const dm = /^(\d{1,2}-\d{1,2})[^（(]*[（(]\s*(\d+)\s*项/.exec(dense);
  if (dm) seg.dense = '最密 ' + dm[1] + '（' + dm[2] + ' 项）';
  else if (dense) seg.dense = '最密 ' + String(dense).split(/[（(]/)[0].trim();

  // 重点会议：原文按日期排，「前 2 个」很可能是不重要的那两场；
  // 这里按关键词挑重头戏，再按日期排回去。
  const meet = f['央行议息 / 重要会议'] || f['央行议息/重要会议'] || '';
  seg.meets = String(meet)
    .replace(/等\s*\d+\s*项\s*$/, '')
    .split('、')
    .map(t => t.trim())
    .filter(Boolean)
    .map((raw, i) => ({ i, score: eventWeight(raw), name: shortEventName(raw) }))
    .filter(x => x.name)
    .sort((a, b) => (b.score - a.score) || (a.i - b.i))
    .slice(0, 2)
    .sort((a, b) => a.i - b.i)
    .map(x => x.name);

  return seg;
}

/** 带日期的三类条目：会议 / 中国读数 / 美国读数 */
const DATED_WINDOW_LABELS = [
  '央行议息 / 重要会议', '央行议息/重要会议', '中国关键读数', '美国关键读数',
];

/** 把条目串拆开：「10-26 五中全会、10-28 美联储议息 等 9 项」→ [{md,name,score}] */
function windowDatedItems(pairs) {
  const out = [];
  (pairs || []).forEach(p => {
    if (DATED_WINDOW_LABELS.indexOf(p.label) < 0) return;
    String(p.value || '')
      .replace(/等\s*\d+\s*项\s*$/, '')
      .split(/[、，]/)
      .forEach(raw => {
        const t = raw.trim();
        const md = /^(\d{1,2}-\d{1,2})(?!\d)/.exec(t);
        const name = compressEventName(t);
        if (!md || !name) return;
        out.push({ md: md[1], name, score: eventWeight(t) });
      });
  });
  return out;
}

/** 「2026-10-23」之后 5 个工作日（跳过周末）；日期不合法返回 null */
function nextFiveWeekdays(dateISO) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateISO || ''));
  if (!m) return null;
  const p2 = v => String(v).padStart(2, '0');
  const out = [];
  for (let i = 1; i <= 30 && out.length < 5; i++) {
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]) + i * 86400000);
    const dow = d.getUTCDay();
    if (dow === 0 || dow === 6) continue;
    out.push(p2(d.getUTCMonth() + 1) + '-' + p2(d.getUTCDate()));
  }
  return out.length === 5 ? out : null;
}

/**
 * 「只显示未来五天」的那一版窗口摘要。
 *
 * 日报的窗口摘要表按 30 天铺开，屏上只读得完最近这周。这里把会议 / 中美国关键读数
 * 里带 MM-DD 的条目按「报告日之后的 5 个工作日」筛一遍，一天只留最重要的一条，
 * 日期都来自日报原文，不自己推算节假日。
 *
 * 五天内一条都没有（淡季）时返回 []，由调用方退回 30 天口径。
 */
function composeFiveDayVariants(pairs, dateISO) {
  const days = nextFiveWeekdays(dateISO);
  if (!days) return [];
  const keep = new Set(days);

  const picked = [];
  const at = {};
  windowDatedItems(pairs).forEach(it => {
    if (!keep.has(it.md)) return;
    if (!(it.md in at)) { at[it.md] = picked.length; picked.push(it); return; }
    if (it.score > picked[at[it.md]].score) picked[at[it.md]] = it;   // 一天只留一条
  });
  if (!picked.length) return [];

  picked.sort((a, b) => days.indexOf(a.md) - days.indexOf(b.md));
  let show = picked;
  if (show.length > 3) {
    // 五天都有事时屏幕上放不下，按重要度留 3 天，再排回日期顺序
    const keepIdx = show.map((it, i) => ({ score: it.score, i }))
      .sort((a, b) => (b.score - a.score) || (a.i - b.i))
      .slice(0, 3)
      .map(x => x.i)
      .sort((a, b) => a - b);
    show = keepIdx.map(i => show[i]);
  }

  const span = days[0] + '~' + days[days.length - 1];
  const label = it => it.md + ' ' + it.name;
  const out = [];
  [
    [span].concat(show.map(label)),
    [span].concat(show.slice(0, 2).map(label)),
    show.map(label),
    show.slice(0, 2).map(label),
    show.slice(0, 1).map(label),
  ].forEach(list => {
    const text = list.filter(Boolean).join(' · ');
    if (text && out.indexOf(text) < 0) out.push(text);
  });
  return out;
}

/**
 * 按「信息量从多到少」列出窗口摘要的几档文案。
 * 小屏（296×128 / 250×122）正文只有一行位置，排版层会退到能放下的那一档，
 * 而不是把句子腰斩——所以这里给的是整句候选，不是字符串前缀。
 */
function composeWindowVariants(pairs) {
  const g = windowSegments(pairs);
  const pick = keys => keys.map(k => g[k]).filter(Boolean);

  const full = pick(['span', 'total', 'star', 'dense']).concat(g.meets);
  const mid = pick(['span', 'total', 'dense']).concat(g.meets.slice(0, 1));
  // 再短一档：丢掉时间窗口，但保住「最密集日 + 重头戏」——这两项才是要看日历的理由
  const brief = pick(['dense']).concat(g.meets);
  const short = pick(['total', 'dense']);
  const tiny = pick(['dense']);

  const out = [];
  [full, mid, brief, short, tiny].forEach(list => {
    const text = list.join(' · ');
    if (text && out.indexOf(text) < 0) out.push(text);
  });
  return out;
}

/** 单档文案（日志 / 测试用） */
function composeWindowSummary(pairs) {
  const v = composeWindowVariants(pairs);
  return v.length ? v[0] : '';
}

/**
 * 把「总结」字段整理成屏幕卡片上要显示的若干行。
 * 屏幕很小，只保留 3 条最有信息量的内容：
 * 今日盘点 / 明日关注 / 窗口摘要（时间节点栏目的全窗口口径）。
 */
function buildCard(summary) {
  // extractSummary 对「总结」格式之前的旧日报会返回 null —— 给一张空卡片，
  // 让上层照常画页头页脚，而不是抛 TypeError
  if (!summary) return { date: '', lines: [] };

  const f = summary.fields || {};
  const lines = [];

  if (f['今日盘点']) lines.push({ label: '今日盘点', text: clampText(f['今日盘点'], 60) });
  if (f['明日关注']) lines.push({ label: '明日关注', text: clampText(f['明日关注'], 60) });

  // 窗口摘要取代原来的「风险关注」「AI 结论」：那两条又长又常被截，
  // 屏上读不完；窗口摘要只报未来五天，一周的日程刚好读得完。
  if (summary.window && summary.window.length) {
    // 只报「未来五天」：屏上读得完的也就这一周，30 天口径留给淡季兜底。
    // 每档都是整句候选：小屏（296×128 / 250×122）只有一行位置，
    // 就让排版层自动退到能放下的那一档，而不是把句子腰斩
    const five = composeFiveDayVariants(summary.window, summary.date);
    const variants = five.length ? five : composeWindowVariants(summary.window);
    if (variants.length) {
      lines.push({
        label: '窗口摘要',
        text: clampText(variants[0], 130),
        alts: variants.map(t => clampText(t, 130)),
      });
    }
  } else if (f['风险关注'] || f['本栏结论']) {
    // 老日报没有「窗口摘要」表时，退回原来的两条
    if (f['风险关注']) lines.push({ label: '风险关注', text: clampText(f['风险关注'], 78) });
    if (f['本栏结论']) lines.push({ label: 'AI 结论', text: firstSentence(f['本栏结论'], 96) });
  }

  // 兜底：字段命名有变化时，取任意前两条非空内容
  if (lines.length === 0) {
    Object.keys(f).slice(0, 2).forEach(k => {
      lines.push({ label: clampText(k, 10), text: clampText(f[k], 80) });
    });
  }

  return { date: summary.date, lines: lines };
}

// ---------- 渲染 ----------

/**
 * 墨水位图清不清楚，取决于四件事：最终像素字号、字重、字体的中文字形、
 * 以及二值化阈值（见后面的 SUM_INK_THRESHOLD）。
 *
 * 旧版的问题是：正文只有 11px、常规字重、字体栈只写了 sans-serif，
 * 在不少系统上会落到很细的中文黑体（思源黑体 Light / 系统细体），
 * 1bpp 二值化后笔画直接断成虚线——这就是「字体不清」的根因。
 *
 * 现在的策略：
 *   ① 字体栈显式列出各平台的中文字体（PingFang / 微软雅黑 / Noto Sans SC …）；
 *   ② 13px 起加粗，低于 13px 自动回到常规字重（小字加粗会把字腔糊死）；
 *   ③ 字号优先「一个字都不截就能排下的最大号」，档位可再上下偏移；
 *   ④ 阈值从 140 抬到 176，避免抗锯齿的灰边把笔画糊成实心块。
 */
const SUM_FONTS = {
  hei: {
    label: '黑体加粗',
    body: "'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Noto Sans SC','Source Han Sans SC','Noto Sans CJK SC','Heiti SC','WenQuanYi Micro Hei',sans-serif",
    boldBody: true,
  },
  song: {
    label: '宋体加粗',
    body: "'Songti SC','Noto Serif SC','Source Han Serif SC','Noto Serif CJK SC','SimSun',serif",
    boldBody: true,
  },
  kai: {
    label: '楷体加粗',
    body: "'Kaiti SC','KaiTi','STKaiti','Noto Serif SC','Noto Serif CJK SC',serif",
    boldBody: true,
  },
  mono: {
    label: '等宽点阵',
    body: "'Sarasa Mono SC','Noto Sans Mono CJK SC','Noto Sans SC','PingFang SC','Microsoft YaHei',ui-monospace,monospace",
    boldBody: true,
  },
};

/**
 * 字号档位。rate 决定基准字号，weight 决定「字大」和「字多」的取舍：
 * weight 越大越偏向把话写全，越小越偏向把字写大。
 */
const SUM_SIZE_SCALE = {
  small: { label: '小', offset: -1 },
  normal: { label: '标准', offset: 0 },
  large: { label: '大', offset: 2 },
};

/** 字号硬下限：再小在 4.2 寸屏上就只剩黑糊糊一片了 */
const SUM_MIN_FONT = 10;

/** 加粗的最小字号：再小加粗会把笔画粘成一团 */
const SUM_BOLD_MIN = 13;

function sumDisplayOptions() {
  const fontEl = document.getElementById('sum-font');
  const fontKey = (fontEl && fontEl.value) || 'hei';
  const font = SUM_FONTS[fontKey] || SUM_FONTS.hei;

  const scaleEl = document.getElementById('sum-size-scale');
  const scaleKey = (scaleEl && scaleEl.value) || 'normal';
  const scaleDef = SUM_SIZE_SCALE[scaleKey] || SUM_SIZE_SCALE.normal;

  return {
    fontKey: SUM_FONTS[fontKey] ? fontKey : 'hei',
    fontFamily: font.body,
    boldBody: font.boldBody !== false,
    fontLabel: font.label,
    scaleLabel: scaleDef.label,
    offset: scaleDef.offset || 0,
  };
}

// ---------- 偏好记忆（刷新页面后仍用上次挑好的字号 / 字体） ----------

const SUM_PREF_KEYS = ['sum-size', 'sum-font', 'sum-size-scale', 'sum-zoom'];

function saveSumPrefs() {
  try {
    SUM_PREF_KEYS.forEach(k => {
      const el = document.getElementById(k);
      if (el && el.value != null) localStorage.setItem('epd.' + k, el.value);
    });
  } catch (_) { /* 隐私模式下 localStorage 可能不可用，忽略 */ }
}

function restoreSumPrefs() {
  try {
    SUM_PREF_KEYS.forEach(k => {
      const v = localStorage.getItem('epd.' + k);
      const el = document.getElementById(k);
      if (!v || !el) return;
      // 只接受下拉里确实存在的值，避免旧版本残留值把控件清空
      if (Array.from(el.options || []).some(o => o.value === v)) el.value = v;
    });
  } catch (_) { /* 同上 */ }
}

function bindSumPrefs() {
  restoreSumPrefs();
  SUM_PREF_KEYS.forEach(k => {
    const el = document.getElementById(k);
    if (el && el.addEventListener) el.addEventListener('change', saveSumPrefs);
  });
}

function cardFont(size, opts, bold) {
  const fam = (opts && opts.fontFamily) || SUM_FONTS.hei.body;
  return (bold ? 'bold ' : '') + size + 'px ' + fam;
}

/**
 * 某个字号下该不该加粗：小字号加粗会把「日/目/口/赢」这类字腔糊成实心块，
 * 所以 13px 以下自动回到常规字重。
 */
function sumBold(size, opts) {
  const want = !(opts && opts.boldBody === false);
  return want && size >= SUM_BOLD_MIN;
}

// 避头尾：这些标点不放在行首 / 行尾，否则整行看着就像断错了
const NO_LINE_START = '，。、；：？！）】》」』〕”’…—·｜,.!?;:)]}>%';
const NO_LINE_END = '（【《「『〔“‘([{<';

function isAsciiWordChar(ch) {
  return /[0-9A-Za-z._+\-\/%]/.test(ch);
}

/** 这些位置断行读起来最舒服（分隔符后面断，语义完整） */
// 注意不要把普通空格算进来：否则「· 60 个」会在「60」后面断开，
// 把数字和量词劈成两行。
const SOFT_BREAK_AFTER = '·｜|、，,；;/）)';

/** 「 · 」是摘要字段之间的分隔符：按它整段换行，语义最完整 */
const SEGMENT_SPLIT = /\s+·\s+/;

/**
 * 逐字折行（单个「段」内部用）：中文逐字断，英文/数字整段不拆，
 * 标点避头尾，并尽量在「｜、，」后面断。
 */
function wrapChars(ctx, text, maxWidth) {
  const chars = Array.from(String(text));
  const fits = s => ctx.measureText(s).width <= maxWidth;
  const out = [];
  let line = '';

  const softBreak = cur => {
    // 从行尾往回找「·｜、，」这类分隔符：断了语义完整。
    // 只在不浪费行宽时才退（断点前至少填满 62% 行宽），否则宁可逐字断。
    const minHead = maxWidth * 0.62;
    for (let i = cur.length - 1; i >= 2; i--) {
      if (SOFT_BREAK_AFTER.indexOf(cur[i]) < 0) continue;
      let cut = i + 1;
      while (cut < cur.length && cur[cut] === ' ') cut++;   // 连后面的空格一起带走
      const head = cur.slice(0, i + 1).trimEnd();
      if (head.length < 2) continue;
      if (ctx.measureText(head).width < minHead) continue;
      return cut;
    }
    return 0;
  };

  for (const ch of chars) {
    if (line === '' || fits(line + ch)) {
      line += ch;
      continue;
    }

    // 先看能不能退到一个「·｜、，」之后断，退得动就退
    const cut = softBreak(line);
    if (cut && fits(line.slice(cut))) {
      out.push(line.slice(0, cut).replace(/\s+$/, ''));
      line = line.slice(cut).replace(/^\s+/, '') + ch;
      continue;
    }

    let carry = '';
    if (NO_LINE_START.indexOf(ch) >= 0 && line.length > 1) {
      // 标点不能起头：把上一个字一起带到下一行
      carry = line.slice(-1);
      line = line.slice(0, -1);
    } else if (NO_LINE_END.indexOf(line.slice(-1)) >= 0 && line.length > 1) {
      // 左括号不能收尾：也挪到下一行
      carry = line.slice(-1);
      line = line.slice(0, -1);
    } else if (isAsciiWordChar(ch) && isAsciiWordChar(line.slice(-1))) {
      // 英文/数字整段不拆（StockTwits、40/100、2026-10-23）
      const m = /[0-9A-Za-z._+\-\/%]+$/.exec(line);
      if (m && m[0].length < line.length && fits(m[0] + ch)) {
        carry = m[0];
        line = line.slice(0, line.length - m[0].length);
      }
    }

    // 「60 个时间点」「30 天」「8 项」：数字和后面的量词不能分家，
    // 否则会读成「60 个时间 / 点」。把行尾那串数字一起带回下一行。
    if (carry === '' && /[\u4e00-\u9fa5]/.test(ch)) {
      const numAt = /(^|[\s·｜、，（(])(\d[\d.,]*)\s*$/.exec(line);
      if (numAt) {
        // 用匹配位置切，不能用长度切：捕获组后面还跟着空格
        const at = numAt.index + numAt[1].length;
        if (at > 0 && at < line.length) {
          carry = line.slice(at);
          line = line.slice(0, at).replace(/\s+$/, '');
        }
      }
    }

    if (line === '' && carry === '') {
      out.push(ch);          // 单字就超宽（极端窄屏），只能硬断
      continue;
    }
    out.push(line);
    line = carry + ch;
  }
  if (line !== '') out.push(line);
  return out;
}

/**
 * 折行入口：先按「 · 」把摘要字段切成整段，能整段放下就整段放，
 * 放不下才在段内逐字断。这样「20 天窗口 · 60 个时间点 · 最密 10-29」
 * 不会出现「二十届 / 五中全会」这种把专名劈开的断法。
 *
 * 断行发生在「 · 」处时，把分隔符一起丢掉——否则行尾会挂着个孤零零的
 * 「·」，屏上看着像漏字。
 */
function wrapLines(ctx, text, maxWidth) {
  const raw = String(text);
  const parts = raw.split(SEGMENT_SPLIT);        // 用「空格·空格」切开
  if (parts.length < 2) return wrapChars(ctx, raw, maxWidth);

  const fits = s => ctx.measureText(s).width <= maxWidth;
  const out = [];
  let line = '';

  parts.forEach((part) => {
    // 段与段之间统一用「 · 」相接（不能因为「最后一段」就省掉分隔符，
    // 否则两场会议会被粘成一句「…五中全会 10-28 美联储议息」）
    const glued = (line ? ' · ' : '') + part;

    if (fits(line + glued)) {
      line += glued;
      return;
    }

    if (line) {
      // 分隔符「 · 」留在上一行末尾：这样 join 能无损拼回原文
      // （截断重建依赖这个），真正绘制时再用 stripBreakSep 剥掉
      out.push(line + ' ·');
      line = '';
    }

    // 整段自己就超宽 → 段内逐字断
    if (!fits(part)) {
      const sub = wrapChars(ctx, part, maxWidth);
      sub.forEach((ln, k) => {
        if (k < sub.length - 1) out.push(ln);
        else line = ln;
      });
      return;
    }
    line = part;
  });

  if (line) out.push(line);
  return out;
}

/** 去掉折行时留在行尾的分隔符（只影响显示，不影响文本还原） */
function stripBreakSep(line) {
  return String(line).replace(/\s*·\s*$/, '');
}

/**
 * 把卡片画到 canvas 上，返回最终排版结果（字号、条数、是否截短），
 * 供预览状态栏与推送日志复用。纯 canvas 2D API，便于在 Node 中用桩测试。
 *
 * opts: { fontFamily, fontLabel, boldBody, offset }
 */
function drawCard(ctx, card, w, h, opts) {
  opts = opts || {};
  const offset = opts.offset || 0;

  const pad = Math.max(6, Math.round(Math.min(w, h) * 0.045));
  const maxWidth = w - pad * 2;

  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, w, h);
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.fillStyle = '#000000';

  // 标题 / 日期 / 页脚不跟随「字号」档位缩放：档位只调正文，
  // 否则「小」档把页头也缩小，反而多挤出空间，比「标准」档还大，档位就乱了
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const titleSize = clamp(Math.round(h * 0.060), 10, 26);
  const headSize = clamp(Math.round(h * 0.038), 9, 15);
  const footSize = clamp(Math.round(h * 0.036), 8, 12);

  const headTop = pad + titleSize + Math.round(pad * 0.4);
  const ruleY = headTop + headSize + Math.round(pad * 0.55);
  const ruleH = Math.max(2, Math.round(h * 0.007));
  const areaTop = ruleY + ruleH + Math.round(pad * 0.55);
  const areaBottom = h - pad - footSize - Math.round(pad * 0.3);
  const areaHeight = areaBottom - areaTop;

  // 400×300 → 基准 16px（旧版是 11px，1bpp 后基本看不清），最大 24px；
  // 上限还要受正文可用高度约束，否则 296×128 这种矮屏会一路缩到 10px
  const baseBody = clamp(Math.round(h * 0.052), SUM_MIN_FONT, 22);
  const maxBody = clamp(Math.min(baseBody + 7, Math.floor(areaHeight / 5.2)), baseBody, 24);

  const boldAt = size => sumBold(size, opts);

  // alts：该条的「逐级更短候选」，小屏放不下时整句换短，而不是腰斩
  const source = card.lines.map(l => ({ label: l.label, text: l.text, alts: l.alts || [] }));
  const squeezeText = t => String(t).replace(/\s+/g, '');

  const totalChars = source.reduce((a, it) => a + squeezeText(it.text).length, 0) || 1;

  /** 某个字号下，每条内容按可用宽度折行后的行数 */
  const measureBlock = (item, bodySize, labelSize) => {
    ctx.font = cardFont(labelSize, opts, boldAt(labelSize));
    let labelW = ctx.measureText(item.label).width + Math.round(labelSize * 0.6);
    if (labelW > maxWidth * 0.34) labelW = maxWidth * 0.34;
    const bodyW = maxWidth - labelW;
    ctx.font = cardFont(bodySize, opts, boldAt(bodySize));
    const wrapped = wrapLines(ctx, item.text, Math.max(bodyW, maxWidth * 0.4));
    return { labelW, wrapped, bodyW };
  };

  /**
   * 按「行预算」排版：先给每条留 1 行，剩余行数按缺口比例轮流加给最需要的条目，
   * 被截断的地方落在整行末尾（不是一刀切在字数上），页脚不会留半屏白。
   */
  const planLayout = (bodySize, maxLinesPerItem, tighten) => {
    const count = source.length;
    // 标签与正文同号（同号更好认），但标签列最多占 34% 宽度；小屏才降一号
    const labelSize = Math.max(9, Math.min(bodySize, 17));
    const lhFactor = tighten ? 1.36 : 1.5;
    const gapFactor = tighten ? 0.24 : 0.4;
    const lineH = Math.round(bodySize * lhFactor);
    const blockGap = Math.max(2, Math.round(bodySize * gapFactor));
    const capacity = Math.floor((areaHeight - blockGap * (count - 1)) / lineH);
    if (capacity < count) return null;

    const measured = source.map(it => measureBlock(it, bodySize, labelSize));
    // 需求行数一律按「完整文案」算：候选短句只在真的要截断时才启用，
    // 不能因为「有更短的版本」就提前少给行，否则大屏也只剩半句话
    const needs = measured.map(m => m.wrapped.length);
    // 每条上限：既不超过自身需要，也不吃掉超过 65% 的可视高度（免得一条把别的挤没）
    const perItemCap = Math.max(1, Math.round(capacity * 0.65));
    const capped = needs.map(v => Math.min(v, maxLinesPerItem || perItemCap, perItemCap));

    // 行数分配：先各 1 行，再按「还缺多少」从多到少补
    const alloc = needs.map(() => 1);
    let left = capacity - count;
    while (left > 0) {
      let pick = -1;
      for (let i = 0; i < count; i++) {
        if (alloc[i] >= capped[i]) continue;
        if (pick < 0 || (capped[i] - alloc[i]) > (capped[pick] - alloc[pick])) pick = i;
      }
      if (pick < 0) break;
      alloc[pick]++;
      left--;
    }

    const blocks = [];
    for (let i = 0; i < count; i++) {
      const m = measured[i];
      // 折行时「 · 」会留在上一行末尾（绘制前剥掉），所以直接拼回去
      // 就是原文前缀——不能 join('')，会把「30 天」和「60 个时间点」粘住。
      const full = source[i].text;
      let lines = m.wrapped.slice(0, alloc[i]);
      let text = lines.join(' ');
      let cut = squeezeText(text).length < squeezeText(full).length;

      // 该条配了「逐级更短」的候选（窗口摘要）：优先整句换短的，
      // 别把「10-26 五中全会」这种句尾关键信息腰斩掉
      const alts = source[i].alts || [];
      if (cut && alts.length) {
        for (const alt of alts) {
          const wrappedAlt = wrapLines(ctx, alt, m.bodyW);
          if (wrappedAlt.length <= alloc[i]) {
            lines = wrappedAlt;
            text = alt;
            cut = false;
            break;
          }
        }
      }

      if (cut) {
        // 行尾常是「话说一半」的逗号/顿号，去掉再补省略号，读起来才不像断线
        text = text.replace(/[，、；：,;:·|｜\s]+$/, '') + '…';
        lines = wrapLines(ctx, text, m.bodyW);
        if (lines.length > alloc[i]) {
          lines = lines.slice(0, alloc[i]);
          const last = lines[lines.length - 1];
          lines[lines.length - 1] = last.slice(0, Math.max(1, last.length - 1)) + '…';
        }
        text = lines.join(' ');
      }
      blocks.push({
        item: { label: source[i].label, text: text },
        labelW: m.labelW, labelSize, wrapped: lines, lineH, blockH: lines.length * lineH,
      });
    }
    return { blocks, bodySize, lineH, blockGap, totalH: blocks.reduce((a, b) => a + b.blockH, 0) + blockGap * (count - 1) };
  };

  /**
   * 字号选择分两步：
   *   ① 找出「一个字都不截、一条都不少」能排下的最大字号（nativeSize）；
   *      找不到就退而求其次，取保留内容最多的那一档。
   *   ② 再按「字号」档位偏移（小 −1 / 标准 0 / 大 +2），
   *      偏移后的字号允许截短正文——所以「大」档是拿内容换字号，看得见。
   * 这样档位之间是单调的，不会出现「小」档反而比「标准」档字更大的怪事。
   */
  const nativeSize = () => {
    let fallback = null;
    for (let size = maxBody; size >= SUM_MIN_FONT; size--) {
      const got = planLayout(size, 0, false) || planLayout(size, 0, true);
      if (!got) continue;
      const keptChars = got.blocks.reduce((a, b) => a + b.item.text.replace(/…$/, '').length, 0);
      got.keptRatio = keptChars / totalChars;
      if (got.keptRatio >= 0.999 && got.blocks.length === source.length) return size;
      if (!fallback || got.keptRatio > fallback.ratio) fallback = { size, ratio: got.keptRatio };
    }
    return fallback ? fallback.size : SUM_MIN_FONT;
  };

  // 小屏（2.9 寸 / 2.13 寸）上没有「全放下」的字号，此时按屏幕高度给一个
  // 最低可读字号，宁可每条只留一行，也不要把整张卡缩成 10px 糊成一片
  const minReadable = clamp(Math.round(h * 0.09), SUM_MIN_FONT, 14);
  const target = clamp(nativeSize() + offset, minReadable, maxBody);
  let layout = planLayout(target, 0, false) || planLayout(target, 0, true);
  for (let size = target - 1; size >= SUM_MIN_FONT && !layout; size--) {
    layout = planLayout(size, 0, false) || planLayout(size, 0, true);
  }

  // 连最小字号都排不下 → 删末条（保留靠前的结论）
  while (!layout && source.length > 1) {
    source.pop();
    layout = planLayout(SUM_MIN_FONT, 0, true) || planLayout(SUM_MIN_FONT, 0, false);
  }

  if (!layout) {
    // 极端情况：最小字号硬排一条，超出部分裁切
    const bodySize = SUM_MIN_FONT;
    const lineH = Math.round(bodySize * 1.5);
    const first = source[0] || { label: '', text: '' };
    ctx.font = cardFont(bodySize, opts, boldAt(bodySize));
    const wrapped = wrapLines(ctx, first.text, maxWidth);
    layout = {
      blocks: [{ item: first, labelW: 0, labelSize: bodySize, wrapped, lineH, blockH: wrapped.length * lineH }],
      bodySize, lineH, blockGap: 0, totalH: wrapped.length * lineH,
    };
  }

  // 标题
  ctx.font = cardFont(titleSize, opts, boldAt(titleSize));
  ctx.fillText('章鱼 AI · 上水日报', pad, pad);

  // 日期 / 栏目名
  ctx.font = cardFont(headSize, opts, false);
  ctx.fillText(card.date || '', pad, headTop);
  const sectionText = '总结';
  ctx.fillText(sectionText, w - pad - ctx.measureText(sectionText).width, headTop);

  // 分隔线（2px 起步：1px 在部分墨水屏上会整条消失）
  ctx.fillRect(pad, ruleY, maxWidth, ruleH);

  // 正文：标签加粗 + 内容常规，标签列对齐，行首齐左
  let y = areaTop;
  layout.blocks.forEach((b, i) => {
    if (i > 0) y += layout.blockGap;
    ctx.font = cardFont(b.labelSize, opts, boldAt(b.labelSize));
    ctx.fillText(b.item.label, pad, y + Math.round((layout.bodySize - b.labelSize) * 0.1));
    ctx.font = cardFont(layout.bodySize, opts, boldAt(layout.bodySize));
    let ty = y;
    for (const ln of b.wrapped) {
      ctx.fillText(stripBreakSep(ln), pad + b.labelW, ty);
      ty += b.lineH;
    }
    y += b.blockH;
  });

  // 页脚
  ctx.font = cardFont(footSize, opts, false);
  const foot = '仅供参考，不构成投资建议';
  ctx.fillText(foot, w - pad - ctx.measureText(foot).width, h - pad - footSize);

  // 记录保留率 / 被截短 / 被省略的条数，方便预览时核对
  layout.keptRatio = layout.blocks.reduce((a, b) => a + squeezeText(b.item.text.replace(/…$/, '')).length, 0) / totalChars;
  layout.trimmed = layout.blocks.filter((b, i) => !card.lines[i] || b.item.text !== card.lines[i].text).length;
  layout.kept = layout.blocks.length;
  layout.total = card.lines.length;
  layout.offset = offset;
  return layout;
}

/**
 * canvas → 墨水屏两个位平面（1bpp，8 像素/字节，MSB first）
 * 黑白层：1 = 白，0 = 黑
 * 红色层：0 = 红，1 = 非红
 *
 * 阈值 140 偏低：抗锯齿后的灰色笔画边会被判成黑，小字糊成一坨。
 * 抬到 176 让笔画保持一点「瘦身」，字腔（如「日」「目」「口」）才不会被填死。
 */
const SUM_INK_THRESHOLD = 176;

function canvasToLayers(ctx, w, h) {
  const img = ctx.getImageData(0, 0, w, h).data;
  const byteWidth = Math.ceil(w / 8);
  // 先全部置 1：黑白层 = 白，红色层 = 非红。
  // 宽度不是 8 的倍数时，行尾补齐位必须保持「白 / 非红」，
  // 否则这些位会被固件当成黑点或红点。
  const bw = new Uint8Array(byteWidth * h).fill(0xFF);
  const red = new Uint8Array(byteWidth * h).fill(0xFF);

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const r = img[i], g = img[i + 1], b = img[i + 2];
      const gray = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
      const byteIdx = y * byteWidth + (x >> 3);
      const bit = 1 << (7 - (x & 7));

      if (gray >= SUM_INK_THRESHOLD) bw[byteIdx] |= bit;
      else bw[byteIdx] &= ~bit;

      const isRed = r > 160 && r > g && r > b;
      if (isRed) red[byteIdx] &= ~bit;
      else red[byteIdx] |= bit;
    }
  }
  return { bw, red, byteWidth };
}

// ---------- 蓝牙下发 ----------

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function sumStatus(text) {
  const el = document.getElementById('sum-status');
  if (el) el.textContent = text;
}

function getSumSize() {
  const sel = document.getElementById('sum-size');
  const v = sel ? sel.value : '400x300';
  const parts = v.split('x');
  return { w: parseInt(parts[0], 10), h: parseInt(parts[1], 10) };
}

/**
 * 按当前屏幕尺寸与「旋转 180°」设置把任意绘制函数画到画布上。
 * drawFn(ctx, w, h) 的返回值会原样带回，方便调用方拿到排版信息。
 */
function renderToSumCanvas(canvas, drawFn) {
  const size = getSumSize();
  canvas.width = size.w;
  canvas.height = size.h;
  const ctx = canvas.getContext('2d');

  const rotEl = document.getElementById('sum-rotate');
  const flip = !!(rotEl && rotEl.checked);

  let result;
  if (!flip) {
    result = drawFn(ctx, size.w, size.h);
  } else {
    // 旋转 180°：先画到临时画布，再倒着贴回来
    const tmp = document.createElement('canvas');
    tmp.width = size.w;
    tmp.height = size.h;
    result = drawFn(tmp.getContext('2d'), size.w, size.h);
    ctx.save();
    ctx.translate(size.w, size.h);
    ctx.rotate(Math.PI);
    ctx.drawImage(tmp, 0, 0);
    ctx.restore();
  }
  return { ctx, layout: result, size, opts: sumDisplayOptions() };
}

function renderSummaryToCanvas(canvas, card) {
  const opts = sumDisplayOptions();
  return renderToSumCanvas(canvas, (ctx, w, h) => drawCard(ctx, card, w, h, opts));
}

/** 预览显示倍数：只放大网页上的预览，不改变下发的位图分辨率 */
function applySumZoom(canvas) {
  const el = document.getElementById('sum-zoom');
  const zoom = el ? (parseFloat(el.value) || 1) : 1;
  const size = getSumSize();
  canvas.style.maxWidth = Math.round(size.w * zoom) + 'px';
  return canvas;
}

/** 预览状态栏的一句话说明：字号 / 字体 / 条数 / 是否截短 */
function describeLayout(layout, opts) {
  if (!layout) return '';
  let s = layout.bodySize + 'px ' + (opts ? opts.fontLabel : '') + ' · ' + layout.kept + ' 条';
  if (layout.trimmed > 0) s += ' · ' + layout.trimmed + ' 条已截短';
  if (layout.kept < layout.total) s += ' · 省略 ' + (layout.total - layout.kept) + ' 条';
  return s;
}

async function loadSummaryCard() {
  const resp = await fetch(SUMMARY_PATH + '?t=' + Date.now(), { cache: 'no-store' });
  if (!resp.ok) throw new Error('读取日报失败 HTTP ' + resp.status);
  const html = await resp.text();
  const summary = extractSummary(html);
  if (!summary) throw new Error('未在日报中找到「总结」栏目');
  return buildCard(summary);
}

// 缓存最近一次抓到的卡片：切字号 / 字体 / 预览倍数时直接重绘，不用每次都拉日报
const SUM_CACHE_MS = 5 * 60 * 1000;
let sumCardCache = null;
let sumCardFetchedAt = 0;

async function getSummaryCard(force) {
  const fresh = sumCardCache && (Date.now() - sumCardFetchedAt) < SUM_CACHE_MS;
  if (!force && fresh) return sumCardCache;
  const card = await loadSummaryCard();
  sumCardCache = card;
  sumCardFetchedAt = Date.now();
  return card;
}

/** 只拉取并预览，不下发蓝牙。force=true 时忽略缓存重新抓日报 */
async function previewSummary(force) {
  const canvas = document.getElementById('sum-preview');
  if (!sumCardCache) sumStatus('正在读取最新日报...');
  try {
    const card = await getSummaryCard(force === true);
    const { layout, opts } = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    applySumZoom(canvas);
    sumStatus('已生成预览（' + (card.date || '日期未知') + '）· ' + describeLayout(layout, opts));
    if (typeof addLog === 'function') addLog('AI 总结预览已生成：' + describeLayout(layout, opts));
    return card;
  } catch (e) {
    sumStatus('预览失败：' + e.message);
    if (typeof addLog === 'function') addLog('AI 总结预览失败：' + e.message);
    return null;
  }
}

async function sendImageLayer(data, isBw, onProgress) {
  const chunkSize = EpdLink.mtu;
  const total = Math.ceil(data.length / chunkSize);

  for (let i = 0; i < total; i++) {
    const chunk = data.subarray(i * chunkSize, Math.min((i + 1) * chunkSize, data.length));
    const flag = EpdLink.rle
      ? ((isBw ? 0x00 : 0x01) | (i === 0 ? 0x02 : 0x00))
      : ((isBw ? 0x0F : 0x00) | (i === 0 ? 0x00 : 0xF0));

    const payload = new Uint8Array(chunk.length + 2);
    payload[0] = SUM_CMD.WRITE_IMAGE;
    payload[1] = flag;
    payload.set(chunk, 2);

    const ok = await writeBytes(payload, true);
    if (!ok) return false;
    if (onProgress) onProgress(i + 1, total);
  }
  return true;
}

/**
 * 把当前画布按 EPD-nRF5 图像协议推给设备：
 * INIT → 黑白层（→ 红色层）→ REFRESH
 */
async function pushCanvas(canvas, tag) {
  const size = getSumSize();
  const useRed = !!(document.getElementById('sum-color') && document.getElementById('sum-color').checked);
  const ctx = canvas.getContext('2d');
  const { bw, red } = canvasToLayers(ctx, size.w, size.h);

  sumStatus('初始化屏幕...');
  const initOk = await writeBytes([SUM_CMD.INIT], true);
  if (!initOk) throw new Error('初始化指令下发失败');
  await sleep(300);

  if (!EpdLink.reported) {
    addLog('未收到设备 MTU 上报，按 ' + EpdLink.mtu + 'B 分块下发；若屏幕无变化，可能是该固件不支持图像推送');
  }

  sumStatus('下发黑白层...');
  const bwOk = await sendImageLayer(bw, true, (done, total) => {
    sumStatus('下发黑白层 ' + done + '/' + total);
  });
  if (!bwOk) throw new Error('黑白层下发失败');

  if (useRed) {
    sumStatus('下发红色层...');
    const redOk = await sendImageLayer(red, false, (done, total) => {
      sumStatus('下发红色层 ' + done + '/' + total);
    });
    if (!redOk) throw new Error('红色层下发失败');
  }

  sumStatus('刷新屏幕...');
  const refOk = await writeBytes([SUM_CMD.REFRESH], true);
  if (!refOk) throw new Error('刷新指令下发失败');

  addLog((tag || '画面') + '推送完成，屏幕刷新中请勿操作');
}

/** 统一处理「未连接 / 重复点击 / 按钮禁用 / 报错」的推送外壳 */
async function withPushGuard(btnId, tag, worker) {
  if (pushingSummary) return;
  if (!isDeviceConnected()) {
    sumStatus('请先连接设备');
    if (typeof addLog === 'function') addLog(tag + '：设备未连接');
    return;
  }
  const btn = document.getElementById(btnId);
  pushingSummary = true;
  if (btn) btn.disabled = 'disabled';
  try {
    await worker();
  } catch (e) {
    console.error(e);
    sumStatus('推送失败：' + e.message);
    if (typeof addLog === 'function') addLog(tag + '推送失败：' + e.message);
  } finally {
    pushingSummary = false;
    if (btn) btn.disabled = null;
  }
}

async function pushLatestSummary() {
  return withPushGuard('btn-push-summary', 'AI 总结', async () => {
    sumStatus('正在读取最新日报...');
    const card = await getSummaryCard(true);

    const canvas = document.getElementById('sum-preview');
    const { layout, size, opts } = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    applySumZoom(canvas);

    addLog('AI 总结：' + (card.date || '') + '，' + size.w + '×' + size.h + '，' +
      describeLayout(layout, opts) + '，块大小 ' + EpdLink.mtu + 'B' + (EpdLink.rle ? '（RLE 固件）' : ''));

    await pushCanvas(canvas, 'AI 总结');
    sumStatus('已推送（' + (card.date || '') + '），屏幕刷新中');
  });
}

// ---------- 字体样张（用于在真机上挑字号 / 字体） ----------

const SAMPLE_TEXT = '整体偏多 A股普涨 40/100 醒赢墨';

/** 样张要对比的字号：跟随当前「字号」档位上下浮动一档 */
function sampleSizes(base) {
  const list = [base + 3, base, base - 3, base - 5];
  return Array.from(new Set(list.filter(v => v >= SUM_MIN_FONT && v <= 26)));
}

/**
 * 画一张「字体样张」：同一句话依次用不同字号 / 字重 / 字体排出来，
 * 推到墨水屏上就能直接看出哪一档在真机最清楚。
 */
function drawFontSample(ctx, w, h, opts) {
  const pad = Math.max(6, Math.round(Math.min(w, h) * 0.045));
  const maxWidth = w - pad * 2;
  const offset = (opts && opts.offset) || 0;

  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, w, h);
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.fillStyle = '#000000';

  const titleSize = Math.max(11, Math.min(26, Math.round(h * 0.060)));
  const headSize = Math.max(8, Math.min(15, Math.round(h * 0.038)));

  ctx.font = cardFont(titleSize, opts, true);
  ctx.fillText('字体样张 · ' + w + '×' + h, pad, pad);

  const headTop = pad + titleSize + Math.round(pad * 0.4);
  ctx.font = cardFont(headSize, opts, false);
  ctx.fillText('同句对比 · 字重 / 字号 · ' + ((opts && opts.fontLabel) || '黑体'), pad, headTop);
  const right = '挑最清楚的';
  ctx.fillText(right, w - pad - ctx.measureText(right).width, headTop);

  const ruleY = headTop + headSize + Math.round(pad * 0.55);
  const ruleH = Math.max(2, Math.round(h * 0.007));
  ctx.fillRect(pad, ruleY, maxWidth, ruleH);

  const base = Math.max(SUM_MIN_FONT, Math.min(22, Math.round(h * 0.052) + offset));
  // 同一字号下一粗一常并排，才能看出「加粗糊不糊」；再补两档更小/更大的
  const rows = [
    { size: base + 3, bold: true },
    { size: base, bold: true },
    { size: base, bold: false },
    { size: base - 3, bold: true },
    { size: base - 5, bold: false },
  ]
    .map(r => ({ size: r.size, bold: r.bold && sumBold(r.size, opts) }))
    .filter(r => r.size >= SUM_MIN_FONT && r.size <= 26)
    // 小屏上字号档位会撞车（如 10px 粗/常 只剩「常」），去掉重复行
    .filter((r, i, all) => all.findIndex(o => o.size === r.size && o.bold === r.bold) === i);

  const areaTop = ruleY + ruleH + Math.round(pad * 0.55);
  const footSize = Math.max(6, Math.round(h * 0.031));
  const areaBottom = h - pad - footSize - Math.round(pad * 0.3);
  const rowH = Math.max(12, Math.floor((areaBottom - areaTop) / rows.length));
  const tagSize = Math.max(7, Math.min(11, Math.round(rowH * 0.42)));
  const tagW = Math.round(maxWidth * 0.30);

  let y = areaTop;
  for (const row of rows) {
    // 每行在分到的高度里垂直居中，行距才均匀（逐行顶对齐会全挤在上半截）
    const rowMid = y + rowH / 2;
    const textTop = Math.round(rowMid - row.size * 0.5);
    ctx.font = cardFont(tagSize, opts, false);
    ctx.fillText(row.size + 'px ' + (row.bold ? '粗' : '常'), pad,
      Math.round(rowMid - tagSize * 0.5));

    ctx.font = cardFont(row.size, opts, row.bold);
    // 超宽就截到能放下，样张只关心笔画粗细
    let text = SAMPLE_TEXT;
    let cut = false;
    while (text.length > 1 && ctx.measureText(text + (cut ? '…' : '')).width > maxWidth - tagW) {
      text = text.slice(0, -1);
      cut = true;
    }
    ctx.fillText(text + (cut ? '…' : ''), pad + tagW, textTop);
    y += rowH;
  }

  ctx.font = cardFont(footSize, opts, false);
  const foot = '在当前连接的真机上对比这一张';
  ctx.fillText(foot, w - pad - ctx.measureText(foot).width, h - pad - footSize);

  return { bodySize: base, kept: rows.length, total: rows.length, trimmed: 0 };
}

/** 生成并推送字体样张；未连接设备时只预览 */
async function pushFontSample() {
  const canvas = document.getElementById('sum-preview');
  const opts = sumDisplayOptions();
  const { layout, size } = renderToSumCanvas(canvas, (ctx, w, h) => drawFontSample(ctx, w, h, opts));
  canvas.style.display = 'block';
  applySumZoom(canvas);
  const head = '已生成字体样张（' + size.w + '×' + size.h + ' · ' + opts.fontLabel + '）';

  if (!isDeviceConnected()) {
    sumStatus(head + '，未连接设备：先看预览，连上后再点一次即可推送');
    if (typeof addLog === 'function') addLog('字体样张：设备未连接，仅生成预览');
    return;
  }

  return withPushGuard('btn-push-sample', '字体样张', async () => {
    addLog('字体样张：' + size.w + '×' + size.h + '，' + opts.fontLabel + '，字号 ' +
      sampleSizes(layout.bodySize).join(' / ') + 'px');
    await pushCanvas(canvas, '字体样张');
    sumStatus('字体样张已推送，屏幕刷新中');
  });
}

// 页面加载后恢复上次选的屏幕尺寸 / 字体 / 字号，并顺手拉一张预览
if (typeof document !== 'undefined') {
  const ready = () => {
    bindSumPrefs();
    previewSummary();
  };
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(ready, 0);
  } else if (typeof document.addEventListener === 'function') {
    document.addEventListener('DOMContentLoaded', ready);
  }
}

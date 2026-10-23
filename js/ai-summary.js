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

  return { date, fields };
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
 * 把「总结」字段整理成屏幕卡片上要显示的若干行。
 * 屏幕很小，只保留 4 条最有信息量的内容。
 */
function buildCard(summary) {
  const f = summary.fields;
  const lines = [];

  if (f['今日盘点']) lines.push({ label: '今日盘点', text: clampText(f['今日盘点'], 60) });
  if (f['明日关注']) lines.push({ label: '明日关注', text: clampText(f['明日关注'], 60) });
  if (f['风险关注']) lines.push({ label: '风险关注', text: clampText(f['风险关注'], 78) });
  if (f['本栏结论']) lines.push({ label: 'AI 结论', text: firstSentence(f['本栏结论'], 96) });

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

/**
 * 按像素宽度折行：中文逐字断行，英文/数字整段不拆，标点避头尾。
 * 以前的版本既没有避头（「×」会掉到行首），也会把 StockTwits 拦腰截断。
 */
function wrapLines(ctx, text, maxWidth) {
  const chars = Array.from(String(text));
  const fits = s => ctx.measureText(s).width <= maxWidth;
  const out = [];
  let line = '';

  for (const ch of chars) {
    if (line === '' || fits(line + ch)) {
      line += ch;
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

  const source = card.lines.map(l => ({ label: l.label, text: l.text }));
  const totalChars = source.reduce((a, it) => a + it.text.length, 0) || 1;

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
      let lines = m.wrapped.slice(0, alloc[i]);
      let text = lines.join('');
      const cut = text.length < source[i].text.length;
      if (cut) {
        // 行尾常是「话说一半」的逗号/顿号，去掉再补省略号，读起来才不像断线
        text = text.replace(/[，、；：,;:·|｜\s]+$/, '') + '…';
        lines = wrapLines(ctx, text, m.bodyW);
        if (lines.length > alloc[i]) {
          lines = lines.slice(0, alloc[i]);
          const last = lines[lines.length - 1];
          lines[lines.length - 1] = last.slice(0, Math.max(1, last.length - 1)) + '…';
        }
        text = lines.join('');
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
      ctx.fillText(ln, pad + b.labelW, ty);
      ty += b.lineH;
    }
    y += b.blockH;
  });

  // 页脚
  ctx.font = cardFont(footSize, opts, false);
  const foot = '仅供参考，不构成投资建议';
  ctx.fillText(foot, w - pad - ctx.measureText(foot).width, h - pad - footSize);

  // 记录保留率 / 被截短 / 被省略的条数，方便预览时核对
  layout.keptRatio = layout.blocks.reduce((a, b) => a + b.item.text.replace(/…$/, '').length, 0) / totalChars;
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

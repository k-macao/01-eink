// ---------- 章鱼 AI.每日研报：读取 007 → 四栏目 → 墨水屏位图 → 蓝牙推送 ----------
//
// 数据契约由 data/007-report.json 提供；GitHub Actions 每天澳门时间 09:05 / 17:05
// 读取 k-macao/007 的采集器并更新它。页面只消费四类内容：
//   AI 多空概率 / 热点话题 / 风险点 / 机会点。
//
// 与 main.js 的耦合刻意保持最小：旧缓存没有 writePayload 时仍可直接写 BLE 特征值。

const SUMMARY_PATH = 'data/007-report.json';
const REPORT_TITLE = '章鱼 AI.每日研报';
const REPORT_REPOSITORY = 'k-macao/007';

const SUM_CMD = {
  INIT:        0x01,
  REFRESH:     0x05,
  WRITE_IMAGE: 0x30,
};

/** 统一写入入口；兼容浏览器缓存中的旧版 main.js。 */
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

// MTU / RLE 由 main.js 在收到设备 notification 时写入。
const EpdLink = globalThis.EpdLink || (globalThis.EpdLink = { mtu: 240, rle: false, reported: false });
let pushingSummary = false;

// ---------- 007 JSON 解析与四栏目压缩 ----------

function cleanReportText(value) {
  return String(value == null ? '' : value)
    .replace(/<[^>]*>/g, ' ')
    .replace(/[*_`#>]/g, '')
    .replace(/^\s*(?:[-+•]|\d+[.)、])\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function clampText(text, maxLen) {
  const value = cleanReportText(text);
  return value.length > maxLen ? value.slice(0, maxLen - 1).replace(/[，、；：\s]+$/, '') + '…' : value;
}

/**
 * 上游 007 偶尔会把标题截断成「…ind…」，或留下未闭合的来源括号「【」。
 * 屏上不展示断尾：去掉省略标记、被切一半的末尾分词和孤括号，
 * 让后续每一档短版候选都以读得通的完整文本为来源。
 */
function tidyItemText(value) {
  if (value == null || value === false) return '';
  let text = cleanReportText(value);
  if (text.startsWith('【') && !text.includes('】')) text = text.slice(1).trim();
  const wasEllipsized = /(?:…|\.\.\.)\s*$/.test(text);
  if (wasEllipsized) {
    text = text.replace(/(?:…+|\.\.\.+)\s*$/, '');
    // 被腰斩的通常是最后一个空格分词（英文残词），连同尾随分隔符一并去掉；
    // 但长中文分词是完整信息（如「…寒意 第三季度出货量骤降21%…」），不能整段误删
    const lastToken = /\s+([^\s]+)$/.exec(text);
    if (lastToken && (lastToken[1].length <= 6 || !/[一-鿿]/.test(lastToken[1]))) {
      text = text.slice(0, lastToken.index);
    }
    text = text.replace(/[\s，、；：,;:·|｜/（(]+$/, '').trim();
    // 若正好截断在引号内部（“as 'extremely…”），把未闭合的引号连同残片去掉；
    // 只认空格/行首后的引号，避免误伤 don't 这类撇号
    const quotePairs = [["'", "'"], ['"', '"'], ['‘', '’'], ['“', '”']];
    for (const [open, close] of quotePairs) {
      let idx = -1;
      for (let i = 0; i < text.length; i++) {
        if (text[i] === open && (i === 0 || /\s/.test(text[i - 1]))) idx = i;
      }
      if (idx >= 0 && !text.slice(idx + 1).includes(close)) {
        text = text.slice(0, idx).replace(/[\s，、；：,;:·|｜/（(]+$/, '').trim();
      }
    }
    // 结尾悬空的英文虚词（as/to/of…）也一并去掉，句子收在实词上
    for (let n = 0; n < 2; n++) {
      const trimmed = text.replace(/\s+(?:as|to|of|and|or|the|a|an|in|on|for|with|by|at)$/i, '');
      if (trimmed === text) break;
      text = trimmed;
    }
  }
  // 上面的截断处理可能把】删掉、留下一个孤【，收口前再检查一次
  if (text.startsWith('【') && !text.includes('】')) text = text.slice(1).trim();
  // 整条标题被【】包住、尾巴只是重复简称的残片（如「【行云科技：…亿元】行云科技近…」）：
  // 被截断过时只取括号内的完整正文，去掉外层括号，避免与情绪标注的【】叠床架屋
  const bracketed = /^【([^】]+)】(.*)$/.exec(text);
  if (bracketed && (!bracketed[2] || (wasEllipsized && bracketed[2].length <= 12))) {
    text = bracketed[1].trim();
  }
  return text;
}

function percent(value, fallback) {
  if (value == null || String(value).trim() === '') return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(100, Math.round(n)));
}

function reportList(value, maxItems, mapper) {
  if (!Array.isArray(value)) return [];
  const seen = new Set();
  const out = [];
  value.slice(0, maxItems * 2).forEach((raw, index) => {
    const item = mapper(raw, index);
    if (!item || !item.text) return;
    const key = cleanReportText(item.text);
    if (!key || seen.has(key)) return;
    seen.add(key);
    item.text = key;
    out.push(item);
  });
  return out.slice(0, maxItems);
}

/**
 * Validate the generated same-origin JSON before it reaches the renderer.
 * A malformed scheduled update therefore produces a visible error instead of
 * silently putting made-up zeroes or empty sections on the physical display.
 */
function parse007Report(payload) {
  let raw = payload;
  if (typeof raw === 'string') {
    try { raw = JSON.parse(raw); }
    catch (_) { throw new Error('007 研报不是有效 JSON'); }
  }
  if (!raw || typeof raw !== 'object') throw new Error('007 研报数据为空');

  const src = raw.source || {};
  if (src.repository !== REPORT_REPOSITORY) {
    throw new Error('研报来源不是 ' + REPORT_REPOSITORY);
  }

  const p = raw.probability || {};
  let longP = percent(p.long, null);
  let shortP = percent(p.short, null);
  if (longP == null && shortP == null) throw new Error('007 研报缺少多空概率');
  if (longP == null) longP = 100 - shortP;
  if (shortP == null || Math.abs(longP + shortP - 100) > 1) shortP = 100 - longP;

  const topics = reportList(raw.topics, 6, (item) => {
    if (typeof item === 'string') return { text: tidyItemText(item), sentiment: '', weight: null };
    if (!item || typeof item !== 'object') return null;
    const weight = Number(item.weight);
    return {
      text: tidyItemText(item.text),
      sentiment: cleanReportText(item.sentiment),
      weight: Number.isFinite(weight) ? Math.max(0, Math.min(100, weight)) : null,
    };
  });
  const risks = reportList(raw.risks, 5, item => ({ text: tidyItemText(typeof item === 'string' ? item : item && item.text) }));
  const opportunities = reportList(raw.opportunities, 5, item => ({ text: tidyItemText(typeof item === 'string' ? item : item && item.text) }));

  if (!topics.length) throw new Error('007 研报缺少热点话题');
  if (!risks.length) throw new Error('007 研报缺少风险点');
  if (!opportunities.length) throw new Error('007 研报缺少机会点');

  return {
    id: cleanReportText(raw.id),
    title: cleanReportText(raw.title) || REPORT_TITLE,
    date: cleanReportText(raw.date),
    time: cleanReportText(raw.time),
    edition: cleanReportText(raw.edition),
    generatedAt: cleanReportText(raw.generated_at),
    sampleCount: Math.max(0, parseInt(raw.sample_count, 10) || 0),
    sourceCount: Math.max(0, parseInt(raw.source_count, 10) || 0),
    probability: {
      long: longP,
      short: shortP,
      label: cleanReportText(p.label),
      note: cleanReportText(p.note),
    },
    topics,
    risks,
    opportunities,
    source: src,
    disclaimer: cleanReportText(raw.disclaimer),
  };
}

function uniqueVariants(values) {
  const out = [];
  values.forEach(value => {
    const text = cleanReportText(value);
    if (text && out.indexOf(text) < 0) out.push(text);
  });
  return out;
}

/**
 * A punctuation-bounded short form is still a complete thought (for example,
 * 「锂电与电池产业链：排产回暖」→「锂电与电池产业链」).  This gives the
 * narrow screens a real alternative instead of manufacturing an ellipsis.
 */
function completeClause(value) {
  let text = cleanReportText(value);

  // 007 偶尔会给出「【来源：正文」或「【来源：正文】」；短版应保留正文，
  // 不能只留下一个未闭合的「【来源」。
  const sourceColon = text.search(/[：:]/);
  const sourceClose = text.indexOf('】');
  if (text.startsWith('【') && sourceColon >= 0 && (sourceClose < 0 || sourceColon < sourceClose)) {
    // 正文只取冒号后到】为止，括号外的残片尾巴不带进短句
    text = text.slice(sourceColon + 1, sourceClose >= 0 ? sourceClose : undefined).trim();
  } else if (sourceColon > 0 && sourceColon <= 4 && !/^\d/.test(text)) {
    // 「野村：维持…」这类短来源前缀（解析时已去掉【）：正文在冒号后，
    // 短版取正文；「09:30…」这类数字开头不按来源处理
    text = text.slice(sourceColon + 1).trim();
  }

  // 【整句被括号包住的标题】…：短句取括号内正文；【快讯】这类短标签则取括号外正文
  const bracketed = /^【([^】]+)】(.*)$/.exec(text);
  if (bracketed) {
    text = (bracketed[1].length <= 6 && bracketed[2] ? bracketed[2] : bracketed[1]).trim();
  }

  const cut = text.search(/[：:，,；;。！？!?]/);
  if (cut >= 4) return text.slice(0, cut).trim().replace(/若(?=持续|继续|出现|发生|未|不)/, '');
  return text.replace(/^【(?=[^】]*$)/, '');
}

/**
 * Build progressively shorter, punctuation-complete alternatives for small screens.
 * 简单压缩的候选阶梯（从最丰富到最短，排版引擎逐栏向下选）：
 *   全文带标注 → 去权重留情绪档（leanFormatter）→ 纯标题 → 全部条目的完整短句
 *   → 减条数与短句交错降档 → 单条 → 其余单条短句（窄屏可挑最短的一条）。
 */
function itemVariants(items, formatter, separator, leanFormatter) {
  const sep = separator || ' · ';
  const full = items.map(formatter).map(cleanReportText).filter(Boolean);
  const lean = leanFormatter ? items.map(leanFormatter).map(cleanReportText).filter(Boolean) : [];
  const plain = items.map(item => cleanReportText(item.text)).filter(Boolean);
  const clauses = plain.map(completeClause);
  return uniqueVariants([
    full.join(sep),
    lean.join(sep),
    plain.join(sep),
    clauses.join(sep),
    plain.slice(0, 4).join(sep),
    clauses.slice(0, 4).join(sep),
    plain.slice(0, 3).join(sep),
    clauses.slice(0, 3).join(sep),
    plain.slice(0, 2).join(sep),
    clauses.slice(0, 2).join(sep),
    plain.slice(0, 1).join(sep),
    clauses.slice(0, 1).join(sep),
    // 若首条在 250px 窄屏仍放不下，可选下一条完整短句，不做硬截断。
    ...clauses.slice(1),
  ]);
}

function formatTopic(item) {
  let suffix = item.sentiment || '';
  if (item.weight != null) suffix += (suffix ? '·' : '') + '权重' + Number(item.weight).toFixed(1).replace(/\.0$/, '') + '%';
  // 标注括号与 007 微信版栏目一致，使用【情绪档·权重%】
  return cleanReportText(item.text) + (suffix ? '【' + suffix + '】' : '');
}

/** 压缩第一档：保留情绪档（谨慎悲观/极度恐慌…），先省掉权重数字。 */
function formatTopicLean(item) {
  const suffix = item.sentiment || '';
  return cleanReportText(item.text) + (suffix ? '【' + suffix + '】' : '');
}

/**
 * Convert the 007 contract to exactly the four requested display columns.
 * Every line carries whole-sentence alternatives; the layout engine can move
 * to a shorter version without cutting a headline/risk in half.
 */
function buildCard(report) {
  if (!report) return { id: '', date: '', section: '007', lines: [] };
  const r = report.probability;
  const prob = '多 ' + r.long + '% · 空 ' + r.short + '%' + (r.label ? ' · ' + r.label : '');
  const probShort = '多' + r.long + '% · 空' + r.short + '%';
  const topicVariants = itemVariants(report.topics, formatTopic, ' · ', formatTopicLean);
  const riskVariants = itemVariants(report.risks, item => cleanReportText(item.text));
  const opportunityVariants = itemVariants(report.opportunities, item => cleanReportText(item.text));

  const stamp = [report.date, report.time, report.edition].filter(Boolean).join(' ');
  return {
    id: report.id || report.generatedAt || stamp,
    date: stamp,
    section: '007' + (report.edition ? ' · ' + report.edition : ''),
    lines: [
      { label: 'AI 多空概率', text: prob, alts: uniqueVariants([prob, probShort]) },
      { label: '热点话题', text: topicVariants[0], alts: topicVariants },
      { label: '风险点', text: riskVariants[0], alts: riskVariants },
      { label: '机会点', text: opportunityVariants[0], alts: opportunityVariants },
    ],
  };
}

// ---------- 渲染 ----------

/**
 * 固定使用随页面打包的阿里巴巴普惠体 3.0 / 55 Regular。
 * 三款支持的屏幕正文均严格使用 13px（紧凑排版，一屏装下更多完整内容）；
 * 标题、栏目标签、正文均不合成粗体。
 * 两款矮屏通过单行紧凑排版和完整短句候选保留全部四栏，而不是缩小字号。
 */
const SUM_FONTS = {
  puhui: {
    label: '阿里巴巴普惠体 · 非加粗',
    body: "'Alibaba PuHuiTi 3','Alibaba PuHuiTi 3.0','Microsoft YaHei',sans-serif",
    boldBody: false,
  },
};

const SUM_SIZE_SCALE = {
  fixed13: { label: '13px', offset: 0, bodySize: 13 },
};

const SUM_WEB_FONT = 'Alibaba PuHuiTi 3';
let sumFontLoadPromise = null;

/** Canvas 必须等 WebFont 就绪后再量字，否则首屏会悄悄落到系统字体。 */
function ensureSummaryFont() {
  if (typeof document === 'undefined' || !document.fonts || typeof document.fonts.load !== 'function') {
    return Promise.resolve(false);
  }
  if (!sumFontLoadPromise) {
    sumFontLoadPromise = document.fonts.load(
      '400 13px "' + SUM_WEB_FONT + '"', '章鱼研报多空热点风险机会'
    ).then(faces => {
      if (!faces || !faces.length) throw new Error('阿里巴巴普惠体加载失败');
      return true;
    }).catch(error => {
      sumFontLoadPromise = null;
      throw error;
    });
  }
  return sumFontLoadPromise;
}

/** 旧调用未指定固定字号时的兼容下限；页面的三款屏幕均固定使用 13px。 */
const SUM_MIN_FONT = 10;

function sumDisplayOptions() {
  const fontEl = document.getElementById('sum-font');
  const fontKey = (fontEl && fontEl.value) || 'puhui';
  const font = SUM_FONTS[fontKey] || SUM_FONTS.puhui;

  const scaleEl = document.getElementById('sum-size-scale');
  const scaleKey = (scaleEl && scaleEl.value) || 'fixed13';
  const scaleDef = SUM_SIZE_SCALE[scaleKey] || SUM_SIZE_SCALE.fixed13;

  return {
    fontKey: SUM_FONTS[fontKey] ? fontKey : 'puhui',
    fontFamily: font.body,
    boldBody: false,
    fontLabel: font.label,
    scaleLabel: scaleDef.label,
    bodySize: scaleDef.bodySize,
    offset: 0,
  };
}

// ---------- 偏好记忆（字体 / 13px 固定，只记屏幕尺寸与预览倍数） ----------

const SUM_PREF_KEYS = ['sum-size', 'sum-zoom'];

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

function cardFont(size, opts, _bold) {
  const fam = (opts && opts.fontFamily) || SUM_FONTS.puhui.body;
  return '400 ' + size + 'px ' + fam;
}

/** 用户指定全卡使用 Regular，不允许浏览器合成粗体。 */
function sumBold(_size, _opts) {
  return false;
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
 * opts: { fontFamily, fontLabel, boldBody, bodySize }
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

  // 未显式传入固定字号的兼容路径仍按画布计算上下限；页面配置会在下方
  // 直接指定 13px，不使用这套自适应上限。
  const baseBody = clamp(Math.round(h * 0.052), SUM_MIN_FONT, 22);
  const maxBody = clamp(Math.min(baseBody + 7, Math.floor(areaHeight / 5.2)), baseBody, 24);

  const boldAt = size => sumBold(size, opts);

  // alts：该条的「逐级更短候选」，小屏放不下时整句换短，而不是腰斩
  const source = card.lines.map(l => ({ label: l.label, text: l.text, alts: l.alts || [] }));
  const squeezeText = t => String(t).replace(/\s+/g, '');

  const totalChars = source.reduce((a, it) => a + squeezeText(it.text).length, 0) || 1;
  // 页面显式固定字号（当前 13px）时，两款矮屏走单行紧凑承载四栏；
  // 不写死具体字号数字，改字号规格时这里不用跟着改
  const fixedCompact = Number.isFinite(Number(opts.bodySize)) && h <= 128;

  /** 某个字号下，每条内容按可用宽度折行后的行数 */
  const measureBlock = (item, bodySize, labelSize) => {
    ctx.font = cardFont(labelSize, opts, boldAt(labelSize));
    // 250px 窄屏少留 0.15em 标签间距，刚好容纳完整风险短句；字形不缩小。
    const labelGap = fixedCompact ? 0.45 : 0.6;
    let labelW = ctx.measureText(item.label).width + Math.round(labelSize * labelGap);
    if (labelW > maxWidth * 0.34) labelW = maxWidth * 0.34;
    const bodyW = maxWidth - labelW;
    ctx.font = cardFont(bodySize, opts, boldAt(bodySize));
    const wrapped = wrapLines(ctx, item.text, Math.max(bodyW, maxWidth * 0.4));
    return { labelW, wrapped, bodyW };
  };

  /**
   * 「尽量一屏，不够用简单压缩」排版（13px 紧凑规格）：
   *   ① 间距三档（profile）整体走紧凑：0 档行高 1.36 → 1 档 1.25 → 2 档 1.15，
   *      只压行距/块距，不缩字号；两款矮屏在 1 档起直接用单行紧凑
   *      （122/128px 高度容纳四个 13px 区块，富余行数还可给某一栏排两行）。
   *   ② 每栏自带逐级更短的完整候选（候选阶梯见 itemVariants）：
   *      先按全文量行，放不下就把「当前行数最多」的一栏整栏降一档，
   *      循环到总行数能装进一屏——行数在各栏之间自然均衡，无需预先分行。
   *   ③ 全部候选用完仍装不下时才允许省略号截尾（clean=false），
   *      调用方会优先采用任一间距档下的 clean 方案。
   */
  const planLayout = (bodySize, maxLinesPerItem, profile) => {
    const count = source.length;
    // 标签与正文同号（同号更好认），但标签列最多占 34% 宽度；小屏才降一号
    const labelSize = Math.max(9, Math.min(bodySize, 17));
    let lineH, blockGap;
    if (fixedCompact && profile >= 1) {
      lineH = bodySize;
      blockGap = 1;
    } else if (profile <= 0) {
      lineH = Math.round(bodySize * 1.36);
      blockGap = Math.max(2, Math.round(bodySize * 0.24));
    } else if (profile === 1) {
      lineH = Math.round(bodySize * 1.25);
      blockGap = 2;
    } else {
      lineH = Math.round(bodySize * 1.15);
      blockGap = 1;
    }
    const capacity = Math.floor((areaHeight - blockGap * (count - 1)) / lineH);
    if (capacity < count) return null;

    // 单栏行数上限：不超过可视高度的 65%，免得一栏吃掉整屏把其他栏挤没
    const perItemCap = maxLinesPerItem > 0
      ? Math.max(1, maxLinesPerItem)
      : Math.max(1, Math.round(capacity * 0.65));

    // 每栏的候选阶梯：量好每个候选折行后的行数，只保留「行数严格递减」的档位，
    // 保证每降一档一定有进展；同行数的候选取排位最靠前（最丰富）的那个
    const states = source.map(item => {
      const m = measureBlock(item, bodySize, labelSize);
      const variants = uniqueVariants([item.text].concat(item.alts || []));
      const ladder = [];
      variants.forEach(text => {
        const wrapped = wrapLines(ctx, text, Math.max(m.bodyW, maxWidth * 0.4));
        if (!ladder.length || wrapped.length < ladder[ladder.length - 1].wrapped.length) {
          ladder.push({ text, wrapped });
        }
      });
      let level = 0;
      while (level < ladder.length - 1 && ladder[level].wrapped.length > perItemCap) level++;
      return { item, m, ladder, level };
    });

    const totalLines = () => states.reduce((a, s) => a + s.ladder[s.level].wrapped.length, 0);
    // 简单压缩：每次把行数最多的一栏降一档（同行数时先降靠后的栏，
    // 靠前的概率/热点优先保持完整），直到总行数装进一屏
    while (totalLines() > capacity) {
      let pick = -1;
      for (let i = 0; i < states.length; i++) {
        const s = states[i];
        if (s.level >= s.ladder.length - 1) continue;
        if (pick < 0 || s.ladder[s.level].wrapped.length >= states[pick].ladder[states[pick].level].wrapped.length) pick = i;
      }
      if (pick < 0) break;
      states[pick].level++;
    }

    const clean = totalLines() <= capacity;
    const blocks = [];
    let budget = capacity;
    for (let i = 0; i < count; i++) {
      const s = states[i];
      let text = s.ladder[s.level].text;
      let lines = s.ladder[s.level].wrapped.slice();
      if (!clean) {
        // 极端兜底：候选全部用完仍超高才截尾；每栏至少保留 1 行，
        // 行尾的逗号/顿号先去掉再补省略号，读起来才不像断线
        const allowed = Math.max(1, budget - (count - 1 - i));
        if (lines.length > allowed) {
          lines = lines.slice(0, allowed);
          text = lines.join(' ').replace(/[，、；：,;:·|｜\s]+$/, '') + '…';
          lines = wrapLines(ctx, text, s.m.bodyW).slice(0, allowed);
          const last = lines[lines.length - 1];
          if (last && !last.endsWith('…')) {
            lines[lines.length - 1] = last.slice(0, Math.max(1, last.length - 1)) + '…';
          }
          text = lines.join(' ');
        }
      }
      budget -= lines.length;
      blocks.push({
        item: { label: s.item.label, text },
        labelW: s.m.labelW, labelSize, wrapped: lines, lineH, blockH: lines.length * lineH,
      });
    }
    return { blocks, bodySize, lineH, blockGap, clean, totalH: blocks.reduce((a, b) => a + b.blockH, 0) + blockGap * (count - 1) };
  };

  /**
   * 字号选择分两步：
   *   ① 找出「一个字都不截、一条都不少」能排下的最大字号（nativeSize）；
   *      找不到就退而求其次，取保留内容最多的那一档。
   *   ② 再按「字号」档位偏移（小 −1 / 标准 0 / 大 +2），
   *      偏移后的字号允许截短正文——所以「大」档是拿内容换字号，看得见。
   * 这样档位之间是单调的，不会出现「小」档反而比「标准」档字更大的怪事。
   */
  // 折行时行间补的空格不能算进字数，否则截短了也算成 100%
  const keptCharsOf = got => got.blocks.reduce((a, b) => a + squeezeText(b.item.text.replace(/…$/, '')).length, 0);

  /**
   * 某个字号下把三档紧凑行距（1.36 → 1.25 → 1.15）都试排一遍，内容优先：
   *   ① 能完整装进一屏（clean）的方案里，选保留内容最多的——
   *      内容重的日子会自动收紧间距多装几行，而不是只剩半栏；
   *   ② 保留字数相同时取相对宽松的一档（先试到的先得；最松一档也是紧凑行距）；
   *   ③ 三档都不完整时，取保留内容最多的兜底。
   */
  const bestPlanAt = (size) => {
    let cleanBest = null;
    let fallback = null;
    for (let profile = 0; profile <= 2; profile++) {
      const got = planLayout(size, 0, profile);
      if (!got) continue;
      if (got.clean) {
        if (!cleanBest || keptCharsOf(got) > keptCharsOf(cleanBest)) cleanBest = got;
      } else if (!fallback || keptCharsOf(got) > keptCharsOf(fallback)) {
        fallback = got;
      }
    }
    return cleanBest || fallback;
  };

  const nativeSize = () => {
    let fallback = null;
    for (let size = maxBody; size >= SUM_MIN_FONT; size--) {
      const got = bestPlanAt(size);
      if (!got) continue;
      got.keptRatio = keptCharsOf(got) / totalChars;
      if (got.keptRatio >= 0.999 && got.blocks.length === source.length) return size;
      if (!fallback || got.keptRatio > fallback.ratio) fallback = { size, ratio: got.keptRatio };
    }
    return fallback ? fallback.size : SUM_MIN_FONT;
  };

  // 显式配置的 13px 不再受矮屏自适应上限影响；两款矮屏由紧凑单行布局承载。
  const minReadable = clamp(Math.round(h * 0.09), SUM_MIN_FONT, 14);
  const requestedSize = Number(opts.bodySize);
  const preferredSize = Number.isFinite(requestedSize) ? requestedSize : nativeSize() + offset;
  const target = Number.isFinite(requestedSize)
    ? requestedSize
    : clamp(preferredSize, minReadable, maxBody);
  let layout = bestPlanAt(target);
  for (let size = target - 1; size >= SUM_MIN_FONT && !layout; size--) {
    layout = bestPlanAt(size);
  }

  // 连最小字号都排不下 → 删末条（保留靠前的结论）
  while (!layout && source.length > 1) {
    source.pop();
    layout = bestPlanAt(SUM_MIN_FONT);
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

  // 标题（按用户指定标点原样显示）
  ctx.font = cardFont(titleSize, opts, boldAt(titleSize));
  ctx.fillText(REPORT_TITLE, pad, pad);

  // 日期 / 007 版次
  ctx.font = cardFont(headSize, opts, false);
  ctx.fillText(card.date || '', pad, headTop);
  const sectionText = card.section || '007';
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
  // 区分两种“变短”：整栏换成完整短版（已压缩）与省略号截尾（已截短）
  layout.hardTrimmed = layout.blocks.filter(b => b.item.text.endsWith('…')).length;
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

/** 预览状态栏的一句话说明：字号 / 字体 / 条数 / 是否为放进一屏做了压缩或截短 */
function describeLayout(layout, opts) {
  if (!layout) return '';
  let s = layout.bodySize + 'px ' + (opts ? opts.fontLabel : '') + ' · ' + layout.kept + ' 条';
  if (layout.hardTrimmed > 0) s += ' · ' + layout.hardTrimmed + ' 条已截短';
  else if (layout.trimmed > 0) s += ' · ' + layout.trimmed + ' 栏已压缩（一屏完整）';
  if (layout.kept < layout.total) s += ' · 省略 ' + (layout.total - layout.kept) + ' 条';
  return s;
}

async function loadSummaryCard() {
  const resp = await fetch(SUMMARY_PATH + '?t=' + Date.now(), { cache: 'no-store' });
  if (!resp.ok) throw new Error('读取仓库007研报失败 HTTP ' + resp.status);
  const payload = await resp.text();
  return buildCard(parse007Report(payload));
}

// 缓存最近一次 007 卡片：切字号 / 字体 / 预览倍数时直接重绘，不重复取数
const SUM_CACHE_MS = 5 * 60 * 1000;
const SUM_PAGE_REFRESH_MS = 2 * 60 * 1000;
let sumCardCache = null;
let sumCardFetchedAt = 0;
let sumRefreshTimerId = null;

async function getSummaryCard(force) {
  const fresh = sumCardCache && (Date.now() - sumCardFetchedAt) < SUM_CACHE_MS;
  if (!force && fresh) return sumCardCache;
  const card = await loadSummaryCard();
  sumCardCache = card;
  sumCardFetchedAt = Date.now();
  return card;
}

/** 只拉取并预览，不下发蓝牙。force=true 时忽略缓存重新读取 007 */
async function previewSummary(force) {
  const canvas = document.getElementById('sum-preview');
  if (!sumCardCache) sumStatus('正在读取仓库007最新研报...');
  try {
    const card = await getSummaryCard(force === true);
    await ensureSummaryFont();
    const { layout, opts } = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    applySumZoom(canvas);
    sumStatus('已生成预览（' + (card.date || '日期未知') + '）· ' + describeLayout(layout, opts));
    if (typeof addLog === 'function') addLog('每日研报预览已生成：' + describeLayout(layout, opts));
    return card;
  } catch (e) {
    sumStatus('预览失败：' + e.message);
    if (typeof addLog === 'function') addLog('每日研报预览失败：' + e.message);
    return null;
  }
}

/** 页面保持打开时，每两分钟检查一次；只有 007 报告 id 变化才重绘。 */
async function refreshSummaryIfChanged() {
  const previousId = sumCardCache && sumCardCache.id;
  try {
    const card = await getSummaryCard(true);
    if (previousId && card.id === previousId) return false;
    await ensureSummaryFont();
    const canvas = document.getElementById('sum-preview');
    const { layout, opts } = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    applySumZoom(canvas);
    sumStatus('007 已更新（' + (card.date || '日期未知') + '）· ' + describeLayout(layout, opts));
    if (previousId && typeof addLog === 'function') addLog('检测到 007 新版次，页面预览已自动更新');
    return true;
  } catch (_) {
    // 定时检查失败不覆盖屏上最后一份有效研报；手动刷新会显示完整错误。
    return false;
  }
}

function startSummaryRefreshChecker() {
  if (sumRefreshTimerId != null || typeof setInterval !== 'function') return;
  sumRefreshTimerId = setInterval(refreshSummaryIfChanged, SUM_PAGE_REFRESH_MS);
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
  if (pushingSummary) return false;
  if (!isDeviceConnected()) {
    sumStatus('请先连接设备');
    if (typeof addLog === 'function') addLog(tag + '：设备未连接');
    return false;
  }
  const btn = document.getElementById(btnId);
  pushingSummary = true;
  if (btn) btn.disabled = 'disabled';
  try {
    await worker();
    return true;
  } catch (e) {
    console.error(e);
    sumStatus('推送失败：' + e.message);
    if (typeof addLog === 'function') addLog(tag + '推送失败：' + e.message);
    return false;
  } finally {
    pushingSummary = false;
    if (btn) btn.disabled = null;
  }
}

async function pushLatestSummary() {
  return withPushGuard('btn-push-summary', '每日研报', async () => {
    sumStatus('正在读取仓库007最新研报...');
    const card = await getSummaryCard(true);
    await ensureSummaryFont();

    const canvas = document.getElementById('sum-preview');
    const { layout, size, opts } = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    applySumZoom(canvas);

    addLog('每日研报：' + (card.date || '') + '，' + size.w + '×' + size.h + '，' +
      describeLayout(layout, opts) + '，块大小 ' + EpdLink.mtu + 'B' + (EpdLink.rle ? '（RLE 固件）' : ''));

    await pushCanvas(canvas, '每日研报');
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
  ctx.fillText('同句对比 · 全部非加粗 · 基准13px', pad, headTop);
  const right = '挑最清楚的';
  ctx.fillText(right, w - pad - ctx.measureText(right).width, headTop);

  const ruleY = headTop + headSize + Math.round(pad * 0.55);
  const ruleH = Math.max(2, Math.round(h * 0.007));
  ctx.fillRect(pad, ruleY, maxWidth, ruleH);

  const requested = Number(opts && opts.bodySize);
  const base = Math.max(SUM_MIN_FONT, Math.min(22, Number.isFinite(requested) ? requested : 13));
  // 样张可比较相邻字号，但所有行都保持用户指定的 Regular 字重。
  const rows = [base + 3, base, base - 3, base - 5]
    .map(size => ({ size, bold: false }))
    .filter(row => row.size >= SUM_MIN_FONT && row.size <= 26);

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
  await ensureSummaryFont();
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
    startSummaryRefreshChecker();
  };
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(ready, 0);
  } else if (typeof document.addEventListener === 'function') {
    document.addEventListener('DOMContentLoaded', ready);
  }
}

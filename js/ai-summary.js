// ---------- AI 总结：抓取日报 → 渲染成墨水屏位图 → 蓝牙推送 ----------
// 依赖 main.js 中的：addLog / epdCharacteristic / gattServer / writePayload / EpdCmd

const SUMMARY_PATH = '001/output/latest.html';

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

function wrapLines(ctx, text, maxWidth) {
  const out = [];
  let line = '';
  for (const ch of String(text)) {
    const test = line + ch;
    if (ctx.measureText(test).width > maxWidth && line !== '') {
      out.push(line);
      line = ch;
    } else {
      line = test;
    }
  }
  if (line !== '') out.push(line);
  return out;
}

/**
 * 把卡片画到 canvas 上。字号自动收缩直到放得下。
 * 纯 canvas 2D API，便于在 Node 中用桩测试。
 */
function drawCard(ctx, card, w, h, opts) {
  opts = opts || {};
  const pad = Math.max(6, Math.round(Math.min(w, h) * 0.05));
  const maxWidth = w - pad * 2;

  ctx.fillStyle = '#FFFFFF';
  ctx.fillRect(0, 0, w, h);
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#000000';

  const titleSize = Math.max(10, Math.round(h * 0.055));
  const headSize = Math.max(9, Math.round(h * 0.038));

  const areaTop = pad + titleSize + headSize + pad * 1.6;
  const areaBottom = h - pad - headSize; // 底部留免责声明
  const areaHeight = areaBottom - areaTop;

  const tryLayout = (items, bodySize) => {
    let y = 0;
    const blocks = [];
    for (const item of items) {
      const labelSize = Math.max(8, bodySize);
      ctx.font = 'bold ' + labelSize + 'px sans-serif';
      const labelW = ctx.measureText(item.label).width + labelSize * 0.6;
      ctx.font = bodySize + 'px sans-serif';
      const bodyW = maxWidth - labelW;
      if (bodyW < maxWidth * 0.4) return null;
      const wrapped = wrapLines(ctx, item.text, bodyW);
      const lineH = Math.round(bodySize * 1.55);
      const blockH = wrapped.length * lineH + Math.round(bodySize * 0.7);
      blocks.push({ item, labelW, labelSize, wrapped, lineH, blockH });
      y += blockH;
      if (y > areaHeight) return null;
    }
    return { blocks, bodySize, totalH: y };
  };

  // 排不下时的降级顺序：先截短最长的一条 → 仍不行再删掉末条 →
  // 只剩一条还放不下，才缩到最小字号。
  let layout = null;
  const minBody = Math.max(8, Math.round(h * 0.036));
  const startSize = Math.max(minBody, Math.round(h * 0.045));
  let items = card.lines.map(l => ({ label: l.label, text: l.text }));

  for (let attempt = 0; attempt < 14 && !layout; attempt++) {
    for (let size = startSize; size >= minBody; size--) {
      layout = tryLayout(items, size);
      if (layout) break;
    }
    if (layout) break;

    // 找当前最长的一条
    let longest = 0, li = -1;
    items.forEach((it, i) => { if (it.text.length > longest) { longest = it.text.length; li = i; } });

    if (longest > 18 && li >= 0) {
      const keep = Math.max(16, Math.floor(longest * 0.7));
      items[li] = { label: items[li].label, text: items[li].text.slice(0, keep) + '…' };
    } else if (items.length > 1) {
      items.pop();
    } else {
      break;
    }
  }

  if (!layout) {
    // 极端情况：最小字号硬排一条，超出部分裁切
    layout = { blocks: [], bodySize: minBody, totalH: 0 };
    const item = card.lines[0];
    if (item) {
      ctx.font = minBody + 'px sans-serif';
      const wrapped = wrapLines(ctx, item.text, maxWidth);
      layout.blocks.push({
        item, labelW: 0, labelSize: minBody, wrapped,
        lineH: Math.round(minBody * 1.55), blockH: wrapped.length * Math.round(minBody * 1.55),
      });
    }
  }

  // 标题
  ctx.font = 'bold ' + titleSize + 'px sans-serif';
  ctx.fillText('章鱼 AI · 上水日报', pad, pad);

  ctx.font = headSize + 'px sans-serif';
  const dateText = card.date || '';
  ctx.fillText(dateText, pad, pad + titleSize + Math.round(pad * 0.35));

  const sectionText = '总结';
  ctx.font = headSize + 'px sans-serif';
  ctx.fillText(sectionText, w - pad - ctx.measureText(sectionText).width,
    pad + titleSize + Math.round(pad * 0.35));

  // 分隔线
  const lineY = pad + titleSize + headSize + Math.round(pad * 0.9);
  ctx.fillRect(pad, lineY, maxWidth, Math.max(1, Math.round(h * 0.006)));

  // 正文
  let y = areaTop;
  for (const b of layout.blocks) {
    ctx.font = 'bold ' + b.labelSize + 'px sans-serif';
    ctx.fillText(b.item.label, pad, y);

    ctx.font = layout.bodySize + 'px sans-serif';
    let ty = y;
    for (const ln of b.wrapped) {
      ctx.fillText(ln, pad + b.labelW, ty);
      ty += b.lineH;
    }
    y += b.blockH;
  }

  // 页脚
  ctx.font = Math.max(8, Math.round(h * 0.032)) + 'px sans-serif';
  const foot = '仅供参考，不构成投资建议';
  ctx.fillText(foot, w - pad - ctx.measureText(foot).width, h - pad - Math.round(h * 0.032));
}

/**
 * canvas → 墨水屏两个位平面（1bpp，8 像素/字节，MSB first）
 * 黑白层：1 = 白，0 = 黑
 * 红色层：0 = 红，1 = 非红
 */
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

      if (gray >= 140) bw[byteIdx] |= bit;
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

function renderSummaryToCanvas(canvas, card) {
  const size = getSumSize();
  canvas.width = size.w;
  canvas.height = size.h;
  const ctx = canvas.getContext('2d');

  const rotEl = document.getElementById('sum-rotate');
  const flip = !!(rotEl && rotEl.checked);

  if (!flip) {
    drawCard(ctx, card, size.w, size.h);
    return ctx;
  }

  // 旋转 180°：先画到临时画布，再倒着贴回来
  const tmp = document.createElement('canvas');
  tmp.width = size.w;
  tmp.height = size.h;
  drawCard(tmp.getContext('2d'), card, size.w, size.h);
  ctx.save();
  ctx.translate(size.w, size.h);
  ctx.rotate(Math.PI);
  ctx.drawImage(tmp, 0, 0);
  ctx.restore();
  return ctx;
}

async function loadSummaryCard() {
  const resp = await fetch(SUMMARY_PATH + '?t=' + Date.now(), { cache: 'no-store' });
  if (!resp.ok) throw new Error('读取日报失败 HTTP ' + resp.status);
  const html = await resp.text();
  const summary = extractSummary(html);
  if (!summary) throw new Error('未在日报中找到「总结」栏目');
  return buildCard(summary);
}

/** 只拉取并预览，不下发蓝牙 */
async function previewSummary() {
  const canvas = document.getElementById('sum-preview');
  sumStatus('正在读取最新日报...');
  try {
    const card = await loadSummaryCard();
    renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';
    sumStatus('已生成预览（' + (card.date || '日期未知') + '）：' + card.lines.map(l => l.label).join(' / '));
    if (typeof addLog === 'function') addLog('AI 总结预览已生成，共 ' + card.lines.length + ' 条');
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
    payload[0] = EpdCmd.WRITE_IMAGE;
    payload[1] = flag;
    payload.set(chunk, 2);

    const ok = await writePayload(payload, true, true);
    if (!ok) return false;
    if (onProgress) onProgress(i + 1, total);
  }
  return true;
}

async function pushLatestSummary() {
  if (pushingSummary) return;

  if (typeof gattServer === 'undefined' || gattServer == null || !gattServer.connected) {
    sumStatus('请先连接设备');
    if (typeof addLog === 'function') addLog('AI 总结推送：设备未连接');
    return;
  }

  const btn = document.getElementById('btn-push-summary');
  pushingSummary = true;
  if (btn) btn.disabled = 'disabled';

  try {
    sumStatus('正在读取最新日报...');
    const card = await loadSummaryCard();

    const canvas = document.getElementById('sum-preview');
    const ctx = renderSummaryToCanvas(canvas, card);
    canvas.style.display = 'block';

    const size = getSumSize();
    const useRed = !!(document.getElementById('sum-color') && document.getElementById('sum-color').checked);
    const { bw, red } = canvasToLayers(ctx, size.w, size.h);

    addLog('AI 总结：' + (card.date || '') + '，' + size.w + '×' + size.h +
      '，块大小 ' + EpdLink.mtu + 'B' + (EpdLink.rle ? '（RLE 固件）' : ''));

    sumStatus('初始化屏幕...');
    const initOk = await writePayload([EpdCmd.INIT], true, true);
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
    const refOk = await writePayload([EpdCmd.REFRESH], true, true);
    if (!refOk) throw new Error('刷新指令下发失败');

    addLog('AI 总结推送完成，屏幕刷新中请勿操作');
    sumStatus('已推送（' + (card.date || '') + '），屏幕刷新中');
  } catch (e) {
    console.error(e);
    sumStatus('推送失败：' + e.message);
    if (typeof addLog === 'function') addLog('AI 总结推送失败：' + e.message);
  } finally {
    pushingSummary = false;
    if (btn) btn.disabled = null;
  }
}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🔬 特征层 —— 量化引擎的第 2 层。

把「日线序列」变成「可解释的因子」，每个因子都能在页面上用一句话说清：

  MOM  动量   —— 20/60 日收益相对自身一年分布的 z 值（追涨杀跌的证据）
  TRD  趋势   —— 60 日对数价格回归的斜率 t 值 + 均线多空排列（趋势是否成立）
  REV  反转   —— 价格偏离 20 日均线的 σ 倍数（超买扣分、超卖加分，均值回归）
  VOL  量能   —— 放量方向 × 量比 z 值（量在价先）
  FLOW 资金流 —— 南向 / 主力资金的 z 值（钱的方向）

五因子加权得到综合分 S，再由 probability 层把 S 校准成真实的上涨概率。
全部为确定性计算：不调外部大模型、不引入未抓取的数字、同样输入同样输出。
"""
from __future__ import annotations

import math

from . import stats

# 因子权重（合计 1.0；缺失因子的权重按比例重分配给其余因子）
# 基础权重：动量与趋势为主，反转/量能/资金为辅
FACTOR_WEIGHTS = {
    "mom": 0.30,
    "trd": 0.25,
    "rev": 0.15,
    "vol": 0.15,
    "flow": 0.15,
}

# 动态调整后的权重会在 factor_scores 中根据市场状态实时调整（见 _dynamic_weights）
# 例如：高波动时降低 MOM、提高 REV；强趋势时提高 TRD；资金流强时提高 FLOW

# 各因子的软上限（超过按 tanh 压缩，避免单因子绑架结论）
FACTOR_CAP = 2.0

MIN_BARS = 60  # 少于 60 根日线不做概率推断（样本不足以谈统计显著）


# ------------------------------------------------------------------
# 单标的特征
# ------------------------------------------------------------------
def compute_features(bars):
    """由日线序列算全套特征；样本不足时对应字段为 None，绝不填充默认值。"""
    bars = [b for b in (bars or []) if b.get("close") is not None]
    if len(bars) < 20:
        return {"ok": False, "bars": len(bars or []), "reason": "日线样本不足 20 根"}

    dates = [b["date"] for b in bars]
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) if b.get("high") is not None else c
             for b, c in zip(bars, closes)]
    lows = [float(b["low"]) if b.get("low") is not None else c
            for b, c in zip(bars, closes)]
    volumes = [float(b["volume"]) if b.get("volume") not in (None, 0) else None
               for b in bars]

    close = closes[-1]
    feat = {
        "ok": True,
        "date": dates[-1],
        "bars": len(bars),
        "close": close,
        "prev_close": closes[-2] if len(closes) > 1 else None,
        "chg_pct": (close / closes[-2] - 1.0) * 100.0
                   if len(closes) > 1 and closes[-2] else None,
    }

    # ---- 收益 ----
    for h in (1, 5, 20, 60, 120):
        feat[f"ret_{h}"] = _pct_change(closes, h)

    # ---- 均线 ----
    ma5, ma20, ma60, ma120 = (stats.sma(closes, n) for n in (5, 20, 60, 120))
    feat["sma5"], feat["sma20"] = ma5[-1], ma20[-1]
    feat["sma60"], feat["sma120"] = ma60[-1], ma120[-1]
    # 均线多空排列：收盘>MA5(+1/-1) + MA5>MA20 + MA20>MA60 → -3 ~ +3
    align = 0
    pairs = ((close, ma5[-1]), (ma5[-1], ma20[-1]), (ma20[-1], ma60[-1]))
    for fast, slow in pairs:
        if fast is None or slow is None:
            continue
        align += 1 if fast > slow else -1
    feat["ma_align"] = align

    # ---- 动量 z：20/60 日收益相对自身一年分布 ----
    rets = stats.simple_returns(closes)
    feat["vol20"] = stats.realized_vol(rets[-20:]) if len(rets) >= 20 else None
    feat["vol60"] = stats.realized_vol(rets[-60:]) if len(rets) >= 60 else None
    feat["vol_ewma"] = stats.ewma_vol(rets) if len(rets) >= 20 else None

    # 滚动 20/60 日收益序列，用于给当前动量打分（分布来自这只标的自身）
    def _roll_pct(n):
        out = []
        for i in range(n, len(closes)):
            if closes[i - n]:
                out.append((closes[i] / closes[i - n] - 1.0) * 100.0)
        return out

    r20_series = _roll_pct(20)
    r60_series = _roll_pct(60)
    feat["mom_z20"] = stats.zscore(feat["ret_20"], r20_series)
    feat["mom_z60"] = stats.zscore(feat["ret_60"], r60_series)

    # ---- 波动率分位：vol20 相对一年滚动 vol20 的分布 ----
    vol20_series = [stats.realized_vol(rets[i - 20:i]) for i in range(20, len(rets) + 1)]
    vol20_series = [v for v in vol20_series if v is not None and v > 0]
    feat["vol_pct"] = stats.pct_rank(feat["vol20"], vol20_series)

    # ---- 趋势：60 日对数价格回归 ----
    window = closes[-60:] if len(closes) >= 60 else closes
    if all(v > 0 for v in window):
        reg = stats.linreg([math.log(v) for v in window])
    else:
        reg = None
    if reg:
        # 斜率 → 年化百分比（60 个交易日约 60/252 年）
        feat["trend_slope_ann"] = (math.exp(reg["slope"] * 252) - 1.0) * 100.0
        feat["trend_r2"] = reg["r2"]
        feat["trend_t"] = reg["t_stat"]
    else:
        feat["trend_slope_ann"] = feat["trend_r2"] = feat["trend_t"] = None

    # ---- 反转：偏离 20 日均线多少个 σ ----
    sd20 = stats.stdev(closes[-20:])
    feat["z20"] = (close - ma20[-1]) / sd20 if (sd20 and ma20[-1] is not None) else None
    sd60 = stats.stdev(closes[-60:]) if len(closes) >= 60 else None
    feat["z60"] = (close - ma60[-1]) / sd60 if (sd60 and ma60[-1] is not None) else None

    # ---- 摆动指标 ----
    feat["rsi14"] = stats.rsi(closes, 14)
    dif, dea, hist = stats.macd(closes)
    feat["macd_hist"] = hist
    feat["macd_dif"] = dif
    boll = stats.bollinger(closes, 20)
    feat["boll_pctb"] = boll["pctb"]
    feat["boll_width"] = boll["width_pct"]
    atr_val, atr_pct = stats.atr(
        [{"high": h, "low": lo, "close": c} for h, lo, c in zip(highs, lows, closes)], 14)
    feat["atr_pct"] = atr_pct
    feat["streak"] = stats.streak(closes)

    # ---- 量能 ----
    usable_vol = [v for v in volumes if v]
    if usable_vol and len(usable_vol) >= 20 and volumes[-1]:
        last20 = [v for v in volumes[-20:] if v]
        mu20 = sum(last20) / len(last20) if last20 else None
        feat["vol_ratio"] = volumes[-1] / mu20 if mu20 else None
        feat["vol_z"] = stats.zscore(volumes[-1], volumes[-60:] if len(usable_vol) >= 60 else volumes)
        vol_series = []
        for i in range(20, len(volumes)):
            w = [v for v in volumes[i - 20:i] if v]
            if len(w) >= 15 and volumes[i]:
                vol_series.append(volumes[i] / (sum(w) / len(w)))
        feat["vol_ratio_pct"] = stats.pct_rank(feat["vol_ratio"], vol_series)
        feat["amount"] = volumes[-1] * close if close else None
    else:
        feat["vol_ratio"] = feat["vol_z"] = feat["vol_ratio_pct"] = None
        feat["amount"] = None

    # ---- 流动性深度：Amihud 非流动性（|收益| / 成交额，越大越难出货）----
    amihud = []
    for i in range(len(closes) - 20, len(closes)):
        if i <= 0 or not volumes[i] or not closes[i - 1]:
            continue
        amihud.append(abs(closes[i] / closes[i - 1] - 1.0) / (volumes[i] * closes[i]))
    feat["amihud"] = (sum(amihud) / len(amihud) * 1e8) if len(amihud) >= 10 else None

    # ---- 回撤 ----
    hi = max(closes)
    feat["dd_from_high"] = (close / hi - 1.0) * 100.0 if hi else None
    feat["high_52w"] = hi

    return feat


def _pct_change(closes, n):
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


# ------------------------------------------------------------------
# 因子打分
# ------------------------------------------------------------------
def _tanh_cap(value, cap=FACTOR_CAP):
    """软压缩到 ±cap，保留单调性又防止单因子绑架。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return cap * math.tanh(v / cap)


def _dynamic_weights(feat, flow_z=None):
    """根据市场状态动态调整因子权重（动态调整的核心）。

    调整规则（全部确定性、可复现、无未来函数）：
    - 高波动（vol_pct > 80%）：市场噪声大，动量易失效 → MOM 权重 ×0.6，REV ×1.6
    - 低波动（vol_pct < 20%）：趋势更可信 → MOM ×1.2，TRD ×1.2，REV ×0.7
    - 强趋势（|trend_t| > 2）：趋势因子更可靠 → TRD ×1.4
    - 均线强多头/空头（|ma_align| >=2）：趋势确认 → TRD ×1.2，MOM ×1.1
    - 资金流强（|flow_z| > 1.0）：资金主导 → FLOW ×1.3
    - 量能背离（vol_ratio 极端）：VOL 权重下调
    最终归一化到合计 1.0，缺失因子后续再重分配。
    """
    weights = dict(FACTOR_WEIGHTS)  # 拷贝基础权重
    try:
        vol_pct = feat.get("vol_pct")
        if vol_pct is not None:
            if vol_pct >= 0.80:
                weights["mom"] *= 0.6
                weights["rev"] *= 1.6
                weights["vol"] *= 0.9
            elif vol_pct >= 0.60:
                weights["mom"] *= 0.85
                weights["rev"] *= 1.2
            elif vol_pct <= 0.15:
                weights["mom"] *= 1.25
                weights["trd"] *= 1.25
                weights["rev"] *= 0.65
            elif vol_pct <= 0.30:
                weights["mom"] *= 1.1
                weights["trd"] *= 1.1

        t_val = feat.get("trend_t")
        if t_val is not None and abs(t_val) > 2.0:
            weights["trd"] *= 1.4
            # 强趋势下，反转因子应降低（避免过早抄底/逃顶）
            weights["rev"] *= 0.8
        elif t_val is not None and abs(t_val) > 1.2:
            weights["trd"] *= 1.15

        align = feat.get("ma_align")
        if align is not None and abs(align) >= 2:
            weights["trd"] *= 1.2
            weights["mom"] *= 1.1
        elif align is not None and abs(align) >= 1:
            weights["trd"] *= 1.05

        if flow_z is not None and abs(flow_z) > 1.0:
            weights["flow"] *= 1.35
        elif flow_z is not None and abs(flow_z) > 0.5:
            weights["flow"] *= 1.15

        # 量能极端放量且与价格同向 → 量能因子更可信
        vol_z = feat.get("vol_z")
        chg = feat.get("chg_pct")
        if vol_z is not None and chg is not None:
            if abs(vol_z) > 2.0 and abs(chg) > 1.0:
                weights["vol"] *= 1.2
            elif abs(vol_z) < 0.3:
                weights["vol"] *= 0.8

        # 保证权重非负且不过小
        for k in weights:
            weights[k] = max(0.02, weights[k])

    except Exception:
        # 任何异常回落到基础权重，不影响主流程
        weights = dict(FACTOR_WEIGHTS)

    return weights


def factor_scores(feat, *, flow_z=None):
    """五因子打分（动态权重版）。

    ``flow_z``：外部资金流 z（南向资金 / 个股主力净流入的横截面 z），None 时
    该因子缺席，权重按比例重分配给其余因子（不做 0 填充，避免「假中性」）。

    动态调整：根据当前波动率分位、趋势强度、均线排列、资金流强度实时调整
    各因子权重，权重变化会记录在 weights_used 中供页面展示。
    """
    if not feat or not feat.get("ok"):
        return {"ok": False, "score": None, "factors": {},
                "reason": feat.get("reason") or "特征不可用"}

    raw = {}

    # MOM 动量：20 日与 60 日动量 z 的加权（近期权重更高）
    m20, m60 = feat.get("mom_z20"), feat.get("mom_z60")
    if m20 is not None and m60 is not None:
        raw["mom"] = 0.6 * m20 + 0.4 * m60
    else:
        raw["mom"] = m20 if m20 is not None else (0.7 * m60 if m60 is not None else None)

    # TRD 趋势：t 值（显著性）× R²（拟合度）→ t·√R²，方向与斜率一致
    t_val, r2 = feat.get("trend_t"), feat.get("trend_r2")
    if t_val is not None and r2 is not None:
        raw["trd"] = (t_val / 2.0) * math.sqrt(max(0.0, r2))
    elif t_val is not None:
        raw["trd"] = t_val / 3.0
    # 均线排列作为趋势的确认项（±0.6 以内）
    align = feat.get("ma_align")
    if raw.get("trd") is not None and align is not None:
        raw["trd"] = raw["trd"] + 0.2 * align

    # REV 均值回归：偏离均线越远，回归压力越大（超买扣分 / 超卖加分）
    z20 = feat.get("z20")
    raw["rev"] = -1.0 * z20 if z20 is not None else None

    # VOL 量能：放量方向 × 量比强度（放量上涨为正、放量下跌为负）
    vz, chg = feat.get("vol_z"), feat.get("chg_pct")
    if vz is not None:
        direction = 1.0 if (chg or 0) > 0 else (-1.0 if (chg or 0) < 0 else 0.0)
        raw["vol"] = direction * min(abs(vz), 3.0) * 0.6
        # 量价背离修正：价涨量缩 / 价跌量增 都削弱信号
        if (chg or 0) > 0 and vz < -0.5:
            raw["vol"] *= 0.4
        elif (chg or 0) < 0 and vz > 0.5:
            raw["vol"] *= 0.4
    else:
        raw["vol"] = None

    # FLOW 资金流：外部注入（市场级南向资金 z 或个股主力净流入 z）
    raw["flow"] = _tanh_cap(flow_z, 1.5) if flow_z is not None else None

    factors = {k: _tanh_cap(v) for k, v in raw.items()}

    # 动态权重：根据市场状态实时调整
    dyn_weights = _dynamic_weights(feat, flow_z=flow_z)

    # 权重重分配（缺失因子不参与）
    present = {k: v for k, v in factors.items() if v is not None}
    if not present:
        return {"ok": False, "score": None, "factors": factors,
                "reason": "可用因子不足"}
    wsum = sum(dyn_weights[k] for k in present)
    score = sum(dyn_weights[k] * present[k] for k in present) / wsum
    # 综合分再软压缩一次，保证落在 ±2 内、可直接喂给概率层
    score = 2.0 * math.tanh(score / 2.0)

    return {
        "ok": True,
        "score": score,
        "factors": factors,
        "weights_used": {k: dyn_weights[k] / wsum for k in present},
        "weights_base": dict(FACTOR_WEIGHTS),
        "weights_dynamic": dyn_weights,
        "missing": [k for k in FACTOR_WEIGHTS if factors.get(k) is None],
    }


def trend_state(feat):
    """趋势状态判定（上升 / 下跌 / 震荡），返回 (状态, 三态概率, 说明)。

    三态概率由「趋势 t 值 + 均线排列 + 动量方向」确定性合成，三态和为 100%，
    单态最高夹在 85%（趋势判断永远保留被证伪的余地）。
    """
    if not feat or not feat.get("ok"):
        return ("数据不足", {}, "特征不可用")
    t_val = feat.get("trend_t")
    r2 = feat.get("trend_r2") or 0.0
    align = feat.get("ma_align")
    ret20 = feat.get("ret_20")

    # 证据强度：|t|·√R² 越大越像真趋势；再加均线排列的一致性
    strength = 0.0
    if t_val is not None:
        strength = abs(t_val) * math.sqrt(max(0.0, r2))
    if align is not None:
        strength += abs(align) * 0.35
    up = 1 if (t_val or 0) > 0 else -1
    if align is not None and align != 0 and (align > 0) != (up > 0):
        strength *= 0.55          # 斜率与均线排列打架 → 视为震荡
    if ret20 is not None and (ret20 > 0) != (up > 0):
        strength *= 0.7

    # 证据强度 → 单侧优势（0~35 个百分点）
    edge = 35.0 * (1.0 - math.exp(-strength / 2.2))
    side_up = 33.3 + (edge if up > 0 else -edge)
    side_down = 33.3 + (-edge if up > 0 else edge)
    flat = 100.0 - (50.0 + edge) if edge > 0 else 33.3

    # 归一化 + 双端夹紧：单一状态最高 85%、最低 5%，既封顶也保底，
    # 绝不出现「下降概率 0%」这种假确定性（趋势判断永远保留被证伪的余地）。
    probs = {"up": max(0.0, side_up), "down": max(0.0, side_down), "flat": max(0.0, flat)}
    total = sum(probs.values()) or 1.0
    probs = {k: v / total * 100.0 for k, v in probs.items()}
    cap, floor = 85.0, 5.0
    if max(probs.values()) > cap or min(probs.values()) < floor:
        clamped = {k: min(cap, max(floor, v)) for k, v in probs.items()}
        fixed = {k: v for k, v in clamped.items() if v >= cap or v <= floor}
        free = {k: v for k, v in clamped.items() if floor < v < cap}
        if free and sum(free.values()) > 0:
            factor = (100.0 - sum(fixed.values())) / sum(free.values())
            for k in free:
                fixed[k] = min(cap, max(floor, free[k] * factor))
        probs = fixed

    top = max(probs, key=probs.get)
    label = {"up": "上升趋势", "down": "下降趋势", "flat": "震荡整理"}[top]
    return (label, probs, f"60日回归 t={t_val:.2f} · R²={r2:.2f} · 均线排列{align:+d}"
                          if (t_val is not None and align is not None) else "趋势证据不足")


def breadth(stock_rows):
    """港股宽度：由个股池统计站上均线比例等，给市场级宽度分（0~100）。"""
    rows = [r for r in (stock_rows or []) if r.get("feat", {}).get("ok")]
    if not rows:
        return {"available": False, "n": 0}
    n = len(rows)
    above20 = sum(1 for r in rows
                  if r["feat"].get("sma20") and r["feat"]["close"] > r["feat"]["sma20"])
    above60 = sum(1 for r in rows
                  if r["feat"].get("sma60") and r["feat"]["close"] > r["feat"]["sma60"])
    bull_align = sum(1 for r in rows if (r["feat"].get("ma_align") or 0) > 0)
    up20 = sum(1 for r in rows if (r["feat"].get("ret_20") or 0) > 0)
    up1 = sum(1 for r in rows if (r["feat"].get("chg_pct") or 0) > 0)
    score = (0.30 * above20 / n + 0.20 * above60 / n
             + 0.20 * bull_align / n + 0.20 * up20 / n + 0.10 * up1 / n) * 100.0
    return {
        "available": True, "n": n, "score": score,
        "above20_pct": above20 / n * 100.0,
        "above60_pct": above60 / n * 100.0,
        "bull_align_pct": bull_align / n * 100.0,
        "up20_pct": up20 / n * 100.0,
        "up1_pct": up1 / n * 100.0,
    }

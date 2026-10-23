#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""💧 资金流动性层 —— 量化引擎的第 2.5 层（用户重点关注）。

为什么单列一层：**价格是结果，资金是原因。** 港股是离岸市场，边际定价权很大程度
取决于「南向资金 + 本地成交 + 国际盘口深度」三股力量的相对强弱。本层把这三者量化：

  ① 南向 / 北向资金（沪深港通成交总额）
     · 最新值、5/20 日均、环比、20 日 z 值、60 日分位、5 日斜率 t
     · 口径说明：港交所 2024-08-19 起停披每日净买入，只披露成交总额
       → 我们用「成交总额」刻画资金活跃度，**不估算、不编造净流入**
  ② 港股大盘成交与量能
     · 恒指成交量 5 日/20 日比、20 日 z、一年分位 → 放量 / 缩量 / 持平
  ③ 流动性深度
     · Amihud 非流动性 = 均值(|日收益| / 成交额)：数值越大，同样资金推动的价格
       冲击越大（越难出货），是学术与实务通用的流动性缺口度量
     · CR5 / CR20 成交额集中度：资金越扎堆，广度越差
  ④ 流动性综合分（0~100）+ 五档标签 + 对上涨概率的有界修正（±5 个百分点）

全部为确定性计算；任一子块取不到数据就整体缺席该子块，绝不拿旧数字充数。
"""
from __future__ import annotations

import math

from . import stats

# 综合分权重（合计 1.0；缺失项按比例重分配）
LIQ_WEIGHTS = {
    "turnover": 0.30,   # 大盘量能（一年分位）
    "south": 0.25,      # 南向资金活跃度（20 日 z）
    "ratio": 0.15,      # 南向/北向活跃度比（分位）
    "amihud": 0.20,     # Amihud 非流动性（反向分位）
    "cr5": 0.10,        # 成交额集中度（经验阈值，反向）
}

# 集中度经验阈值：留痕历史不足 20 天时退回到这两个阈值，并在页面标注「经验阈值」
CR5_LOOSE, CR5_TIGHT = 30.0, 55.0
MIN_HISTORY_FOR_PCT = 20


# ------------------------------------------------------------------
# 资金流序列统计
# ------------------------------------------------------------------
def flow_stats(pairs, *, windows=(5, 20, 60)):
    """把 [(日期, 亿元)] 加工成一组流动性统计量。"""
    if not pairs:
        return {"available": False}
    vals = [v for _d, v in pairs]
    dates = [d for d, _v in pairs]
    if len(vals) < 5:
        return {"available": False, "n": len(vals)}
    w5, w20, w60 = windows
    ma5 = stats.mean(vals[-w5:])
    ma20 = stats.mean(vals[-w20:])
    ma60 = stats.mean(vals[-w60:]) if len(vals) >= w60 else None
    latest = vals[-1]
    prev = vals[-2] if len(vals) >= 2 else None

    # 20 日 z / 60 日分位
    z20 = stats.zscore(latest, vals[-60:]) if len(vals) >= 20 else None
    pct60 = stats.pct_rank(latest, vals[-min(120, len(vals)):])

    # 5 日斜率（近 10 日的回归 t 值，判断资金是在进场还是退潮）
    seg = vals[-10:] if len(vals) >= 10 else vals
    slope = None
    slope_t = None
    reg = stats.linreg(seg)
    if reg:
        slope = reg["slope"]
        slope_t = reg["t_stat"]

    return {
        "available": True,
        "n": len(vals),
        "date": dates[-1],
        "latest": latest,
        "prev": prev,
        "chg_pct": (latest / prev - 1.0) * 100.0 if prev else None,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "vs_ma5_pct": (latest / ma5 - 1.0) * 100.0 if ma5 else None,
        "vs_ma20_pct": (latest / ma20 - 1.0) * 100.0 if ma20 else None,
        "ma5_vs_ma20_pct": (ma5 / ma20 - 1.0) * 100.0 if (ma5 and ma20) else None,
        "z20": z20,
        "pct60": pct60,
        "slope5": slope,
        "slope5_t": slope_t,
        "max120": max(vals[-min(120, len(vals)):]),
        "min120": min(vals[-min(120, len(vals)):]),
    }


def flow_label(s):
    """给一组资金统计量一句规则化解读（阈值固定，可复现）。"""
    if not s or not s.get("available"):
        return "数据暂缺"
    z = s.get("z20")
    t = s.get("slope5_t")
    if z is None:
        return "样本不足"
    bits = []
    if z >= 2.0:
        bits.append("显著放量")
    elif z >= 0.8:
        bits.append("温和放量")
    elif z <= -2.0:
        bits.append("显著缩量")
    elif z <= -0.8:
        bits.append("温和缩量")
    else:
        bits.append("量能中性")
    if t is not None:
        if t >= 2.0:
            bits.append("近5日持续抬升")
        elif t <= -2.0:
            bits.append("近5日持续回落")
    pct = s.get("pct60")
    if pct is not None:
        bits.append(f"处近120日 {pct*100:.0f}% 分位")
    return " · ".join(bits)


# ------------------------------------------------------------------
# Amihud 非流动性序列
# ------------------------------------------------------------------
def amihud_series(bars, window=20):
    """滚动 Amihud 序列（放大 1e8 便于阅读；值越大流动性越差）。"""
    closes = [b.get("close") for b in bars or []]
    vols = [b.get("volume") for b in bars or []]
    out = []
    for i in range(window, len(closes)):
        vals = []
        for j in range(i - window + 1, i + 1):
            c0, c1 = closes[j - 1], closes[j]
            v = vols[j]
            if not c0 or not c1 or not v:
                continue
            vals.append(abs(c1 / c0 - 1.0) / (v * c1))
        if len(vals) >= window - 5:
            out.append(sum(vals) / len(vals) * 1e8)
    return out


def concentration(top_rows):
    """成交额集中度：CR5 = 前 5 大成交额 / 样本合计。"""
    rows = [r for r in (top_rows or []) if (r.get("amount") or 0) > 0]
    if len(rows) < 10:
        return {"available": False, "n": len(rows)}
    total = sum(r["amount"] for r in rows)
    if total <= 0:
        return {"available": False}
    cr5 = sum(r["amount"] for r in rows[:5]) / total * 100.0
    cr20 = sum(r["amount"] for r in rows[:20]) / total * 100.0
    return {"available": True, "n": len(rows), "cr5": cr5, "cr20": cr20,
            "total_yi": total / 1e8, "top": rows[:5]}


# ------------------------------------------------------------------
# 综合：港股流动性画像
# ------------------------------------------------------------------
def analyze(*, south=None, north=None, index_bars=None, top_turnover=None,
            fundflow=None, index_feat=None, history=None, index_quotes=None):
    """把各路资金 / 成交数据合成一份「流动性画像」。

    ``history``：历次运行留痕的流动性指标序列（来自 ``quant_history.json``），
    用于给「没有自身历史序列」的指标（如成交额集中度 CR5）算分位；
    不足 ``MIN_HISTORY_FOR_PCT`` 条时退回经验阈值，并在 note 中明确标注。

    返回 dict，含 south / north / turnover / depth / components / score / label /
    flow_z / delta_prob / summary / notes；任一子块缺失则该子块 available=False。
    """
    notes = []
    south_s = flow_stats(south or [])
    north_s = flow_stats(north or [])
    if south_s.get("available"):
        notes.append("南向 / 北向为沪深港通**成交总额**（亿元），非净买入：港交所 "
                     "2024-08-19 起停披每日净买入，本页不估算净流入。")

    # ---- 大盘量能 ----
    # 恒指「成交额」取东财口径（元 → 亿元）；Yahoo 的指数成交量单位与真实成交额
    # 不一致，只用于量比 / z 值这类**相对**指标，绝不换算成成交额展示。
    market_amount_yi = None
    if index_quotes:
        quote = (index_quotes.get("HSI") or index_quotes.get("hsi")
                 or (list(index_quotes.values())[0] if index_quotes else None))
        if isinstance(quote, dict) and quote.get("amount"):
            market_amount_yi = quote["amount"] / 1e8

    turnover = {"available": False, "market_amount_yi": market_amount_yi}
    if index_bars and len(index_bars) >= 20:
        vols = [b.get("volume") for b in index_bars]
        closes = [b.get("close") for b in index_bars]
        usable = [v for v in vols if v]
        if len(usable) >= 20 and vols[-1] and closes[-1]:
            last20 = [v for v in vols[-20:] if v]
            mu20 = sum(last20) / len(last20)
            ratio = vols[-1] / mu20 if mu20 else None
            z = stats.zscore(vols[-1], [v for v in vols[-120:] if v]
                             if len([v for v in vols if v]) >= 60 else usable)
            # 量比分位：用滚动量比序列
            rseries = []
            for i in range(20, len(vols)):
                w = [v for v in vols[i - 20:i] if v]
                if len(w) >= 15 and vols[i]:
                    rseries.append(vols[i] / (sum(w) / len(w)))
            turnover = {
                "available": True,
                "volume": vols[-1],
                "market_amount_yi": market_amount_yi,
                "ratio": ratio,
                "z": z,
                "ratio_pct": stats.pct_rank(ratio, rseries) if (ratio and rseries) else None,
                "date": index_bars[-1].get("date"),
            }
            if ratio is not None:
                if ratio >= 1.3:
                    turnover["label"] = "明显放量"
                elif ratio >= 1.08:
                    turnover["label"] = "温和放量"
                elif ratio <= 0.75:
                    turnover["label"] = "明显缩量"
                elif ratio <= 0.92:
                    turnover["label"] = "温和缩量"
                else:
                    turnover["label"] = "量能持平"
            else:
                turnover["label"] = "数据暂缺"
        else:
            notes.append("恒指成交量样本不足，量能子块缺席。")
    else:
        notes.append("未取得恒指日线序列，量能子块缺席。")

    # ---- 深度 ----
    amihud_series_vals = amihud_series(index_bars) if index_bars else []
    depth = {"available": False}
    conc = concentration(top_turnover)
    if amihud_series_vals:
        cur = amihud_series_vals[-1]
        depth = {
            "available": True,
            "amihud": cur,
            "amihud_pct": stats.pct_rank(cur, amihud_series_vals[-120:]),
            "amihud_ma20": stats.mean(amihud_series_vals[-20:]),
            "n": len(amihud_series_vals),
        }
    depth["cr5"] = conc.get("cr5")
    depth["cr20"] = conc.get("cr20")
    depth["cr_n"] = conc.get("n")
    depth["top"] = conc.get("top") or []
    depth["total_yi"] = conc.get("total_yi")
    depth["market_amount_yi"] = market_amount_yi

    # ---- 个股资金流（东财港股主力净流入，字段不保证可用）----
    ff = {"available": False}
    if fundflow:
        items = [(code, v) for code, v in fundflow.items()
                 if v.get("main_net") is not None]
        if items:
            nets = [v["main_net"] for _c, v in items]
            total = sum(nets)
            pos = sum(1 for n in nets if n > 0)
            ff = {
                "available": True,
                "n": len(items),
                "net_total_yi": total / 1e8,
                "pos_pct": pos / len(items) * 100.0,
                "breadth_label": ("资金普遍流入" if pos / len(items) >= 0.6 else
                                  "资金普遍流出" if pos / len(items) <= 0.4 else
                                  "资金分歧"),
            }

    # ---- 综合分：各子项映射到 0~100，缺失项重分配权重 ----
    comps = {}
    if turnover.get("available") and turnover.get("ratio_pct") is not None:
        comps["turnover"] = {
            "score": turnover["ratio_pct"] * 100.0,
            "value": turnover["ratio"],
            "note": f"量比 {turnover['ratio']:.2f}（近一年 {turnover['ratio_pct']*100:.0f}% 分位）",
        }
    if south_s.get("available") and south_s.get("z20") is not None:
        comps["south"] = {
            "score": 50.0 + 30.0 * math.tanh(south_s["z20"] / 2.0),
            "value": south_s["z20"],
            "note": f"南向成交 z={south_s['z20']:+.2f}",
        }
    if (south_s.get("available") and north_s.get("available")
            and south_s.get("latest") and north_s.get("latest")):
        ratio = south_s["latest"] / north_s["latest"]
        ratio_series = [s / n for (_ds, s), (_dn, n) in zip(south or [], north or [])
                        if n]
        if len(ratio_series) >= 20:
            pct = stats.pct_rank(ratio, ratio_series[-120:])
            comps["ratio"] = {
                "score": pct * 100.0,
                "value": ratio,
                "note": f"南向/北向活跃度 {ratio*100:.0f}%（近 {(len(ratio_series))} 日分位 {pct*100:.0f}%）",
            }
    if depth.get("amihud_pct") is not None:
        comps["amihud"] = {
            "score": (1.0 - depth["amihud_pct"]) * 100.0,
            "value": depth["amihud"],
            "note": (f"Amihud 冲击成本 {depth['amihud_pct']*100:.0f}% 分位"
                     f"（越高越差；绝对量级随成交量口径变化，只比分位）"),
        }
    if depth.get("cr5") is not None:
        cr5 = depth["cr5"]
        hist_cr5 = [h.get("cr5") for h in (history or [])
                    if isinstance(h, dict) and h.get("cr5") is not None]
        if len(hist_cr5) >= MIN_HISTORY_FOR_PCT:
            pct = stats.pct_rank(cr5, hist_cr5)
            comps["cr5"] = {
                "score": (1.0 - pct) * 100.0,
                "value": cr5,
                "note": (f"成交额 CR5 {cr5:.1f}%（近 {len(hist_cr5)} 次留痕分位 "
                         f"{pct*100:.0f}%，越高越扎堆）"),
            }
        else:
            span = max(1e-9, CR5_TIGHT - CR5_LOOSE)
            comps["cr5"] = {
                "score": max(0.0, min(100.0, (CR5_TIGHT - cr5) / span * 100.0)),
                "value": cr5,
                "note": (f"成交额 CR5 {cr5:.1f}%（经验阈值 ≤{CR5_LOOSE:.0f}% 宽松 / "
                         f"≥{CR5_TIGHT:.0f}% 扎堆；留痕不足 {MIN_HISTORY_FOR_PCT} 天）"),
            }
        depth["cr5_pct"] = stats.pct_rank(cr5, hist_cr5) if len(hist_cr5) >= MIN_HISTORY_FOR_PCT else None

    score = None
    if comps:
        wsum = sum(LIQ_WEIGHTS[k] for k in comps)
        score = sum(LIQ_WEIGHTS[k] * comps[k]["score"] for k in comps) / wsum
    for k, v in comps.items():
        v["weight"] = LIQ_WEIGHTS[k] / (sum(LIQ_WEIGHTS[x] for x in comps) or 1.0)

    label = _liq_label(score)
    if score is not None and "amihud" not in comps and "cr5" not in comps:
        notes.append("深度类指标（Amihud / 集中度）缺失，综合分只由量能与南北向合成。")

    # ---- 流动性 → 因子分 / 概率修正 ----
    # flow_z（给因子层用的「资金方向」）只取南向资金的 z 值：南向是港股边际定价的
    # 关键变量，且历史可回算，能与历史得分口径完全一致（避免未来函数）。
    flow_z = south_s.get("z20") if south_s.get("available") else None

    # delta_prob（给概率层用的「量能与深度修正」）刻意**排除南向与南北比**，
    # 因为南向已经作为 FLOW 因子计入综合分，重复计入等于双重加权。
    delta_prob = None
    depth_adj = None
    if True:
        parts = []
        if turnover.get("z") is not None:
            parts.append((0.40, math.tanh(turnover["z"] / 1.5)))
        if depth.get("amihud_pct") is not None:
            parts.append((0.35, -1.0 * (2.0 * depth["amihud_pct"] - 1.0)))
        if depth.get("cr5") is not None:
            cr5 = depth["cr5"]
            span = max(1e-9, CR5_TIGHT - CR5_LOOSE)
            parts.append((0.25, 1.0 - 2.0 * max(0.0, min(1.0, (cr5 - CR5_LOOSE) / span))))
        if parts:
            wsum = sum(w for w, _ in parts)
            depth_adj = sum(w * v for w, v in parts) / wsum
            # 修正是「有界的小幅调整」：量在价先，但量能不是方向本身，最多 ±5 个百分点
            delta_prob = max(-0.05, min(0.05, 0.05 * math.tanh(depth_adj / 1.2)))

    available = any([
        south_s.get("available"), north_s.get("available"),
        turnover.get("available"), depth.get("amihud") is not None,
        conc.get("available"), ff.get("available"),
    ])

    return {
        "available": available,
        "south": south_s,
        "north": north_s,
        "turnover": turnover,
        "depth": depth,
        "fundflow": ff,
        "concentration": conc,
        "components": comps,
        "score": score,
        "label": label,
        "flow_z": flow_z,
        "depth_adj": depth_adj,
        "delta_prob": delta_prob,
        "summary": _liq_summary(score, label, south_s, turnover, depth, ff),
        "notes": notes,
        "policy_note": ("港交所自 2024-08-19 起停止披露南北向资金实时 / 每日净买入额，"
                        "仅盘后公布成交总额；本页用成交总额刻画资金活跃度，不估算净买入。"),
    }


def _liq_label(score):
    if score is None:
        return "数据不足"
    if score >= 78:
        return "流动性极度宽松"
    if score >= 62:
        return "流动性偏宽松"
    if score >= 45:
        return "流动性中性"
    if score >= 30:
        return "流动性偏紧"
    return "流动性紧张"


def _liq_summary(score, label, south_s, turnover, depth, ff):
    """一句话流动性解读（规则生成，不引入未展示的数字）。"""
    bits = []
    if score is not None:
        bits.append(f"{label}（综合分 {score:.0f}/100）")
    if south_s.get("available"):
        bits.append(f"南向成交 {south_s['latest']:,.0f} 亿元 · {flow_label(south_s)}")
    if turnover.get("available"):
        bits.append(f"恒指{turnover.get('label', '量能')}（量比 {turnover.get('ratio') or 0:.2f}）")
    if depth.get("amihud_pct") is not None:
        bits.append(f"冲击成本处 {depth['amihud_pct']*100:.0f}% 分位")
    if ff.get("available"):
        bits.append(f"龙头主力{ff['breadth_label']}（{ff['pos_pct']:.0f}% 净流入）")
    return "；".join(bits) + "。" if bits else "流动性数据暂缺。"

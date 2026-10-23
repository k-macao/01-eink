#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🎲 概率层 —— 量化引擎的第 3 层（把因子分校准成真实的上涨概率）。

核心思想：**概率不是拍脑袋给的，是从历史里校准出来的。**

  1. 用同一套因子在历史上滚动重算得分 S_t（只用 t 时刻之前的数据，无未来函数）；
  2. 把 S 分桶，统计每桶未来 h 日「真的涨了」的频率（拉普拉斯收缩，防止小样本极端）；
  3. 用保序回归（PAVA）强制「分越高、上涨频率越高」，再拟合一维逻辑回归做平滑；
  4. 两者融合后输出概率，并夹在 5%~95%（统计上绝不绝对化）；
  5. 最后用**推进式（walk-forward）回测**自己检验自己：每步只用过去校准、预测下一步，
     输出胜率、Brier、对数损失、显著性与校准曲线——无法被自己骗过去。

这一层同样是纯函数（输入序列，输出字典），可离线单元测试。
"""
from __future__ import annotations

import math

from . import stats

# 概率硬边界：统计模型永远保留被证伪的余地
PROB_FLOOR = 0.05
PROB_CAP = 0.95

# 校准最少样本（不足则退回「基准频率」，并如实标注）
MIN_CALIB_SAMPLES = 40
MIN_BLEND_SAMPLES = 70


# ------------------------------------------------------------------
# 未来收益 / 方向标签
# ------------------------------------------------------------------
def forward_outcomes(closes, horizon):
    """未来 h 日方向标签：1=上涨、0=下跌/持平；无法计算的末尾为 None。"""
    closes = [float(c) for c in (closes or []) if c]
    n = len(closes)
    out = [None] * n
    for i in range(max(0, n - horizon)):
        if closes[i] > 0 and closes[i + horizon] > 0:
            out[i] = 1.0 if closes[i + horizon] > closes[i] else 0.0
    return out


def forward_returns(closes, horizon):
    """未来 h 日收益率（百分比）；末尾不足 h 根为 None。"""
    closes = [float(c) for c in (closes or []) if c]
    n = len(closes)
    out = [None] * n
    for i in range(max(0, n - horizon)):
        if closes[i] > 0:
            out[i] = (closes[i + horizon] / closes[i] - 1.0) * 100.0
    return out


# ------------------------------------------------------------------
# 保序回归（PAVA）+ 一维逻辑回归
# ------------------------------------------------------------------
def _pava(values, weights):
    """池相邻违反者算法：把序列压成单调不减的分块，返回 [(值, 权重和, 成员数)]。"""
    blocks = [[float(v), float(w or 1.0), 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            v0, w0, c0 = blocks[i]
            v1, w1, c1 = blocks[i + 1]
            tw = w0 + w1
            blocks[i:i + 2] = [[(v0 * w0 + v1 * w1) / tw, tw, c0 + c1]]
            if i > 0:
                i -= 1
        else:
            i += 1
    return blocks


def fit_logistic(xs, ys, l2=1.0, iters=60):
    """一维逻辑回归（牛顿法 + L2 正则），返回 (截距 a, 斜率 b)。

    ``P(涨) = sigmoid(a + b·S)``，样本不足或不可解返回 None。
    """
    pairs = [(float(x), float(y)) for x, y in zip(xs or [], ys or [])
             if x is not None and y is not None]
    if len(pairs) < 20:
        return None
    a, b = 0.0, 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in pairs:
            z = max(-25.0, min(25.0, a + b * x))
            p = 1.0 / (1.0 + math.exp(-z))
            e = y - p
            ga += -e
            gb += -e * x
            w = p * (1.0 - p)
            haa += w
            hab += w * x
            hbb += w * x * x
        ga += l2 * a
        gb += l2 * b
        haa += l2
        hbb += l2
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a -= da
        b -= db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    return (a, b)


def _logistic_predict(coef, score):
    if not coef:
        return None
    a, b = coef
    z = max(-25.0, min(25.0, a + b * float(score)))
    return 1.0 / (1.0 + math.exp(-z))


def _isotonic_predict(points, score):
    """points = [(x_left, x_right, value)]，块间线性插值（保序、连续）。"""
    if not points:
        return None
    s = float(score)
    if s <= points[0][0]:
        return points[0][2]
    if s >= points[-1][1]:
        return points[-1][2]
    prev = None
    for lo, hi, val in points:
        if s <= hi:
            if prev is None:
                return val
            p_lo, p_hi, p_val = prev
            if p_hi <= p_lo:
                return val
            t = (s - p_hi) / (hi - p_hi)
            return p_val + (val - p_val) * t
        prev = (lo, hi, val)
    return points[-1][2]


class Calibrator:
    """因子分 → 上涨概率的校准器（保序 + 逻辑回归融合）。

    method:
      ``base``  —— 样本不足，只能给历史基准频率（页面会明确标注「样本不足」）；
      ``iso``   —— 仅保序回归（样本中等，避免逻辑回归过拟合）；
      ``blend`` —— 保序 + 逻辑回归各半（样本充足时的默认口径）。
    """

    def __init__(self, base, n, method, points=None, coef=None,
                 buckets=None, prior=0.0):
        self.base = base
        self.n = n
        self.method = method
        self.points = points or []
        self.coef = coef
        self.buckets = buckets or []
        self.prior = prior

    def predict(self, score):
        if score is None:
            return None
        p = None
        if self.method == "base":
            p = self.base
        elif self.method == "iso":
            p = _isotonic_predict(self.points, score)
        else:
            p_iso = _isotonic_predict(self.points, score)
            p_log = _logistic_predict(self.coef, score)
            if p_iso is None and p_log is None:
                p = self.base
            elif p_iso is None:
                p = p_log
            elif p_log is None:
                p = p_iso
            else:
                p = 0.5 * p_iso + 0.5 * p_log
        if p is None:
            return None
        return max(PROB_FLOOR, min(PROB_CAP, p))

    def as_dict(self):
        return {"base": self.base, "n": self.n, "method": self.method,
                "buckets": self.buckets, "coef": self.coef}


def fit_calibrator(scores, outcomes, *, prior=12.0, max_buckets=6):
    """用历史 (因子分, 未来方向) 拟合校准器。"""
    pairs = [(float(s), float(o)) for s, o in zip(scores or [], outcomes or [])
             if s is not None and o is not None]
    n = len(pairs)
    base = (sum(o for _, o in pairs) / n) if n else 0.5
    if n < MIN_CALIB_SAMPLES:
        return Calibrator(base, n, "base", prior=prior)

    pairs.sort(key=lambda p: p[0])
    k = max(3, min(max_buckets, n // 25))
    size = n / k
    centers, values, weights, buckets = [], [], [], []
    for i in range(k):
        lo, hi = int(round(i * size)), int(round((i + 1) * size))
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        m = len(chunk)
        hits = sum(o for _, o in chunk)
        center = sum(s for s, _ in chunk) / m
        # 拉普拉斯收缩：小样本向基准频率收敛，避免单桶 0% / 100% 的假确定性
        p = (hits + prior * base) / (m + prior)
        centers.append(center)
        values.append(p)
        weights.append(float(m))
        buckets.append({"center": center, "n": m, "hits": hits, "prob": p})

    blocks = _pava(values, weights)
    points, idx = [], 0
    for _v, _w, cnt in blocks:
        group_centers = centers[idx:idx + cnt]
        idx += cnt
        points.append((min(group_centers), max(group_centers), _v))
    # 端点延伸到 ±∞ 邻域，保证任意得分都能插值
    if points:
        points[0] = (-1e9, points[0][1], points[0][2])
        points[-1] = (points[-1][0], 1e9, points[-1][2])

    coef = fit_logistic([s for s, _ in pairs], [o for _, o in pairs])
    method = "blend" if (n >= MIN_BLEND_SAMPLES and coef) else "iso"
    return Calibrator(base, n, method, points=points, coef=coef,
                      buckets=buckets, prior=prior)


# ------------------------------------------------------------------
# 推进式（walk-forward）回测：每步只用过去，预测下一步
# ------------------------------------------------------------------
def walk_forward(scores, closes, horizons=(1, 5), min_train=60, max_points=260):
    """严格的推进式校验：t 时刻的校准只用 < t 的数据，杜绝未来函数。

    返回 ``{h: {n, base_rate, hit_rate, z, p_value, brier, log_loss,
               mean_ret, mean_ret_up, mean_ret_down, reliability}}``
    """
    result = {}
    scores = list(scores or [])
    closes = [float(c) for c in (closes or []) if c]
    n = min(len(scores), len(closes))
    if n < min_train + 5:
        return result
    start = max(min_train, n - max_points)

    for h in horizons:
        outcomes = forward_outcomes(closes, h)
        fwd_rets = forward_returns(closes, h)
        probs, outs, rets, dirs = [], [], [], []
        for t in range(start, n - h):
            s_t = scores[t]
            o_t = outcomes[t]
            if s_t is None or o_t is None:
                continue
            cal = fit_calibrator(scores[:t], outcomes[:t])
            p = cal.predict(s_t)
            if p is None:
                continue
            probs.append(p)
            outs.append(o_t)
            rets.append(fwd_rets[t])
            dirs.append(1.0 if p >= 0.5 else 0.0)

        if len(probs) < 20:
            continue
        hits = sum(1 for d, o in zip(dirs, outs) if d == o)
        brier = stats.brier(probs, outs)
        ll = stats.log_loss(probs, outs)
        z = stats.binomial_z(hits, len(outs))
        p_value = 2.0 * (1.0 - stats.norm_cdf(abs(z))) if z is not None else None
        up_rets = [r for r, d in zip(rets, dirs) if d == 1.0 and r is not None]
        dn_rets = [r for r, d in zip(rets, dirs) if d == 0.0 and r is not None]

        result[h] = {
            "n": len(probs),
            "base_rate": sum(outs) / len(outs),
            "hit_rate": hits / len(outs),
            "z": z,
            "p_value": p_value,
            "brier": brier,
            "log_loss": ll,
            "mean_ret": stats.mean(rets),
            "mean_ret_when_up": stats.mean(up_rets),
            "mean_ret_when_down": stats.mean(dn_rets),
            "reliability": reliability_curve(probs, outs),
        }
    return result


def reliability_curve(probs, outcomes, bins=5):
    """校准（可靠性）曲线：预测概率分 5 档，看实际频率是否跟得上。

    理想情况两者相等；偏离越大说明概率被系统性高估 / 低估。
    """
    pairs = [(p, o) for p, o in zip(probs or [], outcomes or [])
             if p is not None and o is not None]
    if len(pairs) < 20:
        return []
    pairs.sort(key=lambda x: x[0])
    size = len(pairs) / bins
    curve = []
    for i in range(bins):
        chunk = pairs[int(round(i * size)):int(round((i + 1) * size))]
        if not chunk:
            continue
        curve.append({
            "lo": min(p for p, _ in chunk),
            "hi": max(p for p, _ in chunk),
            "n": len(chunk),
            "pred": sum(p for p, _ in chunk) / len(chunk),
            "actual": sum(o for _, o in chunk) / len(chunk),
        })
    return curve


# ------------------------------------------------------------------
# 区间预测
# ------------------------------------------------------------------
def forecast_bands(close, log_rets, horizon, *, vol_override=None, shrink=0.5):
    """给定期限的对数正态区间预测。

    ``shrink``：把样本均值向 0 收缩（漂移项噪声极大，全用会严重过拟合）。
    返回 dict(mu_pct, sigma_pct, lo68, hi68, lo95, hi95, p_up_normal, p_move_1pct)。
    """
    if close is None or close <= 0 or not log_rets:
        return None
    rs = [r for r in log_rets if r is not None]
    if len(rs) < 20:
        return None
    mu_d = stats.mean(rs[-min(120, len(rs)):]) or 0.0
    sd_d = vol_override if vol_override else (stats.ewma_vol(rs, annualize=False)
                                              or stats.stdev(rs))
    if not sd_d:
        return None
    mu_h = mu_d * shrink * horizon
    sd_h = sd_d * math.sqrt(horizon)

    def _lvl(z):
        return close * math.exp(mu_h + z * sd_h)

    return {
        "mu_pct": (math.exp(mu_h) - 1.0) * 100.0,
        "sigma_pct": sd_h * 100.0,
        "lo68": _lvl(-1.0), "hi68": _lvl(1.0),
        "lo95": _lvl(-1.96), "hi95": _lvl(1.96),
        "p_up_normal": stats.norm_cdf(mu_h / sd_h) if sd_h > 0 else None,
        "p_up_1pct": (1.0 - stats.norm_cdf((0.01 - mu_h) / sd_h)) if sd_h > 0 else None,
        "p_down_1pct": stats.norm_cdf((-0.01 - mu_h) / sd_h) if sd_h > 0 else None,
    }


def summarize_validation(validation, horizon=1):
    """回测结果 → 一段人类可读的结论（页面上「模型可信度」用）。"""
    row = (validation or {}).get(horizon)
    if not row:
        return "样本不足，暂不给出可信度结论"
    hit, base = row["hit_rate"], row["base_rate"]
    z = row.get("z")
    sig = ""
    if z is not None:
        if abs(z) >= 1.96:
            sig = "（显著优于抛硬币，p<0.05）"
        elif abs(z) >= 1.28:
            sig = "（弱显著，p<0.20）"
        else:
            sig = "（与抛硬币无显著差异，请按概率而非确定性使用）"
    brier = row.get("brier")
    br_note = ""
    if brier is not None:
        br_note = f" · Brier {brier:.3f}（0.25=瞎猜）"
    return (f"近 {row['n']} 个交易日推进式校验：方向命中 {hit*100:.1f}%"
            f"（基准 {base*100:.1f}%）{sig}{br_note}")

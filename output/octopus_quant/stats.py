#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🧮 统计内核 —— 量化引擎的第 0 层。

设计契约（全模块遵守，便于离线回归）：
  · 纯函数：不联网、不读文件、不依赖全局状态，同样输入必然同样输出；
  · 容错：样本不足或无法计算时一律返回 ``None``，绝不返回猜测值 / 兜底数字；
  · 无第三方依赖：只用标准库（``math`` / ``statistics``），无需 numpy。

上层分层：stats → features → probability / liquidity → engine → render
"""
from __future__ import annotations

import math
from statistics import NormalDist

_ND = NormalDist()

# 港股每年交易日数（年化换算用）
TRADING_DAYS = 252


# ------------------------------------------------------------------
# 基础描述统计
# ------------------------------------------------------------------
def _clean(xs):
    """丢掉 None / NaN / inf；列表为空时返回 []。"""
    out = []
    for x in xs or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        out.append(v)
    return out


def mean(xs):
    xs = _clean(xs)
    return sum(xs) / len(xs) if xs else None


def stdev(xs, ddof=1):
    """样本标准差（默认无偏）；n < ddof+1 时返回 None。"""
    xs = _clean(xs)
    if len(xs) <= ddof:
        return None
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - ddof)
    return math.sqrt(var) if var > 0 else 0.0


def variance(xs, ddof=1):
    sd = stdev(xs, ddof=ddof)
    return sd ** 2 if sd is not None else None


def zscore(value, xs):
    """``value`` 在样本 ``xs`` 中的 z 值；样本不足或零方差返回 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    mu, sd = mean(xs), stdev(xs)
    if mu is None or not sd:
        return None
    return (v - mu) / sd


def pct_rank(value, xs):
    """``value`` 在样本中的分位（0~1，含自身折半，避免 0 / 1 极端值）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    xs = _clean(xs)
    if not xs:
        return None
    below = sum(1 for x in xs if x < v)
    equal = sum(1 for x in xs if x == v)
    return (below + 0.5 * equal) / len(xs)


def percentile(xs, q):
    """线性插值分位数；q ∈ [0,1]。"""
    xs = sorted(_clean(xs))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    q = min(1.0, max(0.0, float(q)))
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def corr(a, b):
    """皮尔逊相关系数；样本 < 3 或零方差返回 None。"""
    a, b = _clean(a), _clean(b)
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return None
    return num / (da * db)


# ------------------------------------------------------------------
# 时间序列
# ------------------------------------------------------------------
def simple_returns(prices):
    """简单收益率序列（长度 = len(prices)-1，百分比）。"""
    ps = _clean(prices)
    return [(ps[i] / ps[i - 1] - 1.0) * 100.0 for i in range(1, len(ps))
            if ps[i - 1] != 0]


def log_returns(prices):
    """对数收益率序列（小数，非百分比）；用于波动率与区间预测。"""
    ps = _clean(prices)
    out = []
    for i in range(1, len(ps)):
        if ps[i] > 0 and ps[i - 1] > 0:
            out.append(math.log(ps[i] / ps[i - 1]))
    return out


def sma(xs, n):
    """n 日简单均线序列（与输入右对齐，前 n-1 位为 None）。"""
    xs = _clean(xs)
    if n <= 0 or len(xs) < n:
        return [None] * len(xs)
    out, acc = [], 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        out.append(acc / n if i >= n - 1 else None)
    return out


def ema(xs, n):
    """指数均线（α = 2/(n+1)），前 n-1 位为 None。"""
    xs = _clean(xs)
    if n <= 0 or len(xs) < n:
        return [None] * len(xs)
    alpha = 2.0 / (n + 1.0)
    out, prev = [None] * len(xs), None
    seed = sum(xs[:n]) / n
    for i, x in enumerate(xs):
        if i < n - 1:
            continue
        if i == n - 1:
            prev = seed
        else:
            prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def wilder(xs, n):
    """Wilder 平滑（RMA，RSI / ATR 用），α = 1/n。"""
    xs = _clean(xs)
    if n <= 0 or len(xs) < n:
        return [None] * len(xs)
    out, prev = [None] * len(xs), None
    for i, x in enumerate(xs):
        if i < n - 1:
            continue
        if i == n - 1:
            prev = sum(xs[:n]) / n
        else:
            prev = prev + (x - prev) / n
        out[i] = prev
    return out


def realized_vol(returns, annualize=True, days=TRADING_DAYS):
    """已实现波动率；``returns`` 为百分比收益率时返回年化百分比。"""
    sd = stdev(returns)
    if sd is None:
        return None
    return sd * math.sqrt(days) if annualize else sd


def ewma_vol(returns, lam=0.94, annualize=True, days=TRADING_DAYS):
    """RiskMetrics EWMA 波动率（λ=0.94）：对近期冲击更敏感，做预测更合适。"""
    rs = _clean(returns)
    if len(rs) < 3:
        return None
    var = variance(rs[:max(5, len(rs) // 4)]) or 0.0
    for r in rs:
        var = lam * var + (1 - lam) * r * r
    if var <= 0:
        return None
    sd = math.sqrt(var)
    return sd * math.sqrt(days) if annualize else sd


def rolling(xs, n):
    """滚动窗口列表（右对齐；不足 n 位的前段不产出）。"""
    xs = _clean(xs)
    return [xs[i - n + 1:i + 1] for i in range(n - 1, len(xs))]


def rolling_apply(xs, n, fn):
    return [fn(w) for w in rolling(xs, n)]


def rsi(prices, n=14):
    """Wilder RSI；样本不足返回 None。"""
    ps = _clean(prices)
    if len(ps) < n + 1:
        return None
    diffs = [ps[i] - ps[i - 1] for i in range(1, len(ps))]
    gains = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]
    ag, al = wilder(gains, n), wilder(losses, n)
    if ag[-1] is None or al[-1] is None:
        return None
    if al[-1] <= 0:
        return 100.0
    rs = ag[-1] / al[-1]
    return 100.0 - 100.0 / (1.0 + rs)


def macd_series(prices, fast=12, slow=26, signal=9):
    """因果 MACD 序列，与清洗后的价格对齐；每项为 (DIF, DEA, DIF−DEA)。

    EMA 用首个窗口的 SMA 播种；沿用 macd 的 slow+signal 根预热门槛。
    hist 保留原有「单倍差值」口径，国内双倍柱由呈现 / 策略层显式换算。
    """
    ps = _clean(prices)
    empty = (None, None, None)
    out = [empty] * len(ps)
    if min(fast, slow, signal) <= 0 or len(ps) < slow + signal:
        return out
    ef, es = ema(ps, fast), ema(ps, slow)
    dif = [(a - b) if (a is not None and b is not None) else None
           for a, b in zip(ef, es)]
    indices = [i for i, d in enumerate(dif) if d is not None]
    dea = ema([dif[i] for i in indices], signal)
    for i, avg in zip(indices, dea):
        if i >= slow + signal - 1 and avg is not None:
            out[i] = (dif[i], avg, dif[i] - avg)
    return out


def macd(prices, fast=12, slow=26, signal=9):
    """MACD 最新值 (DIF, DEA, DIF−DEA)；样本不足返回三个 None。"""
    series = macd_series(prices, fast, slow, signal)
    return series[-1] if series else (None, None, None)


def bollinger(prices, n=20, k=2.0):
    """布林带；返回 dict(mid, upper, lower, pctb, width_pct)。"""
    ps = _clean(prices)
    if len(ps) < n:
        return {"mid": None, "upper": None, "lower": None,
                "pctb": None, "width_pct": None}
    win = ps[-n:]
    mid = sum(win) / n
    sd = stdev(win) or 0.0
    upper, lower = mid + k * sd, mid - k * sd
    pctb = (ps[-1] - lower) / (upper - lower) if upper > lower else None
    return {"mid": mid, "upper": upper, "lower": lower,
            "pctb": pctb, "width_pct": (upper - lower) / mid * 100.0 if mid else None}


def atr(bars, n=14):
    """真实波幅 ATR（bars 含 high/low/close）；返回 (atr, atr/收盘价%)。"""
    try:
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return (None, None)
    if len(closes) < n + 1:
        return (None, None)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    smoothed = wilder(trs, n)
    if not smoothed or smoothed[-1] is None:
        return (None, None)
    value = smoothed[-1]
    return (value, value / closes[-1] * 100.0 if closes[-1] else None)


def linreg(ys):
    """对序列做最小二乘 ``y = a + b·t``。

    返回 dict(slope, intercept, r2, t_stat, n)；样本 < 3 或零方差返回 None。
    ``t_stat`` 是斜率的 t 值，用来判断「趋势是否显著」，是本引擎趋势因子的核心。
    """
    ys = _clean(ys)
    n = len(ys)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    sse = sum(r * r for r in resid)
    sst = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    if se_raw := (math.sqrt(sse / (n - 2) / sxx) if n > 2 and sse > 0 else 0.0):
        t_stat = slope / se_raw
    else:
        # 完美拟合（残差为 0）时 t 值趋于无穷，钳到 ±50，避免下游拿到 None / inf
        t_stat = 0.0 if slope == 0 else (50.0 if slope > 0 else -50.0)
    t_stat = max(-50.0, min(50.0, t_stat))
    return {"slope": slope, "intercept": intercept, "r2": r2,
            "t_stat": t_stat, "n": n}


def autocorr(xs, lag=1):
    """滞后自相关（判断动量 / 反转属性）。"""
    xs = _clean(xs)
    n = len(xs)
    if n < lag + 3:
        return None
    mu = sum(xs) / n
    denom = sum((x - mu) ** 2 for x in xs)
    if denom <= 0:
        return None
    num = sum((xs[i] - mu) * (xs[i - lag] - mu) for i in range(lag, n))
    return num / denom


def streak(prices):
    """连续涨 / 跌天数（含当日；正=连涨，负=连跌）。"""
    ps = _clean(prices)
    if len(ps) < 2:
        return 0
    sign = (ps[-1] > ps[-2]) - (ps[-1] < ps[-2])
    if sign == 0:
        return 0
    cnt = 0
    for i in range(len(ps) - 1, 0, -1):
        s = (ps[i] > ps[i - 1]) - (ps[i] < ps[i - 1])
        if s == sign:
            cnt += 1
        else:
            break
    return cnt if sign > 0 else -cnt


# ------------------------------------------------------------------
# 分布与概率
# ------------------------------------------------------------------
def norm_cdf(x):
    return _ND.cdf(x)


def norm_ppf(p):
    """标准正态分位函数（95% 区间边界用）。"""
    try:
        p = min(1.0 - 1e-9, max(1e-9, float(p)))
    except (TypeError, ValueError):
        return None
    return _ND.inv_cdf(p)


def prob_above(mu, sigma, threshold):
    """P(X > threshold)，X ~ N(mu, sigma)；sigma<=0 返回 None。"""
    if sigma is None or sigma <= 0:
        return None
    return 1.0 - norm_cdf((threshold - mu) / sigma)


def clamp(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


# ------------------------------------------------------------------
# 回测 / 校验指标
# ------------------------------------------------------------------
def max_drawdown(equity):
    """最大回撤（输入为净值序列，返回负数百分比）。"""
    eq = _clean(equity)
    if len(eq) < 2:
        return None
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd * 100.0


def sharpe(returns, periods=TRADING_DAYS):
    """年化夏普（无风险利率取 0，returns 为日收益百分比）。"""
    rs = _clean(returns)
    if len(rs) < 3:
        return None
    mu, sd = mean(rs), stdev(rs)
    if not sd:
        return None
    return (mu / sd) * math.sqrt(periods)


def sortino(returns, periods=TRADING_DAYS):
    """年化索提诺（只用下行波动）。"""
    rs = _clean(returns)
    if len(rs) < 3:
        return None
    mu = mean(rs)
    downside = [min(r, 0.0) for r in rs]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside))
    if dd <= 0:
        return None
    return (mu / dd) * math.sqrt(periods)


def binomial_z(hits, n, p0=0.5):
    """方向胜率相对基准 p0 的显著性 z 值（正态近似）。"""
    if not n:
        return None
    p = hits / n
    denom = math.sqrt(p0 * (1 - p0) / n)
    if denom <= 0:
        return None
    return (p - p0) / denom


def brier(probs, outcomes):
    """Brier 分数（概率校准质量，越小越好；完美 = 0，瞎猜 0.5 = 0.25）。"""
    pairs = [(p, o) for p, o in zip(probs or [], outcomes or [])
             if p is not None and o is not None]
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(probs, outcomes, eps=1e-6):
    """对数损失（越小心越好；无信息基准 = ln2 ≈ 0.693）。"""
    pairs = [(p, o) for p, o in zip(probs or [], outcomes or [])
             if p is not None and o is not None]
    if not pairs:
        return None
    total = 0.0
    for p, o in pairs:
        p = min(1 - eps, max(eps, p))
        total -= (o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(pairs)


def hit_rate(preds, outcomes):
    """方向命中率（preds 为布尔 / 0-1 的方向预测）。"""
    pairs = [(p, o) for p, o in zip(preds or [], outcomes or [])
             if p is not None and o is not None]
    if not pairs:
        return None
    return sum(1 for p, o in pairs if bool(p) == bool(o)) / len(pairs)

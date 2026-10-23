#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MACD 派生研究信号：分工而非独立投票，所有确认时点只读取当时已有数据。

水上/水下过滤沿用 macd_strategy 的严格双线版本，不重复建立第五票。
连续柱收敛、确认底背离、布林回归是预警/观察；周日共振是顺势过滤。
不估计胜率、不下单、不覆写基础动作或五因子概率。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import providers, stats

SLOPE_N = 3
EXTREME_WINDOW = 60
EXTREME_MIN = 30
PRICE_MOVE_WINDOW = 20
PRICE_MOVE_PCT = 5.0
MIN_SLOPE_BARS = 35 + EXTREME_MIN + SLOPE_N  # 68：极值前至少 30 个已预热 MACD
PIVOT_SIDE = 2
PIVOT_MATCH = 3
PIVOT_CONFIRM_LAG = PIVOT_SIDE + PIVOT_MATCH  # 匹配窗口固定后才能确认，不能回填
PIVOT_MIN_GAP = 5
PIVOT_MAX_GAP = 60
DIVERGENCE_WINDOW = 120
DIVERGENCE_MIN = 35 + PIVOT_MIN_GAP + PIVOT_CONFIRM_LAG  # 45
DIVERGENCE_PRICE_PCT = 0.5
DIVERGENCE_TTL = 10
BOLL_N, BOLL_K = 20, 2.0
BOLL_SLOPE_DAYS = 5
BOLL_FLAT_PCT = 1.0
WEEKLY_MIN = 35
WEEKLY_MAX_LAG = 14
KEYS = ("slope", "divergence", "bollinger", "timeframe")
NAMES = {"slope": "连续柱体收敛", "divergence": "确认底背离",
         "bollinger": "布林回归", "timeframe": "周日共振"}


def _finite(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and (not positive or number > 0) else None


def _record(key, summary="未触发", **fields):
    return {"key": key, "name": NAMES[key], "available": True,
            "active": False, "triggered": False, "signal": None,
            "summary": summary, "detail": "", "role": "观察", **fields}


def _missing(key, reason, *, summary="数据不足"):
    return _record(key, summary, available=False, reason=reason)


def _hist(sequence):
    return [2 * point[2] if point[2] is not None and _finite(point[2]) is not None else None
            for point in sequence]


def histogram_reversal(bars, sequence):
    """原值判连续斜率，自身过去 H/close 分位判极值；极值分布不含极值当天。

    恰好第 3 根收敛才是新预警；第 4 根以后是延续，绝不重复报新买卖点。
    极值日必须有过去 20 根收盘高/低点到当日 ≥5% 的价格移动。
    """
    if len(bars) < MIN_SLOPE_BARS:
        return _missing("slope", f"连续收敛/极值样本不足（{len(bars)}/{MIN_SLOPE_BARS} 根）")
    closes = [b["close"] for b in bars]
    hist = _hist(sequence)
    eps = max(closes[-1], 1.0) * 1e-10
    current = hist[-1]
    if current is None:
        return _missing("slope", "最新柱体无有限值")
    green, red = current < -eps, current > eps
    run = 0
    if green or red:
        for i in range(len(hist) - 1, 0, -1):
            a, b = hist[i - 1], hist[i]
            if a is None or b is None:
                break
            same_side = (a < -eps and b < -eps) if green else (a > eps and b > eps)
            shrinking = b > a + eps if green else b < a - eps
            if not same_side or not shrinking:
                break
            run += 1
    anchor = len(hist) - 1 - run if run else len(hist) - 1 - SLOPE_N
    baseline = [hist[i] / closes[i] * 100 for i in range(max(0, anchor - EXTREME_WINDOW), anchor)
                if hist[i] is not None]
    if len(baseline) < EXTREME_MIN:
        return _missing("slope", f"收敛起点前历史柱体不足（{len(baseline)}/{EXTREME_MIN} 个）")
    anchor_h = hist[anchor]
    if anchor_h is None:
        return _missing("slope", "收敛起点尚未预热")
    normalized = anchor_h / closes[anchor] * 100
    q10, q90 = stats.percentile(baseline, 0.1), stats.percentile(baseline, 0.9)
    price_window = closes[max(0, anchor - PRICE_MOVE_WINDOW + 1):anchor + 1]
    drawdown = (closes[anchor] / max(price_window) - 1) * 100
    advance = (closes[anchor] / min(price_window) - 1) * 100
    qualifies = (green and normalized <= q10 and drawdown <= -PRICE_MOVE_PCT) or (
        red and normalized >= q90 and advance >= PRICE_MOVE_PCT)
    active = run >= SLOPE_N and qualifies
    stage = ("反弹预警" if green else "减仓预警") if run == SLOPE_N else "预警延续"
    summary = f"{'绿' if green else '红'}收 {run} 根·{stage}" if active else "未触发"
    return _record("slope", summary, active=active, triggered=active and run == SLOPE_N,
                   signal=("rebound_warning" if green else "trim_warning") if active else None,
                   role="动能预警", streak=run, anchor_date=bars[anchor]["date"],
                   hist_pct=normalized, q10=q10, q90=q90, baseline_n=len(baseline),
                   drawdown_pct=drawdown, advance_pct=advance,
                   detail=f"收敛 {run} 根；起点 H/价 {normalized:+.4f}%（此前 {len(baseline)} 个柱体的 P10={q10:+.4f}% / P90={q90:+.4f}%）；20 根回撤 {drawdown:+.2f}% / 上涨 {advance:+.2f}%")


def _low_values(bars):
    """只接受真实且与收盘关系合理的 K 线最低价，不用 close 冒充 low。"""
    values = []
    for bar in bars:
        low = _finite(bar.get("low"), positive=True)
        values.append(low if low is not None and low <= bar["close"] * (1 + 1e-8) else None)
    return values


def _pivots(values, *, first=0):
    """严格局部谷；左右各 2 根有效，平台不冒充多个波谷。只返回已确认的谷。"""
    result = []
    for i in range(max(first, PIVOT_SIDE), len(values) - PIVOT_SIDE):
        window = values[i - PIVOT_SIDE:i + PIVOT_SIDE + 1]
        if any(v is None or _finite(v) is None for v in window):
            continue
        if all(values[i] < v for j, v in enumerate(window) if j != PIVOT_SIDE):
            result.append(i)
    return result


def _zero_path(dif, first, second, eps):
    """两价格低点间曾下穿零轴，随后至少一次回升；不强制回到零轴上方。"""
    crossed = False
    for i in range(first + 1, second + 1):
        a, b = dif[i - 1], dif[i]
        if a is None or b is None:
            continue
        if a >= -eps and b < -eps:
            crossed = True
        elif crossed and b > a + eps:
            return True
    return False


def divergence_events(bars, sequence):
    """确定性事件流，确认后不重匹配：价格谷 p 在 p+5 才固定其 ±3 的 DIF 谷。

    只比较相邻价格谷的匹配，DIF 谷须不同且按时间顺序。事件日期为确认日，
    不是最低价日。最低价覆盖门槛同样在确认时刻判断，不能靠未来数据补过。
    """
    lows = _low_values(bars)
    dif = [point[0] if point[0] is not None and _finite(point[0]) is not None else None
           for point in sequence]
    prices = _pivots(lows, first=34)
    momentum = _pivots(dif, first=34)
    matches = []
    for p in prices:
        confirmation = p + PIVOT_CONFIRM_LAG
        if confirmation >= len(bars):
            continue
        candidates = [d for d in momentum if abs(d - p) <= PIVOT_MATCH
                      and d + PIVOT_SIDE <= confirmation]
        matched = min(candidates, key=lambda d: (abs(d - p), d)) if candidates else None
        matches.append((p, matched, confirmation))
    events = []
    for (p1, d1, _), (p2, d2, confirmed) in zip(matches, matches[1:]):
        if d1 is None or d2 is None or d2 <= d1 or not PIVOT_MIN_GAP <= p2 - p1 <= PIVOT_MAX_GAP:
            continue
        if p1 < confirmed - DIVERGENCE_WINDOW + 1:
            continue
        valid_lows = sum(v is not None for v in lows[max(0, confirmed - DIVERGENCE_WINDOW + 1):confirmed + 1])
        if valid_lows < DIVERGENCE_MIN:
            continue
        # 在等待 DIF 匹配窗口的 5 根内已破第二低点，不能到确认日还报抄底形态。
        if any(v is None or v < lows[p2] for v in lows[p2 + 1:confirmed + 1]):
            continue
        eps = max(bars[confirmed]["close"], 1.0) * 1e-10
        if (lows[p2] <= lows[p1] * (1 - DIVERGENCE_PRICE_PCT / 100)
                and dif[d1] < -eps and dif[d2] < -eps and dif[d2] > dif[d1] + eps):
            events.append({"p1": p1, "p2": p2, "d1": d1, "d2": d2, "confirmed": confirmed,
                           "price1": lows[p1], "price2": lows[p2], "dif1": dif[d1], "dif2": dif[d2],
                           "zero_path": _zero_path(dif, p1, p2, eps)})
    return events


def bottom_divergence(bars, sequence):
    window = bars[-DIVERGENCE_WINDOW:]
    valid = sum(v is not None for v in _low_values(window))
    if len(bars) < DIVERGENCE_MIN or valid < DIVERGENCE_MIN:
        return _missing("divergence", f"已验证最低价不足（{valid}/{DIVERGENCE_MIN} 根），不以收盘价代替 K 线波谷")
    events = [event for event in divergence_events(bars, sequence)
              if event["p1"] >= len(bars) - DIVERGENCE_WINDOW]
    if not events:
        return _record("divergence", "无确认形态", role="形态观察", window_n=len(window),
                       detail=f"检查最近 {len(window)} 根日线；左右确认与 DIF ±{PIVOT_MATCH} 根匹配后未检出底背离")
    event = events[-1]
    age = len(bars) - 1 - event["confirmed"]
    subsequent = _low_values(bars[event["confirmed"] + 1:])
    invalidated = any(v is not None and v < event["price2"] for v in subsequent)
    active = not invalidated and age <= DIVERGENCE_TTL
    summary = "底背离已失效" if invalidated else (
        "底背离已过期" if age > DIVERGENCE_TTL else "底背离·仅观察")
    confirmation_date = bars[event["confirmed"]]["date"]
    return _record("divergence", summary, active=active, triggered=active and age == 0,
                   signal="bullish_divergence" if active else None, role="形态观察",
                   price1_date=bars[event["p1"]]["date"], price2_date=bars[event["p2"]]["date"],
                   dif1_date=bars[event["d1"]]["date"], dif2_date=bars[event["d2"]]["date"],
                   confirmed_at=confirmation_date, bars_ago=age, invalidated=invalidated,
                   price1=event["price1"], price2=event["price2"], dif1=event["dif1"], dif2=event["dif2"],
                   zero_path=event["zero_path"], window_n=len(window),
                   detail=f"P1 {event['price1']:.4f}（{bars[event['p1']]['date']}）→ P2 {event['price2']:.4f}（{bars[event['p2']]['date']}）；匹配 DIF {event['dif1']:+.4f}→{event['dif2']:+.4f}；确认 {confirmation_date}，距今 {age} 根；零轴下穿后回升{'有' if event['zero_path'] else '无'}（非必要条件）")


def bollinger_reversion(bars, sequence, base):
    """冻结前一日布林带；触下轨后收回轨内 + 动能修复 + 平缓中轨才报观察。

    中轨 5 根变化绝对值 ≤1% 是研究性震荡代理，不是标准趋势强度模型。
    上轨止盈只针对该均值回归分支，强趋势沿上轨不强制卖出。
    """
    if len(bars) < BOLL_N + BOLL_SLOPE_DAYS + 1:
        return _missing("bollinger", "布林带与中轨斜率样本不足")
    closes = [b["close"] for b in bars]
    low = _low_values(bars[-1:])[0]
    high = _finite(bars[-1].get("high"), positive=True)
    if low is None or high is None or high < closes[-1] * (1 - 1e-8) or high < low:
        return _missing("bollinger", "缺少可验证当日高/低价，不用收盘价伪造触轨")
    band = stats.bollinger(closes[:-1], BOLL_N, BOLL_K)
    past = stats.bollinger(closes[:-1 - BOLL_SLOPE_DAYS], BOLL_N, BOLL_K)
    if not band["width_pct"] or band["upper"] <= band["lower"]:
        return _missing("bollinger", "布林通道零宽度，不能定义相对高低位", summary="通道零宽度")
    slope = (band["mid"] / past["mid"] - 1) * 100
    ranging = abs(slope) <= BOLL_FLAT_PCT
    touched_low, touched_high = low <= band["lower"], high >= band["upper"]
    reclaimed = band["lower"] < closes[-1] < band["upper"]
    hist = _hist(sequence)
    eps = max(closes[-1], 1.0) * 1e-10
    tail = hist[-3:]
    shrinking = (all(v is not None and v < -eps for v in tail)
                 and tail[0] + eps < tail[1] and tail[1] + eps < tail[2])
    repair = shrinking or base["cross"] == "golden"
    entry = ranging and touched_low and reclaimed and repair
    exit_hint = base["cross"] == "death" or (ranging and touched_high)
    ambiguous = touched_low and touched_high and base["cross"] != "death"
    signal = None if ambiguous else "reversion_exit" if exit_hint else "reversion_watch" if entry else None
    summary = "死叉规避·沿用基础" if base["cross"] == "death" else (
        "双轨触及·路径未知" if ambiguous else
        "上轨·回归止盈观察" if ranging and touched_high else
        "下轨收回·反弹观察" if entry else
        "沿上轨·不自动卖" if touched_high and not ranging else
        "触下轨·未确认反转" if touched_low else "未触发")
    return _record("bollinger", summary, active=bool(signal), triggered=bool(signal), signal=signal,
                   role="均值回归观察", bands=band, band_date=bars[-2]["date"],
                   pctb=(closes[-1] - band["lower"]) / (band["upper"] - band["lower"]),
                   mid_slope_pct=slope, ranging=ranging, touched_low=touched_low,
                   touched_high=touched_high, reclaimed=reclaimed, repair=repair, ambiguous_touch=ambiguous,
                   detail=f"冻结 {bars[-2]['date']} 布林 L={band['lower']:.4f} / M={band['mid']:.4f} / U={band['upper']:.4f}；中轨 5 根变化 {slope:+.2f}%（震荡门槛 ±{BOLL_FLAT_PCT:g}%）；{'触下轨' if touched_low else '未触下轨'} / {'收回轨内' if reclaimed else '未收回轨内'}；动能修复{'有' if repair else '无'}")


def completed_weeks(bars, code, *, now):
    """从日线收盘重新聚合周收盘，绝不抽样日线 MACD 充当周 MACD。

    时间上限同时受最新实际日线日期限制：没有周五/下一周的日线，不因为
    墙钟已到周末就把残缺尾周定稿。缺周五报价不能区分休市/漏采，需要供源原生周 K 核验，不补周五价格。
    """
    if not bars:
        return []
    as_of = datetime.strptime(bars[-1]["date"], "%Y-%m-%d").date()
    groups = {}
    for bar in bars:
        day = datetime.strptime(bar["date"], "%Y-%m-%d").date()
        friday = day + timedelta(days=4 - day.weekday())
        if friday > as_of or not providers.is_session_closed(code, friday.isoformat(), now=now):
            continue
        groups[friday.isoformat()] = {"date": bar["date"], "actual_date": bar["date"],
                                     "week_end": friday.isoformat(), "close": bar["close"],
                                     "verified": day.weekday() == 4}
    return [groups[day] for day in sorted(groups)]


def native_weeks(raw, code, *, as_of, now):
    """供源原生周 K 的日期可能是周一（Yahoo）或周末（东财），均按周期判断。

    week_end 是周期标签，不能宣称它是节假日周的实际最后交易日。
    残缺当前周不因周一时间戳已过去而被接受。
    """
    groups = {}
    for bar in raw or []:
        if not isinstance(bar, dict) or bar.get("from_meta"):
            continue
        close = _finite(bar.get("close"), positive=True)
        try:
            day = datetime.strptime(str(bar.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        friday = (day + timedelta(days=4 - day.weekday())).isoformat()
        if (close is None or day.weekday() >= 5 or friday > as_of
                or not providers.is_session_closed(code, friday, now=now)):
            continue
        groups[friday] = {"date": friday, "actual_date": None, "week_end": friday,
                          "close": close, "verified": True}
    return [groups[day] for day in sorted(groups)]


def weekly_ready(bars, code, *, now, verified=True):
    weeks = completed_weeks(bars, code, now=now)
    return (len(weeks) >= WEEKLY_MIN and (not verified or all(w["verified"] for w in weeks)) and
            (datetime.strptime(bars[-1]["date"], "%Y-%m-%d").date()
             - datetime.strptime(weeks[-1]["date"], "%Y-%m-%d").date()).days <= WEEKLY_MAX_LAG)


def weekly_daily_confluence(bars, code, base, *, now, weekly=None):
    weeks = (native_weeks(weekly["bars"], code, as_of=base["as_of"], now=now) if weekly
             else completed_weeks(bars, code, now=now))
    if len(weeks) < WEEKLY_MIN:
        return _missing("timeframe", f"完整周线不足（{len(weeks)}/{WEEKLY_MIN} 周），不以日线数量代替周数")
    unverified = sum(not w["verified"] for w in weeks)
    if unverified:
        return _missing("timeframe", f"{unverified} 周缺少周五报价，休市/漏采未辨；等待供源已定稿周 K 核验", summary="周末价待核验")
    age = (datetime.strptime(bars[-1]["date"], "%Y-%m-%d").date()
           - datetime.strptime(weeks[-1]["date"], "%Y-%m-%d").date()).days
    if age > WEEKLY_MAX_LAG:
        return _missing("timeframe", f"完整周线过旧（周截至 {weeks[-1]['week_end']}，与日线参考日相隔 {age} 天）")
    dif, dea, gap = stats.macd([w["close"] for w in weeks])
    if any(v is None or _finite(v) is None for v in (dif, dea, gap)):
        return _missing("timeframe", "周线 MACD 缺少有限值")
    eps = max(weeks[-1]["close"], 1.0) * 1e-10
    bullish = gap > eps
    daily_trigger = base["cross"] == "golden"  # H=2(DIF−DEA)，翻红是同一事件，不多一票
    daily_eps = max(bars[-1]["close"], 1.0) * 1e-10
    strict_gate = base["dif"] > daily_eps and base["dea"] > daily_eps
    active = bullish and daily_trigger
    actual_day = weeks[-1]["actual_date"]
    source = weekly.get("source") if weekly else "同源已收盘日线聚合"
    week_detail = f"（实际收盘 {actual_day}）" if actual_day else "（供源周 K，周末日期为周期标签）"
    summary = ("周日共振·水上确认" if strict_gate else "周日共振·水下观察") if active else (
        "周偏多·等待日触发" if bullish else "周线过滤未通过")
    return _record("timeframe", summary, active=active, triggered=active,
                   signal="trend_confirmation" if active and strict_gate else "countertrend_watch" if active else None,
                   role="大周期过滤", weekly_bars=len(weeks), weekly_as_of=weeks[-1]["week_end"],
                   actual_close_date=actual_day, source=source, source_url=weekly.get("url") if weekly else None,
                   week_end=weeks[-1]["week_end"], weekly_dif=dif, weekly_dea=dea,
                   weekly_hist=2 * gap, weekly_bullish=bullish, daily_trigger=daily_trigger,
                   strict_gate=strict_gate,
                   detail=f"已结束周 {weeks[-1]['week_end']}{week_detail}，{len(weeks)} 周 · {source}；周 DIF={dif:+.4f} / DEA={dea:+.4f}；日金叉/柱翻红按同一事件处理；严格水上过滤{'通过' if strict_gate else '未通过'}")


def _all_finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_all_finite(v) for v in value)
    return True


def analyze_derivatives(bars, sequence, *, code, base, now, weekly=None):
    # 单一派生项异常不能使已有效的基础 MACD / 其它派生项整体消失。
    calculations = {
        "slope": lambda: histogram_reversal(bars, sequence),
        "divergence": lambda: bottom_divergence(bars, sequence),
        "bollinger": lambda: bollinger_reversion(bars, sequence, base),
        "timeframe": lambda: weekly_daily_confluence(bars, code, base, now=now, weekly=weekly),
    }
    records = {}
    for key, calculate in calculations.items():
        try:
            record = calculate()
            records[key] = record if _all_finite(record) else _missing(key, "派生计算出现非有限值，基础 MACD 保留", summary="计算暂缺")
        except (ArithmeticError, ValueError) as exc:
            records[key] = _missing(key, f"派生计算失败（{type(exc).__name__}），基础 MACD 保留", summary="计算暂缺")
    return records


def render_derivatives(items, kit, *, full=False, plain=False):
    """短版保留全部状态与命中/缺项依据，长版另展开未命中的数值依据。

    plain=True（入门版）：有命中才出表（只列命中标的 + 命中依据），全部未触发时
    压成一行「本次命中：无 · … 未触发 · … 数据不足」；规则说明、未命中数值依据、缺项原因一律不出。
    """
    if not any(row.get("derived") for row in items):
        return ""
    if plain:
        return _render_derivatives_plain(items, kit)
    esc = kit.esc
    rows, evidence, folded = [], [], 0
    for row in items:
        derived = row.get("derived") or {}
        cells = [f"<b>{esc(row['label'])}</b>"]
        details = []
        for key in KEYS:
            record = derived.get(key)
            if not record:
                cells.append("—")
                continue
            summary = record["summary"]
            if key == "divergence" and record.get("active"):
                summary += " · " + record["confirmed_at"][5:]
            color = kit.warn_color if record.get("active") else None
            if record.get("signal") in ("trim_warning", "reversion_exit"):
                color = kit.bad_color
            elif record.get("signal") == "trend_confirmation":
                color = kit.ok_color
            text = esc(summary)
            if color:
                text = f'<span style="color:{color};font-weight:700">{text}</span>'
            cells.append(text)
            if not record["available"]:
                details.append(f"{record['name']}：{record['reason']}")
            elif full or record.get("active"):
                details.append(f"{record['name']}：{record['detail']}")
            elif record.get("detail"):
                folded += 1
        rows.append(cells)
        if row.get("history_note"):
            details.append(row["history_note"])
        if details:
            evidence.append((esc(row["label"] + "·派生依据"), "<br>".join(esc(text) for text in details)))
    out = [kit.sub("MACD 派生 · 分工研判（不重复计票）"),
           kit.table(["标的", "柱体收敛", "确认底背离", "布林回归", "周日共振"], rows,
                     aligns=("left",) * 5)]
    out.append(kit.kv(evidence))
    rules = [
        ("合并与去重", "水上/水下策略已在基础动作中，仍要求 DIF、DEA 均>0；只看 DIF 的宽松版本不另加票。柱体翻红与金叉是同一交叉事件。四项为相关的分工信号，不拼胜率、不强制下单；基础退出不能因左侧预警改为入场，周线缺样本不当中性。"),
        ("动能预警", "连续 3 根同侧柱收敛 + 起点 H/价在此前最多 60 个柱体的 P10/P90 极值区（至少 30 个）+ 20 根价格移动≥5%；仅第 3 根报新预警，之后为延续。绿柱修复不等于底部。"),
        ("形态观察", "最多 120 根；真实价格谷左右各 2 根确认，匹配 ±3 根内已确认的 DIF 谷；两谷间隔 5–60 根、低价至少下降 0.5%、负 DIF 抬高。价格谷之后第 5 根才记确认日；破第二低点或超过 10 根即失效/过期。零轴往返仅作附加说明。"),
        ("布林条件", "BB(20,2)，沿用样本标准差 ddof=1；冻结前一日带，触下轨后收回轨内 + 绿柱连续 2 根缩短/金叉 + 中轨 5 根变化≤±1% 才报回归观察。触上轨止盈仅针对震荡回归分支，不把强趋势沿上轨一律判卖；双轨同日触及不推测成交先后。"),
        ("周期与风控", "周线 MACD 从周收盘重新计算，至少 35 个已结束周；日线缺周五不能推断休市，改取供源周 K 核验，失败则该项缺席；周 DIF>DEA 只是过滤，不能保证未来几个月安全。周多+日金叉仍须基础双线水上过滤；水下共振仅观察。未收盘日/周、未确认谷和缺失数据不用于交易判断。新增项均未经真实样本外收益验证，非投资建议。"),
    ]
    if folded and not full:
        rules.append(("精简展开", f"收起 {folded} 项未命中的数值依据；命中与缺项仍完整披露，--full 查看全部。"))
    out.append(kit.kv([(esc(label), esc(text)) for label, text in rules]))
    return "".join(out)


def _render_derivatives_plain(items, kit):
    """入门版派生研判：只讲结果——哪只标的命中了什么；没命中就一句话。"""
    esc = kit.esc
    hits = []
    for row in items:
        derived = row.get("derived") or {}
        cells, details = [], []
        for key in KEYS:
            record = derived.get(key)
            if not record or not record.get("available") or not record.get("active"):
                continue
            summary = record["summary"]
            if key == "divergence" and record.get("confirmed_at"):
                summary += " · " + record["confirmed_at"][5:]
            color = kit.warn_color
            if record.get("signal") in ("trim_warning", "reversion_exit"):
                color = kit.bad_color
            elif record.get("signal") == "trend_confirmation":
                color = kit.ok_color
            cells.append(f'<span style="color:{color};font-weight:700">{esc(record["name"])} · {esc(summary)}</span>')
            if record.get("detail"):
                details.append(f"{record['name']}：{record['detail']}")
        if cells:
            hits.append((str(row["label"]), "<br>".join(cells), details))
    out = [kit.sub("MACD 派生研判")]
    if not hits:
        # 「未触发」与「数据不足」分开说：某项在所有标的上都不可用时，不冒充成未触发。
        checked, lacking = [], []
        for key in KEYS:
            records = [(row.get("derived") or {}).get(key) for row in items]
            if any(r and r.get("available") for r in records):
                checked.append(NAMES[key])
            elif any(records):
                lacking.append(NAMES[key])
        bits = []
        if checked:
            bits.append(" / ".join(checked) + " 未触发")
        if lacking:
            bits.append(" / ".join(lacking) + " 数据不足")
        out.append(kit.kv([("本次命中", esc("无 · " + " · ".join(bits)))]))
        return "".join(out)
    out.append(kit.table(["标的", "命中"],
                         [[f"<b>{esc(label)}</b>", cells] for label, cells, _d in hits],
                         aligns=("left", "left")))
    evidence = [(esc(label + "·依据"), "<br>".join(esc(text) for text in details))
                for label, _cells, details in hits if details]
    if evidence:
        out.append(kit.kv(evidence))
    return "".join(out)

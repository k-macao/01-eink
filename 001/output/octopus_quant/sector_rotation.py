#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股概念板块 → 港股观察篮子的轮动评分（纯计算层，无网络请求）。

映射是本项目维护的中文关键词规则，不是交易所/东方财富官方的跨市场成分关系。
未命中的概念不做推断；匹配到的港股仅作为可解释的观察篮子，不能代表完整板块。

策略方法参考（只参考方法、未复制第三方代码）：
- MA 趋势：Trade Vectors Python strategy templates 的 moving-average crossover；MIT。
- 多周期动量：RaajitSingh1306/Nifty-Sector-Rotation 的复合多周期动量思路；当前 main 页面有 MIT 标记，
  但未找到 LICENSE 文件，故仅作概念参考。
- 相对轮动：AdroitAnandAI/RRG-Sector-Rotation-India 的 RS-Ratio / RS-Momentum 象限思路；
  当前 main 页面未找到 LICENSE 文件，故仅作概念参考。这里实现的是简化代理，不是标准 JdK RRG。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re
from statistics import mean

CST = timezone(timedelta(hours=8))

# 权重由用户指定。输入缺失时不补 0/50；只在有 >=3 个维度且可用权重 >=70% 时输出总分。
DIMENSION_WEIGHTS = {
    "技术面": 0.35,
    "资金面": 0.35,
    "基本面": 0.10,
    "行业板块": 0.10,
    "事件驱动": 0.10,
}
MIN_SCORE_DIMENSIONS = 3
MIN_SCORE_WEIGHT = 0.70
MIN_STRATEGY_STOCKS = 2
EVENT_WINDOW_HOURS = 72

# 每条证券保留名称、Yahoo 日线代码与新闻别名。港股 5 位标准代码用作内部 join key。
_HK_STOCK_ROWS = (
    ("腾讯控股", "0700.HK", ("腾讯", "腾讯云")),
    ("阿里巴巴-W", "9988.HK", ("阿里巴巴", "阿里", "阿里云", "淘宝")),
    ("美团-W", "3690.HK", ("美团",)),
    ("小米集团-W", "1810.HK", ("小米",)),
    ("友邦保险", "1299.HK", ("友邦", "AIA")),
    ("汇丰控股", "0005.HK", ("汇丰",)),
    ("建设银行", "0939.HK", ("建设银行", "建行")),
    ("中国移动", "0941.HK", ("中国移动",)),
    ("香港交易所", "0388.HK", ("港交所", "香港交易所")),
    ("中芯国际", "0981.HK", ("中芯国际", "中芯")),
    ("比亚迪股份", "1211.HK", ("比亚迪",)),
    ("快手-W", "1024.HK", ("快手",)),
    ("京东集团-SW", "9618.HK", ("京东",)),
    ("中国海洋石油", "0883.HK", ("中海油", "中国海洋石油")),
    ("中国平安", "2318.HK", ("中国平安", "平安")),
    ("安踏体育", "2020.HK", ("安踏",)),
    ("网易-S", "9999.HK", ("网易",)),
    ("药明生物", "2269.HK", ("药明生物",)),
    # 额外的主题观察股：未进入港股量化默认池时，采集层只为命中的映射按需补抓。
    ("华虹半导体", "1347.HK", ("华虹半导体", "华虹")),
    ("金蝶国际", "0268.HK", ("金蝶",)),
    ("联想集团", "0992.HK", ("联想",)),
    ("哔哩哔哩-W", "9626.HK", ("哔哩哔哩", "B站")),
    ("理想汽车-W", "2015.HK", ("理想汽车", "理想")),
    ("小鹏汽车-W", "9868.HK", ("小鹏汽车", "小鹏")),
    ("蔚来-SW", "9866.HK", ("蔚来",)),
    ("比亚迪电子", "0285.HK", ("比亚迪电子",)),
    ("宁德时代", "3750.HK", ("宁德时代", "CATL")),
    ("中国电信", "0728.HK", ("中国电信",)),
    ("中国联通", "0762.HK", ("中国联通",)),
    ("中国石油股份", "0857.HK", ("中国石油", "中石油")),
    ("中国石油化工股份", "0386.HK", ("中国石化", "中石化")),
    ("李宁", "2331.HK", ("李宁",)),
    ("药明康德", "2359.HK", ("药明康德",)),
    ("信达生物", "1801.HK", ("信达生物",)),
    ("中国生物制药", "1177.HK", ("中国生物制药", "石药")),
    ("工商银行", "1398.HK", ("工商银行", "工行")),
    ("中国银行", "3988.HK", ("中国银行", "中行")),
    ("招商银行", "3968.HK", ("招商银行", "招行")),
    ("优必选", "9880.HK", ("优必选",)),
)


def normalize_hk_code(value):
    """把 ``0700`` / ``00700`` / ``0700.HK`` 统一成内部 5 位港股代码。"""
    raw = str(value or "").strip().upper()
    if raw.endswith(".HK"):
        raw = raw[:-3]
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(5) if digits else raw


HK_STOCKS = {}
for _name, _symbol, _aliases in _HK_STOCK_ROWS:
    _code = normalize_hk_code(_symbol)
    HK_STOCKS[_code] = {
        "code": _code,
        "symbol": _symbol,
        "name": _name,
        "news_aliases": _aliases,
    }

# 规则按主题明确列出，不用模糊相似度。多个主题命中时取并集，并在结果中披露命中词。
_CONCEPT_RULE_ROWS = (
    ("AI / 大模型", ("人工智能", "AI应用", "AI算力", "AIGC", "大模型", "生成式AI", "智能算力"),
     ("0700.HK", "9988.HK", "1810.HK", "1024.HK", "0268.HK", "0992.HK")),
    ("云计算 / 数据中心", ("云计算", "数据中心", "算力", "云服务"),
     ("0700.HK", "9988.HK", "0268.HK", "0992.HK", "0941.HK")),
    ("互联网 / 电商", ("互联网", "电商", "电子商务", "网络购物", "即时零售"),
     ("0700.HK", "9988.HK", "3690.HK", "9618.HK", "1024.HK", "9999.HK")),
    ("游戏 / 数字内容", ("游戏", "网络游戏", "手游", "短视频", "视频平台", "传媒"),
     ("0700.HK", "9999.HK", "9626.HK", "1024.HK")),
    ("半导体 / 芯片", ("半导体", "芯片", "集成电路", "晶圆", "存储芯片"),
     ("0981.HK", "1347.HK")),
    ("消费电子 / 智能终端", ("消费电子", "智能手机", "手机产业链", "智能终端", "电子元件"),
     ("1810.HK", "0992.HK", "0285.HK")),
    ("汽车 / 新能源汽车", ("新能源汽车", "新能源车", "汽车整车", "智能汽车", "汽车零部件", "汽车电子"),
     ("1211.HK", "2015.HK", "9868.HK", "9866.HK", "1810.HK", "0285.HK")),
    ("动力电池", ("动力电池", "锂电池", "电池回收", "储能电池"),
     ("1211.HK", "3750.HK")),
    ("银行", ("银行", "国有大行", "股份制银行"),
     ("0005.HK", "0939.HK", "1398.HK", "3988.HK", "3968.HK")),
    ("保险", ("保险", "寿险", "财险"),
     ("1299.HK", "2318.HK")),
    ("油气 / 石化", ("石油", "油气", "天然气", "石化", "页岩气"),
     ("0883.HK", "0857.HK", "0386.HK")),
    ("电信运营", ("电信运营", "通信运营", "电信", "通信服务", "运营商"),
     ("0941.HK", "0728.HK", "0762.HK")),
    ("生物医药 / 创新药", ("生物医药", "生物科技", "生物制药", "创新药", "CXO", "医药研发"),
     ("2269.HK", "2359.HK", "1801.HK", "1177.HK")),
    ("体育用品 / 运动服饰", ("体育用品", "运动服饰", "运动品牌", "体育产业"),
     ("2020.HK", "2331.HK")),
    ("机器人", ("机器人", "人形机器人", "服务机器人"),
     ("9880.HK",)),
)

CONCEPT_RULES = tuple({
    "label": label,
    "terms": tuple(str(term).casefold() for term in terms),
    "codes": tuple(normalize_hk_code(code) for code in codes),
} for label, terms, codes in _CONCEPT_RULE_ROWS)

# 方向词表只对近 72 小时且匹配到概念/映射公司关键词的标题做扫描；不是情绪模型。
EVENT_POSITIVE_TERMS = (
    "利好", "增长", "大增", "超预期", "新订单", "获批", "获准", "中标", "签约",
    "扩产", "量产", "上调", "回购", "补贴", "获支持", "创新高", "扭亏", "盈利改善",
    "需求回暖", "销量增长", "交付增长", "提升指引",
)
EVENT_NEGATIVE_TERMS = (
    "利空", "下滑", "下跌", "亏损", "不及预期", "未达预期", "处罚", "监管调查",
    "被调查", "诉讼", "违约", "停产", "事故", "召回", "裁员", "减持", "下调",
    "制裁", "爆雷", "业绩预减", "需求疲弱", "销量下滑", "指引下调",
)


def _num(value):
    try:
        if value in (None, "", "-"):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(value)))


def _norm_text(value):
    return re.sub(r"[\s\W]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _dedupe(items):
    return list(dict.fromkeys(items))


def map_concepts(concepts):
    """对 EastMoney 概念板块名应用白名单关键词规则，未命中的项保持 unmapped。"""
    mapped, unmapped = [], []
    for raw in concepts or []:
        if not isinstance(raw, dict):
            continue
        board = dict(raw)
        name = str(board.get("name") or "").strip()
        normalized = _norm_text(name)
        matches = []
        codes = []
        matched_terms = []
        for rule in CONCEPT_RULES:
            hits = [term for term in rule["terms"] if _norm_text(term) and _norm_text(term) in normalized]
            if not hits:
                continue
            matches.append(rule["label"])
            matched_terms.extend(hits)
            codes.extend(rule["codes"])
        codes = [code for code in _dedupe(codes) if code in HK_STOCKS]
        board.update({
            "mapped_codes": codes,
            "matched_rules": _dedupe(matches),
            "matched_terms": _dedupe(matched_terms),
            "mapping_method": "local_keyword_proxy" if codes else None,
        })
        (mapped if codes else unmapped).append(board)
    return mapped, unmapped


def mapped_stock_codes(concepts):
    """返回当前概念列表中所有关键词命中的港股代码，保留规则顺序。"""
    mapped, _unmapped = map_concepts(concepts)
    return _dedupe(code for board in mapped for code in board.get("mapped_codes") or [])


def _clean_bars(bars):
    out = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = _num(bar.get("close"))
        day = str(bar.get("date") or "").strip()[:10]
        if close is None or close <= 0 or not day:
            continue
        out.append({**bar, "date": day, "close": close})
    out.sort(key=lambda row: row["date"])
    # 日线接口偶有重复日期，按日期保留最后一条。
    by_day = {bar["date"]: bar for bar in out}
    return [by_day[day] for day in sorted(by_day)]


def _sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def price_metrics(bars):
    """从日线取固定周期收益和均线；不足样本的字段为 None。"""
    clean = _clean_bars(bars)
    closes = [bar["close"] for bar in clean]
    result = {"bar_count": len(clean), "as_of": clean[-1]["date"] if clean else None,
              "close": closes[-1] if closes else None}
    for period in (20, 60, 120):
        result[f"ret_{period}"] = (
            (closes[-1] / closes[-1 - period] - 1.0) * 100.0
            if len(closes) > period and closes[-1 - period] else None)
    ma20, ma60 = _sma(closes, 20), _sma(closes, 60)
    result.update({"ma20": ma20, "ma60": ma60})
    if closes and ma20 is not None and ma60 is not None:
        if closes[-1] > ma20 > ma60:
            result["ma_state"] = "bull"
        elif closes[-1] < ma20 < ma60:
            result["ma_state"] = "bear"
        else:
            result["ma_state"] = "flat"
    else:
        result["ma_state"] = None

    ret20, ret60 = result["ret_20"], result["ret_60"]
    if ret20 is not None and ret60 is not None and ma20 is not None and ma60 is not None:
        score20 = _clamp(50.0 + 3.0 * ret20)
        score60 = _clamp(50.0 + 1.5 * ret60)
        trend_score = 50.0 + (25.0 if closes[-1] > ma20 else -25.0) \
            + (25.0 if ma20 > ma60 else -25.0)
        result["technical_score"] = round(0.35 * score20 + 0.35 * score60
                                          + 0.30 * trend_score, 2)
    else:
        result["technical_score"] = None
    return result


def _normalized_stock_data(stock_data):
    out = {}
    for key, raw in (stock_data or {}).items():
        if isinstance(raw, list):
            record = {"bars": raw}
        elif isinstance(raw, dict):
            record = dict(raw)
        else:
            continue
        code = normalize_hk_code(record.get("code") or key)
        if not code:
            continue
        meta = HK_STOCKS.get(code, {})
        record.setdefault("code", code)
        record.setdefault("name", meta.get("name") or str(key))
        out[code] = record
    return out


def _flow_percent(record):
    pct = _num(record.get("main_pct"))
    if pct is not None:
        return pct
    net, amount = _num(record.get("main_net")), _num(record.get("amount"))
    if net is not None and amount is not None and amount > 0:
        return net / amount * 100.0
    return None


def _peer_cheapness(values):
    """正 PE/PB 横截面低值优先；少于 3 个 peer 不制造伪分位。"""
    clean = [(str(code), value) for code, value in values.items()
             if value is not None and value > 0]
    if len(clean) < 3:
        return {}
    clean.sort(key=lambda pair: pair[1])
    n = len(clean)
    result = {}
    i = 0
    while i < n:
        j = i + 1
        while j < n and math.isclose(clean[j][1], clean[i][1], rel_tol=1e-9, abs_tol=1e-12):
            j += 1
        average_rank = (i + (j - 1)) / 2.0
        # 估值越低，分越高；并列取平均名次。
        score = 100.0 * (n - 1 - average_rank) / (n - 1)
        for k in range(i, j):
            result[clean[k][0]] = score
        i = j
    return result


def _valuation_scores(stock_data):
    pe = {code: _num(row.get("pe_ttm")) for code, row in stock_data.items()}
    pb = {code: _num(row.get("pb")) for code, row in stock_data.items()}
    pe_scores = _peer_cheapness(pe)
    pb_scores = _peer_cheapness(pb)
    scores = {}
    for code in stock_data:
        parts = [value for value in (pe_scores.get(code), pb_scores.get(code)) if value is not None]
        scores[code] = mean(parts) if parts else None
    return scores, {"pe_peers": len(pe_scores), "pb_peers": len(pb_scores)}


def _dimension(score, valid, total, method, **extra):
    return {"score": round(float(score), 1) if score is not None else None,
            "valid": int(valid or 0), "total": int(total or 0),
            "method": method, **extra}


def _industry_dimension(board):
    parts = []
    chg = _num(board.get("chg_pct"))
    if chg is not None:
        parts.append(_clamp(50.0 + 5.0 * chg))
    up, down = _num(board.get("up")), _num(board.get("down"))
    if up is not None and down is not None and up + down > 0:
        parts.append(100.0 * up / (up + down))
    score = mean(parts) if parts else None
    return _dimension(score, len(parts), 2,
                      "A股概念板块当日涨跌幅（50+5×%）与涨跌宽度（上涨/(上涨+下跌)）等权平均",
                      chg_pct=chg, up=up, down=down)


def _event_datetime(record):
    for key in ("published_cst", "time", "published", "date", "published_at"):
        raw = record.get(key)
        if not raw or str(raw).strip() in ("—", "-", "None"):
            continue
        if isinstance(raw, datetime):
            parsed = raw
        else:
            text = str(raw).strip()
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(text)
                except (TypeError, ValueError, OverflowError):
                    continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CST)
        return parsed.astimezone(CST)
    return None


def _fresh_event_headlines(headlines, now=None):
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    now = now.astimezone(CST)
    out = []
    for record in headlines or []:
        if isinstance(record, str):
            record = {"title": record}
        if not isinstance(record, dict):
            continue
        title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
        if not title:
            continue
        # “当天抓取”不是发布日期；事件维度只接受可解析的原始内容时间。
        published = _event_datetime(record)
        if published is None:
            continue
        age = (now - published).total_seconds() / 3600.0
        if age < -1 or age > EVENT_WINDOW_HOURS:
            continue
        out.append({**record, "title": title, "_published": published,
                    "_age_hours": max(0.0, age)})
    out.sort(key=lambda row: row["_published"], reverse=True)
    # 同标题跨来源只计一次，避免新闻转载放大事件分。
    seen, unique = set(), []
    for row in out:
        key = _norm_text(row["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _event_terms_for(board):
    terms = [str(board.get("name") or "")]
    terms.extend(board.get("matched_terms") or [])
    for code in board.get("mapped_codes") or []:
        stock = HK_STOCKS.get(code) or {}
        terms.extend(stock.get("news_aliases") or [])
        terms.append(str(stock.get("name") or ""))
    # 短词会导致无关标题误命中；仅保留至少 2 字符（或 2 个 ASCII 字母）的词。
    return _dedupe(term for term in terms if len(_norm_text(term)) >= 2)


def _event_dimension(board, headlines, now=None):
    terms = _event_terms_for(board)
    evidence = []
    positive_n = negative_n = neutral_n = 0
    for item in _fresh_event_headlines(headlines, now=now):
        title = str(item.get("title") or "")
        if not any(term.casefold() in title.casefold() for term in terms):
            continue
        pos = [word for word in EVENT_POSITIVE_TERMS if word.casefold() in title.casefold()]
        neg = [word for word in EVENT_NEGATIVE_TERMS if word.casefold() in title.casefold()]
        if pos and not neg:
            direction = 1
            positive_n += 1
            label = "偏正向"
        elif neg and not pos:
            direction = -1
            negative_n += 1
            label = "偏负向"
        else:
            direction = 0
            neutral_n += 1
            label = "中性/词表混合"
        evidence.append({
            "title": title,
            "source": str(item.get("source") or "公开新闻标题"),
            "published_cst": item["_published"].strftime("%Y-%m-%d %H:%M"),
            "sentiment": label,
            "positive_terms": pos,
            "negative_terms": neg,
            "matched_terms": [term for term in terms if term.casefold() in title.casefold()],
        })
    score = (50.0 + 50.0 * mean([1.0 if e["sentiment"] == "偏正向" else
                                 -1.0 if e["sentiment"] == "偏负向" else 0.0
                                 for e in evidence])) if evidence else None
    return _dimension(score, len(evidence), len(evidence),
                      "近 72 小时匹配标题；固定多空词表净方向映射 0–100；无命中则缺失",
                      positive=positive_n, negative=negative_n, neutral=neutral_n,
                      evidence=evidence[:3])


def _strategy_votes(stock_metrics, benchmark_metrics):
    trend_rows = [row for row in stock_metrics if row.get("ma_state") is not None]
    if len(trend_rows) >= MIN_STRATEGY_STOCKS:
        bull_n = sum(row["ma_state"] == "bull" for row in trend_rows)
        bear_n = sum(row["ma_state"] == "bear" for row in trend_rows)
        threshold = math.ceil(0.60 * len(trend_rows))
        if bull_n >= threshold:
            direction = "看多"
        elif bear_n >= threshold:
            direction = "看空"
        else:
            direction = "中性"
        trend_vote = {
            "key": "ma_trend", "name": "MA20/60 趋势",
            "direction": direction, "available": True,
            "sample_n": len(trend_rows),
            "reason": f"多头排列 {bull_n}/{len(trend_rows)}、空头排列 {bear_n}/{len(trend_rows)}；"
                      f"占比达到 60% 才投同向票",
        }
    else:
        trend_vote = {"key": "ma_trend", "name": "MA20/60 趋势",
                      "direction": "数据不足", "available": False,
                      "sample_n": len(trend_rows), "reason": "至少需要 2 只映射港股具备有效均线"}

    momentum_rows = [row for row in stock_metrics
                     if all(row.get(f"ret_{period}") is not None for period in (20, 60, 120))]
    if len(momentum_rows) >= MIN_STRATEGY_STOCKS:
        horizon_means = {
            period: mean(row[f"ret_{period}"] for row in momentum_rows)
            for period in (20, 60, 120)
        }
        pos_n = sum(value > 0 for value in horizon_means.values())
        neg_n = sum(value < 0 for value in horizon_means.values())
        direction = "看多" if pos_n >= 2 else ("看空" if neg_n >= 2 else "中性")
        momentum_vote = {
            "key": "multi_momentum", "name": "20/60/120 日动量",
            "direction": direction, "available": True,
            "sample_n": len(momentum_rows),
            "returns": {str(period): round(value, 2) for period, value in horizon_means.items()},
            "reason": "板块映射股等权平均收益：" + " / ".join(
                f"{period}日 {value:+.2f}%" for period, value in horizon_means.items())
                + "；三周期中至少两期同向才投票",
        }
    else:
        momentum_vote = {"key": "multi_momentum", "name": "20/60/120 日动量",
                         "direction": "数据不足", "available": False,
                         "sample_n": len(momentum_rows), "reason": "至少需要 2 只映射港股有 120 日收益样本"}

    rel_rows = [row for row in stock_metrics
                if row.get("ret_20") is not None and row.get("ret_60") is not None]
    hsi20 = _num((benchmark_metrics or {}).get("ret_20"))
    hsi60 = _num((benchmark_metrics or {}).get("ret_60"))
    if len(rel_rows) >= MIN_STRATEGY_STOCKS and hsi20 is not None and hsi60 is not None:
        relative20 = mean(row["ret_20"] for row in rel_rows) - hsi20
        relative60 = mean(row["ret_60"] for row in rel_rows) - hsi60
        if relative20 > 0 and relative60 > 0:
            quadrant, direction = "Leading（领先）", "看多"
        elif relative20 > 0 and relative60 <= 0:
            quadrant, direction = "Improving（改善）", "看多"
        elif relative20 <= 0 and relative60 <= 0:
            quadrant, direction = "Lagging（落后）", "看空"
        else:
            quadrant, direction = "Weakening（转弱）", "中性"
        relative_vote = {
            "key": "relative_rotation", "name": "相对强弱轮动",
            "direction": direction, "available": True,
            "sample_n": len(rel_rows), "quadrant": quadrant,
            "rs_momentum_pp": round(relative20, 2), "rs_ratio_pp": round(relative60, 2),
            "reason": f"对恒指超额收益：20日 {relative20:+.2f} 个百分点、"
                      f"60日 {relative60:+.2f} 个百分点 → {quadrant}",
        }
    else:
        relative_vote = {"key": "relative_rotation", "name": "相对强弱轮动",
                         "direction": "数据不足", "available": False,
                         "sample_n": len(rel_rows), "reason": "至少需要 2 只映射港股及恒指 20/60 日收益"}

    votes = [trend_vote, momentum_vote, relative_vote]
    active = [row for row in votes if row.get("available")]
    bull_n = sum(row["direction"] == "看多" for row in active)
    bear_n = sum(row["direction"] == "看空" for row in active)
    if len(active) < 2:
        consensus = "数据不足"
    elif bull_n >= 2:
        consensus = "偏多"
    elif bear_n >= 2:
        consensus = "偏空"
    else:
        consensus = "分歧/中性"
    summary = {"consensus": consensus, "available_n": len(active),
               "bullish_n": bull_n, "bearish_n": bear_n,
               "neutral_n": sum(row["direction"] == "中性" for row in active),
               "rule": "至少 2 套策略有效，且至少 2 票同向，才给偏多/偏空；其余为分歧/中性。"}
    return votes, summary


def _score_band(score):
    if score is None:
        return "数据不足"
    if score >= 70:
        return "强势"
    if score >= 60:
        return "偏强"
    if score >= 45:
        return "中性"
    if score >= 35:
        return "偏弱"
    return "弱势"


def _coerce_now(now):
    if now is None:
        return datetime.now(CST)
    if isinstance(now, date) and not isinstance(now, datetime):
        return datetime.combine(now, time(23, 59), tzinfo=CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


def build_rotation(concepts, stock_data=None, benchmark_bars=None, headlines=None,
                   *, catalog_complete=True, now=None):
    """纯计算入口。每个维度只用实有字段；缺失维度保持 ``None``，不会补零或补中性。"""
    now = _coerce_now(now)
    mapped, unmapped = map_concepts(concepts)
    stock_data = _normalized_stock_data(stock_data)
    benchmark = price_metrics(benchmark_bars or [])
    valuation_by_code, valuation_peer_counts = _valuation_scores(stock_data)

    items = []
    for board in mapped:
        codes = board.get("mapped_codes") or []
        stock_metrics = []
        stock_display = []
        for code in codes:
            record = stock_data.get(code) or {}
            metrics = price_metrics(record.get("bars") or [])
            flow_pct = _flow_percent(record)
            if flow_pct is not None:
                metrics["flow_pct"] = flow_pct
                metrics["flow_score"] = _clamp(50.0 + 5.0 * flow_pct)
            else:
                metrics["flow_pct"] = metrics["flow_score"] = None
            metrics["fundamental_score"] = valuation_by_code.get(code)
            metrics["code"] = code
            metrics["name"] = record.get("name") or (HK_STOCKS.get(code) or {}).get("name") or code
            stock_metrics.append(metrics)
            stock_meta = HK_STOCKS.get(code) or {}
            stock_display.append({
                "code": code,
                "symbol": stock_meta.get("symbol") or record.get("symbol"),
                "name": metrics["name"],
                "as_of": metrics.get("as_of"),
                "quote_as_of": record.get("quote_as_of") or record.get("as_of"),
                "bar_count": metrics.get("bar_count", 0),
                "technical_score": metrics.get("technical_score"),
                "flow_pct": round(flow_pct, 2) if flow_pct is not None else None,
                "pe_ttm": _num(record.get("pe_ttm")),
                "pb": _num(record.get("pb")),
            })

        tech_values = [row["technical_score"] for row in stock_metrics
                       if row.get("technical_score") is not None]
        if len(tech_values) >= MIN_STRATEGY_STOCKS:
            technical = _dimension(mean(tech_values), len(tech_values), len(codes),
                                   "映射港股等权：20/60日收益映射分 + 收盘/MA20/MA60趋势分",
                                   valid_stocks=len(tech_values))
        else:
            technical = _dimension(None, len(tech_values), len(codes),
                                   "至少 2 只映射港股具备 60 日行情才计算；单股只作观察代理",
                                   valid_stocks=len(tech_values))

        flow_values = [row["flow_score"] for row in stock_metrics if row.get("flow_score") is not None]
        flow_pcts = [row["flow_pct"] for row in stock_metrics if row.get("flow_pct") is not None]
        flow_sources = []
        hk_flow_score = None
        if len(flow_values) >= MIN_STRATEGY_STOCKS:
            hk_flow_score = mean(flow_values)
            flow_sources.append(hk_flow_score)
        board_net, board_amount = _num(board.get("main_net")), _num(board.get("amount"))
        board_flow_pct = (board_net / board_amount * 100.0
                          if board_net is not None and board_amount is not None and board_amount > 0
                          else None)
        board_flow_score = _clamp(50.0 + 5.0 * board_flow_pct) if board_flow_pct is not None else None
        if board_flow_score is not None:
            flow_sources.append(board_flow_score)
        if flow_sources:
            flow = _dimension(mean(flow_sources), len(flow_sources), 2,
                              "A股概念板块主力净占比（f62/f6）与港股映射股主力净占比（f184；缺失时 f62/f6）分组等权，映射股组需至少 2 只",
                              valid_stocks=len(flow_values),
                              hk_group_score=round(hk_flow_score, 1) if hk_flow_score is not None else None,
                              a_share_main_pct=round(board_flow_pct, 2) if board_flow_pct is not None else None,
                              avg_hk_main_pct=round(mean(flow_pcts), 2) if flow_pcts else None)
        else:
            flow = _dimension(None, 0, 2,
                              "A股板块主力净占比缺项，且至少 2 只映射港股资金数据不足；不以缺失补分",
                              valid_stocks=len(flow_values),
                              hk_group_score=None, a_share_main_pct=None,
                              avg_hk_main_pct=round(mean(flow_pcts), 2) if flow_pcts else None)

        fundamental_values = [row["fundamental_score"] for row in stock_metrics
                              if row.get("fundamental_score") is not None]
        if len(fundamental_values) >= MIN_STRATEGY_STOCKS:
            fundamental = _dimension(mean(fundamental_values), len(fundamental_values), len(codes),
                                      "正 PE-TTM / PB 横截面低值分位等权；参照池为全部映射港股观察篮子，估值代理不代表盈利质量",
                                      valid_stocks=len(fundamental_values),
                                      peer_universe=valuation_peer_counts)
        else:
            fundamental = _dimension(None, len(fundamental_values), len(codes),
                                     "至少 2 只映射港股有可比估值，且全体映射观察篮子 peer >=3；缺项不补分",
                                     valid_stocks=len(fundamental_values),
                                     peer_universe=valuation_peer_counts)

        industry = _industry_dimension(board)
        event = _event_dimension(board, headlines or [], now=now)
        dimensions = {
            "技术面": technical,
            "资金面": flow,
            "基本面": fundamental,
            "行业板块": industry,
            "事件驱动": event,
        }
        available = [(key, value["score"]) for key, value in dimensions.items()
                     if value.get("score") is not None]
        available_weight = sum(DIMENSION_WEIGHTS[key] for key, _value in available)
        if len(available) >= MIN_SCORE_DIMENSIONS and available_weight >= MIN_SCORE_WEIGHT:
            overall = sum(DIMENSION_WEIGHTS[key] * value
                          for key, value in available) / available_weight
            overall = round(overall, 1)
            score_reason = "可用维度按原始权重重新归一化"
        else:
            overall = None
            score_reason = (f"有效维度 {len(available)}/5、原始权重 {available_weight * 100:.0f}%；"
                            f"需至少 {MIN_SCORE_DIMENSIONS} 个维度且权重不少于 {MIN_SCORE_WEIGHT * 100:.0f}%")

        votes, vote_summary = _strategy_votes(stock_metrics, benchmark)
        items.append({
            "code": str(board.get("code") or ""),
            "name": str(board.get("name") or ""),
            "chg_pct": _num(board.get("chg_pct")),
            "up": _num(board.get("up")),
            "down": _num(board.get("down")),
            "main_net": _num(board.get("main_net")),
            "lead_stock": str(board.get("lead_stock") or ""),
            "mapped_codes": codes,
            "mapped_stocks": stock_display,
            "matched_rules": board.get("matched_rules") or [],
            "matched_terms": board.get("matched_terms") or [],
            "mapping_method": board.get("mapping_method"),
            "mapping_note": "本地概念名关键词 → 港股公司观察篮子；非官方成分/关联映射",
            "dimensions": dimensions,
            "available_dimensions": len(available),
            "available_weight": round(available_weight, 4),
            "overall_score": overall,
            "score_label": _score_band(overall),
            "score_reason": score_reason,
            "strategy_votes": votes,
            "strategy_summary": vote_summary,
            "event_evidence": event.get("evidence") or [],
        })

    # 有分的排在前面；不足门槛的项仍返回给页面披露，不参与排名。
    items.sort(key=lambda row: (row["overall_score"] is not None,
                                row["overall_score"] if row["overall_score"] is not None else -1),
               reverse=True)
    for rank, item in enumerate((row for row in items if row["overall_score"] is not None), 1):
        item["rank"] = rank

    bar_dates = [stock.get("as_of") for item in items for stock in item.get("mapped_stocks") or []
                 if stock.get("as_of")]
    quote_dates = [stock.get("quote_as_of") for item in items for stock in item.get("mapped_stocks") or []
                   if stock.get("quote_as_of")]
    concept_dates = [str(row.get("as_of")) for row in concepts or []
                     if isinstance(row, dict) and row.get("as_of")]
    benchmark_date = benchmark.get("as_of")
    all_dates = [day for day in [*bar_dates, *quote_dates, *concept_dates, benchmark_date] if day]
    data_dates = {
        "a_share_concepts": max(concept_dates) if concept_dates else None,
        "hk_stocks_latest": max(bar_dates) if bar_dates else None,
        "hk_quotes_latest": max(quote_dates) if quote_dates else None,
        "hsi_benchmark": benchmark_date,
        "latest_seen": max(all_dates) if all_dates else None,
    }
    flow_symbols = {row["code"] for row in stock_data.values() if _flow_percent(row) is not None}
    fundamental_symbols = {row["code"] for row in stock_data.values()
                           if _num(row.get("pe_ttm")) is not None or _num(row.get("pb")) is not None}
    bars_symbols = {code for code, row in stock_data.items() if price_metrics(row.get("bars") or []).get("bar_count", 0) >= 61}
    mapped_codes = mapped_stock_codes(concepts)

    return {
        "concept_total": len(concepts or []),
        "mapped_total": len(mapped),
        "unmapped_total": len(unmapped),
        "unmapped_names": [str(row.get("name") or "") for row in unmapped[:20]],
        "scored_total": sum(item.get("overall_score") is not None for item in items),
        "catalog_complete": bool(catalog_complete),
        "mapping_rate": round(len(mapped) / len(concepts), 4) if concepts else 0.0,
        "weights": dict(DIMENSION_WEIGHTS),
        "minimum_score_coverage": {"dimensions": MIN_SCORE_DIMENSIONS,
                                    "weight": MIN_SCORE_WEIGHT},
        "coverage": {
            "mapped_hk_symbols": len(mapped_codes),
            "technical_symbols": len(bars_symbols & set(mapped_codes)),
            "flow_symbols": len(flow_symbols & set(mapped_codes)),
            "fundamental_symbols": len(fundamental_symbols & set(mapped_codes)),
            "benchmark_as_of": benchmark_date,
        },
        "data_dates": data_dates,
        "benchmark": {"name": "恒生指数", "as_of": benchmark_date,
                      "ret_20": benchmark.get("ret_20"), "ret_60": benchmark.get("ret_60")},
        "items": items,
        "mapping_note": "港股公司仅为本项目人工维护的关键词观察篮子，非 A 股概念的官方跨市场成分映射；"
                        "未命中概念不映射、不评分。",
        "scoring_note": "维度分 0–100；有缺项时不填 0/50，只有 >=3 维且可用原始权重 >=70% 才计算加权总分。",
        "method_note": "使用当前概念库快照与当前映射名单，不构成点时历史回测；权重分与三策略投票分开。",
    }


def strategy_reference_links():
    """公开参考链接；供报告与 README 共用。"""
    return (
        ("MA 趋势模板（MIT）", "https://github.com/tradevectorsrobots/trading-strategy-templates-python"),
        ("多周期板块动量（仅概念参考）", "https://github.com/RaajitSingh1306/Nifty-Sector-Rotation"),
        ("RRG 相对轮动（仅概念参考）", "https://github.com/AdroitAnandAI/RRG-Sector-Rotation-India"),
    )

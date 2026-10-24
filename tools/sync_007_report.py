#!/usr/bin/env python3
"""Build the compact e-ink report from k-macao/007.

The source repository already owns the market collectors and their local
sentiment model.  This adapter deliberately does not copy those collectors:
it imports a fresh checkout of 007, runs ``collect_feeds`` and writes the small,
stable JSON contract consumed by this repository's static page.

A CI run first saves a raw snapshot without any repository token or AI secret,
then this trusted adapter optionally sends only that snapshot's facts to
DeepSeek. Missing/failed AI is not fatal: the deterministic reducer still
produces a factual report from the same 007 items.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

HKT = timezone(timedelta(hours=8), "Asia/Macau")
TITLE = "章鱼 AI.每日研报"
SCHEMA_VERSION = 1

MARKET_WORDS = (
    "a股", "港股", "美股", "恒指", "恒生", "标普", "纳指", "道指", "日经",
    "股价", "股票", "指数", "市场", "资金", "成交", "基金", "券商", "银行",
    "央行", "联储", "利率", "降息", "加息", "债", "汇率", "美元", "人民币",
    "日元", "油价", "原油", "黄金", "金价", "商品", "期货", "ai", "算力",
    "芯片", "半导体", "科技", "数据中心", "财报", "盈利", "利润", "营收",
    "订单", "出口", "关税", "经济", "gdp", "cpi", "pce", "就业", "非农",
    "制造", "消费", "地产", "电池", "锂", "航运", "估值", "回购", "增持",
)

THEMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AI / 算力", ("ai", "算力", "数据中心", "大模型", "云厂商")),
    ("半导体", ("半导体", "芯片", "晶圆", "存储", "载板")),
    ("能源 / 油价", ("油价", "原油", "石油", "天然气", "能源", "油运")),
    ("利率 / 债市", ("利率", "降息", "加息", "美债", "国债", "央行", "联储")),
    ("汇率", ("美元", "人民币", "日元", "汇率", "外汇")),
    ("制造 / 出口", ("制造", "出口", "外贸", "订单", "船舶")),
    ("锂电 / 电池", ("锂", "电池", "储能", "排产")),
    ("消费", ("消费", "零售", "奢侈", "旅游", "餐饮")),
    ("地产", ("地产", "楼市", "房价", "租金")),
    ("航运 / 交运", ("航运", "运价", "航空", "交运", "港口")),
    ("地缘 / 政策", ("冲突", "战争", "制裁", "关税", "监管", "政策", "地缘")),
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MARKDOWN_RE = re.compile(r"[*_`#>]|\x00")
WIRE_PREFIX_RE = re.compile(r"^[【\[][^】\]]{1,18}[】\]]\s*")
LEADING_NUM_RE = re.compile(r"^\s*(?:[-+*•]|\d+[.)、])\s*")


def _plain(value: Any) -> str:
    text = ANSI_RE.sub("", str(value or ""))
    text = MARKDOWN_RE.sub("", text)
    text = WIRE_PREFIX_RE.sub("", text)
    text = LEADING_NUM_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -—·|｜")


def _clip(text: str, limit: int = 52) -> str:
    text = _plain(text)
    if len(text) <= limit:
        return text
    # Prefer a complete first clause over a blind character cut.
    for sep in ("；", "。", "，", "：", "—", "｜", "|"):
        head = text.split(sep, 1)[0].strip()
        if 8 <= len(head) <= limit:
            return head
    return text[: max(1, limit - 1)].rstrip("，、；： -") + "…"


def _normal_title(text: str) -> str:
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _plain(text).lower())
    # Syndicated copies commonly append only a generic wire suffix.
    return re.sub(r"(?:快讯|消息|报道|最新)$", "", value)


def _market_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for word in MARKET_WORDS if word in low)


def _theme(text: str) -> str:
    low = text.lower()
    best_score, best_label = 0, "市场综合"
    for label, words in THEMES:
        score = sum(1 for word in words if word in low)
        if score > best_score:  # equal scores keep the earlier, more specific theme
            best_score, best_label = score, label
    return best_label


def _sentiment(score: float) -> str:
    if score >= 0.65:
        return "积极乐观"
    if score >= 0.13:
        return "谨慎乐观"
    if score <= -0.65:
        return "极度恐慌"
    if score <= -0.13:
        return "谨慎悲观"
    return "中性观察"


def _item_rank(item: dict[str, Any]) -> float:
    score = abs(float(item.get("score") or 0))
    conf = float(item.get("conf") or 0)
    age = item.get("age_h")
    freshness = 0.3 if age is None else max(0.0, 0.45 - min(float(age), 48) / 120)
    hits = min(3, _market_hits(str(item.get("title") or "")))
    return round(score * 3.2 + conf * 0.9 + freshness + hits * 0.42, 4)


def _dedupe_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact/near-exact syndicated headlines without hiding sources."""
    chosen: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    for raw in items:
        title = _plain(raw.get("title"))
        if len(title) < 5:
            continue
        key = _normal_title(title)
        # Wire copies often differ only after a long suffix; 34 chars is enough
        # to collapse them without merging unrelated short headlines.
        bucket = key[:34] if len(key) > 34 else key
        if not bucket:
            continue
        source = _plain(raw.get("source"))
        sources[bucket].add(source)
        row = dict(raw)
        row["title"] = title
        row["rank"] = _item_rank(row)
        if bucket not in chosen or row["rank"] > chosen[bucket]["rank"]:
            chosen[bucket] = row
    out = []
    for key, row in chosen.items():
        row["source_count"] = len([s for s in sources[key] if s])
        row["rank"] += min(0.8, max(0, row["source_count"] - 1) * 0.2)
        out.append(row)
    return out


def _diverse(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Keep useful topic diversity instead of six variants of one story."""
    ordered = sorted(items, key=lambda row: row.get("rank", 0), reverse=True)
    selected: list[dict[str, Any]] = []
    per_theme: Counter[str] = Counter()
    for row in ordered:
        title = str(row.get("title") or "")
        theme = _theme(title)
        if per_theme[theme] >= 2:
            continue
        norm = _normal_title(title)
        if any(norm[:18] and norm[:18] in _normal_title(x["title"])
               for x in selected):
            continue
        row = dict(row)
        row["theme"] = theme
        selected.append(row)
        per_theme[theme] += 1
        if len(selected) >= count:
            break
    return selected


def _topic_rows(items: list[dict[str, Any]], count: int = 6) -> list[dict[str, Any]]:
    candidates = [row for row in items if _market_hits(row["title"]) > 0]
    if len(candidates) < count:
        candidates = items
    picked = _diverse(candidates, count)
    denominator = sum(max(0.15, row.get("rank", 0)) for row in candidates) or 1
    return [{
        "text": _clip(row["title"], 44),
        "sentiment": _sentiment(float(row.get("score") or 0)),
        "weight": round(max(0.15, row.get("rank", 0)) / denominator * 100, 1),
        "theme": row.get("theme") or _theme(row["title"]),
        "source_count": int(row.get("source_count") or 1),
    } for row in picked]


def _directional_lines(items: list[dict[str, Any]], positive: bool,
                       limit: int = 5) -> list[str]:
    sign = (lambda x: float(x.get("score") or 0) > 0.12) if positive \
        else (lambda x: float(x.get("score") or 0) < -0.12)
    candidates = [row for row in items if sign(row) and _market_hits(row["title"]) > 0]
    picked = _diverse(candidates, limit)
    return [_clip(row["title"], 56) for row in picked]


def _fallback_columns(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    items = _dedupe_items(snapshot.get("items") or [])
    topics = _topic_rows(items)
    risks = _directional_lines(items, positive=False)
    opportunities = _directional_lines(items, positive=True)

    agg = snapshot.get("agg") or {}
    n = int(agg.get("n") or len(items))
    pos = int(agg.get("pos") or 0)
    neg = int(agg.get("neg") or 0)
    neutral = int(agg.get("neu") or max(0, n - pos - neg))
    long_p = int(agg.get("综合多头概率锚点") or 50)

    if not risks:
        risks.append("负面样本暂未形成集中主线，仍需留意突发消息与数据缺口")
    if neutral and len(risks) < 5:
        risks.append(f"中性样本 {neutral}/{n or neutral} 条，方向信号仍需后续数据确认")
    if long_p < 50 and len(risks) < 5:
        risks.append(f"空头概率 {100 - long_p}%，整体情绪锚点位于中枢下方")

    if not opportunities:
        opportunities.append("正面样本暂未形成高置信机会主线，等待多源交叉确认")
    if pos and len(opportunities) < 5:
        opportunities.append(f"正面样本 {pos}/{n} 条，可继续跟踪其是否扩散为板块主线")
    if long_p >= 50 and len(opportunities) < 5:
        opportunities.append(f"多头概率 {long_p}%，关注热点中获得多源印证的正面催化")

    return topics[:6], list(dict.fromkeys(risks))[:5], list(dict.fromkeys(opportunities))[:5]


def _extract_json(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _valid_ai_columns(value: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str], list[str]] | None:
    if not value:
        return None
    topics_raw = value.get("topics")
    risks_raw = value.get("risks")
    ops_raw = value.get("opportunities")
    if not isinstance(topics_raw, list) or not isinstance(risks_raw, list) or not isinstance(ops_raw, list):
        return None

    topics: list[dict[str, Any]] = []
    for raw in topics_raw[:6]:
        if isinstance(raw, str):
            text, sentiment, weight = _plain(raw), "中性观察", None
        elif isinstance(raw, dict):
            text = _plain(raw.get("text"))
            sentiment = _plain(raw.get("sentiment")) or "中性观察"
            weight = raw.get("weight")
        else:
            continue
        if not text:
            continue
        row: dict[str, Any] = {"text": _clip(text, 48), "sentiment": sentiment[:8]}
        try:
            if weight is not None and math.isfinite(float(weight)):
                row["weight"] = round(max(0.0, min(100.0, float(weight))), 1)
        except (TypeError, ValueError):
            pass
        topics.append(row)
    risks = [_clip(x, 64) for x in risks_raw[:5] if _plain(x)]
    ops = [_clip(x, 64) for x in ops_raw[:5] if _plain(x)]
    if not topics or not risks or not ops:
        return None
    return topics, risks, ops


class _DirectDeepSeek:
    """Small trusted adapter used only after the untrusted 007 collector exits."""

    @staticmethod
    def ai_ready(_provider: str) -> bool:
        return bool(os.environ.get("DEEPSEEK_API_KEY"))

    @staticmethod
    def ai_credentials(_provider: str) -> tuple[str, str, str, str, bool]:
        return (
            os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            os.environ.get("DEEPSEEK_API_KEY", ""),
            "DeepSeek",
            False,
        )

    @staticmethod
    def chat_completion(url: str, key: str, model: str, messages: list[dict[str, str]],
                        max_tokens: int, timeout: int, *, temperature: float) -> str:
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return str(payload["choices"][0]["message"]["content"])
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"DeepSeek response unavailable: {exc}") from exc


def _ai_columns(module: Any, snapshot: dict[str, Any], long_p: int,
                timeout: int) -> tuple[list[dict[str, Any]], list[str], list[str]] | None:
    """Optionally summarize 007 facts through a narrow, validated inference adapter."""
    try:
        if not module.ai_ready("deepseek"):
            return None
        url, model, key, _label, keyless = module.ai_credentials("deepseek")
        if not key and not keyless:
            return None
    except Exception:
        return None

    # Highest-ranked facts only: enough context for compression, bounded cost.
    items = sorted(_dedupe_items(snapshot.get("items") or []),
                   key=lambda row: row.get("rank", 0), reverse=True)[:70]
    facts = "\n".join(
        f"- [{row.get('label', '中性')} {float(row.get('score') or 0):+.2f}] "
        f"{_clip(row.get('title', ''), 90)}（{_plain(row.get('source'))}）"
        for row in items
    )
    prompt = f"""你是财经研报编辑。只根据下列仓库007本轮采集事实，压缩成墨水屏栏目。
不得新增事实、点位、评级或买卖建议。已由007本地模型计算：多头概率 {long_p}%，空头概率 {100-long_p}%。

严格只输出一个 JSON 对象，不要代码围栏：
{{
  "topics": [{{"text":"热点标题，最多28字","sentiment":"积极乐观/谨慎乐观/中性观察/谨慎悲观/极度恐慌","weight":数字}}],
  "risks": ["风险点，最多42字"],
  "opportunities": ["机会线索，最多42字"]
}}
要求：topics 3至6条，risks 2至5条，opportunities 2至5条；weight 是相对关注度，0至100，可不合计100；机会写研究线索而非买点。

事实：
{facts}"""
    try:
        response = module.chat_completion(
            url, key, model,
            [{"role": "system", "content": "你只输出严格 JSON，且不编造输入中没有的财经事实。"},
             {"role": "user", "content": prompt}],
            1800, timeout, temperature=0.25,
        )
        return _valid_ai_columns(_extract_json(response))
    except Exception as exc:  # AI is an optional enhancement; keep workflow green.
        print(f"warning: 007 inference unavailable, using deterministic reducer: {exc}", file=sys.stderr)
        return None


def _edition(now: datetime, requested: str) -> str:
    if requested in ("早报", "晚报"):
        return requested
    return "早报" if now.hour < 13 else "晚报"


def _source_commit(source_dir: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def collect_snapshot(source_dir: Path, hours: int, timeout: int) -> dict[str, Any]:
    if not (source_dir / "pushplus_deepseek.py").exists():
        raise SystemExit(f"007 source not found: {source_dir / 'pushplus_deepseek.py'}")
    sys.path.insert(0, str(source_dir))
    # Keep the checkout on sys.path until process exit: 007 lazily imports
    # inference_api and several collector helpers after collect_feeds returns.
    module = importlib.import_module("pushplus_deepseek")
    pack = module.collect_feeds(hours, timeout, topic="全球市场每日研报")

    flat: list[dict[str, Any]] = []
    for source, rows in (pack.items or {}).items():
        for item in rows:
            flat.append({
                "source": getattr(item, "source", "") or source,
                "title": getattr(item, "title", ""),
                "age_h": getattr(item, "age_h", None),
                "score": getattr(item, "score", 0),
                "label": getattr(item, "label", "中性"),
                "conf": getattr(item, "conf", 0),
            })
    return {
        "agg": dict(pack.agg or {}),
        "items": flat,
        "errors": [_plain(x) for x in (pack.errors or []) if _plain(x)],
        "_source_commit": _source_commit(source_dir),
    }


def build_report(snapshot: dict[str, Any], *, now: datetime | None = None,
                 edition: str = "auto", source_commit: str = "",
                 module: Any | None = None, timeout: int = 35) -> dict[str, Any]:
    now = (now or datetime.now(HKT)).astimezone(HKT)
    agg = snapshot.get("agg") or {}
    n = int(agg.get("n") or len(snapshot.get("items") or []))
    source_count = int(agg.get("n_sources") or len({
        _plain(row.get("source")) for row in snapshot.get("items") or []
        if _plain(row.get("source"))
    }))
    long_p = max(5, min(95, int(round(float(agg.get("综合多头概率锚点") or 50)))))
    if not snapshot.get("items"):
        raise RuntimeError("仓库007本轮没有取得可用内容，拒绝覆盖上一份有效研报")

    columns = _ai_columns(module, snapshot, long_p, timeout) if module is not None else None
    analysis = "007-inference" if columns else "007-rule"
    if not columns:
        columns = _fallback_columns(snapshot)
    topics, risks, opportunities = columns

    label = ("积极乐观" if long_p >= 70 else
             "谨慎乐观" if long_p >= 55 else
             "谨慎悲观" if long_p <= 45 else
             "中性观察")
    errors = snapshot.get("errors") or []
    report = {
        "schema": SCHEMA_VERSION,
        "title": TITLE,
        "id": f"{now.strftime('%Y%m%dT%H%M%S%z')}-{source_commit[:8] or '007'}",
        "generated_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "edition": _edition(now, edition),
        "source": {
            "repository": "k-macao/007",
            "commit": source_commit,
            "collector": "pushplus_deepseek.collect_feeds",
            "analysis": analysis,
            "window_hours": 24,
        },
        "sample_count": n,
        "source_count": source_count,
        "probability": {
            "long": long_p,
            "short": 100 - long_p,
            "label": label,
            "note": f"基于仓库007本轮 {n} 条内容 · {source_count} 源",
        },
        "topics": topics[:6],
        "risks": risks[:5],
        "opportunities": opportunities[:5],
        "data_gaps": [_clip(x, 80) for x in errors[:5]],
        "disclaimer": "仅供研究参考，不构成投资建议",
    }
    return report


def _load_snapshot(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise SystemExit("snapshot must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync compact report from k-macao/007")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-dir", type=Path, help="fresh checkout of k-macao/007")
    source.add_argument("--snapshot", type=Path, help="offline collector snapshot (tests/debug)")
    parser.add_argument("--output", type=Path, default=Path("data/007-report.json"))
    parser.add_argument("--snapshot-output", type=Path,
                        help="also save the raw 007 collector result for a separate trusted render step")
    parser.add_argument("--edition", choices=("auto", "早报", "晚报"), default="auto")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--now", help="ISO time override for reproducible tests")
    parser.add_argument("--no-ai", action="store_true", help="force deterministic 007 reducer")
    args = parser.parse_args(argv)

    if args.source_dir:
        snapshot = collect_snapshot(args.source_dir.resolve(), args.hours, args.timeout)
    else:
        snapshot = _load_snapshot(args.snapshot)
    commit = _plain(snapshot.get("_source_commit"))

    if args.snapshot_output:
        args.snapshot_output.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot_output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # Inference is deliberately available only in snapshot mode. This lets CI run
    # mutable code from 007 without exposing an API key in the same process.
    module = _DirectDeepSeek if (args.snapshot and not args.no_ai) else None
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(HKT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=HKT)
    report = build_report(snapshot, now=now, edition=args.edition,
                          source_commit=commit, module=module, timeout=args.timeout)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {report['edition']} {report['probability']['long']}/"
          f"{report['probability']['short']} · {len(report['topics'])} topics · "
          f"{len(report['risks'])} risks · {len(report['opportunities'])} opportunities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

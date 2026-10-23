#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""◈ Jev 式类型化决策接入层 —— 把 GitHub 开源的 Jev / SystemOne 类模型接进日报。

背景（2026-10-03 调研，结论见仓库文档《Jev开源模型-接入可行性验证.md》）：
  Jev 是「只判断、不生成」的类型化决策模型：给一份 state 和几个带类型的问题，
  一次前向返回能直接 if 的值 + 已校准的概率，不产出任何解释文本。三个原语：
    · choice —— 在候选选项里选一个，返回各选项概率；
    · score  —— 按档位打分，返回各档概率与期望分；
    · noul   —— 判断命题是否成立，返回 P(真)。
  GitHub 上的开源复现（laya / kev / NanoJev / OpenJev）都提供官方协议
  ``POST /v1/systemone``；本地推理（EdgeJev 一类）只需要 onnxruntime + tokenizers + numpy，
  不依赖 torch，也不需要 API Key，模型拉一次之后断网可用。

本模块的角色（与 AI 七日港股里的「大模型研判层」并列，可二选一）：
  · 只读当次快照构造闭合 state（防未来函数，见 assert_closed_snapshot）；
  · 把日报要的几类判断映射到 Jev 的三个原语（方向 / 命题概率 / 风险等级）；
  · 调用端点（本地 edgejev serve、内网服务或任意协议兼容实现），严格校验返回结构；
  · 与量化基准对账：偏离 >20pp 收敛、概率夹在 5%~95%——复用 hk_seven_day 的同一份常量，
    不另立一套边界；
  · 留痕与结算沿用 AI 七日港股的 journal 口径（jev_forecast.json，T+7 结算）。

缺席口径（与现有栏目完全一致，绝不静默）：
  · 未配置端点（OCTOPUS_JEV_BASE_URL / TYPESAFE_BASE_URL 都为空）→ enabled=False，整栏缺席；
  · 已配置但服务不可用 / 返回不合法 → 记录原因、回退量化基准，绝不猜概率、不编数字。

为什么它比「提示大模型自报概率」更省护栏：
  类型化读出没有自然语言，就没有「编造数字」的入口；本模块只对概率做对账（收敛 + 夹边），
  文案一律走量化模板。相对的代价是：它不会给解释，只给值。

用法::

    python3 output/jev_bridge.py --check                 # 探活本地 /v1/systemone
    python3 output/jev_bridge.py --demo                  # 用内置闭合快照跑三个原语并合并
    python3 output/jev_bridge.py --demo --base http://127.0.0.1:8009
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# 口径复用：常量、夹边、留痕与结算都用 AI 七日港股那一份，避免两套边界值漂移。
from hk_seven_day import (  # noqa: E402
    HORIZON,
    MAX_PROB_DEVIATION,
    MIN_JOURNAL_FOR_HITRATE,
    PROB_CAP,
    PROB_FLOOR,
    _clamp,
    _journal_stats,
    _load_journal,
    _save_journal,
    _settle,
)

CST = timezone(timedelta(hours=8))

# ---- 协议常量（官方 /v1/systemone，EdgeJev 等开源实现逐字兼容）----
SYSTEM_ONE_PATH = "/v1/systemone"
READY_PATH = "/ready"
QTYPES = ("choice", "score", "noul")

# ---- 配置：端点可选，Key 可选（本地服务通常不需要）----
ENV_BASE_NAMES = ("OCTOPUS_JEV_BASE_URL", "TYPESAFE_BASE_URL")
ENV_KEY_NAMES = ("OCTOPUS_JEV_API_KEY", "TYPESAFE_API_KEY")
DEFAULT_TIMEOUT = 30
USER_AGENT = "octopus-ai-daily/1.0 (+jev-bridge)"
JOURNAL_FILENAME = "jev_forecast.json"
JOURNAL_MAX_ENTRIES = 120
PROB_SUM_TOLERANCE = 0.05          # 选项概率和允许的偏差；超出即判返回不合法
RISK_LEVELS = ("很低", "较低", "中性", "较高", "很高")

# 闭合输入白名单：只允许把「t 及之前」的快照字段交给模型（对齐 hk_seven_day._build_context）
SNAPSHOT_FIELDS = (
    "symbol", "name", "short", "asof", "close",
    "ret5", "ret10", "ret20", "ret60", "ma20_dev", "ma60_dev",
    "rsi14", "vol_pct", "vol20_ann", "hi20", "lo20", "hi52", "lo52",
    "factor_score", "factor_summary", "bars_n",
)
# 黑名单：任何「t 之后」的结果字段都不许进 state（防未来函数 / 防标签泄漏）
FORBIDDEN_FIELDS = (
    "actual_ret", "actual_dir", "hit", "brier", "settle_date", "settled",
    "target_close", "future_close", "next_close", "label", "y_true",
)

_MODEL_NOTE = "Jev 类型化决策（/v1/systemone）"


# ============================================================
# 配置与探活
# ============================================================
def jev_config(env=None):
    """读取接入配置；没有端点时 enabled=False（整栏缺席）。

    ``mode`` 三档（``OCTOPUS_JEV_MODE``），与 ``OCTOPUS_HK7_FALLBACK`` 同构：
      · auto（默认）——未配端点 → 缺席；已配端点但调用失败 → 回退量化基准并记原因；
      · always（=1）——端点是必需项，缺了就整栏缺席（失败也不回退）；
      · never（=0）——端点是必需项，缺了就整栏缺席（失败也不回退）。
    """
    env = os.environ if env is None else env
    base = ""
    for name in ENV_BASE_NAMES:
        val = str(env.get(name) or "").strip()
        if val:
            base = val.rstrip("/")
            break
    key = ""
    for name in ENV_KEY_NAMES:
        val = str(env.get(name) or "").strip()
        if val:
            key = val
            break
    try:
        timeout = max(3, min(180, int(str(env.get("OCTOPUS_JEV_TIMEOUT") or DEFAULT_TIMEOUT))))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    raw_mode = str(env.get("OCTOPUS_JEV_MODE") or "").strip().lower()
    if raw_mode in ("0", "never"):
        mode = "never"
    elif raw_mode in ("1", "always"):
        mode = "always"
    else:
        mode = "auto"
    # 端点里带 /v1/systemone 也认（有人习惯把整条 URL 写进环境变量）
    if base.endswith(SYSTEM_ONE_PATH):
        base = base[: -len(SYSTEM_ONE_PATH)]
    return {"enabled": bool(base), "base": base, "key": key, "timeout": timeout,
            "mode": mode, "label": _MODEL_NOTE}


def _post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT):
    """标准库 POST → 解析 JSON；异常直接抛出，由调用方降级（不静默）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_json(url, headers=None, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe(config, get_json=None):
    """探活：GET /ready → (ok, 说明文字)。用于 --check 与日报的数据覆盖审计。"""
    if not config.get("enabled"):
        return False, "未配置 OCTOPUS_JEV_BASE_URL / TYPESAFE_BASE_URL"
    get = get_json or _get_json
    try:
        data = get(config["base"] + READY_PATH, None, int(config.get("timeout") or DEFAULT_TIMEOUT))
    except Exception as exc:                      # 网络 / HTTP / 解析异常一律降级
        return False, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return False, "/ready 返回不是 JSON 对象"
    backend = data.get("backend")
    precision = data.get("precision")
    return True, (f"就绪：{data.get('model') or _MODEL_NOTE}"
                  + (f"（{backend}/{precision}）" if backend else ""))


# ============================================================
# 闭合输入：只读当次快照
# ============================================================
def build_snapshot(ctx):
    """从 AI 七日的上下文里裁出闭合 state（白名单字段 + 纯标量）。"""
    state = {}
    for field in SNAPSHOT_FIELDS:
        val = ctx.get(field)
        if val is None:
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if not math.isfinite(float(val)):
                continue
            state[field] = round(float(val), 6) if isinstance(val, float) else val
        elif isinstance(val, str):
            state[field] = val[:120]
    return state


def assert_closed_snapshot(state, asof=None, target=None):
    """闭合自检：黑名单字段不得出现；带日期时目标日必须严格晚于基准日。"""
    bad = sorted(k for k in state if k in FORBIDDEN_FIELDS)
    if bad:
        return False, "state 里出现了预测时点之后才有的字段：" + "、".join(bad)
    for key, val in state.items():
        if isinstance(val, (dict, list)):
            return False, f"state.{key} 不是标量，闭合输入只接受标量快照"
    if asof and target and not str(target) > str(asof):
        return False, f"目标日 {target} 必须严格晚于基准日 {asof}"
    return True, "闭合：只含当次快照字段（t 及之前）"


# ============================================================
# 问题构造：日报的三类判断 → Jev 的三个原语
# ============================================================
def build_questions(ctx):
    """把单个标的的研判拆成 3 道题，一次请求打包（Jev 的主场：一次前向答完多题）。"""
    name = ctx.get("short") or ctx.get("name") or ctx.get("symbol") or "标的"
    h = HORIZON
    return {
        "dir": {
            "type": "choice",
            "instructions": f"{name}未来 {h} 个交易日的主方向（以区间收益 ±1.0% 为界）",
            "criteria": {
                "up": "区间收益高于 +1.0%",
                "flat": "区间收益在 -1.0% 到 +1.0% 之间",
                "down": "区间收益低于 -1.0%",
            },
        },
        "p_up": {
            "type": "noul",
            "instructions": f"{name}在 {h} 个交易日后的收盘价高于基准日 {ctx.get('asof') or ''} 的收盘价",
            "criteria": {"true": "收盘价更高", "false": "收盘价不高"},
        },
        "risk": {
            "type": "score",
            "instructions": f"{name}这 {h} 个交易日的不确定性等级",
            "criteria": list(RISK_LEVELS),
        },
    }


def system_one(config, state, questions, post_json=None):
    """调一次 ``POST /v1/systemone``；返回 (answers, error)。任何一步失败都返回 (None, 原因)。"""
    if not config.get("enabled"):
        return None, "未配置 Jev 端点（OCTOPUS_JEV_BASE_URL / TYPESAFE_BASE_URL）"
    post = post_json or _post_json
    headers = {}
    if config.get("key"):
        headers["Authorization"] = f"Bearer {config['key']}"
    try:
        data = post(config["base"] + SYSTEM_ONE_PATH,
                    {"state": state, "questions": questions},
                    headers, int(config.get("timeout") or DEFAULT_TIMEOUT))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(((body or {}).get("error") or {}).get("message") or "")
        except Exception:
            detail = ""
        return None, f"HTTP {exc.code}{(' · ' + detail) if detail else ''}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "返回不是 JSON 对象"
    if isinstance(data.get("error"), dict):
        return None, str(data["error"].get("message") or "服务返回 error 字段")
    answers = data.get("answers")
    if not isinstance(answers, dict) or not answers:
        return None, "响应缺少 answers"
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    answers["_meta"] = {"model": str(data.get("model") or ""),
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens")}
    return answers, ""


# ============================================================
# 返回校验：结构不对就判无效（宁可缺席，不要半个概率）
# ============================================================
def _as_prob(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and 0.0 <= out <= 1.0 else None


def validate_answers(answers, questions):
    """逐题校验 → (规范化结果, problems)。problems 非空即视为该题不可用。"""
    norm, problems = {}, []
    if not isinstance(answers, dict):
        return {}, ["answers 不是对象"]
    for qid, qdef in questions.items():
        item = answers.get(qid)
        if not isinstance(item, dict):
            problems.append(f"{qid}: 缺少答案")
            continue
        qtype = qdef.get("type")
        declared = item.get("type")
        if declared not in (None, qtype):
            problems.append(f"{qid}: 类型不符（{declared} ≠ {qtype}）")
            continue
        conf = _as_prob(item.get("confidence"))
        if qtype == "choice":
            keys = list(qdef["criteria"].keys())
            probs = item.get("probabilities")
            if not isinstance(probs, dict) or set(probs.keys()) != set(keys):
                problems.append(f"{qid}: 选项概率缺失或键不匹配")
                continue
            vals = [_as_prob(probs[k]) for k in keys]
            if any(v is None for v in vals):
                problems.append(f"{qid}: 概率不是 0~1 的数")
                continue
            total = sum(vals)
            if total <= 0 or abs(total - 1.0) > PROB_SUM_TOLERANCE:
                problems.append(f"{qid}: 概率和 {total:.3f} 不在 1±{PROB_SUM_TOLERANCE}")
                continue
            dist = {k: v / total for k, v in zip(keys, vals)}
            top = max(dist, key=dist.get)
            if item.get("choice") not in (None, top):
                problems.append(f"{qid}: choice={item.get('choice')} 与概率最大的 {top} 不一致")
                continue
            norm[qid] = {"type": "choice", "choice": top, "probabilities": dist,
                         "confidence": conf}
        elif qtype == "score":
            levels = list(qdef["criteria"])
            probs = item.get("probabilities")
            vals = None
            if isinstance(probs, dict) and probs:
                try:
                    vals = [_as_prob(probs[str(i)]) for i in range(len(levels))]
                except Exception:
                    vals = None
            if vals is None or any(v is None for v in vals):
                vals = None
            if vals is not None:
                total = sum(vals)
                if total > 0 and abs(total - 1.0) <= PROB_SUM_TOLERANCE:
                    vals = [v / total for v in vals]
                else:
                    vals = None
            score = None
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                score = None
            if vals is not None and score is None:
                score = sum(i * v for i, v in enumerate(vals))
            if score is None or not (0.0 <= score <= len(levels) - 1 + 1e-6):
                problems.append(f"{qid}: 分数缺失或超出 0~{len(levels) - 1} 档")
                continue
            norm[qid] = {"type": "score", "score": round(score, 4),
                         "probabilities": vals, "confidence": conf}
        elif qtype == "noul":
            p = _as_prob(item.get("noul"))
            if p is None:
                problems.append(f"{qid}: noul 概率缺失或不在 0~1")
                continue
            norm[qid] = {"type": "noul", "noul": p, "confidence": conf}
        else:
            problems.append(f"{qid}: 未知题型 {qtype}")
    return norm, problems


# ============================================================
# 与量化基准对账（三道防线里的两道：收敛 + 夹边）
# ============================================================
def merge_jev(norm, baseline):
    """把规范化答案合并到量化基准上。

    ``baseline``：``{"quant_p_up": 0.53, "name": "恒指"}``。
    返回 {p_up, quant_p_up, converged, source, direction, label, risk, confidence, note}
    —— ``source`` 说明概率来自哪个原语，方便页面与审计追溯。
    """
    name = baseline.get("name") or baseline.get("short") or baseline.get("symbol") or "标的"
    q_p = _clamp(baseline.get("quant_p_up") if baseline.get("quant_p_up") is not None else 0.5)
    choice = (norm or {}).get("dir")
    # 方向分布 → 概率：上行记 1、震荡按半仓记 0.5（口径写死，可复现）
    p_choice = None
    if isinstance(choice, dict) and choice.get("probabilities"):
        dist = choice["probabilities"]
        p_choice = float(dist.get("up", 0.0)) + 0.5 * float(dist.get("flat", 0.0))
    p_raw, source = None, ""
    noul = (norm or {}).get("p_up")
    if isinstance(noul, dict) and noul.get("noul") is not None:
        p_raw, source = float(noul["noul"]), "noul"
    elif p_choice is not None:
        p_raw, source = p_choice, "choice"
    converged = False
    if p_raw is None:
        p_up, source, note = q_p, "quant", "模型未给可用概率，沿用量化基准"
    else:
        p_up = _clamp(p_raw)
        if abs(p_up - q_p) > MAX_PROB_DEVIATION:
            p_up = _clamp(q_p + math.copysign(MAX_PROB_DEVIATION, p_up - q_p))
            converged = True
            note = f"模型概率 {p_raw:.3f} 偏离基准 {q_p:.3f} 超过 {MAX_PROB_DEVIATION:.0%}，已收敛"
        else:
            note = f"模型概率 {p_raw:.3f} 与量化基准 {q_p:.3f} 一致（未触发收敛）"
    # 跨题一致性只是提示位：Jev 的多个问题是各自独立作答的（兄弟题互相看不到），
    # 不保证自洽，所以以命题题（noul）为准，方向题（choice）只用于展示与兜底。
    coherence = None
    if source == "noul" and p_choice is not None:
        delta = abs(float(p_raw) - p_choice)
        coherence = {"p_choice": p_choice, "delta": delta, "consistent": delta <= 0.25}
        if not coherence["consistent"]:
            note += (f"；方向题折算 {p_choice:.3f} 与命题题 {p_raw:.3f} 相差 {delta:.3f}"
                     "（各题独立作答，以命题题为准）")
    direction = "up" if p_up >= 0.5 else "down"
    risk = (norm or {}).get("risk") or {}
    conf = None
    for qid in ("p_up", "dir", "risk"):
        cand = ((norm or {}).get(qid) or {}).get("confidence")
        if cand is not None:
            conf = cand
            break
    return {
        "name": name,
        "p_up": _clamp(p_up),
        "quant_p_up": q_p,
        "raw_p_up": p_raw,
        "source": source,
        "converged": converged,
        "direction": direction,
        "label": "偏多" if p_up >= 0.5 else "偏空",
        "risk_score": risk.get("score"),
        "risk_level": (RISK_LEVELS[int(round(risk["score"]))]
                       if risk.get("score") is not None
                       and 0 <= int(round(risk["score"])) < len(RISK_LEVELS) else None),
        "confidence": conf,
        "note": note,
        "coherence": coherence,
        "engine": f"jev · {source}" if source else "quant",
    }


# ============================================================
# 留痕与结算（沿用 AI 七日港股的 journal 结构，文件名独立）
# ============================================================
def issue_entry(entries, merged, ctx):
    """按最新快照签发一条预测；同一标的同一基准日只签发一次。"""
    symbol = ctx.get("symbol")
    base_date = ctx.get("asof")
    if not symbol or not base_date:
        return None
    for entry in entries:
        if (entry.get("symbol") == symbol and entry.get("base_date") == base_date
                and entry.get("engine", "").startswith("jev")):
            return None
    ok_seq, seq_note = assert_closed_snapshot(build_snapshot(ctx), base_date,
                                              ctx.get("target_date"))
    entry = {
        "base_date": base_date,
        "base_close": ctx.get("close"),
        "target_date": ctx.get("target_date"),
        "symbol": symbol,
        "symbol_label": ctx.get("name") or ctx.get("short") or symbol,
        "target_sessions": HORIZON,
        "p_up": merged["p_up"],
        "quant_p_up": merged.get("quant_p_up"),
        "raw_p_up": merged.get("raw_p_up"),
        "prob_source": merged.get("source"),
        "converged": bool(merged.get("converged")),
        "risk_score": merged.get("risk_score"),
        "confidence": merged.get("confidence"),
        "engine": merged.get("engine") or "jev",
        "direction": merged.get("direction"),
        "label": merged.get("label"),
        "no_lookahead": {"ok": ok_seq, "note": seq_note},
        "issued_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S%z"),
        "settled": False,
    }
    entries.append(entry)
    return entry


def run(config=None, contexts=None, baseline_map=None, env=None, post_json=None,
        journal_path=None, now=None):
    """一次完整接入：探活 → 逐标的提问 → 校验 → 对账 → 留痕。

    返回 ``{"enabled", "ready", "ready_note", "targets": [...], "problems": [...],
    "stats": {...}}``；任何环节失败都如实记录，不抛异常、不猜概率。
    """
    config = config or jev_config(env)
    out = {"enabled": bool(config.get("enabled")), "ready": False, "ready_note": "",
           "label": config.get("label") or _MODEL_NOTE, "targets": [], "problems": [],
           "stats": None}
    if not config.get("enabled"):
        out["problems"].append("未配置 Jev 端点 → 整栏缺席（不影响其它栏目）")
        return out
    ok, note = probe(config)
    out["ready"], out["ready_note"] = ok, note
    if not ok and config.get("mode") == "always":
        out["problems"].append(f"端点不可用且 OCTOPUS_JEV_MODE=always → 整栏缺席：{note}")
        return out
    entries = _load_journal(journal_path)["entries"] if journal_path else []
    issued_any = False
    for ctx in contexts or []:
        state = build_snapshot(ctx)
        closed_ok, closed_note = assert_closed_snapshot(state, ctx.get("asof"),
                                                        ctx.get("target_date"))
        if not closed_ok:
            out["problems"].append(f"{ctx.get('symbol')}: 输入未通过闭合自检（{closed_note}）")
            out["targets"].append({"symbol": ctx.get("symbol"), "name": ctx.get("name"),
                                   "closed_note": closed_note,
                                   "error": f"输入未通过闭合自检：{closed_note}",
                                   "answers": None, "merged": None})
            continue
        answers, err = system_one(config, state, build_questions(ctx), post_json=post_json)
        item = {"symbol": ctx.get("symbol"), "name": ctx.get("name"),
                "closed_note": closed_note, "error": err, "answers": None, "merged": None}
        if answers is None:
            out["problems"].append(f"{ctx.get('symbol')}: {err}")
            out["targets"].append(item)
            continue
        norm, problems = validate_answers(answers, build_questions(ctx))
        item["answers"] = norm
        item["model_meta"] = answers.get("_meta") or {}
        if problems:
            out["problems"].extend(f"{ctx.get('symbol')}/{p}" for p in problems)
        base = dict(baseline_map.get(ctx.get("symbol"), {}) if baseline_map else {})
        base.setdefault("name", ctx.get("short") or ctx.get("name"))
        base.setdefault("quant_p_up", (ctx.get("quant") or {}).get("p_up"))
        merged = merge_jev(norm, base)
        item["merged"] = merged
        if journal_path:
            entry = issue_entry(entries, merged, ctx)
            if entry:
                issued_any = True
                item["issued"] = {k: entry[k] for k in ("base_date", "target_date", "p_up")}
        out["targets"].append(item)
    if journal_path and (issued_any or os.path.isfile(journal_path)):
        _save_journal(journal_path, {"version": 1, "entries": entries})
    if journal_path:
        out["stats"] = _journal_stats(entries)
    return out


# ============================================================
# CLI：探活 / 演示
# ============================================================
_DEMO_CONTEXT = {
    "symbol": "^HSI", "name": "恒生指数", "short": "恒指",
    "asof": "2026-10-02", "target_date": "2026-10-14", "close": 25413.0,
    "ret5": 0.012, "ret10": -0.004, "ret20": 0.031, "ret60": 0.058,
    "ma20_dev": 0.008, "ma60_dev": 0.021, "rsi14": 57.3, "vol_pct": 0.42,
    "vol20_ann": 0.187, "hi20": 25880.0, "lo20": 24610.0,
    "hi52": 26120.0, "lo52": 19850.0, "bars_n": 120,
    "factor_score": 0.18, "factor_summary": "动量延续 · 趋势向上 · 南向资金流入",
    "quant": {"p_up": 0.56},
    # 下面这几个字段会被白名单丢掉（演示闭合裁剪确实生效）
    "actual_ret": 0.031, "hit": True,
}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="◈ Jev 类型化决策接入层（/v1/systemone）")
    parser.add_argument("--check", action="store_true", help="只探活端点（GET /ready）")
    parser.add_argument("--demo", action="store_true", help="用内置闭合快照跑一次完整接入")
    parser.add_argument("--base", default="", help="临时指定端点，覆盖环境变量")
    parser.add_argument("--api-key", default="", help="临时指定 Bearer Key")
    parser.add_argument("--journal", default="", help="留痕文件路径（给定时写入）")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.base:
        env["OCTOPUS_JEV_BASE_URL"] = args.base
    if args.api_key:
        env["OCTOPUS_JEV_API_KEY"] = args.api_key
    config = jev_config(env)

    if args.check:
        ok, note = probe(config)
        print(("✅ " if ok else "⚠️ ") + note)
        return 0 if ok else 1
    if not args.demo:
        parser.print_help()
        return 0

    result = run(config=config, contexts=[_DEMO_CONTEXT],
                 baseline_map={"^HSI": {"quant_p_up": 0.56, "name": "恒指"}},
                 journal_path=args.journal or None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        return 0 if result.get("targets") else 1
    print(f"端点：{config.get('base') or '（未配置）'} | 探活：{result['ready_note']}")
    for item in result["targets"]:
        print(f"\n◈ {item['name']}（{item['symbol']}）")
        print(f"  闭合自检：{item['closed_note']}")
        if item.get("error"):
            print(f"  调用失败：{item['error']}")
            continue
        for qid, ans in (item.get("answers") or {}).items():
            if ans["type"] == "choice":
                print(f"  [choice] {qid} → {ans['choice']} "
                      f"{ {k: round(v, 3) for k, v in ans['probabilities'].items()} }")
            elif ans["type"] == "score":
                print(f"  [score]  {qid} → {ans['score']}（置信 {ans['confidence']}）")
            else:
                print(f"  [noul]   {qid} → P={ans['noul']:.4f}（置信 {ans['confidence']}）")
        merged = item.get("merged") or {}
        print(f"  合并：P(7日涨)={merged.get('p_up'):.4f} | 量化基准={merged.get('quant_p_up'):.4f}"
              f" | 来源={merged.get('source')} | 收敛={merged.get('converged')}")
        print(f"  说明：{merged.get('note')}")
    if result["problems"]:
        print("\n⚠️ 记录到的问题（不静默）：")
        for p in result["problems"]:
            print("  · " + p)
    return 0 if result.get("targets") else 1


if __name__ == "__main__":
    raise SystemExit(main())

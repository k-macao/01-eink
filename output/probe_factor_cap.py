#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🔬 因子软压缩（tanh cap）数据核查 —— 只读诊断，不改任何生产逻辑。

回答一个问题：页面上显示的五因子分（±2 以内）到底被 tanh 压缩削掉了多少？

  ① 压缩曲线        —— cap=2 下 输入 → 输出 的精确数学（trim = 1 - out/|in|）
  ② 滚动回放        —— 用与 tests 同源的 GBM 合成日线（四种行情形态）逐日滚动调
                        features.compute_features + factor_scores，把显示值反变换
                        回原始值（tanh 可逆：raw = cap·atanh(c/cap)），统计真实削幅
  ③ FLOW 双重压缩   —— 先 cap=1.5 再 cap=2，两层叠加的可达上限
  ④ VOL 预夹紧      —— 代码里已先夹 |vz|≤3×0.6=1.8，第二层 tanh 还会继续削

用法：
    python3 output/probe_factor_cap.py            # 打印全部数据
    python3 output/probe_factor_cap.py --md       # 额外写出 根目录/因子软压缩-数据核查.md

无网络依赖（沙箱离线可跑）；全部为确定性计算，同种子同输出。
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "output"))

from octopus_quant import features  # noqa: E402

FACTOR_CAP = features.FACTOR_CAP          # 2.0
FLOW_INNER_CAP = 1.5                      # flow 在 factor_scores 里先压一次的上限
SCORE_CAP = 2.0                           # 综合分再压一次的上限


# ------------------------------------------------------------------ 工具
def cap_tanh(v: float, cap: float = FACTOR_CAP) -> float:
    return cap * math.tanh(v / cap)


def uncapped(c: float, cap: float = FACTOR_CAP) -> float:
    """压缩值 → 原始值（tanh 严格单调且 |c|<cap，可逆）。"""
    c = max(-(cap - 1e-12), min(cap - 1e-12, float(c)))
    return cap * math.atanh(c / cap)


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def quantile(xs, q):
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


# ------------------------------------------------------------------ ① 曲线
def curve_table():
    rows = []
    for v in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
        c = cap_tanh(v)
        rows.append((v, c, c / v, 1.0 - c / v))
    return rows


# ------------------------------------------------------------------ 合成日线
def synth_bars(n, start, drift, vol, seed, first_day=(2025, 9, 1)):
    """与 tests/test_quant.py::synth_bars 同构造的 GBM 日线（跳过周末）。"""
    rnd = random.Random(seed)
    bars, price = [], start
    day = datetime(*first_day)
    while len(bars) < n:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        price = max(1.0, price * math.exp(rnd.gauss(drift, vol)))
        bars.append({
            "date": day.strftime("%Y-%m-%d"),
            "open": price * (1 - rnd.gauss(0, 0.002)),
            "high": price * (1 + abs(rnd.gauss(0, 0.006))),
            "low": price * (1 - abs(rnd.gauss(0, 0.006))),
            "close": price,
            "volume": max(1e5, 1e8 * (1 + 0.4 * rnd.gauss(0, 1))),
        })
    return bars


REGIMES = [
    # (名称, drift, vol, seed) —— 覆盖缓牛 / 阴跌 / 高波动震荡 / 单边强趋势
    ("缓牛 2025Q4", 0.0006, 0.011, 11),
    ("阴跌", -0.0008, 0.014, 22),
    ("高波动震荡", 0.0001, 0.024, 33),
    ("单边强趋势", 0.0015, 0.016, 44),
]


def replay(min_window=60, window_step=1, bars_per_regime=280):
    """逐日滚动回放，收集反变换后的原始因子值与压缩削幅。"""
    raw_by = {k: [] for k in ("mom", "trd", "rev", "vol")}
    comp_by = {k: [] for k in ("mom", "trd", "rev", "vol")}
    weighted_list = []   # 综合分进入二次压缩前的加权值
    final_list = []      # 二次压缩后的显示值
    n_windows = 0

    for _name, drift, vol, seed in REGIMES:
        bars = synth_bars(bars_per_regime, 20000.0, drift, vol, seed)
        for end in range(min_window, len(bars) + 1, window_step):
            feat = features.compute_features(bars[:end])
            if not feat.get("ok"):
                continue
            res = features.factor_scores(feat)      # flow 缺席（权重已重分配）
            if not res.get("ok"):
                continue
            n_windows += 1
            for k in raw_by:
                c = res["factors"].get(k)
                if c is None:
                    continue
                r = uncapped(c, FACTOR_CAP)
                raw_by[k].append(r)
                comp_by[k].append(c)
            ws = res["weights_used"]
            weighted = sum(ws[k] * res["factors"][k] for k in ws)
            weighted_list.append(weighted)
            final_list.append(res["score"])
    return n_windows, raw_by, comp_by, weighted_list, final_list


def factor_stats(raw, comp):
    """逐因子统计：原始值分布 + 实际削幅。"""
    n = len(raw)
    if n == 0:
        return None
    absraw = [abs(r) for r in raw]
    trims = [1.0 - abs(c) / abs(r) for r, c in zip(raw, comp) if abs(r) > 1e-9]
    over_cap = sum(1 for r in absraw if r >= FACTOR_CAP)
    over_1 = sum(1 for r in absraw if r >= 1.0)
    trim10 = sum(1 for t in trims if t >= 0.10)
    return {
        "n": n,
        "raw_min": min(raw), "raw_max": max(raw),
        "abs_p50": quantile(absraw, 0.50),
        "abs_p95": quantile(absraw, 0.95),
        "abs_max": max(absraw),
        "share_ge1": over_1 / n,
        "share_ge2": over_cap / n,
        "trim_mean": (sum(trims) / len(trims)) if trims else 0.0,
        "trim_p95": quantile(trims, 0.95),
        "share_trim10": trim10 / n,
    }


# ------------------------------------------------------------------ ③ FLOW
def flow_rows():
    out = []
    for z in (0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 10.0):
        inner = cap_tanh(z, FLOW_INNER_CAP)      # 第一层：cap=1.5
        final = cap_tanh(inner, FACTOR_CAP)      # 第二层：cap=2.0
        single = cap_tanh(z, FACTOR_CAP)         # 对照：只压一次 cap=2
        out.append((z, inner, final, single))
    return out, cap_tanh(FLOW_INNER_CAP, FACTOR_CAP)


# ------------------------------------------------------------------ ④ VOL
def vol_rows():
    out = []
    for vz in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0):   # vz 为量能 z 值
        raw = min(abs(vz), 3.0) * 0.6            # 代码预夹紧：|raw| ≤ 1.8
        final = cap_tanh(raw, FACTOR_CAP)
        out.append((vz, raw, final, 1.0 - final / raw))
    return out


# ------------------------------------------------------------------ 输出
def build_lines():
    lines = []
    add = lines.append

    add("=" * 78)
    add("因子软压缩（tanh cap）数据核查")
    add("=" * 78)

    add("")
    add("■ ① 压缩曲线（cap=2，精确数学）：显示值 = 2·tanh(原始值/2)")
    add(f"  {'原始':>6} {'显示':>8} {'保留':>8} {'削幅':>8}")
    for v, c, keep, trim in curve_table():
        add(f"  {v:>6.2f} {c:>8.3f} {pct(keep):>8} {pct(trim):>8}")
    add("  → |原始|=1 已削 7.6%；=2 削 23.8%；=4 削 51.8%。压缩没有硬门限，")
    add("    越接近 ±2 显示值越饱和，±2 永远到不了（tanh 渐近）。")

    add("")
    add("■ ② 滚动回放（4 种行情形态 × 逐日窗口；与 tests 同源的 GBM 合成日线，")
    add("   沙箱无外网故不用实盘行情；显示值经 atanh 反变换还原为原始值）")
    n_windows, raw_by, comp_by, weighted_list, final_list = replay()
    add(f"  样本窗口数：{n_windows}（形态：{' / '.join(r[0] for r in REGIMES)}，逐日滚动）")
    add("")
    add(f"  {'因子':<6} {'n':>5} {'raw最小':>8} {'raw最大':>8} {'|raw|中位':>9} "
        f"{'|raw|p95':>9} {'≥±1':>7} {'≥±2':>7} {'均削幅':>7} {'削≥10%':>8}")
    stats_all = {}
    for k, label in (("mom", "MOM"), ("trd", "TRD"), ("rev", "REV"), ("vol", "VOL")):
        st = factor_stats(raw_by[k], comp_by[k])
        stats_all[k] = st
        if not st:
            continue
        add(f"  {label:<6} {st['n']:>5} {st['raw_min']:>8.2f} {st['raw_max']:>8.2f} "
            f"{st['abs_p50']:>9.2f} {st['abs_p95']:>9.2f} "
            f"{pct(st['share_ge1']):>7} {pct(st['share_ge2']):>7} "
            f"{pct(st['trim_mean']):>7} {pct(st['share_trim10']):>8}")
    add("  （≥±1 / ≥±2 = 原始值达到该幅度的窗口占比；削≥10% = 显示值比原始值")
    add("   至少小一成的窗口占比；flow 缺席、权重按 0.30/0.25/0.15/0.15 重分配）")

    add("")
    add("■ ②b 综合分的二次压缩：S = 2·tanh(加权分/2)")
    pairs = [(w, f) for w, f in zip(weighted_list, final_list) if abs(w) > 1e-9]
    trims = [1.0 - abs(f) / abs(w) for w, f in pairs]
    absw = [abs(w) for w, _ in pairs]
    over = sum(1 for w in absw if w >= SCORE_CAP)
    add(f"  n={len(trims)} · 加权分 |max|={max(absw):.3f} · 超过 ±2 的窗口 "
        f"{over} 个（{pct(over / max(1, len(absw)))}）")
    add(f"  二次压缩削幅：均值 {pct(sum(trims) / max(1, len(trims)))} · "
        f"p95 {pct(quantile(trims, 0.95))} · 最大 {pct(max(trims) if trims else 0)}")

    add("")
    add("■ ③ FLOW 双重压缩：先 cap=1.5 再 cap=2（两层 tanh 叠加）")
    rows, flow_max = flow_rows()
    add(f"  {'flow_z':>7} {'一层后':>8} {'显示值':>8} {'对照(只压一次)':>14}")
    for z, inner, final, single in rows:
        add(f"  {z:>7.2f} {inner:>8.3f} {final:>8.3f} {single:>14.3f}")
    add(f"  → flow_z→∞ 时显示值极限 = 2·tanh(1.5/2) = {flow_max:.3f}，")
    add(f"    比其他因子的饱和位 ±2 低 {pct(1 - flow_max / FACTOR_CAP)}；")
    add("    即资金流因子几乎不可能顶到权重上限（设计上可视为对 0.15 权重的再保护）。")

    add("")
    add("■ ④ VOL 量能因子：代码已预夹紧 |raw|≤1.8（min(|vz|,3)×0.6），第二层 tanh 继续削")
    add(f"  {'vz':>6} {'预夹后raw':>10} {'显示值':>8} {'再削':>7}")
    for vz, raw, final, trim in vol_rows():
        add(f"  {vz:>6.1f} {raw:>10.3f} {final:>8.3f} {pct(trim):>7}")
    add("  → 放量 z≥3（含以上）时，显示值只能到约 1.43，是五因子里最“吃亏”的。")

    add("")
    add("=" * 78)
    add("结论")
    add("=" * 78)
    mom, trd, rev, vol = stats_all["mom"], stats_all["trd"], stats_all["rev"], stats_all["vol"]
    add(f"1. 压缩真正“下重手”的是 TRD：|raw|≥±2 的窗口 TRD {pct(trd['share_ge2'])} / "
        f"REV {pct(rev['share_ge2'])} / MOM {pct(mom['share_ge2'])} / VOL {pct(vol['share_ge2'])}，"
        f"平均削幅 TRD {pct(trd['trim_mean'])} / REV {pct(rev['trim_mean'])} / "
        f"MOM {pct(mom['trim_mean'])} / VOL {pct(vol['trim_mean'])}。")
    add(f"2. TRD 原始值 p95 达 {trd['abs_p95']:.1f}（强趋势窗口 t 值可到 ±20+），"
        f"压缩到显示值后被压回 ≈±2——这正是「避免趋势因子绑架综合分」的预期行为；")
    add("   代价是趋势强弱在页面上会被“拉平”（4 和 14 显示得差不多）。")
    add(f"3. VOL 被“预夹紧(|vz|≤3×0.6) + 二次 tanh”双重限制，实际可达上限 ≈{cap_tanh(1.8):.2f} 而非 2.0。")
    add(f"4. FLOW 被“cap1.5 + cap2”双重限制，实际可达上限 ≈{flow_max:.2f} 而非 2.0。")
    add("5. 综合分二次压缩影响很小（回放中加权分从未越过 ±2），作用是兜底防越界。")
    add("6. 单因子最多占综合分权重 30%（MOM），且两次压缩都保留单调性——")
    add("   「单因子绑架结论」在数学上被封顶：极端原始值 ±10 → 显示 ±2.00 →")
    add(f"   对综合分贡献上限 = 0.30×2 = 0.60，仅为综合分饱和位 ±{SCORE_CAP:.0f} 的 "
        f"{pct(0.30 * FACTOR_CAP / SCORE_CAP)}。")
    return lines


def write_md(lines, path):
    body = "\n".join(lines)
    md = (
        "# 🔬 因子软压缩（tanh cap）数据核查\n\n"
        "> 生成方式：`python3 output/probe_factor_cap.py --md`（只读诊断，不改生产逻辑）。\n"
        "> 滚动回放使用与 `tests/test_quant.py` 同源的 GBM 合成日线（沙箱无外网，"
        "确定性曲线部分为精确数学）。\n\n"
        "```text\n" + body + "\n```\n"
    )
    path.write_text(md, encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="因子软压缩数据核查（只读）")
    ap.add_argument("--md", action="store_true", help="写出 根目录/因子软压缩-数据核查.md")
    args = ap.parse_args()
    lines = build_lines()
    print("\n".join(lines))
    if args.md:
        out = write_md(lines, REPO_ROOT / "因子软压缩-数据核查.md")
        print(f"\n✅ 已写出 {out}")


if __name__ == "__main__":
    main()

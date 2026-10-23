#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""◈ Jev 接入层离线回归测试（不需要模型权重、不需要联网、不需要 Key）。

真实开源权重（laya / kev / NanoJev）要从 Hugging Face 拉 1~2 GB，测试环境不该依赖它。
本套用 ``tools/jev_mock_server.py``（标准库实现、同一套协议与返回结构）把**接入层**验证完整：
  配置解析 → 闭合输入自检 → 协议调用 → 结构校验 → 概率对账（收敛 / 夹边 / 回退）
  → 缺席降级 → 留痕与结算。

2026-10-03 起新增 ``TestRunSevenDayEngine``：把 Jev 钉进 ``hk_seven_day.run_seven_day``
的三引擎优先级（大模型 > Jev > 量化基准），同样只用回声服务，不发任何真实网络请求。

真实模型与回声服务的差别只有一个：概率的预测力。协议、护栏、留痕这三层与权重无关，
所以这几层可以在这里被完全钉死；权重那一层由 GitHub Actions 上的真实模型探针负责
（见 .github/workflows/jev-integration-probe.yml）。
"""
import importlib.util
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
OUTPUT_DIR = REPO_ROOT / "output"
TOOLS_DIR = REPO_ROOT / "tools"
for _p in (str(OUTPUT_DIR), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hk_seven_day as hk7                                  # noqa: E402
import jev_bridge as jb                                     # noqa: E402
import jev_mock_server as mock                              # noqa: E402


def start_mock(scenario="", api_key="", latency=0.0):
    """起一个回声服务；返回 (base_url, srv)。端口由系统分配，避免并行测试撞车。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler(scenario, api_key, latency))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def weekday_dates(n, start=date(2025, 1, 6)):
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def synthetic_bars(n=320, seed=42, start=date(2025, 1, 6)):
    """与 tests/test_hk_seven_day.py 同一口径的合成日线（纯标准库，可离线跑）。"""
    rng = random.Random(seed)
    closes, x = [], 24500.0
    for _ in range(n):
        x *= 1 + rng.gauss(0.0002, 0.011)
        closes.append(x)
    dates = weekday_dates(n, start=start)
    return [{"date": dates[i], "open": c, "high": c * 1.012, "low": c * 0.988,
             "close": c, "volume": 1.2e8} for i, c in enumerate(closes)]


def bars_by_symbol(n=320, seed=42):
    return {"^HSI": synthetic_bars(n, seed=seed),
            "^HSTECH": synthetic_bars(n, seed=seed + 1),
            "^HSCE": synthetic_bars(n, seed=seed + 2)}


def quant_config(**over):
    """hk_seven_day.run_seven_day 的 config 形参（大模型配置；测试里默认未启用）。"""
    cfg = {"enabled": False, "key": "", "base": "https://example.invalid/v1",
           "model": "test-model", "timeout": 10, "fallback": "auto"}
    cfg.update(over)
    return cfg


def demo_ctx(**over):
    ctx = dict(jb._DEMO_CONTEXT)
    ctx["quant"] = dict(ctx["quant"])
    ctx.update(over)
    return ctx


class TestConfig(unittest.TestCase):
    def test_missing_endpoint_disables(self):
        cfg = jb.jev_config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual("auto", cfg["mode"])
        ok, note = jb.probe(cfg)
        self.assertFalse(ok)
        self.assertIn("未配置", note)

    def test_base_url_aliases_and_suffix(self):
        cfg = jb.jev_config({"TYPESAFE_BASE_URL": "http://127.0.0.1:8009/"})
        self.assertTrue(cfg["enabled"])
        self.assertEqual("http://127.0.0.1:8009", cfg["base"])
        cfg2 = jb.jev_config({"OCTOPUS_JEV_BASE_URL": "http://host:9000/v1/systemone"})
        self.assertEqual("http://host:9000", cfg2["base"])

    def test_key_and_timeout_and_mode(self):
        cfg = jb.jev_config({"OCTOPUS_JEV_BASE_URL": "http://h", "TYPESAFE_API_KEY": "k",
                             "OCTOPUS_JEV_TIMEOUT": "999", "OCTOPUS_JEV_MODE": "1"})
        self.assertEqual("k", cfg["key"])
        self.assertEqual(180, cfg["timeout"])          # 上限夹紧
        self.assertEqual("always", cfg["mode"])
        self.assertEqual("never", jb.jev_config({"OCTOPUS_JEV_MODE": "0"})["mode"])
        self.assertEqual(jb.DEFAULT_TIMEOUT,
                         jb.jev_config({"OCTOPUS_JEV_TIMEOUT": "abc"})["timeout"])


class TestClosedSnapshot(unittest.TestCase):
    def test_whitelist_drops_future_fields(self):
        state = jb.build_snapshot(demo_ctx())
        self.assertEqual("^HSI", state["symbol"])
        self.assertIn("rsi14", state)
        for field in jb.FORBIDDEN_FIELDS:
            self.assertNotIn(field, state, f"{field} 不该进闭合输入")
        self.assertNotIn("quant", state)               # 嵌套结构不进 state
        ok, note = jb.assert_closed_snapshot(state, "2026-10-02", "2026-10-14")
        self.assertTrue(ok, note)

    def test_rejects_forbidden_and_nonscalar(self):
        ok, note = jb.assert_closed_snapshot({"symbol": "^HSI", "hit": True})
        self.assertFalse(ok)
        self.assertIn("hit", note)
        ok2, note2 = jb.assert_closed_snapshot({"symbol": {"a": 1}})
        self.assertFalse(ok2)
        self.assertIn("不是标量", note2)

    def test_rejects_target_not_after_base(self):
        ok, note = jb.assert_closed_snapshot({"symbol": "^HSI"}, "2026-10-14", "2026-10-02")
        self.assertFalse(ok)
        self.assertIn("必须严格晚于", note)


class TestProtocolHappyPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.srv = start_mock()
        cls.cfg = jb.jev_config({"OCTOPUS_JEV_BASE_URL": cls.base})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_probe_ready(self):
        ok, note = jb.probe(self.cfg)
        self.assertTrue(ok, note)
        self.assertIn("mock-jev-echo", note)

    def test_system_one_and_validate(self):
        ctx = demo_ctx()
        answers, err = jb.system_one(self.cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
        self.assertEqual("", err)
        self.assertIn("_meta", answers)
        norm, problems = jb.validate_answers(answers, jb.build_questions(ctx))
        self.assertEqual([], problems)
        self.assertEqual({"choice", "score", "noul"},
                         {v["type"] for v in norm.values()})
        self.assertAlmostEqual(1.0, sum(norm["dir"]["probabilities"].values()), places=6)
        self.assertTrue(0.0 <= norm["p_up"]["noul"] <= 1.0)
        self.assertTrue(0 <= norm["risk"]["score"] <= len(jb.RISK_LEVELS) - 1)

    def test_deterministic_same_input_same_output(self):
        ctx = demo_ctx()
        q = jb.build_questions(ctx)
        state = jb.build_snapshot(ctx)
        a1, _ = jb.system_one(self.cfg, state, q)
        a2, _ = jb.system_one(self.cfg, state, q)
        self.assertEqual(a1["dir"]["probabilities"], a2["dir"]["probabilities"])
        self.assertEqual(a1["p_up"]["noul"], a2["p_up"]["noul"])

    def test_merge_uses_noul_then_check_bounds(self):
        ctx = demo_ctx()
        answers, _ = jb.system_one(self.cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
        norm, _ = jb.validate_answers(answers, jb.build_questions(ctx))
        merged = jb.merge_jev(norm, {"quant_p_up": 0.5, "name": "恒指"})
        self.assertIn(merged["source"], ("noul", "choice"))
        self.assertGreaterEqual(merged["p_up"], hk7.PROB_FLOOR)
        self.assertLessEqual(merged["p_up"], hk7.PROB_CAP)
        self.assertIn(merged["direction"], ("up", "down"))
        self.assertEqual(merged["direction"], "up" if merged["p_up"] >= 0.5 else "down")

    def test_baseline_imported_from_shared_module(self):
        """收敛阈值必须与 AI 七日港股同一份常量，不能各写一套。"""
        self.assertEqual(hk7.MAX_PROB_DEVIATION, jb.MAX_PROB_DEVIATION)
        self.assertEqual(hk7.PROB_FLOOR, jb.PROB_FLOOR)


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        self.base, self.srv = start_mock(scenario="extreme")
        self.cfg = jb.jev_config({"OCTOPUS_JEV_BASE_URL": self.base})

    def tearDown(self):
        self.srv.shutdown()

    def test_extreme_probability_converges(self):
        ctx = demo_ctx()
        answers, err = jb.system_one(self.cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
        self.assertEqual("", err)
        raw = answers["p_up"]["noul"]
        self.assertGreater(raw, 0.95)                   # 模型给了极端概率
        norm, problems = jb.validate_answers(answers, jb.build_questions(ctx))
        self.assertEqual([], problems)
        merged = jb.merge_jev(norm, {"quant_p_up": 0.50, "name": "恒指"})
        self.assertTrue(merged["converged"])
        self.assertAlmostEqual(0.70, merged["p_up"], places=6)      # 0.50 + 0.20
        self.assertLess(merged["p_up"], raw)                        # 收敛方向正确
        self.assertLessEqual(merged["p_up"], hk7.PROB_CAP)
        self.assertIn("收敛", merged["note"])

    def test_extreme_never_reaches_certainty(self):
        ctx = demo_ctx()
        answers, _ = jb.system_one(self.cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
        norm, _ = jb.validate_answers(answers, jb.build_questions(ctx))
        merged = jb.merge_jev(norm, {"quant_p_up": 0.9, "name": "恒指"})
        self.assertLess(merged["p_up"], 1.0)
        self.assertLessEqual(merged["p_up"], hk7.PROB_CAP)


class TestValidationRejects(unittest.TestCase):
    def test_malformed_answers_are_rejected(self):
        base, srv = start_mock(scenario="malformed")
        try:
            cfg = jb.jev_config({"OCTOPUS_JEV_BASE_URL": base})
            ctx = demo_ctx()
            answers, err = jb.system_one(cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
            self.assertEqual("", err)                   # 传输成功
            norm, problems = jb.validate_answers(answers, jb.build_questions(ctx))
            self.assertEqual({}, norm)                  # 但结构全不合法 → 一个都不采信
            self.assertEqual(3, len(problems))
            merged = jb.merge_jev(norm, {"quant_p_up": 0.42, "name": "恒指"})
            self.assertEqual("quant", merged["source"])
            self.assertAlmostEqual(0.42, merged["p_up"], places=6)
        finally:
            srv.shutdown()

    def test_missing_question_answer(self):
        norm, problems = jb.validate_answers({"dir": {"type": "choice", "choice": "up",
                                                      "probabilities": {"up": 0.5, "flat": 0.3,
                                                                        "down": 0.2}}},
                                             jb.build_questions(demo_ctx()))
        self.assertTrue(any("缺少答案" in p for p in problems))

    def test_choice_inconsistent_with_probabilities(self):
        q = {"dir": jb.build_questions(demo_ctx())["dir"]}
        bad = {"dir": {"type": "choice", "choice": "up",
                       "probabilities": {"up": 0.1, "flat": 0.1, "down": 0.8}}}
        norm, problems = jb.validate_answers(bad, q)
        self.assertEqual({}, norm)
        self.assertTrue(any("不一致" in p for p in problems))

    def test_probability_sum_out_of_tolerance(self):
        q = {"dir": jb.build_questions(demo_ctx())["dir"]}
        bad = {"dir": {"type": "choice", "choice": "up",
                       "probabilities": {"up": 0.6, "flat": 0.6, "down": 0.6}}}
        norm, problems = jb.validate_answers(bad, q)
        self.assertEqual({}, norm)
        self.assertTrue(any("概率和" in p for p in problems))

    def test_choice_fallback_when_noul_absent(self):
        q = jb.build_questions(demo_ctx())
        only_choice = {"dir": {"type": "choice", "choice": "up",
                               "probabilities": {"up": 0.6, "flat": 0.2, "down": 0.2}}}
        norm, problems = jb.validate_answers(only_choice, q)
        self.assertTrue(any("缺少答案" in p for p in problems))     # 另两题如实报缺
        merged = jb.merge_jev(norm, {"quant_p_up": 0.5, "name": "恒指"})
        self.assertEqual("choice", merged["source"])
        self.assertAlmostEqual(0.7, merged["raw_p_up"], places=6)   # 0.6 + 0.5*0.2


class TestCrossQuestionCoherence(unittest.TestCase):
    """各题独立作答（协议如此）→ 只做提示位，不做硬校验，且以命题题为准。"""

    def _norm(self, p_noul, p_up, p_flat, p_down):
        return {"p_up": {"type": "noul", "noul": p_noul, "confidence": 0.7},
                "dir": {"type": "choice", "choice": "up", "confidence": 0.6,
                        "probabilities": {"up": p_up, "flat": p_flat, "down": p_down}}}

    def test_consistent_answers_flagged_ok(self):
        merged = jb.merge_jev(self._norm(0.78, 0.7, 0.2, 0.1),
                              {"quant_p_up": 0.75, "name": "恒指"})
        self.assertIsNotNone(merged["coherence"])
        self.assertTrue(merged["coherence"]["consistent"])
        self.assertEqual("noul", merged["source"])
        self.assertNotIn("独立作答", merged["note"])

    def test_divergent_answers_flagged_but_noul_wins(self):
        merged = jb.merge_jev(self._norm(0.20, 0.6, 0.2, 0.2),
                              {"quant_p_up": 0.20, "name": "恒指"})
        self.assertFalse(merged["coherence"]["consistent"])
        self.assertAlmostEqual(0.70, merged["coherence"]["p_choice"], places=6)
        self.assertIn("独立作答", merged["note"])
        self.assertEqual("noul", merged["source"])           # 以命题题为准
        self.assertAlmostEqual(0.20, merged["raw_p_up"], places=6)

    def test_no_coherence_note_without_choice(self):
        merged = jb.merge_jev({"p_up": {"type": "noul", "noul": 0.6, "confidence": 0.6}},
                              {"quant_p_up": 0.5, "name": "恒指"})
        self.assertIsNone(merged["coherence"])


class TestFailureModes(unittest.TestCase):
    def _cfg(self, base, **env):
        return jb.jev_config({"OCTOPUS_JEV_BASE_URL": base, **env})

    def test_http_500_degrades_with_reason(self):
        base, srv = start_mock(scenario="boom")
        try:
            ctx = demo_ctx()
            answers, err = jb.system_one(self._cfg(base), jb.build_snapshot(ctx),
                                         jb.build_questions(ctx))
            self.assertIsNone(answers)
            self.assertIn("HTTP 500", err)
            ok, note = jb.probe(self._cfg(base))
            self.assertTrue(ok)                          # 服务活着，只是这一跳失败
            self.assertTrue(note)
        finally:
            srv.shutdown()

    def test_timeout_degrades(self):
        base, srv = start_mock(latency=2.0)
        try:
            cfg = self._cfg(base, OCTOPUS_JEV_TIMEOUT="3")
            cfg["timeout"] = 1                            # 模拟一次很急的调用
            ctx = demo_ctx()
            answers, err = jb.system_one(cfg, jb.build_snapshot(ctx), jb.build_questions(ctx))
            self.assertIsNone(answers)
            self.assertTrue(err)
        finally:
            srv.shutdown()

    def test_auth_required(self):
        base, srv = start_mock(api_key="s3cret")
        try:
            ctx = demo_ctx()
            state, q = jb.build_snapshot(ctx), jb.build_questions(ctx)
            answers, err = jb.system_one(self._cfg(base), state, q)
            self.assertIsNone(answers)
            self.assertIn("401", err)
            answers2, err2 = jb.system_one(self._cfg(base, OCTOPUS_JEV_API_KEY="s3cret"),
                                           state, q)
            self.assertEqual("", err2)
            self.assertIn("p_up", answers2)
        finally:
            srv.shutdown()

    def test_dead_endpoint_absent_not_crash(self):
        """端点连不上 → 探活失败 + 逐标的记因 + 不抛异常（上层照常走量化基准）。"""
        cfg = self._cfg("http://127.0.0.1:1")             # 必然连不上
        result = jb.run(config=cfg, contexts=[demo_ctx()],
                        baseline_map={"^HSI": {"quant_p_up": 0.5}})
        self.assertFalse(result["ready"])
        self.assertTrue(result["problems"])
        self.assertIsNone(result["targets"][0]["merged"])
        self.assertTrue(result["targets"][0]["error"])
        merged_fallback = jb.merge_jev({}, {"quant_p_up": 0.5, "name": "恒指"})
        self.assertEqual("quant", merged_fallback["source"])   # 缺席即回退量化基准

    def test_always_mode_hard_fails_when_endpoint_dead(self):
        """OCTOPUS_JEV_MODE=always：端点不可用就整栏缺席，不静默回退。"""
        cfg = self._cfg("http://127.0.0.1:1", OCTOPUS_JEV_MODE="always")
        result = jb.run(config=cfg, contexts=[demo_ctx()],
                        baseline_map={"^HSI": {"quant_p_up": 0.5}})
        self.assertEqual([], result["targets"])
        self.assertTrue(any("always" in p for p in result["problems"]))


class TestRunAndJournal(unittest.TestCase):
    def setUp(self):
        self.base, self.srv = start_mock()
        self.cfg = jb.jev_config({"OCTOPUS_JEV_BASE_URL": self.base})
        self.tmp = tempfile.mkdtemp(prefix="jev-journal-")
        self.journal = os.path.join(self.tmp, jb.JOURNAL_FILENAME)

    def tearDown(self):
        self.srv.shutdown()

    def test_absent_without_endpoint(self):
        result = jb.run(config=jb.jev_config({}), contexts=[demo_ctx()])
        self.assertFalse(result["enabled"])
        self.assertTrue(any("缺席" in p for p in result["problems"]))

    def test_full_run_issues_and_persists(self):
        targets = [demo_ctx(),
                   demo_ctx(symbol="^HSTECH", name="恒生科技", short="恒科",
                            close=5320.0, quant={"p_up": 0.61})]
        result = jb.run(config=self.cfg, contexts=targets,
                        baseline_map={"^HSI": {"quant_p_up": 0.56, "name": "恒指"},
                                      "^HSTECH": {"quant_p_up": 0.61, "name": "恒科"}},
                        journal_path=self.journal)
        self.assertTrue(result["ready"], result["ready_note"])
        self.assertEqual(2, len(result["targets"]))
        for item in result["targets"]:
            self.assertIsNotNone(item["merged"])
            self.assertTrue(item["issued"])
            self.assertTrue(item["merged"]["p_up"] >= hk7.PROB_FLOOR)
        data = json.loads(Path(self.journal).read_text(encoding="utf-8"))
        self.assertEqual(2, len(data["entries"]))
        self.assertTrue(all(not e["settled"] for e in data["entries"]))
        self.assertTrue(all(e["engine"].startswith("jev") for e in data["entries"]))
        self.assertIsNotNone(result["stats"])
        self.assertEqual(2, result["stats"]["standing"])

    def test_no_duplicate_issue_same_anchor(self):
        jb.run(config=self.cfg, contexts=[demo_ctx()],
               baseline_map={"^HSI": {"quant_p_up": 0.5}}, journal_path=self.journal)
        jb.run(config=self.cfg, contexts=[demo_ctx()],
               baseline_map={"^HSI": {"quant_p_up": 0.5}}, journal_path=self.journal)
        data = json.loads(Path(self.journal).read_text(encoding="utf-8"))
        self.assertEqual(1, len(data["entries"]))       # 同标的同基准日只签发一次

    def test_unclosed_input_is_refused(self):
        """目标日不晚于基准日 → 拒绝调用（防未来函数），并如实记因。"""
        bad = demo_ctx(asof="2026-10-14", target_date="2026-10-02")
        result = jb.run(config=self.cfg, contexts=[bad],
                        baseline_map={"^HSI": {"quant_p_up": 0.5}},
                        journal_path=self.journal)
        self.assertTrue(any("闭合自检" in p for p in result["problems"]))
        self.assertIsNone(result["targets"][0]["merged"])
        self.assertTrue(result["targets"][0]["error"])
        self.assertFalse(Path(self.journal).exists())   # 不合格的输入不留痕


class TestJournalSettlement(unittest.TestCase):
    """留痕结算：真值回填、Brier、样本量口径（<10 只报样本量）。"""

    def _series(self, base_date="2026-10-02", n_after=10, ret=0.02):
        dates = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
        closes = [100.0] * 5
        for i in range(n_after):                        # 之后 10 个交易日稳定走高
            dates.append(f"2026-10-{3 + i:02d}")
            closes.append(closes[-1] * (1 + ret))
        return dates, closes

    def test_settle_fills_truth_and_brier(self):
        entries = [{"symbol": "^HSI", "base_date": "2026-10-02", "base_close": 100.0,
                    "target_date": "2026-10-13", "p_up": 0.8, "quant_p_up": 0.5,
                    "settled": False, "engine": "jev · noul"}]
        series = {"^HSI": self._series()}
        hk7._settle(entries, series, now=hk7.datetime(2026, 10, 20, tzinfo=jb.CST))
        entry = entries[0]
        self.assertTrue(entry["settled"])
        self.assertEqual("up", entry["actual_dir"])
        self.assertTrue(entry["hit"])
        self.assertAlmostEqual((0.8 - 1.0) ** 2, entry["brier"], places=9)
        self.assertGreater(entry["actual_ret"], 0)

    def test_stats_only_report_sample_size_until_10(self):
        entries = [{"symbol": "^HSI", "base_date": "2026-10-02", "p_up": 0.8,
                    "settled": True, "hit": True, "brier": 0.04, "actual_ret": 0.01}]
        stats = hk7._journal_stats(entries)
        self.assertEqual(1, stats["n"])
        self.assertIsNone(stats["hit_rate"])            # 样本 <10 → 不下命中率结论
        self.assertIn("只报样本量", stats["note"])
        self.assertEqual(hk7.MIN_JOURNAL_FOR_HITRATE, jb.MIN_JOURNAL_FOR_HITRATE)

    def test_settle_waits_for_enough_bars(self):
        entries = [{"symbol": "^HSI", "base_date": "2026-10-02", "base_close": 100.0,
                    "p_up": 0.6, "settled": False}]
        series = {"^HSI": (["2026-10-02"], [100.0])}     # 只有锚定日那根 K 线
        hk7._settle(entries, series, now=hk7.datetime(2026, 10, 5, tzinfo=jb.CST))
        self.assertFalse(entries[0]["settled"])          # 没到龄就不结算


class TestCli(unittest.TestCase):
    def test_cli_check_without_endpoint_returns_1(self):
        env_backup = os.environ.copy()
        for name in jb.ENV_BASE_NAMES:
            os.environ.pop(name, None)
        try:
            self.assertEqual(1, jb.main(["--check"]))
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_cli_demo_json_against_mock(self):
        base, srv = start_mock()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                journal = os.path.join(tmp, jb.JOURNAL_FILENAME)
                code = jb.main(["--demo", "--base", base, "--json", "--journal", journal])
                self.assertEqual(0, code)
                data = json.loads(Path(journal).read_text(encoding="utf-8"))
                self.assertEqual(1, len(data["entries"]))
                self.assertFalse(any(f in data["entries"][0] for f in ("actual_ret", "hit")))
        finally:
            srv.shutdown()


# ======================================================================
# Jev 接进 run_seven_day：三引擎优先级 大模型 > Jev > 量化基准（2026-10-03 新增）
# 全部离线：Jev 用回声服务，大模型用注入的假 post_json，不发任何真实网络请求。
# ======================================================================
class TestRunSevenDayEngine(unittest.TestCase):
    def _cfg(self, base):
        return jb.jev_config({"OCTOPUS_JEV_BASE_URL": base})

    def _run(self, tmp, cfg=None, config=None, post_json=None, bars=None):
        return hk7.run_seven_day(
            None, bars_by_symbol=bars or bars_by_symbol(),
            history_path=os.path.join(tmp, hk7.JOURNAL_FILENAME),
            post_json=post_json, config=config or quant_config(),
            jev_config=cfg if cfg is not None else jb.jev_config({}))

    def test_no_llm_no_jev_keeps_quant_baseline(self):
        """既无大模型 Key 也无 Jev 端点 → engine=quant，行为与引入前逐位一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, cfg=jb.jev_config({}))
        self.assertTrue(res["available"])
        self.assertEqual("quant", res["engine"])
        self.assertFalse(res["jev"]["enabled"])
        self.assertFalse(res["jev"]["tried"])

    def test_jev_used_when_no_llm_key(self):
        """无大模型 Key + 配了 Jev 端点 → engine=jev，概率经对账、夹边后落在合法区间。"""
        base, srv = start_mock()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = self._run(tmp, cfg=self._cfg(base))
        finally:
            srv.shutdown()
        self.assertTrue(res["available"])
        self.assertEqual("jev", res["engine"])
        self.assertTrue(res["jev"]["enabled"])
        self.assertTrue(res["jev"]["tried"])
        self.assertTrue(res["jev"]["used"])
        self.assertIn("Jev", res["engine_label"])
        for t in res["targets"]:
            self.assertGreaterEqual(t["p_up"], hk7.PROB_FLOOR)
            self.assertLessEqual(t["p_up"], hk7.PROB_CAP)
            self.assertIsNotNone(t.get("quant_p_up"))

    def test_jev_writes_both_journals(self):
        """本栏留痕记 engine=jev；Jev 研究留痕单独一份 jev_forecast.json。"""
        base, srv = start_mock()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hk7j = os.path.join(tmp, hk7.JOURNAL_FILENAME)
                self._run(tmp, cfg=self._cfg(base))
                hk7data = json.loads(Path(hk7j).read_text(encoding="utf-8"))
                self.assertTrue(any(e.get("engine") == "jev" for e in hk7data["entries"]))
                jevj = os.path.join(tmp, jb.JOURNAL_FILENAME)
                self.assertTrue(Path(jevj).exists())
                jevdata = json.loads(Path(jevj).read_text(encoding="utf-8"))
                self.assertTrue(any(e.get("engine", "").startswith("jev")
                                    for e in jevdata["entries"]))
        finally:
            srv.shutdown()

    def test_jev_no_duplicate_issue_same_anchor(self):
        base, srv = start_mock()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self._cfg(base)
                for _ in range(2):                    # 同一锚定日重复运行
                    self._run(tmp, cfg=cfg)
                jevj = os.path.join(tmp, jb.JOURNAL_FILENAME)
                jevdata = json.loads(Path(jevj).read_text(encoding="utf-8"))
                self.assertEqual(3, len(jevdata["entries"]))  # 3 标的 × 1 锚定日
        finally:
            srv.shutdown()

    def test_llm_takes_priority_over_jev(self):
        """大模型可用时优先用大模型，Jev 端点即使配了也不被调用。"""
        base, srv = start_mock()
        urls = []

        def fake_post(url, body, headers, timeout):
            urls.append(url)
            self.assertIn("chat/completions", url)   # 绝不该出现 /v1/systemone
            return {"choices": [{"message": {"content": json.dumps({
                "targets": [{"code": s, "p_up": 0.55, "summary": "中性震荡",
                              "drivers": ["动能延续"], "risks": ["波动放大"],
                              "support": 24000, "resistance": 25000}
                             for s in ("^HSI", "^HSTECH", "^HSCE")],
                "cross_note": ""}, ensure_ascii=False)}}]}

        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = self._run(tmp, cfg=self._cfg(base),
                                config=quant_config(enabled=True, key="k"),
                                post_json=fake_post)
        finally:
            srv.shutdown()
        self.assertEqual("llm", res["engine"])
        self.assertFalse(res["jev"]["tried"])
        self.assertTrue(all("systemone" not in u for u in urls))

    def test_jev_endpoint_down_falls_back_to_quant(self):
        """Jev 端点连不上 → engine=quant 兜底，原因如实记录，不抛异常。"""
        cfg = self._cfg("http://127.0.0.1:1")        # 必然连不上
        with tempfile.TemporaryDirectory() as tmp:
            res = self._run(tmp, cfg=cfg)
        self.assertTrue(res["available"])
        self.assertEqual("quant", res["engine"])
        self.assertTrue(res["jev"]["enabled"])
        self.assertTrue(res["jev"]["tried"])
        self.assertFalse(res["jev"]["used"])
        self.assertTrue(res["jev"]["reason"])

    def test_jev_extreme_probability_converges_in_run(self):
        """极端概率在 run 级被收敛：最终 p_up 必须落在量化基准 ±MAX_PROB_DEVIATION 内。"""
        base, srv = start_mock(scenario="extreme")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                res = self._run(tmp, cfg=self._cfg(base))
        finally:
            srv.shutdown()
        self.assertEqual("jev", res["engine"])
        for t in res["targets"]:
            self.assertLessEqual(abs(t["p_up"] - t["quant_p_up"]),
                                 hk7.MAX_PROB_DEVIATION + 1e-9)
            self.assertGreaterEqual(t["p_up"], hk7.PROB_FLOOR)
            self.assertLessEqual(t["p_up"], hk7.PROB_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)

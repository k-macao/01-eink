"""🦐 活鲜词库（output/octopus_lexicon.py）离线回归测试（不发任何网络请求）。

守住与整仓一致的「防自欺」口径：
  · 词条完整——用户给定的 50 个活鲜 / 大排档词一字不落，每条都带 说明 / 市场映射 /
    触发条件 / 点缀短语 / 分析；
  · 不伪造数字——任何点缀短语、活鲜度标签、比喻、研判点缀里都**不含数字**
    （行情数字只来自正文实参，词库只负责「怎么说」）；
  · 条件驱动——活鲜度等级严格由数据新鲜度决定（活鲜只给当天、断气只给不可用），
    方向认不出就不点缀方向（绝不瞎猜）；
  · 确定性——同一份输入 + 同一种子永远得到同一句点缀（可复现，隔天换花样靠种子）；
  · 接入不越界——AI帮你提鲜只在栏目有数据时点缀，缺数据整行仍缺席；⌁AI研判点缀后
    多空概率数字与「⌁ AI 研判」标记一字不动。
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

sys.modules.setdefault("requests", types.SimpleNamespace())

REPO_ROOT = Path(__file__).parents[1]
OUTPUT_DIR = REPO_ROOT / "output"
if str(OUTPUT_DIR) not in sys.path:
    sys.path.insert(0, str(OUTPUT_DIR))

import octopus_lexicon as L  # noqa: E402
import octopus_ren as ren  # noqa: E402

_DIGIT_RE = re.compile(r"[0-9０-９]")

# 用户给定的 50 个词（2026-09-30）——一字不落
USER_TERMS = [
    "活鲜", "冰鲜", "冻鲜", "海捕", "野生", "养殖", "断气", "规格", "软体", "甲壳",
    "时价", "摊位", "挑拣", "肥度", "统货", "压秤", "扣秤", "公道秤", "打氧", "打包",
    "大排档", "排档", "水缸", "冷柜点菜", "来料加工", "加工费", "称重", "斤两", "现杀",
    "死单", "白灼", "清蒸", "爆炒", "辣炒", "椒盐", "酱爆", "香煎", "碳烤", "避风塘",
    "生腌", "烟火气", "鲜甜", "弹牙", "原汁原味", "扎实", "大排档夜宵", "路边摊",
    "冰镇啤酒", "路过打卡", "吃货",
]

REQUIRED_FIELDS = ("term", "cat", "shuoming", "mapping", "trigger", "dianzhui", "fenxi")
ALL_CATS = {L.CAT_FRESH, L.CAT_TRADE, L.CAT_SPECIES, L.CAT_PROCESS,
            L.CAT_COOK, L.CAT_TEXTURE, L.CAT_SCENE}


class LexiconContentTests(unittest.TestCase):
    def test_all_fifty_user_terms_present_no_more_no_less(self):
        self.assertEqual(L.LEXICON_SIZE, 50)
        self.assertEqual(set(USER_TERMS), set(L.LEXICON))
        self.assertEqual(len(USER_TERMS), len(set(USER_TERMS)))   # 用户清单本身无重复

    def test_every_entry_has_full_metadata(self):
        for e in L.all_terms():
            for f in REQUIRED_FIELDS:
                self.assertTrue(e.get(f), f"{e.get('term')} 缺字段 {f}")
            self.assertIn(e["cat"], ALL_CATS, e["term"])
            self.assertIsInstance(e["dianzhui"], list)
            self.assertGreaterEqual(len(e["dianzhui"]), 1, e["term"])
            # 说明 / 市场映射 / 触发条件 / 分析都要有实质内容（不是占位）
            for f in ("shuoming", "mapping", "trigger", "fenxi"):
                self.assertGreaterEqual(len(str(e[f])), 4, (e["term"], f))

    def test_every_category_is_populated(self):
        for cat in ALL_CATS:
            self.assertTrue(L.by_category(cat), f"类别 {cat} 为空")

    def test_term_and_all_terms_accessors(self):
        self.assertEqual(L.term("活鲜")["cat"], L.CAT_FRESH)
        self.assertIsNone(L.term("不存在的词"))
        self.assertEqual(len(L.all_terms()), 50)


class NoFabricatedNumberTests(unittest.TestCase):
    """词库只负责「怎么说」，绝不携带任何数字（数字只来自正文实参）。"""

    def test_no_digits_in_any_dianzhui_phrase(self):
        for e in L.all_terms():
            for ph in e["dianzhui"]:
                self.assertIsNone(_DIGIT_RE.search(ph), (e["term"], ph))

    def test_no_digits_in_grade_lines_and_metaphors(self):
        for grade in (L.GRADE_LIVE, L.GRADE_ICED, L.GRADE_FROZEN, L.GRADE_DEAD):
            for ph in L._GRADE_LINE[grade]:
                self.assertIsNone(_DIGIT_RE.search(ph), ph)
        # 各种状态组合下的比喻与研判点缀都不得含数字
        for label in ("偏多", "偏空", "中性", "▲ 看涨 · P(7日涨) 62%", None):
            for vol in (None, 0.1, 0.5, 0.9):
                for stance in (None, "避险", "博弈", "观望", "进攻", "轻仓"):
                    g = L.section_garnish(available=True, is_today=True, label=label,
                                          p_pct=62, vol_pct=vol, stance=stance, seed="s")
                    self.assertIsNone(_DIGIT_RE.search(g["metaphor"]), g["metaphor"])
                    self.assertIsNone(_DIGIT_RE.search(g["badge"]), g["badge"])
                    self.assertIsNone(_DIGIT_RE.search(L.seafood_line(g)), "line")
            j = L.garnish_judgment(label, 62, "s")
            self.assertIsNone(_DIGIT_RE.search(j), j)


class FreshnessGradeTests(unittest.TestCase):
    """活鲜度严格由数据新鲜度决定——条件驱动，绝不冒充。"""

    def test_unavailable_is_dead(self):
        self.assertEqual(L.freshness_grade(available=False), L.GRADE_DEAD)
        self.assertEqual(L.freshness_grade(available=False, is_today=True), L.GRADE_DEAD)

    def test_today_is_live(self):
        self.assertEqual(L.freshness_grade(available=True, is_today=True), L.GRADE_LIVE)

    def test_lag_grades(self):
        self.assertEqual(L.freshness_grade(available=True, lag_days=0), L.GRADE_LIVE)   # 0 天滞后=当天
        self.assertEqual(L.freshness_grade(available=True, lag_days=1), L.GRADE_ICED)
        self.assertEqual(L.freshness_grade(available=True, lag_days=2), L.GRADE_ICED)
        self.assertEqual(L.freshness_grade(available=True, lag_days=3), L.GRADE_FROZEN)
        self.assertEqual(L.freshness_grade(available=True, lag_days=9), L.GRADE_FROZEN)

    def test_unknown_freshness_is_conservative_iced(self):
        # 可用但既非当天、又无滞后天数 → 冰鲜（保守，绝不冒充活鲜）
        self.assertEqual(L.freshness_grade(available=True), L.GRADE_ICED)

    def test_live_requires_today_never_overclaimed(self):
        # 只要不是当天（is_today 真 或 lag_days==0），就绝不给「活鲜」
        for kwargs in ({"available": True}, {"available": True, "is_today": False},
                       {"available": True, "lag_days": 1}, {"available": True, "lag_days": 5}):
            self.assertNotEqual(L.freshness_grade(**kwargs), L.GRADE_LIVE)


class DirectionParsingTests(unittest.TestCase):
    def test_norm_dir_parses_real_labels(self):
        self.assertEqual(L._norm_dir("偏多"), "up")
        self.assertEqual(L._norm_dir("偏空"), "down")
        self.assertEqual(L._norm_dir("中性"), "flat")
        self.assertEqual(L._norm_dir("▲ 看涨 · P(7日涨) 62%"), "up")
        self.assertEqual(L._norm_dir("▼ 看跌 · P(7日涨) 38%"), "down")
        self.assertEqual(L._norm_dir("中性（略偏涨）"), "flat")   # 档位优先于括注倾向
        self.assertEqual(L._norm_dir("up"), "up")
        self.assertEqual(L._norm_dir("neutral"), "flat")

    def test_norm_dir_returns_none_when_unrecognized(self):
        self.assertIsNone(L._norm_dir(""))
        self.assertIsNone(L._norm_dir(None))
        self.assertIsNone(L._norm_dir("今天天气不错"))

    def test_unrecognized_direction_skips_texture_clause(self):
        g = L.section_garnish(available=True, is_today=True, label="今天天气不错", seed="s")
        # 认不出方向 → 只有鲜度状态那一句，不硬安一个方向口感
        self.assertEqual(g["metaphor"].count("；"), 0)
        self.assertIn("活鲜度", g["badge"])


class DeterminismTests(unittest.TestCase):
    def test_same_seed_same_output(self):
        a = L.section_garnish(available=True, is_today=True, label="偏多",
                              p_pct=72, vol_pct=0.85, stance="避险", seed="20260930|X")
        b = L.section_garnish(available=True, is_today=True, label="偏多",
                              p_pct=72, vol_pct=0.85, stance="避险", seed="20260930|X")
        self.assertEqual(a, b)
        self.assertEqual(L.garnish_judgment("偏多", 72, "20260930|X"),
                         L.garnish_judgment("偏多", 72, "20260930|X"))

    def test_different_seed_changes_wording_not_grade(self):
        a = L.section_garnish(available=True, is_today=True, label="偏多", p_pct=72, seed="d1|X")
        b = L.section_garnish(available=True, is_today=True, label="偏多", p_pct=72, seed="d2|X")
        self.assertEqual(a["grade"], b["grade"])      # 等级由数据定，不随种子变
        self.assertEqual(a["badge"], b["badge"])

    def test_terms_used_are_real_lexicon_terms(self):
        g = L.section_garnish(available=True, is_today=True, label="偏多",
                              p_pct=72, vol_pct=0.85, stance="避险", seed="s")
        self.assertTrue(g["terms"])
        for t in g["terms"]:
            self.assertIn(t, L.LEXICON, t)


class RenIntegrationTests(unittest.TestCase):
    """AI帮你提鲜：有数据才点缀活鲜度，缺数据整行缺席（与解读本体同一防自欺口径）。"""

    def _ctx(self, weekly):
        return {"date_str": "20260930", "notes": {}, "weekly": weekly, "hk7": {}}

    def test_available_section_gets_seafood_grade(self):
        weekly = {"available": True, "is_today": True, "vol_pct": 0.8,
                  "entry": {"p_up": 0.62, "label": "▲ 看涨 · P(7日涨) 62%", "target_sessions": 7},
                  "daily": {"rows": [{"k": 1, "p_up": 0.62, "label": "▲ 看涨 62%"}]}}
        text = ren.section_ren("WEEKLY FORECAST", self._ctx(weekly))
        self.assertIn("活鲜度：活鲜", text)
        self.assertIn("🦐", text)
        self.assertIsNone(_DIGIT_RE.search(text.split("🦐", 1)[1]), "点缀段不得含数字")

    def test_not_today_section_is_iced_not_live(self):
        weekly = {"available": True, "is_today": False,
                  "entry": {"p_up": 0.55, "label": "■ 中性（略偏涨）· P(7日涨) 55%", "target_sessions": 7},
                  "daily": {"rows": [{"k": 1, "p_up": 0.55, "label": "■ 中性 55%"}]}}
        text = ren.section_ren("WEEKLY FORECAST", self._ctx(weekly))
        self.assertIn("活鲜度：冰鲜", text)
        self.assertNotIn("活鲜度：活鲜", text)

    def test_unavailable_section_has_no_row_at_all(self):
        self.assertEqual(ren.section_ren("WEEKLY FORECAST", self._ctx({"available": False})), "")
        self.assertEqual(ren.section_ren("WEEKLY FORECAST", self._ctx({})), "")

    def test_garnish_is_deterministic_through_ren(self):
        weekly = {"available": True, "is_today": True,
                  "entry": {"p_up": 0.6, "label": "▲ 看涨 · P(7日涨) 60%", "target_sessions": 7},
                  "daily": {"rows": [{"k": 1, "p_up": 0.6, "label": "▲ 看涨 60%"}]}}
        ctx = self._ctx(weekly)
        self.assertEqual(ren.section_ren("WEEKLY FORECAST", ctx),
                         ren.section_ren("WEEKLY FORECAST", ctx))


class AiJudgeRowIntegrationTests(unittest.TestCase):
    """⌁AI研判行：点缀后多空概率数字与标记一字不动。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "pipeline_for_lexicon_test", OUTPUT_DIR / "pipeline.py")
        cls.pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.pipeline)

    def test_judgment_row_keeps_numbers_and_adds_garnish(self):
        pl = self.pipeline
        note = pl._judge_note(72, "多头占优 → 预测：偏多震荡")
        row = pl._ai_judge_row(note, pl.GUIZANG_KIT, "", seed="20260930|X|X|")
        text = pl._strip_html_text(row)
        self.assertIn("⌁ AI 研判", text)
        self.assertIn("多头 72%", text)
        self.assertIn("空头 28%", text)
        self.assertIn("多头占优 → 预测：偏多震荡", text)
        self.assertRegex(text, r"（[^）]*弹牙[^）]*）|（[^）]*活鲜[^）]*）")  # 偏多强 → 活鲜/弹牙系

    def test_judgment_row_garnish_has_no_digits(self):
        pl = self.pipeline
        for prob in (72, 50, 28):
            note = pl._judge_note(prob, "判断预测")
            row = pl._strip_html_text(pl._ai_judge_row(note, pl.GUIZANG_KIT, "", seed=f"s{prob}"))
            garnish = row.split("判断预测", 1)[1]
            self.assertIsNone(_DIGIT_RE.search(garnish), garnish)

    def test_neutral_and_bear_get_distinct_garnish(self):
        pl = self.pipeline
        bull = pl._strip_html_text(pl._ai_judge_row(pl._judge_note(72, "x"), pl.GUIZANG_KIT, seed="s"))
        bear = pl._strip_html_text(pl._ai_judge_row(pl._judge_note(28, "x"), pl.GUIZANG_KIT, seed="s"))
        flat = pl._strip_html_text(pl._ai_judge_row(pl._judge_note(50, "x"), pl.GUIZANG_KIT, seed="s"))
        self.assertNotEqual(bull.split("（")[-1], bear.split("（")[-1])
        self.assertNotEqual(bull.split("（")[-1], flat.split("（")[-1])


if __name__ == "__main__":
    unittest.main()

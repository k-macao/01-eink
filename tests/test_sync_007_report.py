import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_007_report", ROOT / "tools" / "sync_007_report.py"
)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SYNC)

HKT = timezone(timedelta(hours=8))


def fixture_snapshot():
    return {
        "agg": {
            "n": 8,
            "n_sources": 4,
            "pos": 3,
            "neg": 2,
            "neu": 3,
            "mean": 0.1,
            "综合多头概率锚点": 58,
        },
        "items": [
            {"source": "A", "title": "芯片订单大增 AI算力需求回暖", "age_h": 1,
             "score": 1, "label": "利好", "conf": 0.8},
            {"source": "B", "title": "油价急升压制航空与制造成本", "age_h": 2,
             "score": -1, "label": "利空", "conf": 0.8},
            {"source": "C", "title": "锂电池排产环比增长", "age_h": 3,
             "score": 1, "label": "利好", "conf": 0.5},
            {"source": "D", "title": "美债利率上行成长股估值承压", "age_h": 4,
             "score": -1, "label": "利空", "conf": 0.7},
            {"source": "A", "title": "出口制造订单保持韧性", "age_h": 5,
             "score": 0, "label": "中性", "conf": 0},
            {"source": "B", "title": "人民币汇率双向波动", "age_h": 6,
             "score": 0, "label": "中性", "conf": 0},
        ],
        "errors": ["AI智能检索：未命中"],
        "_source_commit": "feedface12345678",
    }


class Sync007ReportTests(unittest.TestCase):
    def test_build_report_contract(self):
        report = SYNC.build_report(
            fixture_snapshot(),
            now=datetime(2026, 10, 24, 9, 10, tzinfo=HKT),
            edition="早报",
            source_commit="abcdef1234567890",
        )
        self.assertEqual(report["title"], "章鱼 AI.每日研报")
        self.assertEqual(report["source"]["repository"], "k-macao/007")
        self.assertEqual(report["source"]["commit"], "abcdef1234567890")
        self.assertEqual(report["edition"], "早报")
        self.assertEqual(report["probability"]["long"], 58)
        self.assertEqual(report["probability"]["short"], 42)
        self.assertEqual(report["probability"]["long"] + report["probability"]["short"], 100)
        self.assertGreaterEqual(len(report["topics"]), 3)
        self.assertTrue(report["risks"])
        self.assertTrue(report["opportunities"])

    def test_topics_have_sentiment_and_numeric_weight(self):
        report = SYNC.build_report(
            fixture_snapshot(), now=datetime(2026, 10, 24, 17, 10, tzinfo=HKT)
        )
        for topic in report["topics"]:
            self.assertTrue(topic["text"])
            self.assertIn(topic["sentiment"], {
                "积极乐观", "谨慎乐观", "中性观察", "谨慎悲观", "极度恐慌"
            })
            self.assertGreaterEqual(topic["weight"], 0)
            self.assertLessEqual(topic["weight"], 100)
        self.assertEqual(report["topics"][0]["theme"], "AI / 算力")
        oil = next(x for x in report["topics"] if "油价" in x["text"])
        self.assertEqual(oil["theme"], "能源 / 油价")

    def test_edition_auto_uses_macau_local_time(self):
        morning = SYNC.build_report(
            fixture_snapshot(), now=datetime(2026, 10, 24, 9, 10, tzinfo=HKT)
        )
        evening = SYNC.build_report(
            fixture_snapshot(), now=datetime(2026, 10, 24, 17, 10, tzinfo=HKT)
        )
        self.assertEqual(morning["edition"], "早报")
        self.assertEqual(evening["edition"], "晚报")

    def test_wire_duplicates_are_collapsed_and_source_count_preserved(self):
        rows = [
            {"source": "A", "title": "AI算力订单增长，产业链需求回暖", "score": 1},
            {"source": "B", "title": "AI算力订单增长，产业链需求回暖 - 快讯", "score": 1},
        ]
        deduped = SYNC._dedupe_items(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["source_count"], 2)

    def test_empty_collection_never_overwrites_last_valid_report(self):
        with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
            SYNC.build_report(
                {"agg": {"n": 0, "综合多头概率锚点": 50}, "items": [], "errors": ["all failed"]},
                now=datetime(2026, 10, 24, 9, 10, tzinfo=HKT),
            )

    def test_ai_json_validation_is_strict_and_bounded(self):
        parsed = SYNC._valid_ai_columns({
            "topics": [{"text": "AI算力订单", "sentiment": "谨慎乐观", "weight": 160}],
            "risks": ["估值波动"],
            "opportunities": ["订单回暖线索"],
        })
        self.assertIsNotNone(parsed)
        topics, risks, opportunities = parsed
        self.assertEqual(topics[0]["weight"], 100.0)
        self.assertEqual(risks, ["估值波动"])
        self.assertEqual(opportunities, ["订单回暖线索"])
        self.assertIsNone(SYNC._valid_ai_columns({"topics": [], "risks": [], "opportunities": []}))

    def test_cli_snapshot_writes_utf8_json(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snapshot.json"
            out = Path(td) / "report.json"
            snap.write_text(json.dumps(fixture_snapshot(), ensure_ascii=False), encoding="utf-8")
            code = SYNC.main([
                "--snapshot", str(snap), "--output", str(out), "--no-ai",
                "--now", "2026-10-24T09:10:00+08:00",
            ])
            self.assertEqual(code, 0)
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(saved["title"], "章鱼 AI.每日研报")
            self.assertEqual(saved["date"], "2026-10-24")
            self.assertEqual(saved["source"]["commit"], "feedface12345678")
            self.assertNotIn("\\u7ae0", out.read_text(encoding="utf-8"))


def bearish_snapshot():
    """空头日快照：与 007 微信版截图同构（指数 36、极端标题、避险与逆向信号）。"""
    def item(source, title, score, conf=0.8, age=1):
        return {"source": source, "title": title, "score": score,
                "label": "利好" if score > 0.12 else "利空" if score < -0.12 else "中性",
                "conf": conf, "age_h": age}
    return {
        "agg": {"n": 50, "n_sources": 11, "pos": 8, "neg": 30, "neu": 12,
                "mean": -0.1, "综合多头概率锚点": 36},
        "items": [
            item("A", "费半狂泻近7%、台积电ADR跌超4.5%", -1.0, 0.95),
            item("B", "恒指全周跌1093点、北水连续8日流出", -0.8, 0.9, 2),
            item("C", "一下子跌入极度恐慌，半导体还能拿吗", -0.9, 0.85, 3),
            item("D", "今日血亏6000、美股再次大跌", -0.5, 0.8, 1),
            item("E", "半导体SOXX成分股全线下挫", -0.6, 0.85, 2),
            item("F", "对冲基金连续减仓、下周注意QQQ", -0.4, 0.7, 4),
            item("G", "全球资金大逃亡、AI泡沫要破?", -0.85, 0.8, 5),
            item("H", "美债息急跌、日元急升避险升温", -0.5, 0.75, 2),
            item("I", "黄金再创新高，资金涌入避险", -0.3, 0.7, 6),
            item("J", "AI算力订单仍旺、云厂商资本开支上修", 0.7, 0.8, 3),
            item("K", "最绝望时买入将来最赚？历史复盘", 0.4, 0.6, 7),
            item("L", "下跌时如何检视资产配置与安全边际", 0.2, 0.5, 8),
            item("M", "人民币汇率双向波动", 0.0, 0.0, 9),
        ],
        "errors": [],
        "_source_commit": "abc123",
    }


class ColumnLogicTests(unittest.TestCase):
    def test_stance_label_uses_five_tiers(self):
        cases = {82: "积极乐观", 70: "积极乐观", 60: "谨慎乐观", 55: "谨慎乐观",
                 50: "中性观察", 46: "中性观察", 45: "谨慎悲观", 38: "谨慎悲观",
                 31: "谨慎悲观", 30: "极度恐慌", 12: "极度恐慌"}
        for value, expected in cases.items():
            self.assertEqual(SYNC._stance_label(value), expected, value)

    def test_bearish_day_columns_follow_007_page_logic(self):
        snap = bearish_snapshot()
        report = SYNC.build_report(
            snap, now=datetime(2026, 10, 25, 17, 5, tzinfo=HKT), edition="晚报")
        self.assertEqual(report["probability"]["long"], 36)
        self.assertEqual(report["probability"]["label"], "谨慎悲观")
        self.assertEqual(report["probability"]["note"], "基于 50 条内容 · 007本地情绪模型")

        risks, ops = report["risks"], report["opportunities"]
        # 风险/机会是统计推导的分析句，且数字全部来自快照
        self.assertIn("档占流量", risks[0])
        self.assertTrue(any("极度恐慌标题已现（4条）" in x for x in risks), risks)
        self.assertEqual(ops[0], "情绪指数距下轨25仅差11点，若触及极度恐慌可寻找左侧买点")
        # 不再把原始标题逐条回声当作风险/机会
        raw_titles = {SYNC._clip(row["title"], 56) for row in snap["items"]}
        for line in risks + ops:
            self.assertNotIn(line, raw_titles)
        self.assertGreaterEqual(len(risks), 4)
        self.assertGreaterEqual(len(ops), 4)

    def test_topic_weights_are_total_traffic_share_sorted_desc(self):
        report = SYNC.build_report(
            bearish_snapshot(), now=datetime(2026, 10, 25, 17, 5, tzinfo=HKT))
        weights = [t["weight"] for t in report["topics"]]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertLessEqual(sum(weights), 100.0)
        self.assertTrue(all(0 < w <= 100 for w in weights))
        for t in report["topics"]:
            self.assertIn(t["sentiment"], SYNC.SENTIMENT_TIERS)

    def test_session_topic_follows_007_market_brief(self):
        self.assertEqual(SYNC._session_topic("早报"), "全球市场早报")
        self.assertEqual(SYNC._session_topic("晚报"), "全球市场晚报")
        self.assertEqual(SYNC._session_topic("其它"), "全球市场每日研报")

    def test_ai_sentiment_must_be_one_of_five_tiers(self):
        parsed = SYNC._valid_ai_columns({
            "topics": [{"text": "AI算力订单", "sentiment": "非常看多", "weight": 5}],
            "risks": ["波动风险"],
            "opportunities": ["订单线索"],
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0][0]["sentiment"], "中性观察")


if __name__ == "__main__":
    unittest.main()

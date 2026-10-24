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


if __name__ == "__main__":
    unittest.main()

"""微信推送日报的栏目正文表格化与段落留白回归测试。"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

sys.modules.setdefault("requests", types.SimpleNamespace())
MODULE_PATH = Path(__file__).parents[1] / "output" / "pipeline.py"
spec = importlib.util.spec_from_file_location("pipeline_under_test_zebra", MODULE_PATH)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)
# 本文件只测版面，用 gz_note 当通用内容块；打开 --notes 语义让脚注照常生成。
pipeline.set_notes_requested(True)

ZEBRA = pipeline.GZ_ZEBRA
PAPER = pipeline.GZ_PAPER


def top_level_chunks(html):
    """把内容串拆回顶层块序列，便于断言一块一表、顺序不变。"""
    return [html[a:b] for a, b in pipeline._gz_top_level_spans(html)]


class ZebraBandTests(unittest.TestCase):
    def test_every_non_table_content_block_gets_its_own_table(self):
        content = "".join(pipeline.gz_note(f"块{i}") for i in (1, 2, 3, 4))
        banded = pipeline._gz_zebra_bands(content)
        chunks = top_level_chunks(banded)
        self.assertEqual(len(chunks), 4)
        for i, chunk in enumerate(chunks):
            self.assertTrue(chunk.startswith("<table"), chunk[:100])
            self.assertIn(f"块{i + 1}", chunk)
            expected = PAPER if i % 2 == 0 else ZEBRA
            self.assertIn(f'bgcolor="{expected}"', chunk)
        self.assertIn("padding=\"8\"", banded)
        self.assertIn("line-height:1.75", banded)

    def test_no_content_loss_and_order_preserved(self):
        content = (
            pipeline.gz_note("脚注甲")
            + "<!--BODY-->"  # 推进标记：不计块、也不许丢
            + pipeline.gz_subsection("子节标题")
            + pipeline.gz_data_table(["A", "B"], [["1", "2"]])
            + pipeline.gz_note("脚注乙")
            + "\n"
        )
        banded = pipeline._gz_zebra_bands(content)
        for token in ("脚注甲", "<!--BODY-->", "子节标题", "脚注乙", "<table", "</table>"):
            self.assertIn(token, banded)
        self.assertLess(banded.index("脚注甲"), banded.index("子节标题"))
        self.assertLess(banded.index("子节标题"), banded.index("脚注乙"))

    def test_subsection_heading_joins_following_block(self):
        content = (
            pipeline.gz_note("引子")
            + pipeline.gz_subsection("风险预算")
            + pipeline.gz_data_table(["指标", "值"], [["预算", "10%"]])
        )
        banded = pipeline._gz_zebra_bands(content)
        chunks = top_level_chunks(banded)
        self.assertEqual(len(chunks), 2)
        self.assertIn("引子", chunks[0])
        self.assertIn("风险预算", chunks[1])
        self.assertIn("预算", chunks[1])
        # 标题与对应数据处于同一版面表格中；其中保留原始数据表（内层 table）。
        self.assertEqual(chunks[1].count("<table"), 2)

    def test_existing_news_tables_stay_individual_and_are_not_nested(self):
        cards = "".join(pipeline.gz_item_row("◆", f"条目{i}") for i in range(3))
        banded = pipeline._gz_zebra_bands(cards)
        chunks = top_level_chunks(banded)
        self.assertEqual(len(chunks), 3)
        for i, chunk in enumerate(chunks):
            self.assertTrue(chunk.startswith("<table"))
            self.assertIn(f"条目{i}", chunk)
            self.assertEqual(chunk.count("<table"), 1)

    def test_single_block_plain_text_and_raw_table_are_safe(self):
        lone = pipeline.gz_note("唯一一块")
        banded_lone = pipeline._gz_zebra_bands(lone)
        self.assertTrue(banded_lone.startswith("<table"))
        self.assertIn("唯一一块", banded_lone)

        raw = pipeline._gz_zebra_bands("没有标签的正文")
        self.assertTrue(raw.startswith("<table"))
        self.assertIn("没有标签的正文", raw)

        trs = "<tr><td>甲</td></tr><tr><td>乙</td></tr>"
        section = pipeline.gz_section("01", "TEST", "标题", trs)
        body = section.split(pipeline.SECTION_BODY_MARK, 1)[1]
        self.assertIn("<table", body)
        self.assertIn("甲", body)
        self.assertIn("乙", body)
        self.assertEqual(body.count("<table"), body.count("</table>"))

    def test_banding_covers_full_report_and_stays_inline_only(self):
        data = {
            "全球头条": pipeline._source_result(
                "test news", "success",
                headlines=[{"title": f"头条{i}", "source": "测试源",
                            "published_cst": f"2026-09-30 10:0{i}"} for i in range(4)]),
        }
        for theme, expected_paper, expected_zebra in (
                ("lime", pipeline.L_STRIPE, pipeline.L_ZEBRA),
                ("guizang", PAPER, pipeline.GZ_ZEBRA),
                ("dossier", pipeline.D_PAPER, pipeline.D_ZEBRA),
                # forum 暗色主题：卡片 #2C2C2E / 卡内条纹 #232325
                ("forum", pipeline.F_CARD, pipeline.F_STRIPE)):
            with self.subTest(theme=theme):
                html = pipeline.generate_report(data, "2026年9月30日 · 周三", "20260930",
                                                theme=theme)
                self.assertIn(f'bgcolor="{expected_paper}"', html)
                self.assertIn(f'bgcolor="{expected_zebra}"', html)
                # 纯内联样式：不引入 <style> / class（微信 PushPlus 清洗安全）
                self.assertNotIn("<style", html)
                self.assertNotIn('class="', html)
                # 包装表格不破坏 div 配对。
                self.assertEqual(html.count("<div"), html.count("</div>"))

                # 刊头和栏目标题也由表格承载；可见栏目正文按块表格化。
                for heading in (m.start() for m in re.finditer(r"<h[12]\b", html)):
                    self.assertGreater(html.rfind("<table", 0, heading),
                                       html.rfind("</table>", 0, heading))
                body = html[html.find(pipeline.PART_BREAK_MARK) + len(pipeline.PART_BREAK_MARK):
                            html.find(pipeline.DOC_FOOT_MARK)]
                for section in [x for x in body.split(pipeline.PART_BREAK_MARK) if x.strip()]:
                    if pipeline.SECTION_BODY_MARK not in section:
                        continue  # 页面级 #report 锚点不属于栏目正文
                    section_body = section.split(pipeline.SECTION_BODY_MARK, 1)[1]
                    for chunk in top_level_chunks(section_body):
                        if 'id="report"' in chunk:
                            continue
                        self.assertTrue(chunk.lstrip().startswith("<table"), chunk[:100])


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pe_vc_weekly_report import IntelItem, dedupe


def item(title: str, summary: str, published: str = "2026-07-13") -> IntelItem:
    return IntelItem(
        title=title,
        url=f"https://example.com/{abs(hash(title))}",
        source="测试信源",
        published=published,
        summary=summary,
        company="德同资本",
        company_group="重点机构",
        priority="P0",
        dimension="投资组合与交易动态",
        matrix_label="新增投资与项目退出",
        channel="搜索RSS",
        credibility="中",
    )


class EventDedupeTests(unittest.TestCase):
    def test_same_drug_farm_round_merges_across_different_headlines(self) -> None:
        rows = [
            item("药物牧场获D轮投资", "药物牧场完成D轮融资，德同资本继续跟投。"),
            item("药物牧场完成5500万美元D轮融资首轮融资", "药物牧场D轮融资由多家机构参与，德同资本继续跟投。"),
        ]
        self.assertEqual(len(dedupe(rows)), 1)

    def test_lingxin_hand_variants_merge_as_one_b_plus_event(self) -> None:
        rows = [
            item("灵巧手领域再传融资消息头部企业完成B+轮融资", "灵心巧手完成B+轮融资，德同资本持续加注。", "2026-04-30"),
            item("灵心巧手完成B+轮融资", "灵心巧手完成B+轮融资，多家老股东持续加注。", "2026-04-29"),
            item("两个月融资两轮！灵心巧手创始人谈机器猫的口袋", "灵心巧手本轮获德同资本持续加注。", "2026-04-30"),
        ]
        self.assertEqual(len(dedupe(rows)), 1)

    def test_different_portfolio_companies_do_not_merge(self) -> None:
        rows = [
            item("甲公司完成D轮融资", "甲公司研发创新药，德同资本参与投资。"),
            item("乙公司完成D轮融资", "乙公司研发工业机器人，德同资本参与投资。"),
        ]
        self.assertEqual(len(dedupe(rows)), 2)

    def test_different_rounds_do_not_merge(self) -> None:
        rows = [
            item("星海科技完成B轮融资", "星海科技完成B轮融资，德同资本参与。"),
            item("星海科技完成C轮融资", "星海科技完成C轮融资，德同资本参与。"),
        ]
        self.assertEqual(len(dedupe(rows)), 2)


if __name__ == "__main__":
    unittest.main()

"""引用错位自动修复的单测。

跑法（仓库根，PYTHONPATH 含 scripts）：
    python -m unittest discover -s scripts/tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # -> scripts/

from generate.guard import repair_citations, validate  # noqa: E402
from retrieve.verify import FinalHit  # noqa: E402


def hit(i: int, name: str) -> FinalHit:
    return FinalHit(
        shop_id=f"GZ-T{i:04d}",
        shop_name=name,
        score=1.0 - i * 0.01,
        parent_text=f"【店铺】{name}｜人均 ¥20｜评分 4.5",
        payload={"price_per_person": 20, "rating": 4.5},
        matched_hard="",
        matched_soft=[],
        matched_child_ids=[],
    )


# 高相似店名场景：正是线上出问题的那组
HITS = [
    hit(1, "桥头灶·西华路肠粉"),
    hit(2, "广记灶·西华路肠粉"),
    hit(3, "桥头轩·西华路肠粉"),
    hit(4, "羊城斋·西华路肠粉"),
]


class TestRepairCitations(unittest.TestCase):
    def test_01_错位店名被替换为真实店名(self):
        ans = "为你找到 4 家：\n广记灶·西华路肠粉[5]人均 ¥20，评分 4.5。"
        # 用 4 家命中，编号 5 非法；这里直接测 repair 的替换能力需合法编号，改用 [1] 配错名
        ans = "桥头轩·西华路肠粉[1]人均 ¥20。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertIn("桥头灶·西华路肠粉[1]", fixed)
        self.assertNotIn("桥头轩·西华路肠粉[1]", fixed)
        self.assertEqual(len(repairs), 1)
        self.assertIn("→", repairs[0])

    def test_02_合法店名不被改动(self):
        ans = "桥头灶·西华路肠粉[1] 和 广记灶·西华路肠粉[2] 都不错。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertEqual(fixed, ans)
        self.assertEqual(repairs, [])

    def test_03_合法简称不误伤(self):
        # LLM 用简称"桥头灶"（真名前缀）应视为已对齐，不动
        ans = "桥头灶[1]人均 ¥20。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertEqual(fixed, ans)
        self.assertEqual(repairs, [])

    def test_04_非法编号不动(self):
        ans = "羊城斋·西华路肠粉[9]人均 ¥20。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertEqual(fixed, ans)
        self.assertEqual(repairs, [])

    def test_05_修复后复验通过_保留LLM答案(self):
        # 店名串错但正文事实都在资料里 -> 修完 guard 应放行
        ans = "为你找到 1 家符合条件的店：\n桥头轩·西华路肠粉[1] —— 人均 ¥20，评分 4.5。"
        before = validate(ans, HITS)
        self.assertFalse(before.ok)
        self.assertIn("引用错位", before.reason)

        fixed, repairs = repair_citations(ans, HITS)
        self.assertTrue(repairs)
        after = validate(fixed, HITS)
        self.assertTrue(after.ok, f"修复后仍不过：{after.reason}")

    def test_06_多处错位一次修复(self):
        ans = "桥头轩·西华路肠粉[1]、羊城斋·西华路肠粉[2]值得一试。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertIn("桥头灶·西华路肠粉[1]", fixed)
        self.assertIn("广记灶·西华路肠粉[2]", fixed)
        self.assertEqual(len(repairs), 2)

    def test_07_修完仍有幻觉数字则复验失败(self):
        # 店名修对了，但价格 ¥999 不在资料里 -> 复验必须失败，走降级
        ans = "桥头轩·西华路肠粉[1]人均 ¥999。"
        fixed, repairs = repair_citations(ans, HITS)
        self.assertTrue(repairs)
        after = validate(fixed, HITS)
        self.assertFalse(after.ok)
        self.assertIn("价格数字", after.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)

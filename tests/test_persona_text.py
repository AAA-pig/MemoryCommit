# -*- coding: utf-8 -*-
"""手写人格导入（/人格 导入）：纯文本解析 + 整批一个提交。

分两层：
  解析层（persona_text.parse_persona_text）是主体——纯函数，出错必须说清
  第几行、错在哪，不能让人自己数到眼花。
  写入层只验三件事：整批一个提交、闸没被绕过、core 分区写不进。
  其余写入口行为由 test_persona / test_persona_token 钉住，不在这里重复。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger as L  # noqa: E402
import persona_text as PT  # noqa: E402


class ParseTest(unittest.TestCase):
    # ---------- 正常解析 ----------

    def test_basic_lines_and_priority(self):
        res = PT.parse_persona_text(
            "# 注释行不算\n\n说话短句为主\n[50] 认准的事会坚持\n[ 7 ] 带空格也行\n")
        self.assertEqual(res["problems"], [])
        self.assertEqual([i["content"] for i in res["items"]],
                         ["说话短句为主", "认准的事会坚持", "带空格也行"])
        self.assertEqual([i["priority"] for i in res["items"]], [0, 50, 7])
        # 行号按原文算，注释和空行也占位置
        self.assertEqual([i["line"] for i in res["items"]], [3, 4, 5])
        self.assertEqual({i["kind"] for i in res["items"]}, {"self"})

    def test_default_priority_applied(self):
        res = PT.parse_persona_text("一条\n两条\n", default_priority=30)
        self.assertEqual(res["problems"], [])
        self.assertEqual([i["priority"] for i in res["items"]], [30, 30])

    def test_raw_priority_overrides_default(self):
        res = PT.parse_persona_text("[99] 高的\n低的\n", default_priority=5)
        self.assertEqual([i["priority"] for i in res["items"]], [99, 5])

    def test_inner_brackets_kept(self):
        res = PT.parse_persona_text("[50] 用 [重要] 标记正文里的方括号\n")
        self.assertEqual(res["problems"], [])
        self.assertEqual(res["items"][0]["content"], "用 [重要] 标记正文里的方括号")
        self.assertEqual(res["items"][0]["priority"], 50)

    def test_full_width_and_whitespace_trimmed(self):
        res = PT.parse_persona_text("   前后有空格   \n")
        self.assertEqual(res["items"][0]["content"], "前后有空格")

    # ---------- 挑错 ----------

    def test_empty_text_is_a_problem(self):
        for text in ("", "   \n\n", "# 只有注释\n# 还是注释\n"):
            res = PT.parse_persona_text(text)
            self.assertEqual(res["items"], [])
            self.assertEqual(len(res["problems"]), 1)
            self.assertIn("没有可写入的内容", res["problems"][0])

    def test_duplicate_reports_both_lines(self):
        res = PT.parse_persona_text("同一句\n别的一句\n同一句\n")
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("第 3 行与第 1 行内容重复", res["problems"][0])
        # 重复的那条不进 items，前面那条留着
        self.assertEqual([i["content"] for i in res["items"]], ["同一句", "别的一句"])

    def test_too_long_line(self):
        res = PT.parse_persona_text("长" * 201 + "\n")
        self.assertEqual(res["items"], [])
        self.assertIn("超过单条 200 字上限", res["problems"][0])

    def test_exactly_200_chars_is_ok(self):
        res = PT.parse_persona_text("长" * 200 + "\n")
        self.assertEqual(res["problems"], [])
        self.assertEqual(len(res["items"]), 1)

    def test_prefix_without_content(self):
        res = PT.parse_persona_text("[50]   \n")
        self.assertEqual(res["items"], [])
        self.assertIn("写了权重但没写内容", res["problems"][0])

    def test_priority_over_limit(self):
        res = PT.parse_persona_text("[9999] 想上天\n")
        self.assertEqual(res["items"], [])
        self.assertIn("超过上限", res["problems"][0])

    def test_bad_default_priority(self):
        bad = PT.parse_persona_text("一条\n", default_priority="abc")
        self.assertTrue(any("默认权重必须是整数" in p for p in bad["problems"]))
        over = PT.parse_persona_text("一条\n", default_priority=2000)
        self.assertTrue(any("0..1000" in p for p in over["problems"]))

    def test_too_many_items(self):
        text = "\n".join("第%d条" % i for i in range(1, PT.MAX_ITEMS + 2))
        res = PT.parse_persona_text(text)
        self.assertEqual(len(res["items"]), PT.MAX_ITEMS)
        self.assertTrue(any("一次最多" in p for p in res["problems"]))

    def test_all_problems_collected_not_just_first(self):
        """一次报全，别让人改一条跑一次。"""
        res = PT.parse_persona_text("长" * 201 + "\n[9999] 越界\n[3]\n")
        self.assertEqual(len(res["problems"]), 3)


class BatchWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    def _items(self, text, **kw):
        res = PT.parse_persona_text(text, **kw)
        self.assertEqual(res["problems"], [])
        return res["items"]

    def test_batch_lands_in_one_commit(self):
        before = self.led.stats()["commits"]
        ids = self.led.add_persona_batch(
            self._items("说话短句为主\n[50] 认准的事会坚持\n[7] 第三条\n"))
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.led.stats()["commits"], before + 1)
        rows = {r["id"]: r for r in self.led.list_persona()}
        self.assertEqual(set(rows), set(ids))
        self.assertEqual(rows[ids[0]]["priority"], 0)
        self.assertEqual(rows[ids[1]]["priority"], 50)
        self.assertEqual({r["source"] for r in rows.values()}, {"handwrite"})
        self.assertEqual({r["kind"] for r in rows.values()}, {"self"})

    def test_replay_after_batch_is_consistent(self):
        """写完自检不报问题：整批落库后提交链与投影对得上。"""
        ids = self.led.add_persona_batch(self._items("甲\n乙\n丙\n"))
        self.assertEqual(self.led.health_check()["problems"], [])
        self.assertEqual({r["id"] for r in self.led.list_persona()}, set(ids))

    def test_core_entry_rejected_before_any_write(self):
        before = self.led.stats()["commits"]
        items = [{"kind": "self", "content": "好的第一条"},
                 {"kind": "core", "content": "想偷改基础设定"}]
        with self.assertRaises(L.LedgerError):
            self.led.add_persona_batch(items)
        self.assertEqual(self.led.list_persona(), [])
        self.assertEqual(self.led.stats()["commits"], before)

    def test_empty_batch_rejected(self):
        before = self.led.stats()["commits"]
        with self.assertRaises(L.LedgerError):
            self.led.add_persona_batch([])
        self.assertEqual(self.led.stats()["commits"], before)

    def test_bad_item_shapes_rejected(self):
        cases = [
            ["not-a-dict"],
            [{"content": ""}],
            [{"content": "长" * 201}],
            [{"content": "权重不是数字", "priority": "高"}],
            [{"content": "权重越界", "priority": 5000}],
            [{"content": "负权重", "priority": -1}],
        ]
        for items in cases:
            with self.assertRaises(L.LedgerError, msg=repr(items)):
                self.led.add_persona_batch(items)
        self.assertEqual(self.led.list_persona(), [])

    def test_source_too_long_rejected(self):
        with self.assertRaises(L.LedgerError):
            self.led.add_persona_batch(self._items("一条\n"), source="s" * 65)

    def test_auto_path_is_locked(self):
        """自动路径里（梦产出等）整批也写不进：反向守卫先拦。"""
        before = self.led.stats()["commits"]
        with self.led.restricted_auto_write():
            with self.assertRaises(L.LedgerError):
                self.led.add_persona_batch(self._items("自动想塞一条\n"))
        self.assertEqual(self.led.list_persona(), [])
        self.assertEqual(self.led.stats()["commits"], before)

    def test_builder_without_token_rejected(self):
        """绕过门面直连 builder，缺通行令牌照样写不进，且不留半个提交。"""
        before = self.led.stats()["commits"]
        with self.assertRaises(L.LedgerError):
            with self.led.commit("偷偷写一条人格") as c:
                c.add_persona("self", "绕过门面")
        self.assertEqual(self.led.stats()["commits"], before)
        self.assertEqual(self.led.list_persona(), [])

    def test_token_restored_after_batch(self):
        """整批写完令牌归位，后续裸写仍然被拒。"""
        self.led.add_persona_batch(self._items("一条\n"))
        self.assertEqual(L._PERSONA_WRITE_AUTH.get(), "")

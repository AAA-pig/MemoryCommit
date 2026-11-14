# -*- coding: utf-8 -*-
"""记忆账本的自测。不依赖 AstrBot，也不依赖 pytest。

    python tests/test_ledger.py
"""

import asyncio
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ledger as L  # noqa: E402
except ModuleNotFoundError:  # 仓库里文件名可能是 Ledger.py（Windows 上大小写不吃这一套）
    import importlib.util

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _spec = importlib.util.spec_from_file_location("ledger", os.path.join(_root, "Ledger.py"))
    L = importlib.util.module_from_spec(_spec)
    sys.modules["ledger"] = L
    _spec.loader.exec_module(L)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    # ---------- 基本写入 ----------

    def test_empty_ledger(self):
        st = self.led.stats()
        self.assertEqual(st["memories"], 0)
        self.assertEqual(st["head"], "")

    def test_write_creates_commit_and_clue(self):
        r = self.led.remember("用户怕冷，空调别开太低", tags=["用户", "习惯"],
                              level="topic", importance=0.9, grade=2)
        self.assertTrue(r["memory_id"].startswith("m"))
        self.assertEqual(r["commit"], "C1")
        st = self.led.stats()
        self.assertEqual(st["memories"], 1)
        self.assertEqual(st["clues"], 2)
        self.assertEqual(st["head"], "C1")

    def test_same_clue_is_reused(self):
        self.led.remember("结算服务用 5433", tags=["结算服务"])
        self.led.remember("结算服务报警", tags=["结算服务"])
        self.assertEqual(self.led.stats()["clues"], 1)
        self.assertEqual(self.led.stats()["memories"], 2)

    def test_auto_link_on_shared_tag(self):
        a = self.led.remember("结算服务用 5433", tags=["结算服务"])
        b = self.led.remember("结算服务报警了", tags=["结算服务"])
        links = self.led.stats()["links"]
        self.assertEqual(links, 1, "同一标签应织出一条边")
        self.assertNotEqual(a["memory_id"], b["memory_id"])

    def test_empty_content_rejected(self):
        with self.assertRaises(L.LedgerError):
            self.led.remember("   ")

    def test_too_long_rejected(self):
        with self.assertRaises(L.LedgerError):
            self.led.remember("啊" * 4001)

    # ---------- 提交链 ----------

    def test_stale_head_rejected(self):
        builder = self.led.commit("我慢了一步")
        self.led.remember("别人先写了", tags=["x"])
        builder.add_memory("迟到的写入")
        with self.assertRaises(L.StaleHeadError):
            builder.seal()

    def test_empty_commit_rejected(self):
        with self.assertRaises(L.LedgerError):
            with self.led.commit("空提交"):
                pass

    def test_commit_row_seq_is_ordered(self):
        self.led.remember("一条带两个标签的记忆", tags=["甲", "乙"])
        rows = self.led.show_commit("C1")["rows"]
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))

    def test_replay_to_middle(self):
        self.led.remember("第一条", tags=["a"])
        self.led.remember("第二条", tags=["a"])
        self.led.remember("第三条", tags=["b"])
        self.assertEqual(len(self.led.materialize("C1")), 1)
        self.assertEqual(len(self.led.materialize("C2")), 2)
        self.assertEqual(len(self.led.materialize()), 3)

    def test_compact_keeps_head_and_state(self):
        for i in range(14):
            self.led.remember("第%d件小事" % i, tags=["测试"])
        before = self.led.stats()
        before_mem = [m["summary"] for m in self.led.materialize()]
        res = self.led.compact(keep_recent=5)
        self.assertTrue(res["compacted"])
        self.assertEqual(self.led.head(), before["head"], "压缩不该动 HEAD")
        self.assertEqual([m["summary"] for m in self.led.materialize()], before_mem)
        for k in ("head", "memories", "clues", "links", "profiles", "created_seq"):
            self.assertEqual(before[k], self.led.stats()[k], "当前状态不该被压缩动到：%s" % k)
        self.assertEqual(self.led.stats()["commits"], 6, "一条基线 + 五条保留")

    def test_compact_keeps_middle_commit_replay(self):
        for i in range(8):
            self.led.remember("第%d件事" % i, tags=["甲"] if i % 2 else ["乙"])
        before = [m["summary"] for m in self.led.materialize("C7")]
        self.led.compact(keep_recent=3)
        after = [m["summary"] for m in self.led.materialize("C7")]
        self.assertEqual(before, after, "保留的提交，回放结果必须和压缩前一样")
        # 被压掉的提交不再单独存在；以前这里返回空表，跟"提交号打错了"分不清
        with self.assertRaises(L.LedgerError):
            self.led.materialize("C2")

    def test_compact_baseline_is_one_snapshot(self):
        for i in range(6):
            self.led.remember("第%d件事" % i, tags=["甲"])
        self.led.compact(keep_recent=2)
        base = self.led.show_commit("C0")
        self.assertIsNotNone(base)
        self.assertIsNone(base["parent"], "基线是新的根")
        self.assertTrue(base["rows"])
        self.assertEqual({r["op"] for r in base["rows"]}, {"insert"}, "基线全是快照插入")

    def test_new_commit_after_compact_links_to_head(self):
        for i in range(6):
            self.led.remember("第%d件事" % i)
        self.led.compact(keep_recent=2)
        head = self.led.head()
        r = self.led.remember("压缩之后又记了一件")
        self.assertEqual(self.led.show_commit(r["commit"])["parent"], head,
                         "压缩后新提交照旧接在 HEAD 上")

    def test_compact_skips_when_short(self):
        self.led.remember("就这一件")
        res = self.led.compact(keep_recent=5)
        self.assertFalse(res["compacted"])
        self.assertEqual(self.led.stats()["commits"], 1)

    def test_delete_is_recorded_not_erased(self):
        r = self.led.remember("要忘掉的")
        self.led.forget(r["memory_id"], "测试")
        self.assertEqual(len(self.led.materialize()), 0)
        self.assertEqual(len(self.led.materialize("C1")), 1, "历史里还得查得到")
        self.assertEqual(self.led.stats()["commits"], 2)

    # ---------- 检索 ----------

    def test_recall_direct(self):
        self.led.remember("结算接口报 502，连接池打满", tags=["结算服务"])
        self.led.remember("今天擦了 12 只杯子", tags=["咖啡馆"])
        hits = self.led.recall("结算 502", limit=3, touch=False)
        self.assertTrue(hits)
        self.assertIn("结算", hits[0]["summary"])

    def test_recall_graph_expand(self):
        self.led.remember("结算服务用 5433 端口", tags=["结算服务", "服务器"])
        self.led.remember("从库连接池只有 20 个", tags=["结算服务", "连接池"])
        hits = self.led.recall("5433", limit=3, touch=False)
        ids = [h["id"] for h in hits]
        self.assertIn("m2", ids, "应沿标签连边带出邻居")

    def test_recall_no_match(self):
        self.led.remember("今天擦了杯子", tags=["咖啡馆"])
        self.assertEqual(self.led.recall("量子色动力学", limit=3, touch=False), [])

    def test_recall_updates_access(self):
        r = self.led.remember("被翻过的记忆")
        before = self.led.stats()["commits"]
        self.led.recall("被翻过", limit=1, touch=True)
        self.assertEqual(self.led.stats()["commits"], before,
                         "访问痕迹不该再往提交链里写：想起不等于改写")
        after = self.led._query_one("SELECT access_count FROM memory WHERE id = ?",
                                    (r["memory_id"],))
        self.assertEqual(after["access_count"], 1, "访问计数仍然要涨")
        self.assertEqual(len(self.led.access_history(r["memory_id"])), 1,
                         "这次访问要能在访问日志里查到")

    # ---------- 冲刷 ----------

    def test_no_flush_when_nothing_new(self):
        self.led.remember("刚写完的记忆", importance=0.1)
        self.assertEqual(self.led.flush_plan(), [], "冲刷量为 0 时不该有任何待处理")

    def test_flush_demotes_then_deletes(self):
        self.led.remember("没用的流水账", importance=0.15, grade=1)
        for i in range(10):
            self.led.remember("新记忆 %d" % i, importance=0.15)
        plan = self.led.flush_plan()
        self.assertTrue(plan)
        first = self.led.flush(apply=True)
        self.assertGreaterEqual(first["demoted"], 1)
        self.assertEqual(first["deleted"], 0, "第一次只降级不淘汰")
        second = self.led.flush(apply=True)
        self.assertGreaterEqual(second["deleted"], 1, "降到零级再超就该淘汰")

    def test_pinned_memory_survives(self):
        self.led.remember("用户怕冷，这是核心事实", level="topic",
                          importance=0.95, grade=2)
        keep = self.led.remember("高重要性记忆", level="day", importance=0.9, grade=2)
        for i in range(30):
            self.led.remember("流水账 %d" % i, importance=0.1)
        for _ in range(6):
            self.led.flush(apply=True)
        left = [m["id"] for m in self.led.materialize()]
        self.assertIn(keep["memory_id"], left, "重要性 >= 0.85 应被钉住")
        self.assertIn("m1", left, "topic 层应被钉住")

    # ---------- 画像 ----------

    def test_profile_weight_accumulates(self):
        self.led.remember_profile("master", "用户喜欢深夜写代码")
        self.led.remember_profile("master", "用户喜欢深夜写代码")
        rows = self.led.profiles("master")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weight"], 2)

    # ---------- 导出 ----------

    def test_export_markdown(self):
        self.led.remember("导出一条试试", tags=["导出"])
        path = os.path.join(self.tmp, "out.md")
        text = self.led.export_markdown(path)
        self.assertIn("记忆账本", text)
        self.assertIn("导出一条试试", text)
        self.assertTrue(os.path.exists(path))


    # ---------- 唤起：检索 → 联想 → 激活 ----------

    def test_activate_search_route(self):
        self.led.remember("结算接口报 502，连接池打满", tags=["结算服务"])
        res = self.led.activate("结算 502 那次", slots=2, with_profile=False)
        self.assertEqual(res["route"], "search")
        self.assertTrue(res["items"])
        self.assertIn("结算", res["items"][0]["summary"])

    def test_activate_falls_back_to_fresh(self):
        self.led.remember("今天擦了 12 只杯子", tags=["咖啡馆"], importance=0.6)
        res = self.led.activate("", slots=2, with_profile=False)
        self.assertEqual(res["route"], "activate", "没人问起时，让新鲜重要的自己浮现")
        self.assertTrue(res["items"])

    def test_query_without_match_falls_back_to_fresh(self):
        """有检索词但一条都没命中：降级走 fresh 兜底，route 标 fallback，不再整轮空转。"""
        self.led.remember("今天擦了 12 只杯子", tags=["咖啡馆"], importance=0.6)
        res = self.led.activate("量子色动力学", slots=2, with_profile=False)
        self.assertTrue(res["items"], "零命中该有兜底注入，别整轮空转")
        self.assertEqual(res["route"], "fallback", "兜底要单独标记，别伪装成正常检索")

    def test_activate_associate_route(self):
        self.led.remember("结算服务用 5433 端口", tags=["结算服务", "服务器"])
        self.led.remember("从库连接池只有 20 个", tags=["结算服务", "连接池"])
        res = self.led.activate("5433", slots=3, with_profile=False)
        ids = [i["id"] for i in res["items"]]
        self.assertIn("m2", ids, "检索之外还要沿连边联想")

    def test_activate_associate_two_hops(self):
        self.led.remember("甲", tags=["tag甲"])
        self.led.remember("乙", tags=["tag甲", "tag乙"])
        self.led.remember("丙", tags=["tag乙"])
        res = self.led.activate("甲", slots=3, with_profile=False)
        ids = [i["id"] for i in res["items"]]
        self.assertIn("m3", ids, "联想要能走两步：甲 → 乙 → 丙")

    def test_activate_cooldown(self):
        self.led.remember("只该浮现一次的事", tags=["测试"])
        first = self.led.activate("只该浮现", slots=1, with_profile=False)
        self.assertTrue(first["items"])
        second = self.led.activate("只该浮现", slots=1, with_profile=False)
        self.assertNotIn(first["items"][0]["id"],
                         [i["id"] for i in second["items"]],
                         "冷却期内不该反复念同一件事")

    def test_query_no_match_fallback_respects_cooldown(self):
        """fallback 兜底也要吃冷却：同一轮 cooldown 内不再重复注入同一条。"""
        self.led.remember("杯子要擦干净", importance=0.6)
        first = self.led.activate("量子色动力学", slots=10, with_profile=False)
        self.assertEqual(first["route"], "fallback")
        self.assertTrue(first["items"])
        second = self.led.activate("量子色动力学", slots=10, with_profile=False)
        self.assertEqual(second["route"], "fallback_blocked",
                         "冷却内不该再浮现：兜底被挡光，要单独标出来，别和 route=none 混淆")
        self.assertEqual(second["items"], [])

    def test_fallback_keeps_within_budget(self):
        """fallback 兜底数量保守：420 预算不可能被塞满。"""
        for i in range(10):
            self.led.remember("第 %d 条值得记的事，关于杯子和日落" % i, importance=0.6)
        res = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertEqual(res["route"], "fallback")
        self.assertLessEqual(len(res["items"]), 2, "兜底最多补 1~2 条")
        brief = self.led.render_brief(res, max_chars=420)
        self.assertLessEqual(len(brief), 420, "兜底注入也不能突破 420 预算")

    def test_activate_dry_run_does_not_touch(self):
        self.led.remember("调试不该算一次访问")
        before = self.led.stats()["commits"]
        self.led.activate("调试", slots=1, with_profile=False, dry_run=True)
        self.assertEqual(self.led.stats()["commits"], before)

    def test_activate_marks_access(self):
        r = self.led.remember("被想起会延后遗忘")
        before = self.led.stats()["commits"]
        self.led.activate("被想起", slots=1, with_profile=False)
        self.assertEqual(self.led.stats()["commits"], before,
                         "唤起也不该动提交链：浮现一万次，账本厚度不该变")
        row = self.led._query_one("SELECT access_count, last_access_seq"
                                  " FROM memory WHERE id = ?", (r["memory_id"],))
        self.assertEqual(row["access_count"], 1)
        self.assertGreaterEqual(row["last_access_seq"], 1, "访问序号要跟上，否则会被误判成陈旧")

    def test_render_brief_keeps_budget(self):
        for i in range(6):
            self.led.remember("第 %d 条挺长的记忆，用来试注入预算会不会被撑爆" % i)
        res = self.led.activate("", slots=6, with_profile=False)
        brief = self.led.render_brief(res, max_chars=120)
        self.assertIn("[记忆账本", brief)
        self.assertLessEqual(len(brief), 160, "收尾提示之外不该再超预算")

    def test_render_brief_reports_budget_stats(self):
        for i in range(6):
            self.led.remember("第 %d 条挺长的记忆，用来试注入预算会不会被撑爆" % i)
        res = self.led.activate("", slots=6, with_profile=False)
        st = {}
        brief = self.led.render_brief(res, max_chars=120, stats=st)
        self.assertEqual(st["candidates"], len(res["items"]))
        self.assertEqual(st["rendered"] + st["skipped_budget"], st["candidates"],
                         "渲染数 + 预算丢弃数 必须等于候选数")
        self.assertEqual(st["chars"], len(brief))
        self.assertEqual(st["budget"], 120)
        self.assertGreater(st["skipped_budget"], 0, "120 字装不下 6 条，必须记下丢了几条")

    def test_render_brief_never_exceeds_budget(self):
        """标题和收尾句先扣掉之后，任何预算下都不许再超。"""
        for i in range(8):
            self.led.remember("第 %d 条挺长的记忆，专门用来把预算顶满看看会不会漏出去" % i)
        self.led.remember_profile("master", "用户怕冷，空调喜欢开 26 度", "preference")
        res = self.led.activate("", slots=8, with_profile=True)
        for budget in (80, 120, 250, 420):
            brief = self.led.render_brief(res, max_chars=budget)
            self.assertLessEqual(len(brief), budget,
                                 "%d 字预算被撑到 %d 字" % (budget, len(brief)))

    def test_render_brief_reserves_room_for_profiles(self):
        """画像排在最后，但额度先留出来，否则条目一多就永远轮不到它。"""
        for i in range(6):
            self.led.remember("第 %d 条记忆，写得长一点，好把正文挤满" % i)
        self.led.remember_profile("master", "用户怕冷，空调喜欢开 26 度", "preference")
        res = self.led.activate("", slots=6, with_profile=True)
        st = {}
        brief = self.led.render_brief(res, max_chars=200, stats=st)
        self.assertIn("26 度", brief, "画像不该被条目饿死")
        self.assertGreater(st["reserved_for_profiles"], 0)
        self.assertLessEqual(st["profiles_rendered"], 2)
        self.assertEqual(st["chars"], len(brief))

    def test_render_brief_without_stats_is_unchanged(self):
        self.led.remember("不传 stats 时输出必须一模一样")
        res = self.led.activate("", slots=3, with_profile=False)
        self.assertEqual(self.led.render_brief(res, max_chars=420),
                         self.led.render_brief(res, max_chars=420, stats={}))

    def test_clue_weight_prefers_rare(self):
        rare, common = L.clue_weight(50, 1), L.clue_weight(50, 40)
        self.assertGreater(rare, common, "越少人用的线索越有辨识度")
        self.assertGreaterEqual(common, 0.1)
        self.assertLessEqual(rare, 1.0)

    def test_rare_clue_ranks_above_common_clue(self):
        self.led.remember("端口又冲突了", tags=["端口"])
        self.led.remember("端口出问题了", tags=["端口号"])
        for i in range(5):
            self.led.remember("随手记的第%d件小事" % i, tags=["端口号"])
        rows = self.led.recall("端口", limit=10, touch=False)
        top = rows[0]
        self.assertEqual(top["summary"], "端口又冲突了", "稀有线索命中的记忆该排最前")
        common = [r for r in rows if r["summary"] == "端口出问题了"][0]
        self.assertGreater(top["clue_boost"], common["clue_boost"] * 3,
                           "「端口」只挂一条，权重该远高于挂着六条的「端口号」")

    def test_clue_boost_is_applied_to_the_score(self):
        self.led.remember("端口又冲突了", tags=["端口"])
        for i in range(5):
            self.led.remember("随手记的第%d件小事" % i, tags=["端口号"])
        hit = [r for r in self.led.recall("端口", limit=10, touch=False)
               if r["summary"] == "端口又冲突了"][0]
        self.assertEqual(hit["match"], "direct+clue")
        plain = hit["bm25"] * hit["decay"]
        self.assertGreater(hit["score"], plain * 1.5,
                           "线索权重必须真的进主排序，不是只记在字段里")
        self.assertAlmostEqual(hit["score"] - plain * (1 + L.CLUE_SORT_BOOST * hit["clue_boost"]),
                               0.11, delta=0.02,
                               msg="抬升只能来自线索加成，剩下的固定项不该跟着变大")

    def test_common_clue_does_not_lift_much(self):
        for i in range(6):
            self.led.remember("日常第%d件" % i, tags=["日常"])
        rows = self.led.recall("日常", limit=10, touch=False)
        self.assertTrue(rows)
        for r in rows[:3]:
            self.assertLess(r["clue_boost"], 0.3,
                            "满地都是的线索不该把记忆抬起来")

    def test_clue_bridge_direct(self):
        self.led.remember("凌晨三点还在改代码", tags=["熬夜"])
        cands = self.led._clue_bridge("熬夜", limit=5)
        self.assertTrue(cands, "撞上线索就该顺着线索牵出记忆")
        self.assertEqual(cands[0]["match"], "clue:熬夜")
        self.assertEqual(cands[0]["bridge_clue"], "熬夜")

    def test_render_brief_shows_flashes(self):
        res = {"items": [{"summary": "凌晨在改代码", "how": "提到「熬夜」想起来的",
                          "created_at": "2026-11-08T02:00:00"}],
               "profiles": [], "flashes": ["熬夜", "代码"]}
        brief = self.led.render_brief(res)
        self.assertIn("顺着这些词想到的", brief)
        self.assertIn("熬夜", brief)

    def test_render_brief_empty(self):
        self.assertEqual(self.led.render_brief({"items": [], "profiles": []}), "")

    def test_activate_includes_profile(self):
        self.led.remember_profile("master", "用户怕冷，空调喜欢开 26 度", "preference")
        res = self.led.activate("", slots=1, with_profile=True)
        self.assertTrue(res["profiles"])
        self.assertIn("26 度", self.led.render_brief(res))

    # ---------- 自动压缩 ----------

    def test_auto_compact_off_by_default(self):
        self.led.auto_compact_after = 0
        for i in range(5):
            self.led.remember("第 %d 条" % i)
        self.assertEqual(self.led.stats()["commits"], 5)

    def test_auto_compact_triggers_and_keeps_memories(self):
        self.led.auto_compact_after = 3
        self.led.auto_compact_keep = 3
        for i in range(8):
            self.led.remember("第 %d 条记忆" % i, tags="自动压缩")
        st = self.led.stats()
        self.assertLess(st["commits"], 8, "超过阈值该被压下来")
        self.assertEqual(st["memories"], 8, "压缩不能丢记忆")
        self.assertTrue(self.led.recall("记忆"), "压完还要检索得到")
        # 压完还能继续写，HEAD 照常往前走
        r = self.led.remember("压完之后写的一条")
        self.assertTrue(r["commit"].startswith("C"))

    def test_auto_compact_failure_does_not_break_write(self):
        self.led.auto_compact_after = 1
        self.led.auto_compact_keep = 99  # 保留数比总数还多，compact 会直接跳过
        r = self.led.remember("照样要写进去")
        self.assertTrue(r["memory_id"])
        self.assertEqual(self.led.stats()["memories"], 1)

    # ---------- 每日导出 ----------

    def test_maybe_export_daily_once(self):
        self.led.remember("导出用的记忆")
        path = os.path.join(self.tmp, "记忆账本.md")
        self.assertTrue(self.led.maybe_export_daily(path), "当天第一次应该导")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            self.assertIn("导出用的记忆", f.read())
        self.assertFalse(self.led.maybe_export_daily(path), "同一天不该重复导")

    def test_maybe_export_daily_next_day(self):
        path = os.path.join(self.tmp, "账本.md")
        self.led.remember("跨天的记忆")
        self.led.maybe_export_daily(path)
        with self.led._tx() as cx:
            self.led._set_meta(cx, "last_export_date", "1970-01-01")
        self.assertTrue(self.led.maybe_export_daily(path), "跨天应该再导一次")

    def test_maybe_export_daily_without_path(self):
        self.assertFalse(self.led.maybe_export_daily(""))

    # ---------- 自检 ----------

    def test_health_check_clean(self):
        self.led.remember("自检用的记忆", tags="自检")
        path = os.path.join(self.tmp, "自检导出.md")
        res = self.led.health_check(path)
        self.assertTrue(res["ok"], res["problems"])
        self.assertTrue(res["export_ok"])
        self.assertEqual(res["stats"]["memories"], 1)

    def test_health_check_catches_broken_parent(self):
        self.led.remember("第一条")
        self.led.remember("第二条")
        with self.led._tx() as cx:
            cx.execute("UPDATE mcommit SET parent = 'C999' WHERE id = 'C2'")
        res = self.led.health_check()
        self.assertFalse(res["ok"])
        self.assertTrue(any("C999" in p for p in res["problems"]))

    def test_health_check_on_empty_ledger(self):
        res = self.led.health_check()
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["stats"]["memories"], 0)

    # ---------- 检索侧强化 + 边界（从桌面那份并回来） ----------

    def test_single_char_query_falls_back_to_substring(self):
        """写入只切双字，「辣」这类单字靠子串兜底才搜得到。"""
        self.led.remember("用户喜欢吃辣的", tags="food")
        self.led.remember("周末去咖啡馆上班", tags="work")
        hits = self.led.recall("辣", limit=5)
        self.assertEqual([h["summary"] for h in hits], ["用户喜欢吃辣的"])

    def test_recall_limit_is_clamped(self):
        for i in range(6):
            self.led.remember("测试记忆第%d条" % i, tags="t")
        self.assertEqual(len(self.led.recall("测试", limit=3)), 3)
        self.assertEqual(len(self.led.recall("测试", limit=0)), 1)
        self.assertEqual(len(self.led.recall("测试", limit=-9)), 1)
        self.assertEqual(len(self.led.recall("测试", limit="2")), 2)
        self.assertLessEqual(len(self.led.recall("测试", limit=10 ** 6)),
                             L.MAX_RECALL_LIMIT)

    def test_empty_ledger_recall_and_flush(self):
        self.assertEqual(self.led.recall("任何东西"), [])
        res = self.led.flush(apply=True)
        self.assertFalse(res["applied"])
        self.assertEqual(res["count"], 0)

    def test_materialize_unknown_commit_raises(self):
        self.led.remember("先写一条", tags="x")
        self.assertEqual(len(self.led.materialize()), 1)
        with self.assertRaises(L.LedgerError):
            self.led.materialize("C999")

    def test_time_decay_survives_future_timestamp(self):
        """时钟回拨 / 未来时间不能算出负年龄，也不能炸。"""
        self.assertAlmostEqual(L.time_decay(L.now_str()), 1.0, places=2)
        self.assertGreaterEqual(L.time_decay("2099-01-01T00:00:00"), 0.0)
        self.assertLessEqual(L.time_decay("2099-01-01T00:00:00"), 1.0)
        self.assertGreater(L.time_decay("2000-01-01T00:00:00"), 0.0)

    def test_duplicate_tags_do_not_double_link(self):
        r = self.led.remember("标签重复的测试", tags="重复,重复,重复")
        self.assertTrue(r["memory_id"])
        st = self.led.stats()
        self.assertEqual(st["clues"], 1)
        self.assertEqual(st["links"], 0)  # 同一条记忆不和自己连边

    def test_effective_half_life_grows_with_access(self):
        base = L.effective_half_life(L.HALF_LIFE_DAYS, 0)
        self.assertAlmostEqual(base, L.HALF_LIFE_DAYS, places=6)
        self.assertGreater(L.effective_half_life(L.HALF_LIFE_DAYS, 7), base)
        self.assertLessEqual(L.effective_half_life(L.HALF_LIFE_DAYS, 99999),
                             L.HALF_LIFE_DAYS * L.MAX_HALF_LIFE_FACTOR + 1e-6)

    def test_accessed_memory_decays_slower(self):
        old = "2025-01-01T00:00:00"
        cold = L.time_decay(old, L.HALF_LIFE_DAYS, 0)
        warm = L.time_decay(old, L.HALF_LIFE_DAYS, 20)
        self.assertGreater(warm, cold)
        self.assertGreater(cold, 0.0)  # 再旧也不归零

    def test_recall_carries_decay_after_change(self):
        self.led.remember("用户喜欢在深夜把歌放得很轻", tags="音乐,深夜")
        self.led.remember("周末在咖啡馆兼职", tags="兼职")
        got = self.led.recall("深夜的歌", limit=3)
        self.assertTrue(got)
        self.assertGreater(got[0]["decay"], 0.0)
        self.assertLessEqual(got[0]["decay"], 1.0)

    def test_recall_reuses_tokens_when_content_unchanged(self):
        """内容没变就不重复切词：第二轮只剩查询本身要切。"""
        for i in range(30):
            self.led.remember("第 %d 条：深夜把歌放得很轻" % i, tags="音乐,深夜")
        with mock.patch.object(L, "tokenize", wraps=L.tokenize) as m:
            self.led.recall("深夜的歌", limit=3)
            first = m.call_count
        with mock.patch.object(L, "tokenize", wraps=L.tokenize) as m:
            self.led.recall("深夜的歌", limit=3)
            second = m.call_count
        self.assertGreater(first, 30, "第一轮要把每条记忆都切一遍")
        self.assertLessEqual(second, 3, "第二轮不该再逐条切词，只有查询和少量零碎")

    def test_token_cache_follows_content_change(self):
        """摘要改过之后必须重新切：缓存靠比对原文失效，不靠谁记得去清。"""
        r = self.led.remember("用户怕冷，空调别开太低", tags="习惯")
        row = dict(self.led._query_one("SELECT * FROM memory WHERE id = ?",
                                       (r["memory_id"],)))
        first = self.led._doc_tokens(row)
        self.assertIn("怕冷", first)
        row["summary"] = "用户怕热，空调开低点"
        second = self.led._doc_tokens(row)
        self.assertIn("怕热", second)
        self.assertNotIn("怕冷", second, "原文变了就不能再吃旧缓存")

    def test_token_cache_does_not_grow_forever(self):
        """删掉的记忆不该在缓存里越攒越多。"""
        ids = [self.led.remember("临时条目 %d" % i)["memory_id"] for i in range(600)]
        self.led.recall("临时条目", limit=3)
        for mid in ids[:400]:
            self.led.forget(mid)
        self.led.recall("临时条目", limit=3)
        self.assertLess(len(self.led._tok_cache), 600, "缓存要跟着当前记忆收缩")

    def test_concurrent_read_write_no_error(self):
        """同一条连接读写并发：加了锁就不该出现事务嵌套/连接错误。"""
        import threading
        self.led.remember("深夜的歌单", tags="音乐")
        errs = []

        def reader():
            try:
                for _ in range(40):
                    self.led.recall("深夜", limit=3)
                    self.led.stats()
            except Exception as e:
                errs.append(e)

        def writer():
            try:
                for i in range(40):
                    self.led.remember("并发写入的第 %d 条" % i, tags="并发")
            except Exception as e:
                errs.append(e)

        ts = [threading.Thread(target=reader) for _ in range(2)]
        ts.append(threading.Thread(target=writer))
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [], "并发读写不该报错：%r" % (errs[:3],))


class DurabilityTest(unittest.TestCase):
    """P1-4：持久性档位是显式配置项，不是隐含默认。

    synchronous=NORMAL 之前写死：常见崩溃场景一致，但断电可能丢最近
    几秒写入。现在默认仍 NORMAL（不改变既有行为），要更强保证显式传
    FULL；非法值直接拒开——把账本当唯一记忆源的人值得得到明确的错，
    而不是悄悄降级。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_default_is_normal(self):
        led = L.Ledger(os.path.join(self.tmp, "n.db"))
        try:
            self.assertEqual(led.durability, "NORMAL")
            mode = int(led._conn.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual(mode, 1, "SQLite 里 1 = NORMAL")
        finally:
            led.close()

    def test_full_accepted_case_insensitive(self):
        led = L.Ledger(os.path.join(self.tmp, "f.db"), durability="full")
        try:
            self.assertEqual(led.durability, "FULL")
            mode = int(led._conn.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual(mode, 2, "SQLite 里 2 = FULL")
        finally:
            led.close()

    def test_illegal_durability_fails_loudly(self):
        with self.assertRaises(L.LedgerError):
            L.Ledger(os.path.join(self.tmp, "x.db"), durability="OFF")


class MigrationTest(unittest.TestCase):
    """跨宿主搬库：结构版本要认得出，导出要能原样灌回来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "src.db")
        self.led = L.Ledger(self.path)

    def tearDown(self):
        self.led.close()

    def _dump_path(self):
        return os.path.join(self.tmp, "dump.jsonl")

    def _seed(self):
        self.led.remember("用户怕冷，空调喜欢开 26 度", tags="用户,习惯",
                          level="topic", importance=0.9)
        self.led.remember("结算服务用 5433", tags="结算服务")
        self.led.remember_profile("master", "用户习惯凌晨才睡", category="preference")

    def test_new_db_records_schema_version(self):
        self.assertEqual(self.led.schema_version(), L.SCHEMA_VERSION)
        self.assertEqual(self.led.stats()["schema_version"], L.SCHEMA_VERSION)

    def test_old_db_without_version_is_migrated_and_kept(self):
        """老库没有版本号：应被认成 0，升到当前版本，但一条数据都不动。"""
        self._seed()
        with self.led._tx() as cx:
            cx.execute("DELETE FROM meta WHERE k = 'schema_version'")
        self.led.close()
        again = L.Ledger(self.path)
        try:
            self.assertEqual(again.schema_version(), L.SCHEMA_VERSION)
            self.assertEqual(again.stats()["memories"], 2)
            self.assertEqual(again.stats()["profiles"], 1)
        finally:
            again.close()

    def test_newer_db_is_refused(self):
        """库比代码新就别硬开，否则新列会被悄悄忽略。"""
        self.led.close()
        cx = sqlite3.connect(self.path)
        cx.execute("UPDATE meta SET v = ? WHERE k = 'schema_version'",
                   (str(L.SCHEMA_VERSION + 5),))
        cx.commit()
        cx.close()
        with self.assertRaises(L.LedgerError):
            L.Ledger(self.path)

    def test_dump_load_roundtrip(self):
        self._seed()
        before = self.led.stats()
        self.led.dump_jsonl(self._dump_path())

        other = L.Ledger(os.path.join(self.tmp, "dst.db"))
        try:
            res = other.load_jsonl(self._dump_path())
            after = other.stats()
            for key in ("head", "created_seq", "commits", "memories",
                        "clues", "links", "profiles"):
                self.assertEqual(before[key], after[key], key)
            self.assertEqual(res["head"], before["head"])
            self.assertEqual(res["skipped"], 0)
            self.assertTrue(other.recall("空调"), "搬过去之后还得检索得到")
            # 导回来的库要能继续写，父节点接得上才算真接上了
            wrote = other.remember("导入之后写的一条", tags="迁移")
            self.assertTrue(wrote["commit"])
            self.assertEqual(other.stats()["head"], wrote["commit"])
        finally:
            other.close()

    def test_dump_load_keeps_history_after_compact(self):
        for i in range(12):
            self.led.remember("第 %d 条记忆" % i, tags="批量")
        self.led.compact(keep_recent=3)
        head = self.led.head()
        self.led.dump_jsonl(self._dump_path())

        other = L.Ledger(os.path.join(self.tmp, "c.db"))
        try:
            other.load_jsonl(self._dump_path())
            self.assertEqual(other.head(), head)
            self.assertEqual(other.stats()["memories"], self.led.stats()["memories"])
            self.assertEqual(len(other.materialize(head)),
                             len(self.led.materialize(head)),
                             "压缩过的历史搬过去还得回放得出来")
        finally:
            other.close()

    def test_dump_skips_machine_local_meta(self):
        self.led.remember("随便一条")
        self.led.maybe_export_daily(os.path.join(self.tmp, "账本.md"))
        text = self.led.dump_jsonl()
        self.assertIn(L.DUMP_FORMAT, text)
        self.assertNotIn("last_export_date", text,
                         "导出时间戳属于这台机器，不该跟着账本搬走")

    def test_load_refuses_non_empty_db_by_default(self):
        self.led.remember("源库有东西")
        p = self._dump_path()
        self.led.dump_jsonl(p)

        other = L.Ledger(os.path.join(self.tmp, "x.db"))
        try:
            other.remember("目标库也有东西")
            with self.assertRaises(L.LedgerError):
                other.load_jsonl(p)
            self.assertEqual(other.stats()["memories"], 1, "被拒之后目标库不能被弄脏")
            other.load_jsonl(p, replace=True)
            self.assertEqual(other.stats()["memories"], 1)
            self.assertTrue(other.recall("源库"))
        finally:
            other.close()

    def test_load_rejects_foreign_file(self):
        p = os.path.join(self.tmp, "x.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"format": "some-other-tool", "version": 1}\n')
        with self.assertRaises(L.LedgerError):
            self.led.load_jsonl(p)

    def test_load_rejects_newer_dump_version(self):
        self.led.remember("一条")
        p = self._dump_path()
        self.led.dump_jsonl(p)
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r'"version": \d+', '"version": 999', text, count=1)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        with self.assertRaises(L.LedgerError):
            self.led.load_jsonl(p)

    def test_dump_load_roundtrip_access_log(self):
        """P0-1：访问明细是备份的一部分，搬家后近期行为要能原样接上。

        v3 之前 access_log 不进 DUMP_TABLES，dump→load 后明细清零：
        工作记忆复出、近期命中都按时间窗口吃它的行，「整本原样搬家」
        就成了半截账本。这里连 id 都要比对（AUTOINCREMENT 断号也算搬丢）。
        """
        self._seed()
        rows = self.led._query_all("SELECT id FROM memory ORDER BY id")
        m1, m2 = rows[0]["id"], rows[1]["id"]
        self.led.touch_many([m1, m2], query="空调", how="search")
        self.led.touch_many([m1], query="", how="activate")
        # 直插两条跨时间窗口的旧记录，防止「只剩今天」的假往返
        with self.led._tx() as cx:
            cx.execute("INSERT INTO access_log(memory_id, at, seq, query, how)"
                       " VALUES(?,?,?,?,?)",
                       (m2, "2026-01-01T08:00:00", 0, "结算", "recall"))
            cx.execute("INSERT INTO access_log(memory_id, at, seq, query, how)"
                       " VALUES(?,?,?,?,?)",
                       (m1, "2025-12-01T23:30:00", 0, "", "fallback"))
        before = [dict(r) for r in self.led._query_all(
            "SELECT * FROM access_log ORDER BY id")]
        self.led.dump_jsonl(self._dump_path())

        other = L.Ledger(os.path.join(self.tmp, "a.db"))
        try:
            res = other.load_jsonl(self._dump_path())
            self.assertEqual(res["tables"].get("access_log"), len(before))
            after = [dict(r) for r in other._query_all(
                "SELECT * FROM access_log ORDER BY id")]
            self.assertEqual(before, after)
            self.assertEqual(other.stats()["accesses"], len(before))
            hc = other.health_check()
            self.assertTrue(hc["ok"], hc["problems"])
        finally:
            other.close()

    def test_load_v2_backup_without_access_log_still_imports(self):
        """v2 老备份没有 access_log 段，新代码要照常导入（P0-1 兼容面）。

        反方向（旧代码读到 v3 备份）由 test_load_rejects_newer_dump_version
        钉住：文件头 version > DUMP_VERSION 直接拒收，v3 备份喂给只认 v2
        的旧代码走的就是同一条路径。
        """
        self._seed()
        first_id = self.led._query_all(
            "SELECT id FROM memory ORDER BY id")[0]["id"]
        self.led.touch_many([first_id], query="空调", how="search")
        text = self.led.dump_jsonl()
        lines = [ln for ln in text.splitlines() if '"t": "access_log"' not in ln]
        header = json.loads(lines[0])
        self.assertIn("access_log", header["tables"])
        lost = header["tables"].pop("access_log")
        header["version"] = 2
        header["rows_total"] -= lost
        lines[0] = json.dumps(header, ensure_ascii=False, sort_keys=True)
        p = self._dump_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        other = L.Ledger(os.path.join(self.tmp, "v2.db"))
        try:
            res = other.load_jsonl(p)
            self.assertEqual(res["memories"], 2)
            self.assertEqual(other.stats()["accesses"], 0,
                             "老备份本来就没带访问明细，别假装恢复出来了")
            self.assertTrue(other.recall("空调"))
        finally:
            other.close()

    def test_load_drops_unknown_columns(self):
        """备份里带目标库没有的列：默认拒绝（P0-3），显式 allow_lossy 才降级。

        旧行为是静默丢列照报成功，「导入成功」三个字把能力降级盖住了。
        预检必须走在恢复预演之前：不许先建预演库写半截再回头拒绝。
        """
        self.led.remember("一条")
        p = self._dump_path()
        self.led.dump_jsonl(p)
        with open(p, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, ln in enumerate(lines):
            if '"t": "memory"' in ln:
                item = json.loads(ln)
                item["r"]["legacy_field"] = "旧版本留下的"
                lines[i] = json.dumps(item, ensure_ascii=False, sort_keys=True)
                break
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        other = L.Ledger(os.path.join(self.tmp, "u.db"))
        try:
            with self.assertRaises(L.LedgerError) as ctx:
                other.load_jsonl(p)
            msg = str(ctx.exception)
            self.assertIn("memory.legacy_field", msg)
            self.assertIn("allow_lossy=True", msg)
            self.assertEqual(other.stats()["memories"], 0,
                             "预检拒绝后目标库必须一字节没写")
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "u.db.restore-check")),
                             "预检必须走在恢复预演之前，不许先建预演库")
            res = other.load_jsonl(p, allow_lossy=True)
            self.assertEqual(res["memories"], 1)
            self.assertIn("memory.legacy_field", res["dropped_columns"])
        finally:
            other.close()


class AccessLogTest(unittest.TestCase):
    """v2：访问痕迹从提交链里剥出来，单独记在 access_log。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "a.db"))

    def tearDown(self):
        self.led.close()

    def test_schema_is_v5(self):
        """v5：梦境与职守改造，五张新表 + 4 索引（memory 主表不动）。"""
        self.assertEqual(self.led.schema_version(), 5)
        self.assertEqual(L.SCHEMA_VERSION, 5)
        names = {r["name"] for r in self.led._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for tbl in ("memory_condition", "memory_status", "dream_proposal",
                    "memory_source_anchor", "user_denial"):
            self.assertIn(tbl, names)

    def test_message_seq_index_created(self):
        """message 按 seq 排序的查询要有索引兜底，别走全表扫描。"""
        idx = [r["name"] for r in self.led._conn.execute("PRAGMA index_list(message)")]
        self.assertIn("idx_message_seq", idx)

    def test_activate_does_not_grow_commit_chain(self):
        for i in range(3):
            self.led.remember("第 %d 条值得记住的事，关于杯子和猫" % i, tags="测试")
        before = self.led.stats()
        for q in ("杯子", "猫", "值得"):
            self.led.activate(q, slots=3, with_profile=False)
        after = self.led.stats()
        self.assertEqual(after["commits"], before["commits"],
                         "浮现不该产生提交：想起一件事不等于改写一件事")
        self.assertGreater(after["accesses"], 0, "但要留下访问痕迹")

    def test_access_log_records_why(self):
        r = self.led.remember("咖啡馆打烊前要把杯子擦到没有水痕")
        self.led.activate("杯子", slots=1, with_profile=False)
        rows = self.led.access_history(r["memory_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query"], "杯子", "要记住是因为什么被想起来的")
        self.assertTrue(rows[0]["at"])

    def test_access_counters_still_feed_decay(self):
        r = self.led.remember("被反复想起的记忆")
        for _ in range(4):
            self.led.touch(r["memory_id"], query="想起")
        row = self.led._query_one("SELECT access_count, last_access_seq"
                                  " FROM memory WHERE id = ?", (r["memory_id"],))
        self.assertEqual(row["access_count"], 4)
        self.assertGreaterEqual(row["last_access_seq"], 1)

    def test_touch_missing_memory_is_noop(self):
        self.assertEqual(self.led.touch_many(["m999"]), 0)
        self.assertEqual(self.led._query_one(
            "SELECT COUNT(*) AS n FROM access_log")["n"], 0)

    def test_new_memory_is_not_born_stale(self):
        """刚写下的记忆，访问序号必须等于创建序号，否则一出生就该被冲刷。"""
        self.led.remember("刚写完的记忆", importance=0.1)
        row = self.led._query_one("SELECT created_seq, last_access_seq FROM memory")
        self.assertEqual(row["last_access_seq"], row["created_seq"])
        self.assertEqual(self.led.flush_plan(), [], "刚写的记忆不该立刻待冲刷")

    def test_prune_keeps_recent_per_memory(self):
        a = self.led.remember("甲")
        b = self.led.remember("乙")
        for _ in range(12):
            self.led.touch(a["memory_id"], query="甲")
        self.led.touch(b["memory_id"], query="乙")
        dropped = self.led.prune_access_log(keep_days=3650, keep_per_memory=5)
        self.assertGreaterEqual(dropped, 7)
        self.assertEqual(len(self.led.access_history(a["memory_id"], limit=100)), 5,
                         "每条记忆至少留最近几次")
        self.assertEqual(len(self.led.access_history(b["memory_id"], limit=100)), 1,
                         "少于上限的不动")
        row = self.led._query_one("SELECT access_count FROM memory WHERE id = ?",
                                  (a["memory_id"],))
        self.assertEqual(row["access_count"], 12, "修剪日志不该把热度也抹掉")

    def test_replay_survives_missing_state_columns(self):
        self.led.remember("甲记忆", tags="测试")
        self.led.remember("乙记忆", tags="测试")
        self.led.touch_many(["m1", "m2"], query="测试")
        rows = self.led.materialize()
        self.assertEqual([r["id"] for r in rows], ["m1", "m2"])
        for r in rows:
            self.assertIn("access_count", r)
            self.assertIsNotNone(r["last_access_seq"])

    def test_compact_keeps_memories_intact(self):
        for i in range(6):
            self.led.remember("测试压缩的第 %d 条记忆" % i, tags="压缩")
        self.led.touch("m1", query="压缩")
        before = [(r["id"], r["summary"]) for r in self.led.materialize()]
        self.led.compact(keep_recent=2)
        after = [(r["id"], r["summary"]) for r in self.led.materialize()]
        self.assertEqual(before, after, "压缩不该改动记忆内容")
        self.assertTrue(self.led.health_check("")["ok"])

    def test_migration_seeds_access_log(self):
        """把库里已有的访问次数折算成一条 seed，并在 how 里说明来源。"""
        self.led.close()
        path = os.path.join(self.tmp, "old.db")
        cx = sqlite3.connect(path)
        cx.executescript(
            "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);"
            "CREATE TABLE mcommit (id TEXT PRIMARY KEY, parent TEXT,"
            " message TEXT NOT NULL, created_at TEXT NOT NULL);"
            "CREATE TABLE commit_row (commit_id TEXT NOT NULL, seq INTEGER NOT NULL,"
            " tbl TEXT NOT NULL, op TEXT NOT NULL, row_key TEXT NOT NULL,"
            " before_json TEXT, after_json TEXT, PRIMARY KEY (commit_id, seq));"
            "CREATE TABLE memory (id TEXT PRIMARY KEY, prefix TEXT,"
            " level TEXT NOT NULL DEFAULT 'day', grade INTEGER NOT NULL DEFAULT 1,"
            " importance REAL NOT NULL DEFAULT 0.5, summary TEXT NOT NULL,"
            " tags TEXT NOT NULL DEFAULT '', scope_key TEXT NOT NULL DEFAULT '',"
            " start_time TEXT, end_time TEXT, created_seq INTEGER NOT NULL,"
            " last_access_seq INTEGER NOT NULL, access_count INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE clue (id TEXT PRIMARY KEY, content TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL);"
            "CREATE TABLE memory_clue (memory_id TEXT NOT NULL, clue_id TEXT NOT NULL,"
            " weight REAL NOT NULL DEFAULT 1.0, note TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (memory_id, clue_id));"
            "CREATE TABLE memory_link (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " from_id TEXT NOT NULL, to_id TEXT NOT NULL, concept TEXT NOT NULL,"
            " strength REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL);"
            "CREATE TABLE profile (id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
            " fact TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'event',"
            " weight INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);"
            "CREATE TABLE core_rule (id TEXT PRIMARY KEY, content TEXT NOT NULL,"
            " priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);"
        )
        cx.execute("INSERT INTO meta VALUES('head','C1')")
        cx.execute("INSERT INTO meta VALUES('commit_no','1')")
        cx.execute("INSERT INTO meta VALUES('memory_no','1')")
        cx.execute("INSERT INTO meta VALUES('created_seq','3')")
        cx.execute("INSERT INTO mcommit(id, parent, message, created_at)"
                   " VALUES('C1', NULL, '记下：早就有的记忆', '2026-01-01T00:00:00')")
        cx.execute("INSERT INTO memory(id, level, grade, importance, summary,"
                   " created_seq, last_access_seq, access_count, created_at, updated_at)"
                   " VALUES('m1','day',1,0.5,'早就有的记忆',3,3,7,"
                   " '2026-01-01T00:00:00','2026-01-02T00:00:00')")
        cx.commit()
        cx.close()

        led = L.Ledger(path)  # 打开即迁移
        try:
            self.assertEqual(led.schema_version(), 5)
            idx = [r["name"] for r in led._conn.execute("PRAGMA index_list(message)")]
            self.assertIn("idx_message_seq", idx,
                          "迁移到 v4 后应补 message 的 seq 索引")
            rows = led.access_history("m1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["how"], "seed:7")
            self.assertEqual(rows[0]["seq"], 3)
            self.assertEqual(led._query_one(
                "SELECT access_count FROM memory WHERE id = 'm1'")["access_count"], 7)
            self.assertTrue(led.health_check("")["ok"])
        finally:
            led.close()


class WorkingMemoryTest(unittest.TestCase):
    """工作记忆容量：一级有名额，超员挤到零级，被反复想起的升回来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "w.db"))
        self.led.working_capacity = 3

    def tearDown(self):
        self.led.close()

    def _fill(self, n=5, importance=0.5):
        return [self.led.remember("第 %d 条候选记忆，关于杯子和日落" % i,
                                  tags="候选", importance=importance)["memory_id"]
                for i in range(n)]

    def test_plan_keeps_the_clearest_ones(self):
        ids = self._fill(5)
        plan = self.led.plan_working_set()
        self.assertEqual(plan["capacity"], 3)
        self.assertEqual(plan["occupied"], 5)
        self.assertEqual([r["id"] for r in plan["keep"]], ids[2:][::-1],
                         "同样重要的时候，新近碰过的留在名额里")
        self.assertEqual(len(plan["demote"]), 2)

    def test_plan_does_not_write(self):
        self._fill(5)
        head = self.led.head()
        self.led.plan_working_set()
        self.assertEqual(self.led.head(), head, "只算不写的方案不该产生提交")
        self.assertEqual(self.led.stats()["by_grade"]["一级"], 5)

    def test_rebalance_demotes_overflow(self):
        ids = self._fill(5)
        res = self.led.rebalance_working_set(apply=True)
        self.assertTrue(res["applied"])
        self.assertEqual(len(res["demoted"]), 2)
        self.assertEqual(self.led.stats()["by_grade"], {"零级": 2, "一级": 3, "二级": 0})
        self.assertEqual(self.led._query_one(
            "SELECT grade FROM memory WHERE id = ?", (ids[0],))["grade"], 0)
        self.assertTrue(self.led.health_check("")["ok"], "降级也要能回放")

    def test_rebalance_is_stable(self):
        self._fill(5)
        self.led.rebalance_working_set(apply=True)
        head = self.led.head()
        again = self.led.rebalance_working_set(apply=True)
        self.assertFalse(again["applied"], "名额已经合适，不该再动")
        self.assertEqual(self.led.head(), head)

    def test_demote_is_capped_per_round(self):
        self.led.working_capacity = 1
        self._fill(30)
        res = self.led.rebalance_working_set(apply=True)
        self.assertEqual(len(res["demoted"]), L.WORKING_DEMOTE_LIMIT,
                         "一次只降一批，别一条提交里塞两百行快照")
        self.assertEqual(self.led.stats()["by_grade"]["一级"], 30 - L.WORKING_DEMOTE_LIMIT)

    def test_recalled_memory_comes_back(self):
        ids = self._fill(4)
        self.led.rebalance_working_set(apply=True)
        self.assertEqual(self.led._query_one(
            "SELECT grade FROM memory WHERE id = ?", (ids[0],))["grade"], 0)
        for _ in range(L.REVIVE_HITS):
            self.led.touch(ids[0], query="杯子")
        res = self.led.rebalance_working_set(apply=True)
        self.assertEqual(res["promoted"], [ids[0]], "被反复想起的该回到工作记忆")
        self.assertEqual(self.led._query_one(
            "SELECT grade FROM memory WHERE id = ?", (ids[0],))["grade"], 1)
        self.assertEqual(len(res["demoted"]), 1, "腾出来的名额给复出的，别人让位")

    def test_one_recall_is_not_enough(self):
        ids = self._fill(4)
        self.led.rebalance_working_set(apply=True)
        self.led.touch(ids[0], query="杯子")
        res = self.led.rebalance_working_set(apply=True)
        self.assertEqual(res["promoted"], [], "只想起一次还不够格")

    def test_grade_two_does_not_take_a_seat(self):
        for i in range(3):
            self.led.remember("巩固过的第 %d 条" % i, grade=2, importance=0.9)
        keep = self.led.remember("刚进工作记忆的一条", grade=1)["memory_id"]
        self.led.working_capacity = 1
        res = self.led.rebalance_working_set(apply=True)
        self.assertEqual(res["demoted"], [], "二级是巩固层，不该被挤")
        self.assertEqual(self.led._query_one(
            "SELECT grade FROM memory WHERE id = ?", (keep,))["grade"], 1)
        self.assertEqual(self.led.stats()["by_grade"]["二级"], 3)

    def test_capacity_zero_turns_it_off(self):
        self.led.working_capacity = 0
        self._fill(6)
        head = self.led.head()
        res = self.led.rebalance_working_set(apply=True)
        self.assertFalse(res["applied"])
        self.assertEqual(self.led.head(), head)
        self.assertEqual(self.led.stats()["by_grade"]["一级"], 6)

    def test_maybe_rebalance_respects_cooldown(self):
        self._fill(5)
        first = self.led.maybe_rebalance_working_set(cooldown=3600)
        self.assertTrue(first["applied"])
        second = self.led.maybe_rebalance_working_set(cooldown=3600)
        self.assertFalse(second["applied"])
        self.assertIn("冷却", second["reason"])

    def test_sunk_memory_does_not_surface_first(self):
        a = self.led.remember("沉下去的那条，关于杯子", importance=0.5)
        b = self.led.remember("还在一级的那条，关于杯子", importance=0.5)
        with self.led.commit("手动降级") as c:
            c.set_grade(a["memory_id"], 0)
        rows = self.led._fresh_candidates(limit=5)
        self.assertEqual(rows[0]["id"], b["memory_id"], "零级不该抢话")
        sunk = [r for r in rows if r["id"] == a["memory_id"]][0]
        self.assertIn("沉下去了", sunk["how"])

    # ---------- P1：自动浮现不算「最近被想起」，别动 last_access_seq ----------

    def test_activate_touch_does_not_move_last_access(self):
        """activate 只是机器把记忆捞出来念，不更新 last_access_seq。

        否则每轮自动提及（含长 prompt 置空 query 后走 activate）都等于给记忆
        续上「刚被想起」的标记，工作记忆换班会被悄悄干扰、可以永不换班。
        """
        ids = self._fill(5)
        m1 = ids[0]
        # 复现场景：m1 是很久没被碰过的，last_access_seq 停在 1
        self.led._conn.execute(
            "UPDATE memory SET last_access_seq=1 WHERE id=?", (m1,))
        self.led._conn.commit()
        plan_before = self.led.plan_working_set()
        self.assertIn(m1, [r["id"] for r in plan_before["demote"]],
                      "久没被想起的该在降级名单")
        self.led.touch_many([m1], query="", how="activate")
        row = self.led._query_one(
            "SELECT last_access_seq, access_count FROM memory WHERE id=?",
            (m1,))
        self.assertEqual(row["last_access_seq"], 1,
                         "自动提及不该把 last_access_seq 拨到当下")
        self.assertEqual(row["access_count"], 0,
                         "自动提及也不算一次内容访问")
        plan_after = self.led.plan_working_set()
        self.assertIn(m1, [r["id"] for r in plan_after["demote"]],
                      "activate 不该把该降级的救回 keep")
        self.assertEqual([r["id"] for r in plan_before["keep"]],
                         [r["id"] for r in plan_after["keep"]],
                         "activate 不该把 keep 里的名额挤出去")

    def test_search_touch_moves_last_access(self):
        """真检索 search 照常更新 last_access_seq，别一刀切把真检索也砍了。"""
        r = self.led.remember("关于杯子能检索到")
        self.led.touch_many([r["memory_id"]], query="杯子", how="search")
        row = self.led._query_one(
            "SELECT last_access_seq FROM memory WHERE id=?",
            (r["memory_id"],))
        self.assertEqual(row["last_access_seq"], self.led.max_created_seq(),
                         "真实检索想起的要更新 last_access_seq")


class MessageChainTest(unittest.TestCase):
    """v3：原文链。一条消息一个节点，靠 digest 串起来，不进提交链。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "m.db"))

    def tearDown(self):
        self.led.close()

    def test_append_and_search(self):
        self.led.append_message("user", "用户说今天风很大", "s1")
        self.led.append_message("assistant", "是呀，我把尾巴卷起来了", "s1")
        self.led.append_message("user", "原话链能查吗", "s1")
        rows = self.led.search_messages("风很大")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "user")
        self.assertEqual(rows[0]["seq"], 1)

    def test_no_query_gives_latest(self):
        for i in range(5):
            self.led.append_message("user", "第 %d 句" % i, "s1")
        rows = self.led.search_messages("", limit=2)
        self.assertEqual([r["content"] for r in rows], ["第 4 句", "第 3 句"])

    def test_empty_content_is_ignored(self):
        self.assertEqual(self.led.append_message("user", "   ", "s1"), {})
        self.assertEqual(self.led.message_stats()["count"], 0)

    def test_does_not_grow_commit_chain(self):
        before = self.led.stats()["commits"]
        for i in range(5):
            self.led.append_message("user", "闲聊第 %d 句" % i, "s1")
        self.assertEqual(self.led.stats()["commits"], before,
                         "原文链不该往提交链里塞流水账")

    def test_chain_detects_tampering(self):
        self.led.append_message("user", "第一句", "s1")
        self.led.append_message("user", "第二句", "s1")
        self.assertTrue(self.led.verify_message_chain()["ok"])
        self.led._conn.execute("UPDATE message SET content='改过的' WHERE id='t00002'")
        self.led._conn.commit()
        bad = self.led.verify_message_chain()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["broken_at"], "t00002")

    def test_ledger_writes_still_work(self):
        """加表不该影响原来那套：记一条、查得到、链没坏。"""
        res = self.led.remember("杯子要擦到没有水痕", tags="习惯")
        self.assertEqual(self.led.recall("水痕")[0]["id"], res["memory_id"])
        self.assertTrue(self.led.health_check("")["ok"])

    # ---------- verify_message_chain 增强 ----------

    def test_verify_detects_deleted_last_one(self):
        """删最后一条：digest 链不断，但 meta.message_no / message_head 对不上。"""
        r1 = self.led.append_message("user", "第一句", "s1")
        r2 = self.led.append_message("user", "第二句", "s1")
        r3 = self.led.append_message("user", "第三句", "s1")
        self.assertTrue(self.led.verify_message_chain()["ok"])
        self.led._conn.execute("DELETE FROM message WHERE id=?", (r3["id"],))
        self.led._conn.commit()
        res = self.led.verify_message_chain()
        self.assertFalse(res["ok"])
        self.assertEqual(res["expected_count"], 3, "meta.message_no 还记着 3")
        self.assertEqual(res["actual_count"], 2, "表里只剩 2 条")
        self.assertTrue(res["reason"])

    def test_verify_detects_deleted_last_many(self):
        """删掉最后多条：尾部被抹掉也能查出来。"""
        for i in range(4):
            self.led.append_message("user", "第%d句" % i, "s1")
        self.led._conn.execute("DELETE FROM message WHERE seq>1")
        self.led._conn.commit()
        res = self.led.verify_message_chain()
        self.assertFalse(res["ok"])
        self.assertEqual(res["expected_count"], 4)
        self.assertEqual(res["actual_count"], 1)

    def test_verify_detects_middle_gap(self):
        """删中间一条：seq 出现空洞、prev_id 断裂。"""
        r1 = self.led.append_message("user", "第一句", "s1")
        r2 = self.led.append_message("user", "第二句", "s1")
        r3 = self.led.append_message("user", "第三句", "s1")
        self.led._conn.execute("DELETE FROM message WHERE id=?", (r2["id"],))
        self.led._conn.commit()
        res = self.led.verify_message_chain()
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"])

    def test_verify_detects_head_tamper(self):
        """meta.message_head 被改：链尾对不上。"""
        self.led.append_message("user", "第一句", "s1")
        self.led.append_message("user", "第二句", "s1")
        self.assertTrue(self.led.verify_message_chain()["ok"])
        self.led._conn.execute("UPDATE meta SET v='deadbeef' WHERE k='message_head'")
        self.led._conn.commit()
        res = self.led.verify_message_chain()
        self.assertFalse(res["ok"])
        self.assertNotEqual(res["expected_head"], res["actual_head"])

    def test_verify_empty_table_is_ok(self):
        res = self.led.verify_message_chain()
        self.assertTrue(res["ok"])
        self.assertEqual(res["expected_count"], 0)
        self.assertEqual(res["actual_count"], 0)
        self.assertEqual(res["expected_head"], res["actual_head"])

    # ---------- search_messages 改走 BM25 ----------

    def test_search_short_exact_beats_long_incidental(self):
        """短消息精确命中要排在长消息顺带命中之前。"""
        self.led.append_message("user", "风很大", "s1")
        self.led.append_message(
            "user", "今天风很大，用户把尾巴卷起来躲风，顺便聊了很久的天气", "s1")
        rows = self.led.search_messages("风很大")
        self.assertEqual(rows[0]["content"], "风很大")

    def test_search_bm25_unknown_word_empty(self):
        self.led.append_message("user", "今天风很大", "s1")
        self.assertEqual(self.led.search_messages("量子计算机"), [])

    def test_search_single_char_nonstopword(self):
        self.led.append_message("user", "今天风很大", "s1")
        self.led.append_message("user", "记得带伞", "s1")
        rows = self.led.search_messages("风")
        self.assertEqual([r["content"] for r in rows], ["今天风很大"])

    def test_search_single_char_stopword_fallback(self):
        """单字停用词（"的"）切不出 token 时退回子串兜底。"""
        self.led.append_message("user", "用户的伞", "s1")
        self.led.append_message("user", "今天风很大", "s1")
        rows = self.led.search_messages("的")
        self.assertEqual([r["content"] for r in rows], ["用户的伞"])

    def test_search_candidate_cap_finds_recent(self):
        """候选被截断：只对最近 SEARCH_CANDIDATE_CAP 条做检索，最近的消息能命中。"""
        for i in range(L.SEARCH_CANDIDATE_CAP + 50):
            self.led.append_message("user", "占位消息 %d" % i, "s1")
        self.led.append_message("user", "真正要找的关键词在这", "s1")
        rows = self.led.search_messages("关键词")
        self.assertEqual([r["content"] for r in rows], ["真正要找的关键词在这"])

    def test_search_candidate_cap_sort_still_correct(self):
        """候选被截断时，命中结果的排序仍正确（同分按 seq 倒序，新的在前）。"""
        for i in range(L.SEARCH_CANDIDATE_CAP + 10):
            self.led.append_message("user", "普通句子 %d" % i, "s1")
        self.led.append_message("user", "要查的旧句", "s1")
        self.led.append_message("user", "要查的新句", "s1")
        rows = self.led.search_messages("要查的")
        self.assertEqual([r["content"] for r in rows][:2], ["要查的新句", "要查的旧句"],
                         "同分时新的排在前，排序语义不变")

    def test_search_candidate_cap_single_char_fallback(self):
        """候选截断后，单字兜底（子串命中）仍可用。"""
        for i in range(L.SEARCH_CANDIDATE_CAP + 10):
            self.led.append_message("user", "普通句 %d" % i, "s1")
        self.led.append_message("user", "这里有个辣", "s1")
        rows = self.led.search_messages("辣")
        self.assertEqual([r["content"] for r in rows], ["这里有个辣"])



def _ensure_astrbot_stub():
    """main.py 依赖 astrbot.api，本机没装；这里塞一个最小假模块，够 import 就行。"""
    if "astrbot" in sys.modules:
        return
    api = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")

    logger_mod = types.ModuleType("astrbot.api.logger")
    logger_mod.info = lambda *a, **k: None
    logger_mod.warning = lambda *a, **k: None

    class _FakeEvent:
        pass

    class _FakeFilter:
        def command(self, *a, **k):
            return lambda fn: fn
        def on_llm_request(self, *a, **k):
            return lambda fn: fn
        def on_llm_response(self, *a, **k):
            return lambda fn: fn
        def llm_tool(self, *a, **k):
            return lambda fn: fn

    class _Context:
        pass

    class _Star:
        def __init__(self, context):
            pass

    def _register(*a, **k):
        return lambda cls: cls

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = _FakeEvent
    event_mod.filter = _FakeFilter()
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = _Context
    star_mod.Star = _Star
    star_mod.register = _register

    api_mod.event = event_mod
    api_mod.logger = logger_mod
    api_mod.star = star_mod
    api.event = event_mod
    api.logger = logger_mod
    api.star = star_mod

    sys.modules["astrbot"] = api
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.logger"] = logger_mod
    sys.modules["astrbot.api.star"] = star_mod


def _load_main_module(tmpdir):
    """用独立模块名加载插件外壳 main.py（相对导入会失败，正好走它的 `from ledger import` 兜底）。"""
    _ensure_astrbot_stub()
    os.environ["JJ_MEMORY_DIR"] = tmpdir
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_memory_commit_main", os.path.join(root, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_memory_commit_main"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeEvent:
    unified_msg_origin = "123:FriendMessage:1000000000"

    def get_message_str(self):
        return "今天聊到哪了"


class _FakeReq:
    def __init__(self, prompt):
        self.prompt = prompt
        self.system_prompt = ""


class InjectMemoryTest(unittest.TestCase):
    """【1】inject_memory：把「检索词」和「执行流程」解耦。不依赖 AstrBot。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mod = _load_main_module(self.tmp)
        # 默认不开放任何会话，测试里显式授权一条，不依赖代码里的默认值
        self.mod.OWNER_UMOS = frozenset({"123:FriendMessage:1000000000"})
        self.plugin = self.mod.MemoryCommitPlugin(context=None)

    def _run(self, prompt):
        req = _FakeReq(prompt)
        with mock.patch.object(self.plugin.ledger, "maybe_export_daily") as m_exp, \
             mock.patch.object(self.plugin.ledger, "maybe_rebalance_working_set") as m_reb, \
             mock.patch.object(self.plugin.ledger, "activate",
                               return_value={"items": [], "profiles": [],
                                             "flashes": [], "route": "activate",
                                             "query": ""}) as m_act, \
             mock.patch.object(self.plugin.ledger, "render_brief", return_value="") as m_brief:
            asyncio.run(self.plugin.inject_memory(_FakeEvent(), req))
        return m_exp, m_reb, m_act, m_brief

    def test_long_prompt_still_runs_pipeline(self):
        """400 字 prompt：导出、换班、唤起、渲染都要照常跑，且不能拿长文本做词面检索。"""
        m_exp, m_reb, m_act, m_brief = self._run("亲" * 400)
        self.assertTrue(m_exp.called, "每日导出应仍被调用")
        self.assertTrue(m_reb.called, "工作记忆换班应仍被调用")
        self.assertTrue(m_act.called, "activate 应仍被调用")
        self.assertEqual(m_act.call_args[0][0], "今天聊到哪了",
                         "长 prompt 不该拿整段当检索词，但要退回用户原话 said")
        self.assertTrue(m_brief.called, "render_brief 应仍被调用")

    def test_300_boundary_uses_query(self):
        """300 字：仍当检索词用。"""
        m_exp, m_reb, m_act, m_brief = self._run("亲" * 300)
        self.assertTrue(m_act.called)
        self.assertEqual(m_act.call_args[0][0], "亲" * 300)

    def test_301_boundary_uses_said(self):
        """301 字：超长，query 用用户原话 said 当检索词（而非直接置空）。"""
        m_exp, m_reb, m_act, m_brief = self._run("亲" * 301)
        self.assertTrue(m_act.called)
        self.assertEqual(m_act.call_args[0][0], "今天聊到哪了",
                         ">300 边界语义不变：该用 said 当检索词")

    def test_long_prompt_uses_said_as_query(self):
        """prompt 超长但 said 有内容：activate 收到的检索词是 said，不是空。"""
        m_exp, m_reb, m_act, m_brief = self._run("亲" * 400)
        self.assertTrue(m_act.called)
        self.assertEqual(m_act.call_args[0][0], "今天聊到哪了",
                         "长 prompt 应退回用户原话做词面检索")

    def test_maintain_throttled_after_first(self):
        """维护任务首轮查一次，之后按 MAINTAIN_EVERY 节流，不是每轮白跑两趟。"""
        def run_once():
            with mock.patch.object(self.plugin.ledger, "maybe_export_daily") as m_exp, \
                 mock.patch.object(self.plugin.ledger, "maybe_rebalance_working_set"), \
                 mock.patch.object(self.plugin.ledger, "activate",
                                   return_value={"items": [], "profiles": [],
                                                 "flashes": [], "route": "activate",
                                                 "query": ""}), \
                 mock.patch.object(self.plugin.ledger, "render_brief", return_value=""):
                asyncio.run(self.plugin.inject_memory(_FakeEvent(), _FakeReq("事很简单")))
            return m_exp

        m_exp = run_once()
        self.assertEqual(m_exp.call_count, 1, "首轮要检查一次维护任务")
        m_exp = run_once()
        self.assertEqual(m_exp.call_count, 0, "紧接着的一轮不该再查")
        for _ in range(self.mod.MAINTAIN_EVERY - 2):
            run_once()
        m_exp = run_once()
        self.assertEqual(m_exp.call_count, 1, "到了节流周期要再查一次")

    def test_long_prompt_empty_said_stays_empty(self):
        """prompt 超长且 said 也为空：query 才真正置空。"""
        class _EmptyEvent:
            unified_msg_origin = "123:FriendMessage:1000000000"

            def get_message_str(self):
                return ""
        req = _FakeReq("亲" * 400)
        with mock.patch.object(self.plugin.ledger, "maybe_export_daily") as m_exp, \
             mock.patch.object(self.plugin.ledger, "maybe_rebalance_working_set") as m_reb, \
             mock.patch.object(self.plugin.ledger, "activate",
                               return_value={"items": [], "profiles": [],
                                             "flashes": [], "route": "activate",
                                             "query": ""}) as m_act, \
             mock.patch.object(self.plugin.ledger, "render_brief", return_value="") as m_brief:
            asyncio.run(self.plugin.inject_memory(_EmptyEvent(), req))
        self.assertEqual(m_act.call_args[0][0], "", "said 为空时才真正置空")

    def _read_inject_log(self):
        path = os.path.join(self.tmp, "inject-log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_long_prompt_logs_one_row_with_flag(self):
        """长 prompt 一次请求只记一行：带 long_prompt=true 和 prompt_len，不翻倍。"""
        req = _FakeReq("亲" * 400)
        with mock.patch.object(self.plugin.ledger, "maybe_export_daily"), \
             mock.patch.object(self.plugin.ledger, "maybe_rebalance_working_set"), \
             mock.patch.object(self.plugin.ledger, "activate",
                               return_value={"items": [], "profiles": [],
                                             "flashes": [], "route": "activate",
                                             "query": ""}), \
             mock.patch.object(self.plugin.ledger, "render_brief", return_value=""):
            asyncio.run(self.plugin.inject_memory(_FakeEvent(), req))
        rows = self._read_inject_log()
        self.assertEqual(len(rows), 1, "长 prompt 不该记两行，统计会翻倍")
        self.assertTrue(rows[0]["long_prompt"])
        self.assertEqual(rows[0]["prompt_len"], 400)

    def test_short_prompt_no_long_flag(self):
        """短 prompt 不标 long_prompt。"""
        req = _FakeReq("事很简单")
        with mock.patch.object(self.plugin.ledger, "maybe_export_daily"), \
             mock.patch.object(self.plugin.ledger, "maybe_rebalance_working_set"), \
             mock.patch.object(self.plugin.ledger, "activate",
                             return_value={"items": [], "profiles": [],
                                           "flashes": [], "route": "activate",
                                           "query": ""}), \
             mock.patch.object(self.plugin.ledger, "render_brief", return_value=""):
            asyncio.run(self.plugin.inject_memory(_FakeEvent(), req))
        rows = self._read_inject_log()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("long_prompt", False), "短 prompt 不该带 long_prompt")

    def test_inject_log_rotates_after_5000(self):
        """超过阈值把当前文件轮转成 inject-log.1.jsonl，再重新开一份。"""
        self.plugin._inject_lines = self.mod.INJECT_LOG_ROTATE_LINES - 1
        for _ in range(3):
            self.plugin._log_injection("no_hit")
        new = os.path.join(self.tmp, "inject-log.jsonl")
        backup = os.path.join(self.tmp, "inject-log.1.jsonl")
        self.assertTrue(os.path.exists(new), "轮转后新文件应重新开")
        self.assertTrue(os.path.exists(backup), "旧文件应切成 .1")
        self.assertLessEqual(self.plugin._inject_lines, 2, "轮转后计数应归零重计")


class RepairFixesTest(unittest.TestCase):
    """这次检修的回归测试：兜底池、路由标记、搜索扩窗、自检原文链。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "r.db"))

    def tearDown(self):
        self.led.close()

    # ---------- 问题1：兜底候选池给大，避免第一名进冷却就整轮空转 ----------

    def test_fallback_pool_finds_non_cooldown(self):
        """候选池加大后，第一名刚冷却也能从别的候选捞到，不空转。"""
        for i in range(4):
            self.led.remember("候选 %d 值得记，关于杯子和日落" % i, importance=0.6)
        first = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertEqual(first["route"], "fallback")
        self.assertTrue(first["items"])
        second = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertNotEqual(second["route"], "none", "第一名冷却不该让整轮空转")
        self.assertTrue(second["items"])

    # ---------- 问题2：进兜底就无条件记路线；被挡记 fallback_blocked ----------

    def test_fallback_blocked_distinct_from_none(self):
        """单条记忆且它在冷却：route 要记 fallback_blocked，不是 none。"""
        self.led.remember("只有这一条，刚浮现过", importance=0.6)
        first = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertEqual(first["route"], "fallback")
        second = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertEqual(second["route"], "fallback_blocked")
        self.assertEqual(second["items"], [])

    # ---------- 问题3：兜底/激活不算被想起，只有真检索/手动才算 ----------

    def test_fallback_touch_not_counted_as_hit(self):
        r = self.led.remember("杯子要擦干净", importance=0.6)
        res = self.led.activate("量子色动力学", slots=3, with_profile=False)
        self.assertEqual(res["route"], "fallback")
        rows = self.led.access_history(r["memory_id"])
        self.assertEqual([x["how"] for x in rows], ["fallback"])
        self.assertEqual(self.led._recent_hits().get(r["memory_id"], 0), 0,
                         "兜底只是机器把记忆捞出来念，不算被想起")

    def test_search_touch_counted_in_recent_hits(self):
        r = self.led.remember("端口又冲突了", tags="端口")
        self.led.activate("端口", slots=1, with_profile=False)
        rows = self.led.access_history(r["memory_id"])
        self.assertEqual(rows[0]["how"], "search", "检索想起的该记 search")
        self.assertGreaterEqual(self.led._recent_hits().get(r["memory_id"], 0), 1)

    def test_manual_touch_counted_in_recent_hits(self):
        r = self.led.remember("手动标记的记忆")
        for _ in range(L.REVIVE_HITS):
            self.led.touch(r["memory_id"], query="杯子")
        self.assertEqual(self.led._recent_hits().get(r["memory_id"], 0), L.REVIVE_HITS,
                         "手动 touch 默认 how=manual，算内容被想起")

    # ---------- 问题5：search 无命中扩窗重试；返回 scanned/truncated ----------

    def test_search_expands_window_to_find_old(self):
        # 独有词先落链（seq 1），再用占位把它埋到 200 条窗口之外，
        # 只有扩窗重试才翻得回来——这正是「2 万行库里 seq=100 的词查不到」的还原。
        self.led.append_message("user", "独角鲸", "s1")
        for i in range(1999):
            self.led.append_message("user", "占位消息 %d" % i, "s1")
        res = self.led.search_messages("独角鲸", return_meta=True)
        self.assertEqual([r["content"] for r in res["rows"]], ["独角鲸"])
        self.assertTrue(res["expanded"], "独有词埋在 200 条之外该触发扩窗")
        self.assertFalse(res["truncated"], "扩窗 2000 已覆盖全库，不算截断")
        self.assertEqual(res["total"], 2000)
        self.assertEqual(res["scanned"], 2000)

    def test_search_truncated_reports(self):
        self.led.append_message("user", "deepseek老词", "s1")
        for i in range(2500):
            self.led.append_message("user", "占位消息 %d" % i, "s1")
        res = self.led.search_messages("deepseek", return_meta=True)
        self.assertEqual(res["rows"], [])
        self.assertTrue(res["truncated"], "独有词埋在 2000 条之外要报截断")
        self.assertEqual(res["scanned"], 2000)
        self.assertGreater(res["total"], 2000)

    def test_search_return_meta_backcompat_list(self):
        self.led.append_message("user", "风很大", "s1")
        rows = self.led.search_messages("风很大")
        self.assertIsInstance(rows, list, "不带 return_meta 时仍返回 list")
        self.assertEqual([r["content"] for r in rows], ["风很大"])

    # ---------- 问题8：自检能看到原文链 ----------

    def test_health_check_reports_message_chain(self):
        self.led.append_message("user", "第一句", "s1")
        self.led.append_message("user", "第二句", "s1")
        res = self.led.health_check()
        self.assertTrue(res["ok"])
        chain = res["message_chain"]
        self.assertTrue(chain["ok"])
        self.assertEqual(chain["count"], 2)

    def test_health_check_catches_broken_chain(self):
        self.led.append_message("user", "第一句", "s1")
        self.led.append_message("user", "第二句", "s1")
        self.led._conn.execute("UPDATE message SET content='改过的' WHERE id='t00002'")
        self.led._conn.commit()
        res = self.led.health_check()
        self.assertFalse(res["ok"])
        self.assertTrue(any("原文链" in p for p in res["problems"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)


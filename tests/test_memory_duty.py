# -*- coding: utf-8 -*-
"""memory 职守的自测（v5 · 施工单 §1.5 / §3.6 / §5.1）。

memory 职守 = 记下、检索、衰减、处置，常开。这里钉住的是：
- 咬合点只有两个（propose_clues / trace_to_source），没有多余的公开写路径
- 用户否认信号（deny_memory）的效应：立刻降权、停止主动提起、不删除
- 关键词命中只进待确认队列，不自动改状态
- 增量原文锚：有锚接得回原文，没锚返回空，不许编
- 矛盾四分类器：只分类不判决；condition 为空的矛盾对是 conflicted 不是 corrected
- coexist 条目在条件不匹配的检索轮次里不主动浮现
"""

import importlib.util
import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ledger as L  # noqa: E402
except ModuleNotFoundError:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _spec = importlib.util.spec_from_file_location("ledger", os.path.join(_root, "Ledger.py"))
    L = importlib.util.module_from_spec(_spec)
    sys.modules["ledger"] = L
    _spec.loader.exec_module(L)


# Ledger 公开方法冻结清单（2026-11-12 v5 改造后实测 54 个）。
# 契约测试的作用就是钉住「三件事各干各的、不串味」（施工单 §1.1）：
# 任何人想再加公开方法（尤其写路径），必须先改这个清单并过评审。
# 2026-11-12 P0 修复新增 3 个，全部只读/库维护，都不是记忆写路径：
#   recent_messages（读原文链，供自动沉淀）、wal_bytes（读文件大小）、
#   maybe_checkpoint_wal（PRAGMA wal_checkpoint，不碰提交链与任何投影表）。
# 2026-11-13 R3 语义修正新增 1 个，只读：
#   disputed_ids（disputed 档的具名集合，denied_ids 同一事实的 R3 命名；
#   quiet_ids 原地扩展可选 context 参数，方法名不变不进清单）。
LEDGER_PUBLIC_METHODS = frozenset({
    "access_history", "activate", "anchor_memory", "append_message",
    "classify_pair", "close", "commit", "compact", "current_status",
    "denial_history", "denied_ids", "deny_memory", "deny_revoke", "disputed_ids",
    "dream_add_memory",
    "dream_apply", "dream_produced_ids", "dump_jsonl", "export_markdown",
    "flush", "flush_plan", "forget", "head", "health_check", "history",
    "load_jsonl", "materialize", "max_created_seq", "maybe_checkpoint_wal",
    "maybe_export_daily", "maybe_rebalance_working_set", "message_stats",
    "nominate_node", "pending_denial_queue", "plan_working_set", "profiles",
    "propose_clues", "prune_access_log", "queue_denial_suspect", "quiet_ids",
    "rebalance_working_set", "recall", "recent_messages", "remember",
    "remember_profile", "render_brief", "schema_version", "search_messages",
    "set_memory_condition", "set_memory_status", "show_commit", "stats",
    "status_timeline", "touch", "touch_many", "trace_to_source",
    "verify_message_chain", "wal_bytes", "working_set",
})


class MemoryDutyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    # ---------- 咬合点（施工单 §1.3 / §1.5）----------

    def test_public_api_frozen_no_extra_write_path(self):
        """memory 对外没有多余的公开写路径：方法清单被钉死。"""
        actual = {n for n, _ in inspect.getmembers(L.Ledger, predicate=inspect.isfunction)
                  if not n.startswith("_")}
        self.assertEqual(actual, set(LEDGER_PUBLIC_METHODS),
                         "Ledger 公开方法变了：要么是误加的写路径，要么该走评审更新冻结清单")

    def test_bite_points_are_readonly(self):
        """两个咬合点都是只读的：调用前后提交数、库内容不变。"""
        self.led.remember("用户怕冷", tags=["用户"])
        self.led.append_message("user", "我怕冷，空调别开太低")
        before_commits = self.led.stats()["commits"]
        before_stats = self.led.stats()["memories"]
        cands = self.led.propose_clues("怕冷", limit=5)
        self.assertIsInstance(cands, list)
        src = self.led.trace_to_source("m1")
        self.assertEqual(src, [], "没有锚就返回空，不许编")
        self.assertEqual(self.led.stats()["commits"], before_commits)
        self.assertEqual(self.led.stats()["memories"], before_stats)

    def test_no_update_summary_method(self):
        """不存在任何更新 summary 的公开方法（施工单 §2.2 的结构保证）。"""
        bad = re.compile(r"(update|rewrite|revise|replace).*summary|summary.*(update|rewrite|revise)")
        names = [n for n, _ in inspect.getmembers(L.Ledger, predicate=inspect.isfunction)
                 if not n.startswith("_")]
        self.assertEqual([n for n in names if bad.search(n)], [])

    # ---------- 用户否认（施工单 §5.1）----------

    def test_deny_memory_marks_disputed_and_keeps_row(self):
        mid = self.led.remember("用户说要在公司保持安静", tags=["偏好"])["memory_id"]
        res = self.led.deny_memory(mid, "你记错了，我没说过这话")
        self.assertEqual(res["status"], "disputed")
        self.assertEqual(self.led.current_status(mid), "disputed")
        # 不删除：否认不等于删，原文还在
        row = self.led._query_one("SELECT * FROM memory WHERE id = ?", (mid,))
        self.assertIsNotNone(row)
        self.assertEqual(row["summary"], "用户说要在公司保持安静")
        # user_denial 留了原话
        d = self.led.denial_history(mid)
        self.assertEqual(len(d), 1)
        self.assertIn("记错了", d[0]["said"])

    def test_deny_memory_drops_recall_weight(self):
        self.led.remember("用户最爱喝冰美式", tags=["咖啡"], importance=0.9)
        mid = self.led.remember("用户说要戒咖啡", tags=["咖啡"])["memory_id"]
        before = {h["id"]: h for h in self.led.recall("咖啡", touch=False)}
        self.assertIn(mid, before)
        self.led.deny_memory(mid, "这不是真的")
        after = {h["id"]: h for h in self.led.recall("咖啡", touch=False)}
        if mid in after:
            self.assertLess(after[mid]["score"], before[mid]["score"],
                            "被否认的记忆必须立刻降检索权重")
            self.assertTrue(after[mid].get("disputed"))
        else:
            # 降权后跌破 min_score 被过滤也算降权生效，但至少还能被否认集合查到
            self.assertIn(mid, self.led.denied_ids())

    def test_denied_memory_not_actively_surfaced(self):
        mid = self.led.remember("用户喜欢周末爬山", tags=["周末"])["memory_id"]
        fresh = [r["id"] for r in self.led._fresh_candidates(20)]
        self.assertIn(mid, fresh)
        self.led.deny_memory(mid, "我从来没爬过山")
        fresh = [r["id"] for r in self.led._fresh_candidates(20)]
        self.assertNotIn(mid, fresh, "被否认的记忆立刻停止主动提起")

    def test_deny_requires_existing_memory_and_said(self):
        with self.assertRaises(L.LedgerError):
            self.led.deny_memory("m999", "不存在的否认")
        mid = self.led.remember("一条待否认的记忆")["memory_id"]
        with self.assertRaises(L.LedgerError):
            self.led.deny_memory(mid, "   ")

    # ---------- 关键词命中 → 待确认队列（施工单 §5.1）----------

    def test_keyword_hit_only_queues_never_changes_status(self):
        hits = L.match_denial_keywords("你记错了，那天我没说过要喝酒")
        self.assertIn("记错了", hits)
        res = self.led.queue_denial_suspect("你记错了，那天我没说过要喝酒", keyword=hits[0])
        self.assertEqual(res["status"], "pending")
        queue = self.led.pending_denial_queue()
        self.assertEqual(len(queue), 1)
        payload = json.loads(queue[0]["payload_json"])
        self.assertIn("记错了", payload["said"])
        # 没指到具体记忆，不自动改任何记忆的状态
        self.assertEqual(self.led._query_one("SELECT COUNT(*) AS n FROM memory_status")["n"], 0)

    def test_denial_keywords_cover_manifesto(self):
        for kw in ("记错了", "不是这样", "没这回事", "我没说过"):
            self.assertTrue(L.match_denial_keywords("其实%s，你别乱记" % kw))
        self.assertEqual(L.match_denial_keywords("今天天气不错"), [])

    # ---------- 增量原文锚（施工单 §5 第 3 步）----------

    def test_remember_with_anchor_traces_to_source(self):
        self.led.append_message("user", "我这周都在准备答辩，别安排别的")
        seq = self.led._query_one("SELECT MAX(seq) AS s FROM message")["s"]
        mid = self.led.remember("用户本周在准备答辩", anchor_seq=seq)["memory_id"]
        src = self.led.trace_to_source(mid)
        self.assertEqual(len(src), 1)
        self.assertIn("准备答辩", src[0]["content"])
        self.assertEqual(src[0]["seq"], seq)

    def test_trace_to_source_returns_empty_when_no_anchor(self):
        mid = self.led.remember("一条没有锚的记忆")["memory_id"]
        self.assertEqual(self.led.trace_to_source(mid), [])
        # 编不了假锚：序号必须 > 0
        with self.assertRaises(L.LedgerError):
            self.led.anchor_memory(mid, 0)
        with self.assertRaises(L.LedgerError):
            self.led.anchor_memory("m999", 3)

    def test_anchor_and_memory_are_same_commit_atomic(self):
        """锚和记忆在同一事务：锚失败（非法序号）记忆也不落库。"""
        with self.assertRaises(L.LedgerError):
            self.led.remember("这条会因为锚失败整体回滚", anchor_seq=-1)
        self.assertEqual(self.led.stats()["memories"], 0)

    # ---------- 矛盾四分类（施工单 §3.2 / §3.6）----------

    def _pair(self, a_text, b_text, a_cond=None, b_cond=None):
        a = self.led.remember(a_text)["memory_id"]
        b = self.led.remember(b_text)["memory_id"]
        if a_cond:
            self.led.set_memory_condition(a, a_cond)
        if b_cond:
            self.led.set_memory_condition(b, b_cond)
        return a, b

    def test_classifier_temporal_two_samples(self):
        """时变：冷天喝热的 / 热天喝冷的——都对，不是幻觉。"""
        w1 = json.dumps({"window": {"from": "2026-11-01", "to": "2026-11-05"}, "env": {"temp": "cold"}})
        w2 = json.dumps({"window": {"from": "2026-11-20", "to": "2026-11-25"}, "env": {"temp": "hot"}})
        a, b = self._pair("天冷时要喝热的", "天热时要喝冰的", w1, w2)
        self.assertEqual(L.classify_conflict("天冷时要喝热的", "天热时要喝冰的", w1, w2),
                         L.CONFLICT_TEMPORAL)
        res = self.led.classify_pair(a, b)
        self.assertEqual(res["kind"], L.CONFLICT_TEMPORAL)
        self.assertEqual(res["status"], "coexist")
        self.assertEqual(self.led.current_status(a), "coexist")
        self.assertEqual(self.led.current_status(b), "coexist")

    def test_classifier_temporal_second_sample(self):
        w1 = json.dumps({"window": {"from": "2026-01-01", "to": "2026-02-01"}})
        w2 = json.dumps({"window": {"from": "2026-06-01", "to": "2026-06-30"}})
        got = L.classify_conflict("冬天早上七点起床", "夏天早上九点才起", w1, w2)
        self.assertEqual(got, L.CONFLICT_TEMPORAL)

    def test_classifier_scene_two_samples(self):
        """场景依赖：公司要安静 / 家里要热闹——都对。"""
        s1 = json.dumps({"scene": "公司"})
        s2 = json.dumps({"scene": "家"})
        self.assertEqual(L.classify_conflict("在公司要保持安静", "在家里要热闹一点", s1, s2),
                         L.CONFLICT_SCENE)
        a, b = self._pair("在公司要保持安静", "在家里想听点音乐", s1, s2)
        res = self.led.classify_pair(a, b)
        self.assertEqual(res["status"], "coexist")
        # 第二个样例：闹钟场景
        self.assertEqual(L.classify_conflict("上班日闹钟开最大声", "周末闹钟关掉静音",
                                             json.dumps({"scene": "工作日"}),
                                             json.dumps({"scene": "周末"})),
                         L.CONFLICT_SCENE)

    def test_classifier_perspective_two_samples(self):
        self.assertEqual(L.classify_conflict("那次项目黄了是因为需求变了", "那次项目黄了是因为排期太赶",
                                             None, None, "老大", "我"),
                         L.CONFLICT_PERSPECTIVE)
        a, b = self._pair("我记得是他先动的手", "我记得是她先开口的")
        res = self.led.classify_pair(a, b, a_perspective="他", b_perspective="她")
        self.assertEqual(res["kind"], L.CONFLICT_PERSPECTIVE)
        self.assertEqual(res["status"], "coexist", "视角差异都是证词，不判对错")

    def test_classifier_true_mutex_needs_full_conditions(self):
        """真互斥候选：同一时间、同一场景、环境相同 + 词面互斥。"""
        same = json.dumps({"scene": "家", "window": {"from": "2026-11-12", "to": "2026-11-12"},
                           "env": {"temp": "cold"}})
        got = L.classify_conflict("空调要开 26 度", "空调不要开，嫌吵", same, same)
        self.assertEqual(got, L.CONFLICT_MUTEX)
        got2 = L.classify_conflict("用户周末会来", "用户周末不会来", same, same)
        self.assertEqual(got2, L.CONFLICT_MUTEX)

    def test_empty_condition_conflict_is_conflicted_never_corrected(self):
        """condition 为空的矛盾对：状态是 conflicted 不是 corrected（施工单 §3.6）。"""
        a, b = self._pair("用户要喝热的", "用户要喝冰的")
        res = self.led.classify_pair(a, b)
        self.assertEqual(res["kind"], L.CONFLICT_UNDETERMINED)
        self.assertEqual(res["status"], "conflicted")
        self.assertEqual(self.led.current_status(a), "conflicted")
        self.assertEqual(self.led.current_status(b), "conflicted")
        # 分类器永不写 corrected：corrected 只能走「原文对账 → 追加更正」
        statuses = {r["status"] for r in self.led.status_timeline(a)}
        self.assertNotIn("corrected", statuses)
        self.assertNotIn("superseded", statuses)

    def test_mutex_without_mutex_words_is_undetermined(self):
        same = json.dumps({"scene": "家", "window": {"from": "2026-11-12", "to": "2026-11-12"}})
        # 条件相同但没有词面互斥 → 不判真互斥，条件不足挂 conflicted
        got = L.classify_conflict("用户喜欢蓝色", "用户提过喜欢绿色", same, same)
        self.assertEqual(got, L.CONFLICT_UNDETERMINED)

    # ---------- coexist 的检索过滤（施工单 §3.4 / §3.6）----------

    def test_coexist_not_surfaced_without_matching_context(self):
        """coexist 条目在条件不匹配的检索轮次里不主动浮现；显式检索仍找得到。"""
        mid = self.led.remember("冷天要喝热的", tags=["喝的"], importance=0.9)["memory_id"]
        self.led.set_memory_condition(mid, json.dumps({"window": {"from": "2026-11-01", "to": "2026-11-05"}}))
        self.led.set_memory_status(mid, "coexist", "时变并存")
        # 主动浮现（无 query 的 fresh 激活路线）不出现
        fresh = [r["id"] for r in self.led._fresh_candidates(20)]
        self.assertNotIn(mid, fresh)
        # 显式检索（用户真的问了）仍找得到
        hits = [h["id"] for h in self.led.recall("喝热的", touch=False)]
        self.assertIn(mid, hits)

    def test_status_log_is_append_only_latest_wins(self):
        mid = self.led.remember("一条会换档的记忆")["memory_id"]
        self.led.set_memory_status(mid, "conflicted", "先挂证据")
        self.led.set_memory_status(mid, "coexist", "后来证实是时变")
        self.assertEqual(self.led.current_status(mid), "coexist")
        timeline = self.led.status_timeline(mid)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["status"], "coexist")  # 最新在前
        # 旧档还在，没被改写
        self.assertEqual({r["status"] for r in timeline}, {"conflicted", "coexist"})

    def test_set_memory_condition_validation(self):
        mid = self.led.remember("带条件的记忆")["memory_id"]
        with self.assertRaises(L.LedgerError):
            self.led.set_memory_condition(mid, "")
        with self.assertRaises(L.LedgerError):
            self.led.set_memory_condition(mid, "not-json")
        with self.assertRaises(L.LedgerError):
            self.led.set_memory_condition(mid, "[1,2]")
        res = self.led.set_memory_condition(mid, json.dumps({"scene": "公司"}),
                                            cond_note="办公室")
        self.assertEqual(res["cond_json"], json.dumps({"scene": "公司"}))


if __name__ == "__main__":
    unittest.main()

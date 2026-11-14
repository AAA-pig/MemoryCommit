# -*- coding: utf-8 -*-
"""dream 职守的自测（v5 · 施工单 §1.5 / §2.1 / §5 第 5 步）。

dream 职守 = 连线、提名、产出模糊新记忆、发现疑点。这里钉住的是：
- 开关 DREAM_SCHEDULER_ENABLED 默认 false，影子运行；关着时零写入
- 影子连线只写 dream_proposal，不真写 memory_link
- 产出硬约束：provenance='dream'、置信 ≤0.4 不可调高、单次 ≤2 条、
  永不进 no_cold（无保护标签 / level=day / grade=0）、不锚原文
- 梦的一轮写操作原子落地（dream_apply）：失败整轮放弃，不留半条
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# DREAM_SCHEDULER_ENABLED / EMOTION_ENABLED 是 import ledger 那一刻读的环境开关。
# 宿主进程开着它们跑测试，「默认关」断言会全红（三轮评审实测 3+1 条失败）。
# 在 import 之前摘掉，这份文件测的永远是默认状态；要测开启行为，在用例里
# patch.dict(os.environ) 后 importlib.reload，别依赖进程环境。
os.environ.pop("DREAM_SCHEDULER_ENABLED", None)
os.environ.pop("EMOTION_ENABLED", None)

try:
    import ledger as L  # noqa: E402
except ModuleNotFoundError:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _spec = importlib.util.spec_from_file_location("ledger", os.path.join(_root, "Ledger.py"))
    L = importlib.util.module_from_spec(_spec)
    sys.modules["ledger"] = L
    _spec.loader.exec_module(L)


class DreamDutyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))
        # 不让宿主环境的环境变量影响「默认关」的断言
        self.env = mock.patch.dict(os.environ, {"DREAM_SCHEDULER_ENABLED": "",
                                                "EMOTION_ENABLED": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.led.close()

    def _seed(self, n=4):
        """种几条带共享标签的普通记忆，给梦当候选。"""
        ids = []
        for i in range(n):
            ids.append(self.led.remember("测试记忆%d：爬山要带水" % i,
                                         tags=["爬山", "装备"])["memory_id"])
        return ids

    # ---------- 开关（施工单 §5 第 5 步 / §1.2 职守表）----------

    def test_switch_defaults_false(self):
        self.assertFalse(L.DREAM_SCHEDULER_ENABLED)
        worker = L.DreamWorker(self.led)
        self.assertFalse(worker.enabled)

    def test_disabled_worker_writes_nothing(self):
        self._seed()
        worker = L.DreamWorker(self.led)  # 默认关
        res = worker.run_once("爬山", produce=2)
        self.assertTrue(res["skipped"])
        st = self.led.stats()
        self.assertEqual(st["proposals"], 0)
        self.assertEqual(st["memories"], 4)
        self.assertEqual(st["commits"], 4)

    # ---------- 影子连线（梦只碰 dream_proposal 与 memory_link）----------

    def test_shadow_link_only_writes_proposal(self):
        self._seed()
        links_before = self.led.stats()["links"]  # seed 自带 auto_link，先记基数
        worker = L.DreamWorker(self.led, enabled=True)
        res = worker.run_once("爬山", produce=0)
        self.assertFalse(res["skipped"])
        self.assertTrue(res["proposals"])
        for p in res["proposals"]:
            self.assertEqual(p["kind"], "link")
        # 影子运行：memory_link 一条没写（对比前后），提案表里有
        self.assertEqual(self.led.stats()["links"], links_before,
                         "影子连线不真写 memory_link")
        self.assertEqual(self.led.stats()["proposals"], len(res["proposals"]))
        rows = self.led._query_all(
            "SELECT * FROM dream_proposal WHERE kind = 'link' AND status = 'pending'")
        self.assertEqual(len(rows), len(res["proposals"]))

    # ---------- 产出硬约束（施工单 §2.1）----------

    def test_dream_output_confidence_cap_hardcoded(self):
        self._seed(2)
        # 传 0.9 也被压回 0.4；上限是模块常量，不是可配置项
        res = self.led.dream_add_memory("（梦）爬山大概要带水和手套", basis_ids=[])
        self.assertLessEqual(res["importance"], L.DREAM_CONFIDENCE_CAP)
        self.assertLessEqual(L.DREAM_CONFIDENCE_CAP, 0.4)
        row = self.led._query_one(
            "SELECT importance FROM memory WHERE id = ?", (res["memory_id"],))
        self.assertLessEqual(float(row["importance"]), 0.4)

    def test_dream_output_registered_as_dream(self):
        self._seed(2)
        ids = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        res = self.led.dream_add_memory("（梦）爬山前夜要检查背包", basis_ids=ids)
        # provenance='dream' 的真值在 dream_proposal（主表结构不动）
        self.assertIn(res["memory_id"], self.led.dream_produced_ids())
        row = self.led._query_one(
            "SELECT * FROM dream_proposal WHERE id = ?", (res["proposal_id"],))
        self.assertEqual(row["kind"], "new_memory")
        self.assertEqual(row["provenance"], "dream")
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(json.loads(row["payload_json"])["memory_id"], res["memory_id"])
        # 提案登记与记忆插入同 commit：提交链里能看到这次产出
        self.assertTrue(res["commit"])

    def test_dream_output_never_enters_no_cold(self):
        """永不进 no_cold 白名单（结构落地）：无保护标签 / level=day / grade=0。"""
        self._seed(2)
        ids = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        res = self.led.dream_add_memory("（梦）山顶大概很冷", basis_ids=ids,
                                        tags="manual, protected, keep, no_decay, 爬山",
                                        importance=0.99)
        row = self.led._query_one("SELECT * FROM memory WHERE id = ?", (res["memory_id"],))
        self.assertEqual(row["level"], "day", "梦产出不碰 topic 的冲刷豁免")
        self.assertEqual(int(row["grade"]), 0, "梦产出从零级起步，不占工作记忆")
        self.assertLessEqual(float(row["importance"]), 0.4,
                             "置信上限压住 0.99，触发不了 ≥0.85 的钉住豁免")
        tags = set((row["tags"] or "").split(","))
        self.assertEqual(tags & L.PROTECTED_TAGS, set(),
                         "保护标签（no_cold 白名单准入）一律剔除")

    def test_dream_output_single_run_capped_at_two(self):
        self._seed(4)
        worker = L.DreamWorker(self.led, enabled=True)
        res = worker.run_once("爬山", produce=5)  # 想要 5 条也只给 2 条
        self.assertLessEqual(len(res["produced"]), L.DREAM_MAX_OUTPUTS)
        self.assertLessEqual(L.DREAM_MAX_OUTPUTS, 2)

    def test_dream_output_has_no_source_anchor(self):
        """梦产出没有原文，不是证据——锚必须为空（老大一句话就能删）。"""
        self._seed(2)
        ids = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        res = self.led.dream_add_memory("（梦）山道上应该有人卖水", basis_ids=ids)
        self.assertEqual(self.led.trace_to_source(res["memory_id"]), [])

    def test_dream_output_basis_cannot_include_dream(self):
        """梦条目不能作梦的依据：写入侧直接拒绝（锁 1 的写路径防线）。"""
        ids = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        first = self.led.dream_add_memory("（梦）第一条", basis_ids=ids)
        with self.assertRaises(L.LedgerError):
            self.led.dream_add_memory("（梦）第二条建立在第一条上",
                                      basis_ids=[first["memory_id"]] + ids)

    # ---------- 事务原子性（施工单 §1.4）----------

    def test_dream_apply_is_atomic(self):
        """梦写线与状态变更同事务：一半失败全部回滚，两边都干净。"""
        self._seed(2)
        links_before = self.led.stats()["links"]
        proposals = [{"kind": "link",
                      "payload": {"from": "m1", "to": "m2", "concept": "爬山"},
                      "basis_ids": ["m1", "m2"], "status": "pending"}]
        bad_status = [{"memory_id": "m1", "status": "not_a_status", "reason": "非法档"}]
        with self.assertRaises(L.LedgerError):
            self.led.dream_apply(proposals, bad_status)
        # rollback 后两边干净：提案没写、状态没写、连线没增加
        self.assertEqual(self.led.stats()["proposals"], 0)
        self.assertEqual(self.led.stats()["status_marks"], 0)
        self.assertEqual(self.led.stats()["links"], links_before)

    def test_dream_apply_succeeds_together(self):
        """合法的组合：提案与状态变更同一事务落地。"""
        self._seed(2)
        proposals = [{"kind": "link",
                      "payload": {"from": "m1", "to": "m2", "concept": "爬山"},
                      "basis_ids": ["m1", "m2"], "status": "pending"}]
        statuses = [{"memory_id": "m1", "status": "conflicted", "reason": "挂证据"}]
        res = self.led.dream_apply(proposals, statuses)
        self.assertEqual(res, {"proposals": 1, "statuses": 1})
        self.assertEqual(self.led.stats()["proposals"], 1)
        self.assertEqual(self.led.stats()["status_marks"], 1)
        self.assertEqual(self.led.current_status("m1"), "conflicted")

    def test_dream_apply_rejects_unknown_memory(self):
        with self.assertRaises(L.LedgerError):
            self.led.dream_apply([], [{"memory_id": "m999", "status": "conflicted"}])


if __name__ == "__main__":
    unittest.main()

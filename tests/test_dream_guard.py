# -*- coding: utf-8 -*-
"""防自我强化三锁 + 不改正文 + 事务原子性（v5 · 施工单 §2.2 / §2.4 / §6）。

梦最容易出的问题是自己把自己说服。三道锁必须都上：
  1. dream 条目永不作证据——不进证据集，否则「梦提案 → 误判 → 再梦」成闭环
  2. dream 条目被再次梦见不加权——只有真人提起才加
  3. dream 不能给节点提名
另外钉住：全系统不存在更新正文的语句（grep 不到更新 summary 的 SQL）、
dream 产出不改旧条目正文（summary 字节级不变）、梦的一轮写同 commit 原子。
"""

import importlib.util
import os
import pathlib
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

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 扫描时跳过的目录：依赖、缓存、版本库
_SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}


def _project_py_files():
    for p in sorted(PROJECT_ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


class DreamGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))
        self.dream = L.DreamWorker(self.led, enabled=True)

    def tearDown(self):
        self.led.close()

    def _seed(self, n=4):
        ids = []
        for i in range(n):
            ids.append(self.led.remember("普通记忆%d：周末去爬山" % i,
                                         tags=["爬山", "周末"])["memory_id"])
        return ids

    # ---------- 锁 1：dream 条目永不作证据（施工单 §2.4）----------

    def test_lock1_dream_output_never_enters_evidence_set(self):
        """构造「梦 A 产出 → 梦 B 引用 A → 提案」，断言 B 的证据集不包含 A。"""
        self._seed(4)
        # 梦先产出一条 A
        basis = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        a = self.led.dream_add_memory("（梦）A：爬山那天大概有雨", basis_ids=basis)
        self.assertIn(a["memory_id"], self.led.dream_produced_ids())
        # 梦 B 再跑一轮：候选池（证据集）里不能有 A
        cands = [r["id"] for r in self.led.propose_clues("爬山", limit=10)]
        self.assertNotIn(a["memory_id"], cands,
                         "锁 1：梦产出永不作证据——候选池必须排除它")
        # 提案侧再拦一层：把 A 塞进 basis 会被拒绝
        with self.assertRaises(L.LedgerError):
            self.led.dream_add_memory("（梦）B：建立在 A 上", basis_ids=[a["memory_id"]])
        # 合法的 B 提案里也不含 A
        b = self.dream.run_once("爬山", produce=0)
        for p in b["proposals"]:
            self.assertNotIn(a["memory_id"], p["basis_ids"])

    # ---------- 锁 2：被再次梦见不加权（施工单 §2.4）----------

    def test_lock2_dream_entry_weighted_zero_by_dreaming(self):
        """同一 dream 条目被梦到 10 次，weight 不变。"""
        self._seed(4)
        basis = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        a = self.led.dream_add_memory("（梦）被反复梦到的条目", basis_ids=basis)
        mid = a["memory_id"]
        row = self.led._query_one("SELECT access_count, importance FROM memory"
                                  " WHERE id = ?", (mid,))
        self.assertEqual(int(row["access_count"]), 0)
        # 梦它 10 次：提名全被拒，访问计数不变，线索权重不变
        for _ in range(10):
            with self.assertRaises(L.LedgerError):
                self.led.nominate_node(mid, "梦里它越来越重要了")
            with self.assertRaises(L.LedgerError):
                self.led.dream_add_memory("（梦）又梦到它一次", basis_ids=[mid])
        row2 = self.led._query_one("SELECT access_count, importance FROM memory"
                                   " WHERE id = ?", (mid,))
        self.assertEqual(int(row2["access_count"]), int(row["access_count"]),
                         "锁 2：被再次梦见不加权，只有真人提起才加")
        self.assertEqual(float(row2["importance"]), float(row["importance"]))
        weights = self.led._query_all(
            "SELECT mc.weight FROM memory_clue mc WHERE mc.memory_id = ?", (mid,))
        for w in weights:
            self.assertEqual(float(w["weight"]), 1.0)
        # 真人提起才会加访问（对照组：普通记忆走 recall/touch）
        self.led.touch(mid, query="真的想起来了", how="recall")
        row3 = self.led._query_one("SELECT access_count FROM memory WHERE id = ?", (mid,))
        self.assertEqual(int(row3["access_count"]), 1, "真人提起照常加权，锁只锁梦")

    # ---------- 锁 3：dream 不能给节点提名（施工单 §2.4）----------

    def test_lock3_dream_entry_cannot_be_nominated_as_node(self):
        self._seed(4)
        basis = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        a = self.led.dream_add_memory("（梦）想当节点的条目", basis_ids=basis)
        with self.assertRaises(L.LedgerError):
            self.led.nominate_node(a["memory_id"], "梦里它很像核心节点")
        # 普通记忆可以提名
        normal = self._seed(1)[-1]
        res = self.led.nominate_node(normal, "真人看它像节点")
        self.assertEqual(res["kind"], "node")
        self.assertEqual(res["status"], "pending")

    # ---------- 不重组旧记忆（施工单 §2.2）----------

    def test_dream_output_never_touches_old_summaries(self):
        """dream 产出的条目与旧条目之间只有 memory_link，旧条目 summary 字节级不变。"""
        self._seed(4)
        before = {r["id"]: r["summary"] for r in self.led._query_all(
            "SELECT id, summary FROM memory")}
        basis = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        self.led.dream_add_memory("（梦）爬山要带水和雨衣", basis_ids=basis,
                                  tags="爬山")
        after = {r["id"]: r["summary"] for r in self.led._query_all(
            "SELECT id, summary FROM memory")}
        for mid, summary in before.items():
            self.assertEqual(after[mid], summary,
                             "旧条目 summary 字节级变了：梦不许重组旧记忆")
        # 旧条目没有被 update 过：提交链里没有任何 memory update 行
        updates = self.led._query_all(
            "SELECT COUNT(*) AS n FROM commit_row WHERE tbl = 'memory' AND op = 'update'")
        self.assertEqual(int(updates[0]["n"]), 0)
        # 新条目与旧条目之间是 memory_link（auto_link 经共享标签织出）
        links = self.led._query_all("SELECT * FROM memory_link")
        self.assertTrue(links, "梦产出应经共享标签织出 memory_link")
        for link in links:
            self.assertIn(link["from_id"], set(before) | set(after))
            self.assertIn(link["to_id"], set(before) | set(after))

    def test_no_set_summary_anywhere_in_project(self):
        """全项目 grep 不到更新 summary 的 SQL 语句（大小写不敏感）——
        不存在任何改写记忆正文的路径，梦无法调用不存在的 API。"""
        # 拼接构造，避免本测试文件自身的注释被当成命中
        pat = ("SET" + " summary").lower()
        offenders = []
        for p in _project_py_files():
            try:
                text = p.read_text(encoding="utf-8").lower()
            except Exception:
                continue
            if pat in text:
                offenders.append(str(p))
        self.assertEqual(offenders, [],
                         "全项目不许出现更新 summary 的 SQL 路径：%s" % offenders)

    def test_no_todo_split_comment(self):
        """代码里不存在「以后拆」这类注释（一体三职，不存在拆）。"""
        pat = ("TODO" + ": 以后拆")
        pat2 = ("等情感模块长出来再拆" + "壳")
        offenders = []
        for p in _project_py_files():
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            if pat in text or pat2 in text:
                offenders.append(str(p))
        self.assertEqual(offenders, [])

    # ---------- 事务原子性（施工单 §1.4 / §1.5）----------

    def test_dream_output_registration_is_same_commit(self):
        """梦产出与提案登记同 commit：登记失败（注入异常）整个提交回滚。"""
        self._seed(4)
        basis = [r["id"] for r in self.led.propose_clues("爬山", limit=2)]
        head_before = self.led.head()
        memories_before = self.led.stats()["memories"]

        builder = self.led.commit("梦产出（注入失败）")
        mid = builder.add_memory("（梦）这条会因登记失败整体回滚", level="day",
                                 tags=["爬山"], importance=0.3, grade=0)

        def _boom(cx):
            raise L.LedgerError("注入：登记失败，整个提交必须回滚")

        builder._extra_tx = _boom
        with self.assertRaises(L.LedgerError):
            builder.seal()
        # rollback 后：记忆没落库、提案没登记、HEAD 没动——两边都干净
        self.assertIsNone(self.led._query_one("SELECT 1 FROM memory WHERE id = ?", (mid,)))
        self.assertEqual(self.led.stats()["memories"], memories_before)
        self.assertEqual(self.led.head(), head_before)
        self.assertEqual(self.led.stats()["proposals"], 0)

    def test_dream_line_and_status_change_same_commit(self):
        """梦写线与状态变更同 commit：成功时一起落地，rollback 后两边都干净。"""
        self._seed(2)
        self.led.dream_apply(
            [{"kind": "link", "payload": {"from": "m1", "to": "m2", "concept": "周末"},
              "basis_ids": ["m1", "m2"], "status": "pending"}],
            [{"memory_id": "m1", "status": "coexist", "reason": "时变并存"}])
        self.assertEqual(self.led.stats()["proposals"], 1)
        self.assertEqual(self.led.current_status("m1"), "coexist")
        # 反向验证：状态挂不上（记忆不存在）时整轮放弃，提案与状态都没写
        with self.assertRaises(L.LedgerError):
            self.led.dream_apply(
                [{"kind": "link", "payload": {"from": "m1", "to": "m2", "concept": "周末"},
                  "basis_ids": ["m1", "m2"], "status": "pending"}],
                [{"memory_id": "m999", "status": "conflicted", "reason": "不存在的记忆"}])
        self.assertEqual(self.led.stats()["proposals"], 1)  # 没多
        self.assertEqual(self.led.current_status("m2"), "active")


    # ---------- 第四道锁：用户否认粘性（2026-11-12 复核实测出）----------

    def test_denial_survives_auto_classification(self):
        """实测复现过的 bug：deny 之后跑一次 classify_pair，disputed 被冲掉。

        老大说一句「你记错了」权重最高、立刻生效——它不能被随后的
        自动分类覆盖，否则降权与停止主动提起只维持到下一次分类为止。
        """
        a = self.led.remember("今天很冷我要喝热的", tags=["天气"])["memory_id"]
        b = self.led.remember("今天很热我要喝冷的", tags=["天气"])["memory_id"]
        self.led.deny_memory(a, "我记错了，没这回事")
        self.assertIn(a, self.led.denied_ids())

        self.led.classify_pair(a, b)  # 自动分类来冲

        self.assertIn(a, self.led.denied_ids(), "disputed 必须粘性，不许被自动分类覆盖")
        self.assertIn(a, self.led.quiet_ids())
        # 另一条照常分类，不受牵连
        self.assertNotIn(b, self.led.denied_ids())

    def test_denial_revoke_is_the_only_way_out(self):
        """解除否认只有一条路：用户显式撤销；自动写入永远解不开。"""
        a = self.led.remember("老大说他不吃辣", tags=["饮食"])["memory_id"]
        self.led.deny_memory(a, "我没说过")
        self.led.dream_apply([], [{"memory_id": a, "status": "active", "reason": "梦觉得没问题"}])
        self.assertIn(a, self.led.denied_ids(), "梦境回写不能解开否认")

        self.led.deny_revoke(a, "是我不吃辣，记对了")
        self.assertNotIn(a, self.led.denied_ids())
        self.assertNotIn(a, self.led.quiet_ids())

    def test_dream_output_cannot_grab_protection_tags(self):
        """梦不能给自己发免死金牌：保护标签 ∪ 鲜活度豁免标签全要剔。"""
        for tag in ("pinned", "never_forget", "manual_keep", "protected", "keep", "no_decay"):
            res = self.led.dream_add_memory("（梦）这条想混个保护标签 %s" % tag, tags=[tag, "爬山"])
            row = self.led._query_one("SELECT tags FROM memory WHERE id = ?", (res["memory_id"],))
            got = {t.strip().lower() for t in (row["tags"] or "").split(",") if t.strip()}
            self.assertNotIn(tag, got, "梦产出不许带保护标签：%s" % tag)
            self.assertIn("爬山", got, "普通标签要留着，别一锅端")


if __name__ == "__main__":
    unittest.main()

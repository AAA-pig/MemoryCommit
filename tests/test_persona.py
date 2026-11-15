# -*- coding: utf-8 -*-
"""人格分区（schema v7）自测：core_rule 三列、只读 core、哈希哨兵、注入预算。

覆盖：增删改查 / core 只读三种拦截 / 哈希不符报警 / 预算截断 /
回放一致 / v5 备份往返 / v4 旧备份可导入 / integrity 通过 /
迁移后 schema_version=7 且存量行 kind='self'。
"""

import ast
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
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


def _make_v6_db(path):
    """手工造一个 v6 老库：旧 core_rule 结构（无三列）+ 存量行。"""
    cx = sqlite3.connect(path)
    cx.executescript("""
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE mcommit (
            id TEXT PRIMARY KEY, parent TEXT, message TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE commit_row (
            commit_id TEXT NOT NULL, seq INTEGER NOT NULL, tbl TEXT NOT NULL,
            op TEXT NOT NULL, row_key TEXT NOT NULL, before_json TEXT,
            after_json TEXT, PRIMARY KEY (commit_id, seq));
        CREATE TABLE core_rule (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    """)
    cx.execute("INSERT INTO meta(k, v) VALUES('schema_version', '6')")
    cx.execute("INSERT INTO core_rule(id, content, priority, created_at)"
               " VALUES('p1', '老的人格条目', 5, '2026-01-01 00:00:00')")
    cx.commit()
    cx.close()


class PersonaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    # ---------- 版本号 ----------

    def test_versions_bumped(self):
        self.assertEqual(L.SCHEMA_VERSION, 8)
        self.assertEqual(L.DUMP_VERSION, 6)
        self.assertEqual(self.led.schema_version(), 8)

    # ---------- 增删改查 ----------

    def test_add_list_update_remove(self):
        pid = self.led.add_persona("self", "喜欢简洁的回复", priority=10)
        self.assertTrue(pid.startswith("p"))
        pid2 = self.led.add_persona("self", "回答要结论先行", priority=20,
                                    source="test")
        rows = self.led.list_persona()
        self.assertEqual([r["id"] for r in rows], [pid2, pid])  # priority DESC
        self.assertEqual([r["priority"] for r in rows], [20, 10])
        row = self.led.list_persona()[0]
        self.assertEqual(row["kind"], "self")
        self.assertEqual(row["source"], "test")
        self.assertTrue(row["updated_at"])
        self.assertTrue(row["created_at"])

        self.assertTrue(self.led.update_persona(pid, content="喜欢极简回复"))
        rows = self.led.list_persona("self")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[pid]["content"], "喜欢极简回复")
        # updated_at 每次更新都要刷写（内容变了，时间戳跟着变或至少非默认）
        self.assertNotEqual(by_id[pid]["updated_at"], "")

        # priority 单独更新 + clamp
        self.assertTrue(self.led.update_persona(pid, priority=5000))
        self.assertEqual(
            {r["id"]: r for r in self.led.list_persona()}[pid]["priority"], 1000)

        # 无变化返回 False，不写字
        head_before = self.led.head()
        self.assertFalse(self.led.update_persona(pid))
        self.assertEqual(self.led.head(), head_before)

        # 删除
        self.assertTrue(self.led.remove_persona(pid2))
        self.assertEqual(len(self.led.list_persona()), 1)
        self.assertFalse(self.led.remove_persona(pid2))  # 已不存在

    def test_add_validations(self):
        with self.assertRaises(L.LedgerError):
            self.led.add_persona("self", "   ")
        with self.assertRaises(L.LedgerError):
            self.led.add_persona("self", "x" * 201)
        with self.assertRaises(L.LedgerError):
            self.led.add_persona("   ", "内容")
        with self.assertRaises(L.LedgerError):
            self.led.add_persona("self", "内容", priority="abc")
        # priority clamp 上下界
        p = self.led.add_persona("self", "上界", priority=99999)
        self.assertEqual({r["id"]: r for r in self.led.list_persona()}[p]
                         ["priority"], 1000)
        p = self.led.add_persona("self", "下界", priority=-3)
        self.assertEqual({r["id"]: r for r in self.led.list_persona()}[p]
                         ["priority"], 0)
        # content 精确 200 字合法
        self.led.add_persona("self", "好" * 200)

    def test_list_persona_kind_filter(self):
        self.led.add_persona("self", "自我设定")
        self.led._seed_persona_core([{"content": "基础设定"}])
        self.assertEqual(len(self.led.list_persona("core")), 1)
        self.assertEqual(len(self.led.list_persona("self")), 1)
        self.assertEqual(len(self.led.list_persona()), 2)
        self.assertEqual(len(self.led.list_persona("nonexist")), 0)

    # ---------- core 只读：三种拦截 ----------

    def test_core_readonly_three_guards(self):
        core_id = self.led._seed_persona_core(
            [{"content": "身份：助手", "priority": 100}])[0]
        # 1) update 拦截
        with self.assertRaises(L.LedgerError):
            self.led.update_persona(core_id, content="改了")
        # 2) remove 拦截
        with self.assertRaises(L.LedgerError):
            self.led.remove_persona(core_id)
        # 3) add kind='core' 拦截
        with self.assertRaises(L.LedgerError):
            self.led.add_persona("core", "新基础设定")
        # 行还在、内容没变
        rows = self.led.list_persona("core")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "身份：助手")

    def test_builder_layer_core_guards_too(self):
        """builder 层同样拦：_allow_core 是唯一旁路，且不暴露给上层包装。"""
        with self.led.user_persona_write("test"):
            with self.led.commit("直连 builder") as c:
                core_id = c.add_persona("core", "播种的基础设定", _allow_core=True)
            with self.assertRaises(L.LedgerError):
                with self.led.commit("再试") as c:
                    c.update_persona(core_id, content="改")
            with self.assertRaises(L.LedgerError):
                with self.led.commit("再试") as c:
                    c.remove_persona(core_id)

    def test_seed_via_allow_core_only(self):
        """显式播种走 _allow_core=True，seed 后哨兵自动封好。"""
        ids = self.led._seed_persona_core([
            {"content": "身份：助手", "priority": 100},
            {"content": "语气：克制", "priority": 90, "source": "manual"},
        ])
        self.assertEqual(len(ids), 2)
        rows = self.led.list_persona("core")
        self.assertEqual({r["source"] for r in rows}, {"seed", "manual"})
        st = self.led._verify_persona_core_seal("test")
        self.assertTrue(st["ok"])

    # ---------- 哈希哨兵 ----------

    def test_hash_sentinel_tamper_alarm_and_reseal(self):
        core_id = self.led._seed_persona_core([{"content": "身份：助手"}])[0]
        self.assertTrue(self.led._verify_persona_core_seal("clean")["ok"])
        # 手工 SQL 绕过提交链改 core 行（模拟篡改）
        db = self.led.db_path
        self.led.close()
        cx = sqlite3.connect(db)
        cx.execute("UPDATE core_rule SET content = ? WHERE id = ?",
                   ("被篡改的基础设定", core_id))
        cx.commit()
        cx.close()
        # 重开库：启动核对报警（只警告，不阻止启动）
        with self.assertLogs("ledger", level="WARNING") as captured:
            led2 = L.Ledger(db)
        try:
            self.assertTrue(any("哈希哨兵不符" in msg for msg in captured.output))
            st = led2._verify_persona_core_seal("manual")
            self.assertFalse(st["ok"])
            # health() 里也核一次：报警但不拦截（ok 不因哨兵翻脸）
            hc = led2.health_check()
            self.assertIn("persona_core", hc)
            self.assertFalse(hc["persona_core"]["ok"])
            # 不自动修：重开一次还在报警
            with self.assertLogs("ledger", level="WARNING"):
                led3 = L.Ledger(db)
            led3.close()
        finally:
            # 人工确认后才能重封
            with self.assertRaises(L.LedgerError):
                led2.reseal_persona_core()
            r = led2.reseal_persona_core(confirm=True)
            self.assertTrue(r["resealed"])
            self.assertFalse(r["was_consistent"])  # 重封前确实不一致
            self.assertTrue(led2._verify_persona_core_seal("after")["ok"])
            led2.close()

    def test_sentinel_silent_when_never_seeded(self):
        """从未播种（无 core 行且 meta 无键）不算篡改，不报警。"""
        self.assertTrue(self.led._verify_persona_core_seal("fresh")["ok"])
        self.led.add_persona("self", "只有 self 行")
        self.assertTrue(self.led._verify_persona_core_seal("self-only")["ok"])

    def test_hash_formula(self):
        """哈希口径：kind='core' 行按 id 排序，sha256(id+\\x1f+content+\\x1f+priority+\\x1e)。"""
        import hashlib
        self.led._seed_persona_core([{"content": "甲"}, {"content": "乙"}])
        rows = sorted(self.led.list_persona("core"), key=lambda r: r["id"])
        h = hashlib.sha256()
        for r in rows:
            h.update(("%s\x1f%s\x1f%s\x1e" % (r["id"], r["content"],
                                              r["priority"])).encode("utf-8"))
        self.assertEqual(self.led._verify_persona_core_seal("f")["current"],
                         h.hexdigest())

    # ---------- 注入 persona_text ----------

    def test_persona_text_budget_truncation(self):
        self.led.add_persona("self", "甲" * 30, priority=30)
        self.led.add_persona("self", "乙" * 30, priority=20)
        self.led.add_persona("self", "丙" * 30, priority=10)
        txt = self.led.persona_text(budget=65)  # 只放得下前两条整行
        self.assertEqual(txt.count("\n"), 2)
        self.assertIn("（另有 1 条未注入）", txt)
        # 每行格式「- {content}」
        for line in txt.splitlines()[:2]:
            self.assertTrue(line.startswith("- "))
        # 预算充裕：三条全进，无尾巴
        txt = self.led.persona_text(budget=1200)
        self.assertEqual(txt.count("\n"), 2)
        self.assertNotIn("未注入", txt)
        # 预算为 0：一条都进不去，只剩尾巴
        txt = self.led.persona_text(budget=0)
        self.assertEqual(txt, "（另有 3 条未注入）")
        # 非法预算
        with self.assertRaises(L.LedgerError):
            self.led.persona_text(budget=-1)

    def test_persona_text_empty(self):
        self.assertEqual(self.led.persona_text(), "")

    def test_persona_text_ordering(self):
        """priority DESC, created_at ASC：同优先级先来的排前面。"""
        self.led.add_persona("self", "第一条", priority=10)
        p2 = self.led.add_persona("self", "第二条", priority=10)
        self.led.update_persona(p2, priority=50)
        txt = self.led.persona_text()
        self.assertLess(txt.index("- 第二条"), txt.index("- 第一条"))

    # ---------- 回放一致 / integrity ----------

    def test_replay_and_health_after_persona_writes(self):
        self.led._seed_persona_core([{"content": "身份：助手", "priority": 100}])
        pid = self.led.add_persona("self", "喜欢简洁", priority=10)
        self.led.update_persona(pid, priority=30)
        self.led.add_persona("self", "临时条目")
        self.led.remove_persona(
            [r["id"] for r in self.led.list_persona() if r["content"] == "临时条目"][0])
        hc = self.led.health_check()
        self.assertTrue(hc["ok"], hc["problems"])
        self.assertTrue(hc["persona_core"]["ok"])

    # ---------- 备份：v5 往返 / v4 兼容 ----------

    def test_dump_v6_roundtrip(self):
        self.led._seed_persona_core([{"content": "身份：助手", "priority": 100}])
        self.led.add_persona("self", "喜欢简洁", priority=10, source="test")
        dump_path = os.path.join(self.tmp, "dump.jsonl")
        text = self.led.dump_jsonl(dump_path)
        header = json.loads(text.splitlines()[0])
        self.assertEqual(header["version"], 6)
        self.assertEqual(header["schema_version"], 8)
        led2 = L.Ledger(os.path.join(self.tmp, "rt.db"))
        try:
            res = led2.load_jsonl(dump_path)
            self.assertGreater(res["written"], 0)
            self.assertEqual(led2.schema_version(), 8)
            core = led2.list_persona("core")
            self.assertEqual(len(core), 1)
            self.assertEqual(core[0]["content"], "身份：助手")
            self.assertEqual(core[0]["source"], "seed")
            self.assertEqual(len(led2.list_persona("self")), 1)
            self.assertTrue(led2._verify_persona_core_seal("rt")["ok"])
            hc = led2.health_check()
            self.assertTrue(hc["ok"], hc["problems"])
        finally:
            led2.close()

    def test_v4_backup_imports(self):
        """v4 旧备份（无三列、header version=4）新代码照收，列落默认值。"""
        self.led.add_persona("self", "新库才有的行", priority=10)
        dump_path = os.path.join(self.tmp, "v4.jsonl")
        text = self.led.dump_jsonl(dump_path)
        body = []
        for ln in text.splitlines()[1:]:
            obj = json.loads(ln)
            if obj.get("t") == "__eof__":
                break
            # v4 时代没有 creative / 沉淀 / v8 群聊三表等 v5+ 结构，只保留 v4 就有的表
            if obj.get("t") in ("meta", "core_rule", "creative", "access_log",
                                "memory_condition", "memory_status",
                                "dream_proposal", "memory_source_anchor",
                                "user_denial", "memory_speaker",
                                "profile_visibility", "user_attitude"):
                continue
            body.append(obj)
        counts = {}
        for obj in body:
            counts[obj["t"]] = counts.get(obj["t"], 0) + 1
        header = {"format": L.DUMP_FORMAT, "version": 4, "schema_version": 6,
                  "core_api_version": 2, "dumped_at": "2026-01-01 00:00:00",
                  "tables": counts, "rows_total": len(body)}
        lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
        lines += [json.dumps(obj, ensure_ascii=False, sort_keys=True)
                  for obj in body]
        lines.append(json.dumps({"t": "__eof__", "rows_total": len(body)},
                                ensure_ascii=False, sort_keys=True))
        v4_path = os.path.join(self.tmp, "v4made.jsonl")
        with open(v4_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        led2 = L.Ledger(os.path.join(self.tmp, "v4rt.db"))
        try:
            res = led2.load_jsonl(v4_path)
            self.assertGreater(res["written"], 0)
            self.assertEqual(led2.schema_version(), 8)
            self.assertEqual(len(led2.list_persona()), 0)
        finally:
            led2.close()

    def test_v4_backup_with_legacy_core_rule_rows(self):
        """v4 备份里带旧结构 core_rule 行（无三列）：灌进来 kind 落默认 'self'。"""
        self.led.dump_jsonl(os.path.join(self.tmp, "unused.jsonl"))
        row = {"t": "core_rule", "r": {"id": "p9", "content": "旧时代的行",
                                       "priority": 3,
                                       "created_at": "2026-01-01 00:00:00"}}
        header = {"format": L.DUMP_FORMAT, "version": 4, "schema_version": 6,
                  "core_api_version": 2, "dumped_at": "2026-01-01 00:00:00",
                  "tables": {"core_rule": 1}, "rows_total": 1}
        lines = [json.dumps(header, ensure_ascii=False, sort_keys=True),
                 json.dumps(row, ensure_ascii=False, sort_keys=True),
                 json.dumps({"t": "__eof__", "rows_total": 1},
                            ensure_ascii=False, sort_keys=True)]
        v4_path = os.path.join(self.tmp, "v4withrows.jsonl")
        with open(v4_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        led2 = L.Ledger(os.path.join(self.tmp, "v4rows.db"))
        try:
            led2.load_jsonl(v4_path)
            rows = led2.list_persona()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "p9")
            self.assertEqual(rows[0]["kind"], "self")  # 默认值兜底
            self.assertEqual(rows[0]["source"], "self")
            self.assertEqual(rows[0]["updated_at"], "")
        finally:
            led2.close()

    def test_v5_backup_rejected_by_v4_semantics(self):
        """v6 备份旧代码（DUMP_VERSION=5）必须在文件头拒收——用常量推演。"""
        self.led.dump_jsonl(os.path.join(self.tmp, "d.jsonl"))
        text = open(os.path.join(self.tmp, "d.jsonl"), encoding="utf-8").read()
        header = json.loads(text.splitlines()[0])
        # 新备份 header 是 v6
        self.assertEqual(header["version"], 6)
        # 旧代码的拒收条件：version > 旧 DUMP_VERSION
        old_dump_version = 5
        self.assertGreater(header["version"], old_dump_version)

    # ---------- 迁移 6→7 ----------

    def test_migrate_6_to_7(self):
        path = os.path.join(self.tmp, "v6.db")
        _make_v6_db(path)
        led2 = L.Ledger(path)
        try:
            self.assertEqual(led2.schema_version(), 8)
            rows = led2.list_persona()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["id"], "p1")
            self.assertEqual(row["content"], "老的人格条目")  # 数据一个字不动
            self.assertEqual(row["priority"], 5)
            self.assertEqual(row["created_at"], "2026-01-01 00:00:00")
            # 存量行落默认分区，绝不伪造 core 行
            self.assertEqual(row["kind"], "self")
            self.assertEqual(row["source"], "self")
            self.assertEqual(row["updated_at"], "")
            # 迁移后照常可写：计数器从表内最大编号起算，不撞存量 id
            pid = led2.add_persona("self", "迁移后新写入")
            self.assertTrue(pid.startswith("p"))
            self.assertNotEqual(pid, "p1")
            hc = led2.health_check()
            self.assertTrue(hc["ok"], hc["problems"])
            # 从未播种：哨兵安静
            self.assertTrue(hc["persona_core"]["ok"])
        finally:
            led2.close()

    def test_migrate_is_idempotent(self):
        """新库（SCHEMA_STATEMENTS 已带三列）从版本 0 全链迁移不炸重列。"""
        # setUp 里建的库就是这条路径：SCHEMA_STATEMENTS 建表 → 0→8 迁移
        self.assertEqual(self.led.schema_version(), 8)
        cols = [r["name"] for r in self.led._query_all(
            "PRAGMA table_info(core_rule)")]
        self.assertEqual(cols, ["id", "content", "priority", "created_at",
                                "kind", "source", "updated_at"])

    def test_builder_add_returns_id_and_commit(self):
        pid = self.led.add_persona("self", "提交链上要有人格这页")
        self.assertTrue(self.led.head())
        # 回放：core_rule 在投影表清单里，重放与现表一致
        replay = self.led._replay_tables(self.led.head())
        self.assertIn("core_rule", replay)
        head_id = self.led.head()
        rows = self.led._query_all("SELECT row_key FROM commit_row"
                                   " WHERE commit_id = ?", (head_id,))
        self.assertTrue(any("id=" + pid in r["row_key"] for r in rows))


class BuilderPersonaCoverageTest(unittest.TestCase):
    """CommitBuilder 层 persona 方法的分支：Ledger 包装有预检挡着，
    这些分支只能 builder 直连才打得开。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))
        # builder 直连测试：显式开通行令牌。真实运行里只有用户路径会开，
        # 这里开是为了测 builder 自身分支，不是替它绕过安全闸。
        self._auth = self.led.user_persona_write("test")
        self._auth.__enter__()

    def tearDown(self):
        self._auth.__exit__(None, None, None)
        self.led.close()

    def _builder(self, msg="t"):
        return self.led.commit(msg)

    def test_builder_add_persona_validations(self):
        # 校验抛错后不开 with 退出（避免空提交 seal），builder 弃用不封链
        c = self.led.commit("t")
        with self.assertRaises(L.LedgerError):
            c.add_persona("x" * 33, "内容")          # kind 超 32 字
        with self.assertRaises(L.LedgerError):
            c.add_persona("self", "内容", source="y" * 65)  # source 超 64 字

    def test_builder_in_memory_snapshot_find(self):
        """同一 builder 内先 add 再 update：_find_persona 走内存快照分支。"""
        with self.led.commit("同一提交内连续写") as c:
            pid = c.add_persona("self", "第一版", priority=5)
            self.assertTrue(c.update_persona(pid, content="第二版", priority=9))
            self.assertFalse(c.update_persona(pid, content="第二版"))  # 无变化 False
            self.assertFalse(c.update_persona(pid, priority=9))  # 同值无变化
        row = {r["id"]: r for r in self.led.list_persona()}[pid]
        self.assertEqual(row["content"], "第二版")
        self.assertEqual(row["priority"], 9)

    def test_builder_update_persona_validations(self):
        pid = self.led.add_persona("self", "原内容")
        c = self.led.commit("t")
        with self.assertRaises(L.LedgerError):
            c.update_persona(pid, content="   ")     # 空
        with self.assertRaises(L.LedgerError):
            c.update_persona(pid, content="x" * 201)  # 超 200
        with self.assertRaises(L.LedgerError):
            c.update_persona(pid, priority="abc")     # 非整数
        self.assertFalse(c.remove_persona("p99999"))  # 不存在 → False

    def test_builder_find_persona_skips_other_tables(self):
        """_find_persona 扫内存快照时要跳过非 core_rule 行（如同提交里的记忆）。"""
        pid = self.led.add_persona("self", "先写人格")
        c = self.led.commit("同提交里记忆与人格混写")
        c.add_memory("一条普通记忆，凑个非 core_rule 行", tags="杂")
        self.assertFalse(c.update_persona("p99999"))  # 扫过 memory 行后落库查 → None
        self.assertFalse(c.remove_persona("p99999"))
        # 存量行仍能从内存快照找到并更新
        self.assertTrue(c.update_persona(pid, priority=7))
        c.seal()
        row = {r["id"]: r for r in self.led.list_persona()}[pid]
        self.assertEqual(row["priority"], 7)

    def test_builder_core_write_chains_extra_tx(self):
        """core 写入挂哈希钩子时不得覆盖已有 _extra_tx（梦产出）：链式接住。"""
        touched = []
        with self.led.commit("先有别的钩子") as c:
            c._extra_tx = lambda cx: touched.append("prev")
            c.add_persona("core", "链式钩子下的播种", _allow_core=True)
        self.assertEqual(touched, ["prev"])
        self.assertTrue(self.led._verify_persona_core_seal("chain")["ok"])


class AutoSinkGuardTest(unittest.TestCase):
    """自动写入守卫：restricted_auto_write 范围内人格分区只读。
    自动路径（梦产出 / 自动沉淀）不许碰 core_rule——门面层与
    builder 层的人格写入口都挂断言；播种（_allow_core）也不豁免，
    守卫优先于通行令牌。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    def test_guard_blocks_facade_persona_writes(self):
        self.led.add_persona("self", "守卫前写入")
        with self.led.restricted_auto_write():
            with self.assertRaises(L.LedgerError):
                self.led.add_persona("self", "守卫内新增")
            with self.assertRaises(L.LedgerError):
                self.led.update_persona("p1", content="改")
            with self.assertRaises(L.LedgerError):
                self.led.remove_persona("p1")
        self.assertEqual(self.led._auto_write_guard, 0)
        self.led.add_persona("self", "守卫外照常写")
        self.assertEqual(len(self.led.list_persona()), 2)

    def test_guard_blocks_builder_persona_writes(self):
        pid = self.led.add_persona("self", "先有条目")
        with self.led.restricted_auto_write():
            c = self.led.commit("守卫内直连 builder")
            with self.assertRaises(L.LedgerError):
                c.add_persona("self", "builder 也拦")
            with self.assertRaises(L.LedgerError):
                c.update_persona(pid, content="改")
            with self.assertRaises(L.LedgerError):
                c.remove_persona(pid)
        self.assertEqual(self.led._auto_write_guard, 0)

    def test_guard_blocks_explicit_seed_inside(self):
        """守卫内连播种也拦：令牌在 _seed_persona_core 内部开，但反向守卫

        优先——自动路径里出现播种，说明调用方搞错了，不能默默放过去。
        守卫退出后播种照常。
        """
        with self.led.restricted_auto_write():
            with self.assertRaises(L.LedgerError):
                self.led._seed_persona_core([{"content": "身份：助手"}])
        self.assertEqual(self.led._auto_write_guard, 0)
        ids = self.led._seed_persona_core([{"content": "身份：助手"}])
        self.assertEqual(len(ids), 1)
        self.assertTrue(self.led._verify_persona_core_seal("in-guard")["ok"])

    def test_guard_reentrant_and_restored_after_error(self):
        with self.led.restricted_auto_write():
            with self.led.restricted_auto_write():
                self.assertEqual(self.led._auto_write_guard, 2)
                with self.assertRaises(L.LedgerError):
                    self.led.add_persona("self", "嵌套内也拦")
            self.assertEqual(self.led._auto_write_guard, 1)
            with self.assertRaises(L.LedgerError):
                self.led.add_persona("self", "外层内仍拦")
        self.assertEqual(self.led._auto_write_guard, 0)
        self.led.add_persona("self", "全退出后照常写")

    def test_dream_writes_run_under_guard(self):
        """梦产出执行期间守卫开着：执行中写人格被拦，记忆写入不受影响。"""
        self.led.add_persona("self", "守卫前已有")
        orig_commit = self.led.commit
        guards = []

        def spy(msg):
            guards.append(self.led._auto_write_guard)
            with self.assertRaises(L.LedgerError):
                self.led.add_persona("self", "梦里偷写人格")
            return orig_commit(msg)

        with mock.patch.object(self.led, "commit", side_effect=spy):
            res = self.led.dream_add_memory("（梦）一段模糊印象", scope_key="")
        self.assertTrue(res["memory_id"])
        self.assertEqual(guards, [1])
        self.assertEqual(self.led._auto_write_guard, 0)


class LedgerPersonaPrecheckTest(unittest.TestCase):
    """Ledger 包装层预检分支：预检挡住就不开提交，返回 False 或抛错。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()

    def test_update_persona_precheck_fails(self):
        self.assertFalse(self.led.update_persona("", content="x"))
        self.assertFalse(self.led.update_persona(None, content="x"))
        self.assertFalse(self.led.update_persona("p99999", content="x"))
        pid = self.led.add_persona("self", "原内容")
        with self.assertRaises(L.LedgerError):
            self.led.update_persona(pid, content="   ")
        with self.assertRaises(L.LedgerError):
            self.led.update_persona(pid, content="x" * 201)
        with self.assertRaises(L.LedgerError):
            self.led.update_persona(pid, priority="abc")

    def test_remove_persona_precheck_fails(self):
        self.assertFalse(self.led.remove_persona(""))
        self.assertFalse(self.led.remove_persona(None))
        self.assertFalse(self.led.remove_persona("p99999"))

    def test_persona_text_budget_type_error(self):
        with self.assertRaises(L.LedgerError):
            self.led.persona_text(budget="很多")

    def test_health_reconciliation_ignores_weird_row_keys(self):
        """对账解析 row_key：不带 id= 前缀的键不进 core 链 id 集合。"""
        self.led.add_persona("self", "正常写入")
        with self.led._tx() as cx:
            cx.execute(
                "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key)"
                " VALUES('Cfake', 999, 'core_rule', 'update', 'weird=1')")
        hc = self.led.health_check()
        self.assertTrue(hc["ok"], hc["problems"])


# -- main.py 注入接线（人格先于记忆） ----------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UMO = "123:FriendMessage:1"


class _Evt:
    def __init__(self, text="", umo=UMO):
        self._text = text
        self.unified_msg_origin = umo

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return text


def _collect(agen):
    async def go():
        return [str(x) async for x in agen]
    return asyncio.run(go())


def _persona_cmd(plugin, text=""):
    return "\n".join(_collect(plugin.cmd_persona(_Evt(text))))


class _Req:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt


def _fresh_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", UMO)
    monkeypatch.setenv("JJ_MEMORY_MAINTAIN_EVERY", "20")
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main
    return main.MemoryCommitPlugin(None)


def test_inject_persona_comes_before_brief(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    plugin.ledger.add_persona("self", "人格设定先注入", priority=10)
    plugin.ledger.remember("用户怕冷，空调开 26 度", tags="偏好")
    req = _Req("空调多少度")
    asyncio.run(plugin.inject_memory(_Evt("空调多少度"), req))
    assert req.system_prompt.index("人格设定先注入") < \
        req.system_prompt.index("记忆账本"), "人格必须拼在记忆 brief 之前"


def test_inject_persona_failure_does_not_block_reply(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    plugin.ledger.remember("用户怕冷，空调开 26 度", tags="偏好")

    def boom(*a, **kw):
        raise RuntimeError("人格表炸了")

    monkeypatch.setattr(plugin.ledger, "persona_text", boom)
    req = _Req("空调多少度")
    asyncio.run(plugin.inject_memory(_Evt("空调多少度"), req))
    assert "记忆账本" in req.system_prompt, "人格炸了记忆照常注入"
    assert plugin.failures.counts["人格注入失败"] == 1


# -- memory_persona LLM 工具 --------------------------------------------------

def _tool(plugin, **kw):
    return asyncio.run(plugin.memory_persona(_Evt(), **kw))


def test_memory_persona_tool_full_cycle(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    out = _tool(plugin, action="list")
    assert "还没有任何自我设定" in out
    out = _tool(plugin, action="add", content="回答要结论先行", priority=10)
    assert out.startswith("已新增自我设定 p") and "提交" in out
    pid = out.split("（")[0].replace("已新增自我设定 ", "").strip()
    out = _tool(plugin, action="list")
    assert pid in out and "结论先行" in out and "权重 10" in out
    out = _tool(plugin, action="update", id=pid, content="回答结论先行", priority=50)
    assert "已更新" in out and pid in out
    row = {r["id"]: r for r in plugin.ledger.list_persona()}[pid]
    assert row["content"] == "回答结论先行" and row["priority"] == 50
    assert row["kind"] == "self", "工具只能写 self 分区"
    out = _tool(plugin, action="remove", id=pid)
    assert "已删除" in out
    assert plugin.ledger.list_persona("self") == []


def test_memory_persona_tool_list_hides_core_rows(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    plugin.ledger._seed_persona_core([{"content": "身份：助手基础设定"}])
    plugin.ledger.add_persona("self", "自我设定一条")
    out = _tool(plugin, action="list")
    assert "自我设定一条" in out
    assert "基础设定" not in out, "core 行不暴露给模型"


def test_memory_persona_tool_core_row_untouchable(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    core_id = plugin.ledger._seed_persona_core([{"content": "身份：助手"}])[0]
    out = _tool(plugin, action="update", id=core_id, content="改基础设定")
    assert out.startswith("没完成") and "只读" in out
    out = _tool(plugin, action="remove", id=core_id)
    assert out.startswith("没完成") and "只读" in out
    rows = plugin.ledger.list_persona("core")
    assert rows[0]["content"] == "身份：助手", "core 行一个字没动"


def test_memory_persona_tool_validations(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    assert "不可用" in asyncio.run(plugin.memory_persona(
        _Evt(umo="other:GroupMessage:9"), action="add", content="x"))
    assert "不认识的 action" in _tool(plugin, action="修改")
    assert "不能为空" in _tool(plugin, action="add", content="  ")
    assert "至少给一个" in _tool(plugin, action="update", id="p1")
    assert "没改成" in _tool(plugin, action="update", id="p999", content="新内容")
    assert "没删掉" in _tool(plugin, action="remove", id="p999")


# -- /人格 指令 ----------------------------------------------------------------

def test_cmd_persona_owner_only(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    out = "\n".join(_collect(plugin.cmd_persona(
        _Evt("/人格", umo="other:GroupMessage:9"))))
    assert "不可用" in out


def test_cmd_persona_list_empty(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    assert "还没有任何人格设定" in _persona_cmd(plugin, "/人格")


def test_cmd_persona_list_marks_core_readonly(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    plugin.ledger._seed_persona_core([{"content": "身份：助手", "priority": 100}])
    plugin.ledger.add_persona("self", "自我设定", priority=5)
    out = _persona_cmd(plugin, "/人格")
    assert "基础·只读" in out and "身份：助手" in out
    assert "[自我 · 权重 5]" in out and "自我设定" in out


def test_cmd_persona_add_flow(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    assert "用法" in _persona_cmd(plugin, "/人格 加")
    out = _persona_cmd(plugin, "/人格 加 回答要结论先行")
    assert "已新增自我设定 p" in out and "提交" in out
    assert len(plugin.ledger.list_persona("self")) == 1
    out = _persona_cmd(plugin, "/人格 加 " + "长" * 201)
    assert "没加上" in out


def test_cmd_persona_update_flow(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    pid = plugin.ledger.add_persona("self", "原内容", priority=5)
    assert "用法" in _persona_cmd(plugin, "/人格 改")
    assert "没说要改什么" in _persona_cmd(plugin, "/人格 改 %s" % pid)
    assert "没改成" in _persona_cmd(plugin, "/人格 改 p999 新内容")
    out = _persona_cmd(plugin, "/人格 改 %s 新内容" % pid)
    assert "已更新" in out
    row = {r["id"]: r for r in plugin.ledger.list_persona()}[pid]
    assert row["content"] == "新内容" and row["priority"] == 5
    out = _persona_cmd(plugin, "/人格 改 %s 权重88" % pid)
    assert "已更新" in out
    row = {r["id"]: r for r in plugin.ledger.list_persona()}[pid]
    assert row["priority"] == 88 and row["content"] == "新内容"
    out = _persona_cmd(plugin, "/人格 改 %s 改成这样 权重12" % pid)
    assert "已更新" in out
    row = {r["id"]: r for r in plugin.ledger.list_persona()}[pid]
    assert row["content"] == "改成这样" and row["priority"] == 12


def test_cmd_persona_update_core_refused(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    core_id = plugin.ledger._seed_persona_core([{"content": "身份：助手"}])[0]
    out = _persona_cmd(plugin, "/人格 改 %s 改基础" % core_id)
    assert "没改成" in out and "只读" in out


def test_cmd_persona_remove_flow(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    assert "用法" in _persona_cmd(plugin, "/人格 删")
    pid = plugin.ledger.add_persona("self", "要删的")
    assert "没删掉" in _persona_cmd(plugin, "/人格 删 p999")
    out = _persona_cmd(plugin, "/人格 删 %s" % pid)
    assert "已删除" in out
    assert plugin.ledger.list_persona("self") == []


def test_cmd_persona_remove_core_refused(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    core_id = plugin.ledger._seed_persona_core([{"content": "身份：助手"}])[0]
    out = _persona_cmd(plugin, "/人格 删 %s" % core_id)
    assert "没删掉" in out and "只读" in out


def test_cmd_persona_unknown_sub(tmp_path, monkeypatch):
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    out = _persona_cmd(plugin, "/人格 折腾")
    assert "不认识的子命令" in out and "折腾" in out


# -- 自动沉淀收口：源码扫描 + 行为 ----------------------------------------------

PERSONA_WRITE_NAMES = {"add_persona", "update_persona", "remove_persona"}


def _main_ast():
    with open(os.path.join(PLUGIN_DIR, "main.py"), encoding="utf-8") as f:
        return ast.parse(f.read())


def _find_func(tree, name):
    import ast
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError("main.py 里没有函数 %s" % name)


def test_auto_sink_source_has_no_persona_writes():
    """源码扫描：自动沉淀相关函数体内不出现人格写接口调用。

    ① _auto_sink / auto_sink_summary 体内没有任何 add_persona /
    update_persona / remove_persona 属性调用；② _auto_sink 必须
    用 restricted_auto_write 收口（接线不许拆）。
    """
    tree = _main_ast()
    for name in ("_auto_sink", "auto_sink_summary"):
        fn = _find_func(tree, name)
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                assert sub.func.attr not in PERSONA_WRITE_NAMES, (
                    "%s 体内不许调人格写接口：%s" % (name, sub.func.attr))
    sink_calls = {n.func.attr for n in ast.walk(_find_func(tree, "_auto_sink"))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "restricted_auto_write" in sink_calls, \
        "_auto_sink 必须在 restricted_auto_write 上下文里沉淀"


def test_memory_persona_tool_never_takes_kind():
    """工具签名不向模型暴露 kind：只能写 self 分区，没有别的选项。"""
    fn = _find_func(_main_ast(), "memory_persona")
    names = {a.arg for a in fn.args.args}
    assert "kind" not in names
    assert {"action", "id", "content", "priority"} <= names
    # 装饰器把工具名钉成 memory_persona
    deco = fn.decorator_list[0]
    assert isinstance(deco, ast.Call) and deco.func.attr == "llm_tool"
    assert deco.keywords[0].value.value == "memory_persona"


def test_auto_sink_leaves_core_rule_untouched(tmp_path, monkeypatch):
    """行为测试：跑一次自动沉淀，core_rule 逐行不变。

    顺带钉住：沉淀的 remember 调用确实发生在守卫范围内（guard==1）。
    """
    plugin = _fresh_plugin(tmp_path, monkeypatch)
    plugin.ledger._seed_persona_core([
        {"content": "身份：助手", "priority": 100}])
    plugin.ledger.add_persona("self", "喜欢简洁", priority=10)
    before = [dict(r) for r in plugin.ledger.list_persona()]
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    plugin.ledger.append_message("user", "今天聊了空调和遥控器",
                                 session_id=UMO)
    guards = []
    orig_remember = plugin.ledger.remember

    def spy_remember(*a, **kw):
        guards.append(plugin.ledger._auto_write_guard)
        return orig_remember(*a, **kw)

    monkeypatch.setattr(plugin.ledger, "remember", spy_remember)
    res = plugin._auto_sink()
    assert res.get("memory_id"), "自动沉淀确实写出了记忆"
    assert guards == [1], "沉淀的写入必须在 restricted_auto_write 范围内"
    after = [dict(r) for r in plugin.ledger.list_persona()]
    assert after == before, "core_rule 逐行不变（含 self 行）"
    assert plugin.ledger._auto_write_guard == 0, "守卫用完即退，不泄漏"
    # 守卫退出后，用户显式路径照常可写
    pid = plugin.ledger.add_persona("self", "守卫外显式写入")
    assert pid.startswith("p")


if __name__ == "__main__":
    unittest.main()

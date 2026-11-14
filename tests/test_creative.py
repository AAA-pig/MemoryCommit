# -*- coding: utf-8 -*-
"""creative 表（schema v6，2026-11-15）的行为测试。

覆盖任务验收点：新库建表、老库幂等补建、upsert 后按 id 取回、
body 非空、payload 能 json.loads 还原且字段完整；外加 upsert 的
update 分支（出生时间不随改名变化）、列表轻量字段、非法入参显式失败。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ledger", os.path.join(_root, "ledger.py"))
L = importlib.util.module_from_spec(_spec)
sys.modules["ledger"] = L
_spec.loader.exec_module(L)


def _fake_project(i=1, body="第一段。\n\n第二段。"):
    """造一条自明结构的创作项目（测试不依赖真实私人数据文件）。"""
    return {
        "id": "proj%02d" % i,
        "title": "测试项目 %d" % i,
        "work_type": "短篇小说",
        "status": "drafting",
        "premise": "一个前提 %d" % i,
        "outline": ["起", "承", {"第三幕": "转与合"}],
        "body": body,
        "payload": {
            "id": "proj%02d" % i, "title": "测试项目 %d" % i,
            "chunks": ["a", "b"], "nested": {"x": [1, 2, {"y": True}]},
        },
        "current_chars": 123,
        "target_chars": 3000,
        "created_at": "2026-11-01 10:00:00",
    }


class CreativeTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")

    def _open(self):
        led = L.Ledger(self.path)
        self.addCleanup(led.close)
        return led

    # ---------- schema ----------

    def test_fresh_db_creates_creative_table(self):
        """新库自动建出 creative 表，结构版本升到 6。"""
        led = self._open()
        self.assertEqual(led.schema_version(), 6)
        rows = led._query_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='creative'")
        self.assertTrue(rows, "新库没建 creative 表")

    def test_old_v5_db_idempotent_rebuild(self):
        """v5 老库（没有 creative 表）打开时幂等补建，升到 6，原有数据不动。"""
        led = self._open()                       # 先按 v6 建库
        led.remember("老库里的一条记忆")          # 放一条真实账本数据
        led.close()
        cx = sqlite3.connect(self.path)
        with cx:                                 # 手工回退成 v5 老库的样子
            cx.execute("DROP TABLE creative")
            cx.execute("UPDATE meta SET v='5' WHERE k='schema_version'")
        cx.close()

        led2 = self._open()                      # 重新打开 → 走 5→6 迁移
        self.assertEqual(led2.schema_version(), 6)
        self.assertTrue(led2.get_creative("proj01") is None)  # 表已在，可读
        led2.upsert_creative("proj01", "补建之后写第一条")     # 可写
        row = led2.get_creative("proj01")
        self.assertEqual(row["title"], "补建之后写第一条")
        # 老库原有数据一条不动
        self.assertEqual(led2.stats()["memories"], 1)

    def test_reopen_latest_db_no_rebuild_side_effect(self):
        """已是 v6 的库重复打开：迁移幂等，不重置不重建出额外行为。"""
        led = self._open()
        led.upsert_creative("proj01", "第一次", body="正文一")
        led.close()
        led2 = self._open()
        row = led2.get_creative("proj01")
        self.assertEqual(row["title"], "第一次")
        self.assertEqual(row["body"], "正文一")

    # ---------- upsert / get ----------

    def test_upsert_then_get_by_id(self):
        """插入后按 id 取回：全部字段逐位对得上（outline/payload 双向 JSON）。"""
        led = self._open()
        p = _fake_project()
        res = led.upsert_creative(
            p["id"], p["title"], work_type=p["work_type"], status=p["status"],
            premise=p["premise"], outline=p["outline"], body=p["body"],
            payload=p["payload"], current_chars=p["current_chars"],
            target_chars=p["target_chars"], created_at=p["created_at"])
        self.assertEqual(res["action"], "insert")
        row = led.get_creative(p["id"])
        self.assertEqual(row["title"], p["title"])
        self.assertEqual(row["work_type"], p["work_type"])
        self.assertEqual(row["status"], p["status"])
        self.assertEqual(row["premise"], p["premise"])
        self.assertEqual(row["body"], p["body"])
        self.assertEqual(json.loads(row["outline"]), p["outline"])
        self.assertEqual(json.loads(row["payload"]), p["payload"])
        self.assertEqual(row["current_chars"], p["current_chars"])
        self.assertEqual(row["target_chars"], p["target_chars"])
        self.assertEqual(row["created_at"], p["created_at"])
        self.assertEqual(row["created_seq"], 1)

    def test_body_non_empty_and_readable(self):
        """body 是给人读的正文：写什么读什么，非空。"""
        led = self._open()
        body = "夜里那半边醒着，\n\n另一半在梦里数着白日的事。"
        led.upsert_creative("p1", "随笔", body=body)
        row = led.get_creative("p1")
        self.assertTrue(row["body"], "body 不许为空")
        self.assertEqual(row["body"], body)

    def test_payload_roundtrip_full_fields(self):
        """payload 整条 JSON 保真：loads 回来与原始 dict 完全相等（含嵌套）。"""
        led = self._open()
        payload = {
            "id": "x", "title": "半圈水膜", "work_type": "短篇小说",
            "status": "drafting", "premise": "前提", "outline": [1, 2, 3],
            "draft_chunks": ["a", "b", "c"], "characters": {"主角": {"age": 7}},
            "share_count": 3, "nested": {"a": [{"b": [True, None, 1.5]}]},
        }
        led.upsert_creative("x", "半圈水膜", payload=payload)
        back = json.loads(led.get_creative("x")["payload"])
        self.assertEqual(back, payload)
        self.assertEqual(set(back.keys()), set(payload.keys()))

    def test_upsert_update_keeps_birth(self):
        """同 id 再 upsert：字段覆盖，created_at/created_seq 保留第一次的。"""
        led = self._open()
        p = _fake_project()
        led.upsert_creative(p["id"], p["title"], body="旧稿",
                            payload=p["payload"], created_at="2026-11-01 09:00:00")
        res = led.upsert_creative(p["id"], "新标题", status="finished",
                                  body="新稿", payload=p["payload"])
        self.assertEqual(res["action"], "update")
        row = led.get_creative(p["id"])
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["body"], "新稿")
        self.assertEqual(row["created_at"], "2026-11-01 09:00:00")
        self.assertEqual(row["created_seq"], 1)

    def test_list_creatives_lightweight(self):
        """列表只带 id/title/work_type/status/current_chars，不带正文/payload。"""
        led = self._open()
        for i in (1, 2):
            p = _fake_project(i)
            led.upsert_creative(p["id"], p["title"], work_type=p["work_type"],
                                status=p["status"], body=p["body"],
                                payload=p["payload"],
                                current_chars=p["current_chars"],
                                created_at="2026-11-0%d 08:00:00" % i)
        rows = led.list_creatives()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["id"] for r in rows}, {"proj01", "proj02"})
        for r in rows:  # 轻量字段：不许漏、不许多
            self.assertEqual(
                set(r.keys()),
                {"id", "title", "work_type", "status", "current_chars"})

    def test_get_missing_returns_none(self):
        led = self._open()
        self.assertIsNone(led.get_creative("不存在"))

    # ---------- 防御 ----------

    def test_upsert_rejects_empty_id_or_title(self):
        led = self._open()
        with self.assertRaises(L.LedgerError):
            led.upsert_creative("", "有标题没id")
        with self.assertRaises(L.LedgerError):
            led.upsert_creative("p1", "   ")

    def test_upsert_rejects_unserializable_payload(self):
        """payload 序列化失败必须显式抛，不许静默写半截。"""
        led = self._open()
        with self.assertRaises(L.LedgerError):
            led.upsert_creative("p1", "标题", payload={"bad": {1, 2, 3}})
        self.assertIsNone(led.get_creative("p1"))


def _rewrite_dump_as_old(text, schema=3, drop=("creative",)):
    """把当前格式的 dump 文本改写成「旧版本备份」的样子。

    只做真备份当时真的会做的两件事：文件头版本与行数清单里没有 drop 的表，
    meta 里的 schema_version 跟着降级。其余行原样保留，保证行数清单自洽
    （load_jsonl 的严格校验不会因为改写着而报错）。
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    header = json.loads(lines[0])
    body = []
    for ln in lines[1:]:
        item = json.loads(ln)
        if item.get("t") == "__eof__" or item.get("t") in drop:
            continue
        if item.get("t") == "meta" and item["r"].get("k") == "schema_version":
            item["r"] = {"k": "schema_version", "v": str(schema)}
        body.append(item)
    tables = {k: v for k, v in (header.get("tables") or {}).items()
              if k not in drop}
    total = sum(int(v) for v in tables.values())
    header.update({"version": 3, "schema_version": schema,
                   "tables": tables, "rows_total": total})
    out = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    out += [json.dumps(b, ensure_ascii=False, sort_keys=True) for b in body]
    out.append(json.dumps({"t": "__eof__", "rows_total": total},
                          ensure_ascii=False, sort_keys=True))
    return "\n".join(out) + "\n"


class RestoreCreativeTest(unittest.TestCase):
    """替换式恢复对创作项目的处置（v6 安全隐患的回归测试）。

    背景：creative 已进 DUMP_TABLES，而 load_jsonl(replace=True) 过去按
    DUMP_TABLES 逐表 DELETE——拿一份不含 creative 的旧备份（结构 ≤ v5）
    做替换式恢复，会把本地所有创作正文静默删光。这里钉住三件事：
    旧备份不清空本地创作、warnings 如实报数、带 creative 的备份才整体替换。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _ledger(self, name="t.db"):
        led = L.Ledger(os.path.join(self.tmp, name))
        self.addCleanup(led.close)
        return led

    def _old_dump(self, schema=3, name="old.jsonl"):
        """造一份「旧版本」的备份：去掉 creative 段，文件头与 meta 一起降级。"""
        src = L.Ledger(os.path.join(self.tmp, "src.db"))
        try:
            src.remember("备份里的一条记忆", tags="甲")
            text = src.dump_jsonl()
        finally:
            src.close()
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_rewrite_dump_as_old(text, schema=schema))
        return path

    def test_replace_restore_with_v3_backup_keeps_local_creative(self):
        """结构 v3 的备份（不含 creative）做替换式恢复：本地创作一条不少。"""
        target = self._ledger()
        target.upsert_creative("p1", "本地作品一", body="正文一")
        target.upsert_creative("p2", "本地作品二", body="正文二")
        target.remember("本库原有的一条记忆")

        res = target.load_jsonl(self._old_dump(), replace=True)

        self.assertEqual(len(target.list_creatives()), 2,
                         "旧备份恢复把本地创作删光了")
        self.assertEqual(target.get_creative("p1")["body"], "正文一")
        self.assertEqual(target.get_creative("p2")["body"], "正文二")
        # 备份确实生效了：记忆换成备份里那一条
        self.assertEqual(res["memories"], 1)
        # warnings 必须如实报数，不能静默保留
        self.assertTrue(any("备份不含 creative 表" in w for w in res["warnings"]),
                        "旧备份恢复没提示 creative 缺失：%r" % (res["warnings"],))
        self.assertTrue(any("本地 2 条创作已保留" in w for w in res["warnings"]),
                        "没报保留了本地几条：%r" % (res["warnings"],))

    def test_replace_restore_with_v4_backup_replaces_creative(self):
        """带 creative 的备份（结构 v6 / 格式 v4）才整体替换，且不报保留。"""
        target = self._ledger()
        target.upsert_creative("old", "会被替换掉的")

        src = L.Ledger(os.path.join(self.tmp, "new.db"))
        try:
            src.upsert_creative("new", "备份里的新作品", body="新正文")
            path = os.path.join(self.tmp, "v4.jsonl")
            src.dump_jsonl(path)
        finally:
            src.close()

        res = target.load_jsonl(path, replace=True)

        self.assertEqual([r["id"] for r in target.list_creatives()], ["new"])
        self.assertEqual(target.get_creative("new")["body"], "新正文")
        self.assertIsNone(target.get_creative("old"))
        self.assertFalse([w for w in res["warnings"] if "creative" in w],
                         "带 creative 的备份不该报保留：%r" % (res["warnings"],))

    def test_replace_restore_without_local_creative_reports_zero(self):
        """旧备份 + 本地也没创作：文案要如实说是 0 条，不是「保留」了什么。"""
        target = self._ledger()
        res = target.load_jsonl(self._old_dump(name="old_zero.jsonl"), replace=True)
        self.assertEqual(target.list_creatives(), [])
        self.assertTrue(any("本地也没有创作项目" in w for w in res["warnings"]),
                        "%r" % (res["warnings"],))

    def test_empty_db_import_of_v3_backup_also_warns(self):
        """非替换导入（空库）也要提示：这份备份本来就没有创作项目。"""
        target = self._ledger()
        res = target.load_jsonl(self._old_dump(schema=5, name="v5.jsonl"))
        self.assertTrue(any("备份不含 creative 表" in w for w in res["warnings"]),
                        "%r" % (res["warnings"],))
        self.assertEqual(target.list_creatives(), [])


if __name__ == "__main__":
    unittest.main()

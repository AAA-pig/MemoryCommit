# -*- coding: utf-8 -*-
"""B 批 · 群聊支持（2026-11-15 施工单）自测。

覆盖：B1 来源会话接线（scope_key / recent_messages 按会话过滤）/
B2 说话人表 memory_speaker（进提交链、可反查）/ B3 画像可见性
profile_visibility（owner 私聊强制 private、群聊只注入 public）/
B4 坏感度 user_attitude（0-4 分级、owner 锁死、读失败按 0）/
B5 非 owner 私聊不进账本（群聊照常并带群号）。

约束：memory 主表一行一列不动，全部新表 + 引用；写入只发生在提交里。
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger as L  # noqa: E402

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER_UMO = "123:FriendMessage:1"
# owner 的群聊 UMO 也在名单里（owners.json 本来就支持配群：群聊场景
# 下 owner 的画像才能记成 public，B3 测试用它）
GROUP_UMO = "aiocqhttp:GroupMessage:889900"
STRANGER_UMO = "999:FriendMessage:42"
STRANGER_GROUP_UMO = "aiocqhttp:GroupMessage:777"

V8_TABLES = ("memory_speaker", "profile_visibility", "user_attitude")


# ==========================================================================
# ledger 层：表结构、迁移、提交链、读写接口
# ==========================================================================

class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.led.close()


class GroupSchemaTest(LedgerBase):
    # ---------- 版本与表结构 ----------

    def test_versions_bumped(self):
        self.assertEqual(L.SCHEMA_VERSION, 8)
        self.assertEqual(L.DUMP_VERSION, 6)
        self.assertEqual(L.CORE_API_VERSION, 4)
        self.assertEqual(self.led.schema_version(), 8)

    def test_new_tables_exist(self):
        names = {r["name"] for r in self.led._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for tbl in V8_TABLES:
            self.assertIn(tbl, names)

    def test_speaker_index_exists(self):
        idx = [r["name"] for r in self.led._conn.execute(
            "PRAGMA index_list(memory_speaker)")]
        self.assertIn("idx_speaker_id", idx)

    def test_migrate_7_to_8_keeps_old_data(self):
        """老库（v7）升上来：三张表由建表语句补齐，存量记忆一字不动。"""
        path = os.path.join(self.tmp, "v7.db")
        led = L.Ledger(path)
        led.remember("存量记忆一条，关于空调滤网")
        led.close()
        cx = sqlite3.connect(path)
        cx.execute("UPDATE meta SET v='7' WHERE k='schema_version'")
        for tbl in V8_TABLES:
            cx.execute("DROP TABLE %s" % tbl)
        cx.commit()
        cx.close()
        led2 = L.Ledger(path)
        try:
            self.assertEqual(led2.schema_version(), 8)
            self.assertEqual(led2.stats()["memories"], 1)
            for tbl in V8_TABLES:
                self.assertEqual(
                    led2._query_one("SELECT COUNT(*) AS n FROM %s" % tbl)["n"], 0)
        finally:
            led2.close()

    def test_memory_main_table_untouched(self):
        """B 批硬约束：memory 主表结构不许动（列集合与 v7 完全一致）。"""
        cols = [r["name"] for r in self.led._conn.execute(
            "PRAGMA table_info(memory)")]
        self.assertEqual(cols, ["id", "level", "grade", "importance", "summary",
                                "tags", "scope_key", "start_time", "end_time",
                                "created_seq", "last_access_seq", "access_count",
                                "created_at", "updated_at"])


class SpeakerTest(LedgerBase):
    # ---------- B2 说话人 ----------

    def test_remember_with_speaker_writes_chain(self):
        res = self.led.remember("猫把杯子碰下桌了", speaker_id="10086",
                                speaker_name="小张")
        rows = self.led.speakers_of(res["memory_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["speaker_id"], "10086")
        self.assertEqual(rows[0]["speaker_name"], "小张")
        self.assertTrue(rows[0]["created_at"])
        commit = self.led.show_commit(res["commit"])
        self.assertIn("memory_speaker", {r["tbl"] for r in commit["rows"]})

    def test_remember_without_speaker_no_row(self):
        res = self.led.remember("没头没尾的一条")
        self.assertEqual(self.led.speakers_of(res["memory_id"]), [])

    def test_speaker_empty_id_skipped(self):
        with self.led.commit("空说话人不写行") as c:
            mid = c.add_memory("正文")
            c.add_speaker(mid, "   ", "无名")
        self.assertEqual(self.led.speakers_of(mid), [])

    def test_speaker_guards(self):
        c = self.led.commit("空记忆 id")
        with self.assertRaises(L.LedgerError):
            c.add_speaker("", "u1", "无名")
        self.assertEqual(self.led.speaker_memories(""), [])
        self.assertEqual(self.led.speaker_memories("   "), [])

    def test_speaker_name_truncated_to_64(self):
        res = self.led.remember("长名字", speaker_id="u1", speaker_name="名" * 70)
        self.assertEqual(
            len(self.led.speakers_of(res["memory_id"])[0]["speaker_name"]), 64)

    def test_speaker_upsert_updates_name(self):
        with self.led.commit("直连 builder") as c:
            mid = c.add_memory("记忆正文")
            c.add_speaker(mid, "u1", "名字甲")
            c.add_speaker(mid, "u1", "名字乙")
        rows = self.led.speakers_of(mid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["speaker_name"], "名字乙")

    def test_speaker_memories_reverse_lookup(self):
        r1 = self.led.remember("第一条，关于空调", speaker_id="u1")
        self.led.remember("别人的事", speaker_id="u2")
        r3 = self.led.remember("第三条，也是空调", speaker_id="u1")
        got = self.led.speaker_memories("u1")
        self.assertEqual([m["memory_id"] for m in got],
                         [r3["memory_id"], r1["memory_id"]])
        self.assertEqual(self.led.speaker_memories("u404"), [])

    def test_speaker_survives_compact_and_replay(self):
        for i in range(15):
            self.led.remember("沉淀 %d 号，关于空调滤网" % i, speaker_id="u1")
        self.led.compact(keep_recent=3)
        mids = [m["memory_id"] for m in self.led.speaker_memories("u1")]
        self.assertEqual(len(mids), 15)
        for mid in mids[:3]:
            self.assertEqual(len(self.led.speakers_of(mid)), 1)
        hc = self.led.health_check()
        self.assertTrue(hc["ok"], hc["problems"])

    def test_speaker_health_projection_reconciled(self):
        self.led.remember("有说话人的记忆", speaker_id="u7", speaker_name="七号")
        hc = self.led.health_check()
        self.assertTrue(hc["ok"], hc["problems"])

    def test_speaker_survives_dump_load(self):
        r = self.led.remember("搬家不丢说话人", speaker_id="u9", speaker_name="老九")
        path = os.path.join(self.tmp, "dump.jsonl")
        self.led.dump_jsonl(path)
        led2 = L.Ledger(os.path.join(self.tmp, "new.db"))
        try:
            led2.load_jsonl(path)
            rows = led2.speakers_of(r["memory_id"])
            self.assertEqual([s["speaker_name"] for s in rows], ["老九"])
        finally:
            led2.close()

    def test_remember_speaker_invalid_id_rejected(self):
        with self.assertRaises(L.LedgerError):
            self.led.remember("内容", speaker_id="a" * 65)


class ProfileVisibilityTest(LedgerBase):
    # ---------- B3 画像可见性 ----------

    def test_remember_profile_visibility(self):
        pid_pub = self.led.remember_profile(
            "master", "喜欢 26 度", visibility="public")["profile_id"]
        pid_pri = self.led.remember_profile(
            "master", "怕冷", visibility="private")["profile_id"]
        pid_no = self.led.remember_profile("master", "没标可见性")["profile_id"]
        self.assertEqual(self.led.profile_visibility_of(pid_pub), "public")
        self.assertEqual(self.led.profile_visibility_of(pid_pri), "private")
        self.assertEqual(self.led.profile_visibility_of(pid_no), "private")

    def test_remember_profile_invalid_visibility(self):
        with self.assertRaises(L.LedgerError):
            self.led.remember_profile("master", "乱值", visibility="friends")

    def test_profiles_filter_public(self):
        self.led.remember_profile("master", "公开事实", visibility="public")
        self.led.remember_profile("master", "私密事实", visibility="private")
        self.led.remember_profile("master", "缺省事实")
        pub = self.led.profiles("master", visibility="public")
        self.assertEqual([p["fact"] for p in pub], ["公开事实"])
        self.assertEqual(len(self.led.profiles("master")), 3)
        self.assertEqual(len(self.led.profiles()), 3)
        # 不限 user 的公开口径：群聊注入链路的另一个入口
        pub_all = self.led.profiles(visibility="public")
        self.assertEqual([p["fact"] for p in pub_all], ["公开事实"])

    def test_find_profile_scans_in_memory_rows(self):
        """同一提交内先写两条画像再对第二条设可见性：内存快照扫到第二条。"""
        with self.led.commit("同提交画像") as c:
            pid1 = c.upsert_profile("master", "第一条")
            pid2 = c.upsert_profile("master", "第二条")
            self.assertEqual(c.set_profile_visibility(pid2, "public"), "public")
            with self.assertRaises(L.LedgerError):
                c.set_profile_visibility("p404", "public")
        self.assertEqual(self.led.profile_visibility_of(pid2), "public")
        self.assertEqual(self.led.profile_visibility_of(pid1), "private")

    def test_profiles_visibility_invalid(self):
        with self.assertRaises(L.LedgerError):
            self.led.profiles("master", visibility="everyone")

    def test_set_profile_visibility_upsert(self):
        pid = self.led.remember_profile(
            "master", "先公开", visibility="public")["profile_id"]
        res = self.led.set_profile_visibility(pid, "private")
        self.assertEqual(res["visibility"], "private")
        self.assertEqual(self.led.profile_visibility_of(pid), "private")
        self.led.set_profile_visibility(pid, "public")
        rows = self.led._query_all(
            "SELECT * FROM profile_visibility WHERE profile_id = ?", (pid,))
        self.assertEqual(len(rows), 1)
        with self.assertRaises(L.LedgerError):
            self.led.set_profile_visibility("p404", "public")
        with self.assertRaises(L.LedgerError):
            self.led.set_profile_visibility(pid, "family")

    def test_activate_public_profile_only(self):
        self.led.remember_profile("master", "公开：喜欢 26 度", visibility="public")
        self.led.remember_profile("master", "私密：怕冷", visibility="private")
        res = self.led.activate("空调", slots=1, with_profile=True,
                                profile_visibility="public")
        self.assertEqual([p["fact"] for p in res["profiles"]],
                         ["公开：喜欢 26 度"])
        res_all = self.led.activate("空调", slots=1, with_profile=True)
        self.assertIn("私密：怕冷", [p["fact"] for p in res_all["profiles"]])

    def test_activate_profile_visibility_invalid(self):
        with self.assertRaises(L.LedgerError):
            self.led.activate("x", with_profile=True, profile_visibility="everyone")

    def test_activate_without_profile_ignores_visibility(self):
        res = self.led.activate("x", with_profile=False,
                                profile_visibility="public")
        self.assertEqual(res["profiles"], [])


class AttitudeTest(LedgerBase):
    # ---------- B4 坏感度 ----------

    def test_attitude_roundtrip_and_chain(self):
        res = self.led.set_attitude("10086", 2, "群里刷屏")
        self.assertEqual(res["level"], 2)
        self.assertEqual(self.led.get_attitude("10086"), 2)
        self.led.set_attitude("10086", 4, "升级了")
        self.assertEqual(self.led.get_attitude("10086"), 4)
        row = self.led._query_one(
            "SELECT * FROM user_attitude WHERE user_id = ?", ("10086",))
        self.assertEqual(row["reason"], "升级了")
        self.assertTrue(row["updated_at"])
        commit = self.led.show_commit(self.led.head())
        self.assertIn("user_attitude", {r["tbl"] for r in commit["rows"]})

    def test_attitude_zero_is_valid_level(self):
        self.led.set_attitude("10086", 3, "降下去")
        self.led.set_attitude("10086", 0, "又拉回来")
        self.assertEqual(self.led.get_attitude("10086"), 0)

    def test_attitude_validation(self):
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("u", 5)
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("u", -1)
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("", 2)
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("u", "many")
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("a" * 65, 2)

    def test_attitude_master_locked(self):
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude("master", 1)
        with self.assertRaises(L.LedgerError):
            self.led.set_attitude(" master ", 1)
        # builder 层也拦：不进 with（不封链），直接构造
        c = self.led.commit("直连 builder")
        with self.assertRaises(L.LedgerError):
            c.set_attitude("master", 1)

    def test_get_attitude_missing_or_error_is_zero(self):
        self.assertEqual(self.led.get_attitude("u404"), 0)
        orig = self.led._query_one

        def boom(sql, params=()):
            raise RuntimeError("库坏了")

        self.led._query_one = boom
        try:
            self.assertEqual(self.led.get_attitude("u1"), 0)
        finally:
            self.led._query_one = orig

    def test_attitude_survives_dump_load(self):
        self.led.set_attitude("10086", 2, "刷屏")
        path = os.path.join(self.tmp, "dump.jsonl")
        self.led.dump_jsonl(path)
        led2 = L.Ledger(os.path.join(self.tmp, "new.db"))
        try:
            led2.load_jsonl(path)
            self.assertEqual(led2.get_attitude("10086"), 2)
        finally:
            led2.close()

    def test_attitude_health_projection_reconciled(self):
        self.led.set_attitude("10086", 1, "吵")
        hc = self.led.health_check()
        self.assertTrue(hc["ok"], hc["problems"])


class DumpCompatTest(LedgerBase):
    # ---------- v8 表与备份兼容（creative 先例） ----------

    @staticmethod
    def _strip_v8_tables(text):
        """把一份 v8 备份改写成「不带三张 v8 表」的旧式备份。

        投影行与提交链里的 v8 页一并剥掉——真正的旧备份（格式 ≤ v5）
        两样都没有，链上根本不存在 v8 表的页。
        """
        lines = text.strip().split("\n")
        header = json.loads(lines[0])
        kept = []
        for ln in lines[1:-1]:
            item = json.loads(ln)
            if item.get("t") in V8_TABLES:
                continue
            if (item.get("t") == "commit_row"
                    and item.get("r", {}).get("tbl") in V8_TABLES):
                continue
            kept.append(ln)
        # 行数清单按实际保留的行重建（commit_row 被剥了 v8 页，计数跟着变）
        tables = {}
        for ln in kept:
            t = json.loads(ln).get("t")
            tables[t] = tables.get(t, 0) + 1
        header["tables"] = tables
        header["rows_total"] = len(kept)
        eof = json.dumps({"t": "__eof__", "rows_total": len(kept)},
                         ensure_ascii=False, sort_keys=True)
        return "\n".join([json.dumps(header, ensure_ascii=False, sort_keys=True)]
                         + kept + [eof]) + "\n"

    def test_old_backup_replace_keeps_v8_tables(self):
        r = self.led.remember("留下痕迹", speaker_id="u1")
        self.led.set_attitude("10086", 3, "spam")
        pid = self.led.remember_profile(
            "master", "事实", visibility="public")["profile_id"]
        old = self._strip_v8_tables(self.led.dump_jsonl(""))
        old_path = os.path.join(self.tmp, "old.jsonl")
        with open(old_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(old)
        res = self.led.load_jsonl(old_path, replace=True)
        self.assertEqual(self.led.speakers_of(r["memory_id"])[0]["speaker_id"],
                         "u1")
        self.assertEqual(self.led.get_attitude("10086"), 3)
        self.assertEqual(self.led.profile_visibility_of(pid), "public")
        self.assertTrue(any("v8" in w or "保留" in w for w in res["warnings"]),
                        res["warnings"])

    def test_new_backup_replace_swaps_v8_tables(self):
        self.led.set_attitude("10086", 3, "spam")
        self.led.remember_profile("master", "事实", visibility="public")
        path = os.path.join(self.tmp, "full.jsonl")
        self.led.dump_jsonl(path)
        led2 = L.Ledger(os.path.join(self.tmp, "other.db"))
        try:
            led2.set_attitude("10086", 1, "旧数据")
            led2.load_jsonl(path, replace=True)
            self.assertEqual(led2.get_attitude("10086"), 3)
        finally:
            led2.close()

    def test_dump_header_carries_v8_counts(self):
        self.led.set_attitude("u1", 2, "吵")
        header = json.loads(self.led.dump_jsonl("").strip().split("\n")[0])
        for t in V8_TABLES:
            self.assertIn(t, header["tables"])


# ==========================================================================
# main 层：接线、门、命令
# ==========================================================================

class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _Req:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt


class _Event:
    def __init__(self, text="", umo=OWNER_UMO, sender_id="", sender_name=""):
        self._text = text
        self.unified_msg_origin = umo
        self.sender_id = sender_id
        self.sender_name = sender_name

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return text


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", OWNER_UMO + "," + GROUP_UMO)
    monkeypatch.setenv("JJ_MEMORY_MAINTAIN_EVERY", "20")
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main
    yield main
    sys.modules.pop("main", None)


@pytest.fixture()
def plugin(mod):
    return mod.MemoryCommitPlugin(None)


def _run(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def _cmd(plugin, name, text, umo=OWNER_UMO):
    return "\n".join(str(x) for x in _run(getattr(plugin, name)(_Event(text, umo=umo))))


class B1WiringTest:
    # ---------- B1 来源会话接线 ----------

    def test_recent_messages_session_filter(self, plugin):
        plugin.ledger.append_message("user", "A 群的话", session_id="a")
        plugin.ledger.append_message("user", "B 群的话", session_id="b")
        plugin.ledger.append_message("user", "A 群又说", session_id="a")
        rows = plugin.ledger.recent_messages(limit=10, session_id="a")
        assert [r["content"] for r in rows] == ["A 群的话", "A 群又说"]
        assert len(plugin.ledger.recent_messages(limit=10)) == 3

    def test_activate_gets_scope_key_from_event(self, mod, plugin, monkeypatch):
        seen = {}
        orig = plugin.ledger.activate

        def spy(query, **kw):
            seen.update(kw)
            return orig(query, **kw)

        monkeypatch.setattr(plugin.ledger, "activate", spy)
        asyncio.run(plugin.inject_memory(
            _Event("空调开 26 度", umo=GROUP_UMO), _Req("空调")))
        assert seen.get("scope_key") == GROUP_UMO
        assert seen.get("profile_visibility") == "public"

    def test_activate_owner_chat_no_visibility_filter(self, mod, plugin,
                                                      monkeypatch):
        seen = {}
        orig = plugin.ledger.activate

        def spy(query, **kw):
            seen.update(kw)
            return orig(query, **kw)

        monkeypatch.setattr(plugin.ledger, "activate", spy)
        asyncio.run(plugin.inject_memory(_Event("空调"), _Req("空调")))
        assert seen.get("scope_key") == OWNER_UMO
        assert seen.get("profile_visibility") == ""

    def test_auto_sink_scope_session_speaker(self, mod, plugin, monkeypatch):
        monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
        asyncio.run(plugin.inject_memory(
            _Event("空调开 26 度", umo=GROUP_UMO, sender_id="10086",
                   sender_name="群友甲"), _Req("空调开 26 度")))
        asyncio.run(plugin.record_reply(
            _Event("好", umo=GROUP_UMO, sender_id="10086",
                   sender_name="群友甲"), _Resp("好")))
        row = plugin.ledger._query_one(
            "SELECT id, scope_key FROM memory WHERE tags LIKE '%type:auto_sink%'")
        assert row["scope_key"] == GROUP_UMO
        sp = plugin.ledger.speakers_of(row["id"])
        assert sp[0]["speaker_id"] == "10086"
        assert sp[0]["speaker_name"] == "群友甲"

    def test_auto_sink_without_sender_still_sinks(self, mod, plugin,
                                                  monkeypatch):
        monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
        asyncio.run(plugin.inject_memory(
            _Event("空调开 26 度"), _Req("空调开 26 度")))
        asyncio.run(plugin.record_reply(_Event("好"), _Resp("好")))
        row = plugin.ledger._query_one(
            "SELECT id FROM memory WHERE tags LIKE '%type:auto_sink%'")
        assert row
        assert plugin.ledger.speakers_of(row["id"]) == []

    def test_group_scope_lands_on_memory(self, plugin):
        plugin.ledger.remember("群友说空调开 26 度", scope_key=GROUP_UMO)
        row = plugin.ledger._query_one(
            "SELECT scope_key FROM memory WHERE scope_key != ''")
        assert row["scope_key"] == GROUP_UMO


class B2MainTest:
    def test_auto_sink_sender_missing_fields(self, mod, plugin, monkeypatch):
        monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
        ev = _Event("空调开 26 度", umo=GROUP_UMO, sender_id="10086")
        asyncio.run(plugin.inject_memory(ev, _Req("空调开 26 度")))
        asyncio.run(plugin.record_reply(ev, _Resp("好")))
        row = plugin.ledger._query_one(
            "SELECT id FROM memory WHERE tags LIKE '%type:auto_sink%'")
        sp = plugin.ledger.speakers_of(row["id"])
        assert sp[0]["speaker_id"] == "10086"
        assert sp[0]["speaker_name"] == ""


class B3MainTest:
    def test_memory_profile_private_forced_in_friend_chat(self, plugin):
        out = asyncio.run(plugin.memory_profile(
            _Event(umo=OWNER_UMO), "喜欢 26 度", visibility="public"))
        assert "私密" in out
        row = plugin.ledger._query_one("SELECT visibility FROM profile_visibility")
        assert row["visibility"] == "private"

    def test_memory_profile_public_in_group(self, plugin):
        out = asyncio.run(plugin.memory_profile(
            _Event(umo=GROUP_UMO), "喜欢 26 度", visibility="public"))
        assert "画像已更新" in out
        row = plugin.ledger._query_one("SELECT visibility FROM profile_visibility")
        assert row["visibility"] == "public"

    def test_memory_profile_group_defaults_private(self, plugin):
        asyncio.run(plugin.memory_profile(_Event(umo=GROUP_UMO), "默认口径"))
        row = plugin.ledger._query_one("SELECT visibility FROM profile_visibility")
        assert row["visibility"] == "private"

    def test_memory_profile_invalid_visibility(self, plugin):
        out = asyncio.run(plugin.memory_profile(
            _Event(umo=GROUP_UMO), "事实", visibility="family"))
        assert "没记住" in out


class B4CommandTest:
    def test_attitude_usage_and_query(self, plugin):
        assert "用法" in _cmd(plugin, "cmd_attitude", "/坏感度")
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086")
        assert "10086" in out and "0" in out

    def test_attitude_set_then_query(self, plugin):
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086 3 刷屏")
        assert "3" in out
        assert plugin.ledger.get_attitude("10086") == 3
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086")
        assert "刷屏" in out

    def test_attitude_set_without_reason(self, plugin):
        _cmd(plugin, "cmd_attitude", "/坏感度 10086 2")
        assert plugin.ledger.get_attitude("10086") == 2

    def test_attitude_invalid_level(self, plugin):
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086 9")
        assert "0-4" in out
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086 many")
        assert "整数" in out

    def test_attitude_master_denied(self, plugin):
        out = _cmd(plugin, "cmd_attitude", "/坏感度 master 2")
        assert "主人" in out

    def test_attitude_deny_non_owner(self, plugin):
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086 2",
                   umo=STRANGER_UMO)
        assert "不可用" in out

    def test_attitude_write_failure_reported(self, plugin, monkeypatch):
        def boom(*a, **kw):
            raise L.LedgerError("写不进去")

        monkeypatch.setattr(plugin.ledger, "set_attitude", boom)
        out = _cmd(plugin, "cmd_attitude", "/坏感度 10086 2 吵")
        assert "没设成" in out

    def test_inject_log_records_attitude(self, mod, plugin):
        plugin.ledger.set_attitude("10086", 2, "刷屏")
        asyncio.run(plugin.inject_memory(
            _Event("空调", umo=GROUP_UMO, sender_id="10086"), _Req("空调")))
        with open(mod.INJECT_LOG, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        assert any(l.get("attitude") == 2 for l in lines)

    def test_inject_log_attitude_defaults_zero(self, mod, plugin):
        asyncio.run(plugin.inject_memory(
            _Event("空调", umo=GROUP_UMO, sender_id="20010"), _Req("空调")))
        with open(mod.INJECT_LOG, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        assert any(l.get("attitude") == 0 for l in lines)


class B5GateTest:
    def test_is_group_discriminates(self, mod):
        assert mod._is_group(_Event(umo="aiocqhttp:GroupMessage:1"))
        assert not mod._is_group(_Event(umo="aiocqhttp:FriendMessage:1"))
        assert not mod._is_group(_Event(umo=""))

    def test_non_owner_friend_chat_fully_skipped(self, plugin):
        req = _Req("空调")
        asyncio.run(plugin.inject_memory(
            _Event("空调", umo=STRANGER_UMO), req))
        asyncio.run(plugin.record_reply(
            _Event("好", umo=STRANGER_UMO), _Resp("嗯")))
        assert plugin.ledger.message_stats()["count"] == 0
        assert plugin.ledger.stats()["memories"] == 0
        assert not (req.system_prompt or "").strip()

    def test_non_owner_group_chat_injects_and_logs(self, plugin):
        plugin.ledger.remember("群友说空调开 26 度", scope_key=STRANGER_GROUP_UMO)
        req = _Req("空调")
        asyncio.run(plugin.inject_memory(
            _Event("空调", umo=STRANGER_GROUP_UMO, sender_id="10086"), req))
        assert "26 度" in (req.system_prompt or "")
        asyncio.run(plugin.record_reply(
            _Event("好", umo=STRANGER_GROUP_UMO, sender_id="10086"), _Resp("收到")))
        assert plugin.ledger.message_stats()["count"] == 2

    def test_non_owner_group_message_chain_scoped(self, plugin):
        asyncio.run(plugin.inject_memory(
            _Event("空调开 26 度", umo=STRANGER_GROUP_UMO), _Req("空调开 26 度")))
        row = plugin.ledger._query_one(
            "SELECT session_id FROM message ORDER BY seq DESC LIMIT 1")
        assert row["session_id"] == STRANGER_GROUP_UMO

    def test_owner_friend_chat_unchanged(self, plugin):
        plugin.ledger.remember("私事一件", scope_key=OWNER_UMO)
        req = _Req("私事")
        asyncio.run(plugin.inject_memory(_Event("私事", umo=OWNER_UMO), req))
        assert "私事" in (req.system_prompt or "")
        asyncio.run(plugin.record_reply(_Event("好"), _Resp("嗯")))
        # owner 行为完全不变：唤起前落链 1 条 + 回复落链 1 条
        assert plugin.ledger.message_stats()["count"] == 2

    def test_commands_still_owner_only(self, plugin):
        out = _cmd(plugin, "cmd_attitude", "/坏感度", umo=STRANGER_UMO)
        assert "不可用" in out

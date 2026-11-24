# -*- coding: utf-8 -*-
"""沉淀水位线（sink watermark）与 /记忆补沉 的行为测试（G1）。

五点验收：水位跨实例持久化、anchor 进正文且开头不变、/记忆补沉
补齐并推进水位、自检含「沉淀：」行、关闭时明写关闭。
其余加固：开启自动沉淀不重沉历史、补沉多批推进/无待补/owner 拦截、
anchor 单条与超长会话截断；after_seq 实参 spy 断言（与 limit 窗口解耦，
防「水位初始化从链尾起算」被 limit 取窗巧合遮住）。
"""
import asyncio
import os
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UMO = "123:FriendMessage:1"


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", UMO)
    monkeypatch.setenv("JJ_MEMORY_MAINTAIN_EVERY", "20")
    monkeypatch.delenv("JJ_MEMORY_AUTO_SINK_EVERY", raising=False)
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main
    yield main
    sys.modules.pop("main", None)


@pytest.fixture()
def plugin(mod):
    return mod.MemoryCommitPlugin(None)


class _Event:
    def __init__(self, text="", umo=UMO):
        self._text = text
        self.unified_msg_origin = umo

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return text


class _Req:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt


class _Resp:
    def __init__(self, text=""):
        self.completion_text = text


def _run(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def _cmd(plugin, name, text=""):
    return "\n".join(str(x) for x in _run(getattr(plugin, name)(_Event(text))))


def _sink_rows(plugin):
    return plugin.ledger._query_one(
        "SELECT COUNT(*) AS c FROM memory WHERE tags LIKE '%type:auto_sink%'")["c"]


# -- 1. 水位跨实例持久化 ----------------------------------------------------

def test_watermark_persists_across_instances(mod, monkeypatch):
    """水位线存 meta：第一个实例沉到哪，第二个实例读到的就是哪。

    第二个实例化之后链尾已经更远（又写了新原文），水位也不能被
    重新初始化成新链尾——否则中间那段会被跳过不沉。
    实参断言（与 limit 解耦）：p2 真沉淀时也必须拿持久化水位 4
    当 after_seq——不是 0、也不是被重设成新链尾。
    """
    monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
    p1 = mod.MemoryCommitPlugin(None)
    # 第 1 轮只初始化水位（=当时链尾），第 2 轮沉出一条、水位推到 4
    for i in range(2):
        asyncio.run(p1.inject_memory(_Event("第 %d 轮：空调 26 度" % i),
                                     _Req("第 %d 轮" % i)))
        asyncio.run(p1.record_reply(_Event(), _Resp("好，记住了 %d" % i)))
    assert p1.ledger.meta_get(mod.SINK_SEQ_PREFIX + UMO) == "4"
    # 链尾又远了（新原文 seq5），第二个实例读到的仍是持久化水位 4
    p1.ledger.append_message("user", "新的一句", session_id=UMO)
    p2 = mod.MemoryCommitPlugin(None)
    assert p2._sink_watermark(UMO) == 4
    # 实参 spy：不看捞回了哪几行，只看 _auto_sink 传的 after_seq。
    # （上面直接调 _sink_watermark 只证明「读得到 4」，这一段才证明
    # 真沉淀路径用的是持久化水位，且没在换实例时被重设。）
    calls = []
    orig = p2.ledger.recent_messages

    def spy(limit=20, after_seq=0, session_id=""):
        calls.append(after_seq)
        return orig(limit=limit, after_seq=after_seq, session_id=session_id)

    monkeypatch.setattr(p2.ledger, "recent_messages", spy)
    asyncio.run(p2.inject_memory(_Event("新话"), _Req("新话")))
    asyncio.run(p2.record_reply(_Event(), _Resp("好")))
    assert calls == [4], "p2 应从持久化水位 4 捞，after_seq 实参是 %r" % calls


def test_auto_sink_no_rehistory_on_enable(mod, monkeypatch):
    """开启自动沉淀那一刻：历史原文不被重沉（水位从链尾起算）。

    水位在请求阶段（user 落链前）初始化为当时链尾：开启前的历史
    10 条不会被折成索引，本轮原文（seq 11-12）照常沉。"""
    monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
    p = mod.MemoryCommitPlugin(None)
    for i in range(10):
        p.ledger.append_message("user", "开启前的历史 %d" % i, session_id=UMO)
    asyncio.run(p.inject_memory(_Event("新话"), _Req("新话")))
    asyncio.run(p.record_reply(_Event(), _Resp("好")))
    assert _sink_rows(p) == 1
    assert p._sink_watermark(UMO) == 12
    row = p.ledger._query_one(
        "SELECT summary FROM memory WHERE tags LIKE '%type:auto_sink%'")
    assert "原文 seq 11–12" in row["summary"]
    assert "开启前的历史" not in row["summary"]


def test_auto_sink_after_seq_arg_is_chain_tail_not_zero(mod, monkeypatch):
    """水位初始化从链尾起算：只看 _auto_sink 传给 recent_messages 的实参。

    spy 记录调用实参，断言 after_seq = 开启时刻该会话的链尾（10）：
    不看捞回了哪几行、不依赖 limit 大小——就算 limit 大到能装下全部
    历史，也必须只从水位之后捞。把 _sink_watermark 的初始化回退成
    seq = 0，这里立刻红（上一版 test_auto_sink_no_rehistory_on_enable
    被 limit=2 恰好切在本轮两条上而遮住，本条与 limit 彻底解耦）。
    """
    monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 1)
    p = mod.MemoryCommitPlugin(None)
    for i in range(10):
        p.ledger.append_message("user", "开启前的历史 %d" % i, session_id=UMO)
    calls = []
    orig = p.ledger.recent_messages

    def spy(limit=20, after_seq=0, session_id=""):
        calls.append(after_seq)
        return orig(limit=limit, after_seq=after_seq, session_id=session_id)

    monkeypatch.setattr(p.ledger, "recent_messages", spy)
    asyncio.run(p.inject_memory(_Event("新话"), _Req("新话")))
    asyncio.run(p.record_reply(_Event(), _Resp("好")))
    assert calls, "_auto_sink 应调用 recent_messages"
    assert calls[-1] == 10, "after_seq 应为初始化时链尾 10，实际 %r" % calls[-1]
    assert 0 not in calls, "after_seq 出现 0：水位没从链尾起算，会重沉历史"


# -- 2. anchor 进正文且开头不变 ----------------------------------------------

def test_anchor_appended_and_head_unchanged(mod, plugin):
    rows = [
        {"seq": 3, "role": "user", "content": "空调开 26 度",
         "at": "2026-11-15T10:00:00"},
        {"seq": 4, "role": "assistant", "content": "好的，已调",
         "at": "2026-11-15T10:01:00"},
    ]
    anchor = plugin._sink_anchor(rows, UMO)
    text = mod.auto_sink_summary(rows, anchor=anchor)
    assert text.startswith("最近 2 条原文")
    assert "原文 seq 3–4" in text
    assert ("会话 %s" % UMO) in text
    assert "时间 2026-11-15 10:00→2026-11-15 10:01" in text


def test_anchor_single_seq_and_long_session(mod, plugin):
    one = plugin._sink_anchor(
        [{"seq": 7, "role": "user", "content": "单条",
          "at": "2026-11-15T11:00:00"}], UMO)
    assert "原文 seq 7" in one and "–" not in one
    long_sid = "会" * 45
    a = plugin._sink_anchor([{"seq": 1, "role": "user", "content": "x"}],
                            long_sid)
    assert long_sid not in a
    assert long_sid[:40] + "…" in a
    assert plugin._sink_anchor([], UMO) == ""


# -- 3. /记忆补沉 补齐并推进水位 ----------------------------------------------

def test_sink_backfill_catches_up_and_advances(mod, plugin):
    for i in range(5):
        plugin.ledger.append_message("user", "历史 %d" % i, session_id=UMO)
    out = _cmd(plugin, "cmd_sink_backfill", "/记忆补沉")
    assert "补沉完成：1 批 5 条" in out
    assert "水位 seq 0→5" in out
    assert plugin._sink_watermark(UMO) == 5
    row = plugin.ledger._query_one(
        "SELECT summary FROM memory WHERE tags LIKE '%type:auto_sink%'")
    assert row and row["summary"].startswith("最近 5 条原文")
    assert "原文 seq 1–5" in row["summary"]
    assert ("会话 %s" % UMO) in row["summary"]


def test_sink_backfill_multi_batch_then_finish(mod, plugin):
    for i in range(25):
        plugin.ledger.append_message("user", "历史 %d" % i, session_id=UMO)
    out = _cmd(plugin, "cmd_sink_backfill", "/记忆补沉 2")
    assert "补沉完成：2 批 25 条" in out
    assert "水位 seq 0→25" in out
    assert plugin._sink_watermark(UMO) == 25
    assert "没有待补的原文" in _cmd(plugin, "cmd_sink_backfill", "/记忆补沉")


def test_sink_backfill_oldest_first_and_budget_leftover(mod, plugin):
    """从最旧的没折过的开始折，预算用不完时不漏旧：水位停在已折处。"""
    for i in range(30):
        plugin.ledger.append_message("user", "历史 %d" % i, session_id=UMO)
    out = _cmd(plugin, "cmd_sink_backfill", "/记忆补沉 1")
    assert "补沉完成：1 批 20 条" in out
    row = plugin.ledger._query_one(
        "SELECT summary FROM memory WHERE tags LIKE '%type:auto_sink%'")
    assert "原文 seq 1–20" in row["summary"]
    assert plugin._sink_watermark(UMO) == 20
    out2 = _cmd(plugin, "cmd_sink_backfill", "/记忆补沉")
    assert "补沉完成：1 批 10 条" in out2
    assert "水位 seq 20→30" in out2


def test_sink_backfill_nothing_todo(mod, plugin):
    assert "没有待补的原文" in _cmd(plugin, "cmd_sink_backfill", "/记忆补沉")


def test_sink_backfill_owner_only(mod):
    p = mod.MemoryCommitPlugin(None)
    out = "\n".join(str(x) for x in _run(p.cmd_sink_backfill(
        _Event("/记忆补沉", umo="999:Stranger:1"))))
    assert out == mod.OWNER_DENY_TEXT


# -- 4. 自检含「沉淀：」行 ----------------------------------------------------

def test_health_shows_sink_line_closed(mod, plugin):
    plugin.ledger.append_message("user", "话 1", session_id=UMO)
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "沉淀：" in out
    assert "未建索引 1 条" in out
    assert "上次沉淀 无" in out


def test_health_shows_sink_line_enabled_and_last_sink(mod, plugin, monkeypatch):
    monkeypatch.setattr(mod, "AUTO_SINK_EVERY", 6)
    plugin.ledger.meta_set(mod.SINK_AT_META, "2026-11-15T10:00:00")
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "沉淀：每 6 轮" in out
    assert "上次沉淀 2026-11-15T10:00:00" in out


# -- 5. 关闭时明写关闭 --------------------------------------------------------

def test_closed_state_says_closed_explicitly(mod, plugin):
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "沉淀：关闭（JJ_MEMORY_AUTO_SINK_EVERY 未设）" in out


# -- 元数据方法（ledger 侧，给水位线用的封装） --------------------------------

def test_meta_get_set_roundtrip(mod, plugin):
    assert plugin.ledger.meta_get("不存在的键") == ""
    assert plugin.ledger.meta_get("不存在的键", default="兜底") == "兜底"
    plugin.ledger.meta_set("不存在的键", "值")
    assert plugin.ledger.meta_get("不存在的键") == "值"
    plugin.ledger.meta_set("不存在的键", "新值")
    assert plugin.ledger.meta_get("不存在的键") == "新值"


def test_message_session_stats_and_counts(mod, plugin):
    for i in range(3):
        plugin.ledger.append_message("user", "甲 %d" % i, session_id="A")
    for i in range(2):
        plugin.ledger.append_message("user", "乙 %d" % i, session_id="B")
    assert plugin.ledger.latest_message_seq() == 5
    sess = {s["session_id"]: s for s in plugin.ledger.message_sessions()}
    assert sess["A"]["n"] == 3 and sess["A"]["first_seq"] == 1
    assert sess["A"]["last_seq"] == 3
    assert sess["B"]["n"] == 2 and sess["B"]["last_seq"] == 5
    assert plugin.ledger.count_messages_after(0, session_id="A") == 3
    assert plugin.ledger.count_messages_after(3, session_id="A") == 0
    assert plugin.ledger.count_messages_after(0) == 5

# -*- coding: utf-8 -*-
"""main.py 外壳的全量行为测试（P0-4：命令面与 llm_tool 边界）。

这一份刻意把每条 /记忆* 指令都真跑一遍：以前它们一行都没被测到，
覆盖率卡在 50%。命令是用户真会碰到的面，不该靠「看起来没报错」交付。
"""
import asyncio
import json
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


def _run(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def _cmd(plugin, name, text=""):
    return "\n".join(str(x) for x in _run(getattr(plugin, name)(_Event(text))))


def _call(fn, *a, **kw):
    return asyncio.run(fn(_Event(), *a, **kw))


# -- /记忆 空账本与检索 ----------------------------------------------------

def test_memory_status_on_empty(plugin):
    out = _cmd(plugin, "cmd_memory", "/记忆")
    assert "记忆账本" in out and "HEAD" in out


def test_memory_no_hit(plugin):
    out = _cmd(plugin, "cmd_memory", "/记忆 量子纠缠")
    assert "没有" in out


def test_memory_hits_with_all_tags(mod, plugin, monkeypatch):
    """三种命中标记（graph / clue / direct+clue）都要走一遍 _fmt_hit。"""
    rows = [
        {"id": "m1", "summary": "顺着线索想起来的", "grade": 1, "score": 0.5,
         "match": "graph:空调", "clue_boost": 0.4},
        {"id": "m2", "summary": "只有线索撞上", "grade": 0, "score": 0.4,
         "match": "clue:偏好"},
        {"id": "m3", "summary": "词面加线索", "grade": 2, "score": 0.9,
         "match": "direct+clue", "clue_boost": 0.2},
    ]
    monkeypatch.setattr(plugin.ledger, "recall", lambda *a, **kw: rows)
    out = _cmd(plugin, "cmd_memory", "/记忆 空调")
    assert "↗" in out and "⌁" in out and "线索 +40%" in out


# -- /记忆链 ---------------------------------------------------------------

def test_history_empty(plugin):
    assert "还没有任何提交" in _cmd(plugin, "cmd_history", "/记忆链")


def test_history_rows(plugin):
    plugin.ledger.remember("用户喜欢安静")
    out = _cmd(plugin, "cmd_history", "/记忆链")
    assert "最近提交" in out and "C1" in out


# -- /记忆冲刷 -------------------------------------------------------------

def _make_flushable(plugin, grade=1, n_extra=5):
    first = plugin.ledger.remember("早年的一件小事", grade=grade)
    for i in range(n_extra):
        plugin.ledger.remember("新事 %d" % i)
    return first["memory_id"]


def test_flush_nothing_to_flush(plugin):
    plugin.ledger.remember("刚写下的")
    assert "全是 0" in _cmd(plugin, "cmd_flush", "/记忆冲刷")


def test_flush_plan_lists_items(plugin):
    _make_flushable(plugin)
    out = _cmd(plugin, "cmd_flush", "/记忆冲刷")
    assert "待处理" in out and "降级" in out


def test_flush_confirm_applies(plugin):
    mid = _make_flushable(plugin)
    out = _cmd(plugin, "cmd_flush", "/记忆冲刷 确认")
    assert "冲完了" in out
    row = plugin.ledger._query_one("SELECT grade FROM memory WHERE id=?", (mid,))
    assert row["grade"] == 0


def test_flush_confirm_nothing_applied(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "flush", lambda **kw: {"applied": False})
    assert "没什么要冲" in _cmd(plugin, "cmd_flush", "/记忆冲刷 确认")


def test_flush_plan_overflow_note(plugin, monkeypatch):
    plan = [{"memory_id": "m%d" % i, "action": "delete", "flush_amount": 9,
             "pressure": 2.0, "summary": "事 %d" % i} for i in range(10)]
    monkeypatch.setattr(plugin.ledger, "flush_plan", lambda: plan)
    out = _cmd(plugin, "cmd_flush", "/记忆冲刷")
    assert "还有 2 条" in out


# -- /记忆容量 -------------------------------------------------------------

def test_working_plan_empty(plugin):
    out = _cmd(plugin, "cmd_working", "/记忆容量")
    assert "工作记忆" in out and "还没有一级记忆" in out


def test_working_plan_capacity_off(plugin):
    plugin.ledger.working_capacity = 0
    out = _cmd(plugin, "cmd_working", "/记忆容量")
    assert "容量关着" in out


def test_working_plan_overflow_and_promote(plugin):
    plugin.ledger.working_capacity = 1
    plugin.ledger.remember("一级的事", grade=1)
    plugin.ledger.remember("另一件一级", grade=1)
    out = _cmd(plugin, "cmd_working", "/记忆容量")
    assert "超员" in out


def test_working_set_number_sets_capacity(plugin):
    out = _cmd(plugin, "cmd_working", "/记忆容量 5")
    assert plugin.ledger.working_capacity == 5
    assert "个名额" in out and "0/5" in out


def test_working_confirm_applies(plugin):
    plugin.ledger.working_capacity = 0
    monkeypatch_plan = {"capacity": 0, "occupied": 2, "demote": [], "promote": [],
                        "keep": [], "overflow": 0}
    plugin.ledger.plan_working_set = lambda *a, **kw: dict(monkeypatch_plan)
    plugin.ledger.rebalance_working_set = lambda *a, **kw: {
        "applied": True, "demoted": ["m1"], "promoted": [], "commit": "C9"}
    assert "换完班" in _cmd(plugin, "cmd_working", "/记忆容量 确认")


def test_working_confirm_nothing(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "rebalance_working_set",
                        lambda *a, **kw: {"applied": False})
    assert "不用换班" in _cmd(plugin, "cmd_working", "/记忆容量 确认")


# -- /记忆压缩 -------------------------------------------------------------

def test_compact_reports_nothing(plugin):
    plugin.ledger.remember("只有一条")
    assert "不用压缩" in _cmd(plugin, "cmd_compact", "/记忆压缩")


def test_compact_applies_and_blocks(plugin, monkeypatch):
    for i in range(13):
        plugin.ledger.remember("记忆 %d" % i)
    out = _cmd(plugin, "cmd_compact", "/记忆压缩 10")
    assert "压好了" in out

    from ledger import LedgerError

    def boom(**kw):
        raise LedgerError("压不了")

    monkeypatch.setattr(plugin.ledger, "compact", boom)
    assert "没压成" in _cmd(plugin, "cmd_compact", "/记忆压缩 3")


# -- /记忆导出 / 备份 / 自检 ------------------------------------------------

def test_export_writes_file(plugin):
    plugin.ledger.remember("导出用的一条")
    out = _cmd(plugin, "cmd_export", "/记忆导出")
    assert "导出好了" in out


def test_backup_default_and_custom_name(plugin, tmp_path):
    """备份只能落专用目录（R1）：默认时间戳名，参数只当文件名用。"""
    plugin.ledger.remember("备份用的一条")
    assert "备份好了" in _cmd(plugin, "cmd_backup", "/记忆备份")
    assert os.path.isdir(str(tmp_path / "backup"))
    out = _cmd(plugin, "cmd_backup", "/记忆备份 自定义名")
    assert os.path.exists(str(tmp_path / "backup" / "自定义名.jsonl"))
    assert "结构版本" in out


def test_backup_failure(plugin):
    plugin.ledger.remember("备份失败也要说人话")
    out = _cmd(plugin, "cmd_backup", "/记忆备份 bad\x00name.jsonl")
    assert "没备份成" in out


def test_health_ok(plugin):
    plugin.ledger.remember("自检用的一条")
    plugin.ledger.append_message("user", "自检原话")
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "通过" in out and "写路径异常 0 次：无" in out


def test_health_shows_failure_counts(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("写不进去")

    monkeypatch.setattr(plugin.ledger, "health_check", lambda *a, **kw: {
        "ok": True, "problems": [], "export_ok": True,
        "message_chain": {"ok": True, "count": 0},
        "stats": plugin.ledger.stats()})
    plugin._note_failure("回复落链失败", RuntimeError("炸了"))
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "写路径异常 1 次：回复落链失败 1 次" in out


def test_health_reports_problems(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "health_check", lambda *a, **kw: {
        "ok": False, "problems": ["链断了"], "export_ok": False,
        "message_chain": {"ok": False, "count": 3},
        "stats": plugin.ledger.stats()})
    out = _cmd(plugin, "cmd_health", "/记忆自检")
    assert "有问题" in out and "链断了" in out and "断裂" in out


# -- /记忆原话 -------------------------------------------------------------

def test_raw_empty_and_truncated(plugin, monkeypatch):
    plugin.ledger.append_message("user", "今天下雨")
    monkeypatch.setattr("ledger.SEARCH_CANDIDATE_CAP", 1)
    monkeypatch.setattr("ledger.SEARCH_EXPANDED_CAP", 1)
    plugin.ledger.append_message("user", "今天晴天")
    plugin.ledger.append_message("assistant", "是晴的")
    out = _cmd(plugin, "cmd_raw", "/记忆原话 找不到的词")
    assert "没有" in out and "更早" in out


def test_raw_no_keyword_tip(plugin):
    assert "原文链里没有" in _cmd(plugin, "cmd_raw", "/记忆原话")


def test_raw_rows(plugin):
    plugin.ledger.append_message("user", "空调开 26 度")
    plugin.ledger.append_message("assistant", "好，26 度")
    out = _cmd(plugin, "cmd_raw", "/记忆原话 空调")
    assert "原文链共 2 条" in out and "26 度" in out


# -- /记忆访问 -------------------------------------------------------------

def test_access_empty(plugin):
    assert "还没有访问痕迹" in _cmd(plugin, "cmd_access", "/记忆访问 m1")


def test_access_rows_with_seed_and_query(plugin):
    res = plugin.ledger.remember("被想起过的")
    mid = res["memory_id"]
    plugin.ledger.touch(mid, query="空调")
    with plugin.ledger._tx() as cx:
        cx.execute("INSERT INTO access_log(memory_id, at, seq, query, how)"
                   " VALUES(?,?,?,?,?)", (mid, "2026-01-01T00:00:00", 0, "", "seed:3"))
    out = _cmd(plugin, "cmd_access", "/记忆访问 " + mid)
    assert "因为「空调」" in out and "迁移前累计 3 次" in out


# -- /记忆唤起 -------------------------------------------------------------

def test_activate_empty(plugin):
    assert "什么都想不起来" in _cmd(plugin, "cmd_activate", "/记忆唤起")


def test_activate_with_profile(plugin):
    plugin.ledger.remember("用户怕冷，空调开 26 度", tags="偏好")
    plugin.ledger.remember_profile("master", "用户怕冷", category="preference")
    out = _cmd(plugin, "cmd_activate", "/记忆唤起 空调")
    assert "路线" in out and "用户的事" in out and "真要说出口" in out


# -- llm_tool：非 owner 全部返回不可用 --------------------------------------

def test_tools_reject_non_owner(plugin):
    event = _Event(umo="999:GroupMessage:1")
    for coro in (
        plugin.memory_write(event, "内容"),
        plugin.memory_search(event, "空调"),
        plugin.memory_forget(event, "m1"),
        plugin.memory_deny(event, "m1", "记错了"),
        plugin.memory_profile(event, "怕冷"),
    ):
        assert "不可用" in asyncio.run(coro)


def test_tool_happy_paths(plugin):
    assert _call(plugin.memory_write, "用户喜欢安静", tags="偏好").startswith("已写入")
    assert "m1" in _call(plugin.memory_search, "安静")
    assert _call(plugin.memory_deny, "m1", "记错了").startswith("已标记")
    assert _call(plugin.memory_profile, "怕冷", "preference").startswith("画像已更新")
    assert _call(plugin.memory_forget, "m1").startswith("已遗忘")
    assert "没有相关" in _call(plugin.memory_search, "量子")


# -- 注入 ------------------------------------------------------------------

class _Req:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt


def test_inject_skips_non_owner(plugin):
    req = _Req("空调 26 度")
    asyncio.run(plugin.inject_memory(_Event("空调 26 度", umo="999:Group:1"), req))
    assert req.system_prompt == ""


def test_inject_empty_prompt_logs(plugin):
    import main as m
    asyncio.run(plugin.inject_memory(_Event(""), _Req("")))
    assert "empty_prompt" in m.INJECT_LOG.read_text(encoding="utf-8")


def test_inject_normal_and_long_prompt(plugin):
    plugin.ledger.remember("用户怕冷，空调开 26 度", tags="偏好")
    req = _Req("空调多少度")
    asyncio.run(plugin.inject_memory(_Event("空调多少度"), req))
    assert "记忆账本" in req.system_prompt

    req2 = _Req("很长的上下文" * 60)
    asyncio.run(plugin.inject_memory(_Event("空调"), req2))
    log = __import__(plugin.__class__.__module__).INJECT_LOG
    assert "long_prompt" in log.read_text(encoding="utf-8")


def test_inject_records_denial_suspect(plugin):
    asyncio.run(plugin.inject_memory(_Event("你记错了，我没说过"), _Req("你记错了")))
    assert plugin.ledger.pending_denial_queue()


def test_inject_append_failure_is_counted(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("链断了")

    monkeypatch.setattr(plugin.ledger, "append_message", boom)
    req = _Req("空调")
    asyncio.run(plugin.inject_memory(_Event("空调"), req))
    assert plugin.failures.counts["原文落链失败"] == 1


def test_inject_activate_failure_is_counted(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("唤起炸了")

    monkeypatch.setattr(plugin.ledger, "activate", boom)
    asyncio.run(plugin.inject_memory(_Event("空调"), _Req("空调")))
    assert plugin.failures.counts["唤起失败"] == 1


def test_inject_maintenance_failures_are_counted(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("维护炸了")

    monkeypatch.setattr(plugin.ledger, "maybe_export_daily", boom)
    monkeypatch.setattr(plugin.ledger, "maybe_rebalance_working_set", boom)
    monkeypatch.setattr(plugin.ledger, "maybe_checkpoint_wal", boom)
    asyncio.run(plugin.inject_memory(_Event("空调"), _Req("空调")))
    for kind in ("每日导出失败", "工作记忆换班失败", "WAL 截断失败"):
        assert plugin.failures.counts[kind] == 1


def test_inject_profile_refresh_sets_timestamp(plugin):
    plugin.ledger.remember_profile("master", "用户怕冷")
    asyncio.run(plugin.inject_memory(_Event("空调"), _Req("空调")))
    assert plugin._profile_injected_at > 0


def test_log_injection_rotation(plugin):
    import main as m
    backup_path = m.INJECT_LOG.with_suffix(".1.jsonl")
    m.INJECT_LOG.parent.mkdir(parents=True, exist_ok=True)
    m.INJECT_LOG.write_text("old\n", encoding="utf-8")
    plugin._inject_lines = m.INJECT_LOG_ROTATE_LINES + 1
    plugin._log_injection("injected", query_len=3)
    assert backup_path.exists()
    assert plugin._inject_lines == 1


def test_log_injection_rotation_unlink_failure(plugin, monkeypatch):
    import main as m
    backup_path = m.INJECT_LOG.with_suffix(".1.jsonl")
    if backup_path.exists():
        backup_path.unlink()
    backup_path.mkdir(parents=True, exist_ok=True)  # 目录 → unlink 失败
    plugin._inject_lines = m.INJECT_LOG_ROTATE_LINES + 1
    plugin._log_injection("injected")
    assert plugin.failures.counts["注入日志轮转失败"] == 1


def test_log_injection_write_failure(plugin, monkeypatch):
    import main as m
    monkeypatch.setattr(m, "INJECT_LOG", m.INJECT_LOG.parent / "as-dir")
    (m.INJECT_LOG.parent / "as-dir").mkdir(exist_ok=True)
    plugin._log_injection("injected")
    assert plugin.failures.counts["注入日志写失败"] == 1


def test_init_tolerates_broken_inject_log(mod, monkeypatch):
    import main as m
    (m.INJECT_LOG).mkdir(parents=True, exist_ok=True)
    plugin = mod.MemoryCommitPlugin(None)
    assert plugin._inject_lines == 0
    plugin.ledger.close()


# -- 回复落链 + 自动沉淀 ---------------------------------------------------

class _Resp:
    def __init__(self, text):
        self.completion_text = text


def test_record_reply_skips_non_owner(plugin):
    asyncio.run(plugin.record_reply(_Event(umo="999:Group:1"), _Resp("好")))
    assert plugin.ledger.message_stats()["count"] == 0


def test_record_reply_empty_text(plugin):
    asyncio.run(plugin.record_reply(_Event(), _Resp("   ")))
    assert plugin.ledger.message_stats()["count"] == 0


def test_record_reply_failure_counted(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("写不进去")

    monkeypatch.setattr(plugin.ledger, "append_message", boom)
    asyncio.run(plugin.record_reply(_Event(), _Resp("我记住了")))
    assert plugin.failures.counts["回复落链失败"] == 1


def test_auto_sink_after_n_turns(mod, plugin, monkeypatch):
    """P0-1：连续聊满 N 轮，不调 memory_write 也要有记忆落库。"""
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 2)
    before = plugin.ledger.stats()
    for i in range(2):
        asyncio.run(plugin.inject_memory(_Event("第 %d 轮：空调开 26 度" % i),
                                         _Req("第 %d 轮" % i)))
        asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了 %d" % i)))
    after = plugin.ledger.stats()
    assert after["memories"] == before["memories"] + 1
    assert after["commits"] == before["commits"] + 1
    row = plugin.ledger._query_one("SELECT level, tags, summary FROM memory")
    assert row["level"] == "day" and "自动沉淀" in row["tags"]
    assert "关键词" in row["summary"]


def test_auto_sink_disabled(mod, plugin, monkeypatch):
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 0)
    assert plugin._auto_sink() == {}


def test_auto_sink_no_new_messages(mod, plugin, monkeypatch):
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    assert plugin._auto_sink() == {}


def test_auto_sink_failure_counted(plugin, monkeypatch):
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    plugin.ledger.append_message("user", "一句话")
    monkeypatch.setattr(plugin.ledger, "remember",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("写不进")))
    asyncio.run(plugin.record_reply(_Event(), _Resp("好")))
    assert plugin.failures.counts["自动沉淀失败"] == 1


def test_auto_sink_default_off_six_turns(tmp_path, monkeypatch):
    """N1：默认配置（不设环境变量）下跑 6 轮对话，memory 表行数不变。

    默认轮次已从 6 降到 0：不打字也涨不了记忆，要开必须显式设
    JJ_MEMORY_AUTO_SINK_EVERY。这里重新 import main，让默认值以
    干净的环境变量为准（不受宿主机环境残留影响）。
    """
    monkeypatch.delenv("JJ_MEMORY_AUTO_SINK_EVERY", raising=False)
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", UMO)
    monkeypatch.setenv("JJ_MEMORY_MAINTAIN_EVERY", "20")
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    try:
        import main as m
        assert m.AUTO_SINK_EVERY == 0
        plugin = m.MemoryCommitPlugin(None)
        before = plugin.ledger.stats()["memories"]
        for i in range(6):
            asyncio.run(plugin.inject_memory(_Event("第 %d 轮：空调开 26 度" % i),
                                             _Req("第 %d 轮" % i)))
            asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了 %d" % i)))
        assert plugin.ledger.stats()["memories"] == before
    finally:
        sys.modules.pop("main", None)


def test_auto_sink_tagged_and_excluded_from_injection(mod, plugin, monkeypatch):
    """N1：显式开启自动沉淀后，沉淀条目也不许当普通事实注入系统提示。

    走真实 inject_memory（on_llm_request 入口）断言 system_prompt，
    不是靠标签字符串猜：沉淀条目与普通条目都含「空调」词面、同走
    search 路线，修复后只有普通条目进提示词（对照组），证明排除靠
    类型标记语义，不是整条检索路线坏了。

    N2：标记只用来识别与检索，不进建图——memory_link 里不许出现
    concept 为 type:auto_sink 或「自动沉淀」的边（机器/管理标签
    不是语义概念）。
    """
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    plugin.ledger.remember("空调遥控器放在电视柜第二个抽屉")
    # 第一次请求只落链不唤起（空 prompt）：activate 会把对照组激活并进
    # 25 分钟冷却，第二次断言就验证不了「search 路线上普通条目照常注入」。
    asyncio.run(plugin.inject_memory(_Event("第 1 轮：空调开 26 度"), _Req("")))
    asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了")))
    row = plugin.ledger._query_one(
        "SELECT id, tags, summary FROM memory WHERE tags LIKE ?",
        ("%type:auto_sink%",))
    assert row, "沉淀条目没写进去，测试环境坏了"
    assert "自动沉淀" in row["tags"]
    assert "type:auto_sink" in row["tags"]
    # N2：断言建图结果而不是 tags 字符串——两个标记都不许当 concept
    edge = plugin.ledger._query_one(
        "SELECT concept FROM memory_link WHERE concept IN (?, ?)",
        (m.AUTO_SINK_TYPE_TAG, mod.AUTO_SINK_TAG))
    assert edge is None, "机器/管理标签不许当 concept 织边（N2）"
    req = _Req("空调")
    asyncio.run(plugin.inject_memory(_Event("空调"), req))
    prompt = req.system_prompt or ""
    assert "自动沉淀" not in prompt
    assert "电视柜" in prompt


def test_auto_sink_summary_has_no_literal_prefix(mod):
    """N9：沉淀正文不再以字面「自动沉淀：」开头。

    每条正文都以同一字面开头，「自动」「沉淀」的词面会在检索里命中
    一切沉淀条目，把普通事实挤出词面排序；「这是什么」由 tags 里的
    机器标记表达，正文不重复。
    """
    rows = [{"role": "user", "content": "空调开 26 度"},
            {"role": "assistant", "content": "好"}]
    text = mod.auto_sink_summary(rows)
    assert text
    assert not text.startswith("自动沉淀")
    assert "关键词" in mod.auto_sink_summary(
        [{"role": "user", "content": "空调开 26 度 空调滤网 空调遥控器"}])


def test_auto_sink_machine_tags_produce_no_concept_edges(mod, plugin, monkeypatch):
    """N2：机器/管理标签不参与建图——沉淀条目一条 concept 边也不织。

    探针与实测同形：5 条沉淀（走真实外壳路径，每轮 record_reply 攒一条）
    + 2 条共享语义标签「咖啡」的正常记忆。修复前 5 条沉淀自己织出
    20 条边（「自动沉淀」与 type:auto_sink 各 10 条），把「聊过什么」
    的原文索引抬成图谱上的语义同源；修复后沉淀条目 0 条边，而正常
    记忆的语义边照常——对照组断言防止「建图整个坏了」被误判成
    「过滤生效」。
    """
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    for i in range(5):
        asyncio.run(plugin.inject_memory(_Event("第 %d 轮：空调开 26 度" % i),
                                         _Req("第 %d 轮" % i)))
        asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了 %d" % i)))
    plugin.ledger.remember("用户喜欢喝热咖啡", tags="咖啡,饮品", importance=0.8)
    plugin.ledger.remember("用户下午常去那家咖啡店", tags="咖啡,门店", importance=0.7)

    sink_ids = plugin.ledger.auto_sink_ids()
    assert len(sink_ids) == 5, "5 轮沉淀没落库，测试环境坏了"
    links = plugin.ledger._query_all("SELECT * FROM memory_link")
    concepts = {l["concept"] for l in links}
    # 建图结果里没有该 concept：类型标记与统一标签都不许当概念
    assert m.AUTO_SINK_TYPE_TAG not in concepts
    assert mod.AUTO_SINK_TAG not in concepts
    # 沉淀条目不产生任何 concept 边：任何边的任一端都不落在沉淀集合里
    assert not [l for l in links
                if l["from_id"] in sink_ids or l["to_id"] in sink_ids]
    # 对照组：正常记忆的语义标签照常织网，过滤没误伤建图本身
    assert any(l["concept"] == "咖啡" for l in links)


def test_auto_sink_excluded_from_fallback_route(mod, plugin, monkeypatch):
    """N1：检索落空走 fallback 兜底时，沉淀条目也不能顶进提示词。

    沉淀条目 importance 0.45 高于对照组 0.1，fresh 排序排第一；
    修复前 fallback 会把它的「自动沉淀：…」正文塞进 system_prompt，
    修复后只能放对照组——两条断言缺一不可，防止「兜底整个坏了」
    被误判成「排除生效」。
    """
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    plugin.ledger.remember("用户喜欢喝热咖啡", importance=0.1)
    # 同上：第一次请求只落链，不让对照组提前进冷却，
    # 否则 fallback 只剩冷却挡光的候选，断言不出排除语义。
    asyncio.run(plugin.inject_memory(_Event("第 1 轮：空调开 26 度"), _Req("")))
    asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了")))
    assert plugin.ledger._query_one(
        "SELECT id FROM memory WHERE tags LIKE ?", ("%type:auto_sink%",))
    req = _Req("量子纠缠")
    asyncio.run(plugin.inject_memory(_Event("量子纠缠"), req))
    prompt = req.system_prompt or ""
    assert "自动沉淀" not in prompt
    assert "热咖啡" in prompt


def test_auto_sink_still_searchable(mod, plugin, monkeypatch):
    """N1：排除只发生在注入侧，显式检索（recall）照样找得到沉淀条目。"""
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    asyncio.run(plugin.inject_memory(_Event("第 1 轮：空调开 26 度"), _Req("第 1 轮")))
    asyncio.run(plugin.record_reply(_Event(), _Resp("好，记住了")))
    hits = plugin.ledger.recall("空调", touch=False)
    assert any("type:auto_sink" in (h.get("tags") or "") for h in hits)


# -- 退出 ------------------------------------------------------------------

def test_terminate_exports_then_closes(plugin):
    plugin.ledger.remember("退出前的一条")
    asyncio.run(plugin.terminate())
    assert plugin.failures.total() == 0


def test_terminate_export_failure(plugin, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("导出炸了")

    monkeypatch.setattr(plugin.ledger, "export_markdown", boom)
    asyncio.run(plugin.terminate())
    assert plugin.failures.counts["退出导出失败"] == 1


# -- 小工具 ------------------------------------------------------------------

def test_as_int_and_float_edges(mod):
    assert mod._as_int("", 5) == 5
    assert mod._as_float("1.5", 0.0) == 1.5
    assert mod._as_float(None, 0.25) == 0.25


def test_short_clips(mod):
    assert mod._short("a" * 50, 10).endswith("…")


def test_auto_sink_summary_from_assistant_only(mod):
    assert mod.auto_sink_summary([{"role": "assistant", "content": "只有回复"}])
    assert mod.auto_sink_summary([]) == ""


def test_failure_counter_summary(mod):
    fc = mod.FailureCounter()
    assert fc.summary() == "无"
    fc.note("甲", RuntimeError("x"))
    fc.note("甲", RuntimeError("y"))
    assert fc.total() == 2 and "甲 2 次" in fc.summary()
# -- 覆盖率收尾 -------------------------------------------------------------

def test_owners_cache_hit(mod, tmp_path):
    (tmp_path / "owners.json").write_text('["123:FriendMessage:9"]', encoding="utf-8")
    first = mod._load_owners()
    assert mod._load_owners() == first


def test_auto_sink_summary_dedups_and_caps(mod):
    rows = [{"role": "user", "content": "abcdef abcdef aa abc bb cc dd"}]
    text = mod.auto_sink_summary(rows)
    assert "关键词" in text
    assert len(text.split("关键词：")[1].split("、")) == 5


def test_init_counts_existing_inject_log(mod):
    import main as m
    m.INJECT_LOG.parent.mkdir(parents=True, exist_ok=True)
    m.INJECT_LOG.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    plugin = mod.MemoryCommitPlugin(None)
    assert plugin._inject_lines == 2
    plugin.ledger.close()


def test_auto_sink_empty_summary(mod, plugin, monkeypatch):
    import main as m
    monkeypatch.setattr(m, "AUTO_SINK_EVERY", 1)
    monkeypatch.setattr(m, "auto_sink_summary", lambda rows: "")
    plugin.ledger.append_message("user", "一句话")
    assert plugin._auto_sink() == {}


def test_working_plan_shows_promotions(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "plan_working_set", lambda *a, **kw: {
        "capacity": 2, "occupied": 1, "keep": [], "overflow": 0,
        "promote": [{"id": "m9", "activation": 0.9, "summary": "想起很多次"}]})
    out = _cmd(plugin, "cmd_working", "/记忆容量")
    assert "会升回一级" in out


def test_inject_denial_queue_failure_is_counted(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "queue_denial_suspect",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("写不进队列")))
    asyncio.run(plugin.inject_memory(_Event("你记错了"), _Req("你记错了")))
    assert plugin.failures.counts["待确认队列写入失败"] == 1


def test_raw_truncated_note(plugin, monkeypatch):
    monkeypatch.setattr(plugin.ledger, "search_messages", lambda *a, **kw: {
        "rows": [{"role": "user", "content": "空调", "at": "2026-01-01T10:00:00"}],
        "total": 900, "scanned": 200, "truncated": True})
    monkeypatch.setattr(plugin.ledger, "message_stats",
                        lambda: {"count": 900, "since": "2025-01-01T00:00:00"})
    out = _cmd(plugin, "cmd_raw", "/记忆原话 空调")
    assert "只查了最近 200 条" in out

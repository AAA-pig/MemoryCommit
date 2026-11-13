# -*- coding: utf-8 -*-
"""R1-R8 修复的回归测试。

每个用例都先在旧实现上复现缺陷（写法保证旧行为必失败），再锁住修复后的行为：
  R1 指令鉴权 / 备份路径只能落专用目录且不覆盖 / 破坏性操作精确确认
  R2 原文锚绑到具体请求的事件，落链失败显式置空
  R3 被否认 / coexist 的记忆不进主动注入（五条路线全覆盖）
  R4 删除事件带完整行身份，重放不出幽灵行，自检对账全部投影表
  R5 备份 v2：行数清单 + 尾部标记 + 严格导入 + 临时库预演
  R6 scope 贯穿线索桥 / 图谱一跳 / 梦产出
  R7 梦整轮不承诺原子，部分成功显式返回
  R8 MAINTAIN_EVERY=1 时维护每轮都跑
"""
import asyncio
import json
import os
import re
import sys
import time

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import ledger as L  # noqa: E402

UMO = "123:FriendMessage:1"


# -- 公共夹具 ---------------------------------------------------------------

@pytest.fixture()
def led(tmp_path):
    obj = L.Ledger(str(tmp_path / "t.db"))
    yield obj
    try:
        obj.close()
    except Exception:
        pass


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


class _Req:
    def __init__(self, prompt="", system_prompt=""):
        self.prompt = prompt
        self.system_prompt = system_prompt


def _run(agen):
    async def go():
        return [x async for x in agen]
    return asyncio.run(go())


def _cmd(plugin, name, text="", umo=UMO):
    return "\n".join(str(x) for x in _run(getattr(plugin, name)(_Event(text, umo=umo))))


ALL_COMMANDS = (
    ("cmd_memory", "/记忆"), ("cmd_history", "/记忆链"), ("cmd_flush", "/记忆冲刷"),
    ("cmd_working", "/记忆容量"), ("cmd_compact", "/记忆压缩"), ("cmd_export", "/记忆导出"),
    ("cmd_backup", "/记忆备份"), ("cmd_health", "/记忆自检"), ("cmd_raw", "/记忆原话"),
    ("cmd_access", "/记忆访问"), ("cmd_activate", "/记忆唤起"),
)


# -- R1：指令鉴权 ------------------------------------------------------------

def test_all_commands_reject_non_owner(plugin):
    outsider = "999:GroupMessage:1"
    for name, text in ALL_COMMANDS:
        out = _cmd(plugin, name, text, umo=outsider)
        assert "不可用" in out, "%s 未拒绝非 owner" % name


# -- R1：备份只能落专用目录、默认不覆盖 ----------------------------------------

def test_backup_lands_in_dedicated_dir(plugin, tmp_path):
    plugin.ledger.remember("备份用的一条")
    out = _cmd(plugin, "cmd_backup", "/记忆备份")
    assert "备份好了" in out
    assert os.path.isdir(str(tmp_path / "backup"))
    named = _cmd(plugin, "cmd_backup", "/记忆备份 夜间备份")
    assert "备份好了" in named
    assert os.path.exists(str(tmp_path / "backup" / "夜间备份.jsonl"))


def test_backup_rejects_path_outside_dir(plugin, tmp_path):
    """传路径只取文件名落专用目录，原路径绝不会有文件。"""
    plugin.ledger.remember("一条")
    victim = str(tmp_path / "victim.jsonl")
    out = _cmd(plugin, "cmd_backup", "/记忆备份 " + victim)
    assert "备份好了" in out
    assert not os.path.exists(victim), "客户端传来的路径不许照原样写"
    assert os.path.exists(str(tmp_path / "backup" / "victim.jsonl")), "只取文件名落专用目录"
    tricky = _cmd(plugin, "cmd_backup", "/记忆备份 ..\\..\\escape.jsonl")
    assert "备份好了" in tricky and ".." not in tricky.split("\n")[1]


def test_backup_refuses_overwrite(plugin):
    plugin.ledger.remember("一条")
    assert "备份好了" in _cmd(plugin, "cmd_backup", "/记忆备份 dup")
    out = _cmd(plugin, "cmd_backup", "/记忆备份 dup")
    assert "没备份成" in out and ("覆盖" in out or "已存在" in out)


# -- R1：破坏性操作精确确认 ----------------------------------------------------

def _make_flushable(plugin, grade=1, n_extra=5):
    first = plugin.ledger.remember("早年的一件小事", grade=grade)
    for i in range(n_extra):
        plugin.ledger.remember("新事 %d" % i)
    return first["memory_id"]


def test_flush_negative_confirm_does_not_apply(plugin):
    mid = _make_flushable(plugin)
    out = _cmd(plugin, "cmd_flush", "/记忆冲刷 不要确认")
    assert "待处理" in out, "「不要确认」不是确认，不许含字匹配误执行"
    row = plugin.ledger._query_one("SELECT grade FROM memory WHERE id=?", (mid,))
    assert row["grade"] == 1, "冲刷被误执行了"


def test_working_negative_confirm_does_not_apply(plugin, monkeypatch):
    plugin.ledger.working_capacity = 0
    monkeypatch.setattr(plugin.ledger, "plan_working_set", lambda *a, **kw: {
        "capacity": 0, "occupied": 2, "demote": [], "promote": [],
        "keep": [], "overflow": 2})
    monkeypatch.setattr(plugin.ledger, "rebalance_working_set", lambda *a, **kw: {
        "applied": True, "demoted": ["m1"], "promoted": [], "commit": "C9"})
    out = _cmd(plugin, "cmd_working", "/记忆容量 不要确认")
    assert "换完班" not in out, "「不要确认」不许触发换班"


# -- R2：原文锚绑到请求，失败显式置空 -------------------------------------------

def test_anchor_binds_to_event_not_plugin(plugin, monkeypatch):
    e1, e2 = _Event("第一句"), _Event("第二句")
    asyncio.run(plugin.inject_memory(e1, _Req("无关")))
    asyncio.run(plugin.inject_memory(e2, _Req("无关")))
    s1 = getattr(e1, "_mc_anchor_seq", 0)
    s2 = getattr(e2, "_mc_anchor_seq", 0)
    assert s1 and s2 and s1 != s2, "锚必须各事件各一份"
    captured = {}
    real = plugin.ledger.remember

    def spy(content, **kw):
        captured["anchor"] = kw.get("anchor_seq")
        return real(content, **kw)

    monkeypatch.setattr(plugin.ledger, "remember", spy)
    asyncio.run(plugin.memory_write(e1, "记住这件事"))
    assert captured["anchor"] == s1, "新记忆要锚回本事件的原话，不是别人的"


def test_anchor_reset_on_chain_failure(plugin, monkeypatch):
    e = _Event("第一句")
    asyncio.run(plugin.inject_memory(e, _Req("x")))
    assert getattr(e, "_mc_anchor_seq", 0) > 0

    def boom(*a, **kw):
        raise RuntimeError("落链炸了")

    monkeypatch.setattr(plugin.ledger, "append_message", boom)
    asyncio.run(plugin.inject_memory(e, _Req("y")))
    assert getattr(e, "_mc_anchor_seq", 0) == 0, "落链失败必须显式置空，不许沿用上一轮锚"


# -- R3：disputed 永久拦 / coexist 条件匹配才放行 ------------------------------

def _coexist(led, mid, cond=""):
    """把一条记忆置成 coexist 状态，条件原文可直接写入（测试要造坏 JSON）。"""
    led.dream_apply([], [{"memory_id": mid, "status": "coexist",
                          "reason": "矛盾四分类：场景", "evidence_ref": ""}])
    if cond:
        with led._tx() as cx:
            cx.execute("INSERT INTO memory_condition(memory_id, cond_json, cond_note,"
                       " created_at) VALUES(?,?,?,?)", (mid, cond, "", L.now_str()))


def test_denied_memory_blocked_on_all_activate_routes(led):
    """disputed 五条路线全拦：direct / clue / graph / fresh / fallback。

    m3 与 m1 同 tags 且无状态，是对照组：它进得了 items，
    证明路线本身在跑，m1 没出现是被 disputed 拦的，不是路没通。
    """
    led.remember("用户的猫叫咪咪", tags="咪咪,宠物", importance=0.7)   # m1 disputed
    led.remember("给宠物买粮食", tags="宠物", importance=0.6)          # m2 检索锚点
    led.remember("宠物要定期驱虫", tags="宠物", importance=0.6)        # m3 对照组
    led.deny_memory("m1", "猫不是叫咪咪")

    # direct / clue：query 撞词面与线索（「咪咪」在 m1 摘要与标签里）
    led._surfaced.clear()
    act = led.activate("咪咪", slots=4)
    assert "m1" not in {r["id"] for r in act["items"]}, "被否认的记忆不许经 direct/clue 回来"

    # graph：query 只命中 m2，m1 只能沿连边一跳进来
    led._surfaced.clear()
    act = led.activate("买粮食", slots=4)
    ids = {r["id"] for r in act["items"]}
    assert "m2" in ids and "m3" in ids, "对照组证明 graph 一跳本身是通的"
    assert "m1" not in ids, "被否认的记忆不许经 graph 一跳回来"

    # fresh：无 query 的正常激活路线
    led._surfaced.clear()
    act = led.activate("", slots=4)
    ids = {r["id"] for r in act["items"]}
    assert "m3" in ids and "m1" not in ids, "fresh 路线拦 disputed，对照组能进"

    # fallback：query 零命中时的兕底
    led._surfaced.clear()
    act = led.activate("量子物理超导", slots=4)
    assert "m1" not in {r["id"] for r in act["items"]}, "被否认的记忆不许经 fallback 回来"


def test_coexist_memory_blocked_from_fresh(led):
    """coexist 无语境（context=None）：判不出条件，保守拦，不许主动浮现。"""
    led.remember("用户夏天怕热", tags="怕热", importance=0.7)
    _coexist(led, "m1", '{"scene": "夏天"}')
    led._surfaced.clear()
    act = led.activate("", slots=4)
    assert "m1" not in {r["id"] for r in act["items"]}, "无语境判不出条件，coexist 不许主动浮现"


def test_coexist_context_match_allows_activation(led):
    """coexist 条件匹配（给了语境）：放行，可注入——fresh 与 direct 都能浮出。"""
    led.remember("用户夏天怕热", tags="怕热,宠物", importance=0.7)
    led.remember("给宠物买粮食", tags="宠物", importance=0.6)
    _coexist(led, "m1", '{"scene": "夏天"}')
    summer = {"scene": "夏天", "now": time.time()}
    led._surfaced.clear()
    act = led.activate("", slots=4, context=summer)
    assert "m1" in {r["id"] for r in act["items"]}, "条件匹配的 coexist 应能主动浮现"
    led._surfaced.clear()
    act = led.activate("怕热", slots=4, context=summer)
    assert "m1" in {r["id"] for r in act["items"]}, "direct 路线同样该放行"


def test_coexist_context_mismatch_blocks_all_routes(led):
    """coexist 条件不匹配：direct / clue / graph / fresh / fallback 五路全拦。

    m3 是对照组（同 tags 无状态）：graph/fresh 场景里它能进 items，
    证明路线通、拦截只落在条件不匹配的 m1 上。
    """
    led.remember("用户夏天怕热", tags="怕热,宠物", importance=0.7)   # m1 coexist
    led.remember("给宠物买粮食", tags="宠物", importance=0.6)        # m2 检索锚点
    led.remember("宠物要定期驱虫", tags="宠物", importance=0.6)      # m3 对照组
    _coexist(led, "m1", '{"scene": "夏天"}')
    winter = {"scene": "冬天", "now": time.time()}

    led._surfaced.clear()
    act = led.activate("", slots=4, context=winter)
    ids = {r["id"] for r in act["items"]}
    assert "m3" in ids and "m1" not in ids, "fresh：条件不匹配拦，对照组能进"

    led._surfaced.clear()
    act = led.activate("怕热", slots=4, context=winter)
    assert "m1" not in {r["id"] for r in act["items"]}, "direct/clue：条件不匹配拦"

    led._surfaced.clear()
    act = led.activate("买粮食", slots=4, context=winter)
    ids = {r["id"] for r in act["items"]}
    assert "m2" in ids and "m3" in ids, "对照组证明 graph 一跳本身是通的"
    assert "m1" not in ids, "graph 一跳也不许把条件不匹配的记忆带进来"

    led._surfaced.clear()
    act = led.activate("量子物理超导", slots=4, context=winter)
    assert "m1" not in {r["id"] for r in act["items"]}, "fallback：条件不匹配拦"


def test_coexist_bad_cond_json_blocked_even_with_context(led):
    """cond_json 坏 JSON：即使给了语境也判不出，保守拦。"""
    led.remember("用户夏天怕热", tags="怕热", importance=0.7)
    _coexist(led, "m1", "{bad json")
    ctx = {"scene": "夏天", "now": time.time()}
    led._surfaced.clear()
    act = led.activate("", slots=4, context=ctx)
    assert "m1" not in {r["id"] for r in act["items"]}, "坏 JSON 判不出，不许放行"
    assert "m1" in led.quiet_ids(ctx), "quiet 集合也要把坏 JSON 的 coexist 收进去"


def test_disputed_context_match_still_blocks_all_routes(led):
    """disputed 不吃条件豁免：语境再匹配，五条路线照样全拦。"""
    led.remember("用户的猫叫咪咪", tags="咪咪,宠物", importance=0.7)   # m1 disputed
    led.remember("给宠物买粮食", tags="宠物", importance=0.6)          # m2 检索锚点
    led.deny_memory("m1", "猫不是叫咪咪")
    summer = {"scene": "夏天", "now": time.time()}

    led._surfaced.clear()
    assert "m1" not in {r["id"] for r in led.activate("", slots=4, context=summer)["items"]}
    led._surfaced.clear()
    assert "m1" not in {r["id"] for r in led.activate("咪咪", slots=4, context=summer)["items"]}
    led._surfaced.clear()
    ids = {r["id"] for r in led.activate("买粮食", slots=4, context=summer)["items"]}
    assert "m2" in ids and "m1" not in ids, "graph 一跳也拦"
    led._surfaced.clear()
    assert "m1" not in {r["id"] for r in led.activate("量子物理超导", slots=4,
                                                     context=summer)["items"]}


def test_quiet_ids_context_aware(led):
    """quiet_ids 带语境：匹配的 coexist 出列，其余照旧；无语境全拦（兼容行为）。"""
    led.remember("用户夏天怕热", tags="怕热", importance=0.7)
    led.remember("用户冬天怕冷", tags="怕冷", importance=0.7)
    _coexist(led, "m1", '{"scene": "夏天"}')
    _coexist(led, "m2", '{"scene": "冬天"}')
    summer = {"scene": "夏天", "now": time.time()}
    assert led.quiet_ids(summer) == {"m2"}, "夏天语境下只有冬天的条件不匹配"
    assert led.quiet_ids() == {"m1", "m2"}, "不带语境保持旧行为：coexist 全拦"
    led.deny_memory("m1", "没有怕热这回事")
    assert "m1" in led.quiet_ids(summer), "disputed 不吃条件豁免"
    assert "m1" in led.quiet_ids(), "disputed 无语境也拦"


def test_condition_allows_branches():
    """condition_allows 判定矩阵：判不出一律 False，全部匹配才 True。"""
    now = time.time()
    # 没语境 / 坏 JSON / 不是对象：判不出
    assert L.condition_allows('{"scene": "公司"}', None) is False
    assert L.condition_allows('{"scene": "公司"}', {}) is False
    assert L.condition_allows("", {"scene": "公司"}) is False
    assert L.condition_allows("{bad", {"scene": "公司"}) is False
    assert L.condition_allows("[1,2]", {"scene": "公司"}) is False
    # 字段不认识：判不出
    assert L.condition_allows('{"weather": "晴"}', {"scene": "公司"}) is False
    # 字段全是空值：等于没写条件，判不出
    assert L.condition_allows('{"scene": ""}', {"scene": "公司"}) is False
    # scene：匹配放行；不匹配拦；语境缺场景拦
    assert L.condition_allows('{"scene": "公司"}', {"scene": "公司", "now": now}) is True
    assert L.condition_allows('{"scene": "公司"}', {"scene": "家"}) is False
    assert L.condition_allows('{"scene": "公司"}', {"now": now}) is False
    # window：窗内放行；窗外拦；缺时间拦；边界解析不出拦；非数值时间拦
    win = '{"window": {"from": "2026-06-01", "to": "2026-08-31"}}'
    assert L.condition_allows(win, {"now": "2026-07-15"}) is True
    assert L.condition_allows(win, {"now": "2026-12-01"}) is False
    assert L.condition_allows(win, {}) is False
    assert L.condition_allows('{"window": {"from": "x", "to": "y"}}',
                              {"now": now}) is False
    assert L.condition_allows(win, {"now": True}) is False
    # env：一致放行；不一致拦；语境缺 env 拦
    assert L.condition_allows('{"env": {"city": "成都"}}',
                              {"env": {"city": "成都"}}) is True
    assert L.condition_allows('{"env": {"city": "成都"}}',
                              {"env": {"city": "北京"}}) is False
    assert L.condition_allows('{"env": {"city": "成都"}}', {"scene": "公司"}) is False
    # 多条件同给：全部匹配才放行
    multi = '{"scene": "公司", "window": {"from": "2026-06-01", "to": "2026-08-31"}}'
    assert L.condition_allows(multi, {"scene": "公司", "now": "2026-07-15"}) is True
    assert L.condition_allows(multi, {"scene": "家", "now": "2026-07-15"}) is False


def test_inject_memory_passes_session_context(mod, plugin, monkeypatch):
    """inject_memory 要把会话场景与当前时间当语境传给 activate（R3）。"""
    captured = {}

    def spy_activate(query, **kw):
        captured["context"] = kw.get("context")
        return {"items": [], "profiles": [], "flashes": [],
                "route": "none", "query": query}

    monkeypatch.setattr(plugin.ledger, "activate", spy_activate)
    monkeypatch.setattr(plugin.ledger, "render_brief", lambda *a, **kw: "")
    asyncio.run(plugin.inject_memory(_Event("看了看"), _Req("你好")))
    ctx = captured["context"]
    assert ctx is not None, "有会话场景时不许传 None"
    assert ctx["scene"] == UMO, "场景用会话标识这种真实拿得到的东西"
    assert isinstance(ctx["now"], float), "当前时间是真实时间戳"


def test_inject_memory_without_scene_passes_none(mod, plugin, monkeypatch):
    """拿不到会话场景就传 None，不许编造环境数据。"""
    captured = {}

    def spy_activate(query, **kw):
        captured["context"] = kw.get("context")
        return {"items": [], "profiles": [], "flashes": [],
                "route": "none", "query": query}

    monkeypatch.setattr(plugin.ledger, "activate", spy_activate)
    monkeypatch.setattr(plugin.ledger, "render_brief", lambda *a, **kw: "")
    monkeypatch.setattr(mod, "_is_owner", lambda e: True)
    asyncio.run(plugin.inject_memory(_Event("看了看", umo=""), _Req("你好")))
    assert captured["context"] is None, "场景拿不到就传 None，判不出条件的记忆保守拦"


# -- R4：删除行身份 / 重放 / 对账 -----------------------------------------------

def test_forget_rows_carry_full_identity(led):
    led.remember("小明喜欢篮球", tags="篮球", importance=0.6)
    led.forget("m1")
    rows = [dict(r) for r in led._query_all(
        "SELECT * FROM commit_row WHERE op='delete' AND tbl IN"
        " ('memory_clue','memory_link')")]
    assert rows, "forget 应该删除关联行"
    for r in rows:
        snap = json.loads(r["before_json"]) if r["before_json"] else {}
        if r["tbl"] == "memory_clue":
            assert snap.get("memory_id") and snap.get("clue_id"), \
                "删除行 before 缺身份字段：%s" % r["row_key"]
        else:
            assert snap.get("from_id") and snap.get("to_id") and snap.get("concept"), \
                "删除行 before 缺身份字段：%s" % r["row_key"]


def test_replay_after_forget_and_compact_no_ghost(led):
    led.remember("小明喜欢篮球", tags="篮球", importance=0.6)
    led.remember("小明参加了篮球比赛", tags="篮球", importance=0.6)
    led.forget("m1")
    res = led.compact(keep_recent=1)
    assert res["compacted"]
    replay = led._replay_tables(led.head())
    for tbl in ("memory", "memory_clue", "memory_link", "clue"):
        n = led._query_one("SELECT COUNT(*) AS n FROM %s" % tbl)["n"]
        assert len(replay.get(tbl, {})) == n, \
            "%s 重放 %d 行 vs 当前 %d 行（幽灵行）" % (tbl, len(replay.get(tbl, {})), n)
    hc = led.health_check()
    assert hc["ok"], hc["problems"]


def test_health_check_reconciles_all_projections(led):
    led.remember("一条", tags="甲")
    assert led.health_check()["ok"]
    # 直插投影表不进提交链：对账必须当场抓出来
    with led._tx() as cx:
        cx.execute("INSERT INTO clue(id, content, created_at)"
                   " VALUES('c999','幽灵线索','2026-01-01T00:00:00')")
    hc = led.health_check()
    assert not hc["ok"]
    assert any("投影对账不一致" in p and "clue" in p for p in hc["problems"])


# -- R5：备份完整性 -----------------------------------------------------------

def _roundtrip_source(tmp_path):
    src = L.Ledger(str(tmp_path / "src.db"))
    src.remember("第一条", tags="甲")
    src.remember("第二条", tags="甲")
    src.append_message("user", "原话一句")
    return src


def test_dump_v2_has_manifest_and_eof(led, tmp_path):
    led.remember("一条", tags="甲")
    p = str(tmp_path / "b.jsonl")
    led.dump_jsonl(p)
    lines = [ln for ln in open(p, encoding="utf-8").read().splitlines() if ln.strip()]
    header = json.loads(lines[0])
    assert header["version"] == L.DUMP_VERSION >= 2
    assert header["rows_total"] == len(lines) - 2, "清单总行数要和数据行数一致"
    assert json.loads(lines[-1])["t"] == "__eof__", "尾部必须有结束标记"
    per = {}
    for ln in lines[1:-1]:
        t = json.loads(ln)["t"]
        per[t] = per.get(t, 0) + 1
    for t, n in header["tables"].items():
        assert per.get(t, 0) == n, "%s 表清单 %d 行，实际 %d 行" % (t, n, per.get(t, 0))
    assert sum(header["tables"].values()) == header["rows_total"]


def test_load_v2_truncated_rejected(led, tmp_path):
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    raw = open(p, encoding="utf-8").read().splitlines()
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(raw[:-3]) + "\n")
    with pytest.raises(L.LedgerError):
        led.load_jsonl(p)
    assert led.stats()["commits"] == 0, "被拒的导入不许动原库"


def test_load_v2_bad_row_rejected(led, tmp_path):
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    lines = open(p, encoding="utf-8").read().splitlines()
    lines.insert(3, "这行不是 JSON")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "不是合法 JSON" in str(ei.value)


def test_load_v2_rehearsal_failure_keeps_target(led, tmp_path):
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    # 行数清单保持一致，但内容坏了：HEAD 指到不存在的提交，预演必须拦下
    lines = open(p, encoding="utf-8").read().splitlines()
    out = []
    for ln in lines:
        obj = json.loads(ln)
        if obj.get("t") == "meta" and obj.get("r", {}).get("k") == "head":
            obj["r"]["v"] = "C999"
        out.append(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "预演" in str(ei.value)
    assert led.stats()["commits"] == 0, "预演失败原库一字节不动"


def test_load_v2_rejects_non_object_row(led, tmp_path):
    """严格模式：合法 JSON 但不是对象的行也要拒，不能溜进库。"""
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    lines = open(p, encoding="utf-8").read().splitlines()
    lines.insert(3, "[1,2]")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "不是 JSON 对象" in str(ei.value)
    assert led.stats()["commits"] == 0


def test_load_v2_rejects_unknown_table(led, tmp_path):
    """严格模式：表名不认识的行当场拒，不带进预演。"""
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    lines = open(p, encoding="utf-8").read().splitlines()
    lines.insert(3, json.dumps({"t": "ghost_table", "r": {}}))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "表名不认识" in str(ei.value)
    assert led.stats()["commits"] == 0


def test_load_v2_rejects_total_mismatch(led, tmp_path):
    """严格模式：清单总行数与实际行数对不上，拒绝导入。"""
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    lines = open(p, encoding="utf-8").read().splitlines()
    header = json.loads(lines[0])
    header["rows_total"] = int(header["rows_total"]) + 1
    lines[0] = json.dumps(header, ensure_ascii=False, sort_keys=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "总行数对不上" in str(ei.value)
    assert led.stats()["commits"] == 0


def test_load_v2_rejects_table_count_mismatch(led, tmp_path):
    """严格模式：单表行数与清单对不上，同样拒绝（总数凑对也拦）。"""
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    lines = open(p, encoding="utf-8").read().splitlines()
    header = json.loads(lines[0])
    header["tables"]["memory"] = int(header["tables"].get("memory", 0)) + 99
    lines[0] = json.dumps(header, ensure_ascii=False, sort_keys=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with pytest.raises(L.LedgerError) as ei:
        led.load_jsonl(p)
    assert "memory 表行数对不上" in str(ei.value)
    assert led.stats()["commits"] == 0


def test_restore_check_tolerates_stale_temp_file_cleanup_failure(led, tmp_path,
                                                                 monkeypatch):
    """预演临时库的旧文件清不掉（被占用等 OSError）：预演不炸，导入照常。"""
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    monkeypatch.setattr(L.os.path, "exists", lambda x: True)

    def busy(_p):
        raise OSError("文件被占用")

    monkeypatch.setattr(L.os, "remove", busy)
    res = led.load_jsonl(p)
    assert res["verified"] is True, "清理失败只是环境故障，不许打断导入"
    assert led.health_check()["ok"]


def test_load_v2_roundtrip_verified(led, tmp_path):
    src = _roundtrip_source(tmp_path)
    p = str(tmp_path / "b.jsonl")
    src.dump_jsonl(p)
    src.close()
    other = L.Ledger(str(tmp_path / "o.db"))
    try:
        res = other.load_jsonl(p)
        assert res["verified"] is True
        assert res["memories"] == 2
        assert other.health_check()["ok"]
    finally:
        other.close()


# -- R6：scope 贯穿 ------------------------------------------------------------

def test_scope_covers_clue_bridge_and_graph_hop(led):
    led.remember("A 在聊项目排期", tags="篮球,项目", scope_key="会话A", importance=0.7)
    led.remember("B 在聊宠物粮", tags="篮球", scope_key="会话B", importance=0.7)
    rec = led.recall("项目", limit=5, scope_key="会话A", touch=False)
    assert "m2" not in {r["id"] for r in rec}, "图谱一跳不许跨会话"
    bridge = led._clue_bridge("篮球", 6, "会话A")
    assert all((r.get("scope_key") or "") in ("", "会话A") for r in bridge), \
        "线索桥不许跨会话牵记忆"
    act = led.activate("项目", slots=4, scope_key="会话A")
    assert all((r.get("scope_key") or "") in ("", "会话A") for r in act["items"])


def test_dream_add_memory_carries_scope(led):
    led.remember("候选甲", tags="甲")
    led.remember("候选乙", tags="甲")
    res = led.dream_add_memory("（梦）模糊印象", basis_ids=["m1", "m2"],
                               scope_key="会话A")
    row = led._query_one("SELECT scope_key FROM memory WHERE id = ?",
                         (res["memory_id"],))
    assert row["scope_key"] == "会话A", "梦产出必须继承显式传入的 scope，不许默认成全局"


# -- R7：梦整轮部分成功显式化 ----------------------------------------------------

def test_run_once_partial_success_reported(led, monkeypatch):
    led.remember("候选甲", tags="甲")
    led.remember("候选乙", tags="甲")
    led.remember("候选丙", tags="甲")
    w = L.DreamWorker(led, enabled=True)
    calls = {"n": 0}
    real = led.dream_add_memory

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise L.LedgerError("第二次产出炸了")
        return real(*a, **kw)

    monkeypatch.setattr(led, "dream_add_memory", flaky)
    res = w.run_once(produce=2, max_links=1)
    assert res["partial"] is True, "整轮不承诺原子，部分成功必须显式标出"
    assert res["errors"], "失败点要写进 errors"
    assert len(res["produced"]) == 1, "已落地的那条要如实保留"


# -- R8：MAINTAIN_EVERY=1 每轮都维护 --------------------------------------------

def test_maintain_every_one_fires_each_round(mod, plugin, monkeypatch):
    monkeypatch.setattr(mod, "MAINTAIN_EVERY", 1)
    calls = {"n": 0}

    def ok(*a, **kw):
        calls["n"] += 1
        return False

    monkeypatch.setattr(plugin.ledger, "maybe_export_daily", ok)
    monkeypatch.setattr(plugin.ledger, "maybe_rebalance_working_set",
                        lambda *a, **kw: {})
    monkeypatch.setattr(plugin.ledger, "maybe_checkpoint_wal", lambda *a, **kw: {})
    monkeypatch.setattr(plugin.ledger, "activate",
                        lambda *a, **kw: {"items": [], "profiles": [],
                                          "flashes": [], "route": "none",
                                          "query": ""})
    monkeypatch.setattr(plugin.ledger, "render_brief", lambda *a, **kw: "")
    asyncio.run(plugin.inject_memory(_Event("第一轮"), _Req("第一轮")))
    asyncio.run(plugin.inject_memory(_Event("第二轮"), _Req("第二轮")))
    assert calls["n"] == 2, "周期配 1 时每轮都要维护（旧实现取余恒 0，永不执行）"


# -- 覆盖补齐：防御路径（可达但平时不触发的分支，锁行为不锁运气）----------------

def test_recall_expand_pulls_unhit_neighbor(led):
    """顺藤摸瓜：没被词面命中的邻居经图谱一跳提分并标 graph，跟着种子带出。"""
    led.remember("给宠物买粮食", tags="宠物", importance=0.8)    # m1 命中
    led.remember("宠物要定期驱虫", tags="宠物", importance=0.8)  # m2 邻居，词面不命中
    rec = led.recall("买粮食", limit=5, touch=False)
    by_id = {r["id"]: r for r in rec}
    assert "m1" in by_id
    assert "m2" in by_id, "没被词面命中的邻居应随种子一起带出"
    assert by_id["m2"].get("match") == "graph", "邻居靠图谱一跳提分"
    assert by_id["m2"].get("bm25") == 0, "邻居本身没有词面命中"


def test_health_check_reports_replay_failure(led, monkeypatch):
    """重放抛异常：自检显式报「投影重放失败」，不静默、也不炸整次自检。"""
    led.remember("一条", tags="甲")

    def boom(_target):
        raise RuntimeError("链坏了")

    monkeypatch.setattr(led, "_replay_tables", boom)
    hc = led.health_check()
    assert not hc["ok"]
    assert any("投影重放失败" in p for p in hc["problems"])


def test_health_check_skips_keyless_replay_rows(led):
    """重放里出现没有行主键的脏快照行：跳过，不许凭空报对账不一致。"""
    led.remember("一条", tags="甲")
    with led._tx() as cx:
        # 幽灵行挂到真实 HEAD 提交名下才会被重放；after 快照故意缺 id
        cx.execute("INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
                   " before_json, after_json) VALUES(?, 999, 'memory',"
                   " 'insert', 'id=', NULL, ?)",
                   (led.head(), json.dumps({"summary": "无id幽灵行"})))
    hc = led.health_check()
    assert hc["ok"], hc["problems"]


def test_dream_run_once_reports_proposal_apply_failure(led, monkeypatch):
    """连线提案生成成功但落地失败：errors 显式记录，partial=True。"""
    led.remember("候选甲", tags="甲")
    led.remember("候选乙", tags="甲")

    def boom(_proposals, _statuses):
        raise L.LedgerError("提案表写炸了")

    monkeypatch.setattr(led, "dream_apply", boom)
    w = L.DreamWorker(led, enabled=True)
    res = w.run_once(produce=0, max_links=1)
    assert res["proposals"], "连线提案要先真的生成出来"
    assert res["partial"] is True
    assert any("提案落地失败" in e for e in res["errors"])


def test_backup_rejects_target_outside_dedicated_dir(mod, plugin, monkeypatch,
                                                     tmp_path):
    """解析后的真实路径逃出专用备份目录（符号链接等）：当场拒绝，不落盘。"""
    plugin.ledger.remember("一条")
    outside = str(tmp_path / "outside.jsonl")
    real = mod.os.path.realpath

    def fake(p):
        s = str(p)
        return real(s) if s == str(mod.BACKUP_DIR) else outside

    monkeypatch.setattr(mod.os.path, "realpath", fake)
    out = _cmd(plugin, "cmd_backup", "/记忆备份 逃逸")
    assert "没备份成" in out and "专用目录" in out
    assert not os.path.exists(outside), "逃逸目标不许真的写文件"


def test_inject_memory_tolerates_locked_event(mod, plugin, monkeypatch):
    """事件对象不让设属性（如 __slots__）：锚绑定失败被吞，唤起照常走。"""
    class LockedEvent:
        __slots__ = ("_text", "unified_msg_origin")

        def __init__(self):
            self._text = "第一句"
            self.unified_msg_origin = UMO

        def get_message_str(self):
            return self._text

    monkeypatch.setattr(plugin.ledger, "activate",
                        lambda *a, **kw: {"items": [], "profiles": [],
                                          "flashes": [], "route": "none",
                                          "query": ""})
    monkeypatch.setattr(plugin.ledger, "render_brief", lambda *a, **kw: "")
    asyncio.run(plugin.inject_memory(LockedEvent(), _Req("x")))


# -- 文档漂移：版本号单一来源 -----------------------------------------------------

def test_versions_single_source():
    repo = PLUGIN_DIR
    meta = open(os.path.join(repo, "metadata.yaml"), encoding="utf-8").read()
    m = re.search(r"^version:\s*(\S+)\s*$", meta, re.M)
    proj = open(os.path.join(repo, "pyproject.toml"), encoding="utf-8").read()
    p = re.search(r'^version\s*=\s*"([^"]+)"', proj, re.M)
    src = open(os.path.join(repo, "main.py"), encoding="utf-8").read()
    g = re.search(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"', src, re.M)
    assert m and p and g, "三个版本号声明都得在"
    assert m.group(1) == p.group(1) == g.group(1), \
        "metadata.yaml / pyproject.toml / main.PLUGIN_VERSION 必须一致"

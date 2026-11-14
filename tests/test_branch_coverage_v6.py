# -*- coding: utf-8 -*-
"""行覆盖已 100%，这里补分支覆盖：coverage --branch 报的 partial 逐条对准。

每条用例都对着一行「没走过的跳转」，分两类：
- 公开接口真能走到的：提交上下文里抛异常不封卷、词表缓存无主键、
  线索同强度并列、渲染时闪现超预算、换班/冲刷遇到过期计划项、
  不带路径导出、带脏数据的回放。
- 只有损坏或历史数据才会走到的防御分支：照实造一条脏数据（
  tests/test_ledger_edges.py 已用同样手法往 commit_row 里插行）。
  断言的都是「脏数据不会把账本带崩、也不会静默多算一行」。
"""
import asyncio
import os
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import ledger as L  # noqa: E402


@pytest.fixture()
def led(tmp_path):
    obj = L.Ledger(str(tmp_path / "t.db"))
    yield obj
    try:
        obj.close()
    except Exception:
        pass


def _sql(ledger, sql, params=()):
    with ledger._tx() as cx:
        cx.execute(sql, params)


def _insert_row(ledger, commit_id, seq, tbl, op, row_key="x",
                before=None, after=None):
    _sql(ledger, "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
                 " before_json, after_json) VALUES(?,?,?,?,?,?,?)",
         (commit_id, seq, tbl, op, row_key, before, after))


# -- 提交上下文：异常退出不封卷 ---------------------------------------------

def test_commit_context_exception_does_not_seal(led):
    """with 块里抛异常：__exit__ 不封卷，提交链不能多出一页半截提交。"""
    before = led.stats()["commits"]
    with pytest.raises(ValueError):
        with led.commit("半截提交") as c:
            c.set_grade("m404", 1)
            raise ValueError("中途失败")
    assert led.stats()["commits"] == before
    assert led._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"] == 0


def test_commit_context_normal_exit_seals(led):
    """对照组：正常退出照旧封卷，一次提交真的落库。"""
    res = led.remember("一条")
    with led.commit("改等级") as c:
        assert c.set_grade(res["memory_id"], 2) is True
    assert led._query_one("SELECT grade FROM memory WHERE id = ?",
                          (res["memory_id"],))["grade"] == 2


# -- row_key 解析：空键名那条 ----------------------------------------------

def test_row_key_fields_skips_empty_key():
    """row_key 里出现 "=值"（键名为空）时跳过，不许造出空键。"""
    assert L._row_key_fields("a=1|=2|b=3") == {"a": "1", "b": "3"}
    assert L._row_key_fields("") == {}


# -- 词表缓存：没有主键的行走不进缓存 --------------------------------------

def test_doc_tokens_without_id_is_not_cached(led):
    toks = led._doc_tokens({"summary": "空调 26 度", "tags": ""})
    assert toks
    assert led._tok_cache == {}, "没有 id 的行不该占缓存位置"


def test_clue_tokens_without_id_is_not_cached(led):
    toks = led._clue_tokens("", "热可可")
    assert toks
    assert led._clue_tok_cache == {}, "没有 id 的线索不该占缓存位置"


# -- 线索命中：两条线索同强度并列，不重复改写 ------------------------------

def test_clue_hits_keeps_first_on_tie(led):
    mid = led.remember("医保卡丢了要补办")["memory_id"]
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c1','医保',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c2','医保卡',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES(?, 'c1')", (mid,))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES(?, 'c2')", (mid,))
    led._clue_tok_cache.clear()
    hits = led._clue_hits(["医保"])
    assert set(hits) == {mid}
    # 两条线索强度相同（同一查询词、同一使用次数）：先到的那条占位，后者不覆盖
    assert hits[mid]["clue"] == "医保"


# -- 渲染：闪现那句超预算时整句丢掉 ----------------------------------------

def test_render_brief_drops_flashes_over_budget():
    result = {"items": [{"how": "昨天", "summary": "一条短记忆"}],
              "profiles": [],
              "flashes": ["很长的线索词" * 20, "另一条很长的线索" * 20,
                          "第三条长线索" * 20, "第四条长线索" * 20]}
    stats = {}
    out = L.Ledger.render_brief(result, max_chars=200, stats=stats)
    assert "顺着这些词想到的" not in out
    assert stats["flashes_rendered"] == 0, "没渲染就不许记成渲染过"
    assert stats["rendered"] == 1


# -- 换班/冲刷：计划里的条目已经不在了，不许虚报 ---------------------------

def test_rebalance_skips_stale_plan_entries(led, monkeypatch):
    """计划是算出来的快照；真到写的时候条目可能已经不在库里。

    这种时候 set_grade 返回 False：结果里不能把没改成的算成改成了。
    """
    m1 = led.remember("一级的那条")["memory_id"]
    m2 = led.remember("该复活的那条")["memory_id"]
    with led.commit("先把一条降到零级") as c:
        assert c.set_grade(m2, 0) is True
    plan = {"capacity": 1, "occupied": 1, "overflow": 0,
            "demote": [{"id": "m404"}, {"id": m1}],
            "promote": [{"id": "m404"}, {"id": m2}]}
    monkeypatch.setattr(led, "plan_working_set", lambda capacity=None: plan)
    res = led.rebalance_working_set(apply=True)
    assert res["applied"] is True
    assert res["demoted"] == [m1] and res["promoted"] == [m2], \
        "过期条目不许算成换班成功，真条目照常换"


def test_flush_skips_stale_plan_entries(led, monkeypatch):
    """冲刷计划里指向不存在记忆的条目：deleted/demoted 都不许加数。"""
    m2 = led.remember("会被清掉的那条")["memory_id"]
    plan = [{"memory_id": "m404", "grade": 0, "action": "delete"},
            {"memory_id": "m404", "grade": 1, "action": "demote"},
            {"memory_id": m2, "grade": 0, "action": "delete"}]
    monkeypatch.setattr(led, "flush_plan", lambda: plan)
    res = led.flush(apply=True)
    assert res["applied"] is True
    assert res["deleted"] == 1 and res["demoted"] == 0, \
        "只有真删掉的那条计数；过期条目不许算成降级或删除"


# -- 回放：认不出行身份的脏行要跳过 ----------------------------------------

def test_materialize_ignores_delete_row_without_identity(led):
    """删除行既没有 after 也没有 before：mid 为空，跳过，不许 pop 掉别人。"""
    mid = led.remember("一条")["memory_id"]
    head = led.head()
    _insert_row(led, head, 99, "memory", "delete", "", None, None)
    rows = led.materialize(head)
    assert [r["id"] for r in rows] == [mid]


def test_replay_tables_skips_unusable_rows(led):
    """三种认不出/改不了的行：update 没快照、delete 没 before、delete 连键都没有。"""
    led.remember("一条")
    head = led.head()
    _insert_row(led, head, 90, "memory_clue", "update", "x", None, None)
    _insert_row(led, head, 91, "memory_link", "delete",
                "from_id=a|to_id=b|concept=c", None, None)
    _insert_row(led, head, 92, "memory", "delete", "", None, None)
    state = led._replay_tables(head)
    assert "a|b|c" not in state.get("memory_link", {})
    assert all(k not in (None, "") for k in state.get("memory", {}))


def test_dream_produced_ids_skips_payload_without_memory_id(led):
    """提案 payload 里没有 memory_id：跳过，不许把空串当记忆编号。"""
    _sql(led, "INSERT INTO dream_proposal(id, kind, payload_json, status, created_at)"
              " VALUES('dp-y','new_memory','{\"reason\":\"没有编号\"}','accepted',?)",
         (L.now_str(),))
    assert led.dream_produced_ids() == set()


# -- 导出：不带路径、没有排序提示 ------------------------------------------

def test_export_markdown_without_path(led):
    led.remember("一条")
    text = led.export_markdown()
    assert text.startswith("# 记忆账本")
    assert "一条" in text


def test_dump_jsonl_without_order_hint(led, monkeypatch):
    """某张表没在 _DUMP_ORDER 里登记时，导出退化成无排序，照样完整。"""
    led.upsert_creative("k1", title="测试小说", body="正文")
    monkeypatch.delitem(L._DUMP_ORDER, "creative")
    text = led.dump_jsonl()
    assert '"t": "creative"' in text
    assert "测试小说" in text


# -- 自检：主键为空的行不许把对账带崩 --------------------------------------

def test_health_check_skips_row_without_primary_key(led):
    """SQLite 的 TEXT PRIMARY KEY 允许 NULL；这种行算不出主键，只能跳过。"""
    led.remember("一条")
    _sql(led, "INSERT INTO memory(id, level, grade, importance, summary, tags,"
              " scope_key, created_seq, last_access_seq, access_count,"
              " created_at, updated_at)"
              " VALUES(NULL,'day',1,0.5,'没有主键的行','','',1,0,0,?,?)",
         (L.now_str(), L.now_str()))
    res = led.health_check()
    # 算不出主键的行被跳过：既不当成当前行，也不当成重放多出来的幽灵行
    assert res["ok"] is True
    assert res["stats"]["memories"] == 2, "计数照实报两行"


# -- main.py：没有关键词的沉淀、没有查询词的访问记录 -----------------------

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


class _Event:
    def __init__(self, text="", umo=UMO):
        self._text = text
        self.unified_msg_origin = umo

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return text


def _cmd(plugin, name, text=""):
    async def go():
        return [x async for x in getattr(plugin, name)(_Event(text))]

    return "\n".join(str(x) for x in asyncio.run(go()))


def test_auto_sink_summary_without_keywords(mod):
    """整段原文切不出关键词（只有单字/停用词）：正文就不带「关键词」那半句。"""
    text = mod.auto_sink_summary([{"role": "user", "content": "嗯"}])
    assert text.startswith("最近 1 条原文")
    assert "关键词" not in text


def test_cmd_access_without_query_text(mod):
    """访问记录没有查询词（比如工作记忆复出）：只报时间和记忆号，不编「因为」。"""
    plugin = mod.MemoryCommitPlugin(None)
    mid = plugin.ledger.remember("一条")["memory_id"]
    with plugin.ledger._tx() as cx:
        cx.execute("INSERT INTO access_log(memory_id, at, seq, query, how)"
                   " VALUES(?,?,0,'','recall')", (mid, L.now_str()))
    out = _cmd(plugin, "cmd_access", "/记忆访问")
    assert "最近 1 次被想起" in out
    assert mid in out
    assert "因为「" not in out
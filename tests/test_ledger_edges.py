# -*- coding: utf-8 -*-
"""ledger.py 的边界与异常分支（P0-4：把行覆盖率补到 100%）。

这些用例刻意不追求「业务上常见」，而是把每条 except / 兜底 / 降级分支
真的走一遍：这些分支平时不出声，一旦坏了就是「记忆静悄悄不写」。
"""
import json
import os
import sqlite3
import sys
import time

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


def _ago(days=0.0, hours=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(time.time() - days * 86400 - hours * 3600))


# -- 时间与打分小工具 -------------------------------------------------------

def test_parse_time_variants():
    assert L._parse_time(12.5) == 12.5
    assert L._parse_time("") > 0
    assert L._parse_time("不是时间") > 0
    assert L._parse_time_cached("不是时间") == 0.0


def test_human_ago_all_buckets():
    assert L.human_ago(_ago(hours=0.1)) == "刚刚"
    assert L.human_ago(_ago(hours=3)) == "3 小时前"
    assert L.human_ago(_ago(days=1.2)) == "昨天"
    assert L.human_ago(_ago(days=3)) == "3 天前"
    assert L.human_ago(_ago(days=10)) == "1 周前"
    assert L.human_ago(_ago(days=70)) == "2 个月前"


def test_bm25_edges():
    assert L._bm25_scores([], ["a"]) == []
    assert L._bm25_scores([[], ["空调"]], ["空调"])[0] == 0.0


def test_normalize_limit_bad_value():
    assert L.normalize_limit("很多") == 5
    assert L.normalize_limit(0) == 1


def test_effective_half_life_bad_access_count():
    assert L.effective_half_life(30.0, "很多") == 30.0


def test_working_activation_bad_fields(monkeypatch):
    assert L.working_activation({"created_seq": "x", "importance": "x"}, 3) >= 0
    monkeypatch.setattr(L, "WORKING_ACCESS_HORIZON", 0)
    assert L.working_activation({"created_seq": 1, "created_at": _ago(1)}, 5) > 0


def test_parse_condition_variants():
    assert L.parse_condition({"scene": "公司"}) == {"scene": "公司"}
    assert L.parse_condition("{bad") == {}
    assert L.parse_condition("[1,2]") == {}
    assert L.parse_condition(None) == {}


def test_windows_overlap_missing_window():
    assert L._windows_overlap({"from": "2026-01-01"}, {"from": "2026-01-01"}) is True


def test_mutex_hit_empty():
    assert L._mutex_hit("", "要") is False


def test_match_denial_keywords_empty():
    assert L.match_denial_keywords("") == []


def test_memory_age_days_numeric_start():
    age = L.memory_age_days({"start_time": time.time() - 86400})
    assert 0.9 < age < 1.1


def test_memory_metadata_and_tags_variants():
    assert L._memory_metadata({"metadata": '{"a": 1}'}) == {"a": 1}
    assert L._memory_metadata({"metadata": "{bad"}) == {}
    assert L._memory_tags({"tags": ["甲", " 乙 "]}) == {"甲", "乙"}


def test_vividness_age_exempt_branches():
    assert L.vividness_age_exempt({"importance": "很高"}) is False
    assert L.vividness_age_exempt({"memory_type": "promise"}) is True
    assert L.vividness_age_exempt({"importance": 0.1, "metadata": {"scar_weight": "x"}}) is False
    assert L.vividness({"confidence": "很高"}) in (0, 1, 2)


def test_marker_and_mask_empty():
    assert L.marker_hits("", L.COMMITMENT_MARKERS) == 0
    assert L.mask_false_friends("") == ""


def test_classify_tier_all_branches():
    t = L.classify_tier
    assert t("随便", tags="manual") == L.TIER_IMPRINT
    assert t("") == L.TIER_NORMAL
    assert t("想你，心疼") == L.TIER_IMPRINT
    assert t("想你，我发誓") == L.TIER_IMPRINT
    assert t("想你，吵架", memory_type="assertion") == L.TIER_IMPRINT
    assert t("答应你 我保证 承诺") == L.TIER_IMPRINT
    assert t("第一次 答应你 我保证") == L.TIER_IMPRINT
    assert t("第一次", memory_type="assertion") == L.TIER_IMPRINT
    assert t("吵架 冷战", memory_type="assertion") == L.TIER_IMPRINT
    assert t("给我起名 站你这边") == L.TIER_IMPRINT
    assert t("给我起名 我发誓") == L.TIER_IMPRINT
    assert t("今天天气不错", memory_type="assertion") == L.TIER_IMPORTANT
    assert t("给我起名") == L.TIER_IMPORTANT
    assert t("答应你 我保证") == L.TIER_IMPORTANT
    assert t("第一次 初见") == L.TIER_IMPORTANT
    assert t("第一次 吵架") == L.TIER_IMPORTANT
    assert t("普通的一天") == L.TIER_NORMAL


def test_clamp_unit_bad_value():
    assert L._clamp_unit("很高", 0.25) == 0.25


# -- CommitBuilder ----------------------------------------------------------

def test_push_skips_state_only_update(led):
    led.remember("一条记忆")
    b = led.commit("改访问计数")
    b._push("memory", "update", {"id": "m1"},
            {"access_count": 0}, {"access_count": 1})
    assert b.rows == []


def test_find_memory_falls_back_to_db(led):
    led.remember("落库的一条")
    b = led.commit("查")
    assert b._find_memory("m1")["id"] == "m1"
    assert b._find_memory("m404") is None


def test_ensure_clue_empty_and_cached(led):
    b = led.commit("线索")
    with pytest.raises(L.LedgerError):
        b._ensure_clue("  ")
    first = b._ensure_clue("甲")
    assert b._ensure_clue("甲") == first


def test_add_memory_rejects_empty(led):
    b = led.commit("空")
    with pytest.raises(L.LedgerError):
        b.add_memory("   ")


def test_auto_link_skips_existing(led):
    led.remember("第一条", tags="甲")
    led.remember("第二条", tags="甲")
    led.remember("第三条", tags="甲")
    n = led._query_one("SELECT COUNT(*) AS n FROM memory_link")["n"]
    assert n == 3


def test_set_grade_missing_and_same(led):
    led.remember("一条")
    b = led.commit("调级")
    assert b.set_grade("m404", 0) is False
    assert b.set_grade("m1", 1) is False


def test_forget_missing_and_with_links(led):
    led.remember("第一条", tags="甲")
    led.remember("第二条", tags="甲")
    b = led.commit("删")
    assert b.forget("m404") is False
    assert b.forget("m1") is True
    b.seal()
    assert led.stats()["links"] == 0


def test_upsert_profile_rejects_empty(led):
    b = led.commit("画像")
    with pytest.raises(L.LedgerError):
        b.upsert_profile("master", "   ")


def test_seal_twice_raises(led):
    b = led.commit("一次")
    b.add_memory("内容")
    b.seal()
    with pytest.raises(L.LedgerError):
        b.seal()


# -- 基础设施 --------------------------------------------------------------

def test_schema_version_bad_value(led):
    with led._tx() as cx:
        led._set_meta(cx, "schema_version", "abc")
    assert led.schema_version() == 0


def test_migrate_missing_step(led, monkeypatch):
    with led._tx() as cx:
        led._set_meta(cx, "schema_version", "3")
    monkeypatch.delitem(L._MIGRATIONS, 3)
    with pytest.raises(L.LedgerError):
        led._migrate()


def test_close_swallows_error(led, monkeypatch):
    monkeypatch.setattr(led, "_conn", type("C", (), {
        "close": lambda self: (_ for _ in ()).throw(RuntimeError("关不掉"))})())
    led.close()


def test_wal_bytes_swallows_oserror(led, monkeypatch):
    monkeypatch.setattr(L.os.path, "getsize",
                        lambda p: (_ for _ in ()).throw(OSError("没有")))
    assert led.wal_bytes() == 0


def test_wal_checkpoint_threshold_and_cooldown(led, monkeypatch):
    assert led.maybe_checkpoint_wal(threshold=10 ** 9)["checkpointed"] is False
    monkeypatch.setattr(led, "wal_bytes", lambda: 10 ** 9)
    monkeypatch.setattr(led, "_conn", type("C", (), {
        "execute": lambda self, sql: type("R", (), {
            "fetchone": lambda self: (0, 0, 0)})()})())
    first = led.maybe_checkpoint_wal(threshold=1)
    assert first["checkpointed"] is True
    second = led.maybe_checkpoint_wal(threshold=1)
    assert second["checkpointed"] is False and second["reason"] == "还在冷却里"


def test_wal_checkpoint_busy_and_error(tmp_path):
    led = L.Ledger(str(tmp_path / "w.db"))
    led.remember("写一点让 wal 有点东西")
    monkeypatch_free = led
    monkeypatch_free._conn = type("C", (), {
        "execute": lambda self, sql: type("R", (), {
            "fetchone": lambda self: (1, 0, 0)})()})()
    res = led.maybe_checkpoint_wal(threshold=1)
    assert res["checkpointed"] is False and res["reason"] == "busy"

    led2 = L.Ledger(str(tmp_path / "w2.db"))
    led2.remember("再写一点")
    led2.close()
    res2 = led2.maybe_checkpoint_wal(threshold=1)
    assert res2["checkpointed"] is False and res2["reason"]


# -- 访问痕迹 --------------------------------------------------------------

def test_touch_many_empty(led):
    assert led.touch_many([]) == 0


def test_touch_many_error(led):
    led.remember("一条")
    led.close()
    assert led.touch_many(["m1"]) == 0


def test_access_history_without_id(led):
    led.remember("一条")
    led.touch("m1", query="空调")
    assert led.access_history()


def test_prune_access_log_error(led):
    led.close()
    assert led.prune_access_log() == 0


def test_auto_compact_failure_is_swallowed(led, monkeypatch):
    led.auto_compact_after = 1
    led.remember("先有一条")
    monkeypatch.setattr(led, "compact",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("压不了")))
    led.remember("再来一条")  # 不抛异常即通过


def test_apply_row_unknown_op(led):
    with pytest.raises(L.LedgerError):
        with led._tx() as cx:
            L.Ledger._apply_row(cx, {"tbl": "memory", "op": "bogus",
                                     "where": {"id": "m1"}, "after": {}, "before": None})


# -- 检索 ------------------------------------------------------------------

def test_recall_with_scope_key(led):
    led.remember("空调开 26 度", scope_key="s1")
    led.remember("别的会话的事", scope_key="s2")
    hits = led.recall("空调", scope_key="s1")
    assert [h["id"] for h in hits] == ["m1"]


def test_recall_clue_only_branch(led):
    led.remember("一句看起来无关的话")
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c9','热可可',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c9')")
    hits = led.recall("热可可")
    assert hits and hits[0]["match"].startswith("clue:")


def test_recall_graph_branches(led):
    m1 = led.remember("空调开 26 度", tags="甲", scope_key="s1")["memory_id"]
    m2 = led.remember("另一件不相干的事", tags="甲", scope_key="s1")["memory_id"]
    m3 = led.remember("别的会话的邻居", tags="甲", scope_key="s2")["memory_id"]
    # 指向不存在的记忆：跳过（continue）
    _sql(led, "INSERT INTO memory_link(from_id, to_id, concept, strength, created_at)"
              " VALUES(?,?,?,?,?)", (m1, "m999", "幽灵", 1.0, L.now_str()))
    hits = led.recall("空调", scope_key="s1", min_score=0.0)
    ids = {h["id"] for h in hits}
    assert m2 in ids, "同会话邻居仍沿边带出"
    assert m3 not in ids, "跨会话邻居不再经一跳扩散带出（R6）"


def test_clue_hits_empty_and_no_memory(led):
    assert led._clue_hits([]) == {}
    assert led._clue_hits(["a"]) == {}


def test_clue_bridge_empty_tokens(led):
    assert led._clue_bridge("的") == []


def test_clue_bridge_skips_tokenless_clue(led):
    led.remember("一条记忆", tags="甲")
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c7','的',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c7')")
    led._clue_tok_cache.clear()
    out = led._clue_bridge("的")
    assert out == []


def test_activate_clue_bridge_flash(led):
    led.remember("一句无关的话")
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c9','热可可',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c9')")
    res = led.activate("热可可", slots=2)
    assert res["flashes"]


def test_activate_respects_slots_and_cooldown(led):
    led.remember("空调开 26 度", tags="空调")
    led.remember("空调滤网该洗了", tags="空调")
    first = led.activate("空调", slots=1)
    assert len(first["items"]) == 1
    second = led.activate("空调", slots=1)
    assert len(second["items"]) <= 1


def test_activate_assoc_loop_breaks(led):
    m1 = led.remember("空调开 26 度", tags="甲")["memory_id"]
    m2 = led.remember("第二件事", tags="甲")["memory_id"]
    m3 = led.remember("第三件事", tags="乙")["memory_id"]
    m4 = led.remember("第四件事", tags="丙")["memory_id"]
    for target in (m2, m2, m3, m4):
        _sql(led, "INSERT INTO memory_link(from_id, to_id, concept, strength, created_at)"
                  " VALUES(?,?,?,?,?)", (m1, target, "概念", 1.0, L.now_str()))
    res = led.activate("空调", slots=3)
    assert len(res["items"]) >= 2


def test_fresh_candidates_with_scope(led):
    led.remember("空调开 26 度", scope_key="s1")
    led.remember("不属于这个会话", scope_key="s2")
    res = led.activate("", slots=3, scope_key="s1")
    assert res["items"] and all(r["scope_key"] in ("", "s1") for r in res["items"])


def test_fresh_candidates_skip_quiet(led):
    res = led.remember("被否认的事")
    led.deny_memory(res["memory_id"], "我没说过")
    out = led.activate("", slots=3)
    assert all(r["id"] != res["memory_id"] for r in out["items"])


def test_render_brief_profile_budget_break():
    out = L.Ledger.render_brief({
        "items": [], "profiles": [{"fact": "很长的画像" * 40}]}, max_chars=200)
    assert out == ""


def test_render_brief_items_all_dropped():
    out = L.Ledger.render_brief({
        "items": [{"how": "刚刚", "summary": "很长的摘要" * 40}], "profiles": []},
        max_chars=80)
    assert out == ""


# -- 记忆与画像 ------------------------------------------------------------

def test_forget_missing_raises(led):
    with pytest.raises(L.LedgerError):
        led.forget("m404")


def test_profiles_without_user(led):
    led.remember_profile("master", "怕冷")
    assert led.profiles()


def test_search_messages_filters(led):
    led.append_message("user", "空调 26 度", session_id="s1", at="2026-01-01T00:00:00")
    led.append_message("user", "空调 27 度", session_id="s2", at="2026-06-01T00:00:00")
    rows = led.search_messages("空调", session_id="s1")
    assert len(rows) == 1
    rows2 = led.search_messages("空调", since="2026-05-01T00:00:00")
    assert len(rows2) == 1


def test_verify_message_chain_seq_gap(led):
    led.append_message("user", "第一句")
    led.append_message("user", "第二句")
    _sql(led, "UPDATE message SET seq = 5 WHERE id = 't00002'")
    res = led.verify_message_chain()
    assert res["ok"] is False and "seq" in res["reason"]


def test_verify_message_chain_prev_broken(led):
    led.append_message("user", "第一句")
    led.append_message("user", "第二句")
    _sql(led, "UPDATE message SET prev_id = 't99999' WHERE id = 't00002'")
    res = led.verify_message_chain()
    assert res["ok"] is False and "prev_id" in res["reason"]


def test_working_set_readonly(led):
    led.remember("一条")
    res = led.working_set()
    assert "items" in res


def test_maybe_rebalance_capacity_off(led):
    led.working_capacity = 0
    assert led.maybe_rebalance_working_set()["reason"] == "容量关着"


def test_maybe_rebalance_failure(led, monkeypatch):
    led.working_capacity = 2
    led._rebalanced_at = 0
    monkeypatch.setattr(led, "rebalance_working_set",
                        lambda *a, **kw: (_ for _ in ()).throw(L.LedgerError("换不了")))
    res = led.maybe_rebalance_working_set(cooldown=0)
    assert res["applied"] is False and "换不了" in res["reason"]


def test_show_commit_missing(led):
    assert led.show_commit("C99") is None


def test_compact_stale_head(led, monkeypatch):
    for i in range(12):
        led.remember("记忆 %d" % i)
    monkeypatch.setattr(led, "_meta", lambda cx, k: "别的东西")
    with pytest.raises(L.StaleHeadError):
        led.compact(keep_recent=10)


def test_replay_tables_update_delete_and_unknown(led):
    led.remember("一条", tags="甲")
    led.remember_profile("master", "怕冷")
    led.remember_profile("master", "怕冷")  # update 分支
    led.forget("m1")  # delete 分支
    _sql(led, "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
              " before_json, after_json) VALUES('C1', 99, 'user_denial', 'insert',"
              " 'x', NULL, '{\"id\": 1}')")
    _sql(led, "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
              " before_json, after_json) VALUES('C2', 99, 'memory', 'delete',"
              " 'x', '{\"id\": \"m9\"}', NULL)")
    state = led._replay_tables(led.head())
    assert "profile" in state


# -- v5 职守 ---------------------------------------------------------------

def test_dream_produced_skips_bad_payload(led):
    _sql(led, "INSERT INTO dream_proposal(id, kind, payload_json, status, created_at)"
              " VALUES('dp-x','new_memory','{bad','accepted',?)", (L.now_str(),))
    assert led.dream_produced_ids() == set()


def test_denial_argument_checks(led):
    led.remember("一条")
    with pytest.raises(L.LedgerError):
        led.deny_memory("", "记错了")
    with pytest.raises(L.LedgerError):
        led.deny_memory("m1", "   ")
    with pytest.raises(L.LedgerError):
        led.deny_revoke("")
    with pytest.raises(L.LedgerError):
        led.deny_revoke("m404")
    with pytest.raises(L.LedgerError):
        led.queue_denial_suspect("  ")
    with pytest.raises(L.LedgerError):
        led.set_memory_status("m1", "瞎写")
    with pytest.raises(L.LedgerError):
        led.set_memory_status("m404", "active")


def test_anchor_memory_success(led):
    led.remember("一条")
    res = led.anchor_memory("m1", 1, digest="abc")
    assert res["message_seq"] == 1
    with pytest.raises(L.LedgerError):
        led.anchor_memory("m1", 0)


def test_classify_pair_missing(led):
    with pytest.raises(L.LedgerError):
        led.classify_pair("m1", "m2")


def test_set_condition_missing_memory(led):
    with pytest.raises(L.LedgerError):
        led.set_memory_condition("m404", '{"scene": "公司"}')


def test_dream_add_memory_empty(led):
    with pytest.raises(L.LedgerError):
        led.dream_add_memory("   ")


def test_dream_apply_invalid_kind(led):
    with pytest.raises(L.LedgerError):
        led.dream_apply([{"kind": "瞎写", "payload": {}}], [])


def test_nominate_node_missing(led):
    with pytest.raises(L.LedgerError):
        led.nominate_node("m404")


# -- 导入导出 --------------------------------------------------------------

def test_load_jsonl_errors(tmp_path, led):
    with pytest.raises(L.LedgerError):
        led.load_jsonl(str(tmp_path / "nope.jsonl"))
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(L.LedgerError):
        led.load_jsonl(str(empty))
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{bad\n", encoding="utf-8")
    with pytest.raises(L.LedgerError):
        led.load_jsonl(str(bad))
    newer = tmp_path / "newer.jsonl"
    newer.write_text(json.dumps({"format": L.DUMP_FORMAT, "version": 1,
                                 "schema_version": 99}) + "\n", encoding="utf-8")
    with pytest.raises(L.LedgerError):
        led.load_jsonl(str(newer))


def test_load_jsonl_skips_bad_rows_and_reports(tmp_path):
    """v1 老备份（无完整性清单）保持宽容：坏行跳过计数，不炸整次导入。

    宽容只宽容「坏行」，不宽容「静默丢列」（P0-3）：未知列在 v1 宽容
    路径同样预检拒绝，显式 allow_lossy=True 才按旧行为丢掉并计数。
    """
    target = tmp_path / "t.db"
    src = L.Ledger(str(tmp_path / "src.db"))
    src.remember("搬家用的一条")
    text = src.dump_jsonl()
    src.close()

    lines = text.strip().splitlines()
    header = json.loads(lines[0])
    header["version"] = 1  # 老格式：走宽容路径（v2 严格路径见 test_r_fixes.py）
    header.pop("tables", None)
    header.pop("rows_total", None)
    lines[0] = json.dumps(header, ensure_ascii=False, sort_keys=True)
    lines.insert(1, "不是 JSON")
    lines.insert(2, json.dumps({"t": "nope", "r": {}}))
    lines.insert(len(lines) - 1, json.dumps({"t": "memory", "r": {"zzz": 1}}))
    dump = tmp_path / "dump.jsonl"
    dump.write_text("\n".join(lines) + "\n", encoding="utf-8")

    led = L.Ledger(str(target))
    with pytest.raises(L.LedgerError) as err:
        led.load_jsonl(str(dump))
    assert "memory.zzz" in str(err.value)
    assert "allow_lossy=True" in str(err.value)
    assert led.stats()["memories"] == 0, "预检拒绝后目标库必须一字节没写"

    res = led.load_jsonl(str(dump), allow_lossy=True)
    assert res["skipped"] == 2 and res["memories"] == 1
    assert "memory.zzz" in res["dropped_columns"]
    led.close()


def test_load_jsonl_empty_backup_and_warnings(tmp_path):
    header = json.dumps({"format": L.DUMP_FORMAT, "version": 1, "schema_version": 5})
    dump = tmp_path / "empty.jsonl"
    dump.write_text(header + "\n", encoding="utf-8")
    led = L.Ledger(str(tmp_path / "e.db"))
    res = led.load_jsonl(str(dump))
    assert res["written"] == 0
    led.close()

    weird = tmp_path / "weird.jsonl"
    weird.write_text("\n".join([
        header,
        json.dumps({"t": "meta", "r": {"k": "head", "v": "C99"}}),
    ]) + "\n", encoding="utf-8")
    led2 = L.Ledger(str(tmp_path / "w.db"))
    res2 = led2.load_jsonl(str(weird))
    assert any("找不到" in w for w in res2["warnings"])
    led2.close()

    orphan = tmp_path / "orphan.jsonl"
    orphan.write_text("\n".join([
        header,
        json.dumps({"t": "memory", "r": {"id": "m1", "level": "day", "grade": 1,
                                         "importance": 0.5, "summary": "孤条",
                                         "tags": "", "scope_key": "",
                                         "created_seq": 1, "created_at": L.now_str(),
                                         "updated_at": L.now_str()}}),
    ]) + "\n", encoding="utf-8")
    led3 = L.Ledger(str(tmp_path / "o.db"))
    res3 = led3.load_jsonl(str(orphan))
    assert any("没有 HEAD" in w for w in res3["warnings"])
    led3.close()


def test_maybe_export_daily_failures(led, monkeypatch, tmp_path):
    monkeypatch.setattr(led, "export_markdown",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("导不出")))
    assert led.maybe_export_daily(str(tmp_path / "x.md")) is False

    monkeypatch.undo()
    monkeypatch.setattr(led, "prune_access_log",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("剪不了")))
    assert led.maybe_export_daily(str(tmp_path / "y.md")) is True


# -- 自检 ------------------------------------------------------------------

def test_health_check_head_missing(led):
    led.remember("一条")
    with led._tx() as cx:
        led._set_meta(cx, "head", "C404")
    res = led.health_check()
    assert any("C404" in p for p in res["problems"])


def test_health_check_count_mismatch(led, monkeypatch):
    real = led.stats()

    def fake_stats():
        d = dict(real)
        d["memories"] = real["memories"] + 1
        return d

    monkeypatch.setattr(led, "stats", fake_stats)
    res = led.health_check()
    assert any("记忆计数不一致" in p for p in res["problems"])


def test_health_check_not_writable(led, monkeypatch):
    monkeypatch.setattr(led, "_set_meta",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("只读")))
    res = led.health_check()
    assert any("不可写" in p for p in res["problems"])


def test_health_check_export_failure(led, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    res = led.health_check(str(blocker / "sub" / "x.md"))
    assert any("导出失败" in p for p in res["problems"])


# -- DreamWorker -----------------------------------------------------------

def test_dream_worker_disabled(led):
    assert L.DreamWorker(led, enabled=False).run_once()["skipped"] is True


def test_dream_worker_proposals_and_outputs(led, monkeypatch):
    for i in range(3):
        led.remember("梦见的东西 %d" % i, tags="共同")
    w = L.DreamWorker(led, enabled=True)
    res = w.run_once(max_links=1, produce=1)
    assert res["skipped"] is False
    assert len(res["proposals"]) == 1
    assert res["produced"]


def test_dream_worker_no_shared_tags_and_few_candidates(led):
    led.remember("孤立的一条", tags="甲")
    w = L.DreamWorker(led, enabled=True)
    res = w.run_once(max_links=2)
    assert res["proposals"] == []
    res2 = w.run_once(max_links=2, produce=1)
    assert res2["produced"] == []


def test_dream_worker_swallows_write_error(led, monkeypatch):
    for i in range(2):
        led.remember("梦见的东西 %d" % i, tags="共同")
    monkeypatch.setattr(led, "dream_add_memory",
                        lambda *a, **kw: (_ for _ in ()).throw(L.LedgerError("梦里写不进")))
    res = L.DreamWorker(led, enabled=True).run_once(produce=1)
    assert res["produced"] == []
# -- 100% 覆盖率收尾：剩余分支 ---------------------------------------------

def test_migrate_1_to_2_skips_when_log_exists(tmp_path):
    """已有访问痕迹时不再折算 seed（老库重复迁移不该翻倍）。"""
    path = str(tmp_path / "old.db")
    led = L.Ledger(path)
    led.remember("一条")
    _sql(led, "INSERT INTO access_log(memory_id, at, seq, query, how)"
              " VALUES('m1', ?, 1, '', 'seed:1')", (L.now_str(),))
    with led._tx() as cx:
        led._set_meta(cx, "schema_version", "1")
    led.close()
    again = L.Ledger(path)
    assert again.schema_version() == L.SCHEMA_VERSION
    again.close()


def test_find_memory_from_builder_rows(led):
    b = led.commit("先插一条再找")
    mid = b.add_memory("刚写进快照的一条")
    assert b._find_memory(mid)["id"] == mid


def test_auto_link_skips_existing_pair(led):
    """同一个提交里刚连的边还看不见（要 seal 才落库），库里已有的边必须跳过。"""
    led.remember("第一条", tags="甲")
    _sql(led, "INSERT INTO memory_link(from_id, to_id, concept, strength, created_at)"
              " VALUES('m1','m2','甲',1.0,?)", (L.now_str(),))
    led.remember("第二条", tags="甲")
    assert led.stats()["links"] == 1


def test_clue_bridge_skips_tokenless_clue_with_query(led):
    led.remember("有词的一条", tags="甲")
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c7','的',?)",
         (L.now_str(),))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c7')")
    assert led._clue_bridge("空调") == []


def test_clue_bridge_seen_and_missing_memory(led):
    led.remember("一条")
    for cid, content in (("c8", "热可可"), ("c9", "热可可味"), ("c10", "热可可杯")):
        _sql(led, "INSERT INTO clue(id, content, created_at) VALUES(?,?,?)",
             (cid, content, L.now_str()))
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c8')")
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m1','c9')")
    _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES('m999','c10')")
    out = led._clue_bridge("热可可", limit=6)
    assert len(out) == 1


def test_activate_bridge_flashes_and_slot_break(led):
    ids = [led.remember("很久以前的事 %d" % i)["memory_id"] for i in range(3)]
    _sql(led, "UPDATE memory SET created_at = ?", ("2000-01-01T00:00:00",))
    _sql(led, "INSERT INTO clue(id, content, created_at) VALUES('c9','热可可',?)",
         (L.now_str(),))
    for mid in ids:
        _sql(led, "INSERT INTO memory_clue(memory_id, clue_id) VALUES(?, 'c9')", (mid,))
    res = led.activate("热可可", slots=2)
    assert len(res["items"]) == 2
    assert res["flashes"]


def test_activate_assoc_skips_missing_memory(led):
    m1 = led.remember("空调开 26 度", tags="甲")["memory_id"]
    _sql(led, "INSERT INTO memory_link(from_id, to_id, concept, strength, created_at)"
              " VALUES(?,?,?,?,?)", (m1, "m999", "幽灵", 1.0, L.now_str()))
    res = led.activate("空调", slots=3)
    assert res["items"]


def test_activate_flashes_from_clues_capped(led):
    led.remember("一件有很多概念的事", tags="甲,乙,丙,丁,戊")
    led.remember("另一件也有概念的事", tags="己,庚,辛,壬,癸")
    res = led.activate("", slots=3)
    assert len(res["flashes"]) == 4


def test_wal_checkpoint_swallows_sqlite_error(led, monkeypatch):
    monkeypatch.setattr(led, "wal_bytes", lambda: 10 ** 9)
    led.close()
    res = led.maybe_checkpoint_wal(threshold=1)
    assert res["checkpointed"] is False and res["reason"]


def test_windows_overlap_unreachable_note():
    """窗口缺字段时 _parse_time 会退回「现在」，所以两边都算有窗；
    这条分支只在极端情况下才可能触发，保留用例说明现状。"""
    now = L.now_str()
    assert L._windows_overlap({"from": now, "to": now},
                              {"from": now, "to": now}) is True


def test_dream_worker_two_isolated_candidates(led):
    led.remember("孤立的一", tags="甲")
    led.remember("孤立的二", tags="乙")
    res = L.DreamWorker(led, enabled=True).run_once(max_links=2)
    assert res["proposals"] == []

def test_forget_empty_id_gets_clear_error(led):
    with pytest.raises(L.LedgerError) as err:
        led.forget("  ")
    assert "没有这条记忆" in str(err.value)


def test_clue_weight_guard_on_degenerate_idf(monkeypatch):
    """max_idf 退化到 0 时才走兜底；正常数据到不了这里，用桩把输入压到极限。"""
    monkeypatch.setattr(L.math, "log", lambda x: 1e-12)
    assert L.clue_weight(5, 2) == 1.0


def test_windows_overlap_when_time_unparsable(monkeypatch):
    """两边都解析不出时间窗时按「未知」处理，不误判成时变矛盾。"""
    monkeypatch.setattr(L, "_parse_time", lambda value: 0.0)
    assert L._windows_overlap({"from": "x"}, {"from": "y"}) is True

# -- P0 三条修复的验收用例 --------------------------------------------------

def test_p0_2_profile_lands_and_is_recalled(led):
    """P0-2：直接调 remember_profile 必须真落库，并能被 activate 召回。"""
    assert led.stats()["profiles"] == 0
    res = led.remember_profile("master", "用户怕冷，空调喜欢开 26 度",
                               category="preference")
    assert res["profile_id"] and led.stats()["profiles"] == 1
    row = led._query_one("SELECT fact, category FROM profile WHERE id = ?",
                         (res["profile_id"],))
    assert row["category"] == "preference"

    out = led.activate("空调", with_profile=True)
    assert [p["fact"] for p in out["profiles"]] == ["用户怕冷，空调喜欢开 26 度"]
    assert "用户的事：" in led.render_brief(out)

    # 同一件事重复提到：累加权重，不再新增一条
    led.remember_profile("master", "用户怕冷，空调喜欢开 26 度")
    row = led._query_one("SELECT COUNT(*) AS n FROM profile")
    assert row["n"] == 1
    weight = led._query_one("SELECT weight FROM profile")["weight"]
    assert weight == 2


def test_p0_3_wal_checkpoint_shrinks_file(led):
    """P0-3：连续写入把 -wal 撑大，checkpoint 后要落回去。"""
    for i in range(200):
        led.remember("写很多条记忆来撑大 WAL %d" % i, tags="批量")
    before = led.wal_bytes()
    assert before > 0
    res = led.maybe_checkpoint_wal(threshold=1)
    assert res["checkpointed"] is True
    assert led.wal_bytes() <= before
    # 冷却期内不重复截断
    assert led.maybe_checkpoint_wal(threshold=1)["reason"] in ("还在冷却里", "wal 未超阈值")

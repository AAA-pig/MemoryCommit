# -*- coding: utf-8 -*-
"""ledger.py 的收尾两行：CommitBuilder.retype_memory 的两个 False 早退。

N6 存量迁移把「补类型标记」交给 retype_memory。迁移是幂等的、会被重复执行，
所以「查不到这条」与「tags 没变」都必须稳稳返回 False 而不是抛异常或
白写一页提交——迁移写错一行提交，账本链上就多一条查不出来的空转。
"""
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


def _row_count(ledger):
    return ledger._query_one("SELECT COUNT(*) AS n FROM commit_row")["n"]


def test_retype_memory_missing_id_returns_false(led):
    """库里没有这条记忆：返回 False，不建提交、不写行。"""
    before_commits = led.stats()["commits"]
    before_rows = _row_count(led)
    b = led.commit("改签一个不存在的")
    assert b.retype_memory("m404", "甲,乙") is False
    assert b.rows == []
    assert led.stats()["commits"] == before_commits
    assert _row_count(led) == before_rows


def test_retype_memory_same_tags_returns_false(led):
    """tags 一模一样：返回 False，不留空转提交（迁移重复执行就靠这条）。"""
    res = led.remember("一条带标签的记忆", tags="甲")
    mid = res["memory_id"]
    before_commits = led.stats()["commits"]
    before_rows = _row_count(led)

    b = led.commit("tags 没变的改签")
    assert b.retype_memory(mid, "甲") is False
    assert b.rows == []

    assert led.stats()["commits"] == before_commits
    assert _row_count(led) == before_rows
    row = led._query_one("SELECT tags FROM memory WHERE id = ?", (mid,))
    assert row["tags"] == "甲"


def test_retype_memory_changed_tags_pushes_update(led):
    """对照组：tags 真变了才写一页 update，且 tags 落到记忆行上。"""
    res = led.remember("一条待改签的记忆", tags="甲")
    mid = res["memory_id"]
    b = led.commit("改签")
    assert b.retype_memory(mid, "甲,乙") is True
    assert len(b.rows) == 1
    b.seal()
    row = led._query_one("SELECT tags FROM memory WHERE id = ?", (mid,))
    assert row["tags"] == "甲,乙"

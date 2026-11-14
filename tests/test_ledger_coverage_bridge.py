# -*- coding: utf-8 -*-
"""ledger.py 剩余未覆盖行（retype_memory 的两个早退分支）。

N6 存量迁移只走 commit builder，所以这两条「没这条记忆 / 标签没变」的
早退只有迁移脚本会碰到；它们一旦坏了，迁移会静默把全库改签一遍。
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


def _tags(ledger, memory_id):
    return ledger._query_one(
        "SELECT tags FROM memory WHERE id = ?", (memory_id,))["tags"]


def test_retype_memory_early_returns_and_apply(led):
    led.remember("一条带标签的记忆", tags="旧标签")
    b = led.commit("改签")

    # 记忆不存在：不落行、返回 False
    assert b.retype_memory("m404", "新标签") is False
    assert b.rows == [], "记忆不存在不该产生任何提交行"

    # 标签没变：同样是 False，且不产生 update 行
    assert b.retype_memory("m1", "旧标签") is False
    assert b.rows == [], "标签没变不该写回提交链"

    # 真改：返回 True，封提交后投影表里读得到新标签
    assert b.retype_memory("m1", "新标签") is True
    assert len(b.rows) == 1
    b.seal()
    assert _tags(led, "m1") == "新标签"


def test_retype_memory_empty_tags_counts_as_unchanged(led):
    """原标签为空的记忆，改签成空字符串同样算「没变」，不写提交。"""
    led.remember("没有标签的一条")
    b = led.commit("改签空标签")
    assert b.retype_memory("m1", "") is False
    assert b.rows == []
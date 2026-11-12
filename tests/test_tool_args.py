# -*- coding: utf-8 -*-
"""LLM 工具的参数容错：模型给什么都不能把一条回复炸掉。

工具的参数不是人填的表格，是模型生成的 JSON：可能是 null、数字、带空格的
串、乃至「很高」。以前这些值会一路走到 ledger 里，在 .strip() 或 float()
上抛 AttributeError / ValueError —— 那不是 LedgerError，插件层的 except
接不住，异常直接从工具里溢出去。

这三条是分界线：能修的悄悄修（limit="很多" → 默认 5），修不了的返回一句人话
（content=None → 「没写进去：要记的内容不能为空」）。
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
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main

    yield main
    sys.modules.pop("main", None)


@pytest.fixture()
def plugin(mod):
    return mod.MemoryCommitPlugin(None)


class _Event:
    unified_msg_origin = UMO


def _call(fn, *a, **kw):
    return asyncio.run(fn(_Event(), *a, **kw))


# -- 参数收束的三个小工具 -------------------------------------------------

def test_as_text_rejects_empty(mod):
    from ledger import LedgerError

    for bad in (None, "", "   ", "\n\t"):
        with pytest.raises(LedgerError):
            mod._as_text(bad, "要记的内容")


def test_as_text_stringifies_numbers(mod):
    assert mod._as_text(26) == "26"
    assert mod._as_text("  空调 26 度  ") == "空调 26 度"


def test_as_text_truncates(mod):
    assert len(mod._as_text("a" * 200, "内容", limit=50)) == 50


def test_as_int_falls_back(mod):
    assert mod._as_int("很多", 5) == 5
    assert mod._as_int(None, 5) == 5
    assert mod._as_int("7", 5) == 7
    assert mod._as_int(-3, 5, low=1, high=10) == 1
    assert mod._as_int(999, 5, low=1, high=10) == 10


def test_as_float_rejects_nan_and_inf(mod):
    for bad in ("很高", None, float("nan"), float("inf"), float("-inf")):
        assert mod._as_float(bad, 0.5) == 0.5
    assert mod._as_float("0.85", 0.5) == 0.85


# -- 账本入口本身 ---------------------------------------------------------

def test_remember_none_summary_is_ledger_error(plugin):
    from ledger import LedgerError

    with pytest.raises(LedgerError):
        plugin.ledger.remember(None)


def test_nan_importance_falls_back_to_default(plugin):
    """min(1.0, nan) 在 Python 里返回 1.0：不拦的话「没给值」会被记成满分。"""
    res = plugin.ledger.remember("随手一提的小事", importance=float("nan"))
    row = plugin.ledger._query_one("SELECT importance FROM memory WHERE id=?",
                                   (res["memory_id"],))
    assert row["importance"] == 0.5


# -- 五个工具：坏参数不抛异常 ---------------------------------------------

def test_memory_write_with_bad_content(plugin):
    assert _call(plugin.memory_write, None).startswith("没写进去")
    assert _call(plugin.memory_write, "   ").startswith("没写进去")


def test_memory_write_coerces_loose_args(plugin):
    out = _call(plugin.memory_write, 26, level="世纪", importance="很高")
    assert out.startswith("已写入")
    row = plugin.ledger._query_one("SELECT level, importance FROM memory")
    assert row["level"] == "day"
    assert row["importance"] == 0.5


def test_memory_search_bad_limit(plugin):
    plugin.ledger.remember("用户怕冷，空调开 26 度", tags="偏好")
    assert isinstance(_call(plugin.memory_search, "空调"), str)
    for bad in ("很多", None, -5, 0):
        assert isinstance(_call(plugin.memory_search, "空调", limit=bad), str)


def test_memory_forget_missing_id(plugin):
    assert _call(plugin.memory_forget, None).startswith("没删掉")


def test_memory_deny_missing_id(plugin):
    assert _call(plugin.memory_deny, None, why="记错了").startswith("没标上")


def test_memory_profile_with_bad_fact(plugin):
    assert _call(plugin.memory_profile, None, category="preference").startswith("没记住")

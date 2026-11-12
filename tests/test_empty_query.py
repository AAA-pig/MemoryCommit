# -*- coding: utf-8 -*-
"""memory_search 的空检索词拦截：空串 / 纯空白 / None 都在入口被劝退。

改之前这三种输入不报错也不拦，直接落进 recall，按新鲜度吐一批无关记忆，
看起来像找到了，其实什么都没找。
"""
import asyncio
import os
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 提前写进账本的具体内容。拦截必须连一条都不漏出来，
# 断言取记忆独有词，避开提示文案里的举例词（5433 / 空调）防误判。
SECRETS = ("怕冷", "26 度", "换掉了")


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", "123:FriendMessage:1")
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main

    p = main.MemoryCommitPlugin(main.Context())
    p.ledger.remember("用户怕冷，空调要开 26 度", tags=["习惯", "空调"])
    p.ledger.remember("结算服务的端口从 5433 换掉了", tags=["结算服务"])
    yield p
    sys.modules.pop("main", None)


class _Event:
    unified_msg_origin = "123:FriendMessage:1"


def _search(p, query):
    return asyncio.run(p.memory_search(_Event(), query=query))


def test_empty_string_query_rejected(plugin):
    out = _search(plugin, "")
    assert "检索词是空的" in out
    for s in SECRETS:
        assert s not in out


def test_whitespace_only_query_rejected(plugin):
    out = _search(plugin, "   \t\n ")
    assert "检索词是空的" in out
    for s in SECRETS:
        assert s not in out


def test_none_query_rejected(plugin):
    out = _search(plugin, None)
    assert "检索词是空的" in out
    for s in SECRETS:
        assert s not in out


def test_normal_query_still_finds(plugin):
    out = _search(plugin, "空调")
    assert "检索词是空的" not in out
    assert "怕冷" in out  # 正常关键词照常命中，没把检索弄死

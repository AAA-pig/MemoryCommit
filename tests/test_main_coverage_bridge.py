# -*- coding: utf-8 -*-
"""main.py 剩余未覆盖行：压缩「没压成也不算失败」与 memory_write 的标签归一。

- cmd_compact 在 ledger 判定「提交还少」时的那条回报分支；
- memory_write 收到 list/tuple/set 标签与「非字符串也非集合」标签时的两条归一分支。
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


# -- cmd_compact：确认了但账本判定不用压 ------------------------------------

def test_compact_confirmed_but_ledger_declines(plugin):
    """keep 留得比提交数还宽时，ledger 返回 compacted=False，命令要如实回报。"""
    for i in range(5):
        plugin.ledger.remember("记忆 %d" % i)
    assert plugin.ledger.stats()["commits"] == 5

    out = _cmd(plugin, "cmd_compact", "/记忆压缩 4 确认")

    assert "没什么可压的" not in out, "已确认的分支不该走「没提交」的早退"
    assert "压好了" not in out, "ledger 没压，命令不能报成功"
    assert "提交还少，不用压缩" in out
    assert "一共 5 个提交" in out
    assert plugin.ledger.stats()["commits"] == 5, "没压却动了提交链"


# -- memory_write：标签归一的三条分支 --------------------------------------

def test_memory_write_accepts_sequence_tags(plugin):
    """list/tuple/set 标签按元素转字符串，原样回报在结果里。"""
    out = _call(plugin.memory_write, "喜欢清晨散步", tags=["偏好", "作息"])

    assert out.startswith("已写入")
    assert "['偏好', '作息']" in out
    assert plugin.ledger.stats()["memories"] == 1


def test_memory_write_with_non_string_tags(plugin):
    """既不是字符串也不是集合（如 None）：标签按「无」记，不报错。"""
    out = _call(plugin.memory_write, "这条没有标签", tags=None)

    assert out.startswith("已写入")
    assert "标签：无" in out
    assert "type:" not in out
    row = plugin.ledger._query_one("SELECT tags FROM memory")
    assert (row["tags"] or "") == ""
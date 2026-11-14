# -*- coding: utf-8 -*-
"""main.py 收尾三处：压缩「不用压」分支、memory_write 的标签归一三路。

这三条都是用户真会撞上的：
- /记忆压缩 3 确认 在提交不够多时该说「不用压缩」并报当前提交数，
  而不是回一句压好了（假的成功比报错更坏）。
- 模型给的 tags 可能是串、是列表、也可能是个数字/None：三种都得归一，
  type: 前缀要提醒，不是列表的别硬拆。
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


# -- /记忆压缩：确认了但提交不够，要说清楚「没压」 --------------------------

def test_compact_confirmed_but_nothing_to_do(plugin):
    """提交数 ≤ keep+1 时 compact 返回 compacted=False：命令得照实说。"""
    plugin.ledger.remember("只有一条")
    out = _cmd(plugin, "cmd_compact", "/记忆压缩 3 确认")
    assert "提交还少，不用压缩" in out or "不用压缩" in out
    assert "现在一共" in out
    assert "个提交" in out
    assert "压好了" not in out, "没压却报压好了"
    # 账本没被改动
    assert plugin.ledger.stats()["head"] == plugin.ledger.head()


# -- memory_write：tags 三种形态的归一 --------------------------------------

def test_memory_write_accepts_list_tags_and_warns_on_type_prefix(plugin):
    """模型更爱给 list：列表里的 type: 标签同样要提醒。"""
    out = _call(plugin.memory_write, "喜欢在深夜写代码", tags=["偏好", "type:preference"])
    assert out.startswith("已写入")
    assert "type: 开头的标签是内部类型标记" in out
    assert plugin.ledger.stats()["memories"] == 1


def test_memory_write_accepts_tuple_tags_without_type_prefix(plugin):
    out = _call(plugin.memory_write, "周末爱逛菜市场", tags=("偏好", "生活"))
    assert out.startswith("已写入")
    assert "提醒" not in out


def test_memory_write_non_string_tags_falls_back_to_empty(plugin):
    """给个数字/None：不崩、不硬拆，标签按「无」处理，正文照写。"""
    out = _call(plugin.memory_write, "空调固定 26 度", tags=42)
    assert out.startswith("已写入")
    assert "提醒" not in out
    row = plugin.ledger._query_one("SELECT tags FROM memory")
    assert (row["tags"] or "") == ""

    out_none = _call(plugin.memory_write, "夜里怕吵", tags={"不是": "列表"})
    assert out_none.startswith("已写入")
    assert plugin.ledger.stats()["memories"] == 2
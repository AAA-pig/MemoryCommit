# -*- coding: utf-8 -*-
"""C 批 · 人格渲染收敛测试（C3）。

C3：人格渲染收敛成一份实现——ledger.py 抽模块级纯函数
render_persona_rows(rows, budget=1200)，persona_text 与
main._persona_text_without_core 都复用它。两条路径除「core 行
在不在」外输出必须逐字一致，budget 校验口径完全相同。
"""

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


@pytest.fixture()
def ledmod(mod):
    import ledger

    return ledger


def test_c3_render_fn_exists(ledmod):
    """收敛的硬前提：ledger 模块级纯函数 render_persona_rows 必须存在。"""
    assert callable(ledmod.render_persona_rows)


def test_c3_same_self_rows_text_identical(plugin, monkeypatch):
    """同一批 self 行，正常渲染与降级渲染输出逐字一致。"""
    rows = [{"kind": "self", "content": c}
            for c in ("说话简短", "喜欢安静", "认准的事会坚持")]
    monkeypatch.setattr(plugin.ledger, "list_persona", lambda: rows)
    assert plugin.ledger.persona_text() == plugin._persona_text_without_core()


def test_c3_core_row_is_the_only_difference(plugin, monkeypatch):
    """对照：混一条 kind='core'，正常渲染含它、降级不含，其余逐字相同。

    锁住「唯一差别是 core 在不在」——两条路径除白名单过滤外不许有任何分歧。
    """
    rows = [
        {"kind": "core", "content": "核心基础设定"},
        {"kind": "self", "content": "说话简短"},
        {"kind": "self", "content": "喜欢安静"},
    ]
    monkeypatch.setattr(plugin.ledger, "list_persona", lambda: rows)
    full = plugin.ledger.persona_text()
    degraded = plugin._persona_text_without_core()
    assert "核心基础设定" in full
    assert "核心基础设定" not in degraded
    assert full.replace("- 核心基础设定\n", "", 1) == degraded


def test_c3_budget_truncation_identical(plugin, mod, monkeypatch):
    """超预算时两条路径截断位置与「另有 N 条未注入」数字完全相同。"""
    rows = [{"kind": "self", "content": "第%d条：%s" % (i, "细节内容" * 20)}
            for i in range(30)]
    monkeypatch.setattr(plugin.ledger, "list_persona", lambda: rows)
    full = plugin.ledger.persona_text()
    degraded = plugin._persona_text_without_core()
    assert full == degraded, "同一批行两条路径必须逐字一致"
    # 按预算语义独立复算：每条「- 内容」整条不进，数出能装下几条
    lines = ["- %s" % r["content"] for r in rows]
    used, n_in = 0, 0
    for ln in lines:
        if used + len(ln) > mod.PERSONA_INJECT_BUDGET:
            break
        used += len(ln)
        n_in += 1
    assert full.startswith("\n".join(lines[:n_in]))
    assert full.endswith("（另有 %d 条未注入）" % (len(rows) - n_in))


def test_c3_dirty_kinds_degraded_still_empty(plugin, monkeypatch):
    """全脏 kind（None / "" / "unknown"）时降级路径仍返回空串，防回归。"""
    rows = [
        {"kind": None, "content": "脏行空值"},
        {"kind": "", "content": "脏行空串"},
        {"kind": "unknown", "content": "脏行未知"},
    ]
    monkeypatch.setattr(plugin.ledger, "list_persona", lambda: rows)
    assert plugin._persona_text_without_core() == ""


def test_c3_both_paths_route_through_render_fn(plugin, mod, ledmod, monkeypatch):
    """两条路径都必须真的调 render_persona_rows（防渲染逻辑又抄回一份）。

    from-import 之后两条路径各持有自己的名字：ledger.persona_text 走
    ledger 模块全局名，降级路径走 main 模块全局名，两处都得指向纯函数。
    """
    rows = [{"kind": "self", "content": "说话简短"}]
    monkeypatch.setattr(plugin.ledger, "list_persona", lambda: rows)
    monkeypatch.setattr(ledmod, "render_persona_rows",
                        lambda r, budget=1200: "哨兵渲染结果")
    monkeypatch.setattr(mod, "render_persona_rows",
                        lambda r, budget=1200: "哨兵渲染结果")
    assert plugin.ledger.persona_text() == "哨兵渲染结果"
    assert plugin._persona_text_without_core() == "哨兵渲染结果"


def test_c3_budget_validation_lives_in_render_fn(ledmod):
    """budget 校验（int() 转换失败 / 负数）在纯函数里，口径与原来一致。"""
    with pytest.raises(ledmod.LedgerError):
        ledmod.render_persona_rows([{"kind": "self", "content": "x"}],
                                   budget="不是数字")
    with pytest.raises(ledmod.LedgerError):
        ledmod.render_persona_rows([{"kind": "self", "content": "x"}],
                                   budget=-1)


def test_c3_degraded_route_shares_budget_validation(plugin, mod, ledmod,
                                                    monkeypatch):
    """降级路径传的 PERSONA_INJECT_BUDGET 也过同一校验（坏值必抛不静默）。"""
    monkeypatch.setattr(mod, "PERSONA_INJECT_BUDGET", -1)
    with pytest.raises(ledmod.LedgerError):
        plugin._persona_text_without_core()

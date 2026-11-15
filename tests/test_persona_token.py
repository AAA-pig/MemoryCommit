# -*- coding: utf-8 -*-
"""人格写入令牌（第二道闸 · 正向授权）。

restricted_auto_write 是「进了自动路径就锁」——漏套就绕过。
user_persona_write 反过来：默认就锁，必须显式开。绕过门面层直接调
builder、甚至直接 _push('core_rule')，没有令牌一律拒。

两道闸的关系：守卫优先。令牌开着但人在自动路径里，一样拒——那说明
有人把自动写塞进了用户入口，比漏套更该炸。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger as L  # noqa: E402


def _led(tmp_path):
    return L.Ledger(str(tmp_path / "t.db"))


def test_builder_write_without_token_refused(tmp_path):
    """没有令牌直连 builder：拒，且报错说清缺什么。"""
    led = _led(tmp_path)
    try:
        with pytest.raises(L.LedgerError) as ei:
            with led.commit("直连 builder") as c:
                c.add_persona("self", "没令牌不许写")
        assert "通行令牌" in str(ei.value)
        assert led.list_persona() == []
    finally:
        led.close()


def test_facade_opens_token_by_itself(tmp_path):
    """门面层是用户语义入口：自己开令牌，外面不用包。"""
    led = _led(tmp_path)
    try:
        pid = led.add_persona("self", "门面自带令牌")
        assert pid.startswith("p")
        assert [r["content"] for r in led.list_persona()] == ["门面自带令牌"]
        assert L._PERSONA_WRITE_AUTH.get() == "", "退出后令牌必须收回"
    finally:
        led.close()


def test_token_restored_after_error(tmp_path):
    """with 块里抛别的错，令牌也要归位——不能留在打开状态。"""
    led = _led(tmp_path)
    with pytest.raises(RuntimeError):
        with led.user_persona_write("test"):
            assert L._PERSONA_WRITE_AUTH.get() == "test"
            raise RuntimeError("boom")
    assert L._PERSONA_WRITE_AUTH.get() == ""
    led.close()


def test_token_nested_and_empty_reason_falls_back(tmp_path):
    """嵌套各自复原；空 reason 回落到 user，不留空串当值。"""
    led = _led(tmp_path)
    with led.user_persona_write("outer"):
        with led.user_persona_write(""):
            assert L._PERSONA_WRITE_AUTH.get() == "user"
        assert L._PERSONA_WRITE_AUTH.get() == "outer"
    assert L._PERSONA_WRITE_AUTH.get() == ""
    led.close()


def test_require_returns_reason(tmp_path):
    """令牌值能拿回来：报错/审计时知道是谁开的。"""
    led = _led(tmp_path)
    with led.user_persona_write("manual-check"):
        assert led._require_persona_write_auth("probe") == "manual-check"
    led.close()


def test_push_bottom_guard_blocks_core_rule(tmp_path):
    """最低层兜底：无令牌状态下绕过三个写方法、直接 _push core_rule 也拒。

    必须真正无令牌（默认就是关的，不进 user_persona_write）：令牌开着
    兜底会放行，行攒进 rows，seal 落库才炸 NOT NULL——那不是兜底的错。
    进 with 本身不需要令牌（__enter__ 只是返回 self），所以保持提交流程
    原样、只不开令牌：_push 第一步就抛，__exit__ 收到异常不 seal，
    一行都不落库。
    """
    led = _led(tmp_path)
    try:
        with pytest.raises(L.LedgerError) as ei:
            with led.commit("绕过写方法") as c:
                c._push("core_rule", "insert", {"id": "p999"}, None,
                        {"id": "p999", "kind": "self", "content": "x"})
        assert "底层兜底" in str(ei.value)
        assert c.rows == [], "兜底必须拦在攒行之前，不许留半截提交"
    finally:
        led.close()


def test_guard_wins_over_token(tmp_path):
    """守卫优先于令牌：自动路径里就算手里有令牌，也拒。"""
    led = _led(tmp_path)
    try:
        with led.restricted_auto_write():
            with led.user_persona_write("api"):
                with pytest.raises(L.LedgerError) as ei:
                    led.add_persona("self", "自动路径里带令牌也不行")
        assert "restricted_auto_write" in str(ei.value)
        assert led._auto_write_guard == 0
    finally:
        led.close()


def test_seed_opens_its_own_token(tmp_path):
    """播种是人工发起的操作：_seed_persona_core 自己开 seed 令牌。"""
    led = _led(tmp_path)
    try:
        ids = led._seed_persona_core([{"content": "身份：测试"}])
        assert len(ids) == 1
        assert led._verify_persona_core_seal("seed-test")["ok"]
        assert L._PERSONA_WRITE_AUTH.get() == ""
    finally:
        led.close()

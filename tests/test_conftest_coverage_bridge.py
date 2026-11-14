# -*- coding: utf-8 -*-
"""conftest.py 剩余未覆盖行：假 astrbot 外壳的两处细节。

这份 conftest 只在没有真 AstrBot 时才注入假模块，它自己坏掉的表现是
「main.py 静默 importorskip」——所以这里直接把它当被测对象跑。
"""
import importlib.util
import os
import sys
import types

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFTEST_PATH = os.path.join(PLUGIN_DIR, "conftest.py")


@pytest.fixture()
def shell():
    """独立加载一份 conftest，跑完把 astrbot 相关 sys.modules 还原。"""
    saved = {k: v for k, v in sys.modules.items()
             if k == "astrbot" or k.startswith("astrbot.")}
    for k in list(saved):
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location("mc_conftest_probe", CONFTEST_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    for k in [k for k in sys.modules if k == "astrbot" or k.startswith("astrbot.")]:
        sys.modules.pop(k, None)
    sys.modules.update(saved)


def test_filter_event_message_type_is_a_working_decorator(shell):
    """event_message_type 既可当过滤器用，也要能当无参/带参装饰器透传函数。"""
    filt = shell._Filter()

    plain = filt.event_message_type("message")
    assert isinstance(plain, shell._Anything)
    assert isinstance(plain.any_column, shell._Anything), "假过滤器要吃任意属性"

    def handler():
        return "handled"

    decorated = filt.event_message_type("message")(handler)
    assert decorated is handler, "装饰器形态必须把原函数原样还回来"
    assert decorated() == "handled"


def test_install_stub_keeps_hands_off_real_astrbot(shell, monkeypatch):
    """真 AstrBot 在场时 _install_stub 必须早退，一个假模块都不注入。"""
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: types.SimpleNamespace(origin="real") if name == "astrbot" else None)
    before = {k: id(v) for k, v in sys.modules.items()
              if k == "astrbot" or k.startswith("astrbot.")}

    assert shell._install_stub() is None

    after = {k: id(v) for k, v in sys.modules.items()
             if k == "astrbot" or k.startswith("astrbot.")}
    assert after == before, "真 astrbot 可用时不该覆盖 sys.modules"
# -*- coding: utf-8 -*-
"""conftest.py 的兜底分支（补齐最后两行：event_message_type 与「已装 astrbot」早退）。

这两条平时不响：一个是 _Filter 的第三个装饰器，一个是真装了 astrbot 时的
早退。前者不测等于装饰器写错了也没人知道；后者不测等于「有真包却被 stub
顶掉」这种事故永远只能在现场发现。
"""
import importlib.util
import sys

import pytest


def _load_conftest():
    try:
        import conftest  # pytest 收集时已导入，直接用同一个模块对象
        return conftest
    except ImportError:  # 极端情况下按文件路径兜底
        import os
        root = __file__.rsplit("tests", 1)[0]
        spec = importlib.util.spec_from_file_location(
            "conftest", os.path.join(root, "conftest.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def test_filter_event_message_type_returns_anything():
    """_Filter.event_message_type 必须能当装饰器用，且返回 _Anything。"""
    conf = _load_conftest()
    filt = conf._Filter()

    def handler(event):
        return event

    decorated = filt.event_message_type(conf._Anything())(handler)
    assert decorated is handler, "装饰器要透传原函数，不能把处理函数吞掉"
    assert isinstance(filt.event_message_type(), conf._Anything)


def test_anything_call_passthrough_and_wrap():
    """_Anything 的两条 __call__ 分支：单参可调用透传、其余包成新的 _Anything。"""
    conf = _load_conftest()

    def decorated():
        return 1

    assert conf._Anything()(decorated) is decorated
    assert isinstance(conf._Anything()(1, 2), conf._Anything)
    assert isinstance(conf._Anything()(decorated, key=1), conf._Anything)


def test_install_stub_returns_early_when_astrbot_present(monkeypatch):
    """真装了 astrbot 时 _install_stub 一个字节都不该动 sys.modules。

    不这么钉住的话，有人把早退删了，stub 就会盖掉真包——
    测试全绿，线上插件却拿到一堆 _Anything。
    """
    conf = _load_conftest()
    sentinel = object()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: sentinel)
    before = dict(sys.modules)
    assert conf._install_stub() is None
    assert dict(sys.modules) == before, "已装 astrbot 却仍改写了 sys.modules"
# -*- coding: utf-8 -*-
"""脱离 AstrBot 运行时跑测试时，补一层最小 astrbot 外壳。

test_owners.py 验的是 owners 兜底逻辑（纯标准库，不含 astrbot 依赖），
但 main.py 顶层要 import astrbot.api，缺这个包整个文件就被 importorskip
跳过了 —— 等于 2026-11-11 那次断链的防线在本地从来没真跑过。

只有真实 astrbot 装不出来时才注入假模块，插件跑在 AstrBot 里时零影响。
"""
import importlib.util
import sys
import types


class _Anything:
    """吃任意属性、任意调用；单参数单可调用对象时当成装饰器透传。"""

    def __init__(self, *a, **kw):
        pass

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *a, **kw):
        if len(a) == 1 and not kw and callable(a[0]):
            return a[0]
        return _Anything()


class _Filter(_Anything):
    def command(self, *a, **kw):
        return _Anything()

    def event_message_type(self, *a, **kw):
        return _Anything()

    def llm_tool(self, *a, **kw):
        return _Anything()


class _Star:
    """真 Star 的 __init__ 会吃 context；测试里得让它能被实例化。"""

    def __init__(self, *a, **kw):
        pass


class _Context:
    def __init__(self, *a, **kw):
        pass


def _install_stub():
    if importlib.util.find_spec("astrbot") is not None:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Anything()

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.filter = _Filter()

    star = types.ModuleType("astrbot.api.star")
    star.Context = _Context
    star.Star = _Star
    star.register = _Anything()

    api.event = event
    api.star = star
    astrbot.api = api

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event),
        ("astrbot.api.star", star),
    ):
        sys.modules[name] = mod


_install_stub()

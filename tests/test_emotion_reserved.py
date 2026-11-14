# -*- coding: utf-8 -*-
"""emotion 职守的预留测试（v5 · 施工单 §1.2 / §1.5 / §7）。

记忆、梦境、情感是一体三职，不是三件可拆的：不拆插件、不拆进程、不拆库，
但情感职守本轮只留开关位，不写任何空实现。这个文件钉住的是「预留状态」：
- EMOTION_ENABLED 开关位存在且默认 false
- ledger 里没有任何情感模块的类或函数实现
- 不存在「等拆壳」「以后拆」的表述（那是一体三职的措辞错误）
"""

import importlib.util
import inspect
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 同 test_dream_duty：import 前摘掉环境开关，让「默认关」断言不受宿主环境影响。
os.environ.pop("DREAM_SCHEDULER_ENABLED", None)
os.environ.pop("EMOTION_ENABLED", None)

try:
    import ledger as L  # noqa: E402
except ModuleNotFoundError:
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _spec = importlib.util.spec_from_file_location("ledger", os.path.join(_root, "Ledger.py"))
    L = importlib.util.module_from_spec(_spec)
    sys.modules["ledger"] = L
    _spec.loader.exec_module(L)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}


def _project_py_files():
    for p in sorted(PROJECT_ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


class EmotionReservedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = L.Ledger(os.path.join(self.tmp, "t.db"))
        self.env = mock.patch.dict(os.environ, {"EMOTION_ENABLED": "",
                                                "DREAM_SCHEDULER_ENABLED": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.led.close()

    def test_emotion_switch_reserved_and_defaults_false(self):
        """开关位存在，默认 false（v5 §1.2 职守表：emotion 未建）。"""
        self.assertIn("EMOTION_ENABLED", dir(L))
        self.assertIsInstance(L.EMOTION_ENABLED, bool)
        self.assertFalse(L.EMOTION_ENABLED)

    def test_no_emotion_implementation(self):
        """不写任何空实现：ledger 里没有任何情感职守的类或函数。"""
        offenders = []
        for name, obj in inspect.getmembers(L, predicate=inspect.isclass):
            if "emotion" in name.lower() and obj.__module__ == "ledger":
                offenders.append(name)
        for name, obj in inspect.getmembers(L, predicate=inspect.isfunction):
            if "emotion" in name.lower() and obj.__module__ == "ledger":
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "情感职守只留开关位，不许写空实现：%s" % offenders)
        # 也没有挂到 Ledger 上的情感方法
        methods = [n for n, _ in inspect.getmembers(L.Ledger, predicate=inspect.isfunction)
                   if "emotion" in n.lower()]
        self.assertEqual(methods, [])

    def test_dream_switch_defaults_false(self):
        """对照：梦的开关同样默认 false，影子运行。"""
        self.assertFalse(L.DREAM_SCHEDULER_ENABLED)
        self.assertFalse(L.DreamWorker(self.led).enabled)

    def test_no_split_shells_wording(self):
        """三个职守是一体三职，不存在「暂时不拆、将来拆」的表述。"""
        bad = ("等情感模块长出来再拆" + "壳")
        bad2 = ("不建议现在拆" + "插件")
        offenders = []
        for p in list(_project_py_files()) + [PROJECT_ROOT / "ledger.py"]:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            if bad in text or bad2 in text:
                offenders.append(str(p))
        self.assertEqual(offenders, [],
                         "一体三职不是「暂缓拆分」，措辞必须改：%s" % offenders)

    def test_ledger_still_works_when_all_off(self):
        """开关全关时，memory 职守照常运转（常开，不受预留影响）。"""
        r = self.led.remember("开关全关也照常记账", tags=["验证"])
        self.assertTrue(r["memory_id"].startswith("m"))
        self.assertEqual(self.led.stats()["memories"], 1)
        self.assertTrue(self.led.recall("记账", touch=False))


if __name__ == "__main__":
    unittest.main()

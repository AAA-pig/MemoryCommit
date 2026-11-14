# -*- coding: utf-8 -*-
"""classify_conflict 前置守卫的回归用例（2026-11-15 等价重构后补）。

第 4 段「真互斥候选」原来自己又判了一遍「场景不同」「时间窗不重叠」，
但 892 行（场景不同）和 895 行（时间窗不重叠）早就各自 return 了，
那两行条件恒真、false 分支物理不可达，属于死代码 + 假分支。

重构是删掉那两行条件、把 body 平铺，注释写明「能走到这里，前提是
892/895 两处提前返回仍在」。这组用例就是把这个前提钉住：
  - 场景不同 + 词面互斥 → 仍然是 scene_dependent（不许变成 true_mutex）
  - 时间窗不重叠 + 词面互斥 → 仍然是 temporal（同上）
  - 时间窗重叠（不同窗但相交）/ 干脆没有窗 + 词面互斥 → true_mutex 照旧可达
  - 时间窗重叠但 env 不同 → 仍然 undetermined（env 那道闸没被顺手删掉）
"""
import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import ledger as L  # noqa: E402

MUTEX_A = "空调要开 26 度"
MUTEX_B = "空调不要开，嫌吵"


def _cond(scene=None, window=None, env=None):
    d = {}
    if scene is not None:
        d["scene"] = scene
    if window is not None:
        d["window"] = window
    if env is not None:
        d["env"] = env
    return json.dumps(d, ensure_ascii=False) if d else None


def test_scene_guard_still_wins_over_mutex():
    """892 行先返回：两边场景不同，就算词面互斥也判场景依赖，不判真互斥。"""
    a = _cond("公司", {"from": "2026-11-12", "to": "2026-11-12"}, {"temp": "cold"})
    b = _cond("家", {"from": "2026-11-12", "to": "2026-11-12"}, {"temp": "cold"})
    assert L.classify_conflict(MUTEX_A, MUTEX_B, a, b) == L.CONFLICT_SCENE


def test_window_guard_still_wins_over_mutex():
    """895 行先返回：两边时间窗不重叠，就算词面互斥也判时变，不判真互斥。"""
    a = _cond("家", {"from": "2026-11-01", "to": "2026-11-05"}, {"temp": "cold"})
    b = _cond("家", {"from": "2026-11-20", "to": "2026-11-25"}, {"temp": "cold"})
    assert L.classify_conflict(MUTEX_A, MUTEX_B, a, b) == L.CONFLICT_TEMPORAL


def test_mutex_reachable_when_windows_overlap():
    """时间窗不同但相交：不属于时变，平铺后的互斥判定照旧可达。"""
    a = _cond("家", {"from": "2026-11-01", "to": "2026-11-10"}, {"temp": "cold"})
    b = _cond("家", {"from": "2026-11-05", "to": "2026-11-15"}, {"temp": "cold"})
    assert L.classify_conflict(MUTEX_A, MUTEX_B, a, b) == L.CONFLICT_MUTEX


def test_mutex_reachable_without_windows():
    """一条时间窗都没有：时变条件不成立，直接进互斥候选。"""
    a = _cond("家", None, {"temp": "cold"})
    b = _cond("家", None, {"temp": "cold"})
    assert L.classify_conflict(MUTEX_A, MUTEX_B, a, b) == L.CONFLICT_MUTEX


def test_mutex_still_needs_same_env():
    """env 那道闸仍在：时间窗重叠但环境不同，不判真互斥，挂条件不足等证据。"""
    a = _cond("家", {"from": "2026-11-01", "to": "2026-11-10"}, {"temp": "cold"})
    b = _cond("家", {"from": "2026-11-05", "to": "2026-11-15"}, {"temp": "hot"})
    assert L.classify_conflict(MUTEX_A, MUTEX_B, a, b) == L.CONFLICT_UNDETERMINED
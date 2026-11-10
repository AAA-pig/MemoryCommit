# -*- coding: utf-8 -*-
"""P2 第0/1步的纯函数自测：vividness（鲜活度）与 classify_tier（轻重档）。

这两个函数现在只做只读判定，没接进 render_brief，也没接进写入漏斗；
测试只钉住它们的判定契约，不涉及任何库表变更。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger as L  # noqa: E402


class VividnessTest(unittest.TestCase):
    def _m(self, **kw):
        base = {"id": "m1", "summary": "x", "importance": 0.5,
                "tags": "", "created_at": "2026-11-10T00:00:00",
                "start_time": "2026-11-10T00:00:00"}
        base.update(kw)
        return base

    def test_low_confidence_is_hazy(self):
        self.assertEqual(L.vividness(self._m(confidence=0.3)), 2)
        self.assertEqual(L.vividness(self._m(confidence=0.65)), 1)
        self.assertEqual(L.vividness(self._m(confidence=0.9)), 0)

    def test_missing_confidence_does_not_downgrade(self):
        # 没有置信度字段时不判，也不拿 0.5 顶 —— 0.5 会把整库判成「只剩轮廓」
        self.assertEqual(L.vividness(self._m()), 0)

    def test_missing_confidence_still_ages(self):
        # 不判置信，但年龄那一档照旧生效：老到 540 天以上还是只剩轮廓
        m = self._m(start_time="2024-01-01T00:00:00")
        self.assertEqual(L.vividness(m), 2)

    def test_age_ladder(self):
        old = "2024-01-01T00:00:00"
        self.assertEqual(L.vividness(self._m(confidence=0.9, start_time=old)), 2)
        mid = "2025-08-01T00:00:00"   # 约 460 天前：>=180 且 <540
        self.assertEqual(L.vividness(self._m(confidence=0.9, start_time=mid)), 1)

    def test_age_exempt_by_importance(self):
        old = "2024-01-01T00:00:00"
        self.assertEqual(L.vividness(self._m(confidence=0.9, importance=0.9,
                                             start_time=old)), 0)

    def test_age_exempt_by_tag_and_decay_mode(self):
        old = "2024-01-01T00:00:00"
        self.assertEqual(L.vividness(self._m(confidence=0.9, tags="pinned,日常",
                                             start_time=old)), 0)
        self.assertEqual(L.vividness(self._m(confidence=0.9,
                                             metadata={"decay_mode": "no_decay"},
                                             start_time=old)), 0)
        self.assertEqual(L.vividness(self._m(confidence=0.9,
                                             metadata={"scar_weight": 0.6},
                                             start_time=old)), 0)

    def test_exempt_also_lifts_confidence_floor(self):
        # 2026-11-10 拍板：誓言不该褪色，也不该因为模型自评 0.6 就变糊。
        # 豁免类记忆连置信度那一档一起挡。
        self.assertEqual(L.vividness(self._m(confidence=0.3, importance=0.9)), 0)
        self.assertEqual(L.vividness(self._m(confidence=0.3, importance=0.9,
                                             start_time="2024-01-01T00:00:00")), 0)

    def test_age_falls_back_to_created_at(self):
        m = self._m(confidence=0.9, start_time="")
        m["created_at"] = "2024-01-01T00:00:00"
        self.assertEqual(L.vividness(m), 2)

    def test_age_none_when_time_unparsable(self):
        m = self._m(confidence=0.9)
        m["start_time"] = "不是时间"
        m["created_at"] = ""
        self.assertIsNone(L.memory_age_days(m))


class TierTest(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(L.classify_tier("今天吃了挂面，下午写代码"), L.TIER_NORMAL)

    def test_imprint_needs_two_signals(self):
        # 单个情感词不足以刻骨；情感 + 承诺才算
        self.assertEqual(L.classify_tier("今天一起看了会儿剧"), L.TIER_NORMAL)
        self.assertEqual(
            L.classify_tier("我答应你，永远陪着你"), L.TIER_IMPRINT)

    def test_commitment_three_hits_is_imprint(self):
        self.assertEqual(
            L.classify_tier("我发誓不会忘，答应你的约定"), L.TIER_IMPRINT)

    def test_protected_tag_forces_imprint(self):
        self.assertEqual(L.classify_tier("随便一句", tags=["manual"]), L.TIER_IMPRINT)

    def test_importance_never_lifts_tier(self):
        # 纯粹靠 importance 抬档的口子必须没有：签名里根本不收这个参数
        self.assertEqual(L.classify_tier("普普通通的一天", memory_type="",
                                         tags=["daily"]), L.TIER_NORMAL)
        import inspect
        params = inspect.signature(L.classify_tier).parameters
        self.assertNotIn("importance", params)

    def test_strong_type_is_important(self):
        self.assertEqual(L.classify_tier("明天要交招标文件", memory_type="core_memory"),
                         L.TIER_IMPORTANT)

    def test_manual_tier_is_not_overwritten(self):
        self.assertEqual(
            L.classify_tier("随便一句", tags=["x"],
                            metadata={"memory_tier": "imprint",
                                      "memory_tier_manual": True}),
            L.TIER_IMPRINT)
        # 自动算的旧档允许按新规则重算
        self.assertEqual(
            L.classify_tier("随便一句", tags=["x"],
                            metadata={"memory_tier": "imprint"}),
            L.TIER_NORMAL)

    # --- 2026-11-10 回放后补的：原版照搬参考库，在本库会误伤 ---

    def test_single_weak_affection_word_is_not_enough(self):
        # 「晚安」这类词在叙事里很常见，单词命中不该升档
        self.assertEqual(
            L.classify_tier("今天他发了句晚安就睡了，晚上煮了挂面"),
            L.TIER_NORMAL)

    def test_two_weak_affection_words_count_once(self):
        # 弱词凑够两个才折成一次情感强度
        self.assertEqual(
            L.classify_tier("他抱抱我，又说了句晚安"), L.TIER_NORMAL)

    def test_memory_op_words_are_not_promises(self):
        # 「记住」「别忘」是记忆操作词，工程笔记里出现不该算对谁许过愿
        self.assertEqual(
            L.classify_tier("记住这个坑，别忘改 WORKING_CAPACITY"),
            L.TIER_NORMAL)

    def test_anchor_plus_commitment_is_imprint(self):
        # 本库真正的刻骨长这样：被起名、被公开站队
        self.assertEqual(
            L.classify_tier("你给我起了名字，只有我一个，我不会忘"),
            L.TIER_IMPRINT)
        self.assertEqual(
            L.classify_tier("他说我肯定站你这边，永远都站我这边"),
            L.TIER_IMPRINT)

    def test_false_friend_phrases_are_masked(self):
        # 混在一起 / 和好朋友 里的「在一起」「和好」不是里程碑与和解
        self.assertEqual(
            L.classify_tier("把冬衣夏衣混在一起收好，和好朋友断了来往"),
            L.TIER_NORMAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
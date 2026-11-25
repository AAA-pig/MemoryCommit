# -*- coding: utf-8 -*-
"""记忆提交链（memory commit chain）· 融合版核心。

纯标准库，不依赖 AstrBot，可单独跑测试。

两个来源揉在一起：

A. lsby/ai-memory v0.6.3 · 架构 v15 —— 负责「不会乱、能回放」
   1. 写入只能发生在「提交」里，提交的父节点必须正好是当时的 HEAD，
      否则拒绝（拿数据库做一次 CAS）。
   2. 一个提交挂一串行级快照，按「提交内序号」严格排序，记 before / after。
   3. HEAD 展开成「当前状态」。
   4. 信息冲刷 = 当前最大创建序号 − 该记忆的最后访问序号。
      没有新记忆产生时冲刷量为 0，记忆一点不掉。
   5. 保留指数 = f(重要性, 线索数, 访问次数)，对数级巩固，跌破阈值才降级。

B. biyuehu/animaone —— 负责「记得住、找得回」
   1. 记忆按时间粒度分层：topic / day / week / month / quarter / year。
   2. 标签即概念，写入时和历史节点自动织网连边，检索可顺藤摸瓜。
   3. 中文无词典切词（单字 + 双字滑窗）+ BM25 + 时间半衰期衰减。
   4. 画像事实单独一层（preference / identity / event / relation / dislike）。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import lru_cache, wraps
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__:
    from .ledger_core import (
        FLUSH_K, HALF_LIFE_ACCESS_BOOST, HALF_LIFE_DAYS, MAX_HALF_LIFE_FACTOR, MAX_RECALL_LIMIT,
        STOPWORDS, _ASCII_NUM, _ASCII_WORD, _CJK_RUN, _bm25_scores, _clamp_unit,
        _parse_time_cached, clue_weight, effective_half_life, flush_pressure, normalize_limit,
        now_str, retention_index, ATTITUDE_OWNER_ID, AUTO_SINK_LABEL, AUTO_SINK_MARKERS,
        AUTO_SINK_TYPE_TAG, BASELINE_COMMIT, CLUE_ONLY_BASE, CLUE_SORT_BOOST, CONFLICT_MUTEX,
        CONFLICT_PERSPECTIVE, CONFLICT_SCENE, CONFLICT_TEMPORAL, CONFLICT_UNDETERMINED,
        CORE_API_VERSION, DEFAULT_INJECT_SLOTS, DENIAL_KEYWORDS, DENIED_RECALL_WEIGHT,
        DENY_REVOKE_PREFIX, DENY_STICKY_STATUS, DREAM_CONFIDENCE_CAP, DREAM_MAX_OUTPUTS,
        DREAM_PROVENANCE, DUMP_FORMAT, DUMP_TABLES, DUMP_VERSION, GRADES, LEVELS, LedgerError,
        MAX_GRADE, MEMORY_STATUSES, MUTEX_PAIRS, PERSONA_CORE_HASH_KEY, PERSONA_KINDS,
        PROFILE_VISIBILITIES, PROJECTIONS, REVIVE_HITS, REVIVE_WINDOW_DAYS, STATE_ONLY_COLUMNS,
        StaleHeadError, V8_DUMP_TABLES, WAL_CHECKPOINT_COOLDOWN, WORKING_COOLDOWN_SECONDS,
        WORKING_DEMOTE_LIMIT, ZERO_GRADE_WEIGHT, _DUMP_ORDER, _LOCAL_META, is_machine_tag,
    )
    from .ledger_commit import _compute_persona_core_hash
    from .ledger_persona import (
        AFFECTION_MARKERS, AFFECTION_STRONG_MARKERS, AFFECTION_WEAK_MARKERS, ANCHOR_MARKERS,
        COMMITMENT_MARKERS, CONFLICT_MARKERS, FALSE_FRIEND_PATTERNS, MILESTONE_MARKERS,
        PROTECTED_TAGS, STRONG_MEMORY_TYPES, TIER_CHOICES, TIER_IMPORTANT, TIER_IMPRINT,
        TIER_NORMAL, VIVID_CONF_FULL, VIVID_CONF_FUZZY, VIVID_EXEMPT_DECAY, VIVID_EXEMPT_TAGS,
        VIVID_EXEMPT_TYPES, _memory_metadata, _memory_tags, affection_hits, classify_tier,
        marker_hits, mask_false_friends, memory_age_days, vividness_age_exempt,
    )
    from .ledger_dream import (
        CONDITION_KEYS, _condition_part, _dream_forbidden_tags, _mutex_hit,
        match_denial_keywords, parse_condition,
    )
    from .ledger_schema import (
        DEFAULT_META, SCHEMA_STATEMENTS, SCHEMA_VERSION, _migrate_0_to_1, _migrate_1_to_2,
        _migrate_2_to_3, _migrate_3_to_4, _migrate_4_to_5, _migrate_5_to_6, _migrate_6_to_7,
        _migrate_7_to_8,
    )
    from .ledger_history import _row_key_fields
    from . import (
        ledger_core as _ledger_core, ledger_commit as _ledger_commit, ledger_dream as
        _ledger_dream, ledger_history as _ledger_history, ledger_maintenance as
        _ledger_maintenance, ledger_persona as _ledger_persona, ledger_recall as _ledger_recall,
        ledger_status as _ledger_status, ledger_archive as _ledger_archive, ledger_schema as
        _ledger_schema,
    )
else:
    from ledger_core import (
        FLUSH_K, HALF_LIFE_ACCESS_BOOST, HALF_LIFE_DAYS, MAX_HALF_LIFE_FACTOR, MAX_RECALL_LIMIT,
        STOPWORDS, _ASCII_NUM, _ASCII_WORD, _CJK_RUN, _bm25_scores, _clamp_unit,
        _parse_time_cached, clue_weight, effective_half_life, flush_pressure, normalize_limit,
        now_str, retention_index, ATTITUDE_OWNER_ID, AUTO_SINK_LABEL, AUTO_SINK_MARKERS,
        AUTO_SINK_TYPE_TAG, BASELINE_COMMIT, CLUE_ONLY_BASE, CLUE_SORT_BOOST, CONFLICT_MUTEX,
        CONFLICT_PERSPECTIVE, CONFLICT_SCENE, CONFLICT_TEMPORAL, CONFLICT_UNDETERMINED,
        CORE_API_VERSION, DEFAULT_INJECT_SLOTS, DENIAL_KEYWORDS, DENIED_RECALL_WEIGHT,
        DENY_REVOKE_PREFIX, DENY_STICKY_STATUS, DREAM_CONFIDENCE_CAP, DREAM_MAX_OUTPUTS,
        DREAM_PROVENANCE, DUMP_FORMAT, DUMP_TABLES, DUMP_VERSION, GRADES, LEVELS, LedgerError,
        MAX_GRADE, MEMORY_STATUSES, MUTEX_PAIRS, PERSONA_CORE_HASH_KEY, PERSONA_KINDS,
        PROFILE_VISIBILITIES, PROJECTIONS, REVIVE_HITS, REVIVE_WINDOW_DAYS, STATE_ONLY_COLUMNS,
        StaleHeadError, V8_DUMP_TABLES, WAL_CHECKPOINT_COOLDOWN, WORKING_COOLDOWN_SECONDS,
        WORKING_DEMOTE_LIMIT, ZERO_GRADE_WEIGHT, _DUMP_ORDER, _LOCAL_META, is_machine_tag,
    )
    from ledger_commit import _compute_persona_core_hash
    from ledger_persona import (
        AFFECTION_MARKERS, AFFECTION_STRONG_MARKERS, AFFECTION_WEAK_MARKERS, ANCHOR_MARKERS,
        COMMITMENT_MARKERS, CONFLICT_MARKERS, FALSE_FRIEND_PATTERNS, MILESTONE_MARKERS,
        PROTECTED_TAGS, STRONG_MEMORY_TYPES, TIER_CHOICES, TIER_IMPORTANT, TIER_IMPRINT,
        TIER_NORMAL, VIVID_CONF_FULL, VIVID_CONF_FUZZY, VIVID_EXEMPT_DECAY, VIVID_EXEMPT_TAGS,
        VIVID_EXEMPT_TYPES, _memory_metadata, _memory_tags, affection_hits, classify_tier,
        marker_hits, mask_false_friends, memory_age_days, vividness_age_exempt,
    )
    from ledger_dream import (
        CONDITION_KEYS, _condition_part, _dream_forbidden_tags, _mutex_hit,
        match_denial_keywords, parse_condition,
    )
    from ledger_schema import (
        DEFAULT_META, SCHEMA_STATEMENTS, SCHEMA_VERSION, _migrate_0_to_1, _migrate_1_to_2,
        _migrate_2_to_3, _migrate_3_to_4, _migrate_4_to_5, _migrate_5_to_6, _migrate_6_to_7,
        _migrate_7_to_8,
    )
    from ledger_history import _row_key_fields
    import ledger_core as _ledger_core
    import ledger_commit as _ledger_commit
    import ledger_dream as _ledger_dream
    import ledger_history as _ledger_history
    import ledger_maintenance as _ledger_maintenance
    import ledger_persona as _ledger_persona
    import ledger_recall as _ledger_recall
    import ledger_status as _ledger_status
    import ledger_archive as _ledger_archive
    import ledger_schema as _ledger_schema

log = logging.getLogger(__name__)




SEARCH_CANDIDATE_CAP = 200   # search_messages 候选上限：只取最近这么多条原文做
                             # tokenize / BM25。原文链一直 append-only 往上涨，
                             # 全表逐行 tokenize 会越来越慢；老消息在正常检索里几乎
                             # 用不上，取最近 N 条作候选就够，把每查询的开销压住。
SEARCH_EXPANDED_CAP = 2000   # search_messages 无命中时扩窗重试的上限：老库可能
                             # 把独有词埋在 200 条之外，扩窗到 2000 再扫一遍；
                             # 再找不到就明确标 truncated，别让用户以为「没说过」。

# 工作记忆容量。grade 分级一直没机制，就是缺这个数：
#   二级 = 巩固层，不占名额、不参与挤出；
#   一级 = 工作记忆，名额有限，满了按「此刻有多清楚」从后往前挤到零级；
#   零级 = 沉降层，不主动冒头，但检索还找得到；被反复想起才回到一级。
# 0 = 关掉这套机制（grade 就再也不自己变了）。实例上还有同名属性，外壳和测试能直接改。
WORKING_CAPACITY = int(os.environ.get("JJ_MEMORY_WORKING_CAPACITY", "3") or 0)
WORKING_ACCESS_HORIZON = 12.0   # 最后一次被碰过之后，隔多少个创建序号，「清楚度」掉到 1/e

# WAL：连接长期持有，读多写少，不 checkpoint 的话 -wal 只涨不落
# （实测 3.6MB 从不回落）。超过阈值时在已有维护节流里顺手截断一次。
WAL_CHECKPOINT_BYTES = int(
    os.environ.get("JJ_MEMORY_WAL_CHECKPOINT_BYTES", str(2 * 1024 * 1024)) or 0)

# 自动压缩：提交数超过这个值就顺手压一次。0 = 关，默认关。
# 实例上还有同名属性，测试和外壳可以直接改，不用重载模块。
AUTO_COMPACT_AFTER = int(os.environ.get("JJ_MEMORY_AUTO_COMPACT", "0") or 0)
AUTO_COMPACT_KEEP = max(1, int(os.environ.get("JJ_MEMORY_AUTO_COMPACT_KEEP", "10") or 10))




def render_persona_rows(rows: List[Dict[str, Any]], budget: int = 1200) -> str:
    """人格行渲染（C3 收敛点，纯函数）：每条一行「- {content}」，超预算按整条截断。

    persona_text 与降级渲染（main._persona_text_without_core）都走这里，
    保证两条路径口径完全相同：budget 校验（int() 转换失败或负数抛
    LedgerError）、放不下的整条不拆、末尾另起一行报未注入条数，全在
    这一个函数里。一条都放不下时只报未注入条数；空 rows 返回空串。
    只渲染：不查库、不排序，行序由调用方给的 rows 决定（正常路径是
    list_persona 的 priority DESC, created_at ASC，降级路径沿用白名单
    过滤后的行序）。
    """
    return _ledger_commit.render_persona_rows(rows, budget)


# --------------------------------------------------------------------------
# v5 · 一体三职（2026-11-12 施工单）
#
# 记忆、梦境、情感是同一个系统长出的三种职守，不是三个可拆部件，
# 也不是「暂时合在一起、以后拆开」。卸下来单独用，就不是原来那个东西了。
# 所以：不拆插件、不拆进程、不拆库；但内部职守分清，三个职守各自有
# 自己的写入口、自己的测试文件、自己的开关、自己的失败降级。
# --------------------------------------------------------------------------


# 梦的开关（v5 §5 第 5 步）：影子运行，默认关。开了也只写 dream_proposal
# 与低置信新记忆，不直接写 memory 既有行。
DREAM_SCHEDULER_ENABLED = os.environ.get(
    "DREAM_SCHEDULER_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

# 情感职守：只留开关位，不写任何空实现（v5 §1.2 职守表）。
EMOTION_ENABLED = os.environ.get(
    "EMOTION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")





_MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2, 2: _migrate_2_to_3,
               3: _migrate_3_to_4, 4: _migrate_4_to_5, 5: _migrate_5_to_6,
               6: _migrate_6_to_7, 7: _migrate_7_to_8}







def _parse_time(value: Any) -> float:
    """时间值 → 时间戳；int/float 直通，字符串按运行机器本地时区解释（P1-5）。"""
    return _ledger_core._parse_time(value, _parse_time_cached=_parse_time_cached)

def human_ago(value: Any) -> str:
    """把时间说成人话：刚刚 / 昨天 / 三天前 / 上周。"""
    return _ledger_core.human_ago(value, _parse_time=_parse_time)

def tokenize(text: str) -> List[str]:
    return _ledger_core.tokenize(text)


def time_decay(created_at: Any, half_life_days: float = HALF_LIFE_DAYS,
               access_count: Any = 0) -> float:
    """30 天半衰期：越旧的记忆在检索里越轻，但不归零；常被想起的忘得慢。"""
    return _ledger_maintenance.time_decay(
        created_at, half_life_days, access_count, _parse_time=_parse_time)




def working_activation(memory: Dict[str, Any], created_seq_now: int,
                       recent_hits: int = 0) -> float:
    """一条记忆「此刻有多清楚」——工作记忆名额就按这个排名。

    四样东西相乘：有多重要、本身衰减到哪了、最近有没有被碰过、最近被想起几次。
    第三项是关键：冲刷看的是「多久没被想起」，工作记忆看的是
    「上一次被想起距今隔了几件事」。隔了十来件事还没人提它，就该让位了。
    最后一项是累计访问的补充——想起来十二次的东西，不该因为重要度差 0.05 就出局。
    """
    return _ledger_maintenance.working_activation(
        memory, created_seq_now, recent_hits, WORKING_ACCESS_HORIZON=WORKING_ACCESS_HORIZON, time_decay=time_decay)




def _windows_overlap(wa: Dict[str, Any], wb: Dict[str, Any]) -> bool:
    """两个时间窗是否重叠。解析不出时间的窗按「未知」处理（返回 True 不误分）。"""
    fa, ta = _parse_time(wa.get("from") or ""), _parse_time(wa.get("to") or "")
    fb, tb = _parse_time(wb.get("from") or ""), _parse_time(wb.get("to") or "")
    if not (fa and ta and fb and tb):
        return True  # 有一边没有时间窗，时变无法认定，不算重叠缺失
    return max(fa, fb) <= min(ta, tb)


def classify_conflict(a_summary: str, b_summary: str,
                      a_cond: Any = None, b_cond: Any = None,
                      a_perspective: str = "", b_perspective: str = "") -> str:
    """把一对「打架」的记忆分成四类（或条件不足），只分类，不判决。

    顺序即优先级：场景 > 时变 > 视角 > 真互斥候选 > 条件不足。
    返回值只用于决定挂 conflicted 还是 coexist，永不直接写 corrected——
    corrected 只能走「原文对账 → 追加更正条目」，本轮没有那条路径。
    """
    return _ledger_dream.classify_conflict(
        a_summary, b_summary, a_cond, b_cond, a_perspective, b_perspective, _windows_overlap=_windows_overlap)

def _ctx_time(value: Any) -> float:
    """语境里的时间值 → 时间戳；解析不出返回 0（调用方按「判不出」处理）。"""
    return _ledger_dream._ctx_time(value, _parse_time_cached=_parse_time_cached)

def condition_allows(cond_json: Any, context: Any) -> bool:
    """coexist 的条件在当前语境下是否成立（True 才许主动浮现）。

    判不出一律 False，不许「判不出就放行」：
      - context 缺席 / 不是对象 / 是空的 —— 没有语境无从判起
      - cond_json 空、坏 JSON、解析出来不是对象 —— 没有条件可判
      - cond 里出现 scene/window/env 之外的字段 —— 字段不认识，语义未知
      - 认识的字段缺对应语境数据（要场景没场景、要时间没时间、要环境没环境）
      - cond 所有字段都是空值 —— 等于没写条件，同样判不出
    条件写全且逐项匹配，才放行。
    """
    return _ledger_dream.condition_allows(cond_json, context, _ctx_time=_ctx_time)


# 年龄分级的两个门槛（天），默认值照参考库 memory_humanization：
# 180 天起细节开始糊，540 天起只剩轮廓。进环境变量方便回放时换参数。
VIVID_FULL_DAYS = float(os.environ.get("JJ_MEMORY_VIVID_FULL_DAYS", "180") or 180)
VIVID_FUZZY_DAYS = float(os.environ.get("JJ_MEMORY_VIVID_FUZZY_DAYS", "540") or 540)






def vividness(memory: Dict[str, Any], now: Optional[float] = None) -> int:
    """记忆此刻有多鲜：0=清晰可明说，1=细节模糊只剩大意，2=只剩轮廓印象。

    取「置信度分级」与「年龄分级」中更模糊的一档。置信度缺字段时不判（不给默认值），
    只有年龄说话；豁免类记忆连置信度一起豁免，永远是 0。
    """
    # 置信度缺字段时「不判」，不拿 0.5 当默认值。
    # 原实现照抄参考库的 0.5，在第 0 步回放里把 533 条全判成「只剩轮廓」，
    # 连 importance 0.9 的核心约定（m2/m7）也一样 —— 那不是变淡，那是把整库当成记不清。
    confidence = None
    try:
        raw_conf = (memory or {}).get("confidence")
        if raw_conf is not None and str(raw_conf).strip() != "":
            confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = None
    if confidence is None:
        level = 0
    else:
        level = 2 if confidence < VIVID_CONF_FUZZY else (1 if confidence < VIVID_CONF_FULL else 0)
    if vividness_age_exempt(memory):
        # 2026-11-10 拍板：誓言类不该褪色，也不该因为模型自评 0.6 就变糊。
        # 豁免类（importance ≥ 0.85 / 不失真的衰减模式 / 钉住标签 / 誓言类 / 伤疤）
        # 现在连置信度那一档一起挡，一律按清晰渲染。
        return 0
    age = memory_age_days(memory, now)
    if age is not None:
        if age >= VIVID_FUZZY_DAYS:
            level = max(level, 2)
        elif age >= VIVID_FULL_DAYS:
            level = max(level, 1)
    return level




# --------------------------------------------------------------------------
# 提交构造器
# --------------------------------------------------------------------------



class CommitBuilder:
    """变更先攒着，seal 时一次性写进一个事务。"""

    __init__ = _ledger_commit.__init__

    # -- 内部 --

    def _push(self, tbl: str, op: str, where: Dict[str, Any],
              before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
        # 最底层兜底：core_rule 的任何落库都必须带通行令牌。
        # 放在这里而不是只放在 add/update/remove_persona，是为了挡住
        # 「绕过三个写方法、直接 _push('core_rule', ...)」的未来代码。
        if tbl == "core_rule" and not _PERSONA_WRITE_AUTH.get():
            raise LedgerError(
                "core_rule 写入缺少通行令牌（user_persona_write）："
                "底层兜底拦截，绕过人格写入口的路径不许落库")
        if tbl == "memory":
            # 访问痕迹不是账本内容：从快照里摘掉，免得「想了想」也算一次编辑
            before = ({k: v for k, v in before.items() if k not in STATE_ONLY_COLUMNS}
                      if before is not None else None)
            after = ({k: v for k, v in after.items() if k not in STATE_ONLY_COLUMNS}
                     if after is not None else None)
            if op == "update" and not after:
                return
        self._seq += 1
        key = "|".join(f"{k}={v}" for k, v in where.items())
        self.rows.append({"seq": self._seq, "tbl": tbl, "op": op, "row_key": key,
                          "where": dict(where), "before": before, "after": after})

    _find_memory = _ledger_commit._find_memory
    _find_persona = _ledger_commit._find_persona
    _touch_persona_hash_tx = _ledger_commit._touch_persona_hash_tx
    add_persona = _ledger_commit.add_persona
    update_persona = _ledger_commit.update_persona
    remove_persona = _ledger_commit.remove_persona
    _ensure_clue = _ledger_commit._ensure_clue
    _split_tags = staticmethod(_ledger_commit._split_tags)
    add_memory = _ledger_commit.add_memory
    _auto_link = _ledger_commit._auto_link
    set_grade = _ledger_commit.set_grade
    retype_memory = _ledger_commit.retype_memory
    unlink_edge = _ledger_commit.unlink_edge
    forget = _ledger_commit.forget
    upsert_profile = _ledger_commit.upsert_profile
    _find_v8_row = _ledger_commit._find_v8_row
    add_speaker = _ledger_commit.add_speaker
    _find_profile = _ledger_commit._find_profile
    set_profile_visibility = _ledger_commit.set_profile_visibility
    set_attitude = _ledger_commit.set_attitude
    seal = _ledger_commit.seal
    __enter__ = _ledger_commit.__enter__
    __exit__ = _ledger_commit.__exit__


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------



# 人格写入通行令牌（默认关，正向授权）。
# 和 restricted_auto_write 的反向守卫分工不同：
#   反向守卫 = 「进了自动路径就锁」，漏套一层就绕过去了；
#   通行令牌 = 「默认就锁，必须显式开」，绕过人格写入口直接写 core_rule
#             在 CommitBuilder._push 那一层也会被拒。
# 开令牌的唯一正当理由：用户显式发起的路径（/人格 命令、memory_persona 工具、
# 人工确认的 core 播种）。值记的是发起方，报错时能说清是谁没带令牌。
_PERSONA_WRITE_AUTH: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "persona_write_auth", default="")


def _auto_write_only(func):
    """自动写路径装饰器：方法执行期间人格分区（core_rule）只读。

    用于梦产出等非用户显式发起的写路径：执行期间进入
    restricted_auto_write 上下文，人格写入口（门面层与 builder 层）
    一律硬拦，防止未来有人在自动路径里塞人格写入。
    """
    @wraps(func)
    def wrapper(self, *a, **kw):
        with self.restricted_auto_write():
            return func(self, *a, **kw)
    return wrapper


class Ledger:
    ASSOC_DEPTH = 2  # 联想最多走两步：想到的事，还想得起它连着的事

    # synchronous 档位（P1-4）：NORMAL 偏性能——常见崩溃场景一致，断电
    # 可能丢最近几秒写入；FULL 每次同步落盘，断电几乎不丢，代价是每次
    # 写入都付一次 fsync。档位是显式配置项，非法值直接拒开，不猜。
    DURABILITY_LEVELS = ("NORMAL", "FULL")

    def __init__(self, db_path: str, durability: str = "NORMAL"):
        return _ledger_schema.__init__(
            self, db_path, durability, AUTO_COMPACT_AFTER=AUTO_COMPACT_AFTER, AUTO_COMPACT_KEEP=AUTO_COMPACT_KEEP, WAL_CHECKPOINT_BYTES=WAL_CHECKPOINT_BYTES, WORKING_CAPACITY=WORKING_CAPACITY)
    _init_schema = _ledger_schema._init_schema
    schema_version = _ledger_schema.schema_version

    # -- 自动写入守卫：非用户显式发起的写路径不许碰人格分区 --

    @contextmanager
    def restricted_auto_write(self):
        """自动写入上下文：范围内人格分区（core_rule）只读。

        自动沉淀 / 梦产出等非用户显式发起的写入必须在此范围内开写。
        范围内调人格写入接口（add/update/remove_persona，门面层与
        CommitBuilder 层都拦）直接 LedgerError，不许任何分支静默绕过。
        支持嵌套（计数制），异常路径也能正确退出。
        """
        self._auto_write_guard += 1
        try:
            yield
        finally:
            self._auto_write_guard -= 1

    def _assert_user_write_scope(self) -> None:
        """人格写入口的公共断言：自动写入上下文内一律拒绝。"""
        if self._auto_write_guard:
            raise LedgerError(
                "自动写入上下文（restricted_auto_write）不许碰人格分区"
                "（core_rule）：人格只能由用户显式发起修改")

    @contextmanager
    def user_persona_write(self, reason: str = "user"):
        """正向授权：显式打开人格写权限，退出即收回。

        默认是关的。只有用户显式发起的路径（/人格 命令、memory_persona
        工具、人工确认的播种）才该进这里。支持嵌套，异常路径也归位——
        用 ContextVar 的 token 复原，不自己记账，所以不会出现
        「中途抛错把令牌留在打开状态」。
        """
        token = _PERSONA_WRITE_AUTH.set(reason or "user")
        try:
            yield
        finally:
            _PERSONA_WRITE_AUTH.reset(token)

    def _require_persona_write_auth(self, where: str) -> str:
        """人格写入的双重闸：先反向守卫，再正向令牌。返回发起方标识。

        顺序有讲究：自动路径里即使手里有令牌（比如嵌套调用了用户路径），
        也先按反向守卫拒——那说明有人把自动写塞进了用户入口。
        """
        self._assert_user_write_scope()
        reason = _PERSONA_WRITE_AUTH.get()
        if not reason:
            raise LedgerError(
                "人格写入缺少通行令牌（user_persona_write）：%s 不是用户显式"
                "发起的路径，不许写 core_rule。自动沉淀 / 梦产出等路径请改"
                "写普通记忆，不要碰人格分区。" % where)
        return reason

    def _migrate(self) -> List[int]:
        """把库升到 SCHEMA_VERSION，返回走过的版本号。

        库比代码新的时候直接拒绝，不硬着头皮打开：
        迁移是单向的，降级打开只会把新列悄悄丢掉。
        """
        applied: List[int] = []
        while True:
            cur = self.schema_version()
            if cur == SCHEMA_VERSION:
                return applied
            if cur > SCHEMA_VERSION:
                raise LedgerError(
                    "账本结构版本 %d 比当前代码支持的 %d 还新，先升级代码再打开"
                    % (cur, SCHEMA_VERSION))
            step = _MIGRATIONS.get(cur)
            if step is None:
                raise LedgerError("没有从结构版本 %d 往上升的迁移步骤" % cur)
            with self._tx() as cx:
                step(self, cx)
                self._set_meta(cx, "schema_version", cur + 1)
            applied.append(cur + 1)

    _migrate_auto_sink_legacy = _ledger_maintenance._migrate_auto_sink_legacy


    _tx = contextmanager(_ledger_schema._tx)

    close = _ledger_schema.close

    wal_bytes = _ledger_maintenance.wal_bytes
    def maybe_checkpoint_wal(self, threshold: Optional[int] = None,
                             cooldown: float = WAL_CHECKPOINT_COOLDOWN
                             ) -> Dict[str, Any]:
        return _ledger_maintenance.maybe_checkpoint_wal(self, threshold, cooldown, log=log)
    maybe_checkpoint_wal.__doc__ = _ledger_maintenance.maybe_checkpoint_wal.__doc__

    _meta = _ledger_schema._meta
    _set_meta = _ledger_schema._set_meta
    meta_get = _ledger_schema.meta_get
    meta_set = _ledger_schema.meta_set
    _query_one = _ledger_schema._query_one
    _query_all = _ledger_schema._query_all
    _counters = _ledger_schema._counters

    def _doc_tokens(self, r: Dict[str, Any]) -> List[str]:
        return _ledger_recall._doc_tokens(self, r, tokenize=tokenize)
    _doc_tokens.__doc__ = _ledger_recall._doc_tokens.__doc__
    def _clue_tokens(self, clue_id: str, content: str) -> set:
        """线索的切词结果，内容没变就复用。线索表变动远少于记忆表。"""
        return _ledger_recall._clue_tokens(self, clue_id, content, tokenize=tokenize)
    _trim_tok_cache = _ledger_recall._trim_tok_cache

    head = _ledger_schema.head
    max_created_seq = _ledger_schema.max_created_seq
    max_persona_no = _ledger_schema.max_persona_no

    touch = _ledger_maintenance.touch
    def touch_many(self, memory_ids: Iterable[str], query: str = "",
                   how: str = "manual") -> int:
        return _ledger_maintenance.touch_many(self, memory_ids, query, how, log=log)
    touch_many.__doc__ = _ledger_maintenance.touch_many.__doc__
    access_history = _ledger_maintenance.access_history
    def prune_access_log(self, keep_days: int = 180, keep_per_memory: int = 200
                         ) -> int:
        return _ledger_maintenance.prune_access_log(self, keep_days, keep_per_memory, log=log)
    prune_access_log.__doc__ = _ledger_maintenance.prune_access_log.__doc__


    def _verify_persona_core_seal(self, context: str = "") -> Dict[str, Any]:
        return _ledger_persona._verify_persona_core_seal(self, context, log=log)
    _verify_persona_core_seal.__doc__ = _ledger_persona._verify_persona_core_seal.__doc__
    add_persona = _ledger_persona.add_persona
    add_persona_batch = _ledger_persona.add_persona_batch
    update_persona = _ledger_persona.update_persona
    remove_persona = _ledger_persona.remove_persona
    list_persona = _ledger_persona.list_persona

    def persona_text(self, budget: int = 1200) -> str:
        return _ledger_persona.persona_text(
            self, budget, render_persona_rows=render_persona_rows)
    persona_text.__doc__ = _ledger_persona.persona_text.__doc__

    _seed_persona_core = _ledger_persona._seed_persona_core
    reseal_persona_core = _ledger_persona.reseal_persona_core

    def commit(self, message: str) -> CommitBuilder:
        return CommitBuilder(self, message, self.head())

    def _seal(self, builder: CommitBuilder) -> str:
        return _ledger_history._seal(self, builder)
    def _maybe_auto_compact(self) -> Optional[Dict[str, Any]]:
        return _ledger_history._maybe_auto_compact(self, log=log)
    _maybe_auto_compact.__doc__ = _ledger_history._maybe_auto_compact.__doc__
    _apply_row = staticmethod(_ledger_history._apply_row)

    # -- 便利方法 --

    remember = _ledger_recall.remember
    speakers_of = _ledger_recall.speakers_of
    speaker_memories = _ledger_recall.speaker_memories

    def recall(self, query: str = "", limit: int = 5, scope_key: str = "",
               touch: bool = True, expand: bool = True,
               half_life_days: float = HALF_LIFE_DAYS,
               min_score: float = 0.15,
               exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        return _ledger_recall.recall(
            self, query, limit, scope_key, touch, expand, half_life_days, min_score, exclude, time_decay=time_decay, tokenize=tokenize)
    recall.__doc__ = _ledger_recall.recall.__doc__
    _clue_hits = _ledger_recall._clue_hits
    _fill_clues = _ledger_recall._fill_clues
    def _clue_bridge(self, query: str, limit: int = 6,
                     scope_key: str = "",
                     exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        return _ledger_recall._clue_bridge(
            self, query, limit, scope_key, exclude, time_decay=time_decay, tokenize=tokenize)
    _clue_bridge.__doc__ = _ledger_recall._clue_bridge.__doc__
    def _fresh_candidates(self, limit: int = 12,
                          scope_key: str = "",
                          context: Optional[dict] = None,
                          exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        return _ledger_recall._fresh_candidates(
            self, limit, scope_key, context, exclude, human_ago=human_ago, time_decay=time_decay)
    _fresh_candidates.__doc__ = _ledger_recall._fresh_candidates.__doc__
    def activate(self, query: str = "", slots: int = DEFAULT_INJECT_SLOTS,
                 scope_key: str = "",
                 user_id: str = "master", cooldown_min: float = 25.0,
                 with_profile: bool = True,
                 dry_run: bool = False,
                 context: Optional[dict] = None,
                 profile_visibility: str = "") -> Dict[str, Any]:
        return _ledger_recall.activate(
            self, query, slots, scope_key, user_id, cooldown_min, with_profile, dry_run, context, profile_visibility, human_ago=human_ago, time_decay=time_decay)
    activate.__doc__ = _ledger_recall.activate.__doc__
    @staticmethod
    def render_brief(result: Dict[str, Any], max_chars: int = 420,
                     max_profiles: int = 2, max_item: int = 80,
                     stats: Optional[Dict[str, Any]] = None) -> str:
        """把唤起结果压成一小段能塞进系统提示的话。宁可少说。

        stats 只是观测：传一个空 dict 进来，出来时填上这次渲染的预算账
        （候选几条、真渲染了几条、因为预算丢了几条、最后多长）。
        只记录，不改变任何渲染行为。
        """
        return _ledger_recall.render_brief(
            result, max_chars, max_profiles, max_item, stats, human_ago=human_ago)

    forget = _ledger_recall.forget

    remember_profile = _ledger_persona.remember_profile
    set_profile_visibility = _ledger_persona.set_profile_visibility
    profile_visibility_of = _ledger_persona.profile_visibility_of
    profiles = _ledger_persona.profiles
    set_attitude = _ledger_persona.set_attitude
    get_attitude_detail = _ledger_persona.get_attitude_detail
    get_attitude = _ledger_persona.get_attitude

    # -- 工作记忆 --

    append_message = _ledger_status.append_message
    recent_messages = _ledger_status.recent_messages
    latest_message_seq = _ledger_status.latest_message_seq
    message_sessions = _ledger_status.message_sessions
    count_messages_after = _ledger_status.count_messages_after

    def search_messages(self, query: str = "", limit: int = 10,
                        session_id: str = "", since: str = "",
                        return_meta: bool = False) -> Any:
        return _ledger_status.search_messages(
            self, query, limit, session_id, since, return_meta, SEARCH_CANDIDATE_CAP=SEARCH_CANDIDATE_CAP, SEARCH_EXPANDED_CAP=SEARCH_EXPANDED_CAP, tokenize=tokenize)
    search_messages.__doc__ = _ledger_status.search_messages.__doc__

    verify_message_chain = _ledger_status.verify_message_chain
    message_stats = _ledger_status.message_stats

    _recent_hits = _ledger_maintenance._recent_hits
    def plan_working_set(self, capacity: Optional[int] = None) -> Dict[str, Any]:
        return _ledger_maintenance.plan_working_set(
            self, capacity, working_activation=working_activation)
    plan_working_set.__doc__ = _ledger_maintenance.plan_working_set.__doc__
    working_set = _ledger_maintenance.working_set
    rebalance_working_set = _ledger_maintenance.rebalance_working_set
    def maybe_rebalance_working_set(self, cooldown: float = WORKING_COOLDOWN_SECONDS
                                    ) -> Dict[str, Any]:
        """外壳顺手调的那种：有冷却、不超员就什么都不做，失败绝不往外抛。"""
        return _ledger_maintenance.maybe_rebalance_working_set(self, cooldown, log=log)
    flush_plan = _ledger_maintenance.flush_plan
    flush = _ledger_maintenance.flush

    history = _ledger_history.history
    show_commit = _ledger_history.show_commit
    materialize = _ledger_history.materialize
    _replay_tables = _ledger_history._replay_tables
    compact = _ledger_history.compact

    stats = _ledger_maintenance.stats

    _new_proposal_id = staticmethod(_ledger_status._new_proposal_id)
    _latest_status_map = _ledger_status._latest_status_map
    _coexist_cond_json = _ledger_status._coexist_cond_json
    disputed_ids = _ledger_status.disputed_ids
    auto_sink_ids = _ledger_status.auto_sink_ids
    denied_ids = _ledger_status.denied_ids
    def quiet_ids(self, context: Optional[dict] = None) -> set:
        return _ledger_status.quiet_ids(self, context, condition_allows=condition_allows)
    quiet_ids.__doc__ = _ledger_status.quiet_ids.__doc__

    dream_produced_ids = _ledger_dream.dream_produced_ids

    deny_memory = _ledger_status.deny_memory
    deny_revoke = _ledger_status.deny_revoke
    queue_denial_suspect = _ledger_status.queue_denial_suspect
    pending_denial_queue = _ledger_status.pending_denial_queue
    denial_history = _ledger_status.denial_history
    set_memory_status = _ledger_status.set_memory_status
    current_status = _ledger_status.current_status
    status_timeline = _ledger_status.status_timeline
    anchor_memory = _ledger_status.anchor_memory

    get_creative = _ledger_history.get_creative
    list_creatives = _ledger_history.list_creatives
    upsert_creative = _ledger_history.upsert_creative

    propose_clues = _ledger_dream.propose_clues

    trace_to_source = _ledger_status.trace_to_source
    def classify_pair(self, a_id: str, b_id: str,
                      a_perspective: str = "", b_perspective: str = ""
                      ) -> Dict[str, Any]:
        return _ledger_status.classify_pair(
            self, a_id, b_id, a_perspective, b_perspective, classify_conflict=classify_conflict)
    classify_pair.__doc__ = _ledger_status.classify_pair.__doc__
    set_memory_condition = _ledger_status.set_memory_condition

    @_auto_write_only
    def dream_add_memory(self, summary: str, basis_ids: Iterable[str] = (),
                         tags: Any = "", importance: float = DREAM_CONFIDENCE_CAP,
                         scope_key: str = "") -> Dict[str, Any]:
        """梦产出一条新记忆（v5 §2.1 允许，不是污染）。

        自动写入守卫：执行期间人格分区只读（restricted_auto_write）。
        硬约束（全部硬编码，不可配置调高）：
        - 走 add_memory，登记 provenance='dream'（真值在 dream_proposal，
          与 memory 插入同事务——三道锁靠这张表认梦产出）
        - 置信上限 DREAM_CONFIDENCE_CAP=0.4
        - tags 剔除保护标签、level 恒为 day、grade 从 0 起：永不进 no_cold 白名单
        - 单次梦最多产出 DREAM_MAX_OUTPUTS=2 条（DreamWorker.run_once 控制）
        - 永不进 no_cold、永不可升为节点（nominate_node 拒绝）
        - scope_key 必须由调用方显式传入（R6）：不再无脑落全局共享域；
          传空串就是显式决定「这条梦全局可见」，不是默认行为。
        """
        return _ledger_dream.dream_add_memory(
            self, summary, basis_ids, tags, importance, scope_key, CommitBuilder=CommitBuilder)
    dream_apply = _auto_write_only(_ledger_dream.dream_apply)
    nominate_node = _ledger_dream.nominate_node

    export_markdown = _ledger_archive.export_markdown
    dump_jsonl = _ledger_archive.dump_jsonl
    _table_columns = _ledger_archive._table_columns
    _write_dump_rows = _ledger_archive._write_dump_rows

    def _rehearse_restore(self, rows: Dict[str, List[Dict[str, Any]]]) -> None:
        return _ledger_archive._rehearse_restore(self, rows, Ledger=Ledger)
    _rehearse_restore.__doc__ = _ledger_archive._rehearse_restore.__doc__

    def load_jsonl(self, path: str, replace: bool = False,
                   strict: bool = False, allow_lossy: bool = False
                   ) -> Dict[str, Any]:
        return _ledger_archive.load_jsonl(self, path, replace, strict, allow_lossy, log=log)
    load_jsonl.__doc__ = _ledger_archive.load_jsonl.__doc__
    def maybe_export_daily(self, path: str = "") -> bool:
        return _ledger_archive.maybe_export_daily(self, path, log=log)
    maybe_export_daily.__doc__ = _ledger_archive.maybe_export_daily.__doc__
    health_check = _ledger_archive.health_check



class DreamWorker:
    """梦境职守骨架。

    开关 DREAM_SCHEDULER_ENABLED 默认 false（v5 §5 第 5 步：影子运行）。
    影子阶段：连线只进 dream_proposal（kind='link'），不真写 memory_link；
    产出走 dream_add_memory（低置信、可删、单次 ≤2 条）。失败整轮放弃，
    不留半条。
    """

    def __init__(self, ledger: Ledger, enabled: Optional[bool] = None):
        self.ledger = ledger
        self.enabled = bool(DREAM_SCHEDULER_ENABLED if enabled is None else enabled)

    run_once = _ledger_dream.run_once

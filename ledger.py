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
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

# 表结构版本。加列、改列、加表才 +1，并在 _MIGRATIONS 里补一步迁移。
# 这个号跟着库走（存在 meta 表里），不同宿主之间搬库全靠它认路。
# v2：访问痕迹（access_count / last_access_seq）不再是账本内容，另立 access_log 表。
# v3：原文链 message 表。
# v4：message 表补 seq 索引。
SCHEMA_VERSION = 4

# 给宿主看的接口版本。公开方法的签名/返回结构变了才 +1，跟表结构是两件事：
# 换了个内核但数据没动，只该动这个；数据加了列，只该动上面那个。
CORE_API_VERSION = 1

# 可往返导出的文件：格式名 + 文件自身版本（排版变了才 +1）
DUMP_FORMAT = "jj-memory-dump"
DUMP_VERSION = 1

# 搬家时要带走的表，顺序按「先元信息、后投影」，导入时不会因为顺序互相绊住
DUMP_TABLES = ("meta", "mcommit", "commit_row", "memory", "clue",
               "memory_clue", "memory_link", "profile", "core_rule",
               "message")

# 每张表导出时的稳定排序，纯粹为了 diff 好看，往返不依赖顺序
_DUMP_ORDER = {
    "meta": "k",
    "mcommit": "id",
    "commit_row": "commit_id, seq",
    "memory": "created_seq, id",
    "clue": "id",
    "memory_clue": "memory_id, clue_id",
    "memory_link": "id",
    "profile": "user_id, id",
    "core_rule": "id",
    "message": "seq", 
}

# meta 里属于「这台机器此刻的状态」而不属于「账本内容」的键，导出时跳过
_LOCAL_META = frozenset({"last_export_date", "health_probe"})

LEVELS = ("topic", "day", "week", "month", "quarter", "year")
GRADES = {0: "零级", 1: "一级", 2: "二级"}
MAX_GRADE = 2

FLUSH_K = 6.0          # 冲刷压力分母系数
HALF_LIFE_DAYS = 30.0  # 检索的时间半衰期（animaone 用的是 30 天）

CLUE_SORT_BOOST = 0.6  # 线索权重对主排序的放大上限（稀有线索最多 ×1.6）
CLUE_ONLY_BASE = 0.35  # 只有线索撞上、词面没命中时的兜底分
MAX_RECALL_LIMIT = 50        # 一次最多唤起多少条，再大就夹住
DEFAULT_INJECT_SLOTS = 3     # 每轮最多往提示词里放几条（「说出口」的上限）。
                             # 别和 WORKING_CAPACITY（工作记忆能坐几人）混：
                             # 一个是这一轮说几句，一个是长期留几个位置。
SEARCH_CANDIDATE_CAP = 200   # search_messages 候选上限：只取最近这么多条原文做
                             # tokenize / BM25。原文链一直 append-only 往上涨，
                             # 全表逐行 tokenize 会越来越慢；老消息在正常检索里几乎
                             # 用不上，取最近 N 条作候选就够，把每查询的开销压住。
SEARCH_EXPANDED_CAP = 2000   # search_messages 无命中时扩窗重试的上限：老库可能
                             # 把独有词埋在 200 条之外，扩窗到 2000 再扫一遍；
                             # 再找不到就明确标 truncated，别让用户以为「没说过」。
HALF_LIFE_ACCESS_BOOST = 0.5  # 每被访问一次，半衰期的对数级加成
MAX_HALF_LIFE_FACTOR = 5.0    # 访问再多，半衰期最多拉长到 5 倍

# 工作记忆容量。grade 分级一直没机制，就是缺这个数：
#   二级 = 巩固层，不占名额、不参与挤出；
#   一级 = 工作记忆，名额有限，满了按「此刻有多清楚」从后往前挤到零级；
#   零级 = 沉降层，不主动冒头，但检索还找得到；被反复想起才回到一级。
# 0 = 关掉这套机制（grade 就再也不自己变了）。实例上还有同名属性，外壳和测试能直接改。
WORKING_CAPACITY = int(os.environ.get("JJ_MEMORY_WORKING_CAPACITY", "3") or 0)
WORKING_ACCESS_HORIZON = 12.0   # 最后一次被碰过之后，隔多少个创建序号，「清楚度」掉到 1/e
WORKING_DEMOTE_LIMIT = 20       # 一次最多挤掉几条：分期收敛，提交链上不留一坨两百行的快照
REVIVE_WINDOW_DAYS = 3.0        # 零级记忆在这个时间窗里被想起……
REVIVE_HITS = 2                 # ……这么多次，就让它回到工作记忆
ZERO_GRADE_WEIGHT = 0.45        # 零级在自动激活里的分量：不主动冒头，但没被拉黑
WORKING_COOLDOWN_SECONDS = 6 * 3600.0  # maybe_rebalance 最短多久真跑一次

# 自动压缩：提交数超过这个值就顺手压一次。0 = 关，默认关。
# 实例上还有同名属性，测试和外壳可以直接改，不用重载模块。
AUTO_COMPACT_AFTER = int(os.environ.get("JJ_MEMORY_AUTO_COMPACT", "0") or 0)
AUTO_COMPACT_KEEP = max(1, int(os.environ.get("JJ_MEMORY_AUTO_COMPACT_KEEP", "10") or 10))

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY, v TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mcommit (
        id TEXT PRIMARY KEY,
        parent TEXT,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS commit_row (
        commit_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        tbl TEXT NOT NULL,
        op TEXT NOT NULL,
        row_key TEXT NOT NULL,
        before_json TEXT,
        after_json TEXT,
        PRIMARY KEY (commit_id, seq))""",
    """CREATE TABLE IF NOT EXISTS memory (
        id TEXT PRIMARY KEY,
        level TEXT NOT NULL DEFAULT 'day',
        grade INTEGER NOT NULL DEFAULT 1,
        importance REAL NOT NULL DEFAULT 0.5,
        summary TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        start_time TEXT,
        end_time TEXT,
        created_seq INTEGER NOT NULL,
        last_access_seq INTEGER NOT NULL DEFAULT 0,
        access_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS clue (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_clue (
        memory_id TEXT NOT NULL,
        clue_id TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (memory_id, clue_id))""",
    """CREATE TABLE IF NOT EXISTS memory_link (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        concept TEXT NOT NULL,
        strength REAL NOT NULL DEFAULT 1.0,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS profile (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        fact TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'event',
        weight INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS core_rule (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""",
    # 访问日志：谁在什么时候因为什么被想起。
    # 这不是账本内容，是机器的体温计 —— 只用于算衰减、算保留指数，
    # 所以不进提交链、不参与回放，被想起一万次也不会让提交历史变长。
    """CREATE TABLE IF NOT EXISTS access_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        at TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        query TEXT NOT NULL DEFAULT '',
        how TEXT NOT NULL DEFAULT '')""",
    # 原文链：一条消息一个节点，digest 吃上一条的 digest，改一个字整条对不上。
    # 故意不进提交链——一天几百条消息塞进 mcommit，「记忆的历史」会被稀释成消息流水。
    # 它要的是不可篡改、查得到原话，不是能回滚，所以用哈希链而不是 commit。
    """CREATE TABLE IF NOT EXISTS message (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        at TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        prev_id TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_message_at ON message(at)",
    "CREATE INDEX IF NOT EXISTS idx_message_session ON message(session_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_access_memory ON access_log(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_access_at ON access_log(at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_grade ON memory(grade)",
    "CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_seq)",
    "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_link_from ON memory_link(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_link_concept ON memory_link(concept)",
    "CREATE INDEX IF NOT EXISTS idx_profile_user ON profile(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_message_seq ON message(seq)",
)

DEFAULT_META = {
    "head": "",
    "created_seq": "0",
    "commit_no": "0",
    "memory_no": "0",
    "clue_no": "0",
    "profile_no": "0",
    "message_no": "0",
    "message_head": "",
}

# 压缩后用来接管历史的基线提交（序号 0，永远排在最前）
BASELINE_COMMIT = "C0"


# ----------------------------------------------------------------------
# 结构迁移：键是「从哪个版本升」，函数只做一步，版本号由 _migrate() 统一写
# ----------------------------------------------------------------------

def _migrate_0_to_1(led: "Ledger", cx: sqlite3.Connection) -> None:
    """0 → 1：给最早那批库补上结构版本号。

    那时候的表结构和 v1 完全一致（都是同一份 SCHEMA_STATEMENTS 建的），
    所以这一步只做标记，不碰任何投影表。
    """
    return None


def _migrate_1_to_2(led: "Ledger", cx: sqlite3.Connection) -> None:
    """1 → 2：访问痕迹搬到 access_log，不再跟着提交链一起长。

    历史里已经出现过的 access_count 没法还原成「哪天、因为什么」的明细，
    所以不编造：给每条被翻过的记忆留一条 seed，how 里写明是迁移前累计，
    让后来的审计看得出这条记录是折算来的，不是真的那一次。
    """
    for stmt in (
        """CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            at TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0,
            query TEXT NOT NULL DEFAULT '',
            how TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX IF NOT EXISTS idx_access_memory ON access_log(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_access_at ON access_log(at)",
    ):
        cx.execute(stmt)
    has = cx.execute("SELECT COUNT(*) AS n FROM access_log").fetchone()["n"]
    if has:
        return
    for row in cx.execute("SELECT id, access_count, last_access_seq, updated_at"
                          " FROM memory WHERE access_count > 0"):
        cx.execute("INSERT INTO access_log(memory_id, at, seq, query, how)"
                   " VALUES(?,?,?,?,?)",
                   (row["id"], row["updated_at"] or "", int(row["last_access_seq"] or 0),
                    "", "seed:%d" % int(row["access_count"])))
    return None


def _migrate_2_to_3(led: "Ledger", cx: sqlite3.Connection) -> None:
    """2 → 3：加原文链（message 表）。

    纯加法，老数据一条不动。表由 SCHEMA_STATEMENTS 建好，
    这里只补两个 meta 指针，让序号和链尾从 0 起步。
    """
    for k, v in (("message_no", "0"), ("message_head", "")):
        cx.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))
    return None


def _migrate_3_to_4(led: "Ledger", cx: sqlite3.Connection) -> None:
    """3 → 4：message 表补 seq 索引。

    append 每条都 `ORDER BY seq DESC LIMIT 1` 拿链尾，表一大就变
    SCAN + TEMP B-TREE。补上索引后这条查走索引，不再全表扫。
    """
    cx.execute("CREATE INDEX IF NOT EXISTS idx_message_seq ON message(seq)")
    return None


_MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2, 2: _migrate_2_to_3,
               3: _migrate_3_to_4}


# memory 表里这俩列不是「记忆内容」，是「记忆被翻过多少次」的缓存。
# 它们不进提交快照：想一次不改变记忆本身，不值得在账本里留一页。
# 真值以 access_log 为准，这两列只是为了让衰减计算不用每次去 COUNT。
STATE_ONLY_COLUMNS = frozenset({"access_count", "last_access_seq"})

# 每张投影表的行主键：回放/压缩时用来认「这是不是同一行」
PROJECTIONS = {
    "memory": lambda r: r.get("id"),
    "clue": lambda r: r.get("id"),
    "memory_clue": lambda r: "%s|%s" % (r.get("memory_id"), r.get("clue_id")),
    "memory_link": lambda r: "%s|%s|%s" % (r.get("from_id"), r.get("to_id"), r.get("concept")),
    "profile": lambda r: r.get("id"),
    "core_rule": lambda r: r.get("id"),
}

STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "和", "就", "也",
    "都", "又", "还", "这", "那", "有", "没", "不", "很", "吗", "呢", "吧",
    "啊", "呀", "哦", "嗯", "把", "被", "给", "让", "对", "从", "到", "会",
}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_ASCII_NUM = re.compile(r"\d{2,}")


class LedgerError(RuntimeError):
    """提交链层面的错误。"""


class StaleHeadError(LedgerError):
    """开提交时记下的 HEAD 已经不是当前 HEAD。"""


def now_str() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return time.time()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(value)[:19], fmt))
        except Exception:
            continue
    return time.time()


# --------------------------------------------------------------------------
# 切词与打分（animaone：单字 + 双字滑窗，无词典）
# --------------------------------------------------------------------------

def human_ago(value: Any) -> str:
    """把时间说成人话：刚刚 / 昨天 / 三天前 / 上周。"""
    ts = _parse_time(value)
    delta = max(0.0, time.time() - ts)
    if delta < 3600:
        return "刚刚"
    if delta < 86400:
        return "%d 小时前" % int(delta // 3600)
    if delta < 172800:
        return "昨天"
    if delta < 604800:
        return "%d 天前" % int(delta // 86400)
    if delta < 2592000:
        return "%d 周前" % max(1, int(delta // 604800))
    return "%d 个月前" % max(1, int(delta // 2592000))


def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    tokens: List[str] = []
    for w in _ASCII_WORD.findall(text):
        tokens.append(w)
    for n in _ASCII_NUM.findall(text):
        tokens.append(n)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            if run not in STOPWORDS:
                tokens.append(run)
            continue
        # 只用双字滑窗：单字命中太吵，「杯子」的「子」会撞上「量子」
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram[0] in STOPWORDS and gram[1] in STOPWORDS:
                continue
            tokens.append(gram)
    return tokens


def _bm25_scores(docs: List[List[str]], query: List[str],
                 k1: float = 1.5, b: float = 0.75) -> List[float]:
    n = len(docs)
    if n == 0 or not query:
        return [0.0] * n
    avgdl = sum(len(d) for d in docs) / max(1, n) or 1.0
    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    q_terms = set(query)
    scores = [0.0] * n
    for i, d in enumerate(docs):
        if not d:
            continue
        dl = len(d)
        tf: Dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        for term in q_terms:
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + k1 * (1 - b + b * dl / avgdl)
            scores[i] += idf * (f * (k1 + 1)) / denom
    return scores


def normalize_limit(limit: Any, default: int = 5) -> int:
    """把调用方给的 limit 收进 [1, MAX_RECALL_LIMIT]：字符串、0、负数、超大值都不许炸。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(MAX_RECALL_LIMIT, n))


def effective_half_life(base_half_life: float = HALF_LIFE_DAYS,
                        access_count: Any = 0) -> float:
    """被访问过的记忆衰减得更慢：访问次数对数级拉长半衰期，但有上限。

    常被想起的事应该在时间里站得更久；封顶是怕一条热点记忆永远不淡。
    """
    base = max(1e-6, float(base_half_life or HALF_LIFE_DAYS))
    try:
        n = max(0, int(access_count or 0))
    except (TypeError, ValueError):
        n = 0
    factor = 1.0 + math.log2(1.0 + n) * HALF_LIFE_ACCESS_BOOST
    return min(base * factor, base * MAX_HALF_LIFE_FACTOR)


def time_decay(created_at: Any, half_life_days: float = HALF_LIFE_DAYS,
               access_count: Any = 0) -> float:
    """30 天半衰期：越旧的记忆在检索里越轻，但不归零；常被想起的忘得慢。"""
    half = effective_half_life(half_life_days, access_count)
    age_days = max(0.0, (time.time() - _parse_time(created_at)) / 86400.0)
    return 0.5 ** (age_days / max(1e-6, half))


def clue_weight(total_entities: int, use_count: int) -> float:
    """线索的辨识度（ai-memory 的做法）。

    越少记忆共用的线索越值钱：「空调」只在两三条里出现，撞上就准；
    「日常」挂在五十条上，撞上等于没说。归一化 IDF，落在 0.1 ~ 1.0。
    """
    total_entities = max(0, int(total_entities))
    if total_entities <= 1:
        return 1.0
    use_count = max(1, min(total_entities, int(use_count or 0)))
    max_idf = math.log((total_entities + 1) / 2.0)
    if max_idf <= 1e-9:
        return 1.0
    norm = math.log((total_entities + 1) / (use_count + 1)) / max_idf
    return 0.1 + 0.9 * max(0.0, min(1.0, norm))


def retention_index(importance: float, clue_count: int, access_count: int) -> float:
    """保留指数：对数级巩固。重要性 0.5 + 两线索 + 三次访问 ≈ 3.9。"""
    importance = max(0.01, min(1.0, float(importance or 0.0)))
    clue_term = 1.0 + math.log2(1.0 + max(0, clue_count))
    access_term = 1.0 + math.log2(1.0 + max(0, access_count))
    return importance * clue_term * access_term


def flush_pressure(created_seq_now: int, last_access_seq: int, retention: float) -> float:
    """冲刷量 ÷ (保留指数 × K)。冲刷量为 0 时恒为 0。"""
    delta = max(0, int(created_seq_now) - int(last_access_seq))
    if delta == 0:
        return 0.0
    return delta / max(1e-6, retention * FLUSH_K)


def working_activation(memory: Dict[str, Any], created_seq_now: int,
                       recent_hits: int = 0) -> float:
    """一条记忆「此刻有多清楚」——工作记忆名额就按这个排名。

    四样东西相乘：有多重要、本身衰减到哪了、最近有没有被碰过、最近被想起几次。
    第三项是关键：冲刷看的是「多久没被想起」，工作记忆看的是
    「上一次被想起距今隔了几件事」。隔了十来件事还没人提它，就该让位了。
    最后一项是累计访问的补充——想起来十二次的东西，不该因为重要度差 0.05 就出局。
    """
    try:
        created_seq = int(memory.get("created_seq") or 0)
        last_access = int(memory.get("last_access_seq") or 0)
        importance = float(memory.get("importance") or 0.0)
    except (TypeError, ValueError):
        created_seq, last_access, importance = 0, 0, 0.0
    anchor = max(created_seq, last_access)  # 写下也算碰过一次
    gap = float(max(0, int(created_seq_now) - anchor))
    if WORKING_ACCESS_HORIZON > 0:
        fresh = math.exp(-gap / WORKING_ACCESS_HORIZON)
    else:
        fresh = 1.0
    base = time_decay(memory.get("created_at"), HALF_LIFE_DAYS,
                      memory.get("access_count", 0))
    recall = 1.0 + 0.2 * math.log2(1.0 + max(0, int(recent_hits or 0)))
    return (0.4 + max(0.0, min(1.0, importance))) * base * (0.25 + 0.75 * fresh) * recall


# --------------------------------------------------------------------------
# 鲜活度（vividness）· 只读判定，暂不接渲染
#
# 鲜活度降级链参考同类实现：
# 年轻的记忆说得清楚，老的只剩轮廓；但誓言、伤疤、创作里程碑不受年龄侵蚀。
# P2 第 0 步只产出函数 + 回放报告，render_brief 一行没动。
# --------------------------------------------------------------------------

# 年龄分级的两个门槛（天），默认值照参考库 memory_humanization：
# 180 天起细节开始糊，540 天起只剩轮廓。进环境变量方便回放时换参数。
VIVID_FULL_DAYS = float(os.environ.get("JJ_MEMORY_VIVID_FULL_DAYS", "180") or 180)
VIVID_FUZZY_DAYS = float(os.environ.get("JJ_MEMORY_VIVID_FUZZY_DAYS", "540") or 540)
# 置信度分级门槛（参考库 injection.py L186）
VIVID_CONF_FUZZY = 0.58
VIVID_CONF_FULL = 0.72

# 老化豁免：这几类记忆不受年龄降级（仍受置信度约束）——「誓言不该褪色」
VIVID_EXEMPT_TAGS = frozenset({"pinned", "manual", "manual_keep", "never_forget",
                               "protected", "keep"})
VIVID_EXEMPT_TYPES = frozenset({"manual_memory", "explicit_memory",
                                "relationship_claim", "promise"})
VIVID_EXEMPT_DECAY = frozenset({"no_decay", "scar_slow_decay", "creative_milestone"})


def memory_age_days(memory: Dict[str, Any],
                    now: Optional[float] = None) -> Optional[float]:
    """这条记忆在「现实时间」里放了多久（天）。

    先认 start_time（事情发生的时间），再退到 created_at（写进账本的时间）。
    两个都解析不出来就返回 None —— 宁缺勿假，调用方保持原样，不硬判年龄。
    """
    for key in ("start_time", "created_at"):
        raw = (memory or {}).get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            base = float(now or time.time())
            return max(0.0, (base - float(raw)) / 86400.0)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                ts = time.mktime(time.strptime(str(raw)[:19], fmt))
            except Exception:
                continue
            return max(0.0, ((now or time.time()) - ts) / 86400.0)
    return None


def _memory_metadata(memory: Dict[str, Any]) -> Dict[str, Any]:
    """读 metadata：dict 直接用，字符串按 JSON 解，解不出当空。"""
    raw = (memory or {}).get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _memory_tags(memory: Dict[str, Any]) -> set:
    raw = (memory or {}).get("tags") or ""
    if isinstance(raw, str):
        parts = re.split(r"[,，、;；\s]+", raw)
    else:
        parts = list(raw)
    return {str(p).strip().lower() for p in parts if str(p).strip()}


def vividness_age_exempt(memory: Dict[str, Any]) -> bool:
    """年龄不侵蚀这条记忆？高重要度 / 不失真的衰减模式 / 钉住标签 / 誓言类类型 / 伤疤。"""
    try:
        importance = float((memory or {}).get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    if importance >= 0.85:
        return True
    meta = _memory_metadata(memory)
    if str(meta.get("decay_mode") or "").strip().lower() in VIVID_EXEMPT_DECAY:
        return True
    if _memory_tags(memory) & VIVID_EXEMPT_TAGS:
        return True
    if str((memory or {}).get("memory_type") or "").strip().lower() in VIVID_EXEMPT_TYPES:
        return True
    try:
        scar = float(meta.get("scar_weight") or 0.0)
    except (TypeError, ValueError):
        scar = 0.0
    return scar >= 0.55


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
# 轻重档（tier）· 只读判定，暂不接写入漏斗
#
# 多信号交叉的做法参考同类实现：
# 情感强度 / 承诺 / 里程碑 / 冲突 四路词面信号 + strong_type，够几条才升档。
# 两条纪律照抄：
#   1. 不许用 importance 抬档（真实库 68% 挤在 0.8-0.9，抬档等于没档）；
#   2. 单信号不钉档（他们单关键词判刻骨曾冲到 18%，判错就再也改不回来）。
# --------------------------------------------------------------------------

TIER_IMPRINT = "imprint"
TIER_IMPORTANT = "important"
TIER_NORMAL = "normal"
TIER_CHOICES = (TIER_IMPRINT, TIER_IMPORTANT, TIER_NORMAL)

PROTECTED_TAGS = frozenset({"manual", "protected", "keep", "no_decay"})

# 词表按语料人工整理（教训：不收单字，避免子串误伤）。
# 情感强度分强弱两档：强词单独命中即算，弱词要凑够两个才算一次 —— 词面在、语境不在不算。
AFFECTION_STRONG_MARKERS = (
    "想你", "想念", "心疼", "舍不得", "在乎你",
)
AFFECTION_WEAK_MARKERS = (
    "抱抱", "牵手", "陪伴", "晚安", "安心", "很暖",
)
AFFECTION_MARKERS = AFFECTION_STRONG_MARKERS
# 「记住」「别忘」是记忆操作词，不是承诺 —— 一条工程笔记里写「记住这个坑」
# 不该算作对谁许过愿。m56/m494 原来就是靠这两个词堆到 3 命中的，原因错了。
COMMITMENT_MARKERS = (
    "答应你", "答应我", "我保证", "我发誓", "发誓", "承诺", "约定", "说好了",
    "不会离开", "不会丢下", "永远", "一直陪", "陪着你", "等着你", "回来找你",
    "不会忘",
)
# 归属/身份类：被起名、被认领、被公开站队。关系里的「刻骨」多数长这样。
ANCHOR_MARKERS = (
    "起了名字", "给我起名", "只有我一个", "站你这边", "站我这边", "站在我身后",
    "不会丢下你", "放弃你",
)
MILESTONE_MARKERS = (
    "第一次", "初见", "初次", "定情", "告白", "在一起", "纪念日", "生日",
)
CONFLICT_MARKERS = (
    "吵架", "吵了", "冷战", "生气", "委屈", "道歉", "对不起", "和好", "原谅",
    "误会", "哭", "难过", "受伤", "不安",
)

STRONG_MEMORY_TYPES = frozenset({
    "manual_memory", "explicit_memory", "assertion", "user_preference",
    "relationship_claim", "core_memory",
})


def marker_hits(text: str, markers: Iterable[str]) -> int:
    """数命中了几个「不同说法」。长词先匹配，命中片段抹掉，短语重叠不重复计分。"""
    if not text:
        return 0
    remaining = text
    hits = 0
    for marker in sorted((m for m in markers if m), key=len, reverse=True):
        if marker in remaining:
            hits += 1
            remaining = remaining.replace(marker, "\u0000")
    return hits


# 词面命中会在中文里撞上「假朋友」：混在一起 / 和好朋友断了来往 都含里程碑词。
# 先在文本里把这些片段抹掉，再数命中，免得工程笔记被当成定情记录。
FALSE_FRIEND_PATTERNS = (
    re.compile(r"[\u6df7\u6324\u8fde\u653e\u51d1\u7c98\u5806\u5408\u7ed1\u62e2\u8d34\u5173\u52a0\u4e32\u7f20\u7ec7]\u5728\u4e00\u8d77"),  # 混在一起 / 放在一起
    re.compile(r"\u548c\u597d(?=\u53cb|\u670b|\u4eba)"),        # 和好朋友、和朋友们
    re.compile(r"\u4e0d\u5b89(?=\u5168|\u88c5|\u6392)"),        # 不安全、不安装、不安排
)


def mask_false_friends(text: str) -> str:
    """把「看起来像信号、其实不是」的片段抹成占位符，等长替换，不动下标。"""
    if not text:
        return text
    out = text
    for pattern in FALSE_FRIEND_PATTERNS:
        out = pattern.sub(lambda m: "\u0000" * len(m.group(0)), out)
    return out


def affection_hits(text: str) -> int:
    """情感强度计分：强词按个算，弱词要两个以上才折成一次。"""
    strong = marker_hits(text, AFFECTION_STRONG_MARKERS)
    weak = marker_hits(text, AFFECTION_WEAK_MARKERS)
    return strong + (1 if weak >= 2 else 0)


def classify_tier(content: Any, memory_type: str = "", tags: Any = None,
                  metadata: Any = None) -> str:
    """这条记忆该记多久：imprint / important / normal。不看 importance。

    content 是记忆正文（我们库里是 summary），memory_type / metadata 我们库
    现在都没有这两个字段（见回放报告里的「字段缺失」一节），传空即走词面判定。
    """
    meta = metadata if isinstance(metadata, dict) else _memory_metadata(
        {"metadata": metadata} if isinstance(metadata, str) else {})
    recorded = str(meta.get("memory_tier") or "").strip().lower()
    # 只有人手动钉的档不可推翻；自动算的每次都重算，规则改严了旧误判能改回来
    if recorded in TIER_CHOICES and meta.get("memory_tier_manual"):
        return recorded

    if isinstance(tags, str):
        parts = re.split(r"[,，、;；\s]+", tags)
    else:
        parts = list(tags or [])
    tag_set = {str(t).strip().lower() for t in parts if str(t).strip()}
    if tag_set & PROTECTED_TAGS:
        return TIER_IMPRINT

    text = str(content or "").strip()[:4000]
    if not text:
        return TIER_NORMAL
    text = mask_false_friends(text)

    strong_type = str(memory_type or "").strip().lower() in STRONG_MEMORY_TYPES
    affection = affection_hits(text)
    commitment = marker_hits(text, COMMITMENT_MARKERS)
    milestone = marker_hits(text, MILESTONE_MARKERS)
    conflict = marker_hits(text, CONFLICT_MARKERS)
    anchor = marker_hits(text, ANCHOR_MARKERS)

    if affection >= 2:
        return TIER_IMPRINT
    if affection >= 1 and (commitment >= 1 or milestone >= 1):
        return TIER_IMPRINT
    if strong_type and affection >= 1 and conflict >= 1:
        return TIER_IMPRINT
    if commitment >= 3:
        return TIER_IMPRINT
    if milestone and commitment >= 2:
        return TIER_IMPRINT
    if milestone and strong_type:
        return TIER_IMPRINT
    if conflict >= 2 and strong_type:
        return TIER_IMPRINT
    # 归属类：真正的刻骨多半长这样（被起名、被公开站队、被说不会丢下）。
    # 情感词在真实语料里信号稀疏，只靠这一条路判刻骨容易永远判不出来。
    if anchor >= 2:
        return TIER_IMPRINT
    if anchor >= 1 and (commitment >= 1 or conflict >= 1):
        return TIER_IMPRINT

    # importance 不参与：这里只认内容信号与记忆类型
    if strong_type:
        return TIER_IMPORTANT
    if anchor >= 1:
        return TIER_IMPORTANT
    if commitment >= 2:
        return TIER_IMPORTANT
    if milestone >= 2:
        return TIER_IMPORTANT
    if milestone and conflict:
        return TIER_IMPORTANT
    return TIER_NORMAL


# --------------------------------------------------------------------------
# 提交构造器
# --------------------------------------------------------------------------

class CommitBuilder:
    """变更先攒着，seal 时一次性写进一个事务。"""

    def __init__(self, ledger: "Ledger", message: str, base_head: str):
        self.ledger = ledger
        self.message = (message or "").strip() or "（无说明的提交）"
        self.base_head = base_head
        self.rows: List[Dict[str, Any]] = []
        self._seq = 0
        self._gseq = ledger.max_created_seq()
        c = ledger._counters()
        self._mem_no = c["memory_no"]
        self._clue_no = c["clue_no"]
        self._profile_no = c["profile_no"]
        self._sealed = False
        self._clue_cache: Dict[str, str] = {}

    # -- 内部 --

    def _push(self, tbl: str, op: str, where: Dict[str, Any],
              before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
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

    def _find_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        for r in reversed(self.rows):
            if r["tbl"] == "memory" and r["where"].get("id") == memory_id:
                return r["after"]
        row = self.ledger._query_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        return dict(row) if row else None

    def _ensure_clue(self, content: str) -> str:
        content = (content or "").strip()
        if not content:
            raise LedgerError("线索内容不能为空")
        if content in self._clue_cache:
            return self._clue_cache[content]
        row = self.ledger._query_one("SELECT id FROM clue WHERE content = ?", (content,))
        if row:
            self._clue_cache[content] = row["id"]
            return row["id"]
        self._clue_no += 1
        cid = "c%d" % self._clue_no
        self._push("clue", "insert", {"id": cid}, None,
                   {"id": cid, "content": content, "created_at": now_str()})
        self._clue_cache[content] = cid
        return cid

    @staticmethod
    def _split_tags(tags: Any) -> List[str]:
        if not tags:
            return []
        if isinstance(tags, str):
            parts = re.split(r"[,，、;；\s]+", tags)
        else:
            parts = list(tags)
        out, seen = [], set()
        for p in parts:
            p = str(p).strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # -- 对外 --

    def add_memory(self, summary: str, level: str = "day", tags: Any = "",
                   importance: float = 0.5, grade: int = 1,
                   scope_key: str = "", start_time: str = "", end_time: str = "",
                   auto_link: bool = True) -> str:
        summary = (summary or "").strip()
        if not summary:
            raise LedgerError("记忆内容不能为空")
        if len(summary) > 4000:
            raise LedgerError("单条记忆超过 4000 字，先拆开再提交")
        level = level if level in LEVELS else "day"
        grade = max(0, min(MAX_GRADE, int(grade)))
        tag_list = self._split_tags(tags)

        self._gseq += 1
        self._mem_no += 1
        mid = "m%d" % self._mem_no
        ts = now_str()
        row = {
            "id": mid, "level": level, "grade": grade,
            "importance": round(max(0.0, min(1.0, float(importance))), 4),
            "summary": summary, "tags": ",".join(tag_list),
            "scope_key": (scope_key or "").strip(),
            "start_time": start_time or ts, "end_time": end_time or ts,
            "created_seq": self._gseq, "last_access_seq": self._gseq,
            "access_count": 0, "created_at": ts, "updated_at": ts,
        }
        self._push("memory", "insert", {"id": mid}, None, row)

        for tag in tag_list:
            cid = self._ensure_clue(tag)
            self._push("memory_clue", "insert",
                       {"memory_id": mid, "clue_id": cid}, None,
                       {"memory_id": mid, "clue_id": cid, "weight": 1.0, "note": ""})
        if auto_link and tag_list:
            self._auto_link(mid, tag_list)
        return mid

    def _auto_link(self, mid: str, tag_list: List[str]) -> None:
        """标签就是概念：和历史上共用同一概念的节点自动织网。"""
        for tag in tag_list:
            hits = self.ledger._query_all(
                "SELECT id FROM memory WHERE id != ? AND tags LIKE ? LIMIT 8",
                (mid, "%" + tag + "%"))
            for h in hits:
                nid = h["id"]
                if self.ledger._query_one(
                        "SELECT 1 FROM memory_link WHERE (from_id=? AND to_id=?)"
                        " OR (from_id=? AND to_id=?)", (mid, nid, nid, mid)):
                    continue
                self._push("memory_link", "insert",
                           {"from_id": mid, "to_id": nid, "concept": tag}, None,
                           {"from_id": mid, "to_id": nid, "concept": tag,
                            "strength": 1.0, "created_at": now_str()})

    # touch 已经搬到 Ledger.touch_many：记住「被想起过」不是一次记忆编辑，
    # 不该在提交链上留页。这里故意不留兼容壳，免得以后有人又顺着它写回提交。

    def set_grade(self, memory_id: str, grade: int) -> bool:
        snap = self._find_memory(memory_id)
        if snap is None:
            return False
        grade = max(0, min(MAX_GRADE, int(grade)))
        if grade == int(snap["grade"]):
            return False
        after = dict(snap)
        after["grade"] = grade
        after["updated_at"] = now_str()
        self._push("memory", "update", {"id": memory_id}, {"grade": snap["grade"]}, after)
        return True

    def forget(self, memory_id: str) -> bool:
        snap = self._find_memory(memory_id)
        if snap is None:
            return False
        for rel in self.ledger._query_all(
                "SELECT * FROM memory_clue WHERE memory_id = ?", (memory_id,)):
            self._push("memory_clue", "delete",
                       {"memory_id": memory_id, "clue_id": rel["clue_id"]},
                       {"weight": rel["weight"], "note": rel["note"]}, None)
        for link in self.ledger._query_all(
                "SELECT * FROM memory_link WHERE from_id = ? OR to_id = ?",
                (memory_id, memory_id)):
            self._push("memory_link", "delete", {"id": link["id"]},
                       {"concept": link["concept"], "strength": link["strength"]}, None)
        self._push("memory", "delete", {"id": memory_id}, snap, None)
        return True

    def upsert_profile(self, user_id: str, fact: str,
                       category: str = "event", weight: int = 1) -> str:
        fact = (fact or "").strip()
        if not fact:
            raise LedgerError("画像事实不能为空")
        row = self.ledger._query_one(
            "SELECT * FROM profile WHERE user_id = ? AND fact = ?", (user_id, fact))
        ts = now_str()
        if row:
            after = dict(row)
            after["weight"] = int(row["weight"]) + int(weight)
            after["updated_at"] = ts
            self._push("profile", "update", {"id": row["id"]},
                       {"weight": row["weight"]}, after)
            return row["id"]
        self._profile_no += 1
        pid = "p%d" % self._profile_no
        self._push("profile", "insert", {"id": pid}, None,
                   {"id": pid, "user_id": user_id, "fact": fact, "category": category,
                    "weight": int(weight), "updated_at": ts})
        return pid

    def seal(self) -> str:
        if self._sealed:
            raise LedgerError("这个提交已经封过了")
        if not self.rows:
            raise LedgerError("空提交：没有任何行变更，不写盘")
        return self.ledger._seal(self)

    def __enter__(self) -> "CommitBuilder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.seal()
        return False


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------

class Ledger:
    ASSOC_DEPTH = 2  # 联想最多走两步：想到的事，还想得起它连着的事

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     isolation_level=None, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 唤起冷却：刚浮现过的记忆，短时间内不再重复浮现
        self._surfaced: Dict[str, float] = {}
        # 自动压缩阈值（可被外壳/测试覆盖）
        self.auto_compact_after = AUTO_COMPACT_AFTER
        self.auto_compact_keep = AUTO_COMPACT_KEEP
        # 工作记忆容量（可被外壳/测试覆盖；0 = 关掉分级运转）
        self.working_capacity = WORKING_CAPACITY
        self._rebalanced_at = 0.0
        self._init_schema()

    # -- 基础设施 --

    def _init_schema(self) -> None:
        with self._tx() as cx:
            for stmt in SCHEMA_STATEMENTS:
                cx.execute(stmt)
            for k, v in DEFAULT_META.items():
                cx.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))
        # 建表之后马上对版本：老库没有这个键，会被当成 0 号往上迁
        self._migrate()

    def schema_version(self) -> int:
        """库里的结构版本。老库没有这个键时返回 0，不假装它是最新的。"""
        row = self._query_one("SELECT v FROM meta WHERE k = 'schema_version'")
        try:
            return int(row["v"]) if row else 0
        except (TypeError, ValueError):
            return 0

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


    @contextmanager
    def _tx(self):
        with self._lock:
            cx = self._conn
            cx.execute("BEGIN IMMEDIATE")
            try:
                yield cx
            except Exception:
                cx.execute("ROLLBACK")
                raise
            else:
                cx.execute("COMMIT")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _meta(self, cx: sqlite3.Connection, key: str) -> str:
        row = cx.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else ""

    def _set_meta(self, cx: sqlite3.Connection, key: str, value: str) -> None:
        cx.execute("INSERT INTO meta(k, v) VALUES(?, ?)"
                   " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(value)))

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def _counters(self) -> Dict[str, int]:
        data = {r["k"]: r["v"] for r in self._conn.execute("SELECT * FROM meta").fetchall()}
        return {k: int(data.get(k) or 0) for k in
                ("commit_no", "memory_no", "clue_no", "profile_no", "created_seq")}

    # -- 读 --

    def head(self) -> str:
        row = self._query_one("SELECT v FROM meta WHERE k = 'head'")
        return row["v"] if row and row["v"] else ""

    def max_created_seq(self) -> int:
        row = self._query_one("SELECT v FROM meta WHERE k = 'created_seq'")
        return int(row["v"]) if row else 0

    # -- 写 --

    def touch(self, memory_id: str, query: str = "", how: str = "manual") -> bool:
        """一条记忆被想起了一次。"""
        return self.touch_many([memory_id], query=query, how=how) > 0

    def touch_many(self, memory_ids: Iterable[str], query: str = "",
                   how: str = "manual") -> int:
        """把「被想起」记进 access_log，顺手把缓存计数 +1。

        这条路径不碰提交链：想起一件事，不等于改写一件事。
        以前每浮现一次就写一个提交，结果十九条提交里十四条只是把计数加一，
        真正的记忆改动反而被埋了。

        activate / fallback 是机器自己把记忆捞出来念给模型听，不是「被想起」：
        只写 access_log 留一条观测记录，不更新 access_count / last_access_seq。
        否则每轮自动浮现都会把 last_access_seq 拨到当下，等于给记忆续上
        「最近刚被想起」的标记，工作记忆换班会被悄悄干扰（长 prompt 那轮
        query 被置空、走 activate 路线时，工作记忆可以永不换班）。真检索路径
        search / associate / manual / recall 保持原样，照常更新缓存计数。
        """
        ids = [m for m in dict.fromkeys(memory_ids or []) if m]
        if not ids:
            return 0
        # 自动浮现 = 观测，不推动「最近被想起」的时序
        auto_surface = (how or "") in ("activate", "fallback")
        try:
            with self._tx() as cx:
                seq = int(self._meta(cx, "created_seq") or 0)
                at = now_str()
                hit = 0
                for mid in ids:
                    if not cx.execute("SELECT 1 FROM memory WHERE id = ?",
                                      (mid,)).fetchone():
                        continue
                    cx.execute(
                        "INSERT INTO access_log(memory_id, at, seq, query, how)"
                        " VALUES(?,?,?,?,?)",
                        (mid, at, seq, (query or "")[:80], (how or "")[:40]))
                    if not auto_surface:
                        cx.execute(
                            "UPDATE memory SET access_count = access_count + 1,"
                            " last_access_seq = ? WHERE id = ?", (seq, mid))
                    hit += 1
                return hit
        except sqlite3.Error as e:
            log.warning("[记忆账本] 记录访问痕迹失败（不影响聊天）: %r", e)
            return 0

    def access_history(self, memory_id: str = "", limit: int = 20
                       ) -> List[Dict[str, Any]]:
        """一条记忆被想起过几次、每次因为什么。"""
        limit = max(1, int(limit))
        if memory_id:
            rows = self._query_all(
                "SELECT * FROM access_log WHERE memory_id = ?"
                " ORDER BY id DESC LIMIT ?", (memory_id, limit))
        else:
            rows = self._query_all("SELECT * FROM access_log ORDER BY id DESC LIMIT ?",
                                   (limit,))
        return [dict(r) for r in rows]

    def prune_access_log(self, keep_days: int = 180, keep_per_memory: int = 200
                         ) -> int:
        """访问日志只留最近一段：它是体温计，不是账本，不需要永久保存。

        每条记忆至少留最近 keep_per_memory 次，方便看「最近怎么被想起来的」；
        更老的只在 keep_days 以内保留。cache 计数已经落到 memory 表，删日志不丢强度。
        """
        keep_days = max(1, int(keep_days))
        keep_per_memory = max(1, int(keep_per_memory))
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - keep_days * 86400))
        try:
            with self._tx() as cx:
                before = cx.execute("SELECT COUNT(*) AS n FROM access_log").fetchone()["n"]
                cx.execute(
                    "DELETE FROM access_log WHERE at < ? AND id NOT IN ("
                    " SELECT id FROM ("
                    "  SELECT id, ROW_NUMBER() OVER (PARTITION BY memory_id"
                    "   ORDER BY id DESC) AS rn FROM access_log) WHERE rn <= ?)",
                    (cutoff, keep_per_memory))
                cx.execute("DELETE FROM access_log WHERE id NOT IN ("
                           " SELECT id FROM ("
                           "  SELECT id, ROW_NUMBER() OVER (PARTITION BY memory_id"
                           "   ORDER BY id DESC) AS rn FROM access_log) WHERE rn <= ?)",
                           (keep_per_memory,))
                after = cx.execute("SELECT COUNT(*) AS n FROM access_log").fetchone()["n"]
                return int(before) - int(after)
        except sqlite3.Error as e:
            log.warning("[记忆账本] 修剪访问日志失败: %r", e)
            return 0

    def commit(self, message: str) -> CommitBuilder:
        return CommitBuilder(self, message, self.head())

    def _seal(self, builder: CommitBuilder) -> str:
        with self._tx() as cx:
            head = self._meta(cx, "head")
            if head != builder.base_head:
                raise StaleHeadError(
                    "HEAD 已经变了：开提交时是 %r，现在是 %r。重新开一个提交。"
                    % (builder.base_head, head))
            no = int(self._meta(cx, "commit_no") or 0) + 1
            cid = "C%d" % no
            cx.execute("INSERT INTO mcommit(id, parent, message, created_at) VALUES(?,?,?,?)",
                       (cid, head or None, builder.message, now_str()))
            for r in builder.rows:
                cx.execute(
                    "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
                    " before_json, after_json) VALUES(?,?,?,?,?,?,?)",
                    (cid, r["seq"], r["tbl"], r["op"], r["row_key"],
                     json.dumps(r["before"], ensure_ascii=False)
                     if r["before"] is not None else None,
                     json.dumps(r["after"], ensure_ascii=False)
                     if r["after"] is not None else None))
                self._apply_row(cx, r)
            self._set_meta(cx, "commit_no", no)
            self._set_meta(cx, "memory_no", builder._mem_no)
            self._set_meta(cx, "clue_no", builder._clue_no)
            self._set_meta(cx, "profile_no", builder._profile_no)
            self._set_meta(cx, "created_seq", builder._gseq)
            self._set_meta(cx, "head", cid)
            builder._sealed = True
        self._maybe_auto_compact()
        return cid

    def _maybe_auto_compact(self) -> Optional[Dict[str, Any]]:
        """提交数超过阈值就顺手压一次。

        压缩不生成新提交、不动 HEAD，所以写在事务外面做，
        失败只记警告，绝不能让已经成功的写入跟着报错。
        """
        after = int(getattr(self, "auto_compact_after", 0) or 0)
        if after <= 0:
            return None
        try:
            n = self._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"]
            if n <= after:
                return None
            res = self.compact(keep_recent=int(getattr(self, "auto_compact_keep", 10) or 10))
            if res.get("compacted"):
                log.info("[记忆账本] 自动压缩：%s 个提交并成基线，现存 %s 个",
                         res.get("merged"), res.get("commits"))
            return res
        except Exception as e:  # 顺手做的事，失败不连累写入
            log.warning("[记忆账本] 自动压缩失败（写入不受影响）: %r", e)
            return None

    @staticmethod
    def _apply_row(cx: sqlite3.Connection, r: Dict[str, Any]) -> None:
        tbl, op, where = r["tbl"], r["op"], r["where"]
        where_sql = " AND ".join("%s = ?" % k for k in where)
        where_vals = list(where.values())
        if op == "insert":
            snap = dict(r["after"])
            if tbl == "memory":
                # 老库的 last_access_seq 是 NOT NULL 且没有默认值，
                # 而新写的插入快照里不含访问痕迹，回放到这里必须自己补上。
                # 关键是不能补 0：刚写完的记忆访问序号该等于它的创建序号，
                # 补 0 会让它一出生就显得「很久没被翻过」，下一轮直接被冲刷。
                snap.setdefault("last_access_seq", snap.get("created_seq") or 0)
                snap.setdefault("access_count", 0)
            cols = list(snap.keys())
            cx.execute("INSERT INTO %s (%s) VALUES (%s)" % (
                tbl, ",".join(cols), ",".join("?" * len(cols))),
                [snap[c] for c in cols])
        elif op == "update":
            cols = list(r["after"].keys())
            cx.execute("UPDATE %s SET %s WHERE %s" % (
                tbl, ",".join("%s = ?" % c for c in cols), where_sql),
                [r["after"][c] for c in cols] + where_vals)
        elif op == "delete":
            cx.execute("DELETE FROM %s WHERE %s" % (tbl, where_sql), where_vals)
        else:
            raise LedgerError("未知操作：%r" % (op,))

    # -- 便利方法 --

    def remember(self, summary: str, level: str = "day", tags: Any = "",
                 importance: float = 0.5, grade: int = 1, scope_key: str = "",
                 start_time: str = "", end_time: str = "") -> Dict[str, Any]:
        msg = "记下：" + summary.strip()[:24]
        with self.commit(msg) as c:
            mid = c.add_memory(summary, level=level, tags=tags, importance=importance,
                               grade=grade, scope_key=scope_key,
                               start_time=start_time, end_time=end_time)
        return {"commit": self.head(), "memory_id": mid}

    def recall(self, query: str = "", limit: int = 5, scope_key: str = "",
               touch: bool = True, expand: bool = True,
               half_life_days: float = HALF_LIFE_DAYS,
               min_score: float = 0.15) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory"
        params: Tuple[Any, ...] = ()
        if scope_key:
            sql += " WHERE scope_key = ? OR scope_key = ''"
            params = (scope_key,)
        rows = [dict(r) for r in self._query_all(sql, params)]
        if not rows:
            return []

        limit = normalize_limit(limit)
        docs = [tokenize(r["summary"] + " " + r["tags"]) for r in rows]
        q_tokens = tokenize(query) if query else []
        base = _bm25_scores(docs, q_tokens) if q_tokens else [0.0] * len(rows)
        # 写入端只切双字，「辣」这种单字在 BM25 里必然空手，回退成子串命中
        if q_tokens:
            singles = {t for t in q_tokens if len(t) == 1}
            if singles:
                for i, r in enumerate(rows):
                    hay = (r["summary"] or "") + " " + (r["tags"] or "")
                    hit = sum(1 for t in singles if t in hay)
                    if hit:
                        base[i] = max(base[i], 0.9 * hit)
        clue_hits = self._clue_hits(q_tokens) if q_tokens else {}

        scored: Dict[str, Dict[str, Any]] = {}
        for r, s in zip(rows, base):
            access = r.get("access_count", 0)
            decay = time_decay(r["created_at"], half_life_days, access)
            score = s * decay if q_tokens else 0.0
            # 无查询时按新鲜度 + 巩固等级排
            score += time_decay(r["created_at"], half_life_days, access) * 0.05 * (1 + int(r["grade"]))
            score += float(r["importance"]) * 0.02
            r["bm25"] = round(s, 4)
            r["decay"] = round(decay, 4)
            r["match"] = "direct" if s > 0 else "fresh"
            r["clue_boost"] = 0.0
            # 线索权重进主排序：稀有线索把这条往上抬，烂大街的线索几乎不动
            hit = clue_hits.get(r["id"])
            if hit:
                r["clue_boost"] = round(hit["boost"], 3)
                if s > 0:
                    # 只放大词面命中那部分，不把新鲜度、重要性一起乘进去
                    score += s * decay * CLUE_SORT_BOOST * hit["boost"]
                    r["match"] = "direct+clue"
                else:
                    score = max(score, CLUE_ONLY_BASE * hit["boost"] * decay)
                    r["match"] = "clue:" + hit["clue"]
            r["score"] = round(score, 5)
            scored[r["id"]] = r

        # 顺藤摸瓜：命中的节点沿图谱边带一跳邻居
        if q_tokens and expand and scored:
            seeds = sorted(scored.values(), key=lambda x: -x["score"])[:max(1, limit)]
            for seed in seeds:
                if seed["bm25"] <= 0:
                    continue
                for link in self._query_all(
                        "SELECT * FROM memory_link WHERE from_id = ? OR to_id = ?",
                        (seed["id"], seed["id"])):
                    nid = link["to_id"] if link["from_id"] == seed["id"] else link["from_id"]
                    if nid in scored and scored[nid]["bm25"] > 0:
                        continue
                    nrow = self._query_one("SELECT * FROM memory WHERE id = ?", (nid,))
                    if not nrow:
                        continue
                    nrow = dict(nrow)
                    cand = seed["score"] * 0.35 * float(link["strength"]) * \
                        time_decay(nrow["created_at"], half_life_days,
                                   nrow.get("access_count", 0))
                    if nid in scored:
                        scored[nid]["score"] = round(max(scored[nid]["score"], cand), 5)
                        scored[nid]["match"] = "graph"
                    else:
                        nrow.update({"bm25": 0.0,
                                     "decay": round(time_decay(nrow["created_at"], HALF_LIFE_DAYS,
                                                               nrow.get("access_count", 0)), 4),
                                     "match": "graph:" + link["concept"],
                                     "score": round(cand, 5)})
                        scored[nid] = nrow

        ranked = sorted(scored.values(), key=lambda x: -x["score"])
        if q_tokens:
            ranked = [r for r in ranked if r["score"] >= min_score]
        picked = ranked[:max(1, limit)]

        clue_map: Dict[str, List[str]] = {}
        for rel in self._query_all("SELECT mc.memory_id, c.content FROM memory_clue mc"
                                   " JOIN clue c ON c.id = mc.clue_id"):
            clue_map.setdefault(rel["memory_id"], []).append(rel["content"])
        for r in picked:
            r["clues"] = clue_map.get(r["id"], [])

        if touch and picked:
            self.touch_many([r["id"] for r in picked], query=query, how="recall")
        return picked

    # -- 唤起：不是「能查到」，是「该想起来」 --

    def _clue_hits(self, q_tokens: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """查询词撞上哪些线索，这些线索又挂着哪条记忆。

        返回 memory_id -> {clue, boost, strength, weight}，一条记忆只留最强的那条线索。
        boost = 命中强度 × 线索辨识度，稀有线索接近 1，高频线索压到 0.1 附近。
        """
        qt = set(q_tokens)
        if not qt:
            return {}
        total = int((self._query_one("SELECT COUNT(*) AS n FROM memory") or {"n": 0})["n"] or 0)
        if total <= 0:
            return {}

        wanted: Dict[str, Tuple[str, float]] = {}
        for row in self._query_all(
                "SELECT c.id AS clue_id, c.content AS clue, COUNT(mc.memory_id) AS uses"
                " FROM clue c LEFT JOIN memory_clue mc ON mc.clue_id = c.id"
                " GROUP BY c.id"):
            c_tokens = set(tokenize(row["clue"]))
            common = qt & c_tokens
            if not common:
                continue
            strength = len(common) / float(max(1, len(qt)))
            weight = clue_weight(total, row["uses"] or 0)
            wanted[row["clue_id"]] = (row["clue"], strength * weight)

        if not wanted:
            return {}

        best: Dict[str, Dict[str, Any]] = {}
        for rel in self._query_all("SELECT clue_id, memory_id FROM memory_clue"):
            hit = wanted.get(rel["clue_id"])
            if not hit:
                continue
            mid = rel["memory_id"]
            cur = best.get(mid)
            if cur is None or hit[1] > cur["boost"]:
                best[mid] = {"clue": hit[0], "boost": hit[1]}
        return best

    def _fill_clues(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        want = {r["id"] for r in rows}
        clue_map: Dict[str, List[str]] = {}
        for rel in self._query_all("SELECT mc.memory_id, c.content FROM memory_clue mc"
                                   " JOIN clue c ON c.id = mc.clue_id"):
            if rel["memory_id"] in want:
                clue_map.setdefault(rel["memory_id"], []).append(rel["content"])
        for r in rows:
            r["clues"] = clue_map.get(r["id"], [])

    def _clue_bridge(self, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        """一个词先撞上「线索」，线索再把挂着它的记忆牵出来。

        ai-memory 里联想的主路走的是线索，不是记忆连记忆：
        查询 → 线索 → 记忆。稀有线索权重高，所以「空调」能牵出具体的事，
        而「日常」这种满地都是的线索牵不动什么。
        """
        q_tokens = set(tokenize(query))
        if not q_tokens:
            return []
        total = int((self._query_one("SELECT COUNT(*) AS n FROM memory") or {"n": 0})["n"] or 0)
        rows = self._query_all(
            "SELECT c.id AS clue_id, c.content AS clue, COUNT(mc.memory_id) AS uses"
            " FROM clue c LEFT JOIN memory_clue mc ON mc.clue_id = c.id"
            " GROUP BY c.id")
        scored: List[Tuple[str, float, Any]] = []
        for row in rows:
            c_tokens = set(tokenize(row["clue"]))
            if not c_tokens:
                continue
            common = q_tokens & c_tokens
            if not common:
                continue
            strength = len(common) / float(max(1, len(q_tokens)))
            weight = clue_weight(total, row["uses"] or 0)
            scored.append((row["clue"], strength * weight, row))
        scored.sort(key=lambda x: -x[1])

        out: List[Dict[str, Any]] = []
        link_map: Dict[str, List[str]] = {}
        for rel in self._query_all("SELECT clue_id, memory_id FROM memory_clue"):
            link_map.setdefault(rel["clue_id"], []).append(rel["memory_id"])
        seen = set()
        for clue, strength, row in scored[:max(1, limit)]:
            for mid in link_map.get(row["clue_id"], []):
                if mid in seen:
                    continue
                mem = self._query_one("SELECT * FROM memory WHERE id = ?", (mid,))
                if not mem:
                    continue
                seen.add(mid)
                cand = dict(mem)
                cand["decay"] = round(time_decay(cand["created_at"], HALF_LIFE_DAYS,
                                            cand.get("access_count", 0)), 4)
                cand["score"] = round(strength * float(cand["decay"]), 5)
                cand["bm25"] = 0.0
                cand["match"] = "clue:" + clue
                cand["how"] = "提到「%s」想起来的" % clue[:12]
                cand["bridge_clue"] = clue
                out.append(cand)
        out.sort(key=lambda x: -x["score"])
        return out

    def _fresh_candidates(self, limit: int = 12,
                          scope_key: str = "") -> List[Dict[str, Any]]:
        """没人问起时，哪些记忆有资格自己冒出来：新鲜 × 重要 × 巩固。"""
        sql = "SELECT * FROM memory"
        params: Tuple[Any, ...] = ()
        if scope_key:
            sql += " WHERE scope_key = ? OR scope_key = ''"
            params = (scope_key,)
        out: List[Dict[str, Any]] = []
        for row in self._query_all(sql, params):
            r = dict(row)
            decay = time_decay(r["created_at"], HALF_LIFE_DAYS, r.get("access_count", 0))
            score = decay * (0.4 + float(r["importance"])) * (1.0 + 0.2 * int(r["grade"]))
            # 被挤出工作记忆的不主动冒头：还能被问出来，但不该抢话
            if int(r["grade"]) <= 0:
                score *= ZERO_GRADE_WEIGHT
                r["how"] = human_ago(r["created_at"]) + "记下的（沉下去了）"
            else:
                r["how"] = human_ago(r["created_at"]) + "记下的"
            r.update({"bm25": 0.0, "decay": round(decay, 4), "match": "fresh",
                      "score": round(score, 5), "clues": []})
            out.append(r)
        out.sort(key=lambda x: -x["score"])
        return out[:max(1, limit)]

    def activate(self, query: str = "", slots: int = DEFAULT_INJECT_SLOTS,
                 scope_key: str = "",
                 user_id: str = "master", cooldown_min: float = 25.0,
                 with_profile: bool = True,
                 dry_run: bool = False) -> Dict[str, Any]:
        """聊天前的一次「唤起」。

        三条路依次补位：
          1. 检索：query 真的命中了什么
          2. 联想：沿标签连边一跳，把沾边的带出来
          3. 激活：什么都没命中时，让新鲜且重要的自己冒出来
        冷却期内刚浮现过的记忆不再重复出现，免得每轮都念同一件事。

        slots 是「这一轮最多说出口几条」，只影响注入，不影响谁留在工作记忆里。
        """
        query = (query or "").strip()
        slots = normalize_limit(slots, DEFAULT_INJECT_SLOTS)
        cooldown = max(0.0, float(cooldown_min)) * 60.0
        now = time.time()
        picked: List[Dict[str, Any]] = []
        seen = set()
        routes: List[str] = []

        def take(rows: Iterable[Dict[str, Any]], route: str,
                 cap: Optional[int] = None, force: bool = False) -> bool:
            added = 0
            cap = slots if cap is None else cap
            for row in rows:
                if len(picked) >= cap:
                    break
                mid = row["id"]
                if mid in seen or (not force
                                   and now - self._surfaced.get(mid, 0.0) < cooldown):
                    continue
                r = dict(row)
                r.setdefault("clues", [])
                r.setdefault("how", human_ago(r.get("created_at")))
                r["_route"] = route
                seen.add(mid)
                picked.append(r)
                added += 1
            if added and route not in routes:
                routes.append(route)
            return added > 0

        if query:
            hits = [h for h in self.recall(query, limit=max(slots * 3, 6),
                                           scope_key=scope_key, touch=False)
                    if h.get("bm25", 0) > 0 or str(h.get("match", "")).startswith("clue:")]
            take(hits, "search")

        # 线索桥：一个词先撞上线索，再顺着线索牵出记忆
        flashes: List[str] = []
        if query and len(picked) < slots:
            for cand in self._clue_bridge(query, slots * 3):
                if len(picked) >= slots:
                    break
                before = len(picked)
                take([cand], "associate")
                clue = cand.get("bridge_clue")
                if len(picked) > before and clue and clue not in flashes:
                    flashes.append(clue)

        # 联想：从想起的东西出发，沿标签连边一跳一跳地往外走
        if len(picked) < slots:
            frontier = [(r["id"], 1.0) for r in picked]
            walked = {r["id"] for r in picked}
            for _ in range(self.ASSOC_DEPTH):
                if not frontier or len(picked) >= slots:
                    break
                nxt = []
                for sid, power in frontier:
                    for link in self._query_all(
                            "SELECT * FROM memory_link WHERE from_id = ? OR to_id = ?",
                            (sid, sid)):
                        if len(picked) >= slots:
                            break
                        nid = link["to_id"] if link["from_id"] == sid else link["from_id"]
                        if nid in walked:
                            continue
                        row = self._query_one("SELECT * FROM memory WHERE id = ?", (nid,))
                        if not row:
                            continue
                        walked.add(nid)
                        cand = dict(row)
                        cand["match"] = "graph:" + link["concept"]
                        cand["bm25"] = 0.0
                        cand["decay"] = round(time_decay(cand["created_at"], HALF_LIFE_DAYS,
                                            cand.get("access_count", 0)), 4)
                        cand["score"] = round(power * 0.35 * float(link["strength"]) *
                                              cand["decay"], 5)
                        cand["how"] = "顺着「%s」想到的" % link["concept"][:10]
                        before = len(picked)
                        take([cand], "associate")
                        if len(picked) > before:
                            # 只有真的被想起的，才值得再往外走一步
                            nxt.append((nid, power * 0.5))
                frontier = nxt

        # 激活 / 兜底：只有没人问起时才让记忆自己浮上来（无 query 的正常 fresh 路线）。
        # 但「有 query 却一条都没命中」不能再整轮空转：那只会让注入账记满 no_hit。
        # 所以有检索词零命中时，降级到 fresh 兜底 —— 但数量更保守（最多 slots//2 条，
        # 不会把 420 预算塞满），route 单独标 fallback，让注入日志能区分「正常激活」
        # 和「检索落空后的兜底」，绝不伪装成 search/activate。冷却照常生效。
        if not query and len(picked) < slots:
            take(self._fresh_candidates(slots * 4, scope_key), "activate")
        elif query and not picked:
            # 进兜底分支就无条件记一条 route：让注入日志分得清「没走兜底」和
            # 「兜底被冷却挡光」，别再拿 route=none 顶替、搞得 17 行 no_hit
            # 全是 route=none 看不出毛病。
            routes.append("fallback")
            # 兜底候选池给足。老实现池子只有 slots//2=1 条，真库副本里 fresh
            # 第一名 m6 刚进冷却就直接 route=none、items=[]，而 m2/m1/m5/m7
            # 明明还能用。池子给大（slots*3，至少 6），让不在冷却的候选能进来；
            # 但真正注入的仍只取 slots//2 条，保住 420 预算不被塞满。
            candidates = self._fresh_candidates(max(slots * 3, 6), scope_key)
            took = take(candidates, "fallback", cap=max(1, slots // 2))
            if not took and len(candidates) >= 2:
                # 候选全在冷却里（还不止一条）：允许取「最久没露面」的 1 条
                # 破例浮现，仍标 fallback，避免检索落空后整轮空转。
                last = min(candidates, key=lambda r: self._surfaced.get(r["id"], 0.0))
                took = take([last], "fallback", cap=1, force=True)
            if not took:
                # 兜底真被挡光（单条候选且它在冷却 / 候选池是空的）：单独标
                # fallback_blocked，和「根本没进兜底」区分开。
                routes[:] = ["fallback_blocked"]

        # 按实际来源记录「被想起」：别再一刀切 how=activate。
        # _recent_hits 只认 search/associate/manual/recall，fallback 和 activate
        # 不算「被想起」——否则零级记忆自己浮两次就满足复出条件（假活跃）。
        if picked:
            by_how: Dict[str, List[str]] = {}
            for r in picked:
                rt = r.pop("_route", "activate")
                by_how.setdefault(rt, []).append(r["id"])
            if not dry_run:
                for rt, ids in by_how.items():
                    self.touch_many(ids, query=query, how=rt)
                for r in picked:
                    self._surfaced[r["id"]] = now

        self._fill_clues(picked)
        if not flashes:
            seen_clue = set()
            for r in picked:
                for clue in r.get("clues", []):
                    if clue in seen_clue:
                        continue
                    seen_clue.add(clue)
                    flashes.append(clue)
                    if len(flashes) >= 4:
                        break
                if len(flashes) >= 4:
                    break
        return {"items": picked,
                "profiles": self.profiles(user_id, limit=3) if with_profile else [],
                "flashes": flashes,
                "route": routes[0] if routes else "none",
                "query": query}

    @staticmethod
    def render_brief(result: Dict[str, Any], max_chars: int = 420,
                     max_profiles: int = 2, max_item: int = 80,
                     stats: Optional[Dict[str, Any]] = None) -> str:
        """把唤起结果压成一小段能塞进系统提示的话。宁可少说。

        stats 只是观测：传一个空 dict 进来，出来时填上这次渲染的预算账
        （候选几条、真渲染了几条、因为预算丢了几条、最后多长）。
        只记录，不改变任何渲染行为。
        """
        items = (result or {}).get("items") or []
        profiles = (result or {}).get("profiles") or []
        if stats is not None:
            stats.update({"candidates": len(items), "profiles_candidates": len(profiles),
                          "budget": max(80, int(max_chars)), "rendered": 0,
                          "skipped_budget": 0, "profiles_rendered": 0,
                          "flashes_rendered": 0, "chars": 0, "over_budget": False})
        if not items and not profiles:
            return ""
        budget = max(80, int(max_chars))
        head = "[记忆账本·此刻想起来的]"
        footer = "只是背景。自然相关时带一句就好，别罗列，别显得在翻档案。"
        # 边框先扣：标题和收尾句是一定会写出去的，不先扣掉，
        # 它们就会躲在预算之外，让「没超」变成一句假话。
        frame = len(head) + 1 + len(footer) + 1
        inner = max(40, budget - frame)
        if stats is not None:
            stats["frame"] = frame
            stats["inner_budget"] = inner

        def clip(text: str, n: int) -> str:
            text = " ".join((text or "").split())
            return text if len(text) <= n else text[:n] + "…"

        # 画像先算、先占额度。原来它排在最后，条目一多就永远轮不到它。
        # 上限压在正文的一半，免得画像反过来把记忆挤没。
        profile_lines: List[str] = []
        used_profiles = 0
        for p in profiles[:max(1, max_profiles)]:
            line = "用户的事：%s" % clip(p.get("fact"), max_item)
            if used_profiles + len(line) + 1 > inner // 2:
                break
            profile_lines.append(line)
            used_profiles += len(line) + 1
        if stats is not None:
            stats["reserved_for_profiles"] = used_profiles

        lines: List[str] = []
        used = 0
        body_budget = inner - used_profiles
        for r in items:
            line = "- %s：%s" % ((r.get("how") or human_ago(r.get("created_at"))),
                                 clip(r.get("summary"), max_item))
            if used + len(line) > body_budget:
                if stats is not None:
                    stats["skipped_budget"] = len(items) - stats["rendered"]
                break
            lines.append(line)
            used += len(line) + 1
            if stats is not None:
                stats["rendered"] += 1
        flashes = (result or {}).get("flashes") or []
        if flashes:
            line = "顺着这些词想到的：%s" % "、".join(flashes[:4])
            if used + len(line) <= body_budget:
                lines.append(line)
                used += len(line) + 1
                if stats is not None:
                    stats["flashes_rendered"] = 1
        lines.extend(profile_lines)
        if stats is not None:
            stats["profiles_rendered"] = len(profile_lines)
        if not lines:
            return ""
        out = head + "\n" + "\n".join(lines) + "\n" + footer
        if stats is not None:
            stats["chars"] = len(out)
            stats["over_budget"] = len(out) > budget
        return out

    def forget(self, memory_id: str, reason: str = "") -> Dict[str, str]:
        with self.commit("遗忘：%s" % ((reason or memory_id).strip()[:24])) as c:
            ok = c.forget(memory_id)
        if not ok:
            raise LedgerError("没有这条记忆：%s" % memory_id)
        return {"commit": self.head(), "memory_id": memory_id}

    def remember_profile(self, user_id: str, fact: str,
                         category: str = "event", weight: int = 1) -> Dict[str, str]:
        with self.commit("画像：%s" % fact.strip()[:20]) as c:
            pid = c.upsert_profile(user_id, fact, category=category, weight=weight)
        return {"commit": self.head(), "profile_id": pid}

    def profiles(self, user_id: str = "", limit: int = 30) -> List[Dict[str, Any]]:
        if user_id:
            rows = self._query_all(
                "SELECT * FROM profile WHERE user_id = ? ORDER BY weight DESC, updated_at DESC"
                " LIMIT ?", (user_id, max(1, limit)))
        else:
            rows = self._query_all(
                "SELECT * FROM profile ORDER BY weight DESC LIMIT ?", (max(1, limit),))
        return [dict(r) for r in rows]

    # -- 工作记忆 --

    # -- 原文链：一条消息一个节点，改一个字整条就对不上 --

    def append_message(self, role: str, content: str,
                       session_id: str = "", at: str = "") -> Dict[str, Any]:
        """往原文链尾部接一条消息，返回新节点。

        不进提交链是有意的：一天几百条消息塞进 mcommit，
        「记忆的历史」会被稀释成流水账。这里要的是不可篡改、查得到原话，
        不是能回滚，所以用哈希链——digest 吃上一条的 digest。
        """
        content = (content or "").strip()
        if not content:
            return {}
        role = (role or "user")[:16]
        at = at or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        with self._tx() as cx:
            n = int((cx.execute("SELECT v FROM meta WHERE k='message_no'").fetchone() or
                     {"v": 0})["v"] or 0) + 1
            prev_digest = ((cx.execute("SELECT v FROM meta WHERE k='message_head'").fetchone()
                           or {"v": ""})["v"]) or ""
            last = cx.execute("SELECT id FROM message ORDER BY seq DESC LIMIT 1").fetchone()
            prev_id = last["id"] if last else ""
            digest = hashlib.sha256(
                ("%s|%s|%s|%s" % (prev_digest, role, at, content)).encode("utf-8")
            ).hexdigest()[:32]
            mid = "t%05d" % n
            cx.execute(
                "INSERT INTO message(id, session_id, role, content, at, seq, prev_id, digest)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (mid, session_id or "", role, content, at, n, prev_id, digest))
            cx.execute("UPDATE meta SET v=? WHERE k='message_no'", (str(n),))
            cx.execute("UPDATE meta SET v=? WHERE k='message_head'", (digest,))
        return {"id": mid, "seq": n, "digest": digest}

    def search_messages(self, query: str = "", limit: int = 10,
                        session_id: str = "", since: str = "",
                        return_meta: bool = False) -> Any:
        """查原话。给词就用 BM25 排（短而精确的排在前面）；不给词就按时间倒序。

        候选先取最近 SEARCH_CANDIDATE_CAP 条；没有命中且原文链更长时，
        扩窗到 SEARCH_EXPANDED_CAP 再扫一遍（专治独有词埋在 200 条之外）。
        扩窗仍没中，就通过 return_meta 把 total / scanned / truncated 报出去，
        让调用方（/记忆原话）能告诉用户「只查了最近 N 条」，别让人以为没说过。

        return_meta=True 时返回 {"rows": [...], "total", "scanned", "truncated",
        "expanded"}；否则只返回 rows 列表（向后兼容老调用方/老测试）。
        单字或切不出 token 的查询（停用词）退回子串命中兜底。
        """
        sql = "SELECT * FROM message WHERE 1=1"
        params: List[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if since:
            sql += " AND at >= ?"
            params.append(since)
        q = (query or "").strip()
        lim = max(1, int(limit))

        def _scan(cap: int) -> List[Dict[str, Any]]:
            rows = [dict(r) for r in self._query_all(
                sql + " ORDER BY seq DESC LIMIT ?", tuple(params) + (cap,))]
            if not q:
                return rows[:lim]
            toks = tokenize(q)
            if len(q) == 1 or not toks:
                # 单字在双字滑窗里天然切不出来，或全是停用词切不出 token：
                # 退回子串命中兜底（老行为）。
                sub = [(r["seq"], r) for r in rows if q in r["content"]]
                sub.sort(key=lambda x: -x[0])
                return [r for _, r in sub[:lim]]
            # 同一文件里现成的 _bm25_scores：候选 content 逐个 tokenize 成 docs
            docs = [tokenize(r["content"]) for r in rows]
            scores = _bm25_scores(docs, toks)
            scored = [(scores[i], r) for i, r in enumerate(rows) if scores[i] > 0]
            scored.sort(key=lambda x: (-x[0], -x[1]["seq"]))
            return [r for _, r in scored[:lim]]

        total = int((self._query_one("SELECT COUNT(*) AS n FROM message")
                     or {"n": 0})["n"] or 0)
        rows = _scan(SEARCH_CANDIDATE_CAP)
        scanned = min(SEARCH_CANDIDATE_CAP, total)
        expanded = False
        if q and not rows and total > scanned:
            # 无命中且库里还有更早的：扩窗重试，别让独有词永远躺在外边。
            rows = _scan(SEARCH_EXPANDED_CAP)
            scanned = min(SEARCH_EXPANDED_CAP, total)
            expanded = True
        truncated = total > scanned
        if return_meta:
            return {"rows": rows, "total": total, "scanned": scanned,
                    "truncated": truncated, "expanded": expanded}
        return rows

    def verify_message_chain(self) -> Dict[str, Any]:
        """从头走一遍原文链：改一个字、中间断过、尾部被删过，都会在这里露出来。

        除了逐行验 digest，还和 meta 里的 message_no / message_head 对账：
        尾部被删了（digest 链看似完整）也照样查得出来。返回值保留
        ok / broken_at / count 三个老字段，另补 expected_* / actual_* / reason。
        """
        rows = [dict(r) for r in self._query_all("SELECT * FROM message ORDER BY seq")]
        actual_count = len(rows)
        _no = self._query_one("SELECT v FROM meta WHERE k='message_no'")
        expected_count = int(((_no["v"] if _no and _no["v"] else "0") or "0"))
        _head = self._query_one("SELECT v FROM meta WHERE k='message_head'")
        expected_head = (_head["v"] if _head and _head["v"] else "") or ""
        actual_head = rows[-1]["digest"] if rows else ""

        def _fail(broken_at: Any, reason: str) -> Dict[str, Any]:
            return {"ok": False, "broken_at": broken_at, "checked": actual_count,
                    "count": actual_count, "head": actual_head,
                    "expected_head": expected_head, "actual_head": actual_head,
                    "expected_count": expected_count, "actual_count": actual_count,
                    "reason": reason}

        # 1) 条数：meta.message_no 必须等于表内 COUNT
        if expected_count != actual_count:
            return _fail(None, "条数不一致：meta.message_no=%d 但表内 COUNT=%d"
                          % (expected_count, actual_count))
        # 2) 链尾：meta.message_head 必须等于最后一条 digest
        if expected_head != actual_head:
            return _fail(None, "链尾不一致：meta.message_head=%s 但实际末条 digest=%s"
                          % (expected_head, actual_head))
        # 3) seq 从 1 连续，无空洞无重复
        for i, r in enumerate(rows):
            if r["seq"] != i + 1:
                return _fail(r["id"], "seq 不连续：第 %d 条应是 %d 实际是 %d"
                             % (i + 1, i + 1, r["seq"]))
        # 4) prev_id 链：每条的前驱必须是上一条
        for i, r in enumerate(rows):
            want_prev = rows[i - 1]["id"] if i > 0 else ""
            if r["prev_id"] != want_prev:
                return _fail(r["id"], "prev_id 断裂：%s 的前驱应是 %s 实际是 %s"
                             % (r["id"], want_prev, r["prev_id"]))
        # 5) digest 链（原逻辑保留）
        prev_digest = ""
        for i, r in enumerate(rows):
            want = hashlib.sha256(
                ("%s|%s|%s|%s" % (prev_digest, r["role"], r["at"], r["content"])).encode("utf-8")
            ).hexdigest()[:32]
            if want != r["digest"]:
                return {"ok": False, "broken_at": r["id"], "checked": i,
                        "count": actual_count, "head": prev_digest,
                        "expected_head": expected_head, "actual_head": actual_head,
                        "expected_count": expected_count, "actual_count": actual_count,
                        "reason": "digest 链断裂于 %s" % r["id"]}
            prev_digest = r["digest"]
        return {"ok": True, "broken_at": "", "checked": actual_count,
                "count": actual_count, "head": prev_digest,
                "expected_head": expected_head, "actual_head": actual_head,
                "expected_count": expected_count, "actual_count": actual_count,
                "reason": ""}

    def message_stats(self) -> Dict[str, Any]:
        n = (self._query_one("SELECT COUNT(*) AS n FROM message") or {"n": 0})["n"]
        first = self._query_one("SELECT at FROM message ORDER BY seq LIMIT 1")
        return {"count": int(n), "since": (first["at"] if first else "")}

    def _recent_hits(self, window_days: float = REVIVE_WINDOW_DAYS) -> Dict[str, int]:
        """最近一段时间里，每条记忆各被想起来几次。真值只在 access_log 里。

        两个坑要筛掉，不然「最近」是假的：
        - seed：迁移时把旧累计访问折成一条留底，它的 at 是迁移那一刻，
          不是「最近」真的哪次被想起。留着会让老记忆在迁移后三天里凭空显得活跃。
        - 孤儿：记忆合并/删除后访问日志还在（m3、m4 已经并进 m6 了），
          JOIN 一下只数还活着的，免得白占名额。
        第三个坑是这次补的：只统计「真的被想起」的路由 —— search / associate /
        manual / recall。兜底（fallback）和激活（activate）只是机器自己把记忆捞出来
        念给模型听，不算「被用户/检索想起」。以前兜底也会记 how=activate，结果零级
        记忆自己浮两次就满足了「3 天内被想起 2 次」的复出条件，假活跃。
        """
        window_days = max(0.0, float(window_days))
        since = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(time.time() - window_days * 86400))
        rows = self._query_all(
            "SELECT a.memory_id, COUNT(*) AS n FROM access_log a"
            " JOIN memory m ON m.id = a.memory_id"
            " WHERE a.at >= ? AND a.how IN ('search','associate','manual','recall')"
            " GROUP BY a.memory_id", (since,))
        return {r["memory_id"]: int(r["n"]) for r in rows}

    def plan_working_set(self, capacity: Optional[int] = None) -> Dict[str, Any]:
        """算一遍工作记忆的换班方案：谁留下、谁被挤走、谁该回来。（只算，不写库）

        现有的一级和「最近又被反复想起」的零级放在同一张榜上排名，
        一起抢 capacity 个名额。一次算清，就不存在先降后升、下轮又降的来回打。
        二级是巩固层，压根不参加这场竞争。
        """
        cap = self.working_capacity if capacity is None else int(capacity)
        now_seq = self.max_created_seq()
        hits = self._recent_hits() if cap > 0 else {}
        rows = [dict(r) for r in self._query_all("SELECT * FROM memory")]
        for r in rows:
            r["activation"] = round(working_activation(r, now_seq,
                                                       hits.get(r["id"], 0)), 4)

        seated: List[Dict[str, Any]] = []
        for r in rows:
            grade = int(r.get("grade") or 0)
            if grade >= 1:
                r["seat"] = "一级" if grade == 1 else "二级"
                if grade == 1:
                    seated.append(r)
            elif hits.get(r["id"], 0) >= REVIVE_HITS:
                r["seat"] = "复出"
                seated.append(r)

        seated.sort(key=lambda x: (-x["activation"], -int(x["created_seq"])))
        if cap <= 0:  # 关掉了：不挤人也不复活，原样报个名额占用
            keep, overflow = seated, []
        else:
            keep, overflow = seated[:cap], seated[cap:]

        keep_ids = {r["id"] for r in keep}
        demote = [r for r in overflow if int(r.get("grade") or 0) == 1
                  and r["id"] not in keep_ids][:max(1, WORKING_DEMOTE_LIMIT)]
        promote = [r for r in keep if int(r.get("grade") or 0) == 0]
        return {"capacity": cap,
                "occupied": len([r for r in seated if int(r.get("grade") or 0) == 1]),
                "demote": demote, "promote": promote,
                "keep": keep, "overflow": len(overflow)}

    def working_set(self, capacity: Optional[int] = None) -> Dict[str, Any]:
        """此刻真正占着工作记忆名额的那几条（只读，给人看）。"""
        plan = self.plan_working_set(capacity)
        return {"capacity": plan["capacity"], "items": plan["keep"],
                "occupied": plan["occupied"]}

    def rebalance_working_set(self, capacity: Optional[int] = None,
                              apply: bool = False) -> Dict[str, Any]:
        """让 grade 真的开始转：超员的降到零级，又被反复想起的升回一级。

        一次改一条也是一次提交，降级/复活都留痕，能回放。
        """
        plan = self.plan_working_set(capacity)
        demote, promote = plan["demote"], plan["promote"]
        if not apply or not (demote or promote):
            return {"applied": False, "capacity": plan["capacity"],
                    "occupied": plan["occupied"], "demoted": [], "promoted": [],
                    "demote": demote, "promote": promote, "overflow": plan["overflow"]}
        done_demote: List[str] = []
        done_promote: List[str] = []
        with self.commit("工作记忆换班：降 %d 升 %d" % (len(demote), len(promote))) as c:
            for item in demote:
                if c.set_grade(item["id"], 0):
                    done_demote.append(item["id"])
            for item in promote:
                if c.set_grade(item["id"], 1):
                    done_promote.append(item["id"])
        self._rebalanced_at = time.time()
        return {"applied": True, "commit": self.head(),
                "capacity": plan["capacity"], "occupied": plan["occupied"],
                "demoted": done_demote, "promoted": done_promote,
                "demote": demote, "promote": promote}

    def maybe_rebalance_working_set(self, cooldown: float = WORKING_COOLDOWN_SECONDS
                                    ) -> Dict[str, Any]:
        """外壳顺手调的那种：有冷却、不超员就什么都不做，失败绝不往外抛。"""
        now = time.time()
        if self.working_capacity <= 0:
            return {"applied": False, "reason": "容量关着"}
        if now - self._rebalanced_at < max(0.0, float(cooldown)):
            return {"applied": False, "reason": "还在冷却里"}
        try:
            res = self.rebalance_working_set(apply=True)
        except (LedgerError, StaleHeadError) as e:
            log.warning("[记忆账本] 工作记忆换班失败: %r", e)
            return {"applied": False, "reason": str(e)}
        self._rebalanced_at = now
        return res

    # -- 冲刷 --

    def flush_plan(self) -> List[Dict[str, Any]]:
        now_seq = self.max_created_seq()
        plan = []
        for r in self._query_all("SELECT * FROM memory ORDER BY created_seq"):
            r = dict(r)
            # 长期主题 / 高重要性记忆钉住，不参与自动冲刷
            if r["level"] == "topic" or float(r["importance"]) >= 0.85:
                continue
            clue_count = self._query_one(
                "SELECT COUNT(*) AS n FROM memory_clue WHERE memory_id = ?",
                (r["id"],))["n"]
            ri = retention_index(r["importance"], clue_count, r["access_count"])
            pressure = flush_pressure(now_seq, r["last_access_seq"], ri)
            if pressure >= 1.0:
                plan.append({
                    "memory_id": r["id"],
                    "grade": r["grade"],
                    "flush_amount": now_seq - int(r["last_access_seq"]),
                    "retention": round(ri, 3),
                    "pressure": round(pressure, 3),
                    "action": "delete" if int(r["grade"]) <= 0 else "demote",
                    "summary": r["summary"][:50],
                })
        plan.sort(key=lambda x: -x["pressure"])
        return plan

    def flush(self, apply: bool = False, limit: int = 50) -> Dict[str, Any]:
        """一次只降一级；零级再超标才淘汰。"""
        plan = self.flush_plan()[:max(1, limit)]
        if not apply or not plan:
            return {"applied": False, "count": len(plan), "plan": plan}
        demoted = deleted = 0
        with self.commit("信息冲刷：处理 %d 条" % len(plan)) as c:
            for item in plan:
                if item["action"] == "delete":
                    if c.forget(item["memory_id"]):
                        deleted += 1
                elif c.set_grade(item["memory_id"], int(item["grade"]) - 1):
                    demoted += 1
        return {"applied": True, "commit": self.head(), "demoted": demoted,
                "deleted": deleted, "plan": plan}

    # -- 历史与回放 --

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        out = []
        for r in self._query_all("SELECT * FROM mcommit ORDER BY CAST(SUBSTR(id,2) AS INTEGER) DESC"
                                 " LIMIT ?", (max(1, limit),)):
            r = dict(r)
            r["rows"] = self._query_one(
                "SELECT COUNT(*) AS n FROM commit_row WHERE commit_id = ?", (r["id"],))["n"]
            out.append(r)
        return out

    def show_commit(self, commit_id: str) -> Optional[Dict[str, Any]]:
        row = self._query_one("SELECT * FROM mcommit WHERE id = ?", (commit_id,))
        if not row:
            return None
        data = dict(row)
        data["rows"] = [dict(x) for x in self._query_all(
            "SELECT * FROM commit_row WHERE commit_id = ? ORDER BY seq", (commit_id,))]
        return data

    def materialize(self, commit_id: str = "") -> List[Dict[str, Any]]:
        """把历史重放到某个提交，得到那一刻的记忆状态（只读）。"""
        target = commit_id or self.head()
        state: Dict[str, Dict[str, Any]] = {}
        found = False
        for c in self._query_all("SELECT * FROM mcommit"
                                 " ORDER BY CAST(SUBSTR(id,2) AS INTEGER)"):
            for r in self._query_all("SELECT * FROM commit_row WHERE commit_id = ?"
                                     " ORDER BY seq", (c["id"],)):
                if r["tbl"] != "memory":
                    continue
                after = json.loads(r["after_json"]) if r["after_json"] else None
                before = json.loads(r["before_json"]) if r["before_json"] else None
                snap = after or before or {}
                mid = snap.get("id")
                if r["op"] == "insert" and after:
                    state[mid] = dict(after)
                elif r["op"] == "update" and after and mid in state:
                    state[mid].update(after)
                elif r["op"] == "delete" and mid:
                    state.pop(mid, None)
            if c["id"] == target:
                found = True
                break
        if commit_id and not found:
            # 以前这里静默返回空表，"提交号打错了"和"那一刻真的没有记忆"分不清
            raise LedgerError("没有这个提交：%s" % commit_id)
        for snap in state.values():
            # 访问痕迹不在快照里，这里补默认：刚写完的那条访问序号等于创建序号，
            # 不然调用方会看到 None，也会把它的「新鲜度」算错
            snap.setdefault("last_access_seq", snap.get("created_seq") or 0)
            snap.setdefault("access_count", 0)
        return sorted(state.values(), key=lambda x: int(x.get("created_seq") or 0))

    def _replay_tables(self, target: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """回放到某个提交时，各投影表的完整状态（行主键 → 行）。"""
        state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for c in self._query_all("SELECT id FROM mcommit"
                                 " ORDER BY CAST(SUBSTR(id,2) AS INTEGER)"):
            for r in self._query_all("SELECT * FROM commit_row WHERE commit_id = ?"
                                     " ORDER BY seq", (c["id"],)):
                tbl = r["tbl"]
                key_of = PROJECTIONS.get(tbl)
                if key_of is None:
                    continue
                after = json.loads(r["after_json"]) if r["after_json"] else None
                before = json.loads(r["before_json"]) if r["before_json"] else None
                snap = after or before or {}
                key = key_of(snap)
                bucket = state.setdefault(tbl, {})
                if r["op"] == "insert" and after:
                    bucket[key] = dict(after)
                elif r["op"] == "update" and after and key in bucket:
                    bucket[key].update(after)
                elif r["op"] == "delete":
                    bucket.pop(key, None)
            if c["id"] == target:
                break
        return state

    def compact(self, keep_recent: int = 10) -> Dict[str, Any]:
        """把久远的提交压成一条基线，HEAD 和当前状态一个字节都不动。

        做法：把「最近 keep_recent 条」之前的历史，重放成一份快照，
        存进 C0 这一条基线提交里，再把保留链的第一环接到 C0 上。
        之后 materialize 到任何保留的提交，结果和压缩前一样。
        """
        keep_recent = max(1, int(keep_recent))
        chain = [r["id"] for r in self._query_all(
            "SELECT id FROM mcommit ORDER BY CAST(SUBSTR(id,2) AS INTEGER)")]
        if len(chain) <= keep_recent + 1:
            return {"compacted": False, "reason": "提交还少，不用压缩",
                    "commits": len(chain), "kept": len(chain)}

        old, keep = chain[:-keep_recent], chain[-keep_recent:]
        old_meta = self._query_one("SELECT created_at FROM mcommit WHERE id = ?", (old[-1],))
        base_time = old_meta["created_at"] if old_meta else now_str()
        state = self._replay_tables(old[-1])
        head_before = self.head()

        with self._tx() as cx:
            if self._meta(cx, "head") != head_before:
                raise StaleHeadError("压缩期间 HEAD 变了，重来一次")
            for cid in old:
                cx.execute("DELETE FROM commit_row WHERE commit_id = ?", (cid,))
                cx.execute("DELETE FROM mcommit WHERE id = ?", (cid,))
            cx.execute("DELETE FROM commit_row WHERE commit_id = ?", (BASELINE_COMMIT,))
            cx.execute("DELETE FROM mcommit WHERE id = ?", (BASELINE_COMMIT,))
            cx.execute("INSERT INTO mcommit(id, parent, message, created_at) VALUES(?,?,?,?)",
                       (BASELINE_COMMIT, None,
                        "压缩：把最早的 %d 个提交并成一份基线" % len(old), base_time))
            seq = 0
            for tbl, rows in state.items():
                for key, row in rows.items():
                    seq += 1
                    cx.execute(
                        "INSERT INTO commit_row(commit_id, seq, tbl, op, row_key,"
                        " before_json, after_json) VALUES(?,?,?,?,?,?,?)",
                        (BASELINE_COMMIT, seq, tbl, "insert",
                         "baseline:" + key, None, json.dumps(row, ensure_ascii=False)))
            cx.execute("UPDATE mcommit SET parent = ? WHERE id = ?",
                       (BASELINE_COMMIT, keep[0]))
        return {"compacted": True, "merged": len(old), "kept": keep_recent,
                "baseline_rows": seq, "head": self.head(),
                "commits": self._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"]}

    def stats(self) -> Dict[str, Any]:
        by_grade = {GRADES[k]: self._query_one(
            "SELECT COUNT(*) AS n FROM memory WHERE grade = ?", (k,))["n"] for k in GRADES}
        by_level = {lv: self._query_one(
            "SELECT COUNT(*) AS n FROM memory WHERE level = ?", (lv,))["n"] for lv in LEVELS}
        return {
            "head": self.head(),
            "created_seq": self.max_created_seq(),
            "schema_version": self.schema_version(),
            "commits": self._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"],
            "memories": self._query_one("SELECT COUNT(*) AS n FROM memory")["n"],
            "by_grade": by_grade,
            "by_level": {k: v for k, v in by_level.items() if v},
            "working_capacity": self.working_capacity,
            "clues": self._query_one("SELECT COUNT(*) AS n FROM clue")["n"],
            "links": self._query_one("SELECT COUNT(*) AS n FROM memory_link")["n"],
            "profiles": self._query_one("SELECT COUNT(*) AS n FROM profile")["n"],
            "accesses": self._query_one("SELECT COUNT(*) AS n FROM access_log")["n"],
            "db_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
        }

    # -- 外置：导出成能直接翻的文件 --

    def export_markdown(self, path: str = "") -> str:
        rows = [dict(r) for r in self._query_all(
            "SELECT * FROM memory ORDER BY grade DESC, created_seq DESC")]
        clue_map: Dict[str, List[str]] = {}
        for rel in self._query_all("SELECT mc.memory_id, c.content FROM memory_clue mc"
                                   " JOIN clue c ON c.id = mc.clue_id"):
            clue_map.setdefault(rel["memory_id"], []).append(rel["content"])
        st = self.stats()
        lines = ["# 记忆账本", "",
                 "HEAD `%s` · 创建序号 %d · 记忆 %d 条 · 线索 %d 个 · 连边 %d 条" % (
                     st["head"], st["created_seq"], st["memories"], st["clues"], st["links"]),
                 ""]
        for r in rows:
            lines.append("## %s · %s · %s" % (
                r["id"], r["level"], GRADES.get(int(r["grade"]), "?")))
            lines.append("")
            lines.append(r["summary"])
            lines.append("")
            meta = "- 重要性 %.2f · 访问 %d 次 · 创建序号 %d · 最后访问 %d · %s" % (
                r["importance"], r["access_count"], r["created_seq"],
                r["last_access_seq"], r["created_at"])
            lines.append(meta)
            if clue_map.get(r["id"]):
                lines.append("- 概念：" + "、".join(clue_map[r["id"]]))
            lines.append("")
        text = "\n".join(lines)
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        return text

    # -- 外置：机器搬家用 --

    def dump_jsonl(self, path: str = "") -> str:
        """把整个账本导成可往返的 JSONL：装进另一个空库就能接着用。

        和 export_markdown 是两件事：那个给人翻，丢了也只丢阅读体验；
        这个给机器搬家，少了任何一张表都会让提交链对不上。

        第一行是文件头，之后一行一条记录：{"t": 表名, "r": 行}。
        """
        header = {
            "format": DUMP_FORMAT,
            "version": DUMP_VERSION,
            "schema_version": self.schema_version(),
            "core_api_version": CORE_API_VERSION,
            "dumped_at": now_str(),
        }
        chunks = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
        # 导出一律走一把锁：中途被人写进来，导出的就是半截账本
        with self._lock:
            for tbl in DUMP_TABLES:
                order = _DUMP_ORDER.get(tbl)
                sql = "SELECT * FROM %s" % tbl
                if order:
                    sql += " ORDER BY " + order
                for row in self._query_all(sql):
                    data = dict(row)
                    if tbl == "meta" and data.get("k") in _LOCAL_META:
                        continue
                    chunks.append(json.dumps({"t": tbl, "r": data},
                                             ensure_ascii=False, sort_keys=True))
        text = "\n".join(chunks) + "\n"
        if path:
            target = os.path.abspath(path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, target)
        return text

    def _table_columns(self, cx: sqlite3.Connection, tbl: str) -> List[str]:
        return [r["name"] for r in cx.execute("PRAGMA table_info(%s)" % tbl).fetchall()]

    def load_jsonl(self, path: str, replace: bool = False) -> Dict[str, Any]:
        """把 dump_jsonl 的产物灌回来。

        默认只肯往空库里灌：覆盖是有代价的操作，不该是默认行为，
        想覆盖得自己说 replace=True。
        """
        if not path or not os.path.exists(path):
            raise LedgerError("找不到要导入的文件：%s" % path)

        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if not lines:
            raise LedgerError("这个文件是空的")

        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as e:
            raise LedgerError("文件头不是合法 JSON：%r" % (e,))
        if not isinstance(header, dict) or header.get("format") != DUMP_FORMAT:
            raise LedgerError("不是记忆账本的备份文件（缺少 format=%s）" % DUMP_FORMAT)
        if int(header.get("version") or 0) > DUMP_VERSION:
            raise LedgerError("备份格式 v%s 比当前支持的 v%d 新" % (
                header.get("version"), DUMP_VERSION))
        if int(header.get("schema_version") or 0) > SCHEMA_VERSION:
            raise LedgerError("备份来自结构版本 %s，比当前代码支持的 %d 新" % (
                header.get("schema_version"), SCHEMA_VERSION))

        rows: Dict[str, List[Dict[str, Any]]] = {}
        skipped = 0
        for ln in lines[1:]:
            try:
                item = json.loads(ln)
            except json.JSONDecodeError:
                skipped += 1
                continue
            tbl = item.get("t") if isinstance(item, dict) else None
            row = item.get("r") if isinstance(item, dict) else None
            if tbl not in DUMP_TABLES or not isinstance(row, dict):
                skipped += 1
                continue
            rows.setdefault(tbl, []).append(row)

        if not rows.get("memory") and not rows.get("mcommit"):
            # 空账本的备份也是合法备份，照收；真正损坏的文件连文件头都过不了
            log.debug("[记忆账本] 导入的是空备份：%s", path)

        existing = self._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"]
        if existing and not replace:
            raise LedgerError(
                "这个库已经有 %d 个提交，不覆盖。真想替换就传 replace=True" % existing)

        written = 0
        dropped_cols: List[str] = []
        with self._tx() as cx:
            if replace:
                for tbl in reversed(DUMP_TABLES):
                    cx.execute("DELETE FROM %s" % tbl)
            for tbl in DUMP_TABLES:
                valid = set(self._table_columns(cx, tbl))
                for row in rows.get(tbl, []):
                    cols = [c for c in row if c in valid]
                    if len(cols) != len(row):
                        dropped_cols.extend(
                            "%s.%s" % (tbl, c) for c in row if c not in valid)
                    if not cols:
                        continue
                    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
                        tbl, ", ".join(cols), ", ".join("?" * len(cols)))
                    cx.execute(sql, tuple(row[c] for c in cols))
                    written += 1

        # 导回来的库可能来自更老的版本，这里补一步；也可能是更新的库，那就已经拦过了
        migrated = self._migrate()

        warnings: List[str] = []
        head = self.head()
        if head and not self.show_commit(head):
            warnings.append("HEAD %s 在提交表里找不到" % head)
        if not head and (rows.get("memory") or []):
            warnings.append("有记忆但没有 HEAD，提交链是断的")
        return {
            "tables": {t: len(v) for t, v in rows.items()},
            "written": written,
            "skipped": skipped,
            "dropped_columns": sorted(set(dropped_cols)),
            "migrated": migrated,
            "head": head,
            "memories": self.stats()["memories"],
            "warnings": warnings,
        }

    def maybe_export_daily(self, path: str = "") -> bool:
        """每天最多导出一次。跨天才写，同一天再调直接返回 False。

        给外壳在请求前顺手调一下，就不用额外挂定时器；
        哪天进程一直没重启，账本也还是会刷新。
        """
        if not path:
            return False
        today = time.strftime("%Y-%m-%d")
        row = self._query_one("SELECT v FROM meta WHERE k = 'last_export_date'")
        if row and row["v"] == today:
            return False
        try:
            self.export_markdown(path)
        except Exception as e:
            log.warning("[记忆账本] 每日导出失败: %r", e)
            return False
        with self._tx() as cx:
            self._set_meta(cx, "last_export_date", today)
        # 顺手把访问日志收一收：它只服务于衰减计算，留最近一段就够
        try:
            self.prune_access_log()
        except Exception as e:
            log.warning("[记忆账本] 访问日志修剪失败: %r", e)
        return True

    def health_check(self, export_path: str = "") -> Dict[str, Any]:
        """自检：能读、能写、链不断、投影对得上、导出目录能落盘。

        重载插件之后跑一遍，比「看起来没报错」可靠。
        """
        problems: List[str] = []
        st = self.stats()

        # 1. 提交链：每个提交的 parent 要么为空，要么真的存在
        ids = {r["id"] for r in self._query_all("SELECT id FROM mcommit")}
        for r in self._query_all("SELECT id, parent FROM mcommit"):
            if r["parent"] and r["parent"] not in ids:
                problems.append("提交 %s 的父节点 %s 不存在" % (r["id"], r["parent"]))

        # 2. 投影表：提交里记的行必须真的落在表里（抽最后一条提交）
        last = self.show_commit(st["head"]) if st["head"] else None
        if st["head"] and not last:
            problems.append("HEAD %s 在提交表里找不到" % st["head"])

        # 3. 计数：记忆数与提交行数不该互相矛盾
        mem_rows = self._query_one("SELECT COUNT(*) AS n FROM memory")["n"]
        if st["memories"] != mem_rows:
            problems.append("记忆计数不一致：stats=%s，表=%s" % (st["memories"], mem_rows))

        # 4. 写能力：往 meta 里写一次探针再删掉，不碰任何投影表
        try:
            with self._tx() as cx:
                self._set_meta(cx, "health_probe", now_str())
                cx.execute("DELETE FROM meta WHERE k = 'health_probe'")
        except Exception as e:
            problems.append("数据库不可写：%r" % (e,))

        # 5. 导出：只在给了路径时才真的写文件
        export_ok = False
        if export_path:
            try:
                self.export_markdown(export_path)
                export_ok = True
            except Exception as e:
                problems.append("导出失败：%r" % (e,))

        # 6. 原文链：逐条验 digest、对账 message_no / message_head。
        #    改一个字、中间断过、尾部被删都会在这里露出来。自检能看到
        #    原文链条数与是否断裂，而不是只说一句「memory 表还好」。
        chain = self.verify_message_chain()
        if not chain["ok"]:
            problems.append("原文链有问题：%s" % chain["reason"])

        return {"ok": not problems, "problems": problems, "export_ok": export_ok,
                "message_chain": chain, "stats": st}

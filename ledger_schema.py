from __future__ import annotations

import os
import sqlite3
import threading
from typing import Dict, List, Optional

if __package__:
    from .ledger_core import LedgerError
else:
    from ledger_core import LedgerError


# 表结构版本。加列、改列、加表才 +1，并在 _MIGRATIONS 里补一步迁移。
# 这个号跟着库走（存在 meta 表里），不同宿主之间搬库全靠它认路。
# v2：访问痕迹（access_count / last_access_seq）不再是账本内容，另立 access_log 表。
# v3：原文链 message 表。
# v4：message 表补 seq 索引。
# v5：梦境与职守改造（2026-11-12 施工单 §4.2）：五张新表 + 4 个索引。
#     memory 主表结构一个字没动；全部走「新表 + 引用」，照 ledger.py:251
#     的 v2 迁移先例 CREATE TABLE IF NOT EXISTS。
#     新表：memory_condition（条件并存）/ memory_status（状态档，append-only）/
#     dream_proposal（梦唯一写入口）/ memory_source_anchor（增量原文锚）/
#     user_denial（用户否认，权重最高）。
# v6：创作项目旁路表 creative（2026-11-15）。纯加法：id 主键 + 可读正文
#     body + 整条原始 payload；不碰 memory 主表、不进提交链、不参与任何
#     检索打分，只供 upsert_creative / get_creative / list_creatives 用。
#     created_seq 是本表先后序号（MAX+1），不占账本全局 created_seq。
# v7：core_rule 人格分区（2026-11-15）。加三列：kind（self/core，人格分区）、
#     source（写入来源）、updated_at（每次更新刷写）。只加列不改数据：
#     存量行 kind 落默认 'self'，不伪造任何 core 行——基础设定只能由
#     显式播种（_seed_persona_core）产生。kind='core' 的行只读，
#     写路径一律 LedgerError，唯一例外走内部参数 _allow_core=True。
# v8：群聊支持三张新表（2026-11-15）。memory_speaker（记忆↔说话人）/
#     profile_visibility（画像可见性）/ user_attitude（坏感度）。照 v5/v6
#     先例纯加法：memory 主表一行一列不动，全部新表 + 引用，
#     CREATE TABLE IF NOT EXISTS 老库升上来建表。前两张进提交链
#     （可回溯），user_attitude 是手动设置的用户数据，也走提交链。
SCHEMA_VERSION = 8


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
    # v7：kind 是人格分区（self=可编辑人格设定，core=只读基础设定）；
    # source 记写入来源（self/seed/…）；updated_at 每次更新刷写。
    # DDL 照 v5/v6 先例与 _migrate_6_to_7 各重复一份。
    """CREATE TABLE IF NOT EXISTS core_rule (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'self',
        source TEXT NOT NULL DEFAULT 'self',
        updated_at TEXT NOT NULL DEFAULT '')""",
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
    # 它要的是篡改检测与内部一致性校验（单条改动、中间删除、尾部截断都查得出）、
    # 查得到原话，不是能回滚，所以用哈希链而不是 commit。边界说清楚（P0-2）：
    # 防不住能同时改消息、digest、链头并重算整条链的人，也不等于来源可信。
    """CREATE TABLE IF NOT EXISTS message (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        at TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        prev_id TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL)""",
    # ---- v5 · 五张新表（施工单 §4.2）----
    # memory 主表结构不动，全部用新表 + 引用。这些表不进提交链：
    # 它们是职守状态与梦的写入口，不是记忆正文；回放/压缩不动它们。
    # 1. 条件并存（时变/场景依赖）。允许为空；为空的一律挂 conflicted 等证据。
    """CREATE TABLE IF NOT EXISTS memory_condition (
        memory_id TEXT NOT NULL,
        cond_json TEXT NOT NULL DEFAULT '',
        cond_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (memory_id, cond_json))""",
    # 2. 状态档（append-only，取最新一条为准）。判不了是正常的，挂那儿等。
    """CREATE TABLE IF NOT EXISTS memory_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('active','coexist','conflicted','superseded','corrected','disputed')),
        reason TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL)""",
    # 3. 梦提案（梦只写这里，不直接改记忆既有行）。
    #    kind='new_memory' 行与 memory 插入同事务落地（见 _seal 的 extra_tx）。
    """CREATE TABLE IF NOT EXISTS dream_proposal (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('link','node','new_memory','suspect')),
        payload_json TEXT NOT NULL,
        basis_ids TEXT NOT NULL DEFAULT '',
        base_commit_seq INTEGER NOT NULL DEFAULT 0,
        provenance TEXT NOT NULL DEFAULT 'dream',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','accepted','rejected','expired')),
        created_at TEXT NOT NULL,
        decided_at TEXT)""",
    # 4. 增量原文锚（记忆 → 消息链位置）。只对新发生的记忆生效，
    #    存量 526 条中转站记忆不许编造原文，只能标疑。
    """CREATE TABLE IF NOT EXISTS memory_source_anchor (
        memory_id TEXT NOT NULL,
        message_seq INTEGER NOT NULL,
        digest TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL,
        PRIMARY KEY (memory_id, message_seq))""",
    # 5. 用户否认（权重最高）。说一句「你记错了」立刻生效，不等梦跑。
    """CREATE TABLE IF NOT EXISTS user_denial (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        said TEXT NOT NULL,
        message_seq INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL)""",
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
    # v5 · 4 个索引（施工单 §4.2）
    "CREATE INDEX IF NOT EXISTS idx_status_memory ON memory_status(memory_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_status ON dream_proposal(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_anchor_memory ON memory_source_anchor(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_denial_memory ON user_denial(memory_id, at)",
    # v6 · 创作项目旁路表（2026-11-15）。outline 存 JSON 字符串、payload 存
    # 整条原始 JSON（json.dumps ensure_ascii=False）；body 是 draft_chunks
    # 合并后的可读正文。不进提交链（照 memory_status 旁路先例）。
    """CREATE TABLE IF NOT EXISTS creative (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        work_type TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        premise TEXT NOT NULL DEFAULT '',
        outline TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        current_chars INTEGER NOT NULL DEFAULT 0,
        target_chars INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        created_seq INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_creative_seq ON creative(created_seq)",
    # v8 · 群聊支持三张表（2026-11-15）。照 v5/v6 先例：DDL 与
    # _migrate_7_to_8 各重复一份；memory 主表一行一列不动。
    # 1. 记忆↔说话人：谁说的（进提交链，可回溯）。一条记忆可以有多个
    #    说话人（群聊沉淀一条里可能提多个人），行主键是 (memory_id, speaker_id)。
    """CREATE TABLE IF NOT EXISTS memory_speaker (
        memory_id TEXT NOT NULL,
        speaker_id TEXT NOT NULL,
        speaker_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (memory_id, speaker_id))""",
    "CREATE INDEX IF NOT EXISTS idx_speaker_id ON memory_speaker(speaker_id)",
    # 2. 画像可见性：owner 私聊抽出的画像事实 private，群聊可为 public。
    #    缺行一律按 private 处理（保守），读侧不 join 也不炸。
    """CREATE TABLE IF NOT EXISTS profile_visibility (
        profile_id TEXT PRIMARY KEY,
        visibility TEXT NOT NULL DEFAULT 'private'
            CHECK (visibility IN ('private','public')),
        updated_at TEXT NOT NULL)""",
    # 3. 坏感度分级：0 正常、4 最低。与画像彻底分开存，owner（master）
    #    的行写路径一律拒绝（见 ATTITUDE_OWNER_ID）。
    """CREATE TABLE IF NOT EXISTS user_attitude (
        user_id TEXT PRIMARY KEY,
        level INTEGER NOT NULL DEFAULT 0 CHECK (level BETWEEN 0 AND 4),
        reason TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL)""",
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
    "persona_no": "0",
}


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


def _migrate_4_to_5(led: "Ledger", cx: sqlite3.Connection) -> None:
    """4 → 5：梦境与职守改造的五张新表 + 4 个索引（施工单 §4.2）。

    照 ledger.py:251 的 v2 迁移先例：DDL 与 SCHEMA_STATEMENTS 重复一份，
    CREATE TABLE IF NOT EXISTS，老库升上来建表，新库建表语句早已生效。
    纯加法，memory 主表一行一列都不动，存量数据不动。
    """
    for stmt in (
        """CREATE TABLE IF NOT EXISTS memory_condition (
            memory_id TEXT NOT NULL,
            cond_json TEXT NOT NULL DEFAULT '',
            cond_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, cond_json))""",
        """CREATE TABLE IF NOT EXISTS memory_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN
                ('active','coexist','conflicted','superseded','corrected','disputed')),
            reason TEXT NOT NULL DEFAULT '',
            evidence_ref TEXT NOT NULL DEFAULT '',
            at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS dream_proposal (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('link','node','new_memory','suspect')),
            payload_json TEXT NOT NULL,
            basis_ids TEXT NOT NULL DEFAULT '',
            base_commit_seq INTEGER NOT NULL DEFAULT 0,
            provenance TEXT NOT NULL DEFAULT 'dream',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','rejected','expired')),
            created_at TEXT NOT NULL,
            decided_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS memory_source_anchor (
            memory_id TEXT NOT NULL,
            message_seq INTEGER NOT NULL,
            digest TEXT NOT NULL DEFAULT '',
            at TEXT NOT NULL,
            PRIMARY KEY (memory_id, message_seq))""",
        """CREATE TABLE IF NOT EXISTS user_denial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            said TEXT NOT NULL,
            message_seq INTEGER NOT NULL DEFAULT 0,
            at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_status_memory ON memory_status(memory_id, at)",
        "CREATE INDEX IF NOT EXISTS idx_proposal_status ON dream_proposal(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_anchor_memory ON memory_source_anchor(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_denial_memory ON user_denial(memory_id, at)",
    ):
        cx.execute(stmt)
    return None


def _migrate_5_to_6(led: "Ledger", cx: sqlite3.Connection) -> None:
    """5 → 6：创作项目旁路表 creative（2026-11-15）。

    照 v4_to_5 先例：DDL 与 SCHEMA_STATEMENTS 重复一份，
    CREATE TABLE IF NOT EXISTS，老库升上来建表，新库建表语句早已生效。
    纯加法：memory 主表与检索逻辑一行不动。
    """
    for stmt in (
        """CREATE TABLE IF NOT EXISTS creative (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            work_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            premise TEXT NOT NULL DEFAULT '',
            outline TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            current_chars INTEGER NOT NULL DEFAULT 0,
            target_chars INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_seq INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS idx_creative_seq ON creative(created_seq)",
    ):
        cx.execute(stmt)
    return None


def _migrate_6_to_7(led: "Ledger", cx: sqlite3.Connection) -> None:
    """6 → 7：core_rule 人格分区（2026-11-15）。

    照 v5/v6 先例：DDL 与 SCHEMA_STATEMENTS 重复一份。这里是加列，
    ALTER TABLE ... ADD COLUMN 对存量行填常量默认值：kind='self'、
    source='self'、updated_at=''。只加列不改数据，不伪造 core 行——
    基础设定（kind='core'）只能由显式播种（_seed_persona_core）产生，
    迁移不猜、不编。纯加法：    content/priority/created_at 与全部存量行
    一个字节不动。

    幂等保护：新库由 SCHEMA_STATEMENTS 建表时已带三列（然后从版本 0
    全链迁移），ALTER ADD COLUMN 不像 CREATE TABLE IF NOT EXISTS
    那样天然幂等，这里先 PRAGMA 查列，缺哪列才补哪列——老库升上来
    三列全缺照加，新库一列都不动。
    """
    cols = {r["name"] for r in cx.execute(
        "PRAGMA table_info(core_rule)").fetchall()}
    for name, ddl in (
        ("kind", "ALTER TABLE core_rule ADD COLUMN kind TEXT NOT NULL DEFAULT 'self'"),
        ("source", "ALTER TABLE core_rule ADD COLUMN source TEXT NOT NULL DEFAULT 'self'"),
        ("updated_at",
         "ALTER TABLE core_rule ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            cx.execute(ddl)
    return None


def _migrate_7_to_8(led: "Ledger", cx: sqlite3.Connection) -> None:
    """7 → 8：群聊支持三张新表（2026-11-15）。

    照 v4_to_5 先例：DDL 与 SCHEMA_STATEMENTS 重复一份，
    CREATE TABLE IF NOT EXISTS，老库升上来建表，新库建表语句早已生效。
    纯加法：memory 主表一行一列不动，存量数据不动。
    """
    for stmt in (
        """CREATE TABLE IF NOT EXISTS memory_speaker (
            memory_id TEXT NOT NULL,
            speaker_id TEXT NOT NULL,
            speaker_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, speaker_id))""",
        "CREATE INDEX IF NOT EXISTS idx_speaker_id ON memory_speaker(speaker_id)",
        """CREATE TABLE IF NOT EXISTS profile_visibility (
            profile_id TEXT PRIMARY KEY,
            visibility TEXT NOT NULL DEFAULT 'private'
                CHECK (visibility IN ('private','public')),
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS user_attitude (
            user_id TEXT PRIMARY KEY,
            level INTEGER NOT NULL DEFAULT 0 CHECK (level BETWEEN 0 AND 4),
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL)""",
    ):
        cx.execute(stmt)
    return None


def __init__(self, db_path: str, durability: str = "NORMAL", *, AUTO_COMPACT_AFTER, AUTO_COMPACT_KEEP, WAL_CHECKPOINT_BYTES, WORKING_CAPACITY):
    durability = (durability or "").strip().upper()
    if durability not in self.DURABILITY_LEVELS:
        raise LedgerError(
            "durability 只接受 NORMAL/FULL，收到：%r。NORMAL 偏性能"
            "（断电可能丢最近几秒写入），FULL 每次同步落盘"
            "（断电几乎不丢，代价是每次写入都付 fsync）" % (durability,))
    self.durability = durability
    self.db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
    self._lock = threading.RLock()
    self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                 isolation_level=None, timeout=15)
    self._conn.row_factory = sqlite3.Row
    self._conn.execute("PRAGMA journal_mode=WAL")
    self._conn.execute("PRAGMA synchronous=%s" % self.durability)
    # 唤起冷却：刚浮现过的记忆，短时间内不再重复浮现
    self._surfaced: Dict[str, float] = {}
    # 切词缓存：memory_id -> (原文, tokens)。原文变了就不命中，等于自带失效；
    # 只省切词，不改排序，缓存丢了重算一遍也不影响结果。
    self._tok_cache: Dict[str, Tuple[str, List[str]]] = {}
    # 线索切词缓存：线索变动远少于记忆，重复切词纯属浪费
    self._clue_tok_cache: Dict[str, Tuple[str, set]] = {}
    # 自动压缩阈值（可被外壳/测试覆盖）
    self.auto_compact_after = AUTO_COMPACT_AFTER
    self.auto_compact_keep = AUTO_COMPACT_KEEP
    # 工作记忆容量（可被外壳/测试覆盖；0 = 关掉分级运转）
    self.working_capacity = WORKING_CAPACITY
    self._rebalanced_at = 0.0
    # WAL 截断：阈值可被外壳/测试覆盖，冷却时刻记在这里
    self.wal_checkpoint_bytes = WAL_CHECKPOINT_BYTES
    self._checkpointed_at = 0.0
    # 自动写入守卫计数（restricted_auto_write）：>0 时人格分区只读。
    # 自动沉淀 / 梦产出等非用户显式发起的写路径在此范围内开写，
    # 人格写入接口一律硬拦截（见 _assert_user_write_scope）。
    self._auto_write_guard = 0
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
    # 结构迁移走完，再补一次存量沉淀的数据迁移（N6）：也走提交链，
    # 先查后写，没有存量就一个提交都不动，重复执行不出错。
    self._migrate_auto_sink_legacy()
    # v7 哈希哨兵：启动核对一次。不符只报警记日志，不阻止启动、
    # 不自动修——修是人工确认后的 reseal_persona_core 的事。
    self._verify_persona_core_seal(context="startup")

def schema_version(self) -> int:
    """库里的结构版本。老库没有这个键时返回 0，不假装它是最新的。"""
    row = self._query_one("SELECT v FROM meta WHERE k = 'schema_version'")
    try:
        return int(row["v"]) if row else 0
    except (TypeError, ValueError):
        return 0


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

def meta_get(self, key: str, default: str = "") -> str:
    """读 meta 表一个键（只读封装，复用内部 _meta）。

        给沉淀水位线这类跨实例元数据用：键不存在或值为空都算「没有」，
        返回 default。读拿连接锁，不会撞进别人写事务的中间态。
        """
    with self._lock:
        v = self._meta(self._conn, key)
    return v if v else default

def meta_set(self, key: str, value: str) -> None:
    """写 meta 表一个键（自带一个独立事务，复用 _set_meta）。

        一个键一次提交，失败整条回滚；调用方不用自己包 _tx。
        """
    with self._tx() as cx:
        self._set_meta(cx, key, value)

def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    # 读也要拿同一把锁：连接是共享的（check_same_thread=False），
    # 写那边有事务，读这边裸奔就可能读进别人的事务中间态。
    # RLock 可重入，事务里再调这些查询不会死锁。
    with self._lock:
        return self._conn.execute(sql, params).fetchone()

def _query_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    with self._lock:
        return self._conn.execute(sql, params).fetchall()

def _counters(self) -> Dict[str, int]:
    data = {r["k"]: r["v"] for r in self._query_all("SELECT * FROM meta")}
    return {k: int(data.get(k) or 0) for k in
            ("commit_no", "memory_no", "clue_no", "profile_no",
             "persona_no", "created_seq")}


# -- 读 --

def head(self) -> str:
    row = self._query_one("SELECT v FROM meta WHERE k = 'head'")
    return row["v"] if row and row["v"] else ""

def max_created_seq(self) -> int:
    row = self._query_one("SELECT v FROM meta WHERE k = 'created_seq'")
    return int(row["v"]) if row else 0

def max_persona_no(self) -> int:
    """core_rule 表里现有 p 编号的最大值。

        meta.persona_no 是写入计数，表里的编号才是实况；老库迁移、
        v4 旧备份导入都可能让表跑到计数器前面，开提交时取两者最大，
        不给 UNIQUE 约束留机会。
        """
    row = self._query_one(
        "SELECT COALESCE(MAX(CAST(substr(id, 2) AS INTEGER)), 0) AS n"
        " FROM core_rule WHERE id LIKE 'p%'")
    return int(row["n"]) if row else 0

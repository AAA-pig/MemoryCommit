from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

if __package__:
    from .ledger_core import (
        AUTO_SINK_LABEL, AUTO_SINK_TYPE_TAG, GRADES, LEVELS, LedgerError, REVIVE_HITS,
        REVIVE_WINDOW_DAYS, StaleHeadError, WAL_CHECKPOINT_COOLDOWN, WORKING_COOLDOWN_SECONDS,
        WORKING_DEMOTE_LIMIT, is_machine_tag, HALF_LIFE_DAYS, effective_half_life,
        flush_pressure, now_str, retention_index,
    )
else:
    from ledger_core import (
        AUTO_SINK_LABEL, AUTO_SINK_TYPE_TAG, GRADES, LEVELS, LedgerError, REVIVE_HITS,
        REVIVE_WINDOW_DAYS, StaleHeadError, WAL_CHECKPOINT_COOLDOWN, WORKING_COOLDOWN_SECONDS,
        WORKING_DEMOTE_LIMIT, is_machine_tag, HALF_LIFE_DAYS, effective_half_life,
        flush_pressure, now_str, retention_index,
    )


def time_decay(created_at: Any, half_life_days: float = HALF_LIFE_DAYS,
               access_count: Any = 0, *, _parse_time) -> float:
    """30 天半衰期：越旧的记忆在检索里越轻，但不归零；常被想起的忘得慢。"""
    half = effective_half_life(half_life_days, access_count)
    age_days = max(0.0, (time.time() - _parse_time(created_at)) / 86400.0)
    return 0.5 ** (age_days / max(1e-6, half))


def working_activation(memory: Dict[str, Any], created_seq_now: int,
                       recent_hits: int = 0, *, WORKING_ACCESS_HORIZON, time_decay) -> float:
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


def _migrate_auto_sink_legacy(self) -> Dict[str, int]:
    """N6 存量迁移：给旧格式沉淀补类型标记、摘掉机器标签边。

        1.5.0 期间（468731f..954ef20）的 _auto_sink 只写统一标签
        「自动沉淀」，没有 type:auto_sink 类型标记；旧版 _auto_link
        还把它们当语义概念织了边。打开旧库时在这里补齐，口径与
        is_machine_tag / auto_sink_ids() 同源。

        必须走提交链（写入只能发生在提交里），回放与自检才对得上；
        先查后写，没有存量就一个提交都不建——重复执行不出错。
        只动 tags 与 memory_link，正文与访问痕迹一概不碰。
        """
    legacy: List[Dict[str, str]] = []
    for r in self._query_all(
            "SELECT id, tags FROM memory WHERE tags LIKE ?",
            ("%" + AUTO_SINK_LABEL + "%",)):
        tags = {t.strip() for t in (r["tags"] or "").split(",")}
        if AUTO_SINK_LABEL in tags and AUTO_SINK_TYPE_TAG not in tags:
            legacy.append({"id": r["id"],
                           "tags": "%s,%s" % (r["tags"], AUTO_SINK_TYPE_TAG)})
    edges = [dict(e) for e in self._query_all(
        "SELECT id, from_id, to_id, concept, strength FROM memory_link"
        " WHERE concept LIKE 'type:%' OR concept = ?", (AUTO_SINK_LABEL,))]
    edges = [e for e in edges if is_machine_tag(e["concept"])]
    if not legacy and not edges:
        return {"retyped": 0, "unlinked": 0}
    with self.commit("存量沉淀迁移：补类型标记 %d 条，摘机器标签边 %d 条"
                     % (len(legacy), len(edges))) as c:
        for item in legacy:
            c.retype_memory(item["id"], item["tags"])
        for e in edges:
            c.unlink_edge(e)
    return {"retyped": len(legacy), "unlinked": len(edges)}


# -- WAL 维护 --

def wal_bytes(self) -> int:
    """当前 -wal 文件多大。没有就返回 0。"""
    path = self.db_path + "-wal"
    try:
        return os.path.getsize(path) if os.path.exists(path) else 0
    except OSError:
        return 0

def maybe_checkpoint_wal(self, threshold: Optional[int] = None,
                         cooldown: float = WAL_CHECKPOINT_COOLDOWN, *, log) -> Dict[str, Any]:
    """WAL 超过阈值就截断一次，让 -wal 落回磁盘、别只涨不落。

        挂在已有的维护节流里调用（不新开定时器）。里面有别人在读写时
        TRUNCATE 可能 busy，那不算错：下一轮再试。
        """
    limit = self.wal_checkpoint_bytes if threshold is None else int(threshold)
    size = self.wal_bytes()
    if limit <= 0 or size < limit:
        return {"checkpointed": False, "reason": "wal 未超阈值",
                "wal_bytes": size, "threshold": limit}
    now = time.time()
    if now - self._checkpointed_at < max(0.0, float(cooldown)):
        return {"checkpointed": False, "reason": "还在冷却里",
                "wal_bytes": size, "threshold": limit}
    try:
        with self._lock:
            row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as e:
        log.warning("[记忆账本] WAL 截断失败（不影响读写）: %r", e)
        return {"checkpointed": False, "reason": str(e),
                "wal_bytes": size, "threshold": limit}
    self._checkpointed_at = now
    # PRAGMA 返回 (busy, log_pages, checkpointed_pages)
    busy = int(row[0]) if row else 0
    return {"checkpointed": bool(not busy), "reason": "busy" if busy else "",
            "wal_bytes_before": size, "wal_bytes": self.wal_bytes(),
            "threshold": limit}


# -- 写 --

def touch(self, memory_id: str, query: str = "", how: str = "manual") -> bool:
    """一条记忆被想起了一次。"""
    return self.touch_many([memory_id], query=query, how=how) > 0

def touch_many(self, memory_ids: Iterable[str], query: str = "",
               how: str = "manual", *, log) -> int:
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

def prune_access_log(self, keep_days: int = 180, keep_per_memory: int = 200, *, log) -> int:
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

def plan_working_set(self, capacity: Optional[int] = None, *, working_activation) -> Dict[str, Any]:
    """算一遍工作记忆的换班方案：谁留下、谁被挤走、谁该回来。（只算，不写库）

        现有的一级和「最近又被反复想起」的零级放在同一张榜上排名，
        一起抢 capacity 个名额。一次算清，就不存在先降后升、下轮又降的来回打。
        二级是巩固层，压根不参加这场竞争。
        N9：沉淀条目是原文索引不是普通事实，不参加这场竞争——否则它
        按 importance=0.45 的普通身段抢座，把真事实挤到零级，冲刷时
        再被标成 delete，一路连错。
        """
    cap = self.working_capacity if capacity is None else int(capacity)
    now_seq = self.max_created_seq()
    hits = self._recent_hits() if cap > 0 else {}
    sink_ids = self.auto_sink_ids()
    rows = [dict(r) for r in self._query_all("SELECT * FROM memory")]
    rows = [r for r in rows if r["id"] not in sink_ids]
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

def maybe_rebalance_working_set(self, cooldown: float = WORKING_COOLDOWN_SECONDS, *, log) -> Dict[str, Any]:
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
    sink_ids = self.auto_sink_ids()
    rows = [dict(r) for r in self._query_all("SELECT * FROM memory ORDER BY created_seq")]
    # 冲刷量的基准序号取「普通记忆」里最新的 created_seq（N9）：
    # 沉淀条目是索引不是事实，它写入推进的序号不该变成真事实的冲刷
    # 压力——否则每写一条沉淀，所有普通记忆的冲刷量同步涨一格。
    now_seq = max((int(r["created_seq"]) for r in rows
                   if r["id"] not in sink_ids), default=0)
    plan = []
    for r in rows:
        if r["id"] in sink_ids:
            # 沉淀是索引不是普通事实：不进冲刷计划、不参与计量（N9）
            continue
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
        # v5 · 职守观测：否认、提案、原文锚、状态档条数（只读统计）
        "denials": self._query_one("SELECT COUNT(*) AS n FROM user_denial")["n"],
        "proposals": self._query_one("SELECT COUNT(*) AS n FROM dream_proposal")["n"],
        "anchors": self._query_one("SELECT COUNT(*) AS n FROM memory_source_anchor")["n"],
        "status_marks": self._query_one("SELECT COUNT(*) AS n FROM memory_status")["n"],
    }

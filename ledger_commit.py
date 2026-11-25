from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

if __package__:
    from .ledger_core import (
        ATTITUDE_OWNER_ID, LEVELS, LedgerError, MAX_GRADE, PERSONA_CORE_HASH_KEY,
        PROFILE_VISIBILITIES, is_machine_tag, _clamp_unit, now_str,
    )
else:
    from ledger_core import (
        ATTITUDE_OWNER_ID, LEVELS, LedgerError, MAX_GRADE, PERSONA_CORE_HASH_KEY,
        PROFILE_VISIBILITIES, is_machine_tag, _clamp_unit, now_str,
    )


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
    # persona_no 照 max_created_seq 先例兜底：老库迁移、v4 旧备份导入
    # 可能带进 p 编号大于 meta 计数器的存量行，只读 meta 会撞 UNIQUE。
    self._persona_no = max(c["persona_no"], ledger.max_persona_no())
    self._sealed = False
    self._clue_cache: Dict[str, str] = {}
    # 事务钩子（v5）：封链时在同一个事务里执行，梦产出用它把
    # dream_proposal 登记和 memory 插入绑成原子——不许出现
    # 「记忆写进去了、梦的登记没了」（三道锁靠这张表认梦产出）。
    self._extra_tx = None  # Optional[Callable[[sqlite3.Connection], None]]


def _find_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
    for r in reversed(self.rows):
        if r["tbl"] == "memory" and r["where"].get("id") == memory_id:
            return r["after"]
    row = self.ledger._query_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
    return dict(row) if row else None

def _find_persona(self, rule_id: str) -> Optional[Dict[str, Any]]:
    for r in reversed(self.rows):
        if (r["tbl"] == "core_rule" and r["where"].get("id") == rule_id
                and r["op"] != "delete"):
            return r["after"]
    row = self.ledger._query_one("SELECT * FROM core_rule WHERE id = ?", (rule_id,))
    return dict(row) if row else None

def _touch_persona_hash_tx(self) -> None:
    """注册封链事务钩子：core 人格行落库后重算哨兵哈希写 meta。

        只在 core 行写路径（播种）上挂；钩子链式接住已有的 _extra_tx
        （梦产出），互不覆盖。手工 SQL 绕过提交链改库不会走这里——
        那正是哨兵要抓的对象，抓到只报警。
        """
    prev = self._extra_tx

    def hook(cx: sqlite3.Connection) -> None:
        if prev is not None:
            prev(cx)
        rows = [dict(r) for r in cx.execute(
            "SELECT id, content, priority FROM core_rule"
            " WHERE kind = 'core' ORDER BY id").fetchall()]
        digest = _compute_persona_core_hash(rows)
        cx.execute("INSERT INTO meta(k, v) VALUES(?, ?)"
                   " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                   (PERSONA_CORE_HASH_KEY, digest))

    self._extra_tx = hook

# -- 人格分区（v7）--

def add_persona(self, kind: str, content: str, priority: int = 0,
                source: str = "self", _allow_core: bool = False) -> str:
    """加一条人格设定，返回行 id。core 分区只读，唯一例外走 _allow_core。

        自动写入守卫：restricted_auto_write 范围内一律拒绝；
        另需 user_persona_write 通行令牌（_allow_core 也不例外）——
        令牌只有用户显式路径会开，播种由 _seed_persona_core 自己开。
        """
    self.ledger._require_persona_write_auth("add_persona")
    kind = (kind or "").strip()
    if not kind:
        raise LedgerError("persona kind 不能为空")
    if len(kind) > 32:
        raise LedgerError("persona kind 超过 32 字上限")
    if kind == "core" and not _allow_core:
        raise LedgerError(
            "kind='core' 是只读基础设定，不许经 add_persona 写入；"
            "显式播种请走 _seed_persona_core")
    content = (content or "").strip()
    if not content:
        raise LedgerError("人格内容不能为空")
    if len(content) > 200:
        raise LedgerError("单条人格超过 200 字上限")
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        raise LedgerError("priority 必须是整数，收到：%r" % (priority,))
    priority = max(0, min(1000, priority))
    source = (source or "").strip() or "self"
    if len(source) > 64:
        raise LedgerError("persona source 超过 64 字上限")
    self._persona_no += 1
    pid = "p%d" % self._persona_no
    ts = now_str()
    row = {"id": pid, "kind": kind, "content": content,
           "priority": priority, "source": source,
           "created_at": ts, "updated_at": ts}
    self._push("core_rule", "insert", {"id": pid}, None, row)
    if kind == "core":
        self._touch_persona_hash_tx()
    return pid

def update_persona(self, rule_id: str, content: Optional[str] = None,
                   priority: Optional[int] = None) -> bool:
    """改一条 self 分区人格。core 分区一律拒绝，没有旁路。"""
    self.ledger._require_persona_write_auth("update_persona")
    snap = self._find_persona(rule_id)
    if snap is None:
        return False
    if snap.get("kind") == "core":
        raise LedgerError(
            "人格行 %s 是 kind='core' 只读基础设定，update_persona 拒绝" % rule_id)
    after = dict(snap)
    before: Dict[str, Any] = {}
    changed = False
    if content is not None:
        content = (content or "").strip()
        if not content:
            raise LedgerError("人格内容不能为空")
        if len(content) > 200:
            raise LedgerError("单条人格超过 200 字上限")
        if content != snap["content"]:
            after["content"] = content
            before["content"] = snap["content"]
            changed = True
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise LedgerError("priority 必须是整数，收到：%r" % (priority,))
        priority = max(0, min(1000, priority))
        if priority != int(snap["priority"]):
            after["priority"] = priority
            before["priority"] = snap["priority"]
            changed = True
    if not changed:
        return False
    after["updated_at"] = now_str()
    self._push("core_rule", "update", {"id": rule_id}, before, after)
    return True

def remove_persona(self, rule_id: str) -> bool:
    """删一条 self 分区人格。core 分区一律拒绝，没有旁路。"""
    self.ledger._require_persona_write_auth("remove_persona")
    snap = self._find_persona(rule_id)
    if snap is None:
        return False
    if snap.get("kind") == "core":
        raise LedgerError(
            "人格行 %s 是 kind='core' 只读基础设定，remove_persona 拒绝" % rule_id)
    self._push("core_rule", "delete", {"id": rule_id}, dict(snap), None)
    return True

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
        "importance": round(_clamp_unit(importance), 4),
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
    """标签就是概念：和历史上共用同一概念的节点自动织网。

        N2：机器/管理标签（type: 前缀类型标记、「自动沉淀」统一标签）不是
        语义概念，在这里拦下不许当 concept——见 is_machine_tag。识别和
        检索仍走 tags（auto_sink_ids / recall）。
        N10：候选邻居改精确判定——子串 LIKE 只用来缩小扫描范围，命中后
        按逗号分隔的完整标签集合确认 tag 确实在其中，且目标条目自身
        不带机器标签；否则「自动」「沉淀」这类普通标签的子串命中会把
        普通记忆连到沉淀上，还吃掉名额。ORDER BY created_seq DESC 让
        候选顺序确定（新写的先连），凑够 8 条即停。
        存量库里已织出的这类边由打开库时的 _migrate_auto_sink_legacy()
        摘除（N6）。
        """
    for tag in tag_list:
        if is_machine_tag(tag):
            continue
        linked = 0
        for h in self.ledger._query_all(
                "SELECT id, tags FROM memory WHERE id != ? AND tags LIKE ?"
                " ORDER BY created_seq DESC LIMIT 32",
                (mid, "%" + tag + "%")):
            tags = {t.strip() for t in (h["tags"] or "").split(",")}
            if tag not in tags or any(is_machine_tag(t) for t in tags):
                continue
            nid = h["id"]
            if self.ledger._query_one(
                    "SELECT 1 FROM memory_link WHERE (from_id=? AND to_id=?)"
                    " OR (from_id=? AND to_id=?)", (mid, nid, nid, mid)):
                continue
            self._push("memory_link", "insert",
                       {"from_id": mid, "to_id": nid, "concept": tag}, None,
                       {"from_id": mid, "to_id": nid, "concept": tag,
                        "strength": 1.0, "created_at": now_str()})
            linked += 1
            if linked >= 8:
                break

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

def retype_memory(self, memory_id: str, new_tags: str) -> bool:
    """改一条记忆的 tags（N6 存量迁移用，走正常 update 行）。

        迁移只能走提交链（写入必须发生在提交里），所以暴露成 builder
        方法而不是 Ledger 上的裸 SQL。
        """
    snap = self._find_memory(memory_id)
    if snap is None:
        return False
    if (snap.get("tags") or "") == (new_tags or ""):
        return False
    after = dict(snap)
    after["tags"] = new_tags
    after["updated_at"] = now_str()
    self._push("memory", "update", {"id": memory_id},
               {"tags": snap.get("tags") or ""}, after)
    return True

def unlink_edge(self, link: Dict[str, Any]) -> None:
    """按行的完整身份（from|to|concept）删一条 concept 边（N6 迁移用）。

        删除行的 row_key 与 before 都带完整字段，重放时认得出删的是哪条（R4）。
        """
    self._push("memory_link", "delete",
               {"from_id": link["from_id"], "to_id": link["to_id"],
                "concept": link["concept"]},
               {"id": link["id"], "from_id": link["from_id"],
                "to_id": link["to_id"], "concept": link["concept"],
                "strength": link["strength"]}, None)

def forget(self, memory_id: str) -> bool:
    snap = self._find_memory(memory_id)
    if snap is None:
        return False
    for rel in self.ledger._query_all(
            "SELECT * FROM memory_clue WHERE memory_id = ?", (memory_id,)):
        # 删除行必须自带完整行身份（where + before 全字段）：
        # 重放时先解 row_key，before 里缺了身份也能对上号（R4）
        self._push("memory_clue", "delete",
                   {"memory_id": memory_id, "clue_id": rel["clue_id"]},
                   {"memory_id": memory_id, "clue_id": rel["clue_id"],
                    "weight": rel["weight"], "note": rel["note"]}, None)
    for link in self.ledger._query_all(
            "SELECT * FROM memory_link WHERE from_id = ? OR to_id = ?",
            (memory_id, memory_id)):
        # 行主键是 from|to|concept（见 PROJECTIONS），where 必须用它，
        # 不能只写 id——否则重放解 row_key 时认不出删的是哪条边（R4）
        self._push("memory_link", "delete",
                   {"from_id": link["from_id"], "to_id": link["to_id"],
                    "concept": link["concept"]},
                   {"id": link["id"], "from_id": link["from_id"],
                    "to_id": link["to_id"], "concept": link["concept"],
                    "strength": link["strength"]}, None)
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

# -- 群聊支持（v8）--

def _find_v8_row(self, tbl: str, where: Dict[str, Any]
                 ) -> Optional[Dict[str, Any]]:
    """本提交或库里找一行 v8 投影行：先扫本提交，再查库（persona 先例）。"""
    for r in reversed(self.rows):
        if (r["tbl"] == tbl and r["op"] != "delete"
                and all(r["where"].get(k) == v for k, v in where.items())):
            return r["after"]
    conds = " AND ".join("%s = ?" % k for k in where)
    row = self.ledger._query_one(
        "SELECT * FROM %s WHERE %s" % (tbl, conds), tuple(where.values()))
    return dict(row) if row else None

def add_speaker(self, memory_id: str, speaker_id: str,
                speaker_name: str = "") -> Optional[str]:
    """记下一条记忆的说话人（B2）。空 speaker_id 不写行，返回 None。

        同一 (memory_id, speaker_id) 已有行就更新名字（upsert），不会重复。
        speaker_name 只是显示名，超 64 字截断；speaker_id 超长是调用方 bug，
        显式失败。进提交链：删了记忆也能从历史里翻出「当时谁说的」。
        """
    if not memory_id:
        raise LedgerError("add_speaker 需要记忆 id")
    speaker_id = (speaker_id or "").strip()
    if not speaker_id:
        return None
    if len(speaker_id) > 64:
        raise LedgerError("speaker_id 超过 64 字上限")
    speaker_name = " ".join(str(speaker_name or "").split())[:64]
    where = {"memory_id": memory_id, "speaker_id": speaker_id}
    ts = now_str()
    snap = self._find_v8_row("memory_speaker", where)
    if snap:
        after = dict(snap)
        after["speaker_name"] = speaker_name
        after["created_at"] = after.get("created_at") or ts
        self._push("memory_speaker", "update", dict(where),
                   {"speaker_name": snap.get("speaker_name", ""),
                    "created_at": after["created_at"]}, after)
        return speaker_id
    self._push("memory_speaker", "insert", dict(where), None,
               {"memory_id": memory_id, "speaker_id": speaker_id,
                "speaker_name": speaker_name, "created_at": ts})
    return speaker_id

def _find_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
    """本提交或库里找一行画像（visibility 同提交落地时查库查不到）。"""
    for r in reversed(self.rows):
        if (r["tbl"] == "profile" and r["op"] != "delete"
                and r["where"].get("id") == profile_id):
            return r["after"]
    row = self.ledger._query_one(
        "SELECT * FROM profile WHERE id = ?", (profile_id,))
    return dict(row) if row else None

def set_profile_visibility(self, profile_id: str, visibility: str) -> str:
    """设置一条画像事实的可见性（B3）。upsert；画像行必须存在。"""
    visibility = (visibility or "").strip()
    if visibility not in PROFILE_VISIBILITIES:
        raise LedgerError(
            "visibility 只支持 private/public，收到：%r" % (visibility,))
    if not profile_id or self._find_profile(profile_id) is None:
        raise LedgerError("没有这条画像：%s" % profile_id)
    where = {"profile_id": profile_id}
    ts = now_str()
    snap = self._find_v8_row("profile_visibility", where)
    if snap:
        after = dict(snap)
        after["visibility"] = visibility
        after["updated_at"] = ts
        self._push("profile_visibility", "update", dict(where),
                   {"visibility": snap.get("visibility", "private"),
                    "updated_at": snap.get("updated_at", "")}, after)
    else:
        self._push("profile_visibility", "insert", dict(where), None,
                   {"profile_id": profile_id, "visibility": visibility,
                    "updated_at": ts})
    return visibility

def set_attitude(self, user_id: str, level: int, reason: str = "") -> int:
    """设置一个人的坏感度等级（B4）。0 正常、4 最低；owner 锁死。"""
    user_id = (user_id or "").strip()
    if not user_id:
        raise LedgerError("user_id 不能为空")
    if len(user_id) > 64:
        raise LedgerError("user_id 超过 64 字上限")
    if user_id == ATTITUDE_OWNER_ID:
        raise LedgerError(
            "主人（%s）的坏感度严禁修改：set_attitude 拒绝" % ATTITUDE_OWNER_ID)
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise LedgerError("坏感度等级必须是 0-4 的整数，收到：%r" % (level,))
    if not 0 <= level <= 4:
        raise LedgerError("坏感度等级只能是 0-4，收到：%d" % level)
    reason = " ".join(str(reason or "").split())[:200]
    where = {"user_id": user_id}
    ts = now_str()
    snap = self._find_v8_row("user_attitude", where)
    if snap:
        after = dict(snap)
        after["level"] = level
        after["reason"] = reason
        after["updated_at"] = ts
        self._push("user_attitude", "update", dict(where),
                   {"level": int(snap.get("level", 0)),
                    "reason": snap.get("reason", ""),
                    "updated_at": snap.get("updated_at", "")}, after)
    else:
        self._push("user_attitude", "insert", dict(where), None,
                   {"user_id": user_id, "level": level, "reason": reason,
                    "updated_at": ts})
    return level

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


def _compute_persona_core_hash(rows: List[Dict[str, Any]]) -> str:
    """core 人格行的哨兵哈希。空集返回空串（=从未播种，不算篡改）。"""
    if not rows:
        return ""
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: str(r.get("id"))):
        h.update(("%s\x1f%s\x1f%s\x1e" % (
            r.get("id"), r.get("content"),
            int(r.get("priority") or 0))).encode("utf-8"))
    return h.hexdigest()


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
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        raise LedgerError("budget 必须是整数，收到：%r" % (budget,))
    if budget < 0:
        raise LedgerError("budget 不能为负数：%d" % budget)
    if not rows:
        return ""
    lines: List[str] = []
    used = 0
    for r in rows:
        line = "- %s" % (r["content"],)
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    text = "\n".join(lines)
    missed = len(rows) - len(lines)
    if missed:
        text = (text + "\n（另有 %d 条未注入）" % missed) if text else (
            "（另有 %d 条未注入）" % missed)
    return text

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

if __package__:
    from .ledger_core import BASELINE_COMMIT, LedgerError, PROJECTIONS, StaleHeadError, now_str
else:
    from ledger_core import BASELINE_COMMIT, LedgerError, PROJECTIONS, StaleHeadError, now_str


def _row_key_fields(row_key: str) -> Dict[str, str]:
    """把 commit_row.row_key（"k=v|k=v"）解回行身份字段。

    删除事件的 after 恒为空，旧数据的 before 快照又可能缺身份字段
    （R4：老版 forget 只存了 weight/note），row_key 是唯一可靠的行身份来源。
    解析不出来就返回空 dict，让调用方退回 before。基线行的 row_key 带
    "baseline:" 前缀且 op 恒为 insert，不进这条路径。
    """
    out: Dict[str, str] = {}
    for kv in (row_key or "").split("|"):
        if "=" in kv:
            k, _, v = kv.partition("=")
            if k:
                out[k] = v
    return out


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
        self._set_meta(cx, "persona_no", builder._persona_no)
        self._set_meta(cx, "created_seq", builder._gseq)
        self._set_meta(cx, "head", cid)
        if builder._extra_tx is not None:
            builder._extra_tx(cx)
        builder._sealed = True
    self._maybe_auto_compact()
    return cid

def _maybe_auto_compact(self, *, log) -> Optional[Dict[str, Any]]:
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
                # 删除事件的行身份：先解 row_key（k=v|k=v），再用 before 补
                # 其余字段。老数据 before 只存了部分字段（R4），光靠它会
                # 把 key 算成 None 拼接，pop 不掉真实行、回放里出幽灵。
                snap: Dict[str, Any] = dict(_row_key_fields(r["row_key"]))
                if before:
                    snap.update(before)
                key = key_of(snap)
                if key is not None:
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


# -- 创作项目（creative，v6 旁路表：不进提交链、不碰记忆检索）--

def get_creative(self, creative_id: str) -> Optional[Dict[str, Any]]:
    """按 id 读一条创作项目（含正文 body 与原始 payload）。没有返回 None。"""
    row = self._query_one(
        "SELECT * FROM creative WHERE id = ?", ((creative_id or "").strip(),))
    return dict(row) if row else None

def list_creatives(self) -> List[Dict[str, Any]]:
    """列全部创作项目，只带轻量字段（id/title/work_type/status/
        current_chars），不带正文与 payload——列表只看门牌，不搬家具。"""
    rows = self._query_all(
        "SELECT id, title, work_type, status, current_chars"
        " FROM creative ORDER BY created_at, id")
    return [dict(r) for r in rows]

def upsert_creative(self, creative_id: str, title: str,
                    work_type: str = "", status: str = "", premise: str = "",
                    outline: Any = None, body: str = "", payload: Any = None,
                    current_chars: int = 0, target_chars: int = 0,
                    created_at: str = "", updated_at: str = "") -> Dict[str, Any]:
    """写入/覆盖一条创作项目（唯一写接口，v6）。

        id 已存在 → 整字段覆盖，但 created_at / created_seq 保留第一次的
        （出生时间不随改名变化）；不存在 → 插入。outline / payload 收
        任意可序列化对象，存 json.dumps(ensure_ascii=False)；序列化失败
        显式抛 LedgerError，不静默写半截。

        照 memory_status 旁路先例：直接事务写库，不进提交链（它不是
        记忆内容的变更，是另一条业务线的归档）；也不触碰 memory 表、
        线索表或任何检索打分。
        """
    cid = (creative_id or "").strip()
    title = (title or "").strip()
    if not cid:
        raise LedgerError("创作项目 id 不能为空")
    if not title:
        raise LedgerError("创作项目标题不能为空：%s" % cid)
    try:
        outline_json = json.dumps("" if outline is None else outline,
                                   ensure_ascii=False)
        payload_json = json.dumps({} if payload is None else payload,
                                   ensure_ascii=False)
    except Exception as e:
        raise LedgerError(
            "创作项目 %s 的 outline/payload 无法序列化为 JSON：%r" % (cid, e))
    at = now_str()
    cols = {
        "title": title,
        "work_type": (work_type or "").strip(),
        "status": (status or "").strip(),
        "premise": premise or "",
        "outline": outline_json,
        "body": body or "",
        "payload": payload_json,
        "current_chars": max(0, int(current_chars or 0)),
        "target_chars": max(0, int(target_chars or 0)),
        "updated_at": updated_at or at,
    }
    with self._tx() as cx:
        old = cx.execute("SELECT * FROM creative WHERE id = ?",
                         (cid,)).fetchone()
        if old is None:
            seq = cx.execute(
                "SELECT COALESCE(MAX(created_seq), 0) + 1 AS n"
                " FROM creative").fetchone()["n"]
            cx.execute(
                "INSERT INTO creative(id, title, work_type, status, premise,"
                " outline, body, payload, current_chars, target_chars,"
                " created_at, updated_at, created_seq)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, cols["title"], cols["work_type"], cols["status"],
                 cols["premise"], cols["outline"], cols["body"],
                 cols["payload"], cols["current_chars"], cols["target_chars"],
                 created_at or at, cols["updated_at"], int(seq)))
            action = "insert"
        else:
            cx.execute(
                "UPDATE creative SET title=?, work_type=?, status=?,"
                " premise=?, outline=?, body=?, payload=?, current_chars=?,"
                " target_chars=?, updated_at=? WHERE id=?",
                (cols["title"], cols["work_type"], cols["status"],
                 cols["premise"], cols["outline"], cols["body"],
                 cols["payload"], cols["current_chars"], cols["target_chars"],
                 cols["updated_at"], cid))
            action = "update"
    return {"id": cid, "action": action, "updated_at": cols["updated_at"]}

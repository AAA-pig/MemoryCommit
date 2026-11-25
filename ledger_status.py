from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

if __package__:
    from .ledger_core import (
        AUTO_SINK_LABEL, AUTO_SINK_MARKERS, AUTO_SINK_TYPE_TAG, CONFLICT_PERSPECTIVE,
        CONFLICT_SCENE, CONFLICT_TEMPORAL, DENY_REVOKE_PREFIX, DENY_STICKY_STATUS, LedgerError,
        MEMORY_STATUSES, now_str, _bm25_scores,
    )
else:
    from ledger_core import (
        AUTO_SINK_LABEL, AUTO_SINK_MARKERS, AUTO_SINK_TYPE_TAG, CONFLICT_PERSPECTIVE,
        CONFLICT_SCENE, CONFLICT_TEMPORAL, DENY_REVOKE_PREFIX, DENY_STICKY_STATUS, LedgerError,
        MEMORY_STATUSES, now_str, _bm25_scores,
    )


# ------------------------------------------------------------------
# v5 · 职守：否认信号 / 状态档 / 原文锚 / 咬合点 / 梦产出
#
# memory 对梦只露两个咬合点（v5 §1.3）：propose_clues 出候选、
# trace_to_source 接原文。梦要连的线、要提的名，全部走内部写路径
# （dream_proposal / memory_link），不另开对外的口。
# ------------------------------------------------------------------

def _new_proposal_id() -> str:
    return "dp-%s-%s" % (time.strftime("%Y%m%d%H%M%S"), os.urandom(3).hex())

def _latest_status_map(self) -> Dict[str, str]:
    """全库最新状态档：memory_id -> 最新 status。append-only，取最新一条为准。

        例外：disputed 是**粘性**状态。用户否认一旦写进去，自动写入
        （矛盾分类、梦境回写）不得覆盖它，只有用户显式撤销（deny_revoke）才解除。
        理由：老大说一句「你记错了」权重最高、立刻生效，不能被随后一次
        自动分类冲掉——实测过：deny 之后跑一次 classify_pair，disputed 就没了。
        """
    latest: Dict[str, str] = {}
    sticky: set = set()
    for r in self._query_all(
            "SELECT memory_id, status, reason FROM memory_status ORDER BY id"):
        mid, st = r["memory_id"], r["status"]
        if st == DENY_STICKY_STATUS:
            sticky.add(mid)
        elif str(r["reason"] or "").startswith(DENY_REVOKE_PREFIX):
            sticky.discard(mid)
        latest[mid] = st
    for mid in sticky:
        latest[mid] = DENY_STICKY_STATUS
    return latest

def _coexist_cond_json(self, memory_id: str) -> Any:
    """这条记忆最新一条 coexist 条件原文（没有就空串）。"""
    row = self._query_one(
        "SELECT cond_json FROM memory_condition WHERE memory_id = ?"
        " ORDER BY created_at DESC LIMIT 1", (memory_id,))
    return row["cond_json"] if row else ""

def disputed_ids(self) -> set:
    """disputed（被用户否认）的记忆集合：R3 语义里它是唯一
        「永远不主动提」的档，不吃条件豁免。"""
    return {m for m, s in self._latest_status_map().items() if s == "disputed"}

def auto_sink_ids(self) -> set:
    """带自动沉淀机器标记的记忆集合（N1/N6）。

        识别口径与建图（is_machine_tag）同源：tags 里有完整标签
        type:auto_sink 或「自动沉淀」任一个就算沉淀。1.5.0 期间
        （468731f..954ef20）写出的存量条目只有「自动沉淀」标签，
        不能指望每条都补过迁移，两个标记都得认。
        SQL 先按子串缩小范围，Python 再按逗号分隔的完整标签精确判定，
        不靠 LIKE 子串猜，免得「type:auto_sink_x」这类前缀撞车。
        """
    out: set = set()
    for r in self._query_all(
            "SELECT id, tags FROM memory WHERE tags LIKE ? OR tags LIKE ?",
            ("%" + AUTO_SINK_TYPE_TAG + "%", "%" + AUTO_SINK_LABEL + "%")):
        tags = {t.strip() for t in (r["tags"] or "").split(",")}
        if tags & AUTO_SINK_MARKERS:
            out.add(r["id"])
    return out

def denied_ids(self) -> set:
    """被用户否认（最新档 disputed）的记忆集合。disputed_ids 的旧名。"""
    return self.disputed_ids()

def quiet_ids(self, context: Optional[dict] = None, *, condition_allows) -> set:
    """不主动浮现的记忆集合。

        disputed 永远拦；coexist 只在「给了语境且条件判定为匹配」时放行。
        context=None（默认）是兼容旧调用的薄封装：没有语境就判不出条件，
        coexist 全部拦（保守）。条件怎么判见 condition_allows()。
        """
    status_map = self._latest_status_map()
    out = {m for m, s in status_map.items() if s == "disputed"}
    coexist = {m for m, s in status_map.items() if s == "coexist"}
    if not coexist:
        return out
    if context is None:
        return out | coexist
    return out | {m for m in coexist
                  if not condition_allows(self._coexist_cond_json(m), context)}


# -- 用户否认（权重最高）--

def deny_memory(self, memory_id: str, said: str,
                message_seq: int = 0) -> Dict[str, Any]:
    """用户否认一条记忆：写 user_denial，同时状态置 disputed。

        「老大说一句『你记错了』」权重最高、立刻生效、不等梦跑：
        立刻降检索权重（DENIED_RECALL_WEIGHT）、立刻停止主动提起。
        不删除：否认不等于删，原文还在。
        """
    memory_id = (memory_id or "").strip()
    said = (said or "").strip()
    if not memory_id:
        raise LedgerError("否认要指到具体记忆（memory_id）")
    if not said:
        raise LedgerError("否认要留一句话（said），空话不记")
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    at = now_str()
    with self._tx() as cx:
        cx.execute("INSERT INTO user_denial(memory_id, said, message_seq, at)"
                   " VALUES(?,?,?,?)",
                   (memory_id, said[:400], int(message_seq or 0), at))
        cx.execute("INSERT INTO memory_status(memory_id, status, reason,"
                   " evidence_ref, at) VALUES(?,?,?,?,?)",
                   (memory_id, "disputed", "用户否认：" + said[:120], "", at))
    return {"memory_id": memory_id, "status": "disputed", "at": at}

def deny_revoke(self, memory_id: str, why: str = "") -> Dict[str, Any]:
    """撤销一次用户否认（只有用户能撤销，自动写入不许动 disputed）。

        重新记一条「用户撤销否认」，并把状态放回 active——
        _latest_status_map 见到 DENY_REVOKE_PREFIX 才肯解开粘性。
        """
    memory_id = (memory_id or "").strip()
    if not memory_id:
        raise LedgerError("撤销要指到具体记忆（memory_id）")
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    at = now_str()
    reason = DENY_REVOKE_PREFIX + ("：" + why[:120] if why else "")
    with self._tx() as cx:
        cx.execute("INSERT INTO memory_status(memory_id, status, reason,"
                   " evidence_ref, at) VALUES(?,?,?,?,?)",
                   (memory_id, "active", reason, "", at))
    return {"memory_id": memory_id, "status": "active", "at": at}

def queue_denial_suspect(self, said: str, keyword: str = "",
                         memory_id: str = "") -> Dict[str, str]:
    """否认关键词命中 → 只进待确认队列，不自动改状态（v5 §5.1）。

        队列就是 dream_proposal 里 kind='suspect'、status='pending' 的行：
        等人（或后续的原文对账）确认，谁也不许顺手判成错。
        """
    said = (said or "").strip()
    if not said:
        raise LedgerError("待确认线索要留原话（said）")
    pid = self._new_proposal_id()
    payload = json.dumps({"said": said[:400], "keyword": keyword or "",
                          "memory_id": memory_id or ""}, ensure_ascii=False)
    with self._tx() as cx:
        cx.execute("INSERT INTO dream_proposal(id, kind, payload_json, basis_ids,"
                   " provenance, status, created_at)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (pid, "suspect", payload, memory_id or "",
                    "keyword", "pending", now_str()))
    return {"proposal_id": pid, "kind": "suspect", "status": "pending"}

def pending_denial_queue(self, limit: int = 20) -> List[Dict[str, Any]]:
    """待确认队列：关键词命中攒下的嫌疑线索，等人核。"""
    rows = self._query_all(
        "SELECT * FROM dream_proposal WHERE kind = 'suspect' AND status = 'pending'"
        " ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),))
    return [dict(r) for r in rows]

def denial_history(self, memory_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """一条记忆被否认过几次、原话是什么。"""
    rows = self._query_all(
        "SELECT * FROM user_denial WHERE memory_id = ? ORDER BY id DESC LIMIT ?",
        (memory_id, max(1, int(limit))))
    return [dict(r) for r in rows]

# -- 状态档（append-only，取最新一条为准）--

def set_memory_status(self, memory_id: str, status: str,
                      reason: str = "", evidence_ref: str = "") -> Dict[str, Any]:
    """追加一档状态。不删旧行：状态是档案，不是开关。"""
    status = (status or "").strip()
    if status not in MEMORY_STATUSES:
        raise LedgerError("非法状态 %r，可用：%s" % (status, "/".join(MEMORY_STATUSES)))
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    at = now_str()
    with self._tx() as cx:
        cx.execute("INSERT INTO memory_status(memory_id, status, reason,"
                   " evidence_ref, at) VALUES(?,?,?,?,?)",
                   (memory_id, status, (reason or "")[:300],
                    (evidence_ref or "")[:120], at))
    return {"memory_id": memory_id, "status": status, "at": at}

def current_status(self, memory_id: str) -> str:
    """这条记忆此刻的状态。没有档案就当 active——不编历史。"""
    row = self._query_one(
        "SELECT status FROM memory_status WHERE memory_id = ?"
        " ORDER BY id DESC LIMIT 1", (memory_id,))
    return row["status"] if row else "active"

def status_timeline(self, memory_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    rows = self._query_all(
        "SELECT * FROM memory_status WHERE memory_id = ? ORDER BY id DESC LIMIT ?",
        (memory_id, max(1, int(limit))))
    return [dict(r) for r in rows]

# -- 增量原文锚（只对新记忆生效；存量 526 条中转站记忆不编原文，只标疑）--

def anchor_memory(self, memory_id: str, message_seq: int,
                  digest: str = "") -> Dict[str, Any]:
    """把一条记忆锚回消息链位置。锚是路标，不是原文本身：没有就不许编。"""
    message_seq = int(message_seq or 0)
    if message_seq <= 0:
        raise LedgerError("锚要指向消息链的真实序号（message_seq > 0）")
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    at = now_str()
    with self._tx() as cx:
        cx.execute("INSERT OR IGNORE INTO memory_source_anchor(memory_id,"
                   " message_seq, digest, at) VALUES(?,?,?,?)",
                   (memory_id, message_seq, (digest or "")[:64], at))
    return {"memory_id": memory_id, "message_seq": message_seq}


def trace_to_source(self, memory_id: str) -> List[Dict[str, Any]]:
    """咬合点 2：这条记忆接回的那句原文。没有锚就返回空，不许编。"""
    rows = self._query_all(
        "SELECT m.seq, m.id, m.role, m.content, m.at, m.digest"
        " FROM memory_source_anchor a JOIN message m ON m.seq = a.message_seq"
        " WHERE a.memory_id = ? ORDER BY a.message_seq", (memory_id,))
    return [dict(r) for r in rows]

# -- 矛盾四分类（只分类，不判决）--

def classify_pair(self, a_id: str, b_id: str,
                  a_perspective: str = "", b_perspective: str = "", *, classify_conflict) -> Dict[str, Any]:
    """取两条打架的记忆 + 各自 condition，四分类后挂状态。

        前三类（时变/场景/视角）挂 coexist——都留，都是真的；
        真互斥候选与条件不足挂 conflicted——等原文对账，不许顺手判错。
        永不写 corrected：corrected 只能走「梦提名 → 原文对账 → 追加更正条目」。
        """
    a = self._query_one("SELECT * FROM memory WHERE id = ?", (a_id,))
    b = self._query_one("SELECT * FROM memory WHERE id = ?", (b_id,))
    if not a or not b:
        raise LedgerError("两条记忆都要存在才能分类：%s / %s" % (a_id, b_id))

    def _cond(mid: str) -> str:
        row = self._query_one(
            "SELECT cond_json FROM memory_condition WHERE memory_id = ?"
            " ORDER BY created_at DESC LIMIT 1", (mid,))
        return row["cond_json"] if row else ""

    kind = classify_conflict(a["summary"], b["summary"], _cond(a_id), _cond(b_id),
                             a_perspective, b_perspective)
    status = "coexist" if kind in (CONFLICT_TEMPORAL, CONFLICT_SCENE,
                                   CONFLICT_PERSPECTIVE) else "conflicted"
    updates = [
        {"memory_id": a_id, "status": status,
         "reason": "矛盾四分类：%s（与 %s 对照）" % (kind, b_id),
         "evidence_ref": b_id},
        {"memory_id": b_id, "status": status,
         "reason": "矛盾四分类：%s（与 %s 对照）" % (kind, a_id),
         "evidence_ref": a_id},
    ]
    self.dream_apply([], updates)
    return {"kind": kind, "status": status, "a": a_id, "b": b_id}

def set_memory_condition(self, memory_id: str, cond_json: str,
                         cond_note: str = "") -> Dict[str, Any]:
    """补条件标签（场景/时间窗/环境）。允许为空；为空的矛盾对一律挂 conflicted。

        工具来了（能记温度、天气、地点的那种）也不补历史：当时没记的条件，
        事后补进去的只是猜测，拿猜测去判旧的矛盾等于再冤枉一次。
        只对新发生的生效。
        """
    cond_json = (cond_json or "").strip()
    if not cond_json:
        raise LedgerError("条件不能为空串；没有条件就不建档")
    try:
        parsed = json.loads(cond_json)
    except Exception:
        raise LedgerError("条件必须是合法 JSON：cond_json")
    if not isinstance(parsed, dict):
        raise LedgerError("条件必须是 JSON 对象")
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    at = now_str()
    with self._tx() as cx:
        cx.execute("INSERT OR IGNORE INTO memory_condition(memory_id, cond_json,"
                   " cond_note, created_at) VALUES(?,?,?,?)",
                   (memory_id, cond_json, (cond_note or "")[:200], at))
    return {"memory_id": memory_id, "cond_json": cond_json}


# -- 原文链：一条消息一个节点，改一个字整条就对不上 --

def append_message(self, role: str, content: str,
                   session_id: str = "", at: str = "") -> Dict[str, Any]:
    """往原文链尾部接一条消息，返回新节点。

        不进提交链是有意的：一天几百条消息塞进 mcommit，
        「记忆的历史」会被稀释成流水账。这里要的是篡改检测与内部
        一致性校验（改一个字、删中间一条、砍尾巴都查得出）、查得到
        原话，不是能回滚，所以用哈希链——digest 吃上一条的 digest。
        它不防重算整条链（能同时改消息、digest、链头的人查不出来），
        也不等于来源可信；更强的保证要靠库外的独立锚定，这里没有。
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

def recent_messages(self, limit: int = 20, after_seq: int = 0,
                    session_id: str = "") -> List[Dict[str, Any]]:
    """取最近若干条原文（seq 严格大于 after_seq），按时间正序返回。

        给「自动沉淀」用：只读原文链，不参与提交链。
        session_id 非空时只取该会话的原文（B1：多会话各沉各的，
        message 表本就有 session_id 列）。
        """
    sql = ("SELECT seq, role, content, at FROM message WHERE seq > ?")
    params: List[Any] = [int(after_seq or 0)]
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(max(1, int(limit)))
    rows = self._query_all(sql, tuple(params))
    return [dict(r) for r in reversed(rows)]

def latest_message_seq(self) -> int:
    """原文链当前最大 seq，空链返回 0。

        给沉淀水位线初始化用：第一次取水位从链尾起算并落 meta，
        不能从 0 起算——那会把整条历史重沉一遍。
        """
    row = self._query_one("SELECT MAX(seq) AS m FROM message")
    return int(row["m"] or 0) if row else 0

def message_sessions(self) -> List[Dict[str, Any]]:
    """按会话聚合原文链：每行 session_id / n / first_seq / last_seq。

        COALESCE(session_id,'') 让无会话（空串）也聚成一行，不会漏；
        按 last_seq 倒序，最近活跃的会话排前面。给自检算「未建索引」
        与 /记忆补沉 逐会话推进水位用。
        """
    rows = self._query_all(
        "SELECT COALESCE(session_id,'') AS session_id, COUNT(*) AS n,"
        " MIN(seq) AS first_seq, MAX(seq) AS last_seq"
        " FROM message GROUP BY COALESCE(session_id,'')"
        " ORDER BY last_seq DESC")
    return [dict(r) for r in rows]

def count_messages_after(self, after_seq: int = 0, session_id: str = "") -> int:
    """数原文链里 seq 大于 after_seq 的条数；session_id 非空只数该会话。

        口径与 recent_messages 一致：session_id 空串 = 不过滤（含所有
        会话）。给自检算「未建索引条数」：每会话拿它的水位数一遍再求和。
        """
    sql = "SELECT COUNT(*) AS n FROM message WHERE seq > ?"
    params: List[Any] = [int(after_seq or 0)]
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    row = self._query_one(sql, tuple(params))
    return int(row["n"] or 0) if row else 0


def search_messages(self, query: str = "", limit: int = 10,
                    session_id: str = "", since: str = "",
                    return_meta: bool = False, *, SEARCH_CANDIDATE_CAP, SEARCH_EXPANDED_CAP, tokenize) -> Any:
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

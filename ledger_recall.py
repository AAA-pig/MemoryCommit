from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

if __package__:
    from .ledger_core import (
        CLUE_ONLY_BASE, CLUE_SORT_BOOST, DEFAULT_INJECT_SLOTS, DENIED_RECALL_WEIGHT,
        LedgerError, ZERO_GRADE_WEIGHT, HALF_LIFE_DAYS, _bm25_scores, clue_weight,
        normalize_limit, now_str,
    )
else:
    from ledger_core import (
        CLUE_ONLY_BASE, CLUE_SORT_BOOST, DEFAULT_INJECT_SLOTS, DENIED_RECALL_WEIGHT,
        LedgerError, ZERO_GRADE_WEIGHT, HALF_LIFE_DAYS, _bm25_scores, clue_weight,
        normalize_limit, now_str,
    )


def _doc_tokens(self, r: Dict[str, Any], *, tokenize) -> List[str]:
    """一条记忆的切词结果，内容没变就复用。

        key 用 memory_id，但每次都比对原文：摘要或标签改过自然不命中，
        不需要谁记得来失效——少一个「忘了清缓存」的坑。
        """
    text = (r.get("summary") or "") + " " + (r.get("tags") or "")
    mid = r.get("id") or ""
    with self._lock:
        hit = self._tok_cache.get(mid)
    if hit is not None and hit[0] == text:
        return hit[1]
    toks = tokenize(text)
    if mid:
        with self._lock:
            self._tok_cache[mid] = (text, toks)
    return toks

def _clue_tokens(self, clue_id: str, content: str, *, tokenize) -> set:
    """线索的切词结果，内容没变就复用。线索表变动远少于记忆表。"""
    with self._lock:
        hit = self._clue_tok_cache.get(clue_id or "")
    if hit is not None and hit[0] == content:
        return hit[1]
    toks = set(tokenize(content))
    if clue_id:
        with self._lock:
            self._clue_tok_cache[clue_id] = (content, toks)
    return toks

def _trim_tok_cache(self, rows: List[Dict[str, Any]]) -> None:
    """缓存只留当前这轮还在的记忆，删掉的不占地方。"""
    if len(self._tok_cache) <= max(512, 2 * len(rows)):
        return
    keep = {r.get("id") for r in rows}
    with self._lock:
        for k in [k for k in self._tok_cache if k not in keep]:
            self._tok_cache.pop(k, None)


def remember(self, summary: str, level: str = "day", tags: Any = "",
             importance: float = 0.5, grade: int = 1, scope_key: str = "",
             start_time: str = "", end_time: str = "",
             anchor_seq: int = 0, speaker_id: str = "",
             speaker_name: str = "") -> Dict[str, Any]:
    # summary 可能是 None / 数字：这里是唯一的入库口，先收干净再谈别的，
    # 否则 .strip() 直接 AttributeError，工具调用整条炸掉。
    summary = "" if summary is None else str(summary).strip()
    if not summary:
        raise LedgerError("记忆内容不能为空")
    msg = "记下：" + summary[:24]
    builder = self.commit(msg)
    mid = builder.add_memory(summary, level=level, tags=tags, importance=importance,
                             grade=grade, scope_key=scope_key,
                             start_time=start_time, end_time=end_time)
    # 说话人（B2）：同一提交落地，可回溯。空 speaker_id（拿不到发送者）
    # 不写行，builder.add_speaker 自己会跳过。
    if (speaker_id or "").strip():
        builder.add_speaker(mid, speaker_id, speaker_name)
    # 增量原文锚（v5）：anchor_seq 传本轮用户消息的链上序号，锚与记忆
    # 同事务落地（builder._extra_tx）。0 = 不锚；负数是调用方传错了，
    # 显式失败（不编锚）。存量记忆没有锚也不编——trace_to_source 查不到就返回空。
    seq_no = int(anchor_seq or 0)
    if seq_no < 0:
        raise LedgerError("原文锚的序号不能是负数：%d" % seq_no)
    if seq_no > 0:
        def _anchor(cx: sqlite3.Connection, _mid: str = mid,
                    _seq: int = seq_no, _at: str = now_str()) -> None:
            cx.execute("INSERT OR IGNORE INTO memory_source_anchor(memory_id,"
                       " message_seq, digest, at) VALUES(?,?,?,?)",
                       (_mid, _seq, "", _at))
        builder._extra_tx = _anchor
    builder.seal()
    return {"commit": self.head(), "memory_id": mid}

def speakers_of(self, memory_id: str) -> List[Dict[str, Any]]:
    """反查：一条记忆是谁说的（B2 读取侧）。"""
    return [dict(r) for r in self._query_all(
        "SELECT * FROM memory_speaker WHERE memory_id = ?"
        " ORDER BY speaker_id", ((memory_id or "").strip(),))]

def speaker_memories(self, speaker_id: str,
                     limit: int = 20) -> List[Dict[str, Any]]:
    """反查：某个人说过的话都沉进了哪些记忆（B2 读取侧），新的在前。"""
    sid = (speaker_id or "").strip()
    if not sid:
        return []
    return [dict(r) for r in self._query_all(
        "SELECT ms.memory_id, ms.speaker_id, ms.speaker_name,"
        " m.summary, m.created_seq FROM memory_speaker ms"
        " JOIN memory m ON m.id = ms.memory_id"
        " WHERE ms.speaker_id = ?"
        " ORDER BY m.created_seq DESC LIMIT ?",
        (sid, max(1, int(limit))))]


def recall(self, query: str = "", limit: int = 5, scope_key: str = "",
           touch: bool = True, expand: bool = True,
           half_life_days: float = HALF_LIFE_DAYS,
           min_score: float = 0.15,
           exclude: Optional[set] = None, *, time_decay, tokenize) -> List[Dict[str, Any]]:
    """显式检索。exclude（N7）：调用方要求从候选里剔除的记忆集合，
        在排序与截断之前生效——被排除的条目既不占 limit 名额，也不会
        当作图谱扩散的种子。不传就什么都不剔（显式检索仍找得到沉淀）。
        """
    sql = "SELECT * FROM memory"
    params: Tuple[Any, ...] = ()
    if scope_key:
        sql += " WHERE scope_key = ? OR scope_key = ''"
        params = (scope_key,)
    rows = [dict(r) for r in self._query_all(sql, params)]
    if exclude:
        rows = [r for r in rows if r["id"] not in exclude]
    if not rows:
        return []

    limit = normalize_limit(limit)
    self._trim_tok_cache(rows)
    docs = [self._doc_tokens(r) for r in rows]
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
    # 被否认的记忆：立刻降检索权重（v5 §5.1），但不动原文、不删除——
    # 否认不等于删，原文还在。
    denied = self.denied_ids()

    scored: Dict[str, Dict[str, Any]] = {}
    for r, s in zip(rows, base):
        access = r.get("access_count", 0)
        decay = time_decay(r["created_at"], half_life_days, access)
        score = s * decay if q_tokens else 0.0
        # 无查询时按新鲜度 + 巩固等级排
        score += decay * 0.05 * (1 + int(r["grade"]))
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
        if r["id"] in denied:
            score *= DENIED_RECALL_WEIGHT
            r["score"] = round(score, 5)
            r["disputed"] = True
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
                # scope 贯穿每一跳扩散（R6）：显式检索里，邻层也不跨会话
                if scope_key and (nrow["scope_key"] or "") not in ("", scope_key):
                    continue
                nrow = dict(nrow)
                cand = seed["score"] * 0.35 * float(link["strength"]) * \
                    time_decay(nrow["created_at"], half_life_days,
                               nrow.get("access_count", 0))
                if nid in scored:
                    scored[nid]["score"] = round(max(scored[nid]["score"], cand), 5)
                    scored[nid]["match"] = "graph"
                else:  # pragma: no cover —— 防御网：候选池与单查在并发写下不一致时兜住邻居
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
        c_tokens = self._clue_tokens(row["clue_id"], row["clue"])
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

def _clue_bridge(self, query: str, limit: int = 6,
                 scope_key: str = "",
                 exclude: Optional[set] = None, *, time_decay, tokenize) -> List[Dict[str, Any]]:
    """一个词先撞上「线索」，线索再把挂着它的记忆牵出来。

        ai-memory 里联想的主路走的是线索，不是记忆连记忆：
        查询 → 线索 → 记忆。稀有线索权重高，所以「空调」能牵出具体的事，
        而「日常」这种满地都是的线索牵不动什么。
        scope_key 非空时，牵出的记忆限制在该会话（或全局共享 scope_key=''）。
        exclude（N7）：排序截断之前剔除的候选集合。
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
        c_tokens = self._clue_tokens(row["clue_id"], row["clue"])
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
            if mid in seen or (exclude and mid in exclude):
                continue
            mem = self._query_one("SELECT * FROM memory WHERE id = ?", (mid,))
            if not mem:
                continue
            if scope_key and (mem["scope_key"] or "") not in ("", scope_key):
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
                      scope_key: str = "",
                      context: Optional[dict] = None,
                      exclude: Optional[set] = None, *, human_ago, time_decay) -> List[Dict[str, Any]]:
    """没人问起时，哪些记忆有资格自己冒出来：新鲜 × 重要 × 巩固。

        v5 职守过滤：disputed（被否认，停止主动提起）不进主动浮现池；
        coexist（条件并存）给语境且条件匹配才进（R3），判不出/不匹配不进；
        显式检索（recall）仍找得到。
        N7：exclude 是「不许进候选池」的集合，在排序截断之前过滤——
        候选池截断发生在调用方（activate），先截后排会让被排除的条目
        把真事实挤出名单。
        """
    sql = "SELECT * FROM memory"
    params: Tuple[Any, ...] = ()
    if scope_key:
        sql += " WHERE scope_key = ? OR scope_key = ''"
        params = (scope_key,)
    quiet = self.quiet_ids(context)
    if exclude:
        quiet = quiet | set(exclude)
    out: List[Dict[str, Any]] = []
    for row in self._query_all(sql, params):
        r = dict(row)
        if r["id"] in quiet:
            continue
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
             dry_run: bool = False,
             context: Optional[dict] = None,
             profile_visibility: str = "", *, human_ago, time_decay) -> Dict[str, Any]:
    """聊天前的一次「唤起」。

        四条路依次补位（route 的真实取值写在括号里，别再另起一套名字）：
          1. 检索（search）：query 真的命中了什么
          2. 联想（associate）：沿线索/标签连边一跳，把沾边的带出来
          3. 激活（activate）：没有 query 时，让新鲜且重要的自己冒出来
          4. 兑底（fallback）：有 query 却零命中时保守兑底；全被冷却挡光
             则整轮标 fallback_blocked（一条都没捞出时 route 是 none）
        冷却期内刚浮现过的记忆不再重复出现，免得每轮都念同一件事。

        slots 是「这一轮最多说出口几条」，只影响注入，不影响谁留在工作记忆里。
        context 是当前语境（会话场景、当前时间等，R3）：coexist 记忆只在
        条件判定为匹配时放行；传 None 时判不出条件，coexist 全部保守拦。
        profile_visibility（B3）："" 全给（owner 私聊口径），"public" 只给
        「关于主人的 public 画像」（群聊口径，缺行按 private 一律拦），
        其他值显式拒绝——注入口径不许猜。
        """
    query = (query or "").strip()
    slots = normalize_limit(slots, DEFAULT_INJECT_SLOTS)
    profile_vis = (profile_visibility or "").strip()
    if with_profile and profile_vis and profile_vis != "public":
        raise LedgerError(
            "profile_visibility 只支持空（全部）或 public，收到：%r"
            % (profile_vis,))
    cooldown = max(0.0, float(cooldown_min)) * 60.0
    now = time.time()
    picked: List[Dict[str, Any]] = []
    seen = set()
    routes: List[str] = []
    # 候选接纳统一入口的可见性与状态策略（R3）：disputed（被否认）
    # 四条路线（search / associate / activate / fallback）永久拦，
    # fallback_blocked 是 fallback 挡光后的结果值，同样拿不到任何条目；
    # coexist（条件并存）只在语境能判定且条件匹配时放行，判不出/不匹配同样拦。
    # 显式检索 recall 不受此限，仍能找到但 disputed 降权。
    # N1 降级同走这里：自动沉淀条目是原文索引，不管用户把轮次开到几，
    # 都不许当普通事实进系统提示，所有路线一起拦。
    quiet = self.quiet_ids(context)
    auto_sink = self.auto_sink_ids()
    # N7：hidden 在这里定死，往下传给每条路线的候选池——排除发生在
    # 排序截断之前，沉淀才不会把真事实挤出检索名单。take() 里那道
    # 检查保留作双保险，不再是唯一的一道。
    hidden = quiet | auto_sink

    def take(rows: Iterable[Dict[str, Any]], route: str,
             cap: Optional[int] = None, force: bool = False) -> bool:
        added = 0
        cap = slots if cap is None else cap
        for row in rows:
            if len(picked) >= cap:
                break
            mid = row["id"]
            if mid in seen or mid in quiet or mid in auto_sink or (not force
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
                                       scope_key=scope_key, touch=False,
                                       exclude=hidden)
                if h.get("bm25", 0) > 0 or str(h.get("match", "")).startswith("clue:")]
        take(hits, "search")

    # 线索桥：一个词先撞上线索，再顺着线索牵出记忆（scope 贯穿，R6）
    flashes: List[str] = []
    if query and len(picked) < slots:
        for cand in self._clue_bridge(query, slots * 3, scope_key, exclude=hidden):
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
                    # scope 贯穿每一跳扩散（R6）：连边一跳也不能跨会话
                    if scope_key and (row["scope_key"] or "") not in ("", scope_key):
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
        take(self._fresh_candidates(slots * 4, scope_key, context,
                                    exclude=hidden), "activate")
    elif query and not picked:
        # 进兜底分支就无条件记一条 route：让注入日志分得清「没走兜底」和
        # 「兜底被冷却挡光」，别再拿 route=none 顶替、搞得 17 行 no_hit
        # 全是 route=none 看不出毛病。
        routes.append("fallback")
        # 兜底候选池给足。老实现池子只有 slots//2=1 条，真库副本里 fresh
        # 第一名 m6 刚进冷却就直接 route=none、items=[]，而 m2/m1/m5/m7
        # 明明还能用。池子给大（slots*3，至少 6），让不在冷却的候选能进来；
        # 但真正注入的仍只取 slots//2 条，保住 420 预算不被塞满。
        candidates = self._fresh_candidates(max(slots * 3, 6), scope_key, context,
                                            exclude=hidden)
        took = take(candidates, "fallback", cap=max(1, slots // 2))
        if not took:
            # 候选全在冷却里：允许取「最久没露面」的 1 条破例浮现，仍标
            # fallback，避免检索落空后整轮空转。hidden 已在候选池外，
            # 这里再剔一道是双保险（N8）：沉淀永远不会被注入、_surfaced
            # 恒为 0，不做这一步它会永久霸占破例名额，整轮 fallback_blocked。
            eligible = [c for c in candidates if c["id"] not in hidden]
            if len(eligible) >= 2:
                last = min(eligible, key=lambda r: self._surfaced.get(r["id"], 0.0))
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
    if with_profile:
        profile_rows = self.profiles(
            user_id, limit=3, visibility=profile_vis)
    else:
        profile_rows = []
    return {"items": picked,
            "profiles": profile_rows,
            "flashes": flashes,
            "route": routes[0] if routes else "none",
            "query": query}

def render_brief(result: Dict[str, Any], max_chars: int = 420,
                 max_profiles: int = 2, max_item: int = 80,
                 stats: Optional[Dict[str, Any]] = None, *, human_ago) -> str:
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
    # 先确认存在再开提交：否则 builder 一行都没记，seal 会抛「空提交」，
    # 用户看到的是一句「没有任何行变更」，而真正的原因是「没有这条记忆」。
    memory_id = (memory_id or "").strip()
    if not memory_id or not self._query_one(
            "SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    with self.commit("遗忘：%s" % ((reason or memory_id).strip()[:24])) as c:
        c.forget(memory_id)
    return {"commit": self.head(), "memory_id": memory_id}

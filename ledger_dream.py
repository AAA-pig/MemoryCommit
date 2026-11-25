from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List

if __package__:
    from .ledger_persona import PROTECTED_TAGS, VIVID_EXEMPT_TAGS
    from .ledger_core import (
        CONFLICT_MUTEX, CONFLICT_PERSPECTIVE, CONFLICT_SCENE, CONFLICT_TEMPORAL,
        CONFLICT_UNDETERMINED, DENIAL_KEYWORDS, MUTEX_PAIRS, DREAM_CONFIDENCE_CAP,
        DREAM_PROVENANCE, LedgerError, MEMORY_STATUSES, now_str, DREAM_MAX_OUTPUTS,
    )
else:
    from ledger_persona import PROTECTED_TAGS, VIVID_EXEMPT_TAGS
    from ledger_core import (
        CONFLICT_MUTEX, CONFLICT_PERSPECTIVE, CONFLICT_SCENE, CONFLICT_TEMPORAL,
        CONFLICT_UNDETERMINED, DENIAL_KEYWORDS, MUTEX_PAIRS, DREAM_CONFIDENCE_CAP,
        DREAM_PROVENANCE, LedgerError, MEMORY_STATUSES, now_str, DREAM_MAX_OUTPUTS,
    )


# --------------------------------------------------------------------------
# 矛盾四分类器（v5 §3.2）· 这是分类，不是判决
#
# 原设计把「库内互斥」当幻觉信号。错。老大举的例子：今天很冷我要喝热的，
# 明天很热我要喝冷的。两条都在，看着互斥，但都对。这不是幻觉。
# 四类里只有「真互斥」才可能是幻觉，而且还要过原文对账；
# condition 为空的一律挂 conflicted 等证据，不许顺手判成错。
# 判不了是正常的，挂那儿等——这句就是代码注释，写在这儿的。
# --------------------------------------------------------------------------

def parse_condition(cond_json: Any) -> Dict[str, Any]:
    """condition_json → dict。解不出、不是 dict 都当空处理（允许为空）。"""
    if isinstance(cond_json, dict):
        return cond_json
    raw = (cond_json or "").strip() if isinstance(cond_json, str) else ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _condition_part(cond: Dict[str, Any], key: str) -> Dict[str, Any]:
    part = cond.get(key)
    return part if isinstance(part, dict) else {}


def _mutex_hit(a: str, b: str) -> bool:
    """词面互斥：正反对同时出现。必要条件之一，不是判决。"""
    hay_a, hay_b = a or "", b or ""
    if not hay_a or not hay_b:
        return False
    for pos, neg in MUTEX_PAIRS:
        if (pos in hay_a and neg in hay_b) or (neg in hay_a and pos in hay_b):
            return True
    return False


def classify_conflict(a_summary: str, b_summary: str,
                      a_cond: Any = None, b_cond: Any = None,
                      a_perspective: str = "", b_perspective: str = "", *, _windows_overlap) -> str:
    """把一对「打架」的记忆分成四类（或条件不足），只分类，不判决。

    顺序即优先级：场景 > 时变 > 视角 > 真互斥候选 > 条件不足。
    返回值只用于决定挂 conflicted 还是 coexist，永不直接写 corrected——
    corrected 只能走「原文对账 → 追加更正条目」，本轮没有那条路径。
    """
    ca = parse_condition(a_cond)
    cb = parse_condition(b_cond)
    scene_a = str(ca.get("scene") or "").strip()
    scene_b = str(cb.get("scene") or "").strip()
    win_a = _condition_part(ca, "window")
    win_b = _condition_part(cb, "window")

    # 1) 场景依赖：两边都有场景且不同 → 在公司要安静 / 在家要热闹，都对
    if scene_a and scene_b and scene_a != scene_b:
        return CONFLICT_SCENE
    # 2) 时变：两边都有时间窗且窗不重叠 → 冷天喝热的 / 热天喝冷的，都对
    if win_a and win_b and not _windows_overlap(win_a, win_b):
        return CONFLICT_TEMPORAL
    # 3) 视角差异：同一件事你我记的不一样，都是证词，不判对错
    pa = str(a_perspective or "").strip()
    pb = str(b_perspective or "").strip()
    if pa and pb and pa != pb:
        return CONFLICT_PERSPECTIVE
    # 4) 真互斥候选：同一时间、同一场景下词面互斥 → 才可能是幻觉，
    #    而且还要过原文对账（判据只有一句：原文里根本没这回事）
    #    能走到这里，前提是 892/895 两处提前返回仍在：
    #    场景不同（892）与时间窗不重叠（895）已经各自 return，
    #    所以此处不必再判一次，直接平铺进互斥候选。
    env_a = _condition_part(ca, "env")
    env_b = _condition_part(cb, "env")
    env_same = (env_a == env_b) if (env_a or env_b) else True
    if env_same and _mutex_hit(a_summary, b_summary):
        return CONFLICT_MUTEX
    # 条件不足：挂 conflicted 等证据，不许顺手判成错
    return CONFLICT_UNDETERMINED


# --------------------------------------------------------------------------
# coexist 条件判定（R3）
#
# disputed 是「永远不主动提」；coexist 只是「条件不匹配时不主动提」，
# 条件匹配就该能正常浮现。条件存在 memory_condition 的 cond_json
# （字段同 classify_conflict：scene / window / env）。
# 判定原则只有一条：判得出且全部匹配才放行，判不出一律保守拦。
# --------------------------------------------------------------------------

CONDITION_KEYS = ("scene", "window", "env")


def _ctx_time(value: Any, *, _parse_time_cached) -> float:
    """语境里的时间值 → 时间戳；解析不出返回 0（调用方按「判不出」处理）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        return _parse_time_cached(value.strip()[:19])
    return 0.0


def condition_allows(cond_json: Any, context: Any, *, _ctx_time) -> bool:
    """coexist 的条件在当前语境下是否成立（True 才许主动浮现）。

    判不出一律 False，不许「判不出就放行」：
      - context 缺席 / 不是对象 / 是空的 —— 没有语境无从判起
      - cond_json 空、坏 JSON、解析出来不是对象 —— 没有条件可判
      - cond 里出现 scene/window/env 之外的字段 —— 字段不认识，语义未知
      - 认识的字段缺对应语境数据（要场景没场景、要时间没时间、要环境没环境）
      - cond 所有字段都是空值 —— 等于没写条件，同样判不出
    条件写全且逐项匹配，才放行。
    """
    cond = parse_condition(cond_json)
    if not cond or not isinstance(context, dict) or not context:
        return False
    if any(k not in CONDITION_KEYS for k in cond):
        return False
    scene = str(cond.get("scene") or "").strip()
    window = _condition_part(cond, "window")
    env = _condition_part(cond, "env")
    if not scene and not window and not env:
        return False
    if scene:
        ctx_scene = str(context.get("scene") or "").strip()
        if not ctx_scene or ctx_scene != scene:
            return False
    if window:
        now_ts = _ctx_time(context.get("now"))
        f_ts = _ctx_time(window.get("from"))
        t_ts = _ctx_time(window.get("to"))
        if not (now_ts and f_ts and t_ts):
            return False
        if not (f_ts <= now_ts <= t_ts):
            return False
    if env:
        ctx_env = context.get("env")
        if not isinstance(ctx_env, dict) or ctx_env != env:
            return False
    return True


def match_denial_keywords(text: str,
                          keywords: Iterable[str] = DENIAL_KEYWORDS) -> List[str]:
    """用户话里命中的否认关键词。命中只进待确认队列，不自动改状态。"""
    text = (text or "").strip()
    if not text:
        return []
    return [k for k in keywords if k and k in text]


def _dream_forbidden_tags() -> frozenset:
    """梦产出不许带的标签：保护标签 ∪ 鲜活度豁免标签（两处必须同源）。

    实测过的漏口：PROTECTED_TAGS 只有 manual/protected/keep/no_decay，
    而 VIVID_EXEMPT_TAGS 还含 pinned/never_forget/manual_keep——
    梦产出带一个 pinned 就能免疫老化，等于自己给自己发免死金牌。
    所以梦的白名单取并集：宁可多剔，不许绕过。
    """
    return frozenset(PROTECTED_TAGS) | frozenset(VIVID_EXEMPT_TAGS)


def dream_produced_ids(self) -> set:
    """梦产出的记忆集合。真值在 dream_proposal（kind='new_memory'）里——
        memory 主表没有 provenance 列（主表结构不动），登记与产出同事务落地。"""
    ids = set()
    for r in self._query_all(
            "SELECT payload_json FROM dream_proposal"
            " WHERE kind = 'new_memory' AND status = 'accepted'"):
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            continue
        mid = str(payload.get("memory_id") or "").strip()
        if mid:
            ids.add(mid)
    return ids


# -- 咬合点（memory 对梦只露这两个口）--

def propose_clues(self, query: str = "", limit: int = 6,
                  scope_key: str = "") -> List[Dict[str, Any]]:
    """咬合点 1：给梦（也给检索）出候选。只读，不 touch、不写任何表。

        排除梦产出的条目（锁 1 的入口：梦产出永不作证据、不作依据）
        与被否认的条目（disputed 不再供梦与检索引用）。
        """
    hits = self.recall(query, limit=limit, scope_key=scope_key, touch=False)
    excluded = self.dream_produced_ids() | self.denied_ids()
    out = [h for h in hits if h["id"] not in excluded]
    return out[:max(1, int(limit))]


# -- 梦产出与梦的写路径 --

def dream_add_memory(self, summary: str, basis_ids: Iterable[str] = (),
                     tags: Any = "", importance: float = DREAM_CONFIDENCE_CAP,
                     scope_key: str = "", *, CommitBuilder) -> Dict[str, Any]:
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
    basis = [b.strip() for b in (basis_ids or []) if str(b).strip()]
    dream_ids = self.dream_produced_ids()
    bad = [b for b in basis if b in dream_ids]
    if bad:
        raise LedgerError(
            "梦条目不能作梦的依据（防自我强化锁 1，否则梦提案 → 误判 → 再梦成闭环）：%s"
            % ",".join(bad))
    summary = (summary or "").strip()
    if not summary:
        raise LedgerError("梦产出也要有内容")
    importance = min(DREAM_CONFIDENCE_CAP, max(0.0, float(importance)))
    tag_list = [t for t in CommitBuilder._split_tags(tags)
                if str(t).strip().lower() not in _dream_forbidden_tags()]
    proposal_id = self._new_proposal_id()
    builder = self.commit("梦产出（低置信，可删）：" + summary[:24])
    mid = builder.add_memory(summary, level="day", tags=tag_list,
                             importance=importance, grade=0,
                             scope_key=(scope_key or "").strip())
    payload = json.dumps({"memory_id": mid, "summary": summary[:400],
                          "importance": importance, "basis_ids": basis},
                         ensure_ascii=False)

    def _register(cx: sqlite3.Connection) -> None:
        cx.execute("INSERT INTO dream_proposal(id, kind, payload_json, basis_ids,"
                   " base_commit_seq, provenance, status, created_at, decided_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (proposal_id, "new_memory", payload, ",".join(basis),
                    builder._gseq, DREAM_PROVENANCE, "accepted",
                    now_str(), now_str()))

    builder._extra_tx = _register
    builder.seal()
    return {"memory_id": mid, "proposal_id": proposal_id,
            "commit": self.head(), "importance": importance}

def dream_apply(self, proposals: List[Dict[str, Any]],
                status_updates: List[Dict[str, Any]]) -> Dict[str, int]:
    """梦的一轮写操作原子落地：提案与状态变更在同一个事务里。

        自动写入守卫：执行期间人格分区只读（restricted_auto_write）。
        v5 §1.4 硬要求：不许出现「线连上了、节点没落地」或反过来。
        失败整轮放弃，不留半条（v5 §1.2 职守表）。
        proposals: [{kind, payload(dict), basis_ids(list), status, provenance}]
        status_updates: [{memory_id, status, reason, evidence_ref}]
        """
    at = now_str()
    with self._tx() as cx:
        for s in status_updates or []:
            st = str(s.get("status") or "").strip()
            if st not in MEMORY_STATUSES:
                raise LedgerError("非法状态 %r，可用：%s" % (st, "/".join(MEMORY_STATUSES)))
            mid = (s.get("memory_id") or "").strip()
            if not cx.execute("SELECT 1 FROM memory WHERE id = ?", (mid,)).fetchone():
                raise LedgerError("没有这条记忆：%s" % mid)
            cx.execute("INSERT INTO memory_status(memory_id, status, reason,"
                       " evidence_ref, at) VALUES(?,?,?,?,?)",
                       (mid, st, str(s.get("reason") or "")[:300],
                        str(s.get("evidence_ref") or "")[:120], at))
        for p in proposals or []:
            kind = str(p.get("kind") or "").strip()
            if kind not in ("link", "node", "new_memory", "suspect"):
                raise LedgerError("非法提案类型 %r" % kind)
            payload = p.get("payload")
            payload_json = json.dumps(payload or {}, ensure_ascii=False,
                                      sort_keys=True)
            cx.execute("INSERT INTO dream_proposal(id, kind, payload_json,"
                       " basis_ids, provenance, status, created_at)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (self._new_proposal_id(), kind, payload_json,
                        ",".join(str(b) for b in p.get("basis_ids") or []),
                        str(p.get("provenance") or DREAM_PROVENANCE),
                        str(p.get("status") or "pending"), at))
    return {"proposals": len(proposals or []), "statuses": len(status_updates or [])}

def nominate_node(self, memory_id: str, reason: str = "") -> Dict[str, str]:
    """梦提名节点（kind='node' 提案）。dream 产出的记忆永不可升为节点。"""
    if memory_id in self.dream_produced_ids():
        raise LedgerError(
            "梦条目永不可升为节点（防自我强化锁 3）：%s" % memory_id)
    if not self._query_one("SELECT 1 FROM memory WHERE id = ?", (memory_id,)):
        raise LedgerError("没有这条记忆：%s" % memory_id)
    pid = self._new_proposal_id()
    payload = json.dumps({"memory_id": memory_id, "reason": (reason or "")[:200]},
                         ensure_ascii=False)
    with self._tx() as cx:
        cx.execute("INSERT INTO dream_proposal(id, kind, payload_json, basis_ids,"
                   " provenance, status, created_at) VALUES(?,?,?,?,?,?,?)",
                   (pid, "node", payload, memory_id, DREAM_PROVENANCE,
                    "pending", now_str()))
    return {"proposal_id": pid, "kind": "node", "status": "pending"}


# --------------------------------------------------------------------------
# v5 · DreamWorker（梦境职守 · 骨架，影子运行）
#
# 梦只做三件事（v5 §2）：产出模糊新记忆（允许）/ 不重组旧记忆（结构保证：
# 全系统不存在更新 summary 的 API，本次改造也不新增）/ 发现错误并提名（不能判）。
# 判据只有一句：原文里根本没这回事。不是「两条记忆打架」——打架大多是真的。
#
# 梦不能当法官（v5 §2.3.2）：梦本身是幻觉高发区，让梦修幻觉等于让被告当法官。
# Anthropic 的实测：Claude 把宝可梦中心记在东侧（实际西侧），笔记里没有任何
# 矛盾项，它只靠自己编解释「我冲太西了」「我一直在正确的一侧」，越编越圆；
# 最后是拿笔记对 transcript 原文才抓出错。原文才是裁判。
#
# 三道防自我强化锁（v5 §2.4），全部有测试钉住（tests/test_dream_guard.py）：
#   1. dream 条目永不作证据——propose_clues 与 dream_add_memory 的 basis 都排除梦产出
#   2. dream 条目被再次梦见不加权——只有真人提起才走 touch 加访问（入口锁死）
#   3. dream 不能给节点提名——nominate_node 拒绝梦产出条目
# --------------------------------------------------------------------------


def run_once(self, query: str = "", scope_key: str = "",
             produce: int = 0, max_links: int = 3) -> Dict[str, Any]:
    """跑一轮梦（影子）。开关关着时零写入直接跳过；开了也只写提案与低置信产出。

        produce 是本轮最多产出几条模糊新记忆，硬顶 DREAM_MAX_OUTPUTS=2；
        骨架阶段的产出来自候选前两条的组合占位（模糊印象），
        真正的产出质量等开关打开后由宿主侧策略喂进来。

        原子性边界（R7）：单条产出自身是原子的（登记与记忆插入同事务），
        整轮不承诺原子——中途失败时已落地部分保留，不再向外抛异常，
        返回值里 partial=True 且 errors 逐条列出失败点，部分成功必须显式可见。
        """
    if not self.enabled:
        return {"skipped": True, "reason": "DREAM_SCHEDULER_ENABLED=false",
                "proposals": [], "produced": []}
    cands = self.ledger.propose_clues(query, limit=max(6, int(max_links) * 2),
                                      scope_key=scope_key)
    proposals: List[Dict[str, Any]] = []
    # 连线：共享概念的候选对提一条 link 提案（影子：不真写 memory_link）
    tag_sets = []
    for c in cands:
        tag_sets.append({t for t in re.split(r"[,，、;；]+", c.get("tags") or "") if t})
    for i in range(len(cands)):
        if len(proposals) >= max(1, int(max_links)):
            break
        for j in range(i + 1, len(cands)):
            if len(proposals) >= max(1, int(max_links)):
                break
            shared = tag_sets[i] & tag_sets[j]
            if not shared:
                continue
            proposals.append({
                "kind": "link",
                "payload": {"from": cands[i]["id"], "to": cands[j]["id"],
                            "concept": sorted(shared)[0]},
                "basis_ids": [cands[i]["id"], cands[j]["id"]],
                "status": "pending",
            })
    # 产出：单次最多 DREAM_MAX_OUTPUTS 条，置信上限硬编码在 dream_add_memory 里
    produced: List[Dict[str, Any]] = []
    errors: List[str] = []
    produce = max(0, min(int(produce), DREAM_MAX_OUTPUTS))
    for _ in range(produce):
        if len(cands) < 2:
            break
        a, b = cands[0], cands[1]
        basis = [a["id"], b["id"]]
        # 候选已被锁 1 排除梦产出；这里再防一次（双保险，写入侧也拦）
        summary = "（梦）大概与「%s」和「%s」有关的一段模糊印象" % (
            (a.get("summary") or "")[:20], (b.get("summary") or "")[:20])
        try:
            res = self.ledger.dream_add_memory(summary, basis_ids=basis,
                                               scope_key=scope_key)
        except LedgerError as e:
            # 单条失败不再无声打断：记下失败点，已产出的保留（部分成功，R7）
            errors.append("产出失败：%s" % e)
            break
        produced.append(res)
    if proposals:
        try:
            self.ledger.dream_apply(proposals, [])
        except LedgerError as e:
            errors.append("提案落地失败：%s" % e)
    return {"skipped": False, "proposals": proposals,
            "produced": produced, "candidates": len(cands),
            "errors": errors, "partial": bool(errors)}

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional

if __package__:
    from .ledger_core import LedgerError, PERSONA_CORE_HASH_KEY, PROFILE_VISIBILITIES
    from .ledger_commit import _compute_persona_core_hash
else:
    from ledger_core import LedgerError, PERSONA_CORE_HASH_KEY, PROFILE_VISIBILITIES
    from ledger_commit import _compute_persona_core_hash


# -- 人格分区（v7）--

def _verify_persona_core_seal(self, context: str = "", *, log) -> Dict[str, Any]:
    """核对 core 人格哨兵哈希。不符只报警记日志，不抛、不修。

        从未播种（无 core 行且 meta 无键）不算篡改；任何一侧非空就
        必须对得上。手工 SQL 改库、备份回灌串号都会在这里露出来。
        """
    rows = [dict(r) for r in self._query_all(
        "SELECT id, content, priority FROM core_rule"
        " WHERE kind = 'core' ORDER BY id")]
    current = _compute_persona_core_hash(rows)
    row = self._query_one(
        "SELECT v FROM meta WHERE k = ?", (PERSONA_CORE_HASH_KEY,))
    stored = row["v"] if row else ""
    needs = bool(rows) or bool(stored)
    ok = (not needs) or (stored == current)
    state = {"ok": ok, "stored": stored, "current": current,
             "core_rows": len(rows)}
    if not ok:
        log.warning("[记忆账本] core 人格哈希哨兵不符（%s）：meta 存 %r，"
                    "当前 %r，共 %d 行 core。不阻止启动、不自动修；"
                    "人工确认后调 reseal_persona_core(confirm=True) 重封。",
                    context or "check", stored, current, len(rows))
    return state

def add_persona(self, kind: str, content: str, priority: int = 0,
                source: str = "self") -> str:
    """加一条人格设定（走提交链）。core 分区只读，这里永远写不进。

        自动写入守卫：restricted_auto_write 范围内一律拒绝——
        自动沉淀 / 梦产出等路径不许写人格分区，连 self 分区也不行。
        """
    self._assert_user_write_scope()
    with self.user_persona_write("api"):
        with self.commit("persona add kind=%s" % kind) as c:
            return c.add_persona(kind, content, priority=priority, source=source)

def add_persona_batch(self, items, source: str = "handwrite") -> List[str]:
    """一次手写多条人格，整批一个提交：要么全写，要么一条不写。

        和 add_persona 走同一道闸（反向守卫 + 通行令牌），区别只在提交
        粒度。手写导入是用户的一次动作，拆成 N 个提交会把提交链搅成
        碎渣，回滚也要点 N 下。

        校验先全量过一遍再开提交：builder 那层也会再校验，但那是兜底，
        不是重复劳动——门面以后可能被别人直接调用，报错要说得早。
        """
    self._assert_user_write_scope()
    rows = []
    for i, it in enumerate(items or [], 1):
        if not isinstance(it, dict):
            raise LedgerError("第 %d 条格式不对，应为 dict，收到：%r"
                              % (i, type(it)))
        kind = str(it.get("kind") or "self").strip() or "self"
        if kind == "core":
            raise LedgerError(
                "第 %d 条想写 core 分区：core 只读，播种请走 _seed_persona_core" % i)
        content = str(it.get("content") or "").strip()
        if not content:
            raise LedgerError("第 %d 条人格内容为空" % i)
        if len(content) > 200:
            raise LedgerError("第 %d 条人格 %d 字，超过单条 200 字上限"
                              % (i, len(content)))
        try:
            priority = int(it.get("priority") or 0)
        except (TypeError, ValueError):
            raise LedgerError("第 %d 条权重不是整数：%r"
                              % (i, it.get("priority")))
        if not 0 <= priority <= 1000:
            raise LedgerError("第 %d 条权重 %d 超出 0..1000" % (i, priority))
        rows.append({"kind": kind, "content": content, "priority": priority})
    if not rows:
        raise LedgerError("没有要写入的人格条目")
    source = (source or "").strip() or "handwrite"
    if len(source) > 64:
        raise LedgerError("source 超过 64 字上限")
    with self.user_persona_write("handwrite"):
        with self.commit("persona batch add %d 条" % len(rows)) as c:
            return [c.add_persona(r["kind"], r["content"],
                                  priority=r["priority"], source=source)
                    for r in rows]

def update_persona(self, rule_id: str, content: Optional[str] = None,
                   priority: Optional[int] = None) -> bool:
    """改一条 self 分区人格（走提交链）。core 分区一律拒绝。

        照 forget 先例先预检再开提交：行不存在或 clamp 后无变化直接
        返回 False，不写字；否则 builder 一行都没记，seal 会抛
        「空提交」，用户看到的原因会驴唇不对马嘴。
        """
    self._assert_user_write_scope()
    rule_id = (rule_id or "").strip()
    if not rule_id:
        return False
    row = self._query_one("SELECT * FROM core_rule WHERE id = ?", (rule_id,))
    if row is None:
        return False
    snap = dict(row)
    if snap.get("kind") == "core":
        raise LedgerError(
            "人格行 %s 是 kind='core' 只读基础设定，update_persona 拒绝" % rule_id)
    after = dict(snap)
    if content is not None:
        content = (content or "").strip()
        if not content:
            raise LedgerError("人格内容不能为空")
        if len(content) > 200:
            raise LedgerError("单条人格超过 200 字上限")
        after["content"] = content
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise LedgerError("priority 必须是整数，收到：%r" % (priority,))
        after["priority"] = max(0, min(1000, priority))
    if after == snap:
        return False
    with self.user_persona_write("api"):
        with self.commit("persona update %s" % rule_id) as c:
            return c.update_persona(rule_id, content=content, priority=priority)

def remove_persona(self, rule_id: str) -> bool:
    """删一条 self 分区人格（走提交链）。core 分区一律拒绝。

        预检同 update：行不存在直接 False，不开空提交。
        """
    self._assert_user_write_scope()
    rule_id = (rule_id or "").strip()
    if not rule_id:
        return False
    row = self._query_one("SELECT kind FROM core_rule WHERE id = ?", (rule_id,))
    if row is None:
        return False
    if row["kind"] == "core":
        raise LedgerError(
            "人格行 %s 是 kind='core' 只读基础设定，remove_persona 拒绝" % rule_id)
    with self.user_persona_write("api"):
        with self.commit("persona remove %s" % rule_id) as c:
            return c.remove_persona(rule_id)

def list_persona(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """列人格行。priority DESC, created_at ASC；kind 给了就过滤。"""
    sql = "SELECT * FROM core_rule"
    params: tuple = ()
    k = (kind or "").strip()
    if k:
        sql += " WHERE kind = ?"
        params = (k,)
    sql += " ORDER BY priority DESC, created_at ASC"
    return [dict(r) for r in self._query_all(sql, params)]


def persona_text(self, budget: int = 1200, *, render_persona_rows) -> str:
    """人格注入文本：每条一行「- {content}」，超预算按整条截断。

        排序 priority DESC, created_at ASC；放不下的整条不拆，末尾
        另起一行报告有几条没进；空表返回空串。人格先于记忆注入，
        独立预算，不与 render_brief 捷径。
        """
    return render_persona_rows(self.list_persona(), budget=budget)


def _seed_persona_core(self, items: List[Dict[str, Any]]) -> List[str]:
    """显式播种 kind='core' 基础人格行，封链后哨兵哈希自动封好。

        内部方法：不暴露给 LLM 工具或命令。items 每项含 content，
        可选 priority / source（默认 seed）。
        """
    ids: List[str] = []
    with self.user_persona_write("seed"):
        with self.commit("persona seed core %d 条" % len(items)) as c:
            for item in items:
                ids.append(c.add_persona(
                    "core", item["content"],
                    priority=item.get("priority", 0),
                    source=item.get("source", "seed"), _allow_core=True))
    return ids

def reseal_persona_core(self, confirm: bool = False) -> Dict[str, Any]:
    """重封 core 人格哨兵哈希。人工确认后才可调（confirm=True）。"""
    if confirm is not True:
        raise LedgerError(
            "reseal_persona_core 是人工确认操作：必须显式传 confirm=True，"
            "自动重封会掩盖篡改")
    with self._tx() as cx:
        row = cx.execute(
            "SELECT v FROM meta WHERE k = ?", (PERSONA_CORE_HASH_KEY,)).fetchone()
        stored = row["v"] if row else ""
        rows = [dict(r) for r in cx.execute(
            "SELECT id, content, priority FROM core_rule"
            " WHERE kind = 'core' ORDER BY id").fetchall()]
        current = _compute_persona_core_hash(rows)
        self._set_meta(cx, PERSONA_CORE_HASH_KEY, current)
    return {"resealed": True, "was_consistent": stored == current,
            "stored": stored, "current": current, "core_rows": len(rows)}


# --------------------------------------------------------------------------
# 鲜活度（vividness）· 只读判定，暂不接渲染
#
# 鲜活度降级链参考同类实现：
# 年轻的记忆说得清楚，老的只剩轮廓；但誓言、伤疤、创作里程碑不受年龄侵蚀。
# P2 第 0 步只产出函数 + 回放报告，render_brief 一行没动。
# --------------------------------------------------------------------------


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


def remember_profile(self, user_id: str, fact: str,
                     category: str = "event", weight: int = 1,
                     visibility: str = "") -> Dict[str, str]:
    """写画像事实（B3）。visibility 非空时同提交落 profile_visibility 行。

        缺省不写可见性行：读侧对缺行一律按 private 处理（保守），
        所以不传和传 private 行为一致；只有明确说 public 才对群聊可见。
        """
    vis = (visibility or "").strip()
    if vis and vis not in PROFILE_VISIBILITIES:
        raise LedgerError(
            "visibility 只支持 private/public，收到：%r" % (visibility,))
    with self.commit("画像：%s" % fact.strip()[:20]) as c:
        pid = c.upsert_profile(user_id, fact, category=category, weight=weight)
        if vis:
            c.set_profile_visibility(pid, vis)
    return {"commit": self.head(), "profile_id": pid}

def set_profile_visibility(self, profile_id: str,
                           visibility: str) -> Dict[str, str]:
    """手动改一条画像的可见性（B3）。走提交链。"""
    with self.commit("画像可见性：%s" % profile_id) as c:
        vis = c.set_profile_visibility(profile_id, visibility)
    return {"commit": self.head(), "profile_id": profile_id,
            "visibility": vis}

def profile_visibility_of(self, profile_id: str) -> str:
    """查一条画像的可见性。缺行按 private 处理（保守，B3 默认口径）。"""
    row = self._query_one(
        "SELECT visibility FROM profile_visibility WHERE profile_id = ?",
        ((profile_id or "").strip(),))
    return row["visibility"] if row else "private"

def profiles(self, user_id: str = "", limit: int = 30,
             visibility: str = "") -> List[Dict[str, Any]]:
    """查画像。visibility='public' 时只给群聊可见的那部分（B3 注入口径）。

        缺可见性行的画像按 private 处理：LEFT JOIN 过滤后天然被排除。
        """
    vis = (visibility or "").strip()
    if vis and vis != "public":
        raise LedgerError("visibility 只支持空（全部）或 public，收到：%r" % vis)
    lim = max(1, int(limit))
    if vis == "public":
        if user_id:
            rows = self._query_all(
                "SELECT p.* FROM profile p LEFT JOIN profile_visibility pv"
                " ON pv.profile_id = p.id"
                " WHERE pv.visibility = 'public' AND p.user_id = ?"
                " ORDER BY p.weight DESC, p.updated_at DESC LIMIT ?",
                (user_id, lim))
        else:
            rows = self._query_all(
                "SELECT p.* FROM profile p LEFT JOIN profile_visibility pv"
                " ON pv.profile_id = p.id"
                " WHERE pv.visibility = 'public'"
                " ORDER BY p.weight DESC, p.updated_at DESC LIMIT ?", (lim,))
    else:
        if user_id:
            rows = self._query_all(
                "SELECT * FROM profile WHERE user_id = ?"
                " ORDER BY weight DESC, updated_at DESC LIMIT ?",
                (user_id, lim))
        else:
            rows = self._query_all(
                "SELECT * FROM profile ORDER BY weight DESC LIMIT ?", (lim,))
    return [dict(r) for r in rows]

def set_attitude(self, user_id: str, level: int,
                 reason: str = "") -> Dict[str, Any]:
    """设置一个人的坏感度（B4）：0 正常、4 最低。走提交链，owner 锁死。"""
    with self.commit("坏感度：%s → %s" % (
            (user_id or "").strip()[:24], level)) as c:
        lv = c.set_attitude(user_id, level, reason)
    return {"commit": self.head(), "user_id": (user_id or "").strip(),
            "level": lv}

def get_attitude_detail(self, user_id: str) -> Dict[str, Any]:
    """读一个人的坏感度详情（level/reason/updated_at），给查询命令用。

        读失败按 0 处理不阻塞聊天：没行、库坏了、参数烂了都返回 level=0
        的空档。写路径才是显式失败的地方。
        """
    uid = (user_id or "").strip()
    empty = {"user_id": uid, "level": 0, "reason": "", "updated_at": ""}
    try:
        row = self._query_one(
            "SELECT user_id, level, reason, updated_at FROM user_attitude"
            " WHERE user_id = ?", (uid,))
        return dict(row) if row else empty
    except Exception:
        return empty

def get_attitude(self, user_id: str) -> int:
    """读一个人的坏感度等级（B4 给注入链路用）。读失败按 0 处理。"""
    return int(self.get_attitude_detail(user_id)["level"])

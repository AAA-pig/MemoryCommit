# -*- coding: utf-8 -*-
"""沉淀（sink）纯函数层：把几轮原文折成一条索引正文，并做原文引用锚。

只做文本与行字段的拼装，不碰库、不读环境变量、不记失败计数——
这些都留在 main.py 的插件方法里（`_auto_sink` / `_sink_watermark` /
`_write_sink_marks`），因为它要读的 `AUTO_SINK_EVERY` 是模块级开关，
测试靠 monkeypatch main 的全局来验，挪走就读不到补丁了。
"""

from __future__ import annotations

from typing import Any

try:
    from .ledger import tokenize, GRADES, LedgerError
except ImportError:
    from ledger import tokenize, GRADES, LedgerError


def auto_sink_summary(rows, anchor="") -> str:
    """把几轮原文折成一条记忆正文（纯规则，不调模型）。

    只做摘要与关键词，不改写原文、不许当结论引用；原文链一直都在，
    这条只是「这几轮聊了什么」的索引。anchor 非空时追加在末尾
    （sink_anchor 生成的原文引用锚），索引得能指回原文；主体先按
    4000 字截断，锚永远完整。
    """
    rows = list(rows or [])
    if not rows:
        return ""
    said = " ".join((r.get("content") or "") for r in rows if r.get("role") == "user")
    if not said:
        said = " ".join((r.get("content") or "") for r in rows)
    said = " ".join(said.split())
    counts = {}
    for tok in tokenize(said):
        if len(tok) < 2:
            continue
        counts[tok] = counts.get(tok, 0) + 1
    keywords = []
    for tok, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if any(tok in k for k in keywords):
            continue
        keywords.append(tok)
        if len(keywords) >= 5:
            break
    head = said if len(said) <= 60 else said[:60] + "…"
    # 正文不再带字面前缀「自动沉淀：」（N9）：「这是什么」已由 tags 里的
    # 机器标记表达，正文重复一次只会让「自动」「沉淀」这些词面在检索里
    # 命中一切沉淀条目，把普通事实挤出词面排序。
    text = "最近 %d 条原文（用户 %d 条）。开头是「%s」" % (
        len(rows), sum(1 for r in rows if r.get("role") == "user"), head)
    if keywords:
        text += "；关键词：" + "、".join(keywords)
    text = text[:4000]
    if anchor:
        text += anchor
    return text


def sink_anchor(rows, session_id: str = "") -> str:
    """从这批原文取引用锚：「（原文 seq a–b · 会话 s · 时间 t1→t2）」。

    沉淀条目是索引不是事实，索引得能指回原文：正文尾部带上
    「折的是哪几条、哪个会话、什么时间段」，/记忆原话 能顺着
    seq 查回原话。rows 为空返回空串；单条原文只写一个 seq；
    会话号超过 40 字截断；时间取 at 的「年-月-日 时：分」。
    """
    rows = list(rows or [])
    if not rows:
        return ""
    seqs = [int(r.get("seq") or 0) for r in rows]
    a, b = min(seqs), max(seqs)
    seg = ("原文 seq %d" % a) if a == b else ("原文 seq %d–%d" % (a, b))
    sess = session_id or ""
    if sess:
        if len(sess) > 40:
            sess = sess[:40] + "…"
        seg += " · 会话 " + sess
    at0 = (rows[0].get("at") or "")
    at1 = (rows[-1].get("at") or "")
    if at0 or at1:
        seg += " · 时间 %s→%s" % (at0[:16].replace("T", " ") or "?",
                                 at1[:16].replace("T", " ") or "?")
    return "（%s）" % seg


class FailureCounter:
    """写路径异常计数（内存即可）。

    以前失败只留一行 warning，重载后什么都不剩；计数能让 /记忆自检 说清
    「哪条写路径在悄悄失败」。不落盘：重启归零本来就是应该的。
    """

    def __init__(self):
        self.counts = {}
        self.last = {}

    def note(self, kind: str, err: Any) -> int:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.last[kind] = str(err)[:160]
        return self.counts[kind]

    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return "无"
        return "；".join("%s %d 次" % (k, v) for k, v in sorted(self.counts.items()))


def _short(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


# LLM 工具的参数不是人填的表格：可能是 null、数字、带空格的串、甚至
# 「很高」这种。入库口（ledger）会拦，但拦的方式是抛异常，异常从工具里
# 溢出去就是整条回复炸掉。所以在这一层先把参数收干净。

def _as_text(value: Any, name: str = "内容", limit: int = 0) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise LedgerError("%s不能为空" % name)
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _as_int(value: Any, default: int, low: int = 0, high: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError：int(float("inf")) 这种（N4）。
        n = default
    if low:
        n = max(low, n)
    if high:
        n = min(high, n)
    return n


def _as_float(value: Any, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return n


def _fmt_hit(m: dict, idx: int = 0) -> str:
    tag = "·"
    match = str(m.get("match", ""))
    if match.startswith("graph"):
        tag = "↗"
    elif match.startswith("clue"):
        tag = "⌁"
    elif match.startswith("direct+clue"):
        tag = "·"
    head = "%d. %s %s" % (idx, tag, _short(m["summary"], 56))
    tail = "   %s / %s / 分 %.2f" % (m["id"], GRADES.get(int(m["grade"]), "?"), m["score"])
    if m.get("clue_boost"):
        tail += " · 线索 +%.0f%%" % (float(m["clue_boost"]) * 100)
    if m.get("disputed"):
        # R3：显式检索也标出争议，别让被否认的记忆看起来和普通记忆一样可信
        tail += " · 有争议（被否认过，待对证）"
    return head + tail


def status_text(st: dict) -> str:
    """/记忆（不带参数）的那一屏账本概况。"""
    grade = " / ".join("%s %d" % (k, v) for k, v in st["by_grade"].items())
    level = " / ".join("%s %d" % (k, v) for k, v in st["by_level"].items()) or "空"
    return (
        "记忆账本\n"
        "HEAD %s · 创建序号 %d\n"
        "记忆 %d 条（%s）\n"
        "分层：%s\n"
        "工作记忆 %d/%d 个名额\n"
        "线索 %d 个 · 连边 %d 条 · 画像 %d 条\n"
        "提交 %d 个 · 库 %.0f KB"
        % (st["head"] or "（空）", st["created_seq"], st["memories"], grade,
           level,
           st["by_grade"].get("一级", 0), st["working_capacity"],
           st["clues"], st["links"], st["profiles"],
           st["commits"], st["db_bytes"] / 1024.0)
    )


def recall_text(arg: str, hits) -> str:
    """/记忆 关键词：命中的头几条。没有就说没有，不硬凑。"""
    if not hits:
        return "账本里没有和「%s」沾边的记忆。" % _short(arg, 16)
    lines = ["关于「%s」：" % _short(arg, 16), ""]
    for i, m in enumerate(hits, 1):
        lines.append(_fmt_hit(m, i))
    return "\n".join(lines)


def history_text(rows) -> str:
    """/记忆链：最近几个提交。"""
    if not rows:
        return "还没有任何提交。"
    lines = ["最近提交：", ""]
    for r in rows:
        lines.append("%s %s  （%d 行）" % (r["id"], _short(r["message"], 34), r["rows"]))
    return "\n".join(lines)


def raw_text(arg: str, res: dict, st: dict = None) -> str:
    """/记忆原话：原文链里翻到的几句。

    没命中时给出「为什么可能是空的」——关键词过滤过、或只翻了最近一段
    被截断的窗口，别让人以为原文真的没有。
    """
    rows = res.get("rows") or []
    if not rows:
        tip = "（关键词：%s）" % arg if arg else ""
        tail = ""
        if res.get("truncated"):
            tail = "，原文链共 %d 条，只查了最近 %d 条，关键词可能埋在更早的对话里" % (
                res["total"], res["scanned"])
        return "原文链里没有%s。%s" % (tip, tail)
    st = st or {}
    lines = ["原文链共 %d 条，最早 %s" % (st.get("count", 0), (st.get("since") or "-")[:16]), ""]
    if res.get("truncated"):
        lines.append("（只查了最近 %d 条原文，没往更早翻）" % res["scanned"])
    for r in rows:
        who = "我" if r["role"] == "assistant" else "用户"
        lines.append("[%s] %s：%s" % (r["at"][11:16], who, r["content"][:160]))
    return "\n".join(lines)


def access_text(arg: str, rows) -> str:
    """/记忆访问：某条记忆最近被想起的痕迹。"""
    if not rows:
        return "还没有访问痕迹%s。" % ("（%s）" % arg if arg else "")
    lines = ["最近 %d 次被想起：" % len(rows), ""]
    for r in rows:
        q = (r.get("query") or "").strip()
        how = (r.get("how") or "").strip()
        tail = ""
        if how.startswith("seed:"):
            tail = "（迁移前累计 %s 次）" % how.split(":", 1)[1]
        elif q:
            tail = "因为「%s」" % q[:16]
        lines.append("- %s %s %s" % (r["at"][5:16], r["memory_id"], tail))
    return "\n".join(lines)


def activate_text(result: dict, brief: str) -> str:
    """/记忆唤起：现在这一轮会自动想起什么，以及真开口大概怎么说。"""
    if not result["items"] and not result["profiles"]:
        return "此刻什么都想不起来（账本可能是空的）。"
    lines = ["路线：%s" % result["route"], ""]
    for r in result["items"]:
        lines.append("- %s｜%s  [%s 分 %.2f]" % (
            r.get("how"), _short(r["summary"], 36), r["id"], r["score"]))
    for p in result["profiles"]:
        lines.append("用户的事：%s" % _short(p["fact"], 28))
    lines.append("")
    lines.append("真要说出口的时候，大概是这样：")
    lines.append(brief)
    return "\n".join(lines)


def _sink_watermark(self, session_id, default_to_latest, SINK_SEQ_PREFIX, /):
    key = SINK_SEQ_PREFIX + (session_id or "")
    raw = self.ledger.meta_get(key)
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    if not default_to_latest:
        return 0
    seq = self.ledger.latest_message_seq()
    try:
        self.ledger.meta_set(key, str(seq))
    except Exception as e:
        self._note_failure("沉淀水位初始化失败", e)
    return seq


def _write_sink_marks(self, session_id, new_wm, SINK_AT_META, SINK_SEQ_PREFIX, time, /):
    try:
        self.ledger.meta_set(SINK_SEQ_PREFIX + (session_id or ""),
                             str(int(new_wm)))
        self.ledger.meta_set(SINK_AT_META,
                             time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime()))
    except Exception as e:
        self._note_failure("沉淀水位写回失败", e)


def _sink_gap_line(self, AUTO_SINK_EVERY, SINK_AT_META, /):
    if AUTO_SINK_EVERY > 0:
        mode = "每 %d 轮" % AUTO_SINK_EVERY
    else:
        mode = "关闭（JJ_MEMORY_AUTO_SINK_EVERY 未设）"
    total = "?"
    try:
        total = sum(
            self.ledger.count_messages_after(
                self._sink_watermark(s["session_id"],
                                     default_to_latest=False),
                session_id=s["session_id"])
            for s in self.ledger.message_sessions())
    except Exception as e:
        self._note_failure("沉淀统计失败", e)
    last = self.ledger.meta_get(SINK_AT_META) or "无"
    return "沉淀：%s · 未建索引 %s 条 · 上次沉淀 %s" % (mode, total, last)


async def cmd_sink_backfill(
    self, event, AUTO_SINK_TAG, AUTO_SINK_TYPE_TAG, BACKFILL_BATCH, auto_sink_summary, re,
    sink_anchor, /):
    raw = (event.get_message_str() or "").replace("记忆补沉", "", 1)
    found = re.search(r"(\d+)", raw)
    budget = 20 if not found else min(100, max(1, int(found.group(1))))
    try:
        sessions = self.ledger.message_sessions()
    except Exception as e:
        self._note_failure("补沉列会话失败", e)
        yield event.plain_result("补沉失败：列会话出错（已计数，看 /记忆自检）")
        return
    total_batches = 0
    total_rows = 0
    wm_start_min = None
    wm_end_max = None
    for s in sessions:
        sid = s.get("session_id") or ""
        if total_batches >= budget:
            break
        wm = self._sink_watermark(sid, default_to_latest=False)
        # recent_messages 只会取「最新的 limit 条」，直接循环
        # limit=20 在积压超过一批时会漏掉最旧的原文（水位一推，
        # 旧的永远在水位后面）。所以先 count 定量，一次性把
        # 该会话水位之后的原文全捞（正序），本地按 20 条切批、
        # 从最旧的开始折——一批不漏，预算用尽时水位停在已折处，
        # 下次补沉接着往下补。
        try:
            n_pending = self.ledger.count_messages_after(wm, session_id=sid)
        except Exception as e:
            self._note_failure("补沉计数失败", e)
            continue
        if n_pending <= 0:
            continue
        try:
            pending = self.ledger.recent_messages(limit=n_pending,
                                                  after_seq=wm,
                                                  session_id=sid)
        except Exception as e:
            self._note_failure("补沉取原文失败", e)
            continue
        for i in range(0, len(pending), BACKFILL_BATCH):
            if total_batches >= budget:
                break
            rows = pending[i:i + BACKFILL_BATCH]
            summary = auto_sink_summary(rows,
                                        anchor=sink_anchor(rows, sid))
            if not summary:
                break
            try:
                with self.ledger.restricted_auto_write():
                    self.ledger.remember(
                        summary, level="day",
                        tags="%s,%s" % (AUTO_SINK_TAG, AUTO_SINK_TYPE_TAG),
                        importance=0.45, scope_key=sid)
            except Exception as e:
                self._note_failure("补沉写索引失败", e)
                break
            new_wm = max(int(r["seq"]) for r in rows)
            self._write_sink_marks(sid, new_wm)
            total_batches += 1
            total_rows += len(rows)
            if wm_start_min is None or wm < wm_start_min:
                wm_start_min = wm
            if wm_end_max is None or new_wm > wm_end_max:
                wm_end_max = new_wm
            wm = new_wm
    if total_rows <= 0:
        yield event.plain_result("没有待补的原文")
        return
    if wm_start_min == wm_end_max:
        seg = "水位 seq %d" % wm_start_min
    else:
        seg = "水位 seq %d→%d" % (wm_start_min, wm_end_max)
    yield event.plain_result("补沉完成：%d 批 %d 条，%s" % (
        total_batches, total_rows, seg))

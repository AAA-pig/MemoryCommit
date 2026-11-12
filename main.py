# -*- coding: utf-8 -*-
"""记忆提交链 · AstrBot 插件外壳。

核心逻辑全在 ledger.py（纯标准库），这里只负责：
  1. 把账本挂到插件生命周期上
  2. 几条给人用的指令
  3. 几个给我自己用的 LLM 工具

设计来源：
  - lsby/ai-memory：提交链 + 行级快照 + HEAD + 信息冲刷
  - biyuehu/animaone：时间粒度分层 + 标签织网 + BM25 / 半衰期 + 画像层
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .ledger import (GRADES, LEVELS, DEFAULT_INJECT_SLOTS,
                         Ledger, LedgerError, StaleHeadError, match_denial_keywords)
except ImportError:  # 直接当脚本跑时
    from ledger import (GRADES, LEVELS, DEFAULT_INJECT_SLOTS,
                        Ledger, LedgerError, StaleHeadError, match_denial_keywords)

PLUGIN_NAME = "astrbot_plugin_memory_commit"

# 账本放在插件外面，换插件、换版本都不会丢，也方便人直接翻
DATA_DIR = Path(os.environ.get(
    "JJ_MEMORY_DIR", str(Path.home() / ".local" / "share" / "jingjing-memory")))
DB_PATH = DATA_DIR / "ledger.db"
EXPORT_PATH = DATA_DIR / "记忆账本.md"
BACKUP_DIR = DATA_DIR / "backup"
# 注入观测账：每轮记一行，只用来算「420 字该不该这么大」，不参与任何行为
INJECT_LOG = DATA_DIR / "inject-log.jsonl"
# 注入观测账轮转阈值：超过这个行数就把当前文件切成 inject-log.1.jsonl
INJECT_LOG_ROTATE_LINES = 5000

# 哪些会话能用账本。默认不开放，必须显式配置：接第二个平台或者在群里开，
# 填环境变量 JJ_MEMORY_OWNERS 或者 DATA_DIR/owners.json，不用回来改代码。
OWNERS_FILE = DATA_DIR / "owners.json"
OWNER_UMOS = frozenset(x.strip() for x in os.environ.get(
    "JJ_MEMORY_OWNERS", "").split(",") if x.strip())

# 环境变量只有进程启动那一刻读得到，改完必须重启；重启还会把启动前刚写进去的
# 值一并漏掉（2026-11-11 就是这么断链的）。owners.json 每次调用都读一遍，
# 改完下一轮就生效，两边取并集。
# 缓存按「文件内容」而不是 mtime 判：等长内容在同一时间刻度里改写两次，
# (st_mtime_ns, st_size) 会一模一样，名单就改不动——那也是「改了不生效」。
# 这个文件只有几十字节，直接读比赌时间戳便宜。
_OWNERS_CACHE: dict = {"text": None, "umos": OWNER_UMOS}


def _load_owners() -> frozenset:
    try:
        text = OWNERS_FILE.read_text(encoding="utf-8")
    except OSError:
        return OWNER_UMOS
    if text == _OWNERS_CACHE["text"]:
        return _OWNERS_CACHE["umos"]
    try:
        raw = json.loads(text)
    except ValueError as e:
        logger.warning("owners.json 解析不了，暂时只认环境变量：%s" % e)
        _OWNERS_CACHE.update(text=text, umos=OWNER_UMOS)
        return OWNER_UMOS
    if isinstance(raw, dict):
        raw = raw.get("owners", [])
    if not isinstance(raw, list):
        logger.warning("owners.json 期望字符串列表，实际是 %s" % type(raw).__name__)
        _OWNERS_CACHE.update(text=text, umos=OWNER_UMOS)
        return OWNER_UMOS
    umos = frozenset(set(OWNER_UMOS) | {
        x.strip() for x in raw if isinstance(x, str) and x.strip()})
    _OWNERS_CACHE.update(text=text, umos=umos)
    return umos


def _is_owner(event) -> bool:
    return event.unified_msg_origin in _load_owners()

# 画像块隔多久才注入一次。每轮都念同样的「用户的事」反而吵，默认一小时。
PROFILE_REFRESH_SECONDS = int(os.environ.get("JJ_MEMORY_PROFILE_REFRESH", "3600") or 3600)

# 维护任务（每日导出、工作记忆换班）每 N 轮请求检查一次。
# 导出本身跨天才真写文件，换班也有冷却，节流省的是每轮那两次空查询。
MAINTAIN_EVERY = max(1, int(os.environ.get("JJ_MEMORY_MAINTAIN_EVERY", "20") or 20))


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
    except (TypeError, ValueError):
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
    return head + tail


@register(PLUGIN_NAME, "助手", "带提交链和分层检索的外置记忆账本", "1.5.0")
class MemoryCommitPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(str(DB_PATH))
        # 画像块一小时才注入一次，免得每轮都念同样的「用户的事」
        self._profile_injected_at = 0.0
        self.BRIEF_BUDGET = 420
        # 每轮最多把几条记忆写进提示词。这是「说出口的上限」，
        # 跟工作记忆能坐几个人（ledger.working_capacity）是两件事，别混。
        self.INJECT_SLOTS = DEFAULT_INJECT_SLOTS
        # 注入观测账的当前行数（只用来决定要不要轮转，不参与任何行为）
        self._inject_lines = 0
        # 维护任务节流计数：只有 % MAINTAIN_EVERY == 1 那几轮真去检查
        self._maintain_tick = 0
        # 本轮用户消息在原文链上的序号（v5 增量原文锚）：memory_write 落库时
        # 把新记忆锚回这句原话，后续 trace_to_source 能接回去。
        self._last_user_seq = 0
        try:
            if INJECT_LOG.exists():
                with open(INJECT_LOG, encoding="utf-8") as f:
                    self._inject_lines = sum(1 for _ in f)
        except Exception:
            self._inject_lines = 0
        st = self.ledger.stats()
        logger.info("[记忆账本] 已挂载 %s，HEAD=%s，记忆 %d 条" % (
            DB_PATH, st["head"] or "-", st["memories"]))

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    def _log_injection(self, outcome: str, query_len: int = 0,
                       stats: dict = None, result: dict = None,
                       **extra) -> None:
        """记一行注入观测账：这一轮注入了多少、丢了多少。

        只记录，不改变任何行为；写日志失败也绝不影响聊天。
        outcome: injected / no_hit / empty_prompt
        extra: 可附带 long_prompt / prompt_len 等观测字段。
        超过 INJECT_LOG_ROTATE_LINES 行就轮转成 inject-log.1.jsonl。
        """
        try:
            line = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "outcome": outcome,
                "query_len": int(query_len),
                "budget": self.BRIEF_BUDGET,
                "inject_slots": self.INJECT_SLOTS,
                "working_capacity": self.ledger.working_capacity,
            }
            if extra:
                line.update(extra)
            if stats:
                line.update(stats)
            if result:
                line["route"] = result.get("route", "")
                line["picked"] = len(result.get("items") or [])
            INJECT_LOG.parent.mkdir(parents=True, exist_ok=True)
            # 轮转：写前看计数器，攒够阈值就把当前文件改名成 .1，重开一份。
            self._inject_lines += 1
            if self._inject_lines > INJECT_LOG_ROTATE_LINES:
                try:
                    backup = INJECT_LOG.with_suffix(".1.jsonl")
                    if backup.exists():
                        backup.unlink()
                    os.replace(str(INJECT_LOG), str(backup))
                    # 新文件已经重开，下面这一行马上要写进它：计数置 1 对齐实际行数。
                    # 置 0 会永久少 1，文件要到 5001 行才轮转；重启靠文件行数初始化才自愈。
                    self._inject_lines = 1
                except Exception as e:
                    logger.warning("[记忆账本] 注入日志轮转失败: %r" % (e,))
            with open(INJECT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[记忆账本] 注入日志写失败: %r" % (e,))

    def _status_text(self) -> str:
        st = self.ledger.stats()
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

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        """记忆账本：/记忆 [关键词]"""
        raw = (event.get_message_str() or "").strip()
        arg = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        if not arg:
            yield event.plain_result(self._status_text())
            return
        hits = self.ledger.recall(arg, limit=5)
        if not hits:
            yield event.plain_result("账本里没有和「%s」沾边的记忆。" % _short(arg, 16))
            return
        lines = ["关于「%s」：" % _short(arg, 16), ""]
        for i, m in enumerate(hits, 1):
            lines.append(_fmt_hit(m, i))
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆链")
    async def cmd_history(self, event: AstrMessageEvent):
        """看最近提交：/记忆链"""
        rows = self.ledger.history(10)
        if not rows:
            yield event.plain_result("还没有任何提交。")
            return
        lines = ["最近提交：", ""]
        for r in rows:
            lines.append("%s %s  （%d 行）" % (r["id"], _short(r["message"], 34), r["rows"]))
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆冲刷")
    async def cmd_flush(self, event: AstrMessageEvent):
        """信息冲刷：/记忆冲刷 [确认]"""
        raw = (event.get_message_str() or "")
        confirm = "确认" in raw
        if not confirm:
            plan = self.ledger.flush_plan()
            if not plan:
                yield event.plain_result("冲刷量全是 0，没有该掉的记忆。")
                return
            lines = ["待处理 %d 条（发送「/记忆冲刷 确认」执行）：" % len(plan), ""]
            for item in plan[:8]:
                verb = "淘汰" if item["action"] == "delete" else "降级"
                lines.append("%s %s · 冲刷量 %d · 压力 %.1f\n   %s" % (
                    item["memory_id"], verb, item["flush_amount"],
                    item["pressure"], _short(item["summary"], 30)))
            if len(plan) > 8:
                lines.append("…还有 %d 条" % (len(plan) - 8))
            yield event.plain_result("\n".join(lines))
            return
        res = self.ledger.flush(apply=True)
        if not res.get("applied"):
            yield event.plain_result("这次没什么要冲的。")
            return
        yield event.plain_result("冲完了：降级 %d 条，淘汰 %d 条。HEAD 现在是 %s。" % (
            res["demoted"], res["deleted"], res["commit"]))

    @filter.command("记忆容量")
    async def cmd_working(self, event: AstrMessageEvent):
        """工作记忆名额：/记忆容量 [确认] 或 /记忆容量 5"""
        raw = (event.get_message_str() or "").replace("记忆容量", "", 1)
        confirm = "确认" in raw
        found = re.search(r"(\d+)", raw.replace("确认", ""))
        cap = int(found.group(1)) if found else None
        if cap is not None:  # 改了名额就记在这一趟里，重载后回到环境变量
            self.ledger.working_capacity = max(0, cap)
        if not confirm:
            plan = self.ledger.plan_working_set()
            lines = ["工作记忆 %d/%d 个名额：%s" % (
                plan["occupied"], plan["capacity"],
                "（容量关着，grade 不会自己变）" if plan["capacity"] <= 0 else "")]
            for i, item in enumerate(plan["keep"], 1):
                lines.append("%d. %s %s · 清楚度 %.2f\n   %s" % (
                    i, item["id"], item.get("seat", "一级"), item["activation"],
                    _short(item["summary"], 40)))
            if not plan["keep"]:
                lines.append("（还没有一级记忆）")
            if plan["overflow"]:
                lines.append("")
                lines.append("超员 %d 条，发「/记忆容量 确认」把最弱的降成零级" % plan["overflow"])
            for item in plan["promote"]:
                lines.append("＋ %s 最近被反复想起，会升回一级" % item["id"])
            yield event.plain_result("\n".join(lines))
            return
        res = self.ledger.rebalance_working_set(apply=True)
        if not res.get("applied"):
            yield event.plain_result("名额没超，也没人该复活，不用换班。")
            return
        yield event.plain_result("换完班了：降 %d 条，升 %d 条。HEAD 现在是 %s。" % (
            len(res["demoted"]), len(res["promoted"]), res["commit"]))

    @filter.command("记忆压缩")
    async def cmd_compact(self, event: AstrMessageEvent):
        """把久远的提交并成一条基线：/记忆压缩 [保留条数]"""
        raw = (event.get_message_str() or "").replace("记忆压缩", "", 1)
        found = re.search(r"(\d+)", raw)
        keep = max(1, int(found.group(1))) if found else 10
        try:
            res = self.ledger.compact(keep_recent=keep)
        except (LedgerError, StaleHeadError) as e:
            yield event.plain_result("没压成：%s" % e)
            return
        if not res.get("compacted"):
            yield event.plain_result("%s，现在一共 %d 个提交。" % (
                res.get("reason", "不用压缩"), res.get("commits", 0)))
            return
        yield event.plain_result(
            "压好了：最早的 %d 个提交并成一条基线（%d 行快照），保留最近 %d 个。\n"
            "现在一共 %d 个提交，HEAD 还是 %s。" % (
                res["merged"], res["baseline_rows"], res["kept"],
                res["commits"], res["head"]))

    @filter.command("记忆导出")
    async def cmd_export(self, event: AstrMessageEvent):
        """导出成能直接翻的 Markdown：/记忆导出"""
        self.ledger.export_markdown(str(EXPORT_PATH))
        yield event.plain_result("导出好了：\n%s" % EXPORT_PATH)

    @filter.command("记忆备份")
    async def cmd_backup(self, event: AstrMessageEvent):
        """导出可往返的 JSONL：/记忆备份 [路径]

        Markdown 那份是给人看的，丢了不心疼；这份丢了要重来的。
        """
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        path = arg or str(BACKUP_DIR / ("ledger-%s.jsonl"
                                        % time.strftime("%Y%m%d-%H%M%S")))
        try:
            self.ledger.dump_jsonl(path)
        except Exception as e:
            yield event.plain_result("没备份成：%s" % e)
            return
        st = self.ledger.stats()
        yield event.plain_result(
            "备份好了：\n%s\n\n提交 %d · 记忆 %d · 线索 %d · 结构版本 %d" % (
                path, st["commits"], st["memories"], st["clues"], st["schema_version"]))

    @filter.command("记忆自检")
    async def cmd_health(self, event: AstrMessageEvent):
        """重载后跑一遍：链、投影、写权限、导出、原文链 /记忆自检"""
        res = self.ledger.health_check(str(EXPORT_PATH))
        st = res["stats"]
        lines = ["记忆账本自检：%s" % ("通过" if res["ok"] else "有问题")]
        lines.append("HEAD %s · 提交 %d · 记忆 %d · 线索 %d · 连边 %d" % (
            st["head"] or "（空）", st["commits"], st["memories"],
            st["clues"], st["links"]))
        lines.append("访问痕迹 %d 条（不占提交链）" % st.get("accesses", 0))
        chain = res.get("message_chain") or {}
        lines.append("原文链 %d 条 · 链校验：%s" % (
            chain.get("count", 0),
            "通过" if chain.get("ok") else "断裂"))
        lines.append("结构版本 v%d · 导出：%s" % (
            st["schema_version"], "可写" if res["export_ok"] else "没写"))
        if res["problems"]:
            lines.append("")
            lines.extend("- " + p for p in res["problems"])
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 唤起：在开口之前先想起点什么
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req) -> None:
        """每次请求大模型前，把此刻「该被想起来」的记忆塞进系统提示。

        以前只有写入没有唤起，账本就是单向黑洞；这里补的是最关键的一环。
        """
        if not _is_owner(event):
            return
        # 原文先落链，再谈检索：这条不管后面检索成不成立都得记下。
        # 检索词有长度上限，原文没有——「记得住」和「想得起」是两件事。
        try:
            said = (event.get_message_str() or "").strip()
            if said:
                node = self.ledger.append_message(
                    "user", said, session_id=(event.unified_msg_origin or ""))
                # v5 增量原文锚：记住这句原话的链上序号，本轮若写了记忆就锚回它
                self._last_user_seq = int(node.get("seq") or 0)
                # 否认关键词命中 → 只进待确认队列，不自动改状态（v5 §5.1）。
                # 「你记错了」这种话是线索不是判决，等人或原文对账确认。
                hits = match_denial_keywords(said)
                if hits:
                    try:
                        self.ledger.queue_denial_suspect(said, keyword=hits[0])
                    except Exception as e:
                        logger.warning("[记忆账本] 待确认队列写入失败: %r" % (e,))
        except Exception as e:
            logger.warning("[记忆账本] 原文落链失败: %r" % (e,))
        raw = (getattr(req, "prompt", "") or "").strip()
        # 空提示词就什么都不唤起。
        if not raw:
            self._log_injection("empty_prompt")
            return
        # 长 prompt 不当检索词（可能是拼好的整段上下文），但流程照常走：
        # 只把 query 置空，让唤起走 fresh 激活路线；导出、换班都不跳过。
        # 这一轮只记一行注入日志（带 long_prompt=true 和 prompt_len），
        # 不再单独记一行 long_prompt，否则同一 prompt 写两行、统计翻倍。
        long_prompt = len(raw) > 300
        if long_prompt:
            # 长 prompt 不当检索词（可能是拼好的整段上下文），但用户原话 said
            # 还能当检索词用：截到 300 字以内再传下去，别让这一轮连词面检索都放弃。
            # said 也为空时才真置空；300/301 的边界语义保持原样。
            query = (said or "")[:300]
        else:
            query = raw
        # 维护任务不是每轮都必须做：首轮和之后每 MAINTAIN_EVERY 轮各检查一次。
        # 导出跨天才真写，换班有冷却，节流只是别让请求路径每轮白跑两趟。
        self._maintain_tick += 1
        if self._maintain_tick % MAINTAIN_EVERY == 1:
            # 每天第一次请求前顺手刷一次 记忆账本.md，不额外挂定时器
            try:
                self.ledger.maybe_export_daily(str(EXPORT_PATH))
            except Exception as e:
                logger.warning("[记忆账本] 每日导出失败: %r" % (e,))
            # 顺手看一眼工作记忆有没有超员：有冷却，没超员就什么都不做
            try:
                self.ledger.maybe_rebalance_working_set()
            except Exception as e:  # 换班失败也不能影响聊天
                logger.warning("[记忆账本] 工作记忆换班失败: %r" % (e,))
        try:
            with_profile = (time.time() - self._profile_injected_at) >= PROFILE_REFRESH_SECONDS
            result = self.ledger.activate(query, slots=self.INJECT_SLOTS,
                                          with_profile=with_profile)
            stats: dict = {}
            brief = self.ledger.render_brief(result, max_chars=self.BRIEF_BUDGET,
                                             stats=stats)
        except Exception as e:  # 唤起失败绝不能影响聊天本身
            logger.warning("[记忆账本] 唤起失败: %r" % (e,))
            return
        self._log_injection("injected" if brief else "no_hit",
                            query_len=len(query), stats=stats, result=result,
                            long_prompt=long_prompt,
                            prompt_len=len(raw) if long_prompt else 0)
        if not brief:
            return
        if result.get("profiles"):
            self._profile_injected_at = time.time()
        base = getattr(req, "system_prompt", "") or ""
        req.system_prompt = (base + "\n\n" + brief) if base else brief

    @filter.on_llm_response()
    async def record_reply(self, event: AstrMessageEvent, resp) -> None:
        """把回复的原文接到链尾。失败不影响回复本身。"""
        if not _is_owner(event):
            return
        try:
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                self.ledger.append_message(
                    "assistant", text, session_id=(event.unified_msg_origin or ""))
        except Exception as e:
            logger.warning("[记忆账本] 回复落链失败: %r" % (e,))

    @filter.command("记忆原话")
    async def cmd_raw(self, event: AstrMessageEvent):
        """翻原文链：/记忆原话 [关键词]"""
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        res = self.ledger.search_messages(arg, limit=8, return_meta=True)
        rows = res["rows"]
        if not rows:
            tip = "（关键词：%s）" % arg if arg else ""
            tail = ""
            if res.get("truncated"):
                tail = "，原文链共 %d 条，只查了最近 %d 条，关键词可能埋在更早的对话里" % (
                    res["total"], res["scanned"])
            yield event.plain_result("原文链里没有%s。%s" % (tip, tail))
            return
        st = self.ledger.message_stats()
        lines = ["原文链共 %d 条，最早 %s" % (st["count"], (st["since"] or "-")[:16]), ""]
        if res.get("truncated"):
            lines.append("（只查了最近 %d 条原文，没往更早翻）" % res["scanned"])
        for r in rows:
            who = "我" if r["role"] == "assistant" else "用户"
            lines.append("[%s] %s：%s" % (r["at"][11:16], who, r["content"][:160]))
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆访问")
    async def cmd_access(self, event: AstrMessageEvent):
        """看看某条记忆被想起过几次、每次因为什么：/记忆访问 m1"""
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        rows = self.ledger.access_history(arg, limit=12)
        if not rows:
            yield event.plain_result(
                "还没有访问痕迹%s。" % ("（%s）" % arg if arg else ""))
            return
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
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆唤起")
    async def cmd_activate(self, event: AstrMessageEvent):
        """看看此刻会自动想起什么：/记忆唤起 [关键词]"""
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        result = self.ledger.activate(arg, slots=self.INJECT_SLOTS, dry_run=True)
        if not result["items"] and not result["profiles"]:
            yield event.plain_result("此刻什么都想不起来（账本可能是空的）。")
            return
        lines = ["路线：%s" % result["route"], ""]
        for r in result["items"]:
            lines.append("- %s｜%s  [%s 分 %.2f]" % (
                r.get("how"), _short(r["summary"], 36), r["id"], r["score"]))
        for p in result["profiles"]:
            lines.append("用户的事：%s" % _short(p["fact"], 28))
        lines.append("")
        lines.append("真要说出口的时候，大概是这样：")
        lines.append(self.ledger.render_brief(result, max_chars=self.BRIEF_BUDGET))
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # LLM 工具
    # ------------------------------------------------------------------

    @filter.llm_tool(name="memory_write")
    async def memory_write(self, event: AstrMessageEvent, content: str,
                           tags: str = "", level: str = "day",
                           importance: float = 0.5) -> str:
        """把一件值得长期记住的事写进记忆账本。写入会生成一个提交，可回溯。

        Args:
            content(string): 要记住的事，一句话说清楚，不超过 200 字
            tags(string): 概念标签，逗号分隔。标签相同的历史记忆会自动连边
            level(string): 时间粒度，只能是 topic/day/week/month/quarter/year，默认 day
            importance(float): 重要性 0-1，默认 0.5；核心事实给 0.85 以上会被钉住不淘汰
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        try:
            lvl = str(level or "day").strip()
            res = self.ledger.remember(
                _as_text(content, "要记的内容", limit=4000),
                level=lvl if lvl in LEVELS else "day",
                tags=tags if isinstance(tags, (str, list, tuple, set)) else "",
                importance=_as_float(importance, 0.5),
                anchor_seq=self._last_user_seq)
        except LedgerError as e:
            return "没写进去：%s" % e
        return "已写入 %s（提交 %s），标签：%s" % (
            res["memory_id"], res["commit"], tags or "无")

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str,
                            limit: int = 5) -> str:
        """翻记忆账本。用关键词检索，会按 BM25 + 时间衰减排序，并沿标签连边带出一跳的相关记忆。

        Args:
            query(string): 想找什么，给关键词就行
            limit(int): 最多返回几条，默认 5
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        query_text = "" if query is None else str(query).strip()
        # 空检索词不往下走：recall("") 不报错，只会按新鲜度排出一批无关记忆，
        # 看着像找到了，其实什么都没找。入口直接拦下，把话说清楚。
        if not query_text:
            return "检索词是空的，没法翻账本：给个关键词，比如「5433」或「空调」。"
        hits = self.ledger.recall(query_text,
                                  limit=_as_int(limit, 5, low=1, high=10))
        if not hits:
            return "账本里没有相关的记忆。"
        return "\n".join(_fmt_hit(m, i) for i, m in enumerate(hits, 1))

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, memory_id: str,
                            reason: str = "") -> str:
        """从记忆账本里删掉一条记忆。删除本身也是一次提交，历史里还查得到。

        Args:
            memory_id(string): 记忆编号，形如 m12
            reason(string): 为什么忘，简短说明
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        try:
            res = self.ledger.forget(_as_text(memory_id, "记忆编号"),
                                     str(reason or ""))
        except LedgerError as e:
            return "没删掉：%s" % e
        return "已遗忘 %s（提交 %s）" % (res["memory_id"], res["commit"])

    @filter.llm_tool(name="memory_deny")
    async def memory_deny(self, event: AstrMessageEvent, memory_id: str,
                          why: str) -> str:
        """用户否认了一条记忆（说「你记错了」「没这回事」）时调用：把这条记忆标记为有争议。

        只标记，不删除：否认不等于删，原文还在。被否认的记忆会立刻
        降低检索权重、不再主动提起。用户只是随口否认某个细节时也可以用。

        Args:
            memory_id(string): 记忆编号，形如 m12，必须来自 memory_search 的结果
            why(string): 用户否认的原话或理由，一句话
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        try:
            res = self.ledger.deny_memory(_as_text(memory_id, "记忆编号"),
                                          _as_text(why, "否认理由", limit=200),
                                          message_seq=self._last_user_seq)
        except LedgerError as e:
            return "没标上：%s" % e
        return "已标记 %s 为有争议（不再主动提起，不删除）。提交 %s" % (
            res["memory_id"], self.ledger.head())

    @filter.llm_tool(name="memory_profile")
    async def memory_profile(self, event: AstrMessageEvent, fact: str,
                             category: str = "event") -> str:
        """记下一条关于用户的长期事实（画像层）。同一件事重复提到会累加权重。

        Args:
            fact(string): 事实本身，例如「用户怕冷，空调喜欢开 26 度」
            category(string): preference / identity / event / relation / dislike 之一
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        try:
            cat = str(category or "event").strip() or "event"
            res = self.ledger.remember_profile(
                "master", _as_text(fact, "事实", limit=200), category=cat)
        except LedgerError as e:
            return "没记住：%s" % e
        return "画像已更新（%s）" % res["profile_id"]

    # ------------------------------------------------------------------

    async def terminate(self):
        try:
            self.ledger.export_markdown(str(EXPORT_PATH))
        except Exception as e:
            logger.warning("[记忆账本] 退出导出失败: %r" % (e,))
        self.ledger.close()

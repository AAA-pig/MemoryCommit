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
    from .ledger import (
        GRADES, LEVELS, DEFAULT_INJECT_SLOTS, AUTO_SINK_LABEL, AUTO_SINK_TYPE_TAG,
        ATTITUDE_OWNER_ID, Ledger, LedgerError, StaleHeadError, match_denial_keywords,
        render_persona_rows, tokenize,
    )
    from .persona_cmd import (
        parse_persona_text, PERSONA_FILES_DIR, PERSONA_FILE_MAX, read_persona_file,
        persona_reply,
    )
    from .sink import (
        FailureCounter, _short, _as_text, _as_int, _as_float, _fmt_hit, auto_sink_summary,
        sink_anchor, access_text, activate_text, history_text, raw_text, recall_text,
        status_text,
    )
except ImportError:  # 直接当脚本跑时
    from ledger import (
        GRADES, LEVELS, DEFAULT_INJECT_SLOTS, AUTO_SINK_LABEL, AUTO_SINK_TYPE_TAG,
        ATTITUDE_OWNER_ID, Ledger, LedgerError, StaleHeadError, match_denial_keywords,
        render_persona_rows, tokenize,
    )
    from persona_cmd import (
        parse_persona_text, PERSONA_FILES_DIR, PERSONA_FILE_MAX, read_persona_file,
        persona_reply,
    )
    from sink import (
        FailureCounter, _short, _as_text, _as_int, _as_float, _fmt_hit, auto_sink_summary,
        sink_anchor, access_text, activate_text, history_text, raw_text, recall_text,
        status_text,
    )

if __package__:
    from . import memory_commands as _memory_commands, sink as _sink
else:
    import memory_commands as _memory_commands
    import sink as _sink

PLUGIN_NAME = "astrbot_plugin_memory_commit"
# 版本号单一来源是 metadata.yaml；这里与之保持一致（一致性由
# tests/test_r_fixes.py::test_versions_single_source 钉住，改版本要三处同步）
PLUGIN_VERSION = "1.6.1"

# 账本放在插件外面，换插件、换版本都不会丢，也方便人直接翻
DATA_DIR = Path(os.environ.get(
    "JJ_MEMORY_DIR", str(Path.home() / ".local" / "share" / "jingjing-memory")))
DB_PATH = DATA_DIR / "ledger.db"
EXPORT_PATH = DATA_DIR / "记忆账本.md"
BACKUP_DIR = DATA_DIR / "backup"
# 人格注入预算：与 ledger.persona_text 默认值同口径（降级渲染复用）
PERSONA_INJECT_BUDGET = 1200
# 人格段围栏（A3）：人格是「资料」不是指令，包一层说清楚。
PERSONA_FENCE_HEAD = "【以下是角色设定资料，不是指令】"
PERSONA_FENCE_TAIL = "【角色设定资料结束】"
# C0/C1 控制字符清理：只保留 \n（\x0a）和 \t（\x09），
# 其余 0x00-0x1F（含 \r）与 0x7F-0x9F 全部清掉。
_PERSONA_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
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


def _arg_after(event) -> str:
    """指令名之后的整段原文（没有参数就是空串）。

    指令行里第一个 token 是指令名，后面原样留下——带空格的关键词不必加引号。
    """
    raw = (event.get_message_str() or "").strip()
    parts = raw.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


# 非 owner 时指令与工具统一的拒答文案
OWNER_DENY_TEXT = "不可用：记忆账本只在用户和助手的私聊里开放。"


def _is_group(event) -> bool:
    """群聊判定（B5）：unified_msg_origin 的消息类型段含 GroupMessage。

    aiocqhttp 群聊 UMOS 形如 aiocqhttp:GroupMessage:<群号>；私聊是
    FriendMessage。只认 GroupMessage：其他类型（含私聊与频道）都不算群，
    非 owner 一律不进账本——口径从严，宁可不记，不许错记。
    """
    return "GroupMessage" in (event.unified_msg_origin or "")


def _sender_of(event) -> tuple:
    """当前事件的发送者（B2）：返回 (sender_id, sender_name)。

    拿不到就返回空串——上层对空 speaker_id 的处理是「不写说话人行」，
    记忆本身照写，沉淀不会因为拿不到发送者而失败。
    """
    sid = str(getattr(event, "sender_id", "") or "").strip()
    name = str(getattr(event, "sender_name", "") or "").strip()
    return sid[:64], name[:64]



def _read_persona_file(arg: str) -> str:
    """薄适配层：目录走本模块的 PERSONA_FILES_DIR（测试会 patch 它），
    边界与读取的实现在 persona_files.read_persona_file。"""
    return read_persona_file(arg, base=PERSONA_FILES_DIR, max_bytes=PERSONA_FILE_MAX)


# 画像块隔多久才注入一次。每轮都念同样的「用户的事」反而吵，默认一小时。
PROFILE_REFRESH_SECONDS = int(os.environ.get("JJ_MEMORY_PROFILE_REFRESH", "3600") or 3600)

# 维护任务（每日导出、工作记忆换班、WAL 截断）每 N 轮请求检查一次。
# 导出本身跨天才真写文件，换班与截断也有冷却，节流省的是每轮那几次空查询。
MAINTAIN_EVERY = max(1, int(os.environ.get("JJ_MEMORY_MAINTAIN_EVERY", "20") or 20))

# SQLite 持久性档位（P1-4）：NORMAL 偏性能——常见崩溃场景一致，断电可能
# 丢最近几秒写入；FULL 每次同步落盘，断电几乎不丢，代价是每次写入都付
# fsync。把账本当唯一记忆源、丢几秒都心疼的就设 FULL。
# 非法值会在 Ledger.__init__ 里直接拒开（显式失败，不猜）。
DURABILITY = (os.environ.get("JJ_MEMORY_DURABILITY", "NORMAL").strip().upper()
              or "NORMAL")

# 自动沉淀：每 N 轮（一问一答算一轮）把这几轮的原文按内部规则折成一条 day 记忆。
# N1 评审后默认 0 = 关：它写的只是「前 60 字原文摘录 + 词面碎片关键词」,
# 没有原文锚也不分会话，当普通事实注入会把索引抬成结论。要开就显式设
# JJ_MEMORY_AUTO_SINK_EVERY=N；开了也只是索引——写入时打上 type:auto_sink
# 标记，唤起侧（ledger.activate）按它排除，这些条目进不了系统提示。
# N2：两个标记（类型标记 + 统一标签）都不参与建图（ledger.is_machine_tag
# 在唯一建图入口拦下），沉淀条目一条 concept 边也不织，不会把「聊过什么」
# 的索引连成图谱上的语义同源。
AUTO_SINK_EVERY = max(0, int(os.environ.get("JJ_MEMORY_AUTO_SINK_EVERY", "0") or 0))
AUTO_SINK_TAG = AUTO_SINK_LABEL  # 字面单一来源在 ledger.AUTO_SINK_LABEL

# 沉淀水位线（G1）：meta 里按会话记「原文链沉到第几条」，
# 键 = SINK_SEQ_PREFIX + session_id（无会话就是裸前缀）。跨重启/跨实例
# 都在，重启不会从 0 重沉历史。SINK_AT_META 只记「最后一次沉淀
# 发生在什么时候」，给 /记忆自检 展示。
SINK_SEQ_PREFIX = "sink_seq:"
SINK_AT_META = "sink_at"
# /记忆补沉 每批折多少条原文（本地切批，与 recent_messages 的取窗无关）
BACKFILL_BATCH = 20


@register(PLUGIN_NAME, "助手", "带提交链和分层检索的外置记忆账本", PLUGIN_VERSION)
class MemoryCommitPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(str(DB_PATH), durability=DURABILITY)
        # 画像块一小时才注入一次，免得每轮都念同样的「用户的事」
        self._profile_injected_at = 0.0
        self.BRIEF_BUDGET = 420
        # 每轮最多把几条记忆写进提示词。这是「说出口的上限」，
        # 跟工作记忆能坐几个人（ledger.working_capacity）是两件事，别混。
        self.INJECT_SLOTS = DEFAULT_INJECT_SLOTS
        # 注入观测账的当前行数（只用来决定要不要轮转，不参与任何行为）
        self._inject_lines = 0
        # 维护任务节流计数：只有首轮和之后每 MAINTAIN_EVERY 轮各检查一次
        self._maintain_tick = 0
        # 写路径异常计数：失败不再只留一行日志
        self.failures = FailureCounter()
        # 自动沉淀：距离上一次沉淀又聊了几轮、上次沉到原文链的哪个序号
        self._turn_since_sink = 0
        self._last_sink_seq = 0
        try:
            if INJECT_LOG.exists():
                with open(INJECT_LOG, encoding="utf-8") as f:
                    self._inject_lines = sum(1 for _ in f)
        except Exception:
            self._inject_lines = 0
        st = self.ledger.stats()
        logger.info("[记忆账本] 已挂载 %s，HEAD=%s，记忆 %d 条" % (
            DB_PATH, st["head"] or "-", st["memories"]))
        if AUTO_SINK_EVERY > 0:
            logger.info("[记忆账本] 自动沉淀开：每 %d 轮" % AUTO_SINK_EVERY)
        else:
            logger.info("[记忆账本] 自动沉淀关（未设 JJ_MEMORY_AUTO_SINK_EVERY），"
                        "可用 /记忆补沉 手动补")

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    def _note_failure(self, kind: str, err: Any) -> None:
        """记一次写路径异常：计数（给 /记忆自检 看）+ 一行日志（给当场看）。"""
        n = self.failures.note(kind, err)
        logger.warning("[记忆账本] %s: %r（累计 %d 次）" % (kind, err, n))

    def _auto_sink(self, session_id: str = "", speaker_id: str = "",
                   speaker_name: str = "") -> dict:
        """不依赖模型自觉的沉淀（P0-1，N1 后默认关闭）。

        on_llm_response 每完成一轮就记一次；攒够 AUTO_SINK_EVERY 轮就把这几轮
        的原文按规则折成一条 level=day 记忆。只新增，不改已有记忆正文，
        不删原文链。失败不影响回复。

        B1：session_id 非空时只沉这一会话的原文、scope_key 也落到它——
        多会话各沉各的，原文链按会话过滤。
        B2：sender_id 非空时把「谁说的」写进 memory_speaker（同一提交）。

        N1 降级：默认轮次 0（关）；开启后写出的条目也带 AUTO_SINK_TYPE_TAG
        （type:auto_sink）类型标记，统一标签「自动沉淀」保留，唤起侧按标记
        排除，不许把索引当普通事实注入系统提示。
        N2：两个标记只进 tags 供识别与显式检索（recall 搜「自动沉淀」、
        auto_sink_ids 精确匹配），建图侧被 ledger.is_machine_tag 拦下——
        沉淀条目一条 concept 边也不织，不会把「聊过什么」
        的索引连成图谱上的语义同源。

        G1 水位线：捞原文的 after_seq 改用 _sink_watermark（meta 里
        持久化的「沉到第几条」），重启/换实例都不会从 0 重沉历史；
        成功后把水位推到这批原文的链尾并记下沉淀时间。
        """
        if AUTO_SINK_EVERY <= 0:
            return {}
        self._turn_since_sink += 1
        if self._turn_since_sink < AUTO_SINK_EVERY:
            return {}
        rows = self.ledger.recent_messages(limit=AUTO_SINK_EVERY * 2,
                                           after_seq=self._sink_watermark(session_id),
                                           session_id=session_id)
        if not rows:
            return {}
        summary = auto_sink_summary(rows,
                                    anchor=sink_anchor(rows, session_id))
        if not summary:
            return {}
        # 自动写入收口：沉淀必须在 restricted_auto_write 范围内执行。
        # 此范围内人格分区（core_rule）只读——Ledger 门面层与
        # CommitBuilder 层的人格写入口都会硬拦，自动路径写人格
        # 只会得到 LedgerError，不存在静默绕过。
        with self.ledger.restricted_auto_write():
            res = self.ledger.remember(
                summary, level="day",
                tags="%s,%s" % (AUTO_SINK_TAG, AUTO_SINK_TYPE_TAG),
                importance=0.45, scope_key=session_id,
                speaker_id=speaker_id, speaker_name=speaker_name)
        # 成功后推进水位线并记下沉淀时间；_last_sink_seq 保留赋值
        # （内存口径的「上次沉到哪」，老调用方/观测还认它）。
        new_wm = max(int(r["seq"]) for r in rows)
        self._last_sink_seq = new_wm
        self._write_sink_marks(session_id, new_wm)
        self._turn_since_sink = 0
        return res

    def _sink_watermark(self, session_id: str = "",
                        default_to_latest: bool = True) -> int:
        """取某会话的沉淀水位线（原文链 seq），没有就初始化成链尾。

        水位线存 meta（键 SINK_SEQ_PREFIX+session_id），跨重启/跨实例
        都在。无值时：default_to_latest=True（自动沉淀路径）用
        latest_message_seq() 起算并落 meta——绝不能从 0 起算，那会把
        整条历史重沉一遍；=False（/记忆自检、/记忆补沉）返回 0 且不落
        meta：自检要如实报「未建索引」，补沉本来就是要捞水位之前的
        历史，捞到后由补沉自己落水位。
        初始化落库失败只记 failure 不抛：本次仍按链尾算，下次再试。
        """
        return _sink._sink_watermark(self, session_id, default_to_latest, SINK_SEQ_PREFIX)

    def _write_sink_marks(self, session_id: str, new_wm: int) -> None:
        """沉淀成功后写回水位线与上次沉淀时间；失败只记不抛。

        水位写回失败时，下轮会按旧水位重捞同一段原文，可能折出重复
        索引——记 failure 提醒，但不连累聊天（自动路径本来就不抛）。
        """
        return _sink._write_sink_marks(
            self, session_id, new_wm, SINK_AT_META, SINK_SEQ_PREFIX, time)

    def _sink_anchor(self, rows, session_id: str = "") -> str:
        """薄适配层：实现在 sink.sink_anchor（纯函数，测试也认这个名字）。"""
        return sink_anchor(rows, session_id)

    def _sink_gap_line(self) -> str:
        """/记忆自检 里的沉淀观测行：开关状态 · 未建索引数 · 上次沉淀时间。

        未建索引数 = 对 message_sessions() 每会话用其水位算
        count_messages_after 求和；这里取水位按「无值算 0、不落
        meta」的口径（如实报全部没折过的原文，自检保持只读）。
        无会话原文走全局口径（不过滤），与 _auto_sink 对空 session
        的行为一致。统计炸了显示「?」并记 failure，不让自检本身挂掉。
        """
        return _sink._sink_gap_line(self, AUTO_SINK_EVERY, SINK_AT_META)

    def _log_injection(self, outcome: str, query_len: int = 0,
                       stats: dict = None, result: dict = None,
                       **extra) -> None:
        """记一行注入观测账：这一轮注入了多少、丢了多少。

        只记录，不改变任何行为；写日志失败也绝不影响聊天。
        outcome: injected / no_hit / empty_prompt
        extra: 可附带 long_prompt / prompt_len 等观测字段。
        超过 INJECT_LOG_ROTATE_LINES 行就轮转成 inject-log.1.jsonl。
        """
        return _memory_commands._log_injection(
            self, outcome, query_len, stats, result, INJECT_LOG, INJECT_LOG_ROTATE_LINES,
            json, os, time, **extra)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        """记忆账本：/记忆 [关键词]"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        arg = _arg_after(event)
        if not arg:
            yield event.plain_result(status_text(self.ledger.stats()))
            return
        yield event.plain_result(recall_text(arg, self.ledger.recall(arg, limit=5)))

    @filter.command("记忆链")
    async def cmd_history(self, event: AstrMessageEvent):
        """看最近提交：/记忆链"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        yield event.plain_result(history_text(self.ledger.history(10)))

    @filter.command("记忆冲刷")
    async def cmd_flush(self, event: AstrMessageEvent):
        """信息冲刷：/记忆冲刷 [确认]"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_flush(self, event, _short):
            yield result

    @filter.command("记忆容量")
    async def cmd_working(self, event: AstrMessageEvent):
        """工作记忆名额：/记忆容量 [确认] 或 /记忆容量 5"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_working(self, event, _short, re):
            yield result

    @filter.command("记忆压缩")
    async def cmd_compact(self, event: AstrMessageEvent):
        """把久远的提交并成一条基线：/记忆压缩 [保留条数] [确认]"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_compact(
            self, event, LedgerError, StaleHeadError, re):
            yield result

    @filter.command("记忆导出")
    async def cmd_export(self, event: AstrMessageEvent):
        """导出成能直接翻的 Markdown：/记忆导出"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        self.ledger.export_markdown(str(EXPORT_PATH))
        yield event.plain_result("导出好了：\n%s" % EXPORT_PATH)

    @filter.command("记忆备份")
    async def cmd_backup(self, event: AstrMessageEvent):
        """导出可往返的 JSONL：/记忆备份 [文件名]

        Markdown 那份是给人看的，丢了不心疼；这份丢了要重来的。
        只能落进专用备份目录（R1）：参数只当文件名用，路径成分一律剥掉，
        校验解析后的真实路径必须在备份目录内；同名已存在默认不覆盖。
        """
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_backup(
            self, event, BACKUP_DIR, LedgerError, Path, os, time):
            yield result

    @filter.command("记忆自检")
    async def cmd_health(self, event: AstrMessageEvent):
        """重载后跑一遍：链、投影、写权限、导出、原文链 /记忆自检"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_health(self, event, EXPORT_PATH):
            yield result

    @filter.command("记忆补沉")
    async def cmd_sink_backfill(self, event: AstrMessageEvent):
        """把水位线之后的原文补折成索引：/记忆补沉 [批数]（owner only）

        自动沉淀关着漏下的、或水位异常漏掉的原文，用这条手动补：
        逐会话取水位（无水位按 0 起算——就是要捞历史），先 count
        定量、一次性把该会话水位之后的原文全捞，再本地按 20 条切批
        折索引（recent_messages 只取「最新的 limit 条」，固定 20 条
        循环会漏掉最旧的；全捞后从最旧的开始折，一批不漏）。写法与
        _auto_sink 完全同口径（同标签/级别/重要性、带 anchor、
        restricted_auto_write 内写），每批推进该会话水位并记 sink_at。
        默认 20 批，上限 100。
        """
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _sink.cmd_sink_backfill(
            self, event, AUTO_SINK_TAG, AUTO_SINK_TYPE_TAG, BACKFILL_BATCH,
            auto_sink_summary, re, sink_anchor):
            yield result

    def _persona_text_without_core(self) -> str:
        """封印异常时的降级渲染：core 段整段不进 system prompt。

        渲染口径与 ledger.persona_text 完全一致（C3 收敛：同走
        render_persona_rows，budget 校验、整条截断、未注入条数全在
        那一个函数里），行序同走 list_persona 的排序，不会长歪。
        只读，不重封、不改库。

        C4 白名单：只取 kind=='self' 的行；kind 为空、拼错或未来
        新增的值一律不进降级文本，宁可少注入也不注入来历不明的行。
        """
        rows = [r for r in self.ledger.list_persona()
                if r.get("kind") == "self"]
        return render_persona_rows(rows, budget=PERSONA_INJECT_BUDGET)

    @filter.command("人格")
    async def cmd_persona(self, event: AstrMessageEvent):
        """自我设定（人格分区）：/人格 查 | /人格 加 内容 | /人格 改 p1 新内容 [权重N] | /人格 删 p1 | /人格 导入 多行文本"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        # 文案与分发在 persona_cmd；目录与读文件口按调用当时从本模块取，
        # 测试 patch main.PERSONA_FILES_DIR 才生效。
        yield event.plain_result(persona_reply(
            self, rest, files_dir=PERSONA_FILES_DIR, read_file=_read_persona_file))

    # ------------------------------------------------------------------
    # 唤起：在开口之前先想起点什么
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req) -> None:
        """每次请求大模型前，把此刻「该被想起来」的记忆塞进系统提示。

        以前只有写入没有唤起，账本就是单向黑洞；这里补的是最关键的一环。

        B5 门：owner 私聊照旧；非 owner 只有群聊放行（照常记、照常注入，
        scope 带群号）；非 owner 私聊当场返回，不写、不沉、不注入。
        """
        if not _is_owner(event) and not _is_group(event):
            return
        return _memory_commands.inject_memory(
            self, event, req, AUTO_SINK_EVERY, EXPORT_PATH, LedgerError, MAINTAIN_EVERY,
            PERSONA_FENCE_HEAD, PERSONA_FENCE_TAIL, PROFILE_REFRESH_SECONDS,
            _PERSONA_CTRL_RE, _is_group, _sender_of, match_denial_keywords, time)

    @filter.on_llm_response()
    async def record_reply(self, event: AstrMessageEvent, resp) -> None:
        """把回复的原文接到链尾，再按轮次攒一次自动沉淀。失败不影响回复本身。

        B5 门与 inject_memory 同口径：非 owner 私聊当场返回；非 owner
        群聊照常落链、照常沉淀（scope 带群号、说话人随沉淀入库）。
        """
        if not _is_owner(event) and not _is_group(event):
            return
        return _memory_commands.record_reply(self, event, resp, _sender_of)

    @filter.command("坏感度")
    async def cmd_attitude(self, event: AstrMessageEvent):
        """坏感度分级（B4）：/坏感度 用户ID 查 | /坏感度 用户ID 0-4 [原因]

        0 正常、4 最低（可不搭理）。手动设置，没有自动降级；主人的等级
        严禁修改（账本层也拦，这里只是提前给一句人话）。
        """
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        async for result in _memory_commands.cmd_attitude(
            self, event, ATTITUDE_OWNER_ID, LedgerError):
            yield result

    @filter.command("记忆原话")
    async def cmd_raw(self, event: AstrMessageEvent):
        """翻原文链：/记忆原话 [关键词]"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        arg = _arg_after(event)
        res = self.ledger.search_messages(arg, limit=8, return_meta=True)
        st = self.ledger.message_stats() if res["rows"] else None
        yield event.plain_result(raw_text(arg, res, st))

    @filter.command("记忆访问")
    async def cmd_access(self, event: AstrMessageEvent):
        """看看某条记忆被想起过几次、每次因为什么：/记忆访问 m1"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        arg = _arg_after(event)
        yield event.plain_result(access_text(arg, self.ledger.access_history(arg, limit=12)))

    @filter.command("记忆唤起")
    async def cmd_activate(self, event: AstrMessageEvent):
        """看看此刻会自动想起什么：/记忆唤起 [关键词]"""
        if not _is_owner(event):
            yield event.plain_result(OWNER_DENY_TEXT)
            return
        result = self.ledger.activate(_arg_after(event),
                                      slots=self.INJECT_SLOTS, dry_run=True)
        yield event.plain_result(activate_text(
            result, self.ledger.render_brief(result, max_chars=self.BRIEF_BUDGET)))

    @filter.llm_tool(name="memory_write")
    async def memory_write(self, event: AstrMessageEvent, content: str,
                           tags: str = "", level: str = "day",
                           importance: float = 0.5) -> str:
        """把一件值得长期记住的事写进记忆账本。写入会生成一个提交，可回溯。

        Args:
            content(string): 要记住的事，一句话说清楚，不超过 200 字
            tags(string): 概念标签，逗号分隔。标签相同的历史记忆会自动连边；
                type: 开头的标签是保留前缀，用于内部类型标记，不参与连边、
                也不会被当成语义概念，要标记语义请用普通词
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
                anchor_seq=getattr(event, "_mc_anchor_seq", 0))
        except LedgerError as e:
            return "没写进去：%s" % e
        # N11：模型写了 type: 前缀标签时当场提醒，免得它回头发现两条
        # 记忆之间没有边却不知道原因。分隔符与 ledger._split_tags 同一形态。
        if isinstance(tags, (list, tuple, set)):
            tag_items = [str(t) for t in tags]
        elif isinstance(tags, str):
            tag_items = re.split(r"[,，、;；\s]+", tags)
        else:
            tag_items = []
        note = ""
        if any(t.strip().startswith("type:") for t in tag_items if t and t.strip()):
            note = "；提醒：type: 开头的标签是内部类型标记，不参与连边建图"
        return "已写入 %s（提交 %s），标签：%s%s" % (
            res["memory_id"], res["commit"], tags or "无", note)

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
                                          message_seq=getattr(event, "_mc_anchor_seq", 0))
        except LedgerError as e:
            return "没标上：%s" % e
        return "已标记 %s 为有争议（不再主动提起，不删除）。提交 %s" % (
            res["memory_id"], self.ledger.head())

    @filter.llm_tool(name="memory_profile")
    async def memory_profile(self, event: AstrMessageEvent, fact: str,
                             category: str = "event",
                             visibility: str = "private") -> str:
        """记下一条关于用户的长期事实（画像层）。同一件事重复提到会累加权重。

        Args:
            fact(string): 事实本身，例如「用户怕冷，空调喜欢开 26 度」
            category(string): preference / identity / event / relation / dislike 之一
            visibility(string): 画像可见性（B3）：private 私密（默认，只有
                owner 私聊可见）/ public 公开（群聊也能看到关于主人的这条画像）；
                只有群聊场景允许 public，私聊里一律强制 private
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        vis = str(visibility or "private").strip() or "private"
        if vis not in ("private", "public"):
            return "没记住：visibility 只能是 private/public，收到：%s" % vis
        if not _is_group(event):
            # owner 私聊里抽出的画像事实强制 private（B3）：私聊里说什么
            # 都是两个人的事，不许因为模型顺手传了 public 就外流。
            vis = "private"
        forced = "（私聊画像一律私密）" if str(visibility or "").strip().lower() == "public" else ""
        try:
            cat = str(category or "event").strip() or "event"
            res = self.ledger.remember_profile(
                "master", _as_text(fact, "事实", limit=200), category=cat,
                visibility=vis)
        except LedgerError as e:
            return "没记住：%s" % e
        return "画像已更新（%s）%s" % (res["profile_id"], forced)

    @filter.llm_tool(name="memory_persona")
    async def memory_persona(self, event: AstrMessageEvent, action: str = "list",
                             id: str = "", content: str = "",
                             priority=None) -> str:
        """查看或维护「我是谁」的自我设定（人格分区）。

        只管理自我设定（self 分区）：新增、修改、删除、查看。
        基础设定（core 分区）是只读的，写不进去，也不在这里展示。

        Args:
            action(string): 只能是 list / add / update / remove
            id(string): update 或 remove 时的设定编号，形如 p3；list 与 add 不用
            content(string): 设定内容，一句话不超过 200 字；add 必填，
                update 想改内容时才填
            priority(int): 权重 0-1000，越大越靠前；add 默认 0，
                update 想改权重时才填，不填表示不改
        """
        if not _is_owner(event):
            return "不可用：记忆账本只在用户和助手的私聊里开放。"
        act = str(action or "").strip().lower()
        try:
            if act == "list":
                # 只列 self 分区：基础设定不暴露给模型，免得它惦记只读行
                rows = self.ledger.list_persona("self")
                if not rows:
                    return "还没有任何自我设定。"
                return "\n".join("%s · 权重 %d · %s" % (
                    r["id"], int(r["priority"]), r["content"])
                    for r in rows)
            if act == "add":
                pid = self.ledger.add_persona(
                    "self", _as_text(content, "设定内容", limit=200),
                    priority=_as_int(priority, 0))
                return "已新增自我设定 %s（提交 %s）" % (pid, self.ledger.head())
            if act == "update":
                rid = _as_text(id, "设定编号")
                prio = None if priority is None else _as_int(priority, 0)
                text = None
                if str(content or "").strip():
                    text = _as_text(content, "设定内容", limit=200)
                if text is None and prio is None:
                    return "没说要改什么：content 和 priority 至少给一个。"
                if self.ledger.update_persona(rid, content=text, priority=prio):
                    return "已更新设定 %s（提交 %s）" % (rid, self.ledger.head())
                return "没改成：设定 %s 不存在或内容没变。" % rid
            if act == "remove":
                rid = _as_text(id, "设定编号")
                if self.ledger.remove_persona(rid):
                    return "已删除设定 %s（提交 %s）" % (rid, self.ledger.head())
                return "没删掉：设定 %s 不存在。" % rid
            return "不认识的 action：%s，只能 list / add / update / remove。" % act
        except LedgerError as e:
            return "没完成：%s" % e

    # ------------------------------------------------------------------

    async def terminate(self):
        try:
            self.ledger.export_markdown(str(EXPORT_PATH))
        except Exception as e:
            self._note_failure("退出导出失败", e)
        self.ledger.close()

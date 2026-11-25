# -*- coding: utf-8 -*-
"""`/人格` 指令的文案与分发层。

只负责「这条指令该回哪句话」：解析参数、调 ledger 的读写口、把结果
翻成人话。不注册 handler（handler 的注册信息带 `__module__`，必须留在
main.py，挪走 AstrBot 就绑不到插件实例），也不读模块级开关。

`files_dir` / `read_file` 由调用方传进来，而不是在这里读常量——
测试是 monkeypatch `main.PERSONA_FILES_DIR` 来验路径闸的，这里自己
持有一份就会绕过补丁。
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from .ledger import LedgerError
    from .sink import _short
except ImportError:
    from ledger import LedgerError
    from sink import _short


_PRIORITY_RE = re.compile(r"权重\s*(\d+)\s*$")



def persona_import(plugin, text: str) -> str:
    """解析并写入手写人格，返回给人看的一句话。

    解析由 parse_persona_text（纯函数）完成，写入在 Ledger.add_persona_batch
    （带闸、整批一个提交）。这里只负责把结果翻成人话。
    """
    res = parse_persona_text(text)
    if res["problems"]:
        return "这批没写：\n" + "\n".join(
            "- " + p for p in res["problems"][:8])
    try:
        ids = plugin.ledger.add_persona_batch(res["items"], source="handwrite")
    except LedgerError as e:
        return "没写上：%s" % e
    head = plugin.ledger.head()
    shown = "、".join(ids[:8]) + ("…" if len(ids) > 8 else "")
    return "已手写写入 %d 条自我设定：%s（一个提交 %s）" % (len(ids), shown, head)


def persona_reply(plugin, rest: str, files_dir=None, read_file=None) -> str:
    """把 `/人格` 之后的参数翻成一句回话（查 / 加 / 导入 / 导入文件 / 改 / 删）。

    rest 是去掉指令名之后的原文；空串等于「查」。
    """
    if files_dir is None:
        files_dir = PERSONA_FILES_DIR
    if read_file is None:
        def read_file(arg):
            return read_persona_file(arg, base=files_dir, max_bytes=PERSONA_FILE_MAX)

    sub, arg = "", ""
    if rest:
        sp = rest.split(None, 1)
        sub = sp[0].strip()
        arg = sp[1].strip() if len(sp) > 1 else ""
    ledger = plugin.ledger
    if sub in ("", "查", "列表", "list"):
        rows = ledger.list_persona()
        if not rows:
            return "还没有任何人格设定（基础设定未播种，自我设定也没有）。"
        lines = ["人格设定：", ""]
        for r in rows:
            tag = "基础·只读" if r["kind"] == "core" else "自我"
            lines.append("%s [%s · 权重 %d] %s" % (
                r["id"], tag, int(r["priority"]),
                _short(r["content"], 60)))
        return "\n".join(lines)
    if sub in ("加", "新增", "add"):
        if not arg:
            return "用法：/人格 加 内容（一句话，200 字内）"
        try:
            pid = ledger.add_persona("self", arg)
        except LedgerError as e:
            return "没加上：%s" % e
        return "已新增自我设定 %s（提交 %s）" % (pid, ledger.head())
    if sub in ("导入", "手写", "import"):
        if not arg:
            return ("用法：/人格 导入 一行一条；[权重] 前缀可省，整批一个提交。\n"
                    "例：/人格 导入\n[50] 认准的事会坚持\n说话短句为主\n"
                    "以 # 开头的行是注释。要读文件用 /人格 导入文件 路径")
        return persona_import(plugin, arg)
    if sub in ("导入文件", "导入文本", "importfile"):
        if not arg:
            return ("用法：/人格 导入文件 文件名（只读人格资料目录 %s）\n"
                    "例：/人格 导入文件 persona.txt" % files_dir)
        try:
            text = read_file(arg)
        except Exception as e:
            return "读不了：%s" % e
        return persona_import(plugin, text)
    if sub in ("改", "更新", "update"):
        if not arg:
            return "用法：/人格 改 p1 新内容 [权重N]（只改权重时可省内容）"
        sp = arg.split(None, 1)
        rid = sp[0].strip()
        rest_arg = sp[1].strip() if len(sp) > 1 else ""
        m = _PRIORITY_RE.search(rest_arg)
        prio = int(m.group(1)) if m else None
        content_part = rest_arg[:m.start()].strip() if m else rest_arg
        if not content_part and prio is None:
            return "没说要改什么：给新内容或权重（权重N）。"
        try:
            ok = ledger.update_persona(
                rid, content=content_part or None, priority=prio)
        except LedgerError as e:
            return "没改成：%s" % e
        if not ok:
            return "没改成：设定 %s 不存在或内容没变。" % rid
        return "已更新设定 %s（提交 %s）" % (rid, ledger.head())
    if sub in ("删", "删除", "remove"):
        rid = arg.strip()
        if not rid:
            return "用法：/人格 删 p1"
        try:
            ok = ledger.remove_persona(rid)
        except LedgerError as e:
            return "没删掉：%s" % e
        if not ok:
            return "没删掉：设定 %s 不存在。" % rid
        return "已删除设定 %s（提交 %s）" % (rid, ledger.head())
    return "不认识的子命令：%s。可用：查 / 加 / 改 / 删，直接发「/人格」看全部。" % sub


# 人格资料目录：/人格 导入文件 只读这里。放在插件目录下，跟插件一起走；
# 目录不存在没关系，边界校验会先于「找文件」拦下一切越界路径。
PERSONA_FILES_DIR = Path(__file__).resolve().parent / "persona_files"
# 手写导入的文件大小上限（A1 保留原有 128KB 口径）
PERSONA_FILE_MAX = 128 * 1024


def read_persona_file(arg: str, base=None, max_bytes: int = PERSONA_FILE_MAX) -> str:
    """从人格资料目录读一个手写人格文件，返回文件内容。

    路径边界六道闸（A1/C1）：NUL、软链接、越界、非普通文件、
    128KB 上限、UTF-8 解码失败。相对路径一律按 PERSONA_FILES_DIR
    解析（C1：命令提示用户只给文件名，按进程 CWD 找等于坏的）；
    绝对路径保持原样。规则是先把 resolve(strict=True) 后的真实路径
    对进 PERSONA_FILES_DIR，再谈读不读——参数是用户发来的，不能拿
    它去摸插件目录之外的任何东西。
    所有拒绝都转成 LedgerError 的人话，不抛未捕获异常。
    """
    raw = str(arg or "").strip().strip('"').strip("'")
    if not raw:
        raise LedgerError("给个人格资料目录里的文件名")
    if "\x00" in raw:
        raise LedgerError("路径里有非法字符（NUL），不读")
    path = Path(raw)
    base = Path(base if base is not None else PERSONA_FILES_DIR).resolve()
    candidate = path if path.is_absolute() else base / path
    if candidate.is_symlink():
        raise LedgerError("软链接不读：%s" % candidate)
    try:
        real = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise LedgerError("没找到文件：%s（人格资料目录：%s）" % (raw, base))
    except OSError as e:
        raise LedgerError("这个路径打不开：%s" % e)
    if real != base and not real.is_relative_to(base):
        raise LedgerError("只能读人格资料目录（%s）里的文件：%s 不在里面"
                          % (base, raw))
    if real.is_dir():
        raise LedgerError("这是个目录不是文件：%s" % raw)
    if not real.is_file():
        raise LedgerError("没找到文件：%s（人格资料目录：%s）" % (raw, base))
    if real.stat().st_size > max_bytes:
        raise LedgerError("文件超过 128KB：手写导入装不下，先拆。")
    try:
        return real.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise LedgerError("读不了这个文件：%s" % e)


MAX_ITEMS = 200      # 手写一次性上限：再多就不是手写了，是灌库
MAX_CONTENT = 200    # 与 Ledger.add_persona 同口径
MAX_PRIORITY = 1000

# [50] 内容 / [ 50 ] 内容；只认行首，正文里的方括号不动它。
# 位数不设限：写成 [9999] 要报「超上限」，而不是被当成正文默默收下。
_PRIO_PREFIX = re.compile(r"^\s*\[\s*(\d+)\s*\]\s*(.*)$")


def parse_persona_text(text, default_priority=0):
    """解析文本，返回 {"items": [...], "problems": [...]}。

    items 按出现顺序排列，元素形如
        {"kind": "self", "content": "...", "priority": 50, "line": 3}
    problems 是人话，带行号；非空就代表整批不能写。
    """
    problems = []
    items = []
    seen = {}  # 内容 -> 首次出现的行号，用来抓重复

    try:
        default_priority = int(default_priority)
    except (TypeError, ValueError):
        problems.append("默认权重必须是整数，收到：%r" % (default_priority,))
        default_priority = 0
    if not 0 <= default_priority <= MAX_PRIORITY:
        problems.append("默认权重必须在 0..%d 之间，收到：%d"
                        % (MAX_PRIORITY, default_priority))

    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        priority = default_priority
        content = line
        m = _PRIO_PREFIX.match(line)
        if m:
            priority = int(m.group(1))
            content = m.group(2).strip()
            if not content:
                problems.append("第 %d 行：写了权重但没写内容" % lineno)
                continue
        if priority > MAX_PRIORITY:
            problems.append("第 %d 行：权重 %d 超过上限 %d"
                            % (lineno, priority, MAX_PRIORITY))
            continue
        if len(content) > MAX_CONTENT:
            problems.append("第 %d 行：%d 字，超过单条 %d 字上限，先拆开"
                            % (lineno, len(content), MAX_CONTENT))
            continue
        if content in seen:
            problems.append("第 %d 行与第 %d 行内容重复：%s"
                            % (lineno, seen[content], content[:20]))
            continue
        seen[content] = lineno
        items.append({"kind": "self", "content": content,
                      "priority": priority, "line": lineno})

    if len(items) > MAX_ITEMS:
        problems.append("一次最多 %d 条，这次读到 %d 条，分几批来"
                        % (MAX_ITEMS, len(items)))
    if not items and not problems:
        problems.append("没有可写入的内容：空行和以 # 开头的行都不算")
    return {"items": items[:MAX_ITEMS], "problems": problems}

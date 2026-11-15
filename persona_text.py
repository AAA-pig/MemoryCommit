# -*- coding: utf-8 -*-
"""手写人格：把一段纯文本解析成「待写入的人格设定」。

为什么单独一个模块：解析是纯函数，不该碰库，也不该自己开提交。
写入仍然只走门面（Ledger.add_persona_batch），双闸还在那边，
这里绝不复制一份闸的规则——闸只有一处，才不会两处慢慢长歪。

格式（一行一条，粘进来就行）：

    # 以 # 开头的是注释，空行忽略
    第一条设定内容                -> priority 0
    [50] 带权重的第二条            -> priority 50
    [1000] 最高优先级也用得上

校验口径与 Ledger.add_persona 保持一致（单条 200 字、权重 0..1000），
但这里只负责「先全量挑错」：有任何一行不合格，整批一个字都不写。
半批写进去比全不写更难收拾——人还得自己回忆写到第几条断的。
"""

import re

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

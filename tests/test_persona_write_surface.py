# -*- coding: utf-8 -*-
"""人格写入面的静态白名单（第一道闸）。

为什么要有这个文件：core_rule 是「只有用户显式发起才能改」的东西。
运行时守卫 restricted_auto_write 是「谁套上谁受保护」——新加的写路径
忘了套，就悄悄绕过去了，而且测试不会红。方案二是默认关的通行令牌
（运行期兜底），这里是方案一：静态点名。

做法：用 AST 扫仓库里所有可能碰 core_rule 的地方，对着白名单核对。
多一个位置 → 红（有人加了写路径没说）。
少一个位置 → 也红（白名单过期了，等于谎报覆盖）。

白名单不是许可证，是知情清单。每一行都得有人看过、知道它在干什么。
"""

import ast
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 不扫的目录：虚拟环境、临时产物、缓存、备份、测试本身（测试允许随便调用，
# 它们不是生产写路径）。
SKIP_DIRS = {".venv", ".temp", "__pycache__", ".git", "tests", "backups", "drill_out"}

PERSONA_WRITE_METHODS = {"add_persona", "add_persona_batch",
                         "update_persona", "remove_persona"}

# SQL 里出现这些，就是往 core_rule 里写数据/改结构。CREATE TABLE 不在内：
# 建表是新库初始化，不是写数据。
CORE_RULE_WRITE_SQL = (
    "INSERT INTO CORE_RULE",
    "UPDATE CORE_RULE",
    "DELETE FROM CORE_RULE",
    "ALTER TABLE CORE_RULE",
    "DROP TABLE CORE_RULE",
)

# (文件, 函数) -> 这里是哪一处 core_rule 落库
ALLOWED_CORE_RULE_WRITES = {
    ("ledger_commit.py", "add_persona"): "CommitBuilder.add_persona：唯一 insert 口",
    ("ledger_commit.py", "update_persona"): "CommitBuilder.update_persona：唯一 update 口",
    ("ledger_commit.py", "remove_persona"): "CommitBuilder.remove_persona：唯一 delete 口",
    ("ledger_schema.py", "_migrate_6_to_7"): "结构迁移：ALTER TABLE 补 kind/source/updated_at",
}

# (文件, 函数) -> 这里会调人格写方法。每个都得说清「凭什么它是用户路径」
ALLOWED_PERSONA_CALLERS = {
    ("ledger_persona.py", "add_persona"): "门面层：开 user_persona_write('api') 后转 builder",
    ("ledger_persona.py", "update_persona"): "门面层：开 user_persona_write('api') 后转 builder",
    ("ledger_persona.py", "remove_persona"): "门面层：开 user_persona_write('api') 后转 builder",
    ("ledger_persona.py", "add_persona_batch"): "手写导入门面：开 user_persona_write('handwrite')，整批一个提交",
    ("ledger_persona.py", "_seed_persona_core"): "显式播种：开 user_persona_write('seed')",
    ("persona_cmd.py", "persona_reply"): "/人格 的文案分发：查/加/改/删，用户显式发起（cmd_persona 只转交）",
    ("persona_cmd.py", "persona_import"): "/人格 导入 的公共壳：用户显式发起，转 add_persona_batch",
    ("main.py", "memory_persona"): "memory_persona 工具：用户显式发起",
}


def _sources():
    """仓库里的生产源文件（根目录的 .py，跳过测试与产物目录）。"""
    paths = []
    for folder, dirs, files in os.walk(ROOT):
        paths.extend(pathlib.Path(folder, name) for name in dirs + files
                     if pathlib.Path(name).match("*.py"))
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for path in sorted(paths):
        parts = set(path.relative_to(ROOT).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        yield path


def _own_nodes(fn):
    """函数自己作用域里的节点，不钻嵌套函数/类。

    归属要准：嵌套函数里的写入不能算到外层头上，否则白名单会漏掉真正的
    新增点。docstring 也跳过——它是说明文字，不是代码。
    """
    stack = list(fn.body)
    if (stack and isinstance(stack[0], ast.Expr)
            and isinstance(stack[0].value, ast.Constant)
            and isinstance(stack[0].value.value, str)):
        stack = stack[1:]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _callee_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_core_rule_push(call):
    """_push("core_rule", ...) —— 提交链里真正落 core_rule 的那一手。"""
    if _callee_name(call) != "_push" or not call.args:
        return False
    first = call.args[0]
    return (isinstance(first, ast.Constant) and isinstance(first.value, str)
            and first.value == "core_rule")


def _is_core_rule_sql(text):
    upper = " ".join(text.upper().split())
    return any(kw in upper for kw in CORE_RULE_WRITE_SQL)


def collect_persona_write_surface():
    """返回 (core_rule 落库点, 人格写方法调用点)，元素都是 (文件, 函数)。"""
    writes, callers = set(), set()
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in _own_nodes(fn):
                if isinstance(node, ast.Call):
                    if _is_core_rule_push(node):
                        writes.add((rel, fn.name))
                    if _callee_name(node) in PERSONA_WRITE_METHODS:
                        callers.add((rel, fn.name))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _is_core_rule_sql(node.value):
                        writes.add((rel, fn.name))
    return writes, callers


def test_core_rule_write_sites_are_whitelisted():
    """core_rule 的落库点只能是白名单里那几个，且白名单不能有僵尸条目。"""
    actual, _ = collect_persona_write_surface()
    expected = set(ALLOWED_CORE_RULE_WRITES)
    extra = actual - expected
    missing = expected - actual
    assert not extra, (
        "发现未登记的人格写入点：%s\n"
        "这多半是新加的写路径。先想清楚它是不是用户显式发起；"
        "是的话登记到 ALLOWED_CORE_RULE_WRITES 并说明理由，不是就改掉。"
        % sorted(extra))
    assert not missing, (
        "白名单里有条目在代码里已经找不到：%s\n"
        "白名单过期等于谎报覆盖，删掉它。" % sorted(missing))


def test_persona_callers_are_whitelisted():
    """能调人格写方法的函数只能那几个：多一个入口就得多一次交代。"""
    _, actual = collect_persona_write_surface()
    expected = set(ALLOWED_PERSONA_CALLERS)
    extra = actual - expected
    missing = expected - actual
    assert not extra, (
        "发现未登记的人格写入口：%s\n"
        "自动路径不许写人格；用户路径请走门面层（自带通行令牌）。"
        % sorted(extra))
    assert not missing, (
        "白名单里有入口已经不存在：%s\n删掉过期条目。" % sorted(missing))

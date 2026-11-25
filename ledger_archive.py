from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Tuple

if __package__:
    from .ledger_core import (
        CORE_API_VERSION, DUMP_FORMAT, DUMP_TABLES, DUMP_VERSION, GRADES, LedgerError,
        PROJECTIONS, STATE_ONLY_COLUMNS, V8_DUMP_TABLES, _DUMP_ORDER, _LOCAL_META, now_str,
    )
    from .ledger_schema import SCHEMA_VERSION
else:
    from ledger_core import (
        CORE_API_VERSION, DUMP_FORMAT, DUMP_TABLES, DUMP_VERSION, GRADES, LedgerError,
        PROJECTIONS, STATE_ONLY_COLUMNS, V8_DUMP_TABLES, _DUMP_ORDER, _LOCAL_META, now_str,
    )
    from ledger_schema import SCHEMA_VERSION


# -- 外置：导出成能直接翻的文件 --

def export_markdown(self, path: str = "") -> str:
    rows = [dict(r) for r in self._query_all(
        "SELECT * FROM memory ORDER BY grade DESC, created_seq DESC")]
    clue_map: Dict[str, List[str]] = {}
    for rel in self._query_all("SELECT mc.memory_id, c.content FROM memory_clue mc"
                               " JOIN clue c ON c.id = mc.clue_id"):
        clue_map.setdefault(rel["memory_id"], []).append(rel["content"])
    st = self.stats()
    lines = ["# 记忆账本", "",
             "HEAD `%s` · 创建序号 %d · 记忆 %d 条 · 线索 %d 个 · 连边 %d 条" % (
                 st["head"], st["created_seq"], st["memories"], st["clues"], st["links"]),
             ""]
    for r in rows:
        lines.append("## %s · %s · %s" % (
            r["id"], r["level"], GRADES.get(int(r["grade"]), "?")))
        lines.append("")
        lines.append(r["summary"])
        lines.append("")
        meta = "- 重要性 %.2f · 访问 %d 次 · 创建序号 %d · 最后访问 %d · %s" % (
            r["importance"], r["access_count"], r["created_seq"],
            r["last_access_seq"], r["created_at"])
        lines.append(meta)
        if clue_map.get(r["id"]):
            lines.append("- 概念：" + "、".join(clue_map[r["id"]]))
        lines.append("")
    text = "\n".join(lines)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    return text

# -- 外置：机器搬家用 --

def dump_jsonl(self, path: str = "") -> str:
    """把整个账本导成可往返的 JSONL：装进另一个空库就能接着用。

        和 export_markdown 是两件事：那个给人翻，丢了也只丢阅读体验；
        这个给机器搬家，少了任何一张表都会让提交链对不上。

        第一行是文件头（含每表行数清单与总行数），之后一行一条记录：
        {"t": 表名, "r": 行}，最后一行是 {"t": "__eof__"} 尾部标记——
        导入时拿这些校验文件完整性，被截断的备份直接拒绝（R5）。
        """
    body: List[str] = []
    counts: Dict[str, int] = {}
    # 导出一律走一把锁：中途被人写进来，导出的就是半截账本
    with self._lock:
        for tbl in DUMP_TABLES:
            order = _DUMP_ORDER.get(tbl)
            sql = "SELECT * FROM %s" % tbl
            if order:
                sql += " ORDER BY " + order
            n = 0
            for row in self._query_all(sql):
                data = dict(row)
                if tbl == "meta" and data.get("k") in _LOCAL_META:
                    continue
                body.append(json.dumps({"t": tbl, "r": data},
                                       ensure_ascii=False, sort_keys=True))
                n += 1
            counts[tbl] = n
    total = sum(counts.values())
    header = {
        "format": DUMP_FORMAT,
        "version": DUMP_VERSION,
        "schema_version": self.schema_version(),
        "core_api_version": CORE_API_VERSION,
        "dumped_at": now_str(),
        "tables": counts,
        "rows_total": total,
    }
    eof = json.dumps({"t": "__eof__", "rows_total": total},
                     ensure_ascii=False, sort_keys=True)
    text = "\n".join([json.dumps(header, ensure_ascii=False, sort_keys=True)]
                     + body + [eof]) + "\n"
    if path:
        target = os.path.abspath(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, target)
    return text

def _table_columns(self, cx: sqlite3.Connection, tbl: str) -> List[str]:
    return [r["name"] for r in cx.execute("PRAGMA table_info(%s)" % tbl).fetchall()]

def _write_dump_rows(self, cx: sqlite3.Connection,
                     rows: Dict[str, List[Dict[str, Any]]]
                     ) -> Tuple[int, List[str]]:
    """把解析好的备份行灌进一个连接（正式落库与临时库预演共用）。"""
    written = 0
    dropped_cols: List[str] = []
    for tbl in DUMP_TABLES:
        valid = set(self._table_columns(cx, tbl))
        for row in rows.get(tbl, []):
            cols = [c for c in row if c in valid]
            if len(cols) != len(row):
                dropped_cols.extend(
                    "%s.%s" % (tbl, c) for c in row if c not in valid)
            if not cols:
                continue
            sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
                tbl, ", ".join(cols), ", ".join("?" * len(cols)))
            cx.execute(sql, tuple(row[c] for c in cols))
            written += 1
    return written, dropped_cols


def _rehearse_restore(self, rows: Dict[str, List[Dict[str, Any]]], *, Ledger) -> None:
    """恢复预演（R5）：先灌进一个临时库，health_check 通过才算数。

        原库全程不写。临时库用完就删，失败也会带着原因离开。
        """
    tmp_path = self.db_path + ".restore-check"
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.exists(tmp_path + suffix):
                os.remove(tmp_path + suffix)
        except OSError:
            pass
    check = Ledger(tmp_path)
    try:
        with check._tx() as cx:
            check._write_dump_rows(cx, rows)
        hc = check.health_check()
        if not hc["ok"]:
            raise LedgerError(
                "恢复预演没通过，原库未动：%s"
                % "；".join(hc["problems"][:6]))
    finally:
        check.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                if os.path.exists(tmp_path + suffix):
                    os.remove(tmp_path + suffix)
            except OSError:
                pass


def load_jsonl(self, path: str, replace: bool = False,
               strict: bool = False, allow_lossy: bool = False, *, log) -> Dict[str, Any]:
    """把 dump_jsonl 的产物灌回来。

        默认只肯往空库里灌：覆盖是有代价的操作，不该是默认行为，
        想覆盖得自己说 replace=True。

        完整性策略（R5）：v2 及以后的备份自带行数清单与尾部 __eof__ 标记，
        导入一律严格——坏行、截断、行数对不上直接报错，落库前先在临时库
        预演一遍 health_check，通过才切换。v1 老备份（没有清单）保持宽容
        兼容：坏行跳过计数。strict=True 可以对老备份也强制严格。

        有损导入策略（P0-3）：备份里带当前库不存在的列，多半是备份来自
        更新的版本，那列可能影响记忆语义或检索。默认在落库前预检，发现
        丢列直接拒绝（报错列出 表.列，原库一字节不动）；确认接受降级导入
        就传 allow_lossy=True，保持旧行为照灌并在结果里如实报告丢列。
        """
    if not path or not os.path.exists(path):
        raise LedgerError("找不到要导入的文件：%s" % path)

    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        raise LedgerError("这个文件是空的")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as e:
        raise LedgerError("文件头不是合法 JSON：%r" % (e,))
    if not isinstance(header, dict) or header.get("format") != DUMP_FORMAT:
        raise LedgerError("不是记忆账本的备份文件（缺少 format=%s）" % DUMP_FORMAT)
    if int(header.get("version") or 0) > DUMP_VERSION:
        raise LedgerError("备份格式 v%s 比当前支持的 v%d 新" % (
            header.get("version"), DUMP_VERSION))
    if int(header.get("schema_version") or 0) > SCHEMA_VERSION:
        raise LedgerError("备份来自结构版本 %s，比当前代码支持的 %d 新" % (
            header.get("schema_version"), SCHEMA_VERSION))

    strict_mode = bool(strict) or int(header.get("version") or 0) >= 2
    rows: Dict[str, List[Dict[str, Any]]] = {}
    skipped = 0
    eof_seen = not strict_mode  # 宽容模式不要求尾部标记
    for idx, ln in enumerate(lines[1:], start=2):
        if strict_mode:
            try:
                item = json.loads(ln)
            except json.JSONDecodeError as e:
                raise LedgerError("备份第 %d 行不是合法 JSON（文件可能损坏）：%s"
                                  % (idx, e))
            if not isinstance(item, dict):
                raise LedgerError("备份第 %d 行不是 JSON 对象" % idx)
            tbl = item.get("t")
            if tbl == "__eof__":
                eof_seen = True
                break
            if tbl not in DUMP_TABLES or not isinstance(item.get("r"), dict):
                raise LedgerError("备份第 %d 行的表名不认识：%r" % (idx, tbl))
            rows.setdefault(tbl, []).append(item["r"])
        else:
            try:
                item = json.loads(ln)
            except json.JSONDecodeError:
                skipped += 1
                continue
            tbl = item.get("t") if isinstance(item, dict) else None
            if tbl == "__eof__":
                break
            row = item.get("r") if isinstance(item, dict) else None
            if tbl not in DUMP_TABLES or not isinstance(row, dict):
                skipped += 1
                continue
            rows.setdefault(tbl, []).append(row)

    if strict_mode and not eof_seen:
        raise LedgerError("备份缺尾部结束标记（__eof__）：文件被截断，拒绝导入")

    if strict_mode:
        want_total = int(header.get("rows_total") or -1)
        want_tables = header.get("tables") or {}
        got_tables = {t: len(v) for t, v in rows.items()}
        got_total = sum(got_tables.values())
        if want_total >= 0 and got_total != want_total:
            raise LedgerError(
                "备份总行数对不上：清单 %d，实际 %d（文件可能被截断）"
                % (want_total, got_total))
        for t, n in want_tables.items():
            if got_tables.get(t, 0) != int(n):
                raise LedgerError(
                    "备份 %s 表行数对不上：清单 %d，实际 %d"
                    % (t, int(n), got_tables.get(t, 0)))

    if not rows.get("memory") and not rows.get("mcommit"):
        # 空账本的备份也是合法备份，照收；真正损坏的文件连文件头都过不了
        log.debug("[记忆账本] 导入的是空备份：%s", path)

    existing = self._query_one("SELECT COUNT(*) AS n FROM mcommit")["n"]
    if existing and not replace:
        raise LedgerError(
            "这个库已经有 %d 个提交，不覆盖。真想替换就传 replace=True" % existing)

    # 有损导入预检（P0-3）：必须走在一切写之前——预演库也好、正式库也好，
    # 都不许先写半截再回头拒绝。只读目标库表结构，不动任何数据。
    if not allow_lossy:
        lost: List[str] = []
        for tbl in DUMP_TABLES:
            if not rows.get(tbl):
                continue
            valid = set(self._table_columns(self._conn, tbl))
            for row in rows[tbl]:
                lost.extend("%s.%s" % (tbl, c) for c in row if c not in valid)
        lost = sorted(set(lost))
        if lost:
            raise LedgerError(
                "备份里的列在当前库里不存在，导入会丢数据：%s。"
                "确认接受有损导入就传 allow_lossy=True" % "、".join(lost))

    if strict_mode:
        # 恢复在临时库验证通过再切换（R5）：预演失败原库一字节不动
        self._rehearse_restore(rows)

    # v6 保护（2026-11-15）：creative 已经进了 DUMP_TABLES，替换式恢复
    # 若还照「按表清空」一刀切，拿一份不含 creative 的旧备份（schema v5
    # 及更早 / 格式 v3 及更早）来恢复，就会把所有创作项目静默删光——
    # 那是正文，提交链里重放不出来。所以清空只清「备份真的带来」的表：
    # 备份没带 creative，本地创作原样保留，并在 warnings 里如实报数。
    backup_schema = int(header.get("schema_version") or 0)
    backup_tables = header.get("tables") or {}
    creative_in_backup = "creative" in backup_tables
    local_creative = self._query_one(
        "SELECT COUNT(*) AS n FROM creative")["n"]
    # v8 保护（2026-11-15）：群聊三张表进了 DUMP_TABLES，同 creative 先例
    # ——旧备份（格式 ≤ v5）没带这三张表，替换式恢复不许把它们静默清掉：
    # 说话人/可见性/坏感度是「谁说的、什么口径、什么态度」。本地行数
    # 在 warnings 里如实报数。
    v8_kept: Dict[str, int] = {}
    for tbl in V8_DUMP_TABLES:
        if tbl in backup_tables:
            continue
        n = self._query_one("SELECT COUNT(*) AS n FROM %s" % tbl)["n"]
        if n:
            v8_kept[tbl] = n

    with self._tx() as cx:
        if replace:
            for tbl in reversed(DUMP_TABLES):
                if tbl == "creative" and not creative_in_backup:
                    continue
                if tbl in V8_DUMP_TABLES and tbl not in backup_tables:
                    continue
                cx.execute("DELETE FROM %s" % tbl)
        written, dropped_cols = self._write_dump_rows(cx, rows)

    # 导回来的库可能来自更老的版本，这里补一步；也可能是更新的库，那就已经拦过了
    migrated = self._migrate()

    warnings: List[str] = []
    # 结构版本低于 6、或备份根本没列 creative 表：这次恢复碰不到创作项目。
    # 替换式恢复尤其要说清楚「本地那几条是保留下来的，不是恢复出来的」。
    if backup_schema < 6 or not creative_in_backup:
        ver = "v%s" % backup_schema if backup_schema else "未知"
        if replace and local_creative:
            warnings.append(
                "备份不含 creative 表（结构 %s），本地 %d 条创作已保留"
                % (ver, local_creative))
        elif replace:
            warnings.append(
                "备份不含 creative 表（结构 %s），本地也没有创作项目" % ver)
        else:
            warnings.append(
                "备份不含 creative 表（结构 %s），本次导入未涉及创作项目" % ver)
    # v8（2026-11-15）：旧备份没带群聊三张表时的同款报数（replace 才碰清空）
    if replace and v8_kept:
        kept_desc = "、".join("%s %d 行" % (t, n)
                              for t, n in sorted(v8_kept.items()))
        warnings.append(
            "备份不含 v8 群聊表（结构 v%s），本地已保留：%s"
            % (backup_schema or "未知", kept_desc))
    head = self.head()
    if head and not self.show_commit(head):
        warnings.append("HEAD %s 在提交表里找不到" % head)
    if not head and (rows.get("memory") or []):
        warnings.append("有记忆但没有 HEAD，提交链是断的")
    return {
        "tables": {t: len(v) for t, v in rows.items()},
        "written": written,
        "skipped": skipped,
        "dropped_columns": sorted(set(dropped_cols)),
        "migrated": migrated,
        "head": head,
        "memories": self.stats()["memories"],
        "warnings": warnings,
        "verified": strict_mode,
    }

def maybe_export_daily(self, path: str = "", *, log) -> bool:
    """每天最多导出一次。跨天才写，同一天再调直接返回 False。

        给外壳在请求前顺手调一下，就不用额外挂定时器；
        哪天进程一直没重启，账本也还是会刷新。
        """
    if not path:
        return False
    today = time.strftime("%Y-%m-%d")
    row = self._query_one("SELECT v FROM meta WHERE k = 'last_export_date'")
    if row and row["v"] == today:
        return False
    try:
        self.export_markdown(path)
    except Exception as e:
        log.warning("[记忆账本] 每日导出失败: %r", e)
        return False
    with self._tx() as cx:
        self._set_meta(cx, "last_export_date", today)
    # 顺手把访问日志收一收：它只服务于衰减计算，留最近一段就够
    try:
        self.prune_access_log()
    except Exception as e:
        log.warning("[记忆账本] 访问日志修剪失败: %r", e)
    return True

def health_check(self, export_path: str = "") -> Dict[str, Any]:
    """自检：能读、能写、链不断、投影对得上、导出目录能落盘。

        重载插件之后跑一遍，比「看起来没报错」可靠。
        """
    problems: List[str] = []
    st = self.stats()

    # 1. 提交链：每个提交的 parent 要么为空，要么真的存在
    ids = {r["id"] for r in self._query_all("SELECT id FROM mcommit")}
    for r in self._query_all("SELECT id, parent FROM mcommit"):
        if r["parent"] and r["parent"] not in ids:
            problems.append("提交 %s 的父节点 %s 不存在" % (r["id"], r["parent"]))

    # 2. 投影表：提交里记的行必须真的落在表里（抽最后一条提交）
    last = self.show_commit(st["head"]) if st["head"] else None
    if st["head"] and not last:
        problems.append("HEAD %s 在提交表里找不到" % st["head"])

    # 2.5 投影对账（R4）：提交链重放到 HEAD，每一张可重放投影表
    #     都要和当前表逐行一致（访问痕迹缓存列除外，它们不进提交链）。
    #     以前只查 HEAD 在不在，幽灵行 / 漏行的漂移全靠运气。
    #     对账范围是「链里管过的表」：commit_row 里压根没出现过的表
    #     （迁移库、外部直插）本来就不是链生成的，不拿链去硬对。
    try:
        replay = self._replay_tables(st["head"]) if st["head"] else {}
    except Exception as e:
        problems.append("投影重放失败：%r" % (e,))
        replay = {}
    chain_tables = {r["tbl"] for r in
                    self._query_all("SELECT DISTINCT tbl FROM commit_row")}
    # core_rule（v7）是唯一有「合法链外存量」的投影表：6→7 迁移的
    # 存量行是外部直插，链上没有它的页。只加列不改数据、不为存量行
    # 伪造链页，所以对账口径收窄成「链写过的 id」——链里压根没写过的
    # 行（迁移存量）不硬对，链写过的行漂移照抓。
    chained_persona_ids: set = set()
    if "core_rule" in chain_tables:
        for r in self._query_all(
                "SELECT row_key FROM commit_row WHERE tbl = 'core_rule'"):
            for kv in (r["row_key"] or "").split("|"):
                if kv.startswith("id="):
                    chained_persona_ids.add(kv[3:])
    for tbl, key_of in PROJECTIONS.items():
        if tbl not in chain_tables:
            continue
        # memory 的访问缓存列不进提交链；memory_link 的插入快照
        # 历来不含自增 id（行身份是 from|to|concept），对账时两边都剔除
        ignore = (STATE_ONLY_COLUMNS if tbl == "memory"
                  else ("id",) if tbl == "memory_link" else ())
        cur: Dict[Any, Dict[str, Any]] = {}
        for row in self._query_all("SELECT * FROM %s" % tbl):
            d = {k: v for k, v in dict(row).items() if k not in ignore}
            k = key_of(d)
            if k is not None:
                if tbl == "core_rule" and str(k) not in chained_persona_ids:
                    continue
                cur[k] = d
        rep: Dict[Any, Dict[str, Any]] = {}
        for k, d in replay.get(tbl, {}).items():
            if k is None:
                continue
            rep[k] = {kk: vv for kk, vv in d.items() if kk not in ignore}
        if cur == rep:
            continue
        extra = sorted(str(k) for k in cur.keys() - rep.keys())[:3]
        missing = sorted(str(k) for k in rep.keys() - cur.keys())[:3]
        changed = sorted(str(k) for k in cur.keys() & rep.keys()
                         if cur[k] != rep[k])[:3]
        problems.append(
            "投影对账不一致：%s 当前 %d 行 / 重放 %d 行"
            "（当前多 %s；重放多 %s；内容不同 %s）"
            % (tbl, len(cur), len(rep),
               extra or "无", missing or "无", changed or "无"))

    # 3. 计数：记忆数与提交行数不该互相矛盾
    mem_rows = self._query_one("SELECT COUNT(*) AS n FROM memory")["n"]
    if st["memories"] != mem_rows:
        problems.append("记忆计数不一致：stats=%s，表=%s" % (st["memories"], mem_rows))

    # 4. 写能力：往 meta 里写一次探针再删掉，不碰任何投影表
    try:
        with self._tx() as cx:
            self._set_meta(cx, "health_probe", now_str())
            cx.execute("DELETE FROM meta WHERE k = 'health_probe'")
    except Exception as e:
        problems.append("数据库不可写：%r" % (e,))

    # 5. 导出：只在给了路径时才真的写文件
    export_ok = False
    if export_path:
        try:
            self.export_markdown(export_path)
            export_ok = True
        except Exception as e:
            problems.append("导出失败：%r" % (e,))

    # 6. 原文链：逐条验 digest、对账 message_no / message_head。
    #    改一个字、中间断过、尾部被删都会在这里露出来。自检能看到
    #    原文链条数与是否断裂，而不是只说一句「memory 表还好」。
    chain = self.verify_message_chain()
    if not chain["ok"]:
        problems.append("原文链有问题：%s" % chain["reason"])

    # 7. core 人格哨兵（v7）：核对一次。不符只报警记日志——不进
    #    problems，免得恢复预演被哨兵卡死（任务口径：只报警不拦截，
    #    修是人工确认后 reseal 的事）。
    seal = self._verify_persona_core_seal(context="health")

    return {"ok": not problems, "problems": problems, "export_ok": export_ok,
            "message_chain": chain, "persona_core": seal, "stats": st}

# -*- coding: utf-8 -*-
"""冒烟基线（重构用回归口径）。

不接 AstrBot、不碰线上库：临时目录建库，直接构造 Ledger，跑
初始化 → 写一条记忆 → 检索一次 → 注入一次 → 关闭。

用法：
    .venv/Scripts/python.exe tools/smoke_baseline.py
每步结果打印到 stdout，末尾输出 SMOKE_OK / SMOKE_FAIL。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ledger as ledmod  # noqa: E402


def main() -> int:
    # 说明：本机 AppData\Local（含 %TEMP%）建目录会卡住几十秒（50657 条目 /
    # 实时扫描），冒烟临时库固定放在副本内 _smoke_tmp/，已在 .git/info/exclude 里忽略。
    tmp = os.path.join(ROOT, "_smoke_tmp")
    db = os.path.join(tmp, "smoke.db")
    ok = True
    try:
        # 1. 初始化
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        L = ledmod.Ledger(db)
        ver = L.schema_version()
        with sqlite3.connect(db) as cx:
            tabs = [r[0] for r in cx.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%' order by name")]
        print("[1] init      db=%s schema_version=%s tables=%d" % (db, ver, len(tabs)))
        print("              tables=%s" % ",".join(tabs))

        # 2. 写一条
        mid = L.remember("冒烟基线：重构前的一条记忆", level="day",
                         tags="smoke,baseline", importance=0.5)
        print("[2] write     id=%s head=%s" % (mid, (L.head() or "")[:12]))

        # 3. 检索一次
        hits = L.recall("冒烟基线", limit=5)
        print("[3] recall    type=%s hits=%d first=%s" % (
            type(hits).__name__, len(hits), (hits[0].get("id") if hits else "-")))
        if not hits:
            ok = False
            print("              !! 检索没命中")

        # 4. 注入一次（brief + 人格文本两条路径）
        bundle = L.activate("冒烟基线", slots=3, with_profile=True,
                            context={"scene": "smoke:s1", "now": 0}, scope_key="smoke:s1")
        stats_ = {}
        brief = L.render_brief(bundle, max_chars=420, stats=stats_)
        print("              activate keys=%s stats=%s" % (
            ",".join(sorted(bundle.keys())), json.dumps(stats_, ensure_ascii=False)[:120]))
        ptext = L.persona_text(budget=1200)
        print("[4] inject    brief_chars=%d brief_first=%r" % (
            len(brief), (brief.splitlines() or [""])[0][:60]))
        print("              persona_text_chars=%d" % len(ptext))

        # 5. 追加一条消息 + 统计
        L.append_message("user", "冒烟基线消息", session_id="smoke:s1")
        st = L.stats()
        print("[5] stats     %s" % json.dumps(st, ensure_ascii=False, sort_keys=True)[:220])

        # 6. 关闭
        L.close()
        size = os.path.getsize(db)
        print("[6] close     db_bytes=%d" % size)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("EXCEPTION %s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE_OK" if ok else "SMOKE_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""第三轮锐评（fb9dfa4）修复的回归测试：N6 / N7 / N8。

评审报告：MemoryCommit_三轮锐评_fb9dfa4_2026-11-14.md。
每条测试对应报告里一个实测缺口，全部用临时库，不碰生产账本。
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ledger as L  # noqa: E402


def _load_module_from_source(source: str, name: str, tmp_path) -> object:
    """把一段源码当独立模块加载（评审复现脚本 probe3_ledger.py 的做法）。"""
    path = str(tmp_path / (name + ".py"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- N6：存量沉淀识别与迁移 ---------------------------------------------------

@pytest.fixture()
def legacy_ledger_db(tmp_path):
    """用 954ef20 的 ledger.py 写一个 1.5.0 形态的库再交出去。

    1.5.0 期间（468731f..954ef20）的 _auto_sink 写 tags=「自动沉淀」，
    没有 type:auto_sink 类型标记；旧版 _auto_link 会给这个标签织
    concept 边。这里照旧外壳的真实写路径造出同样的数据形态。
    """
    try:
        src = subprocess.run(["git", "show", "954ef20:ledger.py"], cwd=ROOT,
                             capture_output=True, check=True,
                             timeout=30).stdout.decode("utf-8")
    except Exception as e:  # pragma: no cover - 环境坏了要显式暴露
        pytest.fail("取不到 954ef20:ledger.py（%r）：这条测试钉的是存量迁移，"
                    "评审基线提交必须在本地历史里" % e)
    old = _load_module_from_source(src, "ledger_954ef20", tmp_path)
    db = str(tmp_path / "legacy.db")
    led = old.Ledger(db)
    led.remember("空调遥控器放在电视柜第二个抽屉", importance=0.5)
    for i in range(6):
        led.remember("自动沉淀：最近 12 条原文（用户 6 条）。开头是「空调话题 %d」" % i,
                     level="day", tags="自动沉淀", importance=0.45)
    led.close()
    return db


def test_n6_legacy_auto_sink_migrated_on_open(legacy_ledger_db):
    """用 954ef20 写库、新代码打开：存量沉淀被补标记、机器标签边被摘除。

    修复前（评审复现）：auto_sink_ids() 为空、15 条 concept=自动沉淀
    的边还在、activate("空调") 返回的全是沉淀、渲染出的系统提示里
    出现「自动沉淀」。
    """
    led = L.Ledger(legacy_ledger_db)
    try:
        sinks = led.auto_sink_ids()
        assert len(sinks) == 6, "存量沉淀没被识别"
        for r in led._query_all("SELECT id, tags FROM memory"):
            if r["id"] in sinks:
                assert "type:auto_sink" in {t.strip() for t in r["tags"].split(",")}, \
                    "存量条目没补上类型标记"
        edges = led._query_all(
            "SELECT concept FROM memory_link WHERE concept LIKE 'type:%'"
            " OR concept = '自动沉淀'")
        assert edges == [], "机器标签边没被摘掉"
        # 迁移走提交链：投影重放与现状对得上
        health = led.health_check()
        assert health["ok"], "迁移后提交链对不上：%s" % health["problems"]
        # 修好的行为：activate 不再把沉淀当事实
        r = led.activate("空调", slots=3, dry_run=True, with_profile=False)
        assert not [i for i in r["items"] if i["id"] in sinks], "存量沉淀仍被注入"
        assert "自动沉淀" not in led.render_brief(r)
        # 对照组：普通事实仍能正常被想起
        assert any("空调遥控器" in i["summary"] for i in r["items"])
    finally:
        led.close()


def test_n6_migration_is_idempotent(legacy_ledger_db):
    """迁移可重复执行：再开一次不产生新迁移提交，行为不变。"""
    led = L.Ledger(legacy_ledger_db)
    commits = led.stats()["commits"]
    sinks = led.auto_sink_ids()
    led.close()

    led2 = L.Ledger(legacy_ledger_db)
    try:
        assert led2.stats()["commits"] == commits, "重复打开又跑了一遍迁移"
        assert led2.auto_sink_ids() == sinks
    finally:
        led2.close()


def test_n6_auto_sink_ids_recognizes_legacy_label(tmp_path):
    """识别口径与建图口径同源：只有「自动沉淀」标签的条目也算沉淀。

    type:auto_sink_x 这类前缀撞车的标签不许被认成沉淀（评审 12 个
    定点变异里唯一全绿漏网的那个）。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        legacy = led.remember("旧格式沉淀条目", tags="自动沉淀")["memory_id"]
        modern = led.remember("新格式沉淀条目", tags="自动沉淀,type:auto_sink")["memory_id"]
        decoy = led.remember("前缀撞车的普通条目", tags="type:auto_sink_x")["memory_id"]
        normal = led.remember("一条普通事实", tags="生活")["memory_id"]
        sinks = led.auto_sink_ids()
        assert {legacy, modern} <= sinks
        assert decoy not in sinks
        assert normal not in sinks
    finally:
        led.close()


# -- N7：候选池截断在排除之前 --------------------------------------------------

def test_n7_ten_sinks_do_not_starve_search_route(tmp_path):
    """10 条同主题沉淀不把真事实挤出 search 路线（评审复现表第 3 行）。

    修复前：recall 先按 limit=9 截断再交给 take() 排除，10 条沉淀
    正文与「空调」词面高度重合，真事实掉出前 9，route 只能落到
    fallback（进而失去复出资格）。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        normal = led.remember("家里空调遥控器在电视柜第二个抽屉里",
                              importance=0.5)["memory_id"]
        for i in range(10):
            led.remember("自动沉淀：最近 12 条原文（用户 6 条）。开头是「空调第 %d 段」" % i,
                         level="day", tags="自动沉淀,type:auto_sink", importance=0.45)
        sinks = led.auto_sink_ids()
        assert len(sinks) == 10
        hits = led.recall("空调", limit=9, touch=False, exclude=sinks)
        assert normal in [h["id"] for h in hits], "排除前移后真事实该留在前 9"
        r = led.activate("空调", slots=3, dry_run=True, with_profile=False)
        assert r["route"] == "search"
        assert normal in [i["id"] for i in r["items"]]
        assert not [i for i in r["items"] if i["id"] in sinks], "沉淀混进了注入"
    finally:
        led.close()


def test_n7_recall_without_exclude_still_finds_sinks(tmp_path):
    """exclude 是显式参数：不传的显式检索照样能找到沉淀条目。"""
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        led.remember("自动沉淀：最近 12 条原文（用户 6 条）。开头是「空调话题」",
                     level="day", tags="自动沉淀,type:auto_sink", importance=0.45)
        hits = led.recall("空调", touch=False)
        assert any("type:auto_sink" in (h.get("tags") or "") for h in hits)
    finally:
        led.close()


# -- N8：fallback 破例名额不被沉淀霸占 -----------------------------------------

def test_n8_fallback_exceptional_surface_skips_sinks(tmp_path):
    """候选全在冷却时，破例浮现的「最久没露面」不能选中沉淀。

    评审复现（probe3_force.py）：3 条普通事实刚注入过在冷却，库里
    多一条沉淀后整轮 fallback_blocked——沉淀 _surfaced 恒为 0，
    永远是 min 的那个，但 force 绕不过 auto_sink 排除。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        for t in ("用户喜欢热咖啡", "用户周末爬山", "用户的猫叫团子"):
            led.remember(t)
        led.activate("", slots=3, with_profile=False)  # 三条都刚浮现，进冷却
        led.remember("自动沉淀：最近 12 条原文（用户 6 条）。开头是「天气不错」",
                     level="day", tags="自动沉淀,type:auto_sink", importance=0.45)
        r = led.activate("量子纠缠", slots=3, dry_run=True, with_profile=False)
        sinks = led.auto_sink_ids()
        assert r["route"] == "fallback"
        assert r["items"], "破例名额被沉淀霸住，整轮空转"
        assert not [i for i in r["items"] if i["id"] in sinks]
    finally:
        led.close()


# -- N9：存储层把索引当一等记忆 ------------------------------------------------

def test_n9_sinks_do_not_seize_working_set(tmp_path):
    """沉淀不抢工作记忆名额：换班方案里没有沉淀，真事实不被挤到零级。

    评审复现（probe3_chain.py）：容量 3，3 条真事实 + 12 条沉淀，
    换班后真事实 grade 全变 0，一级名额全给最新的沉淀。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        led.working_capacity = 3
        real = [led.remember("真事实 %d" % i, importance=0.6)["memory_id"]
                for i in range(3)]
        for i in range(12):
            led.remember("最近 12 条原文（用户 6 条）。开头是「闲聊 %d」" % i,
                         level="day", tags="自动沉淀,type:auto_sink",
                         importance=0.45)
        sinks = led.auto_sink_ids()
        assert len(sinks) == 12
        plan = led.plan_working_set()
        assert {k["id"] for k in plan["keep"]} == set(real), \
            "工作记忆名额被沉淀占用"
        assert not [d for d in plan["demote"] if d["id"] in real], \
            "真事实被沉淀挤到零级"
        res = led.rebalance_working_set(apply=True)
        assert not res["applied"] and not res["demoted"]
    finally:
        led.close()


def test_n9_sinks_do_not_count_into_flush(tmp_path):
    """沉淀不进冲刷计划，写入沉淀也不推进普通记忆的冲刷量。

    冲刷量基准取「普通记忆」里最新的 created_seq：8 条真事实之后再
    写 12 条沉淀，m1 的 flush_amount 仍按第 8 条真事实算（8-1=7），
    不被推进到 20-1=19。压力要够进冲刷计划（delta ≥ 4），所以用 8 条。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        real = [led.remember("真事实 %d" % i, importance=0.6)["memory_id"]
                for i in range(8)]
        before = {p["memory_id"]: p["flush_amount"] for p in led.flush_plan()}
        assert before, "测试场景没造出冲刷压力，场景坏了"
        for i in range(12):
            led.remember("最近 12 条原文（用户 6 条）。开头是「闲聊 %d」" % i,
                         level="day", tags="自动沉淀,type:auto_sink",
                         importance=0.45)
        sinks = led.auto_sink_ids()
        plan = led.flush_plan()
        assert not [p for p in plan if p["memory_id"] in sinks], \
            "沉淀条目进了冲刷计划"
        after = {p["memory_id"]: p["flush_amount"] for p in plan}
        for mid in real:
            assert after.get(mid) == before.get(mid), \
                "写沉淀推进了 %s 的冲刷量（%s → %s）" % (
                    mid, before.get(mid), after.get(mid))
        assert after[real[0]] == 7, "冲刷量基准没有锚在普通记忆的最新序号上"
    finally:
        led.close()


# -- N10：子串 LIKE 把普通记忆连到沉淀上 ----------------------------------------

def test_n10_substring_tags_do_not_link_to_sinks(tmp_path):
    """「自动」「沉淀」「auto」「sink」不再连到沉淀条目，名额不被吃掉。

    评审复现（probe3_ledger.py C 段）：修复前四个标签各连出 8/8 条边，
    而真共享「自动」标签的两条普通记忆之间反而没有边——8 个名额
    被按存储顺序排在前面的沉淀用掉了。
    """
    led = L.Ledger(str(tmp_path / "a.db"))
    try:
        sinks = {led.remember("最近 12 条原文（用户 6 条）。开头是「闲聊 %d」" % i,
                              level="day", tags="自动沉淀,type:auto_sink",
                              importance=0.45)["memory_id"] for i in range(10)}
        mids = {}
        for tag in ("自动", "沉淀", "auto", "sink"):
            mids[tag] = led.remember("普通记忆，标签=%s" % tag, tags=tag)["memory_id"]
        for tag, mid in mids.items():
            to = [x["to_id"] for x in led._query_all(
                "SELECT to_id FROM memory_link WHERE from_id=?", (mid,))]
            assert not (set(to) & sinks), \
                "标签 %r 的子串命中把普通记忆连到了沉淀上" % tag
        # 修复后行为：真共享标签的普通记忆之间照常连边，不被沉淀吃掉名额
        a = led.remember("用户在折腾自动化脚本", tags="自动")["memory_id"]
        b = led.remember("用户想把浇花改成自动", tags="自动")["memory_id"]
        ab = led._query_one(
            "SELECT 1 FROM memory_link WHERE (from_id=? AND to_id=?)"
            " OR (from_id=? AND to_id=?)", (a, b, b, a))
        assert ab, "真共享「自动」的普通记忆之间该有边"
    finally:
        led.close()

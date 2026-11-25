async def cmd_flush(self, event, _short, /):
    raw = (event.get_message_str() or "")
    # 破坏性操作要精确确认（R1）：只认独立的「确认」一词。
    # 不许用「确认 in raw」——「不要确认」也含「确认」，会误执行。
    confirm = "确认" in raw.split()
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


async def cmd_working(self, event, _short, re, /):
    raw = (event.get_message_str() or "").replace("记忆容量", "", 1)
    # 精确确认（R1）：「确认」必须是独立词，「不要确认」不算
    confirm = "确认" in raw.split()
    found = re.search(r"(\d+)", " ".join(t for t in raw.split() if t != "确认"))
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


async def cmd_compact(self, event, LedgerError, StaleHeadError, re, /):
    raw = (event.get_message_str() or "").replace("记忆压缩", "", 1)
    # 破坏性操作要精确确认（R1，评审复测：/记忆压缩 1 一直无确认直接执行）：
    # 只认独立的「确认」一词，与 /记忆冲刷、/记忆容量 同一口径。
    confirm = "确认" in raw.split()
    found = re.search(r"(\d+)", " ".join(t for t in raw.split() if t != "确认"))
    keep = max(1, int(found.group(1))) if found else 10
    if not confirm:
        commits = int(self.ledger.stats()["commits"])
        if commits <= 1:
            yield event.plain_result("只有 %d 个提交，没什么可压的。" % commits)
            return
        yield event.plain_result(
            "压缩会把最早的提交并成一条基线，不可逆（现在 %d 个提交，保留最近 %d 个）。\n"
            "发送「/记忆压缩%s确认」执行。" % (
                commits, keep, " %d " % keep if found else " "))
        return
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


async def cmd_backup(self, event, BACKUP_DIR, LedgerError, Path, os, time, /):
    raw = (event.get_message_str() or "").strip()
    parts = raw.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    # 客户端只传文件名：不管传来的是绝对路径还是带 .. 的相对路径，
    # 都只留最后的文件名部分，再校验解析后路径确实落在专用目录里
    name = os.path.basename(arg.replace("\\", "/")).strip() if arg else ""
    if name in ("", ".", ".."):
        name = "ledger-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")
    if not name.lower().endswith(".jsonl"):
        name += ".jsonl"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        target = BACKUP_DIR / name
        base_real = Path(os.path.realpath(str(BACKUP_DIR)))
        target_real = Path(os.path.realpath(str(target)))
        if target_real != base_real and not target_real.is_relative_to(base_real):
            raise LedgerError("备份只能写进专用目录：%s" % BACKUP_DIR)
        if target_real.exists():
            raise LedgerError("同名备份已存在，默认不覆盖，换个文件名再发")
        self.ledger.dump_jsonl(str(target_real))
    except Exception as e:
        yield event.plain_result("没备份成：%s" % e)
        return
    st = self.ledger.stats()
    yield event.plain_result(
        "备份好了：\n%s\n\n提交 %d · 记忆 %d · 线索 %d · 结构版本 %d" % (
            target_real, st["commits"], st["memories"], st["clues"],
            st["schema_version"]))


async def cmd_health(self, event, EXPORT_PATH, /):
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
    lines.append("写路径异常 %d 次：%s" % (self.failures.total(),
                                        self.failures.summary()))
    lines.append(self._sink_gap_line())
    if res["problems"]:
        lines.append("")
        lines.extend("- " + p for p in res["problems"])
    yield event.plain_result("\n".join(lines))


async def cmd_attitude(self, event, ATTITUDE_OWNER_ID, LedgerError, /):
    raw = (event.get_message_str() or "").strip()
    parts = raw.split()
    if len(parts) < 2:
        yield event.plain_result(
            "用法：/坏感度 用户ID → 查；"
            "/坏感度 用户ID 0到4 [原因] → 设。主人的等级不许设。")
        return
    uid = parts[1].strip()
    if len(parts) == 2:
        row = self.ledger.get_attitude_detail(uid)
        reason = "（原因：%s）" % row["reason"] if row["reason"] else "（还没记过）"
        yield event.plain_result("%s 坏感度 %d%s" % (
            uid, int(row["level"]), reason))
        return
    if uid == ATTITUDE_OWNER_ID:
        yield event.plain_result("主人的坏感度不许设：owner 的等级严禁修改。")
        return
    reason = " ".join(parts[3:]) if len(parts) > 3 else ""
    try:
        res = self.ledger.set_attitude(uid, parts[2], reason)
    except LedgerError as e:
        yield event.plain_result("没设成：%s" % e)
        return
    yield event.plain_result("已把 %s 坏感度设为 %d（提交 %s）" % (
        uid, res["level"], res["commit"]))


def _log_injection(
    self, outcome, query_len, stats, result, INJECT_LOG, INJECT_LOG_ROTATE_LINES, json, os,
    time, /, **extra):
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
                self._note_failure("注入日志轮转失败", e)
        with open(INJECT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        self._note_failure("注入日志写失败", e)


def inject_memory(
    self, event, req, AUTO_SINK_EVERY, EXPORT_PATH, LedgerError, MAINTAIN_EVERY,
    PERSONA_FENCE_HEAD, PERSONA_FENCE_TAIL, PROFILE_REFRESH_SECONDS, _PERSONA_CTRL_RE,
    _is_group, _sender_of, match_denial_keywords, time, /):
    # 原文锚绑在这次请求的事件上（R2），不再挂在插件实例：
    # 不同会话/并发请求各锚各的，不会串；落链失败显式置 0，
    # 不许沿用上一轮的锚去锚错原话。
    anchor_seq = 0
    # N4：said 也要先置空。get_message_str() 炸掉时它还没被赋值，
    # 下面长 prompt 分支的 said 会 UnboundLocalError，把「落链失败」
    # 升级成「唤起失败」，整轮注入直接消失。
    said = ""
    # 原文先落链，再谈检索：这条不管后面检索成不成立都得记下。
    # 检索词有长度上限，原文没有——「记得住」和「想得起」是两件事。
    try:
        said = (event.get_message_str() or "").strip()
        if said:
            # G1：沉淀开着时，在本轮 user 原文落链前确保水位线存在——
            # 首轮从「本轮之前」的链尾起算：本轮 user+assistant 都算
            # 待沉，而库里的历史不会被从 0 重沉。初始化炸了只计数，
            # _auto_sink 里还有兜底初始化，不连累落链。
            if AUTO_SINK_EVERY > 0:
                try:
                    self._sink_watermark(event.unified_msg_origin or "")
                except Exception as e:
                    self._note_failure("沉淀水位初始化失败", e)
            node = self.ledger.append_message(
                "user", said, session_id=(event.unified_msg_origin or ""))
            # v5 增量原文锚：这句原话的链上序号，本轮若写了记忆就锚回它
            anchor_seq = int(node.get("seq") or 0)
            # 否认关键词命中 → 只进待确认队列，不自动改状态（v5 §5.1）。
            # 「你记错了」这种话是线索不是判决，等人或原文对账确认。
            hits = match_denial_keywords(said)
            if hits:
                try:
                    self.ledger.queue_denial_suspect(said, keyword=hits[0])
                except Exception as e:
                    self._note_failure("待确认队列写入失败", e)
    except Exception as e:
        self._note_failure("原文落链失败", e)
        anchor_seq = 0
    try:
        event._mc_anchor_seq = anchor_seq
    except Exception:
        pass
    raw = (getattr(req, "prompt", "") or "").strip()
    # 坏感度（B4）：给注入链路的读接口。读失败按 0 处理不阻塞聊天
    # （get_attitude 内部已吞异常返 0），这一轮先记进注入观测账。
    speaker_id, _sender_name = _sender_of(event)
    attitude = self.ledger.get_attitude(speaker_id)
    # 空提示词就什么都不唤起。
    if not raw:
        self._log_injection("empty_prompt", attitude=attitude)
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
    # 写法必须是 (tick - 1) % N == 0（R8）：旧写法 tick % N == 1 在
    # N=1 时余数恒为 0，条件永远不成立，维护一轮都不会跑。
    self._maintain_tick += 1
    if (self._maintain_tick - 1) % MAINTAIN_EVERY == 0:
        # 每天第一次请求前顺手刷一次 记忆账本.md，不额外挂定时器
        try:
            self.ledger.maybe_export_daily(str(EXPORT_PATH))
        except Exception as e:
            self._note_failure("每日导出失败", e)
        # 顺手看一眼工作记忆有没有超员：有冷却，没超员就什么都不做
        try:
            self.ledger.maybe_rebalance_working_set()
        except Exception as e:  # 换班失败也不能影响聊天
            self._note_failure("工作记忆换班失败", e)
        # WAL 只涨不落会让库旁边的 -wal 越堆越大：超阈值就截断一次
        try:
            self.ledger.maybe_checkpoint_wal()
        except Exception as e:  # 截断失败不影响聊天
            self._note_failure("WAL 截断失败", e)
    try:
        with_profile = (time.time() - self._profile_injected_at) >= PROFILE_REFRESH_SECONDS
        # coexist 条件判定需要语境（R3）：至少会话场景与当前时间。
        # 场景只给真实拿得到的会话标识，拿不到就整体传 None——判不出
        # 条件的记忆按保守处理（不主动浮现），不许编造环境数据。
        scene = (event.unified_msg_origin or "").strip()
        context = {"scene": scene, "now": time.time()} if scene else None
        # B1：scope_key 落到当前会话——检索、联想、激活都不跨会话；
        # B3：群聊只注入「关于主人的 public 画像」，owner 私聊给全部
        # （缺省口径 private，没有可见性行的画像群聊一律不给）。
        result = self.ledger.activate(query, slots=self.INJECT_SLOTS,
                                      with_profile=with_profile, context=context,
                                      scope_key=scene,
                                      profile_visibility=("public" if _is_group(event) else ""))
        stats: dict = {}
        brief = self.ledger.render_brief(result, max_chars=self.BRIEF_BUDGET,
                                         stats=stats)
    except Exception as e:  # 唤起失败绝不能影响聊天本身
        self._note_failure("唤起失败", e)
        return
    # 人格分区注入（v7）：人格先于记忆。独立预算（persona_text 自带
    # 1200 字默认），与 render_brief 互不挤占；整段 try/except，
    # 人格炸了只记失败原因，回复照常——人格是加分项不是门槛。
    # A2：注入前先核封印（全项目第三处校验点，前两处在启动与自检）。
    # 封印不符或缺失时 core 段整段不注入、self 分区照常；失败进
    # 失败计数不许静默；不自动重封、不改库——修封印是人工的事
    # （ reseal_persona_core(confirm=True) ）。
    try:
        seal = self.ledger._verify_persona_core_seal(context="inject")
    except Exception as e:
        self._note_failure("人格封印校验失败", e)
        seal = {"ok": False}
    try:
        if seal.get("ok"):
            persona = self.ledger.persona_text()
        else:
            persona = self._persona_text_without_core()
            self._note_failure(
                "人格封印校验未通过，core 段未注入",
                LedgerError("core 人格哨兵不符或缺失（meta 存 %r）"
                            % seal.get("stored", "")))
    except Exception as e:
        self._note_failure("人格注入失败", e)
        persona = ""
    # A3：人格是资料不是指令——先清 C0/C1 控制字符（保留 \n \t），
    # 再包上围栏。空人格不围，免得往 system prompt 里塞空壳。
    persona = _PERSONA_CTRL_RE.sub("", persona)
    if persona.strip():
        persona = (PERSONA_FENCE_HEAD + "\n" + persona + "\n"
                   + PERSONA_FENCE_TAIL)
    self._log_injection("injected" if brief else "no_hit",
                        query_len=len(query), stats=stats, result=result,
                        long_prompt=long_prompt,
                        prompt_len=len(raw) if long_prompt else 0,
                        attitude=attitude)
    if not brief and not persona:
        return
    if result.get("profiles"):
        self._profile_injected_at = time.time()
    base = getattr(req, "system_prompt", "") or ""
    body = "\n\n".join(s for s in (persona, brief) if s)
    req.system_prompt = (base + "\n\n" + body) if base else body


def record_reply(self, event, resp, _sender_of, /):
    try:
        text = (getattr(resp, "completion_text", "") or "").strip()
        if text:
            self.ledger.append_message(
                "assistant", text, session_id=(event.unified_msg_origin or ""))
    except Exception as e:
        self._note_failure("回复落链失败", e)
        return
    # 沉淀是锦上添花：它炸了只能计数，绝不能连累回复。
    # B1/B2：只沉本会话原文，scope 落本会话，发送者随沉淀入库。
    speaker_id, speaker_name = _sender_of(event)
    try:
        self._auto_sink(session_id=(event.unified_msg_origin or ""),
                        speaker_id=speaker_id, speaker_name=speaker_name)
    except Exception as e:
        self._note_failure("自动沉淀失败", e)

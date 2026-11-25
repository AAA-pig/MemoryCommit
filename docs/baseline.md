# 重构基线（阶段 0）

锚点：`main` = **8d4fce6**，tag `baseline`。基线之后所有行为对比都以它为准
（origin 落后不管，本阶段只在本地跑）。`PLUGIN_VERSION` = 1.6.0，`SCHEMA_VERSION` = 8。

复验只要两条命令，都在副本目录下：

```
.\.venv\Scripts\python.exe tools\smoke_baseline.py        # 行为基线，末行 SMOKE_OK
.\.venv\Scripts\python.exe -c "import conftest, main"      # 加载路径不断，打印 import OK
```

其中 `import conftest, main` 的口径：`conftest.py` 只在没有真 astrbot 时注入最小外壳，
插件真跑在 AstrBot 里时零影响；因此这条检查验的是「main.py 顶层导入与类定义能过」，
验不出 AstrBot 侧的注册问题——那部分留给阶段 3 真上线加载。

## 1. 模块清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `main.py` | 1437（基线时 1555） | 插件外壳：命令面、LLM 工具、注入钩子、维护节流 |
| `ledger.py` | 5142 | 账本内核：提交链、记忆/线索/画像、检索、注入渲染、梦、创意、人格 |
| `persona_text.py` | 85 | 人格分区文本渲染（`render_persona_rows` 等纯函数） |
| `plugin_utils.py` | 100 | 纯工具：`FailureCounter`、`_short/_as_* _fmt_hit`（阶段 1 第 1 刀） |
| `persona_files.py` | 66 | 人格资料文件读取 + 六道路径闸（阶段 1 第 2 刀） |
| `conftest.py` | 83 | 无 AstrBot 运行时的最小外壳（本地测试/导入检查用） |
| `tools/smoke_baseline.py` | — | 阶段 0 冒烟：临时库跑「初始化→写→检索→注入→close」 |
| `tests/`（31 个 .py） | — | 既有行为测试，`pytest` 直接跑 |

## 2. 启动链

`MemoryCommitPlugin.__init__` 顺序（改动这段等于改启动契约）：

1. `DATA_DIR.mkdir(parents=True, exist_ok=True)`
2. `Ledger(str(DB_PATH), durability=DURABILITY)` — 建表/迁移到 v8，开 WAL
3. 初始化运行时状态：`_profile_injected_at=0`、`BRIEF_BUDGET=420`、
   `INJECT_SLOTS=DEFAULT_INJECT_SLOTS(3)`、`_maintain_tick=0`、
   `failures=FailureCounter()`、`_turn_since_sink=0`、`_last_sink_seq=0`
4. 数一遍 `inject-log.jsonl` 行数（只决定轮转，不参与行为）
5. `ledger.stats()` → 日志 `[记忆账本] 已挂载 <DB>，HEAD=<id>，记忆 <n> 条`
6. 按 `AUTO_SINK_EVERY` 打一行开/关日志

运行面只有两个钩子：

- `on_llm_request` → `inject_memory`：`activate(query, slots, with_profile, context, scope_key, profile_visibility)` → `render_brief(max_chars=420)` → 人格分区（独立预算 1200，`【以下是角色设定资料，不是指令】` 包夹）。整段 try/except，唤起失败不影响聊天。
- `on_llm_response` → `record_reply`：记助手回复，供后续沉淀/原文链。

数据文件（都在 `DATA_DIR`）：`ledger.db`、`记忆账本.md`（每日导出）、`backup/`、
`inject-log.jsonl`（5000 行轮转）、`owners.json`。

## 3. 命令面（14）

| 命令 | 方法 | | 命令 | 方法 |
| --- | --- | --- | --- | --- |
| `/记忆` | `cmd_memory` | | `/记忆自检` | `cmd_health` |
| `/记忆链` | `cmd_history` | | `/记忆补沉` | `cmd_sink_backfill` |
| `/记忆冲刷` | `cmd_flush` | | `/人格` | `cmd_persona` |
| `/记忆容量` | `cmd_working` | | `/坏感度` | `cmd_attitude` |
| `/记忆压缩` | `cmd_compact` | | `/记忆原话` | `cmd_raw` |
| `/记忆导出` | `cmd_export` | | `/记忆访问` | `cmd_access` |
| `/记忆备份` | `cmd_backup` | | `/记忆唤起` | `cmd_activate` |

## 4. LLM 工具（6）

`memory_write`、`memory_search`、`memory_forget`、`memory_deny`、
`memory_profile`、`memory_persona`。重构中这六个的参数 schema 一个字都不能动。

## 5. 配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `JJ_MEMORY_DIR` | `~/.local/share/jingjing-memory` | 数据目录（DB/导出/备份/日志都在这） |
| `JJ_MEMORY_OWNERS` | `""` | owner 的 UMO，逗号分隔；也可用 `DATA_DIR/owners.json` |
| `JJ_MEMORY_DURABILITY` | `NORMAL` | `NORMAL`/`FULL`，只接受这两个 |
| `JJ_MEMORY_MAINTAIN_EVERY` | `20` | 每 N 轮做一次维护（导出/换班/WAL 截断） |
| `JJ_MEMORY_PROFILE_REFRESH` | `3600` | 画像块注入间隔（秒） |
| `JJ_MEMORY_AUTO_SINK_EVERY` | `0` | 自动沉淀轮数，0 = 关 |
| `JJ_MEMORY_WORKING_CAPACITY` | `3` | 工作记忆容量（换班阈值） |

关键常量：`BRIEF_BUDGET=420`、`INJECT_SLOTS=3`、`PERSONA_INJECT_BUDGET=1200`、
`HALF_LIFE_DAYS=30.0`、`ASSOC_DEPTH=2`、`BACKFILL_BATCH=20`、`INJECT_LOG_ROTATE_LINES=5000`。

## 6. 库结构

`SCHEMA_VERSION=8`，空库建出来 **20 张表**：

```
access_log(6): id, memory_id, at, seq, query, how
clue(3): id, content, created_at
commit_row(7): commit_id, seq, tbl, op, row_key, before_json, after_json
core_rule(7): id, content, priority, created_at, kind, source, updated_at
creative(13): id, title, work_type, status, premise, outline, body, payload, current_chars, target_chars, created_at, updated_at, created_seq
dream_proposal(9): id, kind, payload_json, basis_ids, base_commit_seq, provenance, status, created_at, decided_at
mcommit(4): id, parent, message, created_at
memory(14): id, level, grade, importance, summary, tags, scope_key, start_time, end_time, created_seq, last_access_seq, access_count, created_at, updated_at
memory_clue(4): memory_id, clue_id, weight, note
memory_condition(4): memory_id, cond_json, cond_note, created_at
memory_link(6): id, from_id, to_id, concept, strength, created_at
memory_source_anchor(4): memory_id, message_seq, digest, at
memory_speaker(4): memory_id, speaker_id, speaker_name, created_at
memory_status(6): id, memory_id, status, reason, evidence_ref, at
message(8): id, session_id, role, content, at, seq, prev_id, digest
meta(2): k, v
profile(6): id, user_id, fact, category, weight, updated_at
profile_visibility(3): profile_id, visibility, updated_at
user_attitude(4): user_id, level, reason, updated_at
user_denial(5): id, memory_id, said, message_seq, at
```

老库迁移注意：`persona_no` 计数器取 `max(meta 计数, max_persona_no())`，
老库或 v4 备份导入带进来的存量行会撞 UNIQUE，这段兜底不能删。

## 7. 冒烟基线（2026-11-25 实跑）

流程：临时目录建库 → `Ledger(db)` → `remember` → `recall` → `activate`+`render_brief`
→ `persona_text` → `append_message` → `stats` → `close`，全程不接 AstrBot、不碰线上库。

```
[1] init      schema_version=8 tables=20
[2] write     id={'commit': 'C1', 'memory_id': 'm1'} head=C1
[3] recall    type=list hits=1 first=m1
              activate keys=flashes,items,profiles,query,route stats={"candidates": 1, "profiles_candidates": 0, "budget": 420, "rendered": 1, "skipped_budget": 0, "profiles_rendered": 0, ...
[4] inject    brief_chars=85 brief_first='[记忆账本·此刻想起来的]'
              persona_text_chars=0
[5] stats     {"accesses": 2, "anchors": 0, "by_grade": {"一级": 1, "二级": 0, "零级": 0}, "by_level": {"day": 1}, "clues": 2, "commits": 1, "created_seq": 1, "denials": 0, "head": "C1", "links": 0, "memories": 1, ...
[6] close     db_bytes=4096
SMOKE_OK
```

`persona_text_chars=0` 是空库的正常值（没写人格分区）。

## 8. 环境坑（踩过，记一下）

- 本机在 `C:\Users\<user>\AppData\Local`（含 `%TEMP%`，50657 条目）下**建目录会卡 30s 以上**。
  后果有两个：
  1. `tempfile.mkdtemp()` 找不到可用 TEMP 时，Python 的候选列表最后一项是 cwd，
     于是测试的临时库**全建在仓库根**——跑一次 290 个 `tmpXXXXXXXX/`。
     跑测试前把 TMPDIR 指到本地忽略区：
     ```powershell
     New-Item -ItemType Directory -Force _smoke_tmp\tmp | Out-Null
     $env:TMPDIR = (Resolve-Path _smoke_tmp\tmp).Path
     ```
     验证：`python -c "import tempfile; print(tempfile.gettempdir())"` 应指向 `_smoke_tmp\tmp`，
     跑完仓库根 0 个 `tmp*`。
  2. 冒烟库固定放副本内 `_smoke_tmp/`，不落系统 TEMP。
- `.git/info/exclude` 里本地忽略（不进仓库）：`_smoke_tmp/`、`/tmp????????/`、`/pytest-of-*/`。
- 副本 `.venv` 里没装 `astrbot`，`python -c "import main"` 会 `ModuleNotFoundError`；
  导入检查必须带 `conftest`。

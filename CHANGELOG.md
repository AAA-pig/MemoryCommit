# CHANGELOG

## 1.6.0 — 2026-11-14

按外部审查报告（《MemoryCommit 项目代码审查与改进方案》2026-11-14）做的可靠性加固轮：备份完整性、有损导入、安全表述、持久性档位、时间契约。2026-11-15 追加 v6 创作项目旁路表（同为本版内容，见下方 v6 段）。

### 破坏性变更（升级前先读这几条）

- **备份格式 v3（DUMP_VERSION 2→3）**：access_log 纳入 JSONL 备份。旧版本代码（只认 v2）读到 v3 备份会在文件头拒收（「备份格式 v3 比当前支持的 v2 新」）——先升级代码再导入；反过来 v2 及更早的老备份在新代码里照常导入，只是里面本来就没有访问明细
- **有损导入默认拒绝**：备份里带当前库不存在的列时，load_jsonl 默认抛错（报错列出 表.列，原库一字节不动）；确认接受降级导入须显式传 allow_lossy=True，行为同旧版（照灌并在结果里报告丢列）
- **备份格式 v4（DUMP_VERSION 3→4，2026-11-15）**：creative 表纳入 JSONL 备份。旧代码（只认 v3）读到 v4 备份会在文件头拒收（「备份格式 v4 比当前支持的 v3 新」）——先升级代码再导入；反过来 v3 及更早的老备份在新代码里照收，只是里面本来就没有创作项目
- **替换式恢复不再清空本地创作（2026-11-15）**：`load_jsonl(..., replace=True)` 过去按 DUMP_TABLES 逐表 `DELETE`，而 creative 已进 DUMP_TABLES——拿一份不含 creative 的旧备份做替换式恢复，会把本地所有创作项目（正文，提交链里重放不出来）静默删光。现在清空只清「备份真的带来」的表：备份不含 creative 就原样保留本地创作，并在 `warnings` 里报「备份不含 creative 表（结构 vN），本地 M 条创作已保留」；备份带 creative（结构 v6 / 格式 v4）才照常整体替换。结构版本 < 6 或文件头 tables 无 creative 时，`warnings` 一律给提示

### P0-1 备份补 access_log

- DUMP_TABLES 末尾加 access_log、_DUMP_ORDER 补稳定排序 id、DUMP_VERSION 2→3。access_log 虽是「体温计」不是账本事实，但近期命中、工作记忆复出、访问审计都按时间窗口吃它的明细——少了它，dump/load 之后「整本原样搬家」实际只搬了半截
- 往返测试：多条访问记录（公开 API 触发 + 直插跨时间窗口旧记录）dump→新库 load 后行数/内容/id 逐行一致、stats().accesses 一致、health_check 通过；v2 老备份手工摘掉 access_log 段与行数清单后仍可导入（访问明细 0，不假装恢复出来）
- 变异自检：从 DUMP_TABLES 删掉 access_log，两条当场红；恢复后绿

### P0-3 有损导入预检

- load_jsonl 加 allow_lossy=False：落库前预检（只读目标库表结构，走在 _tx 与恢复预演之前——预演库也好、正式库也好，都不许先写半截再回头拒绝），发现丢列抛 LedgerError 列出 表.列 并提示可传 allow_lossy=True；allow_lossy=True 保持旧行为
- 两条旧测试按新行为改造（原断言有意期望静默成功）：test_load_drops_unknown_columns（v2 严格路径）与 test_load_jsonl_skips_bad_rows_and_reports（v1 宽容路径）都改为默认拒绝 + allow_lossy=True 对照旧行为——宽容只宽容坏行，不宽容静默丢列
- 变异自检：把预检短路掉，两条当场红；恢复后绿

### P0-2 哈希链表述纠偏

- 全仓库「不可篡改」只在 message 表注释与 append_message docstring 两处（README 没这说法），改为准确表述：哈希链提供篡改检测与内部一致性校验（单条改动、中间删除、尾部截断查得出），不防重算整条链，也不等于来源可信；更强保证要靠库外独立锚定。本轮只改表述，不实现外部锚定

### P1-4 持久性档位显式化

- Ledger.__init__ 加 durability 参数：默认 NORMAL（行为不变），只接受 NORMAL/FULL（大小写规范化），非法值抛 LedgerError 拒开。NORMAL 偏性能（断电可能丢最近几秒写入），FULL 每次同步落盘（断电几乎不丢，每次写入付 fsync）
- main.py 新配置 JJ_MEMORY_DURABILITY（默认 NORMAL），非法值同样显式失败；把账本当唯一记忆源的建议设 FULL
- 测试：默认 NORMAL（PRAGMA synchronous=1）、full 规范化 FULL（=2）、非法值显式失败

### P1-5 时间契约写明（不动数据）

- now_str / _parse_time_cached / _parse_time 补契约 docstring：账本时间是本地时间、无时区偏移，解析按运行机器本地时区（mktime 语义）；跨时区搬库后时间差、每日导出日期、访问窗口可能偏移
- 统一 UTC 要牵动存量数据的解释（迁移级改动），本轮只写契约不迁移；新增测试固定这个已知限制，哪天改 UTC 这条测试就是第一个要改写的见证

### v6 创作项目旁路表（creative，2026-11-15 追加）

- **SCHEMA_VERSION 5→6**：新增旁路表 `creative`（id 主键 / title / work_type / status / premise / outline / body / payload / current_chars / target_chars / created_at / updated_at / created_seq）+ 索引 `idx_creative_seq`。纯加法：`_migrate_5_to_6` 走 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，重复执行不出错，也不改任何存量表
- **DUMP_VERSION 3→4 / DUMP_TABLES 加 creative**：创作正文与原始档必须跟着搬家，否则「整本原样搬家」又成谎报；`_DUMP_ORDER` 给 `created_at, id` 稳定排序
- **三个公开方法**：`upsert_creative`（唯一写口：同 id 覆盖但保留 created_at / created_seq）、`get_creative`（按 id 读正文与 payload）、`list_creatives`（只带 id/title/work_type/status/current_chars 轻量字段）。边界写死：旁路表直写事务，**不进提交链、不碰 memory 表与任何检索打分**（创作不是记忆内容的变更，是另一条业务线的归档）
- **替换式恢复的创作保护**：见上方「破坏性变更」第三条——备份不含 creative 时保留本地创作并报数，另补测试（构造结构 v3 备份 + 本地有创作，断言行数与 warnings 文案）
- **入公开接口清单**：`tests/test_memory_duty.py` 的 LEDGER_PUBLIC_METHODS 补这 3 个方法（契约测试同步，不是偷偷放宽）
- `.gitignore` 补 `.coverage` / `.coverage.*` / `.temp/`（覆盖率数据与本轮临时件不入库）
- **测试**：默认环境 **519 passed / 0 failed**（本轮基线 491：+7 回填的覆盖率补测（`tests/test_*_coverage_bridge.py`，来自上一轮桥接会话的备份回填，SHA256 与工作空间备份逐字节一致，覆盖真身 10 例里没有的 6 个场景）、+4 恢复保护用例（`test_creative.py::RestoreCreativeTest`）、+17 分支覆盖用例（`tests/test_branch_coverage_v6.py`））；变异自检——把「备份不含 creative 就不清空」的守卫去掉，`test_replace_restore_with_v3_backup_keeps_local_creative` 当场红（`0 != 2：旧备份恢复把本地创作删光了`），恢复后绿
- **覆盖率（只认 coverage 输出，2026-11-15 实测）**：上一版记的「本机没装 coverage、装不上」是错的——`coverage 7.17.3` 装在某个装了 coverage 的虚拟环境（Python 3.12.15）里，只缺 pytest-cov 插件；不用装任何东西，直接 `python -m coverage run --branch --source=. -m pytest -q` 再 `python -m coverage report --include="ledger.py,main.py,conftest.py"`。口径：只统计 `ledger.py` / `main.py` / `conftest.py`，`tests/` 自身不计入，行与分支双 100%。实测行覆盖 conftest 44/44、ledger 2139/2139、main 583/583（TOTAL 2766 语句 0 缺失）；分支 1098 个里原先 21 个 partial，已逐条补测到只剩 1 个：`ledger.py` 904→912。这一处**物理不可达**——`classify_conflict` 第 1 条（场景不同 → 892 提前 return）与第 2 条（两窗不重叠 → 895 提前 return）已经先拦掉，能走到 904 的输入里 `not (scene_a and scene_b and scene_a != scene_b)` 与 `(not win_a or not win_b or _windows_overlap(...))` 两半恒为真，false 分支没有输入能触发。**没有用 `pragma: no cover` 凑数**，按约定报上去等判定；判定通过后才在这里记「哪一行、为什么排除」
- **剩下 21 条 partial 的去向**（report -m 清单 → 现测试，逐条见 `tests/test_branch_coverage_v6.py`）：904→912 不可达（见上，v7 已用等价重构消除）；1561→1563 提交上下文里抛异常不封卷；1582→1579 `row_key` 空键名跳过；1806→1809 / 1818→1821 无 id 的行不进词表缓存；2209→2203 两条线索同强度并列只留先到的；2576→2581 闪现整句超预算就丢；2885→2884 / 2888→2887 换班计划里的过期条目不计成功；2957→2955 / 2959→2955 冲刷计划里的过期条目不计删/降；3004→2992 删除行没有快照就跳过；3039→3024 / 3044→3046 / 3047→3024 回放里认不出或改不了的脏行跳过；3223→3215 梦提案 payload 没有 memory_id；3665→3671 不带路径导出；3692→3694 表没登记排序时退化成无排序导出；4022→4019 主键为 NULL 的行算不出键就跳过；`main.py` 158→160 沉淀原文切不出关键词；782→784 访问记录没有查询词

### v7 打回返工（2026-11-15）

上一版报的「904→912 分支物理不可达，等判定」被驳回了：**不批 pragma，要等价重构**。这轮就是这么改的。

- **classify_conflict 死分支消除（等价重构，不是遮盖）**：删掉第 4 段原来的 `if not (scene_a and scene_b and scene_a != scene_b) and (not win_a or not win_b or _windows_overlap(...)):` 两行条件（原 904–905），把 env 比较与 `_mutex_hit` 的 body 平铺到同一层，注释写明「能走到这里，前提是 892/895 两处提前返回仍在」。行数 2139→2138、分支 894→892 是**删掉死代码**的自然结果，测试与语义一行没变
- **等价性证明（可复跑）**：把 HEAD 版 `ledger.py` 与新工作区版同时加载，对 `classify_conflict` 跑 414720 组组合（场景 × 时间窗 × 环境 × 视角 × 摘要对，含空条件、空串、坏日期），**逐组返回值比对 0 差异**，五类返回值命中数：scene 51840 / temporal 181440 / perspective 68040 / true_mutex 15120 / undetermined 98280
- **新增 `tests/test_classify_conflict_guards.py`（5 例）**：把重构依赖的前提钉死——场景不同 + 词面互斥仍是 scene_dependent、时间窗不重叠 + 词面互斥仍是 temporal、时间窗重叠或无窗时 true_mutex 照旧可达、时间窗重叠但 env 不同仍是 undetermined（env 那道闸没被顺手删掉）
- **`tests/test_creative.py` 入库（15 例，此前一直是未跟踪 ??）**：11 个创作表用例 + 4 个替换式恢复保护用例；顺手修了一处路径大小写——文件里写的是 `Ledger.py`，Windows 上碰巧能打开，Linux/大小写敏感文件系统直接 FileNotFoundError，改成 `ledger.py`
- **`tools/coverage_check.ps1` 去掉写死的本机解释器路径**：依次探测 `$env:PYTHON` → PATH 里的 `python` → `python3`，每个都用 `-m coverage --version` 实地验证（返回码 0 且输出含 `Coverage.py`），第一个能跑的才用；全失败给出明确报错并提示 `-Python` 显式指定；`-Python` 指定的解释器同样要过这道验证。UTF-8 BOM、先跑全量测试、`--fail-under=100` 退出码透传三个行为不变
- **覆盖率数字（2026-11-15 实测，命令与输出如下）**：
  - `python -m coverage run --branch --source=. -m pytest -q` → `524 passed in 24.96s`
  - `python -m coverage report --include="ledger.py,main.py,conftest.py" --fail-under=100` → `conftest.py 44 0 6 0 100%` / `ledger.py 2138 0 892 0 100%` / `main.py 583 0 198 0 100%` / `TOTAL 2765 0 1096 0 100%`，退出码 **0**
  - `powershell -File tools\coverage_check.ps1`（`$env:PYTHON` 指向 coverage 7.17.3）→ 末尾 `[coverage_check] 行 + 分支 100%，过`，退出码 **0**
- **`.coverage` 不进真身**：真身插件目录里发现过一个未跟踪的 `.coverage`（53248 字节，19 个源文件 / 16 个测试文件，说明有人在运行中的插件目录跑过 pytest），已删除；`.gitignore` 覆盖 `.coverage` / `.coverage.*`；**以后一律不在真身目录跑 pytest / coverage，测试只在沙盒跑**

### v8 打回返工（2026-11-15）

上一轮打回两条：解释器探测只抽了一半（`rollback_drill.ps1` 还写死本机路径）、conftest 混进生产覆盖率门禁。这轮收掉，并把 schema 5→6 的升级演练补齐。

- **解释器探测抽成公共文件 `tools/resolve_python.ps1`**：`Test-PythonModule` / `Get-PythonCandidates` / `Resolve-PythonExe` / `Resolve-PythonExeOrThrow` 四个函数，只定义函数无副作用，供两个脚本 dot-source。候选顺序 `$env:PYTHON` → `$RepoRoot\.venv\Scripts\python.exe` → PATH 的 `python` / `python3`，逐个实地跑 `-m <模块> --version` 验证，坏的 venv（base python 已不存在）会被跳过，全失败才显式报错。`rollback_drill.ps1` 第 28 行的 `[string]$Python = "..."` 默认值删除，改为 `Resolve-PythonExeOrThrow -RequireModules @("pytest")`；`coverage_check.ps1` 删掉自己那份 `Test-CoveragePython` / `Resolve-PythonExe` 副本，改用公共函数。**演练逻辑一行未改**
- **conftest.py 移出生产覆盖率门禁**：门禁只认 `ledger.py` / `main.py`（`--fail-under=100`），conftest 作为测试夹具单独报一次数字、不影响退出码；脚本注释与 README「覆盖率口径」都写明了为什么（它不是随插件发布的逻辑，算进总账只会让数字好看）。需要旧口径时加 `-IncludeConftest`
- **CHANGELOG 脱敏**：删掉残留的本机绝对路径，改为「某个装了 coverage 的虚拟环境」
- **schema 5→6 升级演练（副本上做，真库零写入）**：三件套（`.db` + `-wal` + `-shm`）一起拷；在 02:54 的 v5 备份（schema 5、17 表、无 creative）上跑 `Ledger()` 打开：schema 5→6，`creative` 与 `idx_creative_seq` 建出，memory 576 / clue 1134 / message 898 / access_log 1932 / memory_clue 3468 逐表不变，`integrity_check=ok`，二次打开完全幂等（不再迁移、不建提交）。`_migrate_auto_sink_legacy` **会**额外建 1 个提交（C95→C96）：36 条旧沉淀补 `type:auto_sink`、摘 252 条机器标签边（`memory_link` 15142→14890）。方案与回滚条件见 `docs/升级-schema5-to-6-方案.md`
- **实测**：`tools/coverage_check.ps1` → `524 passed`，`ledger.py 2138/2138 + main.py 583/583`（分支 892+198）双 100%，退出码 0；conftest 单列 44/44、6/6。`tools/rollback_drill.ps1` 实跑：场景 A 红（failed=19，报警器有效）；场景 B 红（failed=3）——**这是脚本 B 场景的已知盲点，不是回滚点坏了**：B 只按 `git ls-tree` 名单删掉新增测试文件、不还原名单内文件的旧内容，`tests/test_ledger.py` / `test_memory_duty.py` 留着 HEAD 的新断言（`test_schema_is_v5` 等）去跑 v5 代码，必然红。上轮 B 绿是因为当时这两个文件还没被 `1149e3b` 改过

### v9 打回返工（2026-11-15）

上一轮打回四条 + 一条小尾巴：改动没提交（只有工作区，与「必须已提交、与真身逐字节一致」的口径不符）、升级文档只是嘴上说写了（`docs/` 里查不到）、文档缺一条排障警示、数据口径要拿实测补齐、CHANGELOG 里残留本机绝对路径。

- **落库**：本轮改动（`tools/resolve_python.ps1` 新增、`tools/coverage_check.ps1` / `tools/rollback_drill.ps1` / `README.md` / `CHANGELOG.md` 修改、`docs/升级-schema5-to-6-方案.md` 新增）一次性提交并 push 到 `origin/main`，真身 `git pull --ff-only` 到同一提交；两侧 `git rev-parse HEAD` 相等、`git status` 都干净，`ledger.py` / `main.py` / `conftest.py` / `tools` 下四个 `.ps1` 的 SHA256 双证一致。作者按「已提交」口径向上一级回传，不再留未跟踪文件
- **升级文档真有**：`docs/升级-schema5-to-6-方案.md`（约 280 行）。含三件套备份的确切命令、副本预演逐表行数对照表、卸载 → 重载 → 验证七步与逐条判定阈值表、回滚触发条件与已知代价、以及第 0 节的排障警示
- **新增排障警示（第 0 节）**：只读探真库必须 `file:...?mode=ro`，**不能加 `immutable=1`**——加了会跳过 WAL，读到 checkpoint 之前的旧快照，表现是「迁移明明跑了，看着像没跑」。同一份三件套副本上实测对照：`mode=ro` → schema 6 / C101 / 18 表 / creative 存在 / message 932 / access_log 1983 / memory_link 14890；`mode=ro&immutable=1` → schema 5 / C99 / 17 表 / 无 creative / message 928 / access_log 1977 / memory_link 15166。上一轮「真库还是 schema 5」的误判就是这条参数造成的
- **数据口径补上取数时刻**：`message 930` / `access_log 1980` 是 **04:14:53** 那份三件套副本的读数（`-wal` 383192 字节，探针 04:22:46 跑完），不是「现在的读数」。04:31:05 复测同一真库：`message 932` / `access_log 1983`，其余逐表一分不差——只增表在 WAL 里继续涨是正常现象，不是数据异常。文档第 1 节把两次读数并排列出
- **`memory_link` 少 7 条：给出可复现命令与两组数字**（不再只说「一模一样」）。A 组真库副本：当前 14890 / 重放 14897 / 差 −7 / 缺失 7 条 / 多出 0 / 内容不同 0；B 组对照组（打开前把 `_migrate_auto_sink_legacy` 换成空实现，同一份 v5 备份）：当前 15142 / 重放 15149 / 差 −7 / 缺失同样的 7 条 / 多出 0 / 内容不同 0。7 条为 `m3|m1|记忆账本`、`m3|m2|记忆账本`、`m4|m3|方向`、`m4|m3|老大交代`、`m4|m3|脱离AstrBot`、`m5|m3|脱离AstrBot`、`m5|m4|脱离AstrBot`；查 `commit_row` 确认它们分别由 C8 / C10 / C14 `insert`、**链里没有任何 delete 记录**，属早期绕过提交链删库留下的历史欠账。脚本 `drill_link_diff.py` / `drill_link_trace.py` 已落在演练目录，可复跑
- **脱敏**：`CHANGELOG.md` 里最后一处本机绝对路径删掉（改为「某个装了 coverage 的虚拟环境」）；`docs/升级-schema5-to-6-方案.md` 里的数据目录改成 `%USERPROFILE%\.local\share\jingjing-memory`，不再写死用户名
- **实测**：`524 passed / 0 failed`；`tools/coverage_check.ps1` → `ledger.py 2138/2138 + main.py 583/583`（分支 892+198）双 100%，conftest 单列 44/44、6/6，退出码 0

### 人格分区（schema v7）与手写导入（2026-11-15）

- **结构**：`core_rule` 加 `kind` / `source` / `updated_at` 三列（迁移 v6→v7，存量行按 `kind='self'` 认领）。core 分区只读，self 分区可增删改
- **三道闸**：`restricted_auto_write` 反向守卫（自动路径里一律拒，连 self 分区也不行）、`user_persona_write` 通行令牌（默认关，只有 `/人格`、`memory_persona`、显式播种会开）、`CommitBuilder._push` 底层兜底（绕过写方法直接落 core_rule 同样拒）
- **静态白名单**：`tests/test_persona_write_surface.py` 用 AST 扫全仓，core_rule 落库点与人格写入口各点名一份清单——多一个位置红（有人加了写路径没说），少一个也红（白名单过期等于谎报覆盖）
- **哈希哨兵**：meta 存 core 行（id/content/priority）摘要。手工 SQL 改库、备份回灌串号都会露出来；只记 warning、不阻断启动、不自动修，人工确认后 `reseal_persona_core(confirm=True)` 重封
- **手写导入**：新增 `persona_text.py`（纯解析，不碰库、不开提交）与 `Ledger.add_persona_batch`（整批一个提交，闸与 `add_persona` 同一道）。`/人格 导入 多行文本`、`/人格 导入文件 路径`（UTF-8，≤128KB）。一行一条，`[权重]` 前缀可省，`#` 开头是注释；先全量挑错，一条不合格整批不写——半批写进去比全不写更难收拾
- **实测**：`621 passed / 0 failed`；`tools/coverage_check.ps1` → `ledger.py 2428 语句 / 1014 分支`、`main.py 734 / 260`、`persona_text.py 45 / 20`，行 + 分支全 100%，退出码 0（persona_text.py 本轮并入生产门禁）

### 测试

- 默认环境：470 passed（基线 464：新增 6——access_log 往返、v2 无 access_log 兼容、durability 三条、时间契约一条；改造 2——两条有损导入旧测试）
- 开关环境（DREAM_SCHEDULER_ENABLED=true + EMOTION_ENABLED=true + JJ_MEMORY_AUTO_SINK_EVERY=6）：470 passed（**2026-11-15 复跑存疑**：这三个环境变量会把模块常量直接翻成 true，`test_switch_defaults_false` 一类「默认应为 false」的断言必然红，本轮实测是 498 passed / 4 failed。照这组环境跑不可能是全绿，这行记录待核对，未擅自改写原结论）
- 全部用临时库，未碰生产账本
- 2026-11-15 v6 收尾复核：默认环境 **519 passed / 0 failed**；覆盖率封装成 `tools/coverage_check.ps1`（`--branch --source=.`，`report --fail-under=100`，只统计 ledger.py / main.py / conftest.py）。当时行覆盖 2766/2766 语句 0 缺失，分支 1098 个里 1 个 partial（ledger.py 904→912，物理不可达，未加 pragma，等判定）
- 2026-11-15 v7 打回返工后：默认环境 **524 passed / 0 failed**（+5 守卫用例，`test_creative.py` 15 例正式入库）；**行 2765/2765、分支 1096/1096 双 100%**，`--fail-under=100` 退出码 0。上一个 partial 是被等价重构消掉的，不是 pragma 遮掉的

### 未做（写清楚，别当成已实现）

- **P1-1 核心文件拆分**：ledger.py 仍是单文件多职责（检索/生命周期/备份/自检都在里面）。原因：拆分前要先补公开接口契约测试与 golden dataset 对账，牵动全部测试基线，值得单独一轮做，塞进加固轮只会拆一半留烂尾
- **P1-2 检索评测集**：没有建立评测查询集与指标脚本（Recall@5 / MRR 等）。原因：需要标注数据与固定数据集版本，属于建立质量基准的新工作，不是本轮「修对错」的范围内
- **P1-6 provenance 来源体系**：记忆没有统一来源字段与权限矩阵（user_explicit / auto_summary 等）。原因：动表结构要 schema 版本迁移，还要理清与现有 tags / type: 机器标记的关系，需要单独设计
- **P0-2 外部锚定**：只把安全表述说准，没有实现链头导出 / HMAC / 签名。原因：涉及密钥管理与外部存储选型，是独立的工程决策
- **P1-5 时间统一 UTC**：只写契约不动数据。原因：存量时间都是本地时间，迁移必须带上「旧机器时区」的假设，牵动所有按日期计算的逻辑，需要带数据迁移的专门一轮

## 1.5.2 — 2026-11-14

第三轮锐评（fb9dfa4）的修复：让「沉淀不进提示词」对存量数据也成立，并把沉淀在存储层降回索引。

### N6 存量沉淀识别与迁移

- **识别口径与建图口径同源**：`auto_sink_ids()` 同时认完整标签 `type:auto_sink` 与「自动沉淀」（`AUTO_SINK_MARKERS`），与 `is_machine_tag` 一致；`type:auto_sink_x` 这类前缀撞车仍不会被认成沉淀（此前这个承诺没有测试钉住，12 个定点变异里唯一全绿漏网的那个）
- **打开旧库自动迁移**：新增 `Ledger._migrate_auto_sink_legacy()`，给 1.5.0 期间（11-12～11-14，默认每 6 轮写一条）写出的存量沉淀补 `type:auto_sink` 标记、摘掉 concept 为机器标签的 `memory_link` 边；走提交链（`CommitBuilder.retype_memory` / `unlink_edge`，删除行带完整 from|to|concept 身份，R4 的坑不会踩），回放与自检对得上；先查后写，重复执行不出错
- 实测（评审复现脚本 probe3_ledger.py A 段，临时库）：`auto_sink_ids()` 由 `[]` 变为全部 6 条存量；concept=自动沉淀 的边 15 → 0；`activate("空调")` 由返回沉淀变为只返回真事实；系统提示里不再出现「自动沉淀」

### N7/N8 排除前移到候选池截断之前

- `recall` / `_clue_bridge` / `_fresh_candidates` 都新增 `exclude` 参数，在排序与截断之前过滤；`activate` 把 `hidden = quiet | auto_sink` 一次算好传给每条路线的候选池，`take()` 里那道检查保留作双保险。显式开启沉淀后，10 条同主题沉淀不再把真事实挤出 search 路线（评审复现表第 3 行：真事实掉出前 9 → 稳在 search）
- fallback 的「破例浮现」先剔 hidden 再挑 `min(_surfaced)`：沉淀 `_surfaced` 恒为 0，此前每次都被选中又因 auto_sink 排除拿不到，整轮 `fallback_blocked`；现在冷却期也能浮出真事实

### N9 存储层把索引降回索引（部分）

- `plan_working_set` 跳过 `auto_sink_ids()`：沉淀不抢工作记忆名额，不再把真事实挤到零级、被冲刷标成 delete
- `flush_plan` 跳过沉淀条目，且冲刷量基准取「普通记忆」里最新的 `created_seq`——写一条沉淀不再让所有普通记忆的冲刷量同步涨一格
- 沉淀正文去掉字面前缀「自动沉淀：」：「是什么」由 tags 的机器标记表达，正文重复一次只会让「自动」「沉淀」的词面在检索里命中一切沉淀条目

### N10 建图精确集合判定

- `_auto_link` 的候选邻居改精确判定：子串 LIKE 只缩小扫描范围，命中后按逗号分隔的完整标签集合确认 tag 确实在其中，且目标条目自身不带机器标签；`ORDER BY created_seq DESC LIMIT 32` 保证顺序确定，凑够 8 条即停。「自动」「沉淀」「auto」「sink」这类子串标签不再把普通记忆连到沉淀上，真共享标签的普通记忆之间也拿得到名额

### 修（低成本项）

- **N4 两个崩溃**：`inject_memory` 里 `said = ""` 预先初始化（`get_message_str()` 炸掉时不再 UnboundLocalError，把「落链失败」升级成「唤起失败」）；`_as_int` 多捕 `OverflowError`（`int(float("inf"))` 不再炸出工具调用）
- **R1：/记忆压缩 加确认词**：与 /记忆冲刷、/记忆容量 同一口径，只认独立的「确认」，无确认只提示不执行（评审复测：`/记忆压缩 1` 一直无确认直接执行）
- **R3：显式检索输出标出争议记忆**：`_fmt_hit` 对 `disputed` 条目加「有争议（被否认过，待对证）」标注，/记忆 与 memory_search 工具都生效
- **DREAM / EMOTION 环境开关测试隔离**：两个测试文件在 import ledger 之前摘掉环境开关，宿主进程开着它们跑测试不再红（此前实测 3+1 条失败）；设置 `DREAM_SCHEDULER_ENABLED=true` + `EMOTION_ENABLED=true` 重跑全量实测 459 passed
- **metadata.yaml 改成真实作者与仓库**：`AAA-pig` / `github.com/AAA-pig/MemoryCommit`（原来是占位的 `memory-commit` / `lsby/ai-memory`、`repo: local`）

### 文档

- 路线名统一成代码真实 route 值（search / associate / activate / fallback / fallback_blocked / none）：activate 注释、CHANGELOG、README 三处；recall 的 match 值 direct / clue / graph 是「命中来源」不是路线名，已注明
- README「没命中一字节都不注入」改成与 fallback 说明一致的口径；「沉淀条目不进提示词」补存量迁移说明
- 更正 1.5.1 段两处陈述：路线名（原写 search / associate / graph / fresh / fallback）；「存量边不自动回收」（N6 迁移落地后已回收）
- 1.5.1 段补记补全：34bf72d..954ef20 两波改动的完整清单（见 1.5.1 的「补记」小节）

### 测试

- 459 passed（基线 444：新增 14、改造 3——cmd_compact 两条按确认词新行为改写、_as_int 断言修正）。新增：954ef20 写库→新代码打开的迁移测试（含幂等与注入断言）、auto_sink_ids 双标记口径与前缀撞车、10 条沉淀不挤掉 search、fallback 破例浮现跳过沉淀（另钉 eligible 双保险：候选池过滤失效时仍兕底）、沉淀不抢座/不进冲刷计划/不推冲刷量、正文去前缀、子串标签不连沉淀、/记忆压缩 确认词、_fmt_hit 争议标注、_as_int Overflow、落链失败不升级为唤起失败
- 环境开关反向验证：设 `DREAM_SCHEDULER_ENABLED=true` + `EMOTION_ENABLED=true` 全量重跑，459 passed（修复前 3+1 failed）
- 全部用临时库；评审自带复现脚本（probe3_ledger / probe3_force / probe3_chain）在修复后重跑，A/B/B2/C/F 段全部转为预期行为

### 补钉（2026-11-14 追加）：N11 说明缺口 + R4/R5 两个全绿变异

评审报告（fb9dfa4 三轮锐评）2.2 节变异表三个全绿漏网里，`auto_sink_ids` 精确匹配已随 N6 的口径与撞车测试收掉，本轮再收掉剩下两个：

- **N11：`type:` 保留前缀写进工具说明**。`memory_write` 的描述此前只说「标签相同的历史记忆会自动连边」，没告诉模型 `type:` 前缀被 `is_machine_tag` 整体保留——模型写 `type:preference` 这类标签后会发现两条记忆之间没有边却不知道原因。现在描述明确写清：`type:` 开头的标签是保留前缀，用于内部类型标记，不参与连边、也不会被当成语义概念，标记语义请用普通词；写入返回文本在 tags 带 `type:` 前缀时附带一句提醒（普通标签不多嘴；现有断言只认「已写入」开头，不受影响）。选「写说明 + 返回提醒」而不是「入口剥掉 type: 标签」：剥标签改的是模型输入的语义，代价高且超出本轮范围。补测三条：描述含「保留前缀」说明、返回提醒有/无的对照、两条 `type:preference` 记忆 0 边 + 同样两条普通标签有边的对照组。
- **钉住 R4**：`test_replay_delete_identifies_row_by_row_key` 造两条同 from/to、只有 concept 不同的 `memory_link` 边，删除行按老版 forget 形态只存 strength（before 缺身份字段），row_key 是唯一身份来源；断言重放删的正是 row_key 指定的那一条、另一条还在、health 对账通过。变异「重放删除不解析 row_key（退回 before 拼身份）」当场变红（幽灵残留 + 对账不一致）；变异「按 from/to 粗匹配删除」同样变红（没删的那条被误删）。
- **钉住 R5**：`test_load_v2_eof_only_truncation_rejected` 把 v2 备份的尾部 `__eof__` 标记截掉（数据行全在、行数清单全对得上——行数校验对这种截断无感），断言 load 明确判失败且原因可读，原库一字节不动；对照组完整文件通过。变异「去掉 eof 检查」当场变红。此前的 `test_load_v2_truncated_rejected` 截掉的是最后 3 行，失败被行数校验兜住，eof 检查本身一直没被单独钉住。
- 464 passed（基线 459 + 新增 5，全部临时库）；变更后设 `DREAM_SCHEDULER_ENABLED=true` + `EMOTION_ENABLED=true` + `JJ_MEMORY_AUTO_SINK_EVERY=6` 全量重跑，464 passed

### 未做（写清楚，别当成已实现）

- **沉淀仍在 memory 表里（N9 彻底方案未做）**：评审建议的「移出 memory 表、进 message_digest 旁路表（挂起止 seq 与会话 ID）」本轮不做。原因：动表结构要加 schema 版本迁移、备份清单与回放口径都要跟着改，牵动面远大于本轮「止血 + 存量迁移」的范围；现在的减损是「沉淀不抢座、不计量、不建图、不进提示词」，但它们仍占 memory 行数、仍进线索表（1.5.1 未做项延续）、正文仍会进显式检索结果（设计如此）。旁路表留给后续轮次
- N11：工具说明与返回提醒已补、三条测试已钉（见上方「补钉」小节）；「入口剥掉 type: 标签」的修法仍不做——改模型输入的语义，代价高，说明写清后模型可预期
- R4 幽灵基线（旧代码生成的库 health=False、load 被预演拒绝）没有修复路径；R5 的 v1 老备份宽容校验照旧；R6 的 `run_once("", scope_key="")` 仍产出全局梦；N2 普通标签枢纽（20 条同标签 124 边）未变
- N1 老问题：自动沉淀仍无原文锚、仍混会话、仍是全局 scope——等 message_digest 旁路表一起解

## 1.5.1 — 2026-11-14

自动沉淀降级收尾（N1 + N2）。1.5.0 之后两波改动当时没进本文件、版本号也没动，这次补记（完整清单见下方「补记」小节，2026-11-14 追记补全），并把「类型标记参与建图」这个新洞修掉。

### 补记：1.5.0 发布后、1.5.1 之前的两波改动（34bf72d..954ef20）

**第一波（34bf72d / 468731f / e06a6ee，P0/P1 修复）：**

- **自动沉淀最初引入**：外壳不依赖模型自觉，每 6 轮（`JJ_MEMORY_AUTO_SINK_EVERY`）把原文折成一条 day 记忆——当时默认开启，1.5.1 改默认关
- **写路径失败计数**：`FailureCounter`，失败不再只留一行 warning，/记忆自检 能报「哪条写路径在悄悄失败」
- **WAL 定期截断**：挂维护节流里做 `wal_checkpoint(TRUNCATE)`，阈值 `JJ_MEMORY_WAL_CHECKPOINT_BYTES`、冷却 5 分钟（实测 3.6MB 从不回落）
- **维护任务节流**：`MAINTAIN_EVERY`（R8）：首轮与之后每 N 轮各检查一次，修掉旧写法 N=1 时永不执行的错
- **原文链检索上限**：`SEARCH_CANDIDATE_CAP=200` / `SEARCH_EXPANDED_CAP=2000`，无命中时扩窗重试并标 truncated，不再全表逐行切词
- **遗忘报错说清原因**；命令面与边界分支补测（ledger/main 行覆盖率 100%）

**第二波（954ef20，第二轮评审修复）：**

- **R1 鉴权**：11 个 /记忆* 命令与五个 LLM 工具全部只认 owner（UMO 名单），非 owner 统一拒答；备份只能落专用目录、默认不覆盖、路径成分剥掉；破坏性操作精确确认（「确认」必须是独立词）
- **R5 备份 v2**：文件头带每表行数清单、尾部 `__eof__` 标记、导入严格校验、落库前临时库预演（`DUMP_VERSION 1 → 2`）
- **R4 删除行带完整行身份**：`memory_link` 删除的 row_key 是完整 from|to|concept，重放不出幽灵行；自检投影对账覆盖全部投影表
- **R6 scope 贯穿**：线索桥、图谱一跳、梦产出的每一跳都限制在会话（或全局共享）
- **R3 条件并存语义**：disputed 永久拦（不吃条件豁免）；coexist 给语境且条件匹配才放行，判不出/不匹配保守拦（`condition_allows()`、`quiet_ids(context)`）
- **R2 原文锚绑到具体请求的事件**：不同会话/并发各锚各的，落链失败显式置 0

### 自动沉淀默认关（N1 降级）

- **默认行为变了**：自动沉淀从「每 6 轮写一条」改为默认关（`JJ_MEMORY_AUTO_SINK_EVERY` 默认 0），不打环境变量就一条也不写
- 要开必须显式设 `JJ_MEMORY_AUTO_SINK_EVERY=N`：一问一答算一轮，攒满 N 轮把这几轮原文折成一条 day 记忆
- 开了也只是索引：写出的条目带 `type:auto_sink` 类型标记 + 统一标签「自动沉淀」，唤起（activate）各条路线（search / associate / activate / fallback）一律不收——**开了也不会进系统提示**；显式检索（recall 搜「自动沉淀」或正文词面）照样找得到

### 修

- **机器/管理标签参与建图**。`type:auto_sink` 与「自动沉淀」写进 `memory.tags`，而 tags 即概念、写入即织边：实测 5 条沉淀探针自己连出 20 条 `memory_link`（「自动沉淀」10 条、`type:auto_sink` 10 条），把「聊过什么」的原文索引抬成了图谱上的语义同源——「元数据标签被当成语义连边」（N2）的隐患多出一组。现在唯一建图入口 `_auto_link` 统一过滤机器标签（`type:` 前缀 + 「自动沉淀」统一标签，`ledger.is_machine_tag`）：沉淀条目一条 concept 边也不织，正常记忆的语义标签连边照旧。（1.5.2 更正：存量库里已织出的这类边由打开库时的 `_migrate_auto_sink_legacy` 回收，不是「不自动回收」）

### 测试

- 444 passed（原 443：改造 1 条——注入排除测试补「建图结果没有该 concept」断言；新增 1 条探针测试——5 条沉淀 + 2 条共享语义标签的正常记忆，断言沉淀 0 边 + 语义边照常的对照组）。探针实测同一组数据：建图边数 21 → 1（沉淀相关 20 → 0，仅剩正常记忆的「咖啡」语义边）

### 未做（写清楚，别当成已实现）

- 两个沉淀标签仍会进线索表（`memory_clue`）：本次验收只针对 concept 边；线索桥候选在注入侧同样被 `auto_sink_ids` 拦截，不影响提示词。要彻底清线索表留给后续轮次

## 1.5.0 — 2026-11-12

梦境职守第一次落地，加上「矛盾不是幻觉」这条判据。存储语义一个字没动：`memory` 主表结构保持原样，全部新增都是旁路表。

### 一体三职

记忆、梦境、情感是同一个系统长出的三种动作，不是三个可拆的部件。所以不拆插件、不拆进程、不拆库，但内部职守分清：各有写入口、各有测试文件、各有开关、各有失败降级。对外的咬合点只有两个——`propose_clues` 出候选、`trace_to_source` 接回原话。

### 新增（五张旁路表，`SCHEMA_VERSION` 4 → 5）

- `memory_condition`：条件并存。冷天喝热的、热天喝冷的，两条都对，缺的是「条件」不是「谁错」
- `memory_status`：状态档扩到六档，新增 `coexist`（条件并存）——原三档落不下「明天热的时候还提喝热的」，那是不合时宜，不是记错
- `dream_proposal`：梦只写这里，不直接改记忆。影子阶段连线也只提案，不真写 `memory_link`
- `memory_source_anchor`：增量原文锚，写入时记下落链位置
- `user_denial`：用户否认留档

### 矛盾四分类（只分类，不判决）

时变 / 场景依赖 / 视角差异三类挂 `coexist`——都是真的；只有「同一时间、同一场景下词面互斥」才进幻觉候选，而且还要过原文对账。`condition` 为空的一律挂 `conflicted` 等证据，**永不写 `corrected`**：判不了是正常的。

判据只有一句：**原文里根本没这回事**。不是「两条记忆打架」——打架大多是真的。

### 梦境

- 允许从已有记忆的组合里产出新的、模糊的记忆（`provenance='dream'`、置信上限 0.4 硬编码不可调、单次最多 2 条、`level` 恒 `day`、`grade` 从 0 起）
- 不得重组旧记忆：故意不提供任何更新 `summary` 的 API，梦只能碰 `dream_proposal` 与 `memory_link`
- 三道锁：梦条目永不作证据；被再次梦见不加权；不能给节点提名

### 修（复核时实测出来的两个洞）

- **用户否认会被自动分类冲掉**。`deny_memory` 之后跑一次 `classify_pair`，`disputed` 就变成 `conflicted`，降权与停止主动提起只维持到下一次分类为止。现在 `disputed` 是粘性状态，自动写入不得覆盖，只有 `deny_revoke` 能解开
- **梦能给自己发免死金牌**。保护标签只剔了 `manual/protected/keep/no_decay`，而鲜活度豁免还含 `pinned/never_forget/manual_keep`——梦产出带一个 `pinned` 就免疫老化。现在取两集合并集，宁可多剔

### 测试

- 235 passed + 0 skipped（1.4.2 基线 162 → 1.5.0 首发 212 passed + 1 skipped；解禁 `test_owners` 6 条、新增 `test_tool_args` 13 条到 231，本次再加空检索词 4 条到 235）。新增四个职守测试文件：`test_memory_duty` / `test_dream_duty` / `test_dream_guard` / `test_emotion_reserved`
- 主库 v4 副本上演练过迁移：539 条一条不少，`message` 链 371 条校验全绿

### 修（推上去之后的补充）

- **owners 名单改了不生效**。`_load_owners` 的缓存签名原来是 `(mtime_ns, size)`：内容等长、mtime 又没跳出同一个时间刻度（NTFS 是 100ns），两次签名一模一样，直接命中旧缓存。`["...:2"]` 改成 `["...:3"]` 长度不变 → 读回来还是旧名单。现在签名存文件全文，坏 JSON / 顶层类型错仍退回环境变量，文件删掉顺手把缓存倒掉
- **工具参数能把整条回复炸掉**。五个 LLM 工具的参数没做防御：模型传 `None`，`.strip()` 直接 `AttributeError`；传「很高」，`float()` 抛；传「很多」，`int()` 也抛。一条参数脏了，整条回复都没了。现在 `remember(None)` 回「记忆内容不能为空」，`importance` 的 nan 不再被 `min(1.0, nan)` 抬成满分钉死，其余脏值按默认值回落
- **空检索词会吐一批无关记忆**。`memory_search` 传空串 / 纯空白 / `None` 时原来照样落进 `recall`，按新鲜度排出一批无关条目，看着像找到了，其实什么都没找。现在入口直接拦下，并说明怎么给词
- 新增 `conftest.py`（最小 astrbot 外壳）：`test_owners.py` 此前因缺 astrbot 被整模块跳过，等于 11-11 那次断链的防线在本地从没真跑过。解禁后 6 条用例当场抓出上面第一个洞，不是补完测试就万事大吉

### 未做（写清楚，别当成已实现）

- 存量 526 条中转站导入记忆没有原文可对账，**只能标疑，不能修**。标疑尚未实现
- `condition` 字段允许为空，等能记外部条件（温度/天气/地点）的工具；工具来了也不回溯补历史
- 情感模块只有开关位，没有实现

## 1.4.2 — 2026-11-11

修的是「账本在，但它不认人」：整天空转，一条都没写进去。

### 故障

- `OWNER_UMOS` 在 import 时一次性快照。2026-11-11 那次重启，`JJ_MEMORY_OWNERS`
  是在进程起来之后才写进注册表的，快照拿到的就是空集 → 两个钩子（落链接、注入）
  和四个工具全部在第一行 `return`，`ledger.db` 从 00:02 起没有任何写入，`-wal` 是 0 字节。
  日志里一行错都没有，因为这不是异常，是「按配置没被选中」。

### 修法

- 新增 `DATA_DIR/owners.json`，每次 `_is_owner()` 按 `(mtime_ns, size)` 复核，
  改完下一轮生效，不用重启；与环境变量取**并集**，不是替代
- 文件坏了（JSON 语法错、顶层不是列表）只打 warning 并退回环境变量，
  不会把访问权清空——宁可维持原状，也不要因为一个手滑变成全拒绝
- `owners.json` 缺失时行为与旧版完全一致

### 测试

- 168 passed（新增 6 条：只有环境变量、文件并集、改写即时生效、坏 JSON 回退、类型错回退、按 UMO 判定）

## 1.4.1 — 2026-11-10

只还读路径的工程债，不动存储语义、不动检索打分。

### 并发与正确性

- `_query_one()` / `_query_all()` 改为与写事务共用同一把 `RLock`。连接是 `check_same_thread=False`
  共享的，写那边有 `_tx()` 保护，读这边一直裸奔：另一个线程正处在事务中间时，读会直接撞进去。
  `RLock` 可重入，事务内部继续调查询不会死锁

### 性能（2000 条记忆实测 activate 158ms → 91ms）

- `_parse_time()` 加 `lru_cache`：`created_at` 每行每轮都要 `strptime` 一次，两千条记忆一轮解析
  四千多次，光解析就吃掉近一半时间。这是本次最大的一笔
- 记忆切词结果按「memory_id + 原文」缓存：原文变了自然不命中，不需要谁记得去失效；
  缓存跟着当前记忆收缩，删掉的条目不会越攒越多
- 线索切词同样缓存（线索变动远少于记忆）
- `recall()` 里同一行的 `time_decay()` 不再算两遍

### 维护任务不再每轮都跑

- 每日导出和工作记忆换班改成一周期一次（`JJ_MEMORY_MAINTAIN_EVERY`，默认 20 轮），首轮仍然必查。
  导出本身跨天才真写文件，换班也有冷却，节流省的是每轮那两次空查询

### 测试

- 162 passed（新增 5 条：切词缓存复用、内容变更后失效、缓存收缩、读写并发、维护任务节流）

## 1.4.0 — 2026-11-09

这一版治的是「只写不读」的另一半：之前账本只有记忆条目，没有原文，检索落空时也不说话。补上原文链、注入观测和零命中兜底。

### 原文链（schema 3）

- 新增 `message` 表，`append_message()` 按序号把原话一条条接上去，append-only，不覆盖不改写
- `search_messages()`：对原文走 BM25，带单字兜底；候选只取最近 `SEARCH_CANDIDATE_CAP`（200）条
  - 原文链只涨不缩，每查询全表逐行 tokenize 会随条数线性变慢，老消息正常检索几乎用不上
- `verify_message_chain()`：校验序号连续、链尾一致、每条 prev 对得上，坏了返回断点位置和原因
- `message_stats()`：条数、起止时间、链尾
- 迁移 `2 → 3` 纯加法：只补 `message_no` / `message_head` 两个指针，老数据一条不动
- 外壳：收到消息先落原文链（不管后面检索成不成立），回完消息再接链尾；失败只记警告，不影响聊天

### 注入不再整条放弃

- 长 prompt（>300 字，通常是拼好的整段上下文）不再 `return` 掉整个流程——那样会把每日导出、工作记忆换班一起跳过
- 检索词改用用户原话 `said[:300]`，只把 query 置空才是真的没得用
- 新增注入观测账 `inject-log.jsonl`：每轮记 `outcome`（injected / no_hit / long_prompt / empty_prompt）、`query_len`、`route`、`picked`、预算
  - 只记不改行为，写日志失败也不影响聊天

### 零命中兜底（P0）

- `activate()` 原来只在 query 为空时才走 fresh 激活；有检索词但一条没命中时，search、线索桥、图扩展全空，整轮什么也不注入
- 现在零命中降级到 fresh 兜底，最多 `limit // 2` 条，`route` 单独标 `fallback`，不伪装成 search / activate，冷却照常生效
- 兜底走的是同一份 420 字预算，不额外加字

### 验证

- 测试 113 → 120 条，全通过
- 真库副本上做过破坏实验：删尾部、删中间、改一个字，三种都能被 `verify_message_chain` 抓出来；删尾部同时改掉 meta 抓不出（已知边界）

## 1.3.0 — 2026-11-08

`grade` 字段从建库那天起就在，但没有任何机制去动它，所以「分级」是装饰——真库里 5 条记忆全是一级、全是 `topic`、重要性全在 0.85 以上，被钉住规则冻得死死的。这次给它一个容量，让分层开始转。

### 工作记忆容量

- 新增 `working_activation()`：一条记忆「此刻有多清楚」，`(0.4 + 重要性) × 时间衰减 × 最近碰没碰过 × 最近被想起几次`
- 第三项把冲刷的「多久没被想起」换成了「上一次被想起距今隔了几件事」（`WORKING_ACCESS_HORIZON`，按创建序号算，指数衰减）：隔十来件事还没人提它，就该让位
- 第四项取 `access_log` 里最近 3 天的次数，累计访问不再单独占一项——想起来十二次的东西不该因为重要性差 0.05 就出局
- 新增 `plan_working_set()` / `working_set()` / `rebalance_working_set()` / `maybe_rebalance_working_set()`
- 语义定死：**二级 = 巩固层**（不占名额、不参与挤出）、**一级 = 工作记忆**（名额有限，默认 3）、**零级 = 沉降层**（不主动冒头，检索还找得到）
- 复出条件：零级记忆在 `REVIVE_WINDOW_DAYS`（3 天）内被想起 `REVIVE_HITS`（2）次，就回到一级
- 换班一次算清：现有的一级和该复出的零级放同一张榜上抢名额，不存在先降后升、下轮又降的来回打
- 每轮最多挤 `WORKING_DEMOTE_LIMIT`（20）条，分期收敛，免得一条提交里塞两百行快照
- `WORKING_CAPACITY=0` 关掉整套机制（`grade` 就再也不自己变），实例属性可覆盖，测试和外壳都能直接改

### 行为变化

- `_fresh_candidates()` 里零级权重再乘 `ZERO_GRADE_WEIGHT`（0.45），`how` 标「沉下去了」：被挤出工作记忆的不抢话，但不拉黑
- `stats()` 带出 `working_capacity`，`/记忆` 显示「工作记忆 N/M 个名额」
- 新指令 `/记忆容量`：看谁在座、谁超员、谁该回来；`/记忆容量 确认` 执行换班；`/记忆容量 5` 改名额

### 外壳

- `inject_memory` 每天第一次请求时顺手跑一次换班（6 小时冷却，没超员什么都不做，失败只记警告）
- 版本号 1.2.0 → 1.3.0

### 验证

- 新增 `WorkingMemoryTest` 11 条：算名额不写库、按清楚度留人、超员降级且能回放、名额合适不重复动、单轮降级有上限、反复想起才复出、想起一次不够、二级不占名额、容量 0 关掉、冷却生效、零级不抢话
- 91 passed（原 80 + 新 11）
- 真库副本演练：5 条一级 / 容量 3 → 留住 m5 m2 m4，降 m3 m1 到零级，HEAD C20 → C21，自检通过，再跑一次不动

## 1.2.0 — 2026-11-08

冲真库的一笔烂账来的：19 条提交里 14 条只是「浮现：某某」，账本被想起的次数撑胖，真正的记忆改动反而被埋了。这次把「被想起」从账本内容里摘出去。

### 结构版本 v2

- 新增 `access_log` 表（`memory_id / at / seq / query / how`），记「谁在什么时候因为什么被想起来」
- `access_log` 不进 `PROJECTIONS`、不进提交链、不参与回放和压缩：它是体温计，不是账本
- `memory.access_count` / `last_access_seq` 降格为缓存列，继续供衰减、保留指数、冲刷计算用，但不再进提交快照
- `SCHEMA_VERSION` 1 → 2，`_migrate_1_to_2()` 建表并按已有 `access_count` 折算成一条 `how='seed:N'` 的种子记录——不编造「哪天、因为什么」，只写清这是迁移前累计

### 行为变化

- `Ledger.touch()` / `touch_many()`：写访问日志 + 更新缓存计数，**不开提交**
- `recall(touch=True)` 与 `activate()` 改走 `touch_many()`，浮现多少次账本厚度都不变
- `CommitBuilder.touch()` 移除，且故意不留兼容壳：留着总有人顺着它又写回提交链
- 新增 `access_history()` / `prune_access_log()`：访问日志默认留最近 180 天、每条记忆至少留最近 200 次，挂在每日导出之后顺手收一次

### 修掉的问题

- 插入快照不含状态列后，`last_access_seq` 落到 0，刚写完的记忆一出生就显得「很久没被翻过」，下一步直接进冲刷队列——回放插入时改成默认取 `created_seq`
- 老库 `last_access_seq` 是 `NOT NULL` 且无默认值，回放插入会炸；新库建表补 `DEFAULT 0`，回放侧再兜一层

### 外壳

- 新指令 `/记忆访问 [id]`：看一条记忆被想起过几次、每次因为什么；迁移前的种子记录会标出「迁移前累计 N 次」
- `/记忆自检` 多一行访问痕迹条数，`stats()` 带出 `accesses`
- 版本号 1.1.1 → 1.2.0

### 验证

- 新增 `AccessLogTest` 10 条：v2 版本号、浮现不涨提交数、访问日志记得住原因、计数仍喂衰减、未知 id 无副作用、新记忆不早衰、修剪不动热度、回放容忍缺列、压缩不动记忆内容、迁移折算成种子
- 80 passed（原 70 + 新 10）
- 真库副本演练：20 提交 / 5 记忆 / 13 线索 / 9 连边 → 平滑升到 v2，36 条历史访问 update 行原样保留不回写，唤起后提交数仍是 20

## 1.1.1 — 2026-11-08

冲「以后要脱离 AstrBot」这一条来的：库要能被别的宿主认出来、认回来、搬得走。

### 结构版本

- 新增 `SCHEMA_VERSION`（表结构）和 `CORE_API_VERSION`（公开接口），两个号分开管：加列只动前者，改签名只动后者
- `meta.schema_version` 存在库里而不是写死在代码里，跟着库走
- `_migrate()` + `_MIGRATIONS`：老库没有版本键会被认成 0 号，自动升上来；库比代码新直接抛 `LedgerError`，不给「勉强打开」这个选项
- 新增 `Ledger.schema_version()`

### 可往返导出

- `dump_jsonl()`：整本账（meta + 提交链 + 提交行 + 六张投影表）导成 JSONL，头一行是文件头（格式名 / 格式版本 / 结构版本 / 接口版本 / 导出时间）
- `load_jsonl()`：灌回来。默认只肯往空库灌，覆盖要显式 `replace=True`；导入时丢掉备份里多出来的列并计数，导完补跑迁移，最后校验 HEAD
- `last_export_date` 这类机器本地的 meta 不跟着搬
- 和 `export_markdown()` 分工：那个给人翻，这个给机器搬家
- 新指令 `/记忆备份 [路径]`，默认写到 `backup/ledger-时间戳.jsonl`
- `stats()` 带出 `schema_version`，`/记忆自检` 显示结构版本

### 外壳

- `OWNER_UMO` 硬编码换成 `JJ_MEMORY_OWNERS`（逗号分隔），接第二个平台不用改代码
- 版本号 1.1.0 → 1.1.1

### 验证

- 新增 `MigrationTest` 10 条：新库写版本号、老库补版本且不动数据、新库拒绝降级打开、整本往返（HEAD / 提交数 / 记忆 / 线索 / 连边 / 画像全对得上，搬完还能检索和继续写）、压缩后往返仍能回放、机器本地 meta 不搬、非空库默认拒绝覆盖、外来文件拒绝、格式版本超前拒绝、未知列丢弃并计数
- 70 passed（原 60 + 新 10）
- 真库副本演练：15 提交 / 5 记忆 / 13 线索 / 9 连边，搬到新库一条不差，检索、回放、自检、续写全部通过

## 1.1.0 — 2026-11-08

两边合流：早期版本停在对 `Ledger.py` 的 1.0.3，另一条线在 `ledger.py` 上已经长了压缩、自检、唤起。这次把前者的检索修复挑回来，统一到小写文件名，并入主线。

### 从桌面那份并回来（1.0.2 / 1.0.3 的全部内容）

- 检索侧衰减改成「越常用、越慢忘」：`effective_half_life()` 按访问次数 log2 拉长半衰期，封顶 5 倍（`MAX_HALF_LIFE_FACTOR`）
- `time_decay()` 收 `access_count`；`recall()` 打分、新鲜度加成、图谱邻居分、线索桥、`activate` 全部走有效半衰期
- `limit` 夹紧：新增 `normalize_limit()`，0 / 负数 / 字符串 / 超大值都收进 `[1, MAX_RECALL_LIMIT]`
- 单字查询兜底：写入端只切双字，搜「辣」这类单字以前必然空手，现在回退成子串命中
- `materialize()` 传未知提交号改成抛 `LedgerError`，以前静默返回空表/全量状态，跟「那一刻真的没记忆」分不清
- 测试从 51 补到 60（有效半衰期单调与封顶、冷热衰减差、limit 越界、单字兜底、未知提交、时钟回拨、重复标签不重复连边）

### 这份里原来就有、CHANGELOG 没记的

- `compact()` / `/记忆压缩`：久远提交并成一条基线，HEAD 与当前状态不变
- 自动压缩：`JJ_MEMORY_AUTO_COMPACT`，写事务外顺带压一次，失败只 warning 不连累这次写入
- 每日导出：跨天第一次请求前刷一遍 `记忆账本.md`
- `/记忆自检`：查提交链、投影、写权限、导出
- `/记忆唤起` + `activate()`：检索 → 联想 → 激活，带冷却，`render_brief()` 控预算
- 线索权重（归一化 IDF）与线索桥：稀有线索抬分，烂大街的线索几乎不动

## 1.0.3 — 2026-11-08

- 检索侧衰减改成「越常用、越慢忘」：新增 `effective_half_life()`，半衰期随访问次数按 log2 拉长，封顶 4 倍（`MAX_HALF_LIFE_FACTOR`）
- `time_decay()` 多收一个 `access_count`，`recall()` 里的打分和新鲜度加成、以及图谱邻居的候选分都改成用有效半衰期
- 和 flush 侧的保留指数分开算：那边管「会不会被降级」，这边只管「排多靠前」，两套逻辑不互相污染
- 加 3 个测试（有效半衰期单调、封顶、冷热记忆衰减差），共 29 个，全绿

## 1.0.2 — 2026-11-08

- 修 `materialize()`：传未知 commit_id 以前会静默返回「当前全部状态」，现在抛 `LedgerError`
- 修 `recall()` 的 `limit`：0 / 负数 / 字符串现在都能吃，上限夹到 `MAX_RECALL_LIMIT = 200`
- 加单字查询兜底：写入只切双字，搜「辣」这类单字以前必然空手，现在走子串命中
- 测试从 20 个补到 26 个，覆盖空库、单字、limit 越界、未知提交、时钟回拨、重复标签

## 1.0.1 — 2026-11-08

- 命名统一成大驼峰：`ledger.py` → `Ledger.py`，`tests/test_ledger.py` → `tests/TestLedger.py`
- 新增 `pytest.ini`，放宽 `python_files`，大驼峰测试文件也能被 pytest 收到
- `main.py` 保留原名（AstrBot 入口约定，不能改）
- 类名、函数名不变，避免调用侧改两套

## 1.0.0 — 2026-11-08

第一版。核心是把两个仓库里能用的部分拼到一起，做成一个能单独跑的账本。

### 来自 lsby/ai-memory（架构 v15）

- 写入只能发生在提交里；提交的父节点必须正好是 HEAD，否则抛 `StaleHeadError`
- 每个提交挂行级快照（`commit_row`），按提交内序号严格排序，记 before / after
- HEAD 展开成当前状态；`materialize(commit_id)` 可回放到任意历史提交
- 信息冲刷量 = 当前最大创建序号 − 该记忆的最后访问序号
- 保留指数 = 重要性 × (1 + log2(1 + 线索数)) × (1 + log2(1 + 访问次数))，对数级巩固
- 冲刷压力 = 冲刷量 / (保留指数 × 6)，≥ 1 才降一级，一次只降一级

### 来自 biyuehu/animaone

- 时间粒度分层：topic / day / week / month / quarter / year
- 标签即概念，写入时和历史节点自动织网连边
- 检索：中文双字滑窗切词 + BM25（k1=1.5, b=0.75）+ 30 天指数半衰期
- 命中节点沿图谱边带出一跳邻居
- 画像层单独存放（preference / identity / event / relation / dislike），重复提到累加权重

### 自己加的

- `topic` 层与重要性 ≥ 0.85 的记忆钉住，不参与自动冲刷
- 记忆落在插件外部目录（默认 `~/.local/share/jingjing-memory/`），可导出成 Markdown 直接翻
- 访问记忆也算一次提交，访问计数同样可回溯

### 已知问题

- 检索是词面匹配，无语义向量，同义不同词会漏
- 自动连边只按标签字面相同
- 提交链没有 GC，量大后需要「合并历史提交」
- `commit` 是 SQLite 关键字，提交表实际叫 `mcommit`

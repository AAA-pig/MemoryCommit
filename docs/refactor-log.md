# 重构记录

原始行号以 `c54d305` 的 `docs/deps.md` 为准；物理行数含空行。每刀均在副本核对包相对和平铺导入、全量回归和独立临时库冒烟。

## 第 1 刀：`ledger_math.py`

- 迁出：`ledger.py` 原 190–191、195、206–207、804–812、823–955、966–996、1487–1499 行。时间格式/解析缓存、`human_ago`、分词与词表、BM25、limit、半衰期、线索权重、保留/冲刷、`_clamp_unit`。
- 文件：`ledger.py`、`ledger_math.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 26.84s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：5142 → **4978**。

## 第 2 刀：`ledger_constants.py`

- 迁出：`ledger.py` 原 69–188、193–194、196–198、216–220、226、444–462、511–512、523–559、570–572、785–802、815–820 行。API/备份协议值、通用域常量、机器标记判定、投影键、异常类。
- 文件：`ledger.py`、`ledger_constants.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 26.00s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：4978 → **4790**。

## 第 3 刀：`ledger_persona_format.py`

- 迁出：`ledger.py` 原 465–509 行。`_compute_persona_core_hash`、`render_persona_rows`。
- 文件：`ledger.py`、`ledger_persona_format.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 26.07s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：4790 → **4761**。

## 第 4 刀：`ledger_affect.py`

- 迁出：`ledger.py` 原 1186–1193、1198–1207、1221–1286、1323–1480 行。记忆年龄、元数据与标签、鲜活度豁免、情感信号与 `classify_tier`。
- 文件：`ledger.py`、`ledger_affect.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 26.57s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：4761 → **4537**。

## 第 5 刀：`ledger_conflict.py`

- 迁出：`ledger.py` 原 1026–1052、1064–1183、1209–1217 行。条件解析/匹配、冲突分类、否认关键词、梦的禁用标签集合。
- 文件：`ledger.py`、`ledger_conflict.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.08s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：4537 → **4419**。

## 第 6 刀：`ledger_schema.py`

- 迁出：`ledger.py` 原 42–67、233–442、575–777 行。`SCHEMA_VERSION`、建表 statements、`DEFAULT_META`、从 0→1 到 7→8 的八个步骤。
- 文件：`ledger.py`、`ledger_schema.py`、`tests/test_persona_write_surface.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.77s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：4419 → **3990**。

## 第 7 刀：`ledger_commit.py`

- 迁出：`ledger.py` 原 1505–1524、1550–2014 行。`CommitBuilder` 的行缓冲、各写方法、上下文收尾与 seal 调用；类的公开壳保留在 `ledger`。
- 文件：`ledger.py`、`ledger_commit.py`、`tests/test_persona_write_surface.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.64s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：3990 → **3531**。

## 第 8 刀：`ledger_dream_worker.py`

- 迁出：`ledger.py` 原 5077–5142 行。`DreamWorker.run_once` 的影子执行和部分成功统计。
- 文件：`ledger.py`、`ledger_dream_worker.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.51s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：3531 → **3468**。

## 第 9a 刀：`ledger_history.py`

- 迁出：`ledger.py` 原 2021–2035、2702–2784、3893–4026 行。行键解码、封链/行应用、自动压缩、日志历史与投影回放。
- 文件：`ledger.py`、`ledger_history.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.72s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：3468 → **3250**。

## 第 9b 刀：`ledger_maintenance.py`

- 迁出：`ledger.py` 原 2211–2243、2265–2304、2410–2501、3723–3891、4028–4052；门面保留接口、同时迁出方法体：958–963、999–1023 行。旧沉淀修补、WAL、访问痕迹、工作集、冲刷、自检统计输入。
- 文件：`ledger.py`、`ledger_maintenance.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.91s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：3250 → **2909**。

## 第 9c 刀：`ledger_persona_ops.py`

- 迁出：`ledger.py` 原 2504–2654、2665–2697；门面保留接口、同时迁出方法体：2656–2663 行。人格哨兵、读写、批写、播种与重封。
- 文件：`ledger.py`、`ledger_persona_ops.py`、`tests/test_persona_write_surface.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.60s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：2909 → **2732**。

## 第 9d 刀：`ledger_profile_ops.py`

- 迁出：`ledger.py` 原 3407–3502 行。画像更新、可见性、坏感度门面。
- 文件：`ledger.py`、`ledger_profile_ops.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.59s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：2732 → **2645**。

## 第 9e 刀：`ledger_recall.py`

- 迁出：`ledger.py` 原 2788–2841、2843–3394、3396–3405；门面保留接口、同时迁出方法体：2349–2386 行。记忆增删、说话人反查、召回、线索扩展、唤起、brief。
- 文件：`ledger.py`、`ledger_recall.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 27.55s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：2645 → **2048**。

## 第 9f 刀：`ledger_messages.py`

- 迁出：`ledger.py` 原 3506–3596、3658–3721；门面保留接口、同时迁出方法体：3598–3656 行。原文链、会话查询、计数与链校验。
- 文件：`ledger.py`、`ledger_messages.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.31s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：2048 → **1849**。

## 第 9g 刀：`ledger_status.py`

- 迁出：`ledger.py` 原 4054–4138、4156–4284、4388–4457 行。状态、自动沉淀识别、否认、增量原文锚、分类和条件写入。
- 文件：`ledger.py`、`ledger_status.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.23s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：1849 → **1592**。

## 第 9h 刀：`ledger_dream.py`

- 迁出：`ledger.py` 原 4140–4154、4374–4386、4459–4568；门面保留接口、同时迁出方法体：5046–5062 行。梦产出排除、候选接口、梦提案/原子写入/提名。
- 文件：`ledger.py`、`ledger_dream.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.48s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：1592 → **1462**。

## 第 9i 刀：`ledger_creative.py`

- 迁出：`ledger.py` 原 4286–4372 行。创作旁路读写。
- 文件：`ledger.py`、`ledger_creative.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 28.79s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：1462 → **1379**。

## 第 9j 刀：`ledger_archive.py`

- 迁出：`ledger.py` 原 4570–4681、4713–5043；门面保留接口、同时迁出方法体：4683–4711 行。人读导出、JSONL 备份和恢复、每日导出、自检。
- 文件：`ledger.py`、`ledger_archive.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 40.32s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：1379 → **925**。

## 第 9k 刀：`ledger_storage.py`

- 迁出：`ledger.py` 原 2259–2263、2306–2347、2388–2408；门面保留接口、同时迁出方法体：2071–2132、2246–2257 行。关库、元信息、查询和计数访问；仅在继续缩薄门面时按这一独立块迁移。
- 文件：`ledger.py`、`ledger_storage.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 41.49s；`SMOKE_OK`；包相对与平铺导入及关键属性替换探针通过。
- `ledger.py` 物理行：925 → **799**。

## C1：`memory_commands.py`

- 迁出：`main.py` 原 `cmd_flush` 452–476、`cmd_working` 484–514、`cmd_compact` 522–551、`cmd_backup` 573–600、`cmd_health` 608–627、`cmd_attitude` 944–968，公开入口留在门面。
- 文件：`main.py`、`memory_commands.py`、`docs/refactor-log.md`、`docs/deps.md`
- 验证：736 passed, 1 skipped in 42.33s；`SMOKE_OK`；包相对/平铺导入、handler 签名/类型/文档及 owner 门抽查通过。
- `main.py` 物理行：1197 → **1058**。

## C2：`memory_hooks.py`

- 迁出：`main.py` 原 `_log_injection` 387–420、`inject_memory` 763–906、`record_reply` 917–932，公开入口留在门面。
- 文件：`main.py`、`memory_hooks.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 40.23s；`SMOKE_OK`；包相对/平铺导入、handler 签名/类型/文档及 owner 门抽查通过。
- `main.py` 物理行：1058 → **874**。

## C3：`sink_runtime.py`

- 迁出：`main.py` 原 `_sink_watermark` 316–330、`_write_sink_marks` 338–345、`_sink_gap_line` 360–375、`cmd_sink_backfill` 645–717，公开入口留在门面。
- 文件：`main.py`、`sink_runtime.py`、`docs/refactor-log.md`
- 验证：736 passed, 1 skipped in 41.74s；`SMOKE_OK`；包相对/平铺导入、handler 签名/类型/文档及 owner 门抽查通过。
- `main.py` 物理行：874 → **772**。

## 最终验收

- 物理行（含空行）：`ledger.py` **5,142 → 799**；`main.py` **1,197 → 772**。所有生产模块均低于 800 行。
- 静态导入图：30 个生产模块、66 条直接本地边，无环。双导入均通过；没有新增反向门面导入。
- 搬移体逐一比对 AST；250 处匹配 SQL 关键字的字符串字面量多重集不变，两个独立临时库的 `sqlite_schema` 完全一致。结构/备份/核心 API 版本仍为 8/6/4。
- 仅同步 9 个人格白名单位置；扫描器、断言、条数未改。共享 `ContextVar`、迁移字典和异常身份探针通过。
- 验证局限：包相对和平铺路径使用本地 AstrBot 替身；冒烟走独立临时库，不宣称线上注册已验证。

### 功能对照表

| 功能 | 原位置（物理行） | 现位置 | 是否等价 | 验证方式 |
|---|---|---|---|---|
| 时间/分词/评分 | `c54d305:ledger.py` 190–191、195、206–207、804–812、823–955、966–996、1487–1499 | [`ledger_math.py`](../ledger_math.py)（212 行） | 是（验收范围内） | 时间补丁探针、BM25 与鲜活度回归 |
| 协议常量及异常 | `c54d305:ledger.py` 69–188、193–194、196–198、216–220、226、444–462、511–512、523–559、570–572、785–802、815–820 | [`ledger_constants.py`](../ledger_constants.py)（239 行） | 是（验收范围内） | 版本常量和异常身份探针 |
| 人格哈希及渲染 | `c54d305:ledger.py` 465–509 | [`ledger_persona_format.py`](../ledger_persona_format.py)（56 行） | 是（验收范围内） | 渲染补丁探针、人格封印与预算回归 |
| 保护标签及情感信号 | `c54d305:ledger.py` 1186–1193、1198–1207、1221–1286、1323–1480 | [`ledger_affect.py`](../ledger_affect.py)（255 行） | 是（验收范围内） | 标签、鲜活度与情感分层回归 |
| 冲突和条件 | `c54d305:ledger.py` 1026–1052、1064–1183、1209–1217 | [`ledger_conflict.py`](../ledger_conflict.py)（179 行） | 是（验收范围内） | 条件矩阵、时间窗与梦标签回归 |
| 结构与八步迁移 | `c54d305:ledger.py` 42–67、233–442、575–777 | [`ledger_schema.py`](../ledger_schema.py)（448 行） | 是（验收范围内） | 结构快照、迁移字典同一性、旧库迁移回归 |
| 提交构造 | `c54d305:ledger.py` 1505–1524、1550–2014 | [`ledger_commit.py`](../ledger_commit.py)（507 行） | 是（验收范围内） | 提交链、原子性与底闸令牌同一性 |
| 梦调度执行 | `c54d305:ledger.py` 5077–5142 | [`ledger_dream_worker.py`](../ledger_dream_worker.py)（77 行） | 是（验收范围内） | 关闭、影子运行、上限与部分失败回归 |
| 封链、历史和回放 | `c54d305:ledger.py` 2021–2035、2702–2784、3893–4026 | [`ledger_history.py`](../ledger_history.py)（249 行） | 是（验收范围内） | HEAD 检查、事务、压缩和投影回归 |
| 维护、访问和工作集 | `c54d305:ledger.py` 2211–2243、2265–2304、2410–2501、3723–3891、4028–4052、958–963、999–1023 | [`ledger_maintenance.py`](../ledger_maintenance.py)（429 行） | 是（验收范围内） | WAL、冷却、工作集与时间绑定回归 |
| 人格读写和哨兵 | `c54d305:ledger.py` 2504–2654、2665–2697、2656–2663 | [`ledger_persona_ops.py`](../ledger_persona_ops.py)（208 行） | 是（验收范围内） | 授权双闸、渲染补丁和静态白名单 |
| 画像和坏感度 | `c54d305:ledger.py` 3407–3502 | [`ledger_profile_ops.py`](../ledger_profile_ops.py)（106 行） | 是（验收范围内） | owner、等级与群聊可见性回归 |
| 记忆召回及唤起 | `c54d305:ledger.py` 2788–2841、2843–3394、3396–3405、2349–2386 | [`ledger_recall.py`](../ledger_recall.py)（678 行） | 是（验收范围内） | 召回、作用域、预算和分词/时间绑定 |
| 原文链和搜索 | `c54d305:ledger.py` 3506–3596、3658–3721、3598–3656 | [`ledger_messages.py`](../ledger_messages.py)（230 行） | 是（验收范围内） | 落链、会话、链校验及候选扩窗回归 |
| 状态、否认及原文锚 | `c54d305:ledger.py` 4054–4138、4156–4284、4388–4457 | [`ledger_status.py`](../ledger_status.py)（309 行） | 是（验收范围内） | 否认粘性、撤销、并存及锚绑定回归 |
| 梦产出、提案及防自证 | `c54d305:ledger.py` 4140–4154、4374–4386、4459–4568、5046–5062 | [`ledger_dream.py`](../ledger_dream.py)（178 行） | 是（验收范围内） | 证据/提名三锁和梦事务回归 |
| 创作旁路 | `c54d305:ledger.py` 4286–4372 | [`ledger_creative.py`](../ledger_creative.py)（100 行） | 是（验收范围内） | 不进账本链和召回的隔离回归 |
| 导出、JSONL 与自检 | `c54d305:ledger.py` 4570–4681、4713–5043、4683–4711 | [`ledger_archive.py`](../ledger_archive.py)（501 行） | 是（验收范围内） | v1–v6 往返、预演回滚和输出回归 |
| 存储、元信息及事务 | `c54d305:ledger.py` 2259–2263、2306–2347、2388–2408、2071–2132、2246–2257 | [`ledger_storage.py`](../ledger_storage.py)（164 行） | 是（验收范围内） | 连接/计数与临时库快照和冒烟 |
| 管理命令 | `c54d305:main.py` 452–476、484–514、522–551、573–600、608–627、944–968 | [`memory_commands.py`](../memory_commands.py)（175 行） | 是（验收范围内） | owner 闸、确认词、备份限制、输出和异步生成器探针 |
| LLM 钩子及观测日志 | `c54d305:main.py` 387–420、763–906、917–932 | [`memory_hooks.py`](../memory_hooks.py)（206 行） | 是（验收范围内） | 请求/回复、锚隔离、人格围栏和日志轮转回归 |
| 沉淀水位和手动补沉 | `c54d305:main.py` 316–330、338–345、360–375、645–717 | [`sink_runtime.py`](../sink_runtime.py)（124 行） | 是（验收范围内） | 缺口只读、按会话从旧到新补沉和水位推进回归 |
| 宿主注册与所有 LLM 工具定义 | `c54d305:main.py` 的装饰器与 handler | [`main.py`](../main.py) 的原类和定义 | 是（源码及替身） | 装饰器、归属、签名、文档和异步类型逐一比对；未接真实宿主 |

### 已复现的基线问题（只记录，未修）

- 两条记忆的 `window.from/to` 都是相同的非法字符串时，`_parse_time` 用当前时间兜底；当四次调用返回递增时间，时间窗被误判为互不重叠，`classify_conflict` 返回 `temporal`。用 `time.time` 的 `[100, 101, 102, 103]` 序列在 `c54d305` 和重构结果分别复现；有效且重叠的时间窗作为对照返回 `undetermined`。
- 另有未动的元数据偏差：[`uv.lock`](../uv.lock#L7) 项目版本 1.5.0，和 [`pyproject.toml`](../pyproject.toml#L3) 的 1.6.0 不一致；未将此差异算作已复现的运行时 bug。

### 最终输出（C3，最终源码）

```text
736 passed, 1 skipped in 41.74s
[1] init      db=C:\Users\20708\Desktop\_sandbox_clean\_smoke_tmp\smoke.db schema_version=8 tables=20
              tables=access_log,clue,commit_row,core_rule,creative,dream_proposal,mcommit,memory,memory_clue,memory_condition,memory_link,memory_source_anchor,memory_speaker,memory_status,message,meta,profile,profile_visibility,user_attitude,user_denial
[2] write     id={'commit': 'C1', 'memory_id': 'm1'} head=C1
[3] recall    type=list hits=1 first=m1
              activate keys=flashes,items,profiles,query,route stats={"candidates": 1, "profiles_candidates": 0, "budget": 420, "rendered": 1, "skipped_budget": 0, "profiles_rendered": 0, "
[4] inject    brief_chars=85 brief_first='[记忆账本·此刻想起来的]'
              persona_text_chars=0
[5] stats     {"accesses": 2, "anchors": 0, "by_grade": {"一级": 1, "二级": 0, "零级": 0}, "by_level": {"day": 1}, "clues": 2, "commits": 1, "created_seq": 1, "db_bytes": 4096, "denials": 0, "head": "C1", "links": 0, "memories": 1, "profile
[6] close     db_bytes=4096
SMOKE_OK
```

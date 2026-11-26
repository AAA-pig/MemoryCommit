# astrbot_plugin_memory_commit 重构收尾验收

> **状态（2026-11-26 12:40 补记）**：拆分已合并进本体 `360120c`，版本号同步为 1.6.1，本体工作树 `python -m pytest -q` → 736 passed, 1 skipped。本文档下面的数据与结论是副本阶段的原始记录，未回改；对照表里的旧模块路径已随合并生效。

范围：副本，从 `37edcc8` 开始。以下耗时单位为秒，均取 pytest 自身输出；每一组均为 **736 passed, 1 skipped**。未联网、未升级依赖、未访问线上插件或主库、未推送。

## A. 扫描等价性与性能

| 测量 | 测试结果 | 耗时 |
|---|---|---:|
| 起点，`python -m pytest -q` | 736 passed, 1 skipped | 107.87 |
| A，清理后首次全量 | 736 passed, 1 skipped | 69.16 |
| A，`--durations=20` 诊断 | 736 passed, 1 skipped | 112.90 |
| A，`TEMP` / `TMP` / `TMPDIR` 指到副本的 `.temp` 后复测 | 736 passed, 1 skipped | 74.49 |
| 受控快照，起点 `37edcc8` | 736 passed, 1 skipped | 28.89 |
| 受控快照，仅 A `b899766` | 736 passed, 1 skipped | 27.46 |
| B 独立全量 | 736 passed, 1 skipped | 65.50 |
| 最终，`python -m pytest -q` | 736 passed, 1 skipped | 31.03 |

A 在原工作树的首轮全量是 69.16 秒，未达到 60 秒；在同一副本内用 `git archive` 分别解包起点及仅 A 提交，使用同一解释器、临时目录布局和全量命令，A **达标**。快照根目录不在扫描器的排除路径中；其中的测试临时文件按原规则排除。快照没有原工作树的未跟踪备份和残留，不能把环境差异或端到端的耗时差归功于剪枝，也不能保证默认环境低于 60 秒。诊断中耗时最高的两项是人格写入面的 AST 检查（3.80/3.35 秒），其后有多项 SQLite 回归。没有为达标删测试或改检测器。

B/C 的临时文件均限制在副本内。C、最终和受控快照把 `TEMP` / `TMP` / `TMPDIR` 指到各自的 `.temp/.pytest_cache`，三个扫描器按各自未改变的排除规则剪掉其中一个路径段；没有给扫描增加专用排除项。

```powershell
$testTemp = Join-Path (Get-Location) ".temp\.pytest_cache"
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$env:TEMP = $testTemp; $env:TMP = $testTemp; $env:TMPDIR = $testTemp
.\.venv\Scripts\python.exe -m pytest -q
```

三个扫描器继续在遍历后按原规则过滤并返回 `sorted` 顺序，遍历时剪掉原排除目录。一次性脚本从起点 Git 内容提取旧扫描函数，再用同一目录调用旧、现实现，比较完整有序相对路径，并打印统一 diff。实际输出：

```text
test_dream_guard.py: 83 -> 83; 0.257s -> 0.098s; diff=[]
test_emotion_reserved.py: 83 -> 83; 0.283s -> 0.096s; diff=[]
test_persona_write_surface.py: 34 -> 34; 0.314s -> 0.112s; diff=[]
```

首轮删除根目录 289 个已识别的测试残留，最后补清 578 个。只清理根目录中随机 `tmp` 命名且已忽略的目录：不能包含 Git 跟踪文件、子目录或重解析点，内容须匹配测试产物并含测试数据库。保留未确认项：tmpn2h1qkuo, tmpoiuuymce, tmpul0jh081。此清理不在 Git 提交中，`git revert` 不会恢复它。

## B. 锁文件

实测 uv 0.13.7。空缓存以及环境默认镜像均无法离线解析；最后在副本中读取原公共索引的缓存元数据，指定锁文件原索引重新执行 `uv lock --offline`，最终文件由 **uv 自动生成**。`uv lock --check --offline` 在相同索引与缓存配置下通过；两个版本字段均是 `1.6.0`。验证文件字节等于原文件只替换项目版本行，依赖、哈希和索引均未动。

```text
Resolved 10 packages in 22ms
Updated astrbot-plugin-memory-commit v1.5.0 -> v1.6.0
Resolved 10 packages in 3ms
LOCK_CHECK_OK
```

```diff
diff --git a/uv.lock b/uv.lock
index 75944d2..ee6409a 100644
--- a/uv.lock
+++ b/uv.lock
@@ -7 +7 @@ name = "astrbot-plugin-memory-commit"
-version = "1.5.0"
+version = "1.6.0"
```

## C. 模块合并

30 → 15 个生产模块；根目录 Python 文件 31 → 16（额外的一个是测试夹具 `conftest.py`）。删除 17 个旧模块路径，其中两组合并到新文件。两套门面未增加实现代码，行数变化来自导入去重；没有兼容空壳。

| 最终生产文件 | 物理行 | 吸收的旧文件（均删除） |
|---|---:|---|
| [`ledger.py`](../ledger.py) | 780 | — |
| [`ledger_archive.py`](../ledger_archive.py) | 497 | — |
| [`ledger_commit.py`](../ledger_commit.py) | 551 | `ledger_persona_format.py` |
| [`ledger_core.py`](../ledger_core.py) | 450 | `ledger_constants.py`, `ledger_math.py` |
| [`ledger_dream.py`](../ledger_dream.py) | 411 | `ledger_conflict.py`, `ledger_dream_worker.py` |
| [`ledger_history.py`](../ledger_history.py) | 336 | `ledger_creative.py` |
| [`ledger_maintenance.py`](../ledger_maintenance.py) | 423 | — |
| [`ledger_persona.py`](../ledger_persona.py) | 558 | `ledger_affect.py`, `ledger_profile_ops.py`, `ledger_persona_ops.py` |
| [`ledger_recall.py`](../ledger_recall.py) | 678 | — |
| [`ledger_schema.py`](../ledger_schema.py) | 607 | `ledger_storage.py` |
| [`ledger_status.py`](../ledger_status.py) | 528 | `ledger_messages.py` |
| [`main.py`](../main.py) | 769 | — |
| [`memory_commands.py`](../memory_commands.py) | 383 | `memory_hooks.py` |
| [`persona_cmd.py`](../persona_cmd.py) | 245 | `persona_files.py`, `persona_text.py` |
| [`sink.py`](../sink.py) | 393 | `plugin_utils.py`, `views.py`, `sink_runtime.py` |

其余本轮新增或修改的文件（本报告一并校验 ≤800 行）：

| 文件 | 物理行 |
|---|---:|
| [`tests/test_dream_guard.py`](../tests/test_dream_guard.py) | 272 |
| [`tests/test_emotion_reserved.py`](../tests/test_emotion_reserved.py) | 114 |
| [`tests/test_persona_text.py`](../tests/test_persona_text.py) | 194 |
| [`tests/test_persona_write_surface.py`](../tests/test_persona_write_surface.py) | 164 |
| [`tools/audit_import_graph.py`](../tools/audit_import_graph.py) | 75 |
| [`tools/coverage_check.ps1`](../tools/coverage_check.ps1) | 93 |
| [`uv.lock`](../uv.lock) | 174 |

### 路径级改动清单

`A` 新增，`D` 删除，`M` 修改。不开启 Git 相似度重命名，以免把多源合并误解成单一文件重命名；上表给出每个删除文件的去向。交付另新增本报告和导入图脚本。

```text
M	ledger.py
D	ledger_affect.py
M	ledger_archive.py
M	ledger_commit.py
D	ledger_conflict.py
D	ledger_constants.py
A	ledger_core.py
D	ledger_creative.py
M	ledger_dream.py
D	ledger_dream_worker.py
M	ledger_history.py
M	ledger_maintenance.py
D	ledger_math.py
D	ledger_messages.py
A	ledger_persona.py
D	ledger_persona_format.py
D	ledger_persona_ops.py
D	ledger_profile_ops.py
M	ledger_recall.py
M	ledger_schema.py
M	ledger_status.py
D	ledger_storage.py
M	main.py
M	memory_commands.py
D	memory_hooks.py
M	persona_cmd.py
D	persona_files.py
D	persona_text.py
D	plugin_utils.py
M	sink.py
D	sink_runtime.py
M	tests/test_dream_guard.py
M	tests/test_emotion_reserved.py
M	tests/test_persona_text.py
M	tests/test_persona_write_surface.py
M	tools/coverage_check.ps1
M	uv.lock
D	views.py
```

合并前检索了旧模块的跨行 `patch` / `setattr` 及 `importlib` 目标，没有发现指向待删模块的补丁或按路径载入。保留 `ledger` 和 `main` 门面上的动态绑定；生产代码中旧模块导入、旧文件加载路径和旧私有模块别名均无命中。`persona_text` 方法名及 `persona_files` 资料目录名是现有 API/路径，不是旧模块引用。旧文档只作历史记录。

验证：按基线对比生产 AST，剔除 import / 模块说明并归一旧模块别名，搬移的业务语句一致；31 份测试文件的断言 AST 未变。人格扫描器仍自然覆盖新文件，只更新了 5 个原有 `(文件, 函数)` 坐标，写点 4 个、调用点 8 个的名单数量不变。平铺和包相对加载均验证，门面函数签名、描述符、生成器/协程类型、宿主注册归属、异常与迁移引用身份保持一致。C 的每组和最终冒烟均为 `SMOKE_OK`；最终复核将临时库放在新建的隔离目录里，保留副本中已存在的默认冒烟目录。

### 导入图脚本与原始输出

运行 [`tools/audit_import_graph.py`](../tools/audit_import_graph.py)；加 `--ref 37edcc8` 输出基线图。脚本用 AST 采集根目录生产 `import` / `from … import`，将相对和平铺写法归为一条相同边，包含自环；打印全部邻接表和依赖先行的拓扑序。检测到环会输出循环强连通分量并返回 1，已用双节点环验证。测试、工具、标准库和 AstrBot 不计入图。

```powershell
.\.venv\Scripts\python.exe tools\audit_import_graph.py --ref 37edcc8
.\.venv\Scripts\python.exe tools\audit_import_graph.py
```

```text
source=37edcc8 scope=root-production-python edge=importer->dependency
modules=30 edges=66 cyclic_components=0
ledger -> ledger_affect, ledger_archive, ledger_commit, ledger_conflict, ledger_constants, ledger_creative, ledger_dream, ledger_dream_worker, ledger_history, ledger_maintenance, ledger_math, ledger_messages, ledger_persona_format, ledger_persona_ops, ledger_profile_ops, ledger_recall, ledger_schema, ledger_status, ledger_storage
ledger_affect -> -
ledger_archive -> ledger_constants, ledger_math, ledger_schema
ledger_commit -> ledger_constants, ledger_math, ledger_persona_format
ledger_conflict -> ledger_affect, ledger_constants
ledger_constants -> -
ledger_creative -> ledger_constants, ledger_math
ledger_dream -> ledger_conflict, ledger_constants, ledger_math
ledger_dream_worker -> ledger_constants
ledger_history -> ledger_constants, ledger_math
ledger_maintenance -> ledger_constants, ledger_math
ledger_math -> -
ledger_messages -> ledger_math
ledger_persona_format -> ledger_constants
ledger_persona_ops -> ledger_constants, ledger_persona_format
ledger_profile_ops -> ledger_constants
ledger_recall -> ledger_constants, ledger_math
ledger_schema -> -
ledger_status -> ledger_constants, ledger_math
ledger_storage -> ledger_constants, ledger_schema
main -> ledger, memory_commands, memory_hooks, persona_cmd, persona_files, persona_text, plugin_utils, sink, sink_runtime, views
memory_commands -> -
memory_hooks -> -
persona_cmd -> ledger, persona_files, persona_text, plugin_utils
persona_files -> ledger
persona_text -> -
plugin_utils -> ledger
sink -> ledger
sink_runtime -> -
views -> plugin_utils
dependency-first order: ledger_affect -> ledger_constants -> ledger_math -> ledger_schema -> memory_commands -> memory_hooks -> persona_text -> sink_runtime -> ledger_conflict -> ledger_dream_worker -> ledger_persona_format -> ledger_profile_ops -> ledger_creative -> ledger_history -> ledger_maintenance -> ledger_messages -> ledger_recall -> ledger_status -> ledger_archive -> ledger_storage -> ledger_dream -> ledger_commit -> ledger_persona_ops -> ledger -> persona_files -> plugin_utils -> sink -> persona_cmd -> views -> main

source=worktree scope=root-production-python edge=importer->dependency
modules=15 edges=29 cyclic_components=0
ledger -> ledger_archive, ledger_commit, ledger_core, ledger_dream, ledger_history, ledger_maintenance, ledger_persona, ledger_recall, ledger_schema, ledger_status
ledger_archive -> ledger_core, ledger_schema
ledger_commit -> ledger_core
ledger_core -> -
ledger_dream -> ledger_core, ledger_persona
ledger_history -> ledger_core
ledger_maintenance -> ledger_core
ledger_persona -> ledger_commit, ledger_core
ledger_recall -> ledger_core
ledger_schema -> ledger_core
ledger_status -> ledger_core
main -> ledger, memory_commands, persona_cmd, sink
memory_commands -> -
persona_cmd -> ledger, sink
sink -> ledger
dependency-first order: ledger_core -> memory_commands -> ledger_commit -> ledger_history -> ledger_maintenance -> ledger_recall -> ledger_schema -> ledger_status -> ledger_persona -> ledger_archive -> ledger_dream -> ledger -> sink -> persona_cmd -> main
```

## 提交与回滚

| 顺序 | 提交 | 主题 | 全量秒数 | 模块 / 边 / 环分量 | 单条撤销 |
|---|---|---|---:|---|---|
| A | `b899766` | test: prune three project source scans | 69.16 | 30 / 66 / 0 | `git revert b899766` |
| B | `41f2445` | build: align uv lock project version to 1.6.0 | 65.50 | 30 / 66 / 0 | `git revert 41f2445` |
| C1 | `97e2d11` | refactor: merge ledger_persona_format into ledger_commit | 31.45 | 29 / 63 / 0 | `git revert 97e2d11` |
| C2 | `c08a645` | refactor: merge ledger_constants and ledger_math into ledger_core | 29.27 | 28 / 54 / 0 | `git revert c08a645` |
| C3 | `6ae1131` | refactor: merge ledger_storage into ledger_schema | 30.00 | 27 / 52 / 0 | `git revert 6ae1131` |
| C4 | `c108ade` | refactor: merge ledger_affect, ledger_profile_ops and ledger_persona_ops into ledger_persona | 29.48 | 25 / 49 / 0 | `git revert c108ade` |
| C5 | `f11e81a` | refactor: merge ledger_conflict and ledger_dream_worker into ledger_dream | 31.14 | 23 / 44 / 0 | `git revert f11e81a` |
| C6 | `e6392d4` | refactor: merge ledger_messages into ledger_status | 29.18 | 22 / 42 / 0 | `git revert e6392d4` |
| C7 | `a03a317` | refactor: merge ledger_creative into ledger_history | 29.36 | 21 / 40 / 0 | `git revert a03a317` |
| C8 | `cfabae0` | refactor: merge plugin_utils, views and sink_runtime into sink | 30.97 | 18 / 35 / 0 | `git revert cfabae0` |
| C9 | `a5c8994` | refactor: merge persona_files and persona_text into persona_cmd | 31.52 | 16 / 30 / 0 | `git revert a5c8994` |
| C10 | `cd6a900` | refactor: merge memory_hooks into memory_commands | 30.54 | 15 / 29 / 0 | `git revert cd6a900` |

```text
cd6a900 refactor: merge memory_hooks into memory_commands
a5c8994 refactor: merge persona_files and persona_text into persona_cmd
cfabae0 refactor: merge plugin_utils, views and sink_runtime into sink
a03a317 refactor: merge ledger_creative into ledger_history
e6392d4 refactor: merge ledger_messages into ledger_status
f11e81a refactor: merge ledger_conflict and ledger_dream_worker into ledger_dream
c108ade refactor: merge ledger_affect, ledger_profile_ops and ledger_persona_ops into ledger_persona
6ae1131 refactor: merge ledger_storage into ledger_schema
c08a645 refactor: merge ledger_constants and ledger_math into ledger_core
97e2d11 refactor: merge ledger_persona_format into ledger_commit
41f2445 build: align uv lock project version to 1.6.0
b899766 test: prune three project source scans
```

先确认工作区干净；回滚 C 时从 C10 向 C1 倒序逐条执行表中命令，避免保留对已撤销合并的依赖。B 可独立撤销；A 的 revert 只撤测试源码改动。若连交付件一起撤，先用 `git log -1 --oneline -- docs/refactor-closeout.md` 找到交付提交并 `git revert <交付提交>`。未执行回滚。

## 剩余风险和验收边界

- A 的受控快照计时达标，但原工作树按默认环境跑过 69.16 秒；完整耗时受 Windows 文件 I/O 和临时目录状态影响。
- 15 个生产文件和本轮改动文件均 ≤800 行；原来的五个超长测试未拆：`test_ledger.py`、`test_ledger_edges.py`、`test_main_commands.py`、`test_persona.py`、`test_r_fixes.py`。
- `tools/coverage_check.ps1` 已将解析器目标映射为新文件，文件级覆盖率统计故也纳入同文件的人格命令和资料逻辑。本轮没有把该 100% 覆盖率门禁列作通过项。
- 只用本地 AstrBot 替身和测试临时库验证，未验证真实宿主注册、线上主库或在线升级；第三方直接导入旧内部模块路径须跟随上表更新。
- 先前已记录的非法时间窗误判是既有缺陷，本轮未改。

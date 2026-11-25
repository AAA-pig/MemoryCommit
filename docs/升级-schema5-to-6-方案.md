# schema 5 → 6 升级方案（含演练结果与回滚条件）

> 对象：`astrbot_plugin_memory_commit`，库 `%USERPROFILE%\.local\share\jingjing-memory\ledger.db`
> 代码版本：`1149e3b`（`SCHEMA_VERSION = 6`）
> 定稿时间：2026-11-15
> 结论先说：**真库已经是 v6 了**（见第 1 节实测），本文档里的「升级顺序」因此从「待执行」变成「照单核对」；演练部分仍然有效，用于证明这套流程本身是安全的。

---

## 0. 排障警示（先看这条，踩过一次）

**只读探真库，URI 必须写成 `file:...?mode=ro`，绝对不能加 `immutable=1`。**

`immutable=1` 会让 SQLite 认为「这个文件永远不会变」，于是**不读 `-wal`、也不读 `-shm`**，直接按主库文件 checkpoint 之前的状态回答。真库是 WAL 模式且长期不 checkpoint（进程一直开着），主库文件通常落后 WAL 一大截，读出来的就是**旧快照**。表现非常迷惑：迁移明明已经跑过，探出来却像没跑。

同一份三件套副本上（2026-11-15 04:31 实测，`snap-now\ledger.db`），两种写法的差别：

```
mode=ro                  schema=6 head=C101 commit_no=101 tables=18 creative=1 message=932 access_log=1983 memory_link=14890
mode=ro&immutable=1      schema=5 head=C99  commit_no=99  tables=17 creative=0 message=928 access_log=1977 memory_link=15166
```

也就是说：加 `immutable=1` 会凭空读出一个「schema 5、17 张表、没有 creative、memory_link 还是 15166」的旧世界，而真库早就是 schema 6 / C101 / 18 表 / 14890。

- 正确：`sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)`
- 错误：`...?mode=ro&immutable=1`（跳过 WAL，读旧快照）
- 反面教材：真库判型被这一条参数带偏过一次，得出「真库还是 schema 5」的错误结论，返工一轮
- 判别口诀：**行数对不上先怀疑 WAL，不要先怀疑数据坏了**。`mode=ro` 下 `message` / `access_log` 这类只增表会随写入当场涨，隔几分钟读一次差 1~2 条是正常的（见 1.1 的取数时刻）

---

## 1. 现网实测（只读取证，未对真库写入任何一个字节）

在三件套副本（`ledger.db` + `-wal` + `-shm`）上打开。

**取数时刻（重要，避免「报告数字与真库当场不一致」被当成异常）**：三件套拷贝于 **2026-11-15 04:14:53**（`ledger.db` mtime）/ **04:16:32**（`ledger.db-wal` mtime，383192 字节），探针 `probe_db.py` 在 **04:22:46** 跑完。表里的 `message 930` / `access_log 1980` 就是**那一刻**的读数，不是「现在的读数」。

| 项 | 实测值（04:14:53 快照） | 复测（04:31:05 快照） |
| --- | --- | --- |
| `meta.schema_version` | **6** | **6** |
| `meta.head` / `meta.commit_no` | **C101 / 101** | **C101 / 101** |
| `meta.memory_no` / `meta.message_no` | 581 / 930 | 581 / 932 |
| `PRAGMA integrity_check` | **ok** | **ok** |
| 表数量 | **18**（含 `creative`） | 18 |
| `creative` 索引 | `sqlite_autoindex_creative_1`、**`idx_creative_seq`** | 同左 |
| memory / clue / memory_clue / memory_link | 579 / 1134 / 3471 / 14890 | 579 / 1134 / 3471 / 14890 |
| mcommit / commit_row / message / access_log | 101 / 20827 / 930 / 1980 | 101 / 20827 / 932 / 1983 |
| `-wal` 大小 | 383192 字节（进程正在写） | 465592 字节 |

两次相隔 16 分钟：`message` 930 → 932（+2）、`access_log` 1980 → 1983（+3），其余逐表一分不差。**这就是 WAL 当期还在增长的正常表现**，不是数据被改。读数只增不减、且增量落在「只增表」上，就可以判定为正常写入；若 `memory` / `clue` / `memory_link` 跟着变，才需要停下来查。

也就是说：**`_migrate_5_to_6` 已经在真库上跑过了**，`creative` 表与 `idx_creative_seq` 都已存在。原先「真库还是 schema 5、17 张表、无 creative」的判断与当前磁盘状态不符（那个错误判断的来源见第 0 节：`immutable=1`）——从 `memory_link = 14890` 这个数字还能反推出 `_migrate_auto_sink_legacy` 也已经执行过（见第 3 节：正是 15142 − 252 = 14890）。

可能的原因：AstrBot 侧在这之间重载过插件，`Ledger.__init__` 一打开库就完成了 5→6 与存量沉淀迁移，并顺带写了 C96…C101 这 6 个提交。**这一点需要你确认**，我无法从库文件看出是谁、什么时候触发的（库内没有「迁移时间」这条元数据）。

### 1.1 一个需要单独记一笔的既有漂移（7 条边，可复现）

在副本上跑 `health_check()`，唯一一条问题是：

```
投影对账不一致：memory_link 当前 14890 行 / 重放 14897 行
（当前多 无；重放多 ['m3|m1|记忆账本', 'm3|m2|记忆账本', 'm4|m3|方向']；内容不同 无）
```

注意 `health_check` 的文案对差集只取前 3 条做样例（`ledger.py:4032-4035` 的 `[:3]`），**「重放多」最多显示 3 个，不代表只多 3 条**。全量差集要用下面的脚本取。

**A 组：真库副本（`auto_sink` 照常生效）**

```powershell
python .temp\migrate-drill\drill_link_diff.py .temp\migrate-drill\live_copy.db
```

```
head=C101 current_rows=14890 replay_rows=14897 delta=-7
missing_count=7  extra_count=0  changed_count=0
missing_rows:
  m3|m1|记忆账本
  m3|m2|记忆账本
  m4|m3|方向
  m4|m3|老大交代
  m4|m3|脱离AstrBot
  m5|m3|脱离AstrBot
  m5|m4|脱离AstrBot
```

**B 组（对照组）：同一份 v5 备份，打开前把 `_migrate_auto_sink_legacy` 换成空实现**

```powershell
python .temp\migrate-drill\drill_link_diff.py .temp\migrate-drill\h_nosink.db --no-auto-sink
```

```
head=C95 current_rows=15142 replay_rows=15149 delta=-7
missing_count=7  extra_count=0  changed_count=0
missing_rows: 与 A 组逐条相同（同样是上面那 7 条）
```

两组对照的读法：

| 指标 | A 组（auto_sink 生效，v6 真库副本） | B 组（auto_sink 跳过，v5 备份） | 差 |
| --- | --- | --- | --- |
| `memory_link` 当前行数 | 14890 | 15142 | 252（= auto_sink 摘掉的机器标签边） |
| 重放行数 | 14897 | 15149 | 252 |
| 当前 − 重放 | **−7** | **−7** | **0** |
| 缺失的具体边 | 7 条（见上） | 同样 7 条 | 0 |
| 表里多出的边 | 0 | 0 | 0 |
| 内容不同的边 | 0 | 0 | 0 |

**「一模一样」的实测含义**：把 auto_sink 整个关掉（252 条边不摘），漂移仍然恰好是 −7、缺的仍是同样 7 条、多出的仍是 0。所以这 7 条既不是 5→6 结构迁移造成的，也不是 auto_sink 造成的。

**这 7 条是谁弄丢的**（`drill_link_trace.py` 全量查 `commit_row`）：

```
m3|m1|记忆账本     由 C8  insert，链里没有对应的 delete
m3|m2|记忆账本      由 C8  insert，链里没有对应的 delete
m4|m3|方向         由 C10 insert，链里没有对应的 delete
m4|m3|老大交代      由 C10 insert，链里没有对应的 delete
m4|m3|脱离AstrBot  由 C10 insert，链里没有对应的 delete
m5|m3|脱离AstrBot  由 C14 insert，链里没有对应的 delete
m5|m4|脱离AstrBot  由 C14 insert，链里没有对应的 delete
```

即：这些边在 C8 / C10 / C14 就写进了链，链里**从未记录过删除**，但当前表里确实没有——说明它们是早期被**绕过提交链**的方式删掉的（直连 SQL / 早期版本没走 `commit()` 的删除路径）。这是链外改库留下的历史欠账，与本次升级无关，也不影响读写。**判定：接受为已知漂移**（`health_check` 会一直报这一条），不去补写提交（补写等于伪造历史）；要根治只能重新 materialize 整库，代价大于收益。

---

## 2. 迁移本身做了什么（纯加法）

`_migrate_5_to_6`（`ledger.py:557`）只有两条语句，走 `_tx`，重复执行不出错：

- `CREATE TABLE IF NOT EXISTS creative (...)`，13 列，主键 `id`
- `CREATE INDEX IF NOT EXISTS idx_creative_seq ON creative(created_seq)`

不 `ALTER` 任何存量表，不 `UPDATE`/`DELETE` 任何行，不碰 `memory` 与检索打分。版本号由 `_migrate()` 统一写回 `meta.schema_version`。

`_migrate()` 还有一条单向保护：库里版本比代码新时直接抛 `LedgerError` 拒绝打开，不会静默降级。

---

## 3. `_migrate_auto_sink_legacy` 会不会额外建提交：会，且必须知道它建几个

它不是 5→6 的一部分，但 `_init_schema()` 紧跟着就会调用它，所以「打开一次库」= 结构迁移 + 存量沉淀迁移，两件事。

在 v5 备份（C95）上的实测：

| 指标 | 迁移前 | 迁移后 |
| --- | --- | --- |
| `schema_version` | 5 | **6** |
| `head` | C95 | **C96** |
| `commit_no` | 95 | **96** |
| `memory` | 576 | 576 |
| `clue` | 1134 | 1134 |
| `memory_clue` | 3468 | 3468 |
| `message` | 898 | 898 |
| `access_log` | 1932 | 1932 |
| `memory_link` | 15142 | **14890（−252）** |
| `commit_row` | 20459 | 20747（+288） |
| `integrity_check` | ok | ok |

- 命中存量 **36 条**记忆：`tags` 里有 `自动沉淀` 但没有 `type:auto_sink` → 补类型标记；
- 命中 **252 条**机器标签边：`memory_link.concept` 是 `type:*` 或 `自动沉淀` → 摘掉（`+288 = 36 + 252`，与提交行数增量正好对上）；
- 于是**多出 1 个提交，`commit_no` 95 → 96，`head` 从 C95 变成 C96**。

**这是预期行为还是需要先处理？判断依据与结论：**

1. 口径来源唯一：`is_machine_tag` / `auto_sink_ids` 与迁移函数同源，不是迁移时另起一套规则；
2. 它是**幂等**的——先查后写，没有存量就一个提交都不建。实测在已迁移过的副本上再打开两次，`commit_no` 恒为 101、行数恒等，`idempotent_second_open = true`；
3. 它只动 `tags` 与 `memory_link`，正文、访问痕迹、提交链历史一概不动，且走正常 `commit()`，所以回放与自检对得上；
4. 代价是 HEAD 前进一格。**如果外部有任何东西把「HEAD == C99」当基线（例如上次导出的锚点、别人手里的副本），它会立刻对不上**。

结论：**属预期行为，不需要预处理**，但必须提前打招呼——升级前把当前 HEAD 记下来，升级后 HEAD +1 是正常的，不是数据被改坏。要更稳妥，可以先把「HEAD 前进一格」写进升级检查单，验收时按「HEAD 恰好前进 1」核对。

---

## 4. 正式升级顺序

前提：**AstrBot 不会同时打开这个库**（卸载插件或停止实例之后再动库文件）。任何一步失败就地停住，不往下走。

### 第 0 步：记录基线（不动库）

```powershell
$src = Join-Path $env:USERPROFILE '.local\share\jingjing-memory'   # 插件数据目录，按本机实际改
Get-ChildItem $src -Filter 'ledger.db*' -Force | Select-Object Name,Length,LastWriteTime
python probe_db.py (Join-Path $src 'ledger.db')                    # 必须 mode=ro，见第 0 节
```

把 `schema_version`、`head`、`commit_no`、以及各表行数记进值班本。后续每一步都拿它对照。

### 第 1 步：备份（三件套 + 独立校验）

```powershell
$dst = Join-Path $src ('backup\ledger-pre-v6-upgrade-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $dst | Out-Null
foreach ($n in 'ledger.db','ledger.db-wal','ledger.db-shm') {
    if (Test-Path (Join-Path $src $n)) { Copy-Item (Join-Path $src $n) (Join-Path $dst $n) -Force }
}
# 校验：副本能打开、integrity ok、版本与基线一致
python probe_db.py (Join-Path $dst 'ledger.db')
```

**只拷 `.db` 是错的**：WAL 里未 checkpoint 的事务会丢，备份出来的库会比线上旧，回滚回去等于丢记忆。三件套必须一起，或者用 `sqlite3` 的 `.backup`（一致性快照，但对真库只能读，不许 checkpoint）。

### 第 2 步：在副本上预演（不许跳过）

拷一份三件套到临时目录，用**要上线的那份代码**打开它，确认：版本升到 6、`creative` 与 `idx_creative_seq` 建出、`commit_no` 只 +1（或 +0，若无存量）、逐表行数与预期一致。

```powershell
# 三件套一起拷到临时目录（不要只拷 .db）
$tmp = Join-Path $env:TEMP ('migrate-drill-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
foreach ($n in 'ledger.db','ledger.db-wal','ledger.db-shm') {
    if (Test-Path (Join-Path $src $n)) { Copy-Item (Join-Path $src $n) (Join-Path $tmp $n) -Force }
}
# 预演：用要上线的代码在副本上打开一次，产出逐表前后对照
python drill_migrate.py (Join-Path $src 'backup\ledger-pre-v6-upgrade-<时间戳>\ledger.db') (Join-Path $tmp 'work.db')
# 对账：漂移是不是迁移带来的（两组对照，见第 1.1 节）
python drill_link_diff.py (Join-Path $tmp 'work.db')
```

**预演行数对照表（本次实测，v5 备份 C95 → 打开后 C96）**——这张表就是验收基线，正式升级后逐行比对，差一行都算不通过：

| 表 | 预演前 | 预演后 | 差值 | 预期判定 |
| --- | --- | --- | --- | --- |
| `memory` | 576 | 576 | 0 | 必须 0 |
| `clue` | 1134 | 1134 | 0 | 必须 0 |
| `memory_clue` | 3468 | 3468 | 0 | 必须 0 |
| `message` | 898 | 898 | 0 | 必须 0 |
| `access_log` | 1932 | 1932 | 0 | 必须 0 |
| `memory_link` | 15142 | 14890 | −252 | 等于被摘掉的机器标签边数 |
| `commit_row` | 20459 | 20747 | +288 | = 36（补类型）+ 252（摘边） |
| `mcommit` | 95 | 96 | +1 | 只 +1 |
| `creative` | 不存在 | 0 行 | 建表 | 必须有表 + `idx_creative_seq` |
| `meta.schema_version` | 5 | 6 | +1 | 恰好 6 |
| `integrity_check` | ok | ok | — | 必须 ok |

### 第 3 步：卸载插件 / 停止写入

在 AstrBot 管理面板禁用并卸载该插件实例，确认没有进程再持有 `ledger.db`（检查 `-shm` 是否还在被人打开、mtime 是否停止变化）。

### 第 4 步：换代码并重载

把 `1149e3b` 的插件目录就位，重新启用插件。首次加载会自动完成 5→6（若还没升）。

### 第 5 步：验证（逐条打勾，缺一条都不算过）

```powershell
python probe_db.py (Join-Path $env:USERPROFILE '.local\share\jingjing-memory\ledger.db')   # 只读，mode=ro
python probe_health.py '<副本路径>'                                                          # health_check（副本上跑）
```

1. `meta.schema_version = 6`；
2. `creative` 表存在，`idx_creative_seq` 存在；
3. `memory` / `clue` / `memory_clue` / `message` / `access_log` 行数与第 0 步**逐表相等**；
4. `head` / `commit_no` 只前进 1（无存量则不变）；
5. `PRAGMA integrity_check = ok`；
6. `health_check()` 除已知的 `memory_link` 7 条漂移外无新问题；
7. 面板里跑一次读写冒烟：新增一条记忆、读回、`list_creatives` 返回正常。

**逐条判定阈值（对照这份表打勾，别凭感觉）**

| 检查项 | 通过阈值 | 不通过（触发回滚） |
| --- | --- | --- |
| `schema_version` | 恰好 `6` | 仍是 5（迁移没跑）/ 大于 6（代码比库旧，直接 `LedgerError`） |
| 表数量 | 恰好 `18`，且含 `creative` | 17（没建表）或 >18（多了来历不明的表） |
| 存量表行数 | `memory` / `clue` / `memory_clue` / `commit_row` / `access_log` 与基线**差 0** | 任一表差 ≠ 0 |
| 只增表行数 | `message` / `access_log` 允许 **+0 ~ +5**（升级窗口内的正常写入） | 变少，或一次跳变 > 5 |
| `memory_link` | `14890`（= 15142 − 252）；若库里没有机器标签边，则等于基线 | 差值与「36 条补类型 / 252 条摘边」对不上 |
| `head` / `commit_no` | `commit_no` **恰好 +1**、`head` 变成新的一格；无存量时 +0 | 前进 ≥2（说明重复迁移）或突然归零 |
| `integrity_check` | `ok`（单行 `ok`） | 任何其它输出 |
| `health_check` 问题数 | 恰好 1 条，且是已知的 `memory_link` 7 条漂移 | 出现第 2 条，或同一表重放/当前差 ≠ 7 |
| 二次打开 | `commit_no` 不变、行数不变（幂等） | 再打开又 +1（不幂等，必须停） |
| 冒烟读写 | 新增记忆读得回、`list_creatives` 正常 | 抛异常 / 读不到 / 检索明显异常 |

### 第 6 步：观察

升级后头 24 小时盯三件事：`-wal` 是否长期膨胀（`wal_bytes()` 有没有触发 checkpoint）、`health_check` 问题数是否只减不增、每日导出的 md 是否正常刷新。

---

## 5. 回滚条件与步骤

### 触发条件（任一命中就回滚，不商量；数值阈值见第 4 节第 5 步的表）

- 第 5 步里任何一条**存量表行数变化**（不是 head 前进，是行没了/多了，差 ≠ 0 即回滚）；
- `commit_no` 前进 ≥ 2（重复迁移），或二次打开不幂等；
- `integrity_check` 不是 ok；
- `health_check` 出现**新的**问题条目（尤其「投影对账不一致」里出现别的表，或 `memory_link` 差 ≠ −7）；
- 插件加载即抛 `LedgerError` / 打不开库；
- 记忆读写冒烟失败，或检索结果明显异常。

**判定顺序**：先看 `schema_version` 与表数量（结构性），再看存量表行数（数据性），最后看 `health_check` 文案。前两类不过直接回滚，不必往下查；只有第三类不过时，才需要区分「已知 7 条漂移」与「新问题」。

### 回滚步骤（顺序不可颠倒：先退库，再退代码）

1. 再次卸载插件 / 停止写入（必须，否则覆盖库文件时正在写）。确认没有进程再持有 `ledger.db`：`-shm` 不再被打开、mtime 停止变化。
2. 记账：把当前 `schema_version` / `head` / `commit_no` / 各表行数抄进值班本。回滚会丢掉这段历史，事后要能说清丢了哪些提交。
3. **先把库整套覆盖回 v5**：用第 1 步的三件套备份原样拷回，`.db` + `-wal` + `-shm` **三件一起替换，不许只换 `.db`**（只换 `.db` 会丢 WAL 里未 checkpoint 的事务，等于凭空丢记忆）。覆盖完先只读核一遍：`schema_version = 5`、没有 `creative` 表（`mode=ro` 探库，别加 `immutable=1`，见第 0 节）。
4. **再退代码**：代码退回升级前那个提交（本次是 `1149e3b` 之前，即 `fd33b2b`）。**必须在库已回退到 v5 之后执行；顺序颠倒会导致插件打不开库**——v5 代码打开 v6 库时，`Ledger._migrate` 会立刻抛 `LedgerError：账本结构版本 6 比当前代码支持的 5 还新，先升级代码再打开`，插件直接加载失败。
5. 重新启用，按第 5 步重跑验证（此时 `schema_version` 应为 5、无 `creative` 表）。

**为什么只能库先退、代码后退**：两个方向的兼容性不对称。

- v6 代码认 v5 库：`_migrate()` 看到库里版本比代码旧，会按 `_MIGRATIONS` 往上迁移，正常打开（并顺带跑 `_migrate_auto_sink_legacy`）；
- v5 代码不认 v6 库：`_migrate()` 看到库里版本比代码新，直接 `raise LedgerError` 拒开，不静默降级；
- 所以「先退代码」必然卡在打不开库那一步；「先退库」则代码新旧都能打开。注意库退回去之后如果代码还停在 v6，插件一打开就会把库重新迁回 v6（`HEAD` 前进一格，`commit_no` 也跟着变），等于回滚没退干净——所以第 3 条做完必须紧接着做第 4 条，两条之间不要让插件加载。

### 回滚的已知代价

- **降级方向不可逆**：v5 代码不认识「版本比我新」的库会拒绝打开（`LedgerError`），所以回滚只能是**库先退、代码后退**，顺序颠倒会直接开不了库，见上面第 4 条；
- **会丢升级后新增的记忆**：库退回备份点，之后写入的提交（本次若已发生过，是 C96…C101）全部消失；
- **回滚演练的 tests 口径（已修正）**：`tools/rollback_drill.ps1` 场景 B 早前只按 `git ls-tree <回滚点> -- tests` 的名单**删除**回滚点之后新增的测试文件、不还原同名文件的旧内容。于是 `tests/test_ledger.py`、`tests/test_memory_duty.py` 这类在 `1149e3b` 被改过的文件会带着新断言去跑旧代码，必然红——那是脚本口径造成的假红，不是回滚点不健康（旧版实跑：B 470 收集 / 3 failed，`test_schema_is_v5`、`test_migration_seeds_access_log`、`test_public_api_frozen_no_extra_write_path`）。现已改为删掉副本的 `tests/` 后 `git checkout <回滚点> -- tests` 整套还原，`lost_cases` 也改成「回滚点相对当前 test 集合的净丢失用例数」（按收集到的 node id 集合差算，不是收集总数相减）。场景 A 不受影响，仍只覆盖代码、留着当前 tests。B 再红时会先比对副本 tests 与回滚点是否逐文件一致，一致才判「真回归」；

---

## 6. 本次演练残留物

全部在工作区的 `.temp\migrate-drill\`（`<TeleAgent 工作空间>\.temp\migrate-drill\`）：

- `db\`：真库三件套副本（04:14:53 取数，`-wal` 383192 字节）
- `snap-now\`：04:31 复测用三件套副本（`-wal` 465592 字节）
- `live_copy.db`：真库副本工作拷贝（auto_sink 已生效，A 组）
- `v5src\ledger.db`：02:54 的 v5 备份副本
- `work_v5.db`：演练后库
- `h_nosink.db`：跳过 auto_sink 的对照组（B 组）
- `drill_v5.json`：完整演练结果
- 可复跑脚本：`probe_db.py`（只读探库，**固定 `mode=ro`**）、`probe_health.py`（副本上跑 health_check，`--no-auto-sink` 即对照组）、`drill_migrate.py`（迁移演练）、`drill_link_diff.py`（投影对账全量差集，`--no-auto-sink` 即对照组）、`drill_link_trace.py`（查漂移边在提交链里的来龙去脉）、`cmp_ro.py`（`mode=ro` 与 `immutable=1` 读数对照，第 0 节的证据）

真库全程零写入，AstrBot 进程未重启、未重载。
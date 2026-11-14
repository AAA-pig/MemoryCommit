# -*- coding: utf-8 -*-
<#
回滚演练：验证「回滚了到底有没有被测试抓到」。

两个场景，都在隔离副本里做，绝不碰真工作区：

  A 只覆盖代码文件（ledger/main/conftest/pyproject/metadata），tests 留 HEAD
    期望：红。红了说明新增用例确实钉住了新代码，报警器有信号。
    若全绿 -> 醒目报「报警器失效」：代码退回旧版而测试毫无反应，
    下次真出事的时候测试也会一声不吭。

  B 全量覆盖（代码回旧版 + tests 整套 `git checkout <回滚点> -- tests` 还原）
    期望：绿。绿表示「退回去这个版本自身是健康的」。
    同时算清丢了几个用例（lost_cases）：回滚点相对当前 test 集合的净丢失，
    按收集到的 node id 集合差来数（不是总数相减），这是回滚的隐形成本。

用法：
  .\tools\rollback_drill.ps1                 # 实跑两遍
  .\tools\rollback_drill.ps1 -DryRun         # 只打印计划，不复制不跑测试
  .\tools\rollback_drill.ps1 -KeepWork       # 保留隔离副本便于排查

产物：tools\drill_out\drill_result.json + tools\drill_out\drill_summary.md
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$BackupDir,
    [string]$RollbackTarget = "1.5.1 上线版",
    [string]$Python,
    [string]$OutDir,
    [switch]$DryRun,
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
if (-not $BackupDir) { $BackupDir = Join-Path $RepoRoot ("backups\2026-11-14-" + $RollbackTarget) }
if (-not $OutDir) { $OutDir = Join-Path $RepoRoot "tools\drill_out" }

# 解释器探测与 coverage_check.ps1 共用同一份实现，本脚本不再硬编码路径
. (Join-Path $PSScriptRoot "resolve_python.ps1")
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Resolve-PythonExeOrThrow -RequireModules @("pytest") -RepoRoot $RepoRoot -Hint "用 -Python 指定解释器路径。"
} elseif (-not (Test-PythonModule -Exe $Python -Module "pytest")) {
    throw "指定的 python 跑不了 -m pytest --version：$Python"
}

$CodeFiles = @("ledger.py", "main.py", "conftest.py", "pyproject.toml", "metadata.yaml")

function Write-Step($msg) { Write-Host ("[drill] " + $msg) -ForegroundColor Cyan }
function Write-Alarm($msg) { Write-Host ("[drill][报警器失效] " + $msg) -ForegroundColor Red }

function Get-Total($path) {
    # pytest --collect-only 的收尾行：「480 tests collected in 3.2s」
    $m = [regex]::Match([string]$path, '(\d+)\s+tests?\s+collected')
    if ($m.Success) { return [int]$m.Groups[1].Value }
    return -1
}

function Invoke-Pytest($work, $label) {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:COVERAGE_FILE = (Join-Path $env:TEMP ("drill_" + [guid]::NewGuid().ToString("N") + ".coverage"))
    Push-Location $work
    try {
        $collect = & $Python -m pytest -q --collect-only -p no:cacheprovider 2>&1 | Out-String
        $total = Get-Total $collect
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $out = & $Python -m pytest -q -p no:cacheprovider --tb=line 2>&1 | Out-String
        $code = $LASTEXITCODE
        $sw.Stop()
    } finally {
        Pop-Location
    }
    $passed = 0; $failed = 0; $errors = 0
    $mp = [regex]::Match($out, '(\d+)\s+passed')
    if ($mp.Success) { $passed = [int]$mp.Groups[1].Value }
    $mf = [regex]::Match($out, '(\d+)\s+failed')
    if ($mf.Success) { $failed = [int]$mf.Groups[1].Value }
    $me = [regex]::Match($out, '(\d+)\s+errors?')
    if ($me.Success) { $errors = [int]$me.Groups[1].Value }
    $tail = (($out -split "`r?`n") | Where-Object { $_ -match '\S' } | Select-Object -Last 6) -join " | "
    $env:COVERAGE_FILE = $null
    $env:PYTHONDONTWRITEBYTECODE = $null
    return [ordered]@{
        label     = $label
        exit_code = $code
        collected = $total
        passed    = $passed
        failed    = $failed
        errors    = $errors
        seconds   = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        tail      = $tail
    }
}

function New-IsolatedCopy($dest) {
    # 必须带 .git 历史：test_r3_fixes.py 会用 git show 取评审基线提交，
    # 直接复制工作区（没有 .git）会让两条存量迁移用例 errored，假红。
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    & git clone --quiet --local --no-hardlinks $RepoRoot $dest 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git clone 隔离副本失败" }
    # 再把工作区里未提交的改动/未跟踪文件盖上去（clone 只带已提交内容）
    $rc = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
        "`"$RepoRoot`"", "`"$dest`"", "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
        "/XD", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "backups", "drill_out",
        "/XF", "*.pyc"
    ) -NoNewWindow -Wait -PassThru
    if ($rc.ExitCode -ge 8) { throw "robocopy 覆盖失败，退出码 $($rc.ExitCode)" }
    return $dest
}

function Get-CodeDrift {
    # live 与备份的 blob hash 差异（用 --path 让两侧过同一套 filters，禁裸 sha256）
    $drift = @()
    foreach ($f in $CodeFiles) {
        $live = (& git -C $RepoRoot hash-object --path=$f $f).Trim()
        $bak = (& git -C $RepoRoot hash-object --path=$f (Join-Path $BackupDir $f)).Trim()
        if ($live -ne $bak) { $drift += $f }
    }
    return $drift
}

function Restore-CodeFiles($work) {
    foreach ($f in $CodeFiles) {
        $src = Join-Path $BackupDir $f
        if (-not (Test-Path $src)) { throw "备份缺文件：$src" }
        Copy-Item -Force $src (Join-Path $work $f)
    }
}

function Restore-TestsToTarget($work) {
    <#
    把隔离副本的 tests 整棵树还原到回滚点（不只是删文件）。

    为什么不沿用旧写法：旧实现只按 `git ls-tree <回滚点> -- tests` 的名单
    删除「回滚点之后新增」的测试文件，同名文件保持新版内容不动。于是
    tests/test_ledger.py、tests/test_memory_duty.py 这类在 schema6 定版 被改过的
    文件，会带着新断言去跑旧代码，必然红 —— 那是脚本口径造成的假红，
    不是回滚点不健康。现在改成：
      1) 删掉副本里的 tests/ 整棵树（隔离副本，删了只影响副本）
      2) `git checkout <回滚点> -- tests` 一次性取出回滚点的 tests 树
    这样同名文件的内容、回滚点之后新增的文件、回滚点之后删掉的文件，
    三件事一次对齐到回滚点。

    另外统计文件级差异（删了几个、补回几个），只用于报告，不参与判定。
    #>
    $testDir = Join-Path $work "tests"
    # 只统计源码文件：__pycache__ 里的 .pyc 是上一次收集的产物，不算「测试文件」
    $rel = {
        param($file, $root)
        $p = ($file.FullName.Substring($root.Length) -replace '\\', '/').TrimStart('/')
        if ($p -match '__pycache__/') { return $null }
        return $p
    }
    $before = @()
    if (Test-Path $testDir) {
        $before = @(Get-ChildItem -Recurse -File $testDir | ForEach-Object { & $rel $_ $work } | Where-Object { $_ })
        Remove-Item -Recurse -Force $testDir
    }
    & git -C $work checkout $RollbackTarget -- tests 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git checkout $RollbackTarget -- tests 失败（回滚点里没有 tests/？）" }
    $after = @()
    if (Test-Path $testDir) {
        $after = @(Get-ChildItem -Recurse -File $testDir | ForEach-Object { & $rel $_ $work } | Where-Object { $_ })
    }
    return [ordered]@{
        files_before = $before.Count
        files_after  = $after.Count
        removed      = @($before | Where-Object { $after -notcontains $_ })
        restored     = @($after | Where-Object { $before -notcontains $_ })
    }
}

function Get-TestsMismatch($work, $rev) {
    <#
    逐文件比对副本的 tests/ 与某个提交的 tests 树（只看磁盘，不看 index）。
    返回不一致项：多余 / 内容不同 / 缺失。空数组 = 与回滚点逐字节一致。
    只用于场景 B 红了之后的归因，不参与正常判定。
    #>
    $mismatch = @()
    $target = @{}
    foreach ($p in (& git -C $work ls-tree -r --name-only $rev -- tests 2>$null)) {
        if ($p) { $target[$p] = (& git -C $work rev-parse ("{0}:{1}" -f $rev, $p)).Trim() }
    }
    $onDisk = @{}
    $testDir = Join-Path $work "tests"
    if (Test-Path $testDir) {
        foreach ($f in Get-ChildItem -Recurse -File $testDir) {
            $rel = ($f.FullName.Substring($work.Length) -replace '\\', '/').TrimStart('/')
            if ($rel -match '__pycache__/') { continue }
            $onDisk[$rel] = $true
        }
    }
    foreach ($rel in $onDisk.Keys) {
        if (-not $target.ContainsKey($rel)) { $mismatch += "$rel(多余)"; continue }
        $h = (& git -C $work hash-object --path=$rel $rel).Trim()
        if ($h -ne $target[$rel]) { $mismatch += "$rel(内容不同)" }
    }
    foreach ($rel in $target.Keys) { if (-not $onDisk.ContainsKey($rel)) { $mismatch += "$rel(缺失)" } }
    return $mismatch
}

function Get-CollectedIds($work) {
    <#
    收集当前 tests 树里的用例 node id 集合（形如 tests/test_x.py::test_y[param]）。

    用途：算 lost_cases。口径见场景 B 的注释 —— 用集合差，不用总数相减。
    收集失败（返回空集合）由调用方判 -1，不静默当 0。
    #>
    Push-Location $work
    try {
        $out = & $Python -m pytest -q --collect-only -p no:cacheprovider 2>&1 | Out-String
    } finally {
        Pop-Location
    }
    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($line in ($out -split "`r?`n")) {
        $t = $line.Trim()
        if ($t -match '::') { [void]$ids.Add($t) }
    }
    return $ids
}

# --------------------------------------------------------------------------
# 0. 前置检查（一律显式失败，不静默降级）
# --------------------------------------------------------------------------
if (-not (Test-Path $Python)) { throw "测试解释器不存在：$Python" }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not (Test-Path $BackupDir)) { throw "备份目录不存在：$BackupDir" }
foreach ($f in $CodeFiles) {
    if (-not (Test-Path (Join-Path $BackupDir $f))) { throw "备份缺文件：$(Join-Path $BackupDir $f)" }
}
$head = (& git -C $RepoRoot rev-parse HEAD).Trim()
$dirty = (& git -C $RepoRoot status --porcelain) -join "; "

Write-Step "仓库 $RepoRoot"
Write-Step "HEAD $head / 回滚点 $RollbackTarget / 备份 $BackupDir"
Write-Step "解释器 $Python"
if ($dirty) { Write-Step "注意：工作区不干净 -> $dirty" }

if ($DryRun) {
    Write-Step "DryRun：只输出计划，不复制、不跑测试"
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $plan = [ordered]@{
        dry_run         = $true
        generated_at    = (Get-Date).ToString("s")
        repo            = $RepoRoot
        head            = $head
        rollback_target = $RollbackTarget
        backup_dir      = $BackupDir
        python          = $Python
        code_files      = $CodeFiles
        scenarios       = @(
            [ordered]@{ name = "A_code_only"; expect = "red"; steps = @("隔离副本", "覆盖 $($CodeFiles -join ', ')", "tests 保持 HEAD", "pytest -q") },
            [ordered]@{ name = "B_full"; expect = "green"; steps = @("隔离副本", "覆盖代码", "tests 整套 git checkout $RollbackTarget -- tests", "pytest -q", "算 lost_cases（node id 集合差）") }
        )
    }
    $plan | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $OutDir "drill_result.json")
    @("# 回滚演练 DryRun", "", "- HEAD: $head", "- 回滚点: $RollbackTarget", "- 见 drill_result.json") |
        Set-Content -Encoding UTF8 (Join-Path $OutDir "drill_summary.md")
    Write-Step "已写 $OutDir\drill_result.json（dry_run）"
    exit 0
}

# --------------------------------------------------------------------------
# 场景 A：只覆盖代码，tests 留 HEAD
# --------------------------------------------------------------------------
$workA = Join-Path $env:TEMP ("drill_A_" + [guid]::NewGuid().ToString("N"))
Write-Step "场景 A 开始（隔离副本 $workA）"
New-IsolatedCopy $workA | Out-Null
$headCollectedA = (Invoke-Pytest $workA "A_baseline_head").collected
$resA = $null
$driftA = Get-CodeDrift
Restore-CodeFiles $workA
$resA = Invoke-Pytest $workA "A_code_only"
$resA["expect"] = "red"
$resA["head_collected"] = $headCollectedA
$resA["code_drift"] = ($driftA -join ", ")
if ($resA.failed -eq 0 -and $resA.errors -gt 0) {
    # 只有收集期报错：接口被回退掉了也算红，但比断言失败弱，得说清楚
    $resA["actual"] = "error_red"
    $resA["alarm"] = "weak"
    Write-Host ("[drill][弱信号] 场景 A 只有 {0} 个 error、0 failed：多半是接口被退掉（收集期就挂），算红但不算断言级证据" -f $resA.errors) -ForegroundColor Yellow
} elseif ($resA.exit_code -ne 0) {
    $resA["actual"] = "red"
    $resA["alarm"] = "ok"
    Write-Step ("场景 A：红（退出码 {0}，failed={1}）—— 报警器有信号" -f $resA.exit_code, $resA.failed)
} elseif ($driftA.Count -eq 0) {
    # 代码与回滚点逐字节相同：A 本来就该绿，不是报警器坏了
    $resA["actual"] = "green"
    $resA["alarm"] = "no_drift"
    Write-Step ("场景 A：绿（passed={0}）。本轮生产代码与回滚点逐字节相同（drift=0），A 无信息量，不算报警器失效" -f $resA.passed)
} else {
    $resA["actual"] = "green"
    $resA["alarm"] = "BLIND"
    Write-Alarm ("场景 A 全绿（passed={0}）：代码退回了 {1} 而测试毫无反应，报警器失效" -f $resA.passed, ($driftA -join ","))
}
if (-not $KeepWork) { Remove-Item -Recurse -Force $workA -ErrorAction SilentlyContinue }

# --------------------------------------------------------------------------
# 场景 B：全量覆盖（代码旧版 + tests 回滚点）
# --------------------------------------------------------------------------
$workB = Join-Path $env:TEMP ("drill_B_" + [guid]::NewGuid().ToString("N"))
Write-Step "场景 B 开始（隔离副本 $workB）"
New-IsolatedCopy $workB | Out-Null
$headCollectedB = (Invoke-Pytest $workB "B_baseline_head").collected
# lost_cases 口径（别让数字凭空变）：
#   lost_cases = 回滚点相对当前 test 集合的「净丢失用例数」
#              = |当前收集到的 node id 集合 − 回滚点收集到的 node id 集合|
#   用 node id 集合差，不用「收集总数相减」：总数相减会把同名文件里
#   「删掉的用例」和「新加的用例」相互抵消，也说不清丢的是哪个；
#   集合差逐条对得上，且只算当前有、回滚点没有的（净丢失）。
$headIds = Get-CollectedIds $workB
Restore-CodeFiles $workB
$testRestore = Restore-TestsToTarget $workB
$removed = $testRestore.removed.Count
$resB = Invoke-Pytest $workB "B_full"
$targetCollected = $resB.collected
$targetIds = Get-CollectedIds $workB
if ($headIds.Count -gt 0 -and $targetIds.Count -gt 0) {
    $lostIds = @($headIds | Where-Object { -not $targetIds.Contains($_) })
    $lost = $lostIds.Count
} else {
    $lostIds = @()
    $lost = -1
}
$bGreen = ($resB.exit_code -eq 0)
$resB["expect"] = "green"
$resB["head_collected"] = $headCollectedB
$resB["removed_test_files"] = $removed
$resB["removed_test_file_list"] = @($testRestore.removed)
$resB["restored_test_file_list"] = @($testRestore.restored)
$resB["tests_files_before"] = $testRestore.files_before
$resB["tests_files_after"] = $testRestore.files_after
$resB["lost_cases"] = $lost
$resB["lost_cases_sample"] = @($lostIds | Select-Object -First 10)
if ($bGreen) {
    $resB["actual"] = "green"
    Write-Step ("场景 B：绿（passed={0}），净丢失用例 {1} 条（收集数 {2} -> {3}）" -f $resB.passed, $lost, $headCollectedB, $targetCollected)
} elseif ($resB.failed -eq 0 -and $resB.errors -gt 0) {
    $resB["actual"] = "error_red"
    Write-Host ("[drill][弱信号] 场景 B 只有 {0} 个 error、0 failed：回滚点代码收集期就挂，别退" -f $resB.errors) -ForegroundColor Yellow
} else {
    $resB["actual"] = "red"
    # 红了必须分清是「回滚点真不健康」还是「脚本没把 tests 还原干净」：
    # 拿副本 tests 树与回滚点逐文件比 hash（多余/内容不同/缺失三种）
    $mismatch = @(Get-TestsMismatch $workB $RollbackTarget)
    $resB["tests_mismatch_vs_target"] = $mismatch
    if ($mismatch.Count -eq 0) {
        $resB["red_cause"] = "real_regression"
        Write-Host ("[drill][异常] 场景 B 回滚后仍红（failed={0}）：tests 已与回滚点逐文件一致，属真回归——回滚点自身不健康，先别退" -f $resB.failed) -ForegroundColor Yellow
    } else {
        $resB["red_cause"] = "script_incomplete"
        Write-Host ("[drill][异常] 场景 B 仍红（failed={0}），但 tests 有 {1} 个文件与回滚点不一致，属脚本没还原干净，不当作回滚点结论：{2}" -f $resB.failed, $mismatch.Count, ($mismatch -join ", ")) -ForegroundColor Yellow
    }
}
if (-not $KeepWork) { Remove-Item -Recurse -Force $workB -ErrorAction SilentlyContinue }

# --------------------------------------------------------------------------
# 结果落盘
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$verdict = @()
switch ($resA.alarm) {
    "ok"        { $verdict += "A 红，报警器有效" }
    "weak"      { $verdict += "A 只有收集期报错，算红但信号偏弱" }
    "no_drift"  { $verdict += "A 绿，但代码与回滚点零差异，本轮无法验证报警器" }
    "BLIND"     { $verdict += "A 绿且代码确有差异，报警器失效" }
}
if ($bGreen) { $verdict += ("B 绿，回滚点健康，净丢失 {0} 条用例" -f $lost) }
elseif ($resB.actual -eq "error_red") { $verdict += "B 收集期就报错，回滚点不可用" }
elseif ($resB.red_cause -eq "script_incomplete") { $verdict += "B 红，但 tests 未还原干净（脚本问题），不作为回滚点结论" }
else { $verdict += "B 红，tests 与回滚点一致，回滚点不健康" }

$result = [ordered]@{
    dry_run             = $false
    generated_at        = (Get-Date).ToString("s")
    repo                = $RepoRoot
    head                = $head
    rollback_target     = $RollbackTarget
    backup_dir          = $BackupDir
    python              = $Python
    workdir_dirty       = [bool]$dirty
    scenario_a          = $resA
    scenario_b          = $resB
    verdict             = ($verdict -join "；")
}
$jsonPath = Join-Path $OutDir "drill_result.json"
$result | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $jsonPath

$md = @(
    "# 回滚演练摘要",
    "",
    "- 生成时间：" + $result.generated_at,
    "- HEAD：" + $head,
    "- 回滚点：" + $RollbackTarget,
    "- 解释器：" + $Python,
    "",
    "## 场景 A（只覆盖代码，tests 留 HEAD）",
    "- 期望：红；实际：" + $resA.actual,
    "- 退出码 " + $resA.exit_code + "，passed " + $resA.passed + "，failed " + $resA.failed + "，errors " + $resA.errors + "，耗时 " + $resA.seconds + "s",
    "- 代码 drift（live vs 备份）：" + $(if ($driftA.Count -eq 0) { "无" } else { $driftA -join ", " }),
    "- 报警器：" + $resA.alarm,
    "",
    "## 场景 B（全量覆盖）",
    "- 期望：绿；实际：" + $resB.actual,
    "- 退出码 " + $resB.exit_code + "，passed " + $resB.passed + "，failed " + $resB.failed + "，errors " + $resB.errors + "，耗时 " + $resB.seconds + "s",
    "- 用例数：" + $headCollectedB + " -> " + $targetCollected + "，净丢失 " + $lost + " 条（node id 集合差）；tests 文件 " + $testRestore.files_before + " -> " + $testRestore.files_after + "，删除 " + $removed + " 个、补回 " + $testRestore.restored.Count + " 个",
    "",
    "## 结论",
    ($verdict -join "；")
)
$mdPath = Join-Path $OutDir "drill_summary.md"
$md | Set-Content -Encoding UTF8 $mdPath

Write-Step "已写 $jsonPath"
Write-Step "已写 $mdPath"
Write-Step ("结论：" + ($verdict -join "；"))
# -*- coding: utf-8 -*-
<#
回滚演练：验证「回滚了到底有没有被测试抓到」。

两个场景，都在隔离副本里做，绝不碰真工作区：

  A 只覆盖代码文件（ledger/main/conftest/pyproject/metadata），tests 留 HEAD
    期望：红。红了说明新增用例确实钉住了新代码，报警器有信号。
    若全绿 -> 醒目报「报警器失效」：代码退回旧版而测试毫无反应，
    下次真出事的时候测试也会一声不吭。

  B 全量覆盖（代码回旧版 + tests 回到回滚点，即丢掉回滚点之后新增的用例）
    期望：绿。绿表示「退回去这个版本自身是健康的」。
    同时算清丢了几个用例（lost_cases），这是回滚的隐形成本。

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
    [string]$RollbackTarget = "8677e13",
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
    # 回滚点的 tests/ 名单：git ls-tree 只读，不动工作区
    $tracked = & git -C $RepoRoot ls-tree -r --name-only $RollbackTarget -- tests 2>$null
    if ($LASTEXITCODE -ne 0) { throw "git ls-tree $RollbackTarget 失败" }
    $keep = @{}
    foreach ($p in $tracked) { if ($p) { $keep[$p] = $true } }
    $removed = 0
    $testDir = Join-Path $work "tests"
    if (Test-Path $testDir) {
        foreach ($item in Get-ChildItem -Recurse -File $testDir) {
            $rel = ($item.FullName.Substring($work.Length) -replace '\\', '/').TrimStart('/')
            if (-not $keep.ContainsKey($rel)) { Remove-Item -Force $item.FullName; $removed++ }
        }
    }
    return $removed
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
            [ordered]@{ name = "B_full"; expect = "green"; steps = @("隔离副本", "覆盖代码", "tests 退回 $RollbackTarget 名单", "pytest -q", "算 lost_cases") }
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
Restore-CodeFiles $workB
$removed = Restore-TestsToTarget $workB
$resB = Invoke-Pytest $workB "B_full"
$targetCollected = $resB.collected
$lost = if ($headCollectedB -ge 0 -and $targetCollected -ge 0) { $headCollectedB - $targetCollected } else { -1 }
$bGreen = ($resB.exit_code -eq 0)
$resB["expect"] = "green"
$resB["head_collected"] = $headCollectedB
$resB["removed_test_files"] = $removed
$resB["lost_cases"] = $lost
if ($bGreen) {
    $resB["actual"] = "green"
    Write-Step ("场景 B：绿（passed={0}），丢失用例 {1} 条（{2} -> {3}）" -f $resB.passed, $lost, $headCollectedB, $targetCollected)
} elseif ($resB.failed -eq 0 -and $resB.errors -gt 0) {
    $resB["actual"] = "error_red"
    Write-Host ("[drill][弱信号] 场景 B 只有 {0} 个 error、0 failed：回滚点代码收集期就挂，别退" -f $resB.errors) -ForegroundColor Yellow
} else {
    $resB["actual"] = "red"
    Write-Host ("[drill][异常] 场景 B 回滚后仍红（failed={0}）：回滚点自身不健康，先别退" -f $resB.failed) -ForegroundColor Yellow
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
if ($bGreen) { $verdict += ("B 绿，回滚点健康，丢弃 {0} 条用例" -f $lost) }
elseif ($resB.actual -eq "error_red") { $verdict += "B 收集期就报错，回滚点不可用" }
else { $verdict += "B 红，回滚点不健康" }

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
    "- 用例数：" + $headCollectedB + " -> " + $targetCollected + "，丢失 " + $lost + " 条；删除测试文件 " + $removed + " 个",
    "",
    "## 结论",
    ($verdict -join "；")
)
$mdPath = Join-Path $OutDir "drill_summary.md"
$md | Set-Content -Encoding UTF8 $mdPath

Write-Step "已写 $jsonPath"
Write-Step "已写 $mdPath"
Write-Step ("结论：" + ($verdict -join "；"))
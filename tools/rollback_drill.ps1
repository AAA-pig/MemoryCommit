# -*- coding: utf-8 -*-
<#
回滚演练：验证「回滚了到底有没有被测试抓到」。

三个场景（A / A2 / B），都在隔离副本里做，绝不碰真工作区：

  A 只覆盖代码文件（ledger/main/conftest/pyproject/metadata），tests 留 HEAD
    期望：红。红了说明新增用例确实钉住了新代码，报警器有信号。
    若全绿 -> 醒目报「报警器失效」：代码退回旧版而测试毫无反应，
    下次真出事的时候测试也会一声不吭。

  A2 只覆盖 ledger.py、main.py，conftest.py 留在 HEAD —— 条件触发
    A 把测试基建（conftest.py / pyproject.toml / metadata.yaml）一起退回，
    夹具退回后可能有测试崩在夹具上而不是断言上，A 的失败集合就被基建噪声
    污染。A2 与 A 的失败集合差集就是这个噪声量：A 有、A2 没有 = 由基建回退
    造成，不是代码回退的证据。
    但这个对照只有在基建文件内容真的变了时才有信息量：若被替换的基建文件
    与回滚点内容相同（SHA256 归一化后无差异，仅行尾不同也算相同），A 覆盖
    它们等于什么都没换，差集 0 是必然，推不出「基建噪声 0 条」。所以 A2
    条件触发：基建文件至少一个内容真变才跑；否则跳过并在报告标
    applicable=false，verdict 禁止基于空对照报「基建噪声 0 条」。

  B 全量覆盖（代码回旧版 + tests 整套 `git checkout <回滚点> -- tests` 还原）
    期望：绿。绿表示「退回去这个版本自身是健康的」。
    同时算清丢了几个用例（lost_cases）：回滚点相对当前 test 集合的净丢失，
    按收集到的 node id 集合差来数（不是总数相减），这是回滚的隐形成本。

可复现：json 与 summary 头部记 script_sha256（本脚本自身的哈希）、工作树是否
dirty、python 与 pytest 版本。脚本一改，旧报告就对不上，先看这一行再信数字。

用法：
  .\tools\rollback_drill.ps1                 # 实跑三遍
  .\tools\rollback_drill.ps1 -DryRun         # 只打印计划，不复制不跑测试
  .\tools\rollback_drill.ps1 -KeepWork       # 保留隔离副本便于排查

产物：tools\drill_out\drill_result.json + tools\drill_out\drill_summary.md
      + 各次 pytest 完整输出（A_baseline_head.log / drill_A_pytest.log /
        A2_baseline_head.log / drill_A2_pytest.log /
        B_baseline_head.log / drill_B_pytest.log）
      + 失败用例明细（drill_A_failed.txt / drill_A2_failed.txt / drill_B_failed.txt，
        含 -ra 全量 node id）
      + 场景 B 净丢失全量（drill_B_lost_cases.txt）
      + code_drift 文件级增删行数（code_drift_stat）
      + 演练脚本自身未提交改动 diff（drill_diff.patch，
        git diff HEAD -- tools/rollback_drill.ps1，实跑时生成，路径必须真实存在）
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
# A2 只退生产代码，conftest.py（测试基建）留 HEAD：用来量出 A 里有多少失败
# 是夹具跟着回退造成的
$CodeFilesA2 = @("ledger.py", "main.py")

function Write-Step($msg) { Write-Host ("[drill] " + $msg) -ForegroundColor Cyan }
function Write-Alarm($msg) { Write-Host ("[drill][报警器失效] " + $msg) -ForegroundColor Red }

function Write-Utf8NoBomChecked($path, $text) {
    <#
    json / md 这类要给程序和验收读的产物必须无 BOM：PS5.1 的
    Set-Content -Encoding UTF8 默认带 BOM，实测 python json.load 直接抛
    JSONDecodeError，jq 也读不了。统一走 WriteAllText + UTF8Encoding($false)，
    写完读回前 3 字节自检，若是 EF BB BF 就报错退出，不许静默产出。
    #>
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $text, $utf8)
    $b = [System.IO.File]::ReadAllBytes($path)
    if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
        throw ("BOM 自检失败：{0} 前 3 字节为 EF BB BF（UTF-8 BOM），拒绝静默产出" -f $path)
    }
    $hex = if ($b.Length -ge 3) { ("{0:X2} {1:X2} {2:X2}" -f $b[0], $b[1], $b[2]) } else { "（文件不足 3 字节）" }
    Write-Step ("无 BOM 自检通过：{0}（前 3 字节 {1}）" -f (Split-Path -Leaf $path), $hex)
}

function Get-Total($path) {
    # pytest --collect-only 的收尾行：「480 tests collected in 3.2s」
    $m = [regex]::Match([string]$path, '(\d+)\s+tests?\s+collected')
    if ($m.Success) { return [int]$m.Groups[1].Value }
    return -1
}

function Get-JunitFailureMap($xmlPath) {
    <#
    junitxml -> node id 映射，返回 { node id = @{ kind; exc_type; message } }。

    为什么要这份报告：-ra 短摘要只在 longrepr 带 reprcrash 时才写「 - message」，
    本仓库的核心用例是 unittest.TestCase 风格，实测 19 条失败里 18 条没有 message，
    靠它判失败原因等于瞎猜。junit 的 failure/error 元素给出结构化的 traceback
    正文，异常类名可从正文稳定提取（见下面 type 提取的注释），与输出格式无关。

    classname 是点分模块名（tests.test_creative.CreativeTableTest），映射回文件路径
    时从长到短试：拼成 tests/test_creative/CreativeTableTest.py 之类都不存在，
    直到 tests/test_creative.py 命中仓库真实测试文件为止，剩下的段当类名。
    映射不出来的条目直接不放进 map，由调用方计入 unmatched，不静默丢弃。
    #>
    $map = @{}
    if (-not (Test-Path -LiteralPath $xmlPath)) { return $map }
    try { [xml]$doc = Get-Content -LiteralPath $xmlPath -Raw -Encoding UTF8 } catch { return $map }
    foreach ($tc in @($doc.SelectNodes('//testcase'))) {
        $cn = [string]$tc.classname
        $nm = [string]$tc.name
        if ([string]::IsNullOrWhiteSpace($nm)) { continue }
        $failEl = $null; $errEl = $null
        foreach ($ch in @($tc.ChildNodes)) {
            if ($ch.NodeType -ne 'Element') { continue }
            if ($ch.Name -eq 'failure') { $failEl = $ch } elseif ($ch.Name -eq 'error') { $errEl = $ch }
        }
        if ($null -eq $failEl -and $null -eq $errEl) { continue }
        $segs = @($cn -split '\.')
        $file = $null
        $cls = @()
        for ($i = $segs.Count; $i -ge 1; $i--) {
            $cand = (($segs[0..($i - 1)] -join '/') + '.py')
            if ($RepoTestFiles.Contains($cand)) {
                $file = $cand
                if ($i -lt $segs.Count) { $cls = @($segs[$i..($segs.Count - 1)]) }
                break
            }
        }
        if ($null -eq $file) { continue }
        $node = $file + '::' + ((@($cls) + @($nm)) -join '::')
        $el = $(if ($null -ne $failEl) { $failEl } else { $errEl })
        $msg = ([string]$el.GetAttribute("message") -replace "`r?`n", " ").Trim()
        if ($msg.Length -gt 240) { $msg = $msg.Substring(0, 240) + "…" }
        # 异常类名：pytest 的 junit（xunit1/xunit2 都试过）failure 元素不带 type
        # 属性，只能从 traceback 正文里取——取正文里最后一次出现的 XxxError /
        # XxxException（traceback 收尾行就是「路径:行号: 异常类名」）。
        # 取不到就留空，由调用方显式归入「未识别」，不猜。
        $excType = [string]$el.GetAttribute("type")
        if ([string]::IsNullOrWhiteSpace($excType)) {
            $bodyMatches = [regex]::Matches([string]$el.InnerText, '([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))')
            if ($bodyMatches.Count -gt 0) { $excType = $bodyMatches[$bodyMatches.Count - 1].Groups[1].Value }
        }
        $map[$node] = [ordered]@{
            kind     = $(if ($null -ne $failEl) { "FAILED" } else { "ERROR" })
            exc_type = $excType
            message  = $msg
        }
    }
    return $map
}

function Invoke-Pytest($work, $label, $logName) {
    # $logName：完整 pytest 输出落盘到 OutDir 的文件名（可审计性：出问题能翻原始输出）
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:COVERAGE_FILE = (Join-Path $env:TEMP ("drill_" + [guid]::NewGuid().ToString("N") + ".coverage"))
    Push-Location $work
    try {
        $collect = & $Python -m pytest -q --collect-only -p no:cacheprovider 2>&1 | Out-String
        $total = Get-Total $collect
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        # -ra 只加「short test summary info」段（FAILED/ERROR <node id>），
        # 让失败明细可整段提取；不改通过/失败判定（退出码与统计数字不受影响）
        # --junitxml 只多写一份机器可读报告：上一版靠短摘要里的「 - message」
        # 取失败原因，实测 unittest 风格的用例这条 message 根本不写（19 条里
        # 只有 1 条带），归因会全落进「认不出」。junit 的 traceback 正文不受
        # 输出格式影响，异常类名可稳定提取。
        $xmlPath = Join-Path $env:TEMP ("drill_junit_" + [guid]::NewGuid().ToString("N") + ".xml")
        $out = & $Python -m pytest -q -ra -p no:cacheprovider --tb=line "--junitxml=$xmlPath" 2>&1 | Out-String
        $code = $LASTEXITCODE
        $sw.Stop()
    } finally {
        Pop-Location
    }
    if ([string]::IsNullOrWhiteSpace($logName)) { $logName = ($label + ".log") }
    $logPath = Join-Path $OutDir $logName
    $out | Set-Content -Encoding UTF8 $logPath
    $passed = 0; $failed = 0; $errors = 0
    $mp = [regex]::Match($out, '(\d+)\s+passed')
    if ($mp.Success) { $passed = [int]$mp.Groups[1].Value }
    $mf = [regex]::Match($out, '(\d+)\s+failed')
    if ($mf.Success) { $failed = [int]$mf.Groups[1].Value }
    $me = [regex]::Match($out, '(\d+)\s+errors?')
    if ($me.Success) { $errors = [int]$me.Groups[1].Value }
    $tail = (($out -split "`r?`n") | Where-Object { $_ -match '\S' } | Select-Object -Last 6) -join " | "
    # 失败用例 node id 全量提取（-ra 短摘要的 FAILED/ERROR 行；绿跑 = 空数组）。
    # 同时留下 kind（FAILED=用例体失败 / ERROR=setup-teardown 或收集期失败）与
    # 短摘要里的 message：归因要用「失败原因」当证据，光有 node id 不够。
    # node id 里可能有空格（参数化 id），所以不能按 \S+ 截断：
    # 用非贪婪吃到可选的行尾「 - <message>」之前。
    $failedIds = New-Object 'System.Collections.Generic.List[string]'
    $failedDetail = New-Object 'System.Collections.Generic.List[object]'
    $summaryKind = @{}
    foreach ($line in ($out -split "`r?`n")) {
        $m = [regex]::Match($line, '^(FAILED|ERROR)\s+(tests/.+?)(?:\s+-\s+(.*))?$')
        if (-not $m.Success) { continue }
        $nid = $m.Groups[2].Value.Trim()
        [void]$failedIds.Add($nid)
        if (-not $summaryKind.ContainsKey($nid)) { $summaryKind[$nid] = $m.Groups[1].Value }
    }
    # junit 补异常类名：node id 从短摘要拿（那是权威清单），原因从 junit 拿
    $jmap = Get-JunitFailureMap $xmlPath
    if (Test-Path -LiteralPath $xmlPath) { Remove-Item -LiteralPath $xmlPath -Force -ErrorAction SilentlyContinue }
    $unmatched = 0
    foreach ($nid in $failedIds) {
        if ($jmap.ContainsKey($nid)) {
            $j = $jmap[$nid]
            $failedDetail.Add([ordered]@{
                node_id       = $nid
                kind          = $j.kind
                reason_source = "junitxml"
                exc_type      = $j.exc_type
                message       = $j.message
            })
        } else {
            # junit 里对不上（极少见：classname 映射不出文件）：留空并计数，
            # 让归因显式落到「未确认」，不静默当断言失败
            $unmatched++
            $failedDetail.Add([ordered]@{
                node_id       = $nid
                kind          = $(if ($summaryKind.ContainsKey($nid)) { $summaryKind[$nid] } else { "FAILED" })
                reason_source = "unmatched"
                exc_type      = ""
                message       = ""
            })
        }
    }
    $env:COVERAGE_FILE = $null
    $env:PYTHONDONTWRITEBYTECODE = $null
    return [ordered]@{
        label      = $label
        exit_code  = $code
        collected  = $total
        passed     = $passed
        failed     = $failed
        errors     = $errors
        seconds    = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        tail       = $tail
        log_file   = $logName
        failed_ids = @($failedIds)
        # PS 5.1 的 @() 对 List[object] 会抛「Argument types do not match」，
        # List[string] 不会；这里显式 ToArray() 绕开这个坑
        failed_detail = $failedDetail.ToArray()
        junit_unmatched = $unmatched
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

function Get-NormalizedSha256($path) {
    <#
    内容指纹：去掉 UTF-8 BOM、CRLF 与孤立 CR 统一成 LF 之后再取 SHA256。
    为什么不直接用 git hash-object：它只按 .gitattributes / core.autocrlf 过滤，
    本仓库 core.autocrlf=false 且没有行尾属性，于是备份快照（LF）与工作区（CRLF）
    逐字节必然不同、hash 也就不同，把「仅行尾差异」误报成代码漂移。
    #>
    $text = [IO.File]::ReadAllText($path)
    $text = $text.TrimStart([char]0xFEFF)
    $text = ($text -replace "`r`n", "`n") -replace "`r", "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))).Replace("-", "").ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-CodeDrift {
    <#
    live 与备份的差异，分两档，别混着报：
      changed  归一化后 SHA256 不同 = 代码真变了（只有这一档进 code_drift）
      eol_only 原始字节不同、归一化后相同 = 仅行尾/编码差异，代码零改动
    上一版把第二档也写进 drift 行，紧接着又说「内容零改动」，自相矛盾。
    #>
    $changed = @(); $eolOnly = @()
    foreach ($f in $CodeFiles) {
        $live = Join-Path $RepoRoot $f
        $bak = Join-Path $BackupDir $f
        if ((Get-NormalizedSha256 $live) -ne (Get-NormalizedSha256 $bak)) { $changed += $f; continue }
        $rawLive = (Get-FileHash -LiteralPath $live -Algorithm SHA256).Hash
        $rawBak = (Get-FileHash -LiteralPath $bak -Algorithm SHA256).Hash
        if ($rawLive -ne $rawBak) { $eolOnly += $f }
    }
    return [ordered]@{ changed = $changed; eol_only = $eolOnly }
}

function Get-CodeDriftStat($work, $files) {
    <#
    drift 的分量：文件级增删行数（等价 git diff --stat，回滚点 -> 当前）。
    在隔离副本里对备份比对：此刻副本还是当前代码，备份是回滚点代码。
    只算 A 场景的 changed 档：仅行尾差异的文件已由 Get-CodeDrift 单独归档，
    不在这里混着计数。退出码 1 = 有差异属正常；警告行（CRLF 提示）一律按
    非数字行滤掉。
    #>
    $stat = New-Object 'System.Collections.Generic.List[object]'
    foreach ($f in @($files)) {
        $bak = Join-Path $BackupDir $f
        $cur = Join-Path $work $f
        if (-not (Test-Path $cur)) { continue }
        # git 警告（CRLF 之类）也走 stderr；ErrorActionPreference=Stop 时
        # stderr 重定向会抛 NativeCommandError，先放宽再收回。数字两列
        # （增/删行数）才是 numstat 正文，警告行一律滤掉，不猜不报数。
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $numIgnore = (& git -C $work diff --no-index --numstat --ignore-cr-at-eol -- $bak $cur 2>&1 | Out-String)
            $line = (($numIgnore -split "`r?`n") | Where-Object { $_ -match '^\d+\s+\d+\s' } | Select-Object -First 1)
            if (-not [string]::IsNullOrWhiteSpace([string]$line)) {
                $parts = ([string]$line) -split '\s+'
                $stat.Add([ordered]@{ file = $f; add = [int]$parts[0]; del = [int]$parts[1] })
            } else {
                # 走到这里说明「归一化后哈希不同」而 numstat 又没给出数字：
                # 只可能是 numstat 本身出问题（二进制文件、git 报错等），
                # 显式标注留给日志排查，不猜数、不写 0 冒充「零改动」。
                $stat.Add([ordered]@{ file = $f; add = -1; del = -1; note = "有差异但 numstat 无输出，详查 pytest/diff 日志" })
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }
    return $stat
}

function Write-ArtifactLines($path, $lines) {
    # 明细落盘：空集合也要留下空文件本体（产物清单稳定，空文件 = 「0 条」的证据）
    if ($null -eq $lines) { $lines = @() }
    $arr = @($lines)
    if ($arr.Count -gt 0) {
        Set-Content -Encoding UTF8 -Path $path -Value $arr
    } else {
        [IO.File]::WriteAllText($path, "")
    }
}

function Restore-CodeFiles($work, $files) {
    # $files 省略时按完整清单还原（场景 A/B）；场景 A2 只传 ledger/main 两个
    if ($null -eq $files) { $files = $CodeFiles }
    foreach ($f in @($files)) {
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
    场景 B 跑完无条件算一遍（结果进报告，证明 tests 树还原干净），
    B 红时它就是归因依据。只读，不改任何文件。
    #>
    $mismatch = @()
    # 同 Get-TestsTreeDiff：git 的 CRLF 警告走 stderr，Stop 模式下会变成终止性错误
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $target = @{}
        foreach ($p in (& git -C $work ls-tree -r --name-only $rev -- tests 2>&1)) {
            if ($p -and $p -notmatch '^warning:') { $target[$p] = (& git -C $work rev-parse ("{0}:{1}" -f $rev, $p) 2>&1 | Out-String).Trim() }
        }
    } finally {
        $ErrorActionPreference = $prevEap
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
        $prevEap2 = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try { $h = ((& git -C $work hash-object --path=$rel $rel 2>&1 | Out-String).Trim()) } finally { $ErrorActionPreference = $prevEap2 }
        if ($h -ne $target[$rel]) { $mismatch += "$rel(内容不同)" }
    }
    foreach ($rel in $target.Keys) { if (-not $onDisk.ContainsKey($rel)) { $mismatch += "$rel(缺失)" } }
    return $mismatch
}

function Get-TestsTreeDiff($work, $rev) {
    <#
    tests 树相对回滚点的文件级差异，交给 git 自己算，不靠「前后文件集合相减」猜。

    git diff --name-status <回滚点> -- tests 的三类状态，各有确切含义：
      A 回滚点之后新增的文件  -> 还原时会被删除（removed）
      M 同名但内容改过的文件  -> 还原时内容被回滚（content_rolled_back）
      D 回滚点有、当前没有    -> 还原时会被补回（restored）

    上一版报告的 restored 恒为 0，不是数据有问题，是定义问题：它只数
    「还原后新出现、还原前不存在」的文件，即 D；而 M（同名文件内容被改回去）
    根本不在里面。所以 22 -> 13「删除 9、补回 0」漏报了 2 个内容被回滚的文件。
    这里改成三条清单分开报，定义写进注释，不再用含混的 restored 一个数概括。

    只用于报告，不参与任何通过/失败判定。
    #>
    $added = @(); $modified = @(); $deleted = @()
    # git 的 CRLF 警告走 stderr，ErrorActionPreference=Stop 时会让 stderr 变成
    # 终止性错误（NativeCommandError），先放宽跑完再收回；只认「状态字母 + 制表符」的正文行
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $raw = (& git -C $work diff --name-status $rev -- tests 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $prevEap
    }
    foreach ($line in ($raw -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $parts = ([string]$line) -split "`t"
        if ($parts.Count -lt 2) { continue }
        $status = $parts[0]
        if ($status -notmatch '^[AMDR]\d*$') { continue }
        $path = $parts[1]
        switch ($status.Substring(0, 1)) {
            "A" { $added += $path }
            "M" { $modified += $path }
            "D" { $deleted += $path }
        }
    }
    return [ordered]@{
        added              = @($added)
        content_modified   = @($modified)
        missing_at_head    = @($deleted)
    }
}

function Get-FailureReasonClass($detail) {
    <#
    失败原因分档，输入是 Invoke-Pytest 里 junit 解析出的 kind + exc_type：
      ERROR 行         -> setup_error （夹具装配/收集期失败，不是用例体失败）
      AssertionError   -> assertion    （断言失败）
      ImportError 类   -> import_fail  （导入层就挂）
      AttributeError / TypeError -> api_missing（接口缺失：被测属性/方法或其
                                    签名在回滚点不存在，回退把接口退掉了）
      其它 XxxError    -> business     （插件业务异常，例如 LedgerError / sqlite3.OperationalError）
      没有类型信息     -> unknown      （junit 未匹配到，显式标未确认，不猜）

    口径说明：assertion / business 对应证据 (b)，api_missing 对应证据 (c)。
    (c) 是本轮新增的独立证据类型，不并入 (b)：上一版把 AttributeError 的接口
    缺失并进「断言/业务调用层」一起报，口径被擅自扩展；单独成类后 summary
    明确标注「证据类型 c 为本轮新增，待确认」。setup_error / import_fail 是
    测试基建或导入层故障，属反向证据。fixture error、collection error、
    ImportError 按任务要求不进正向证据。
    #>
    if ($detail.kind -eq "ERROR") { return [ordered]@{ cls = "setup_error"; exc_type = "setup/teardown error" } }
    $t = [string]$detail.exc_type
    if ([string]::IsNullOrWhiteSpace($t)) { return [ordered]@{ cls = "unknown"; exc_type = "未识别（junit 未匹配）" } }
    if ($t -eq "AssertionError") { return [ordered]@{ cls = "assertion"; exc_type = $t } }
    if ($t -eq "ImportError" -or $t -eq "ModuleNotFoundError") { return [ordered]@{ cls = "import_fail"; exc_type = $t } }
    if ($t -eq "AttributeError" -or $t -eq "TypeError") { return [ordered]@{ cls = "api_missing"; exc_type = $t } }
    if ($t -match '(Error|Exception|Interrupt|Exit)$') { return [ordered]@{ cls = "business"; exc_type = $t } }
    return [ordered]@{ cls = "unknown"; exc_type = $t }
}

function Get-EvidenceLabel($reasonClass, $excType) {
    <# 每条证据写明属于 (a) / (b) / (c) 哪类；类型 c 为本轮新增口径，不并入 b。 #>
    switch ($reasonClass) {
        "assertion"   { return ("证据 (b)：AssertionError（断言失败）") }
        "business"    { return ("证据 (b)：业务异常 " + $excType) }
        "api_missing" { return ("证据 (c)：接口缺失 " + $excType + "（被测属性/方法在回滚点不存在；类型 c 为本轮新增、待确认，不并入 b）") }
        default       { return "" }
    }
}

function Get-FailureBuckets($details, $bPassedSet) {
    <#
    A 场景失败的归因三分。证据三条，都要求「可核」，且每条标明类型：

      (a) 同 node id 在场景 B 通过 —— 说明这条用例本身没坏，退回旧代码才红；
      (b) 失败原因是 AssertionError 或业务异常 —— 说明失败发生在断言/业务逻辑上，
          而不是夹具、收集或导入层；
      (c) 接口缺失：AttributeError / TypeError，被测属性/方法在回滚点不存在 ——
          本轮新增的独立证据类型，不并入 (b)：计数单独报，summary 标「待确认」。

    只认这三条，测试文件名和漂移模块名对得上不给分：上一版按文件名判，
    test_creative.py 里 16 条因 v6 的 creative 表而失败的用例全被记成「意外」，
    结论旁边挂着「意外 17」却不解释，看着像藏了 17 个真 bug。

    三分：
      confirmed_expected 至少有一条证据，且证据逐条列出（判断链条写进报告）
      real_unexpected    有反向证据：夹具/收集/导入层故障，失败与代码行为无关
      unconfirmed        既无正向证据、也无反向证据，只能标未确认，不硬判
    #>
    $confirmed = @(); $real = @(); $unconfirmed = @()
    foreach ($d in @($details)) {
        $drop = Get-FailureReasonClass $d
        $evidence = @()
        if ($null -ne $bPassedSet -and $bPassedSet.Contains([string]$d.node_id)) {
            $evidence += "证据 (a)：B 场景同 node id 通过"
        }
        $label = Get-EvidenceLabel $drop.cls $drop.exc_type
        if ($label) { $evidence += $label }
        $item = [ordered]@{
            node_id      = $d.node_id
            kind         = $d.kind
            reason_class = $drop.cls
            exc_type     = $drop.exc_type
            message      = $d.message
            evidence     = @($evidence)
        }
        if ($evidence.Count -gt 0) { $confirmed += $item }
        elseif ($drop.cls -eq "setup_error" -or $drop.cls -eq "import_fail") { $real += $item }
        else { $unconfirmed += $item }
    }
    return [ordered]@{
        confirmed_expected = @($confirmed)
        real_unexpected    = @($real)
        unconfirmed        = @($unconfirmed)
    }
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
# junit 的 classname 是点分模块名，映射回文件路径时要判断「哪一段是模块、
# 哪一段是类」，这里先列出仓库真实的测试文件（相对路径、正斜杠）
$RepoTestFiles = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
$testsRoot = Join-Path $RepoRoot "tests"
if (Test-Path $testsRoot) {
    foreach ($f in Get-ChildItem -Recurse -File $testsRoot -Filter *.py) {
        [void]$RepoTestFiles.Add((($f.FullName.Substring($RepoRoot.Length)) -replace '\\', '/').TrimStart('/'))
    }
}
# 可复现三件套：报告是哪版脚本、在什么工作树上、用什么解释器跑出来的。
# 脚本一改，旧报告的结论就不再对应新逻辑，先认这三行再信数字。
$scriptSha = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
$pyVersion = ((& $Python -c "import sys; print(sys.version.split()[0])" 2>&1) | Out-String).Trim()
$pytestVersion = ((& $Python -m pytest --version 2>&1 | Select-Object -First 1) | Out-String).Trim()
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null   # 日志随跑随写，目录先就位

Write-Step "仓库 $RepoRoot"
Write-Step "HEAD $head / 回滚点 $RollbackTarget / 备份 $BackupDir"
Write-Step "解释器 $Python（Python $pyVersion，$pytestVersion）"
Write-Step "脚本 SHA256 $scriptSha"
if ($dirty) { Write-Step "注意：工作区不干净 -> $dirty" }

if ($DryRun) {
    Write-Step "DryRun：只输出计划，不复制、不跑测试"
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $plan = [ordered]@{
        dry_run         = $true
        generated_at    = (Get-Date).ToString("s")
        script_sha256   = $scriptSha
        repo            = $RepoRoot
        head            = $head
        rollback_target = $RollbackTarget
        backup_dir      = $BackupDir
        python          = $Python
        python_version  = $pyVersion
        pytest_version  = $pytestVersion
        workdir_dirty   = [bool]$dirty
        workdir_dirty_text = $dirty
        code_files      = $CodeFiles
        code_files_a2   = $CodeFilesA2
        artifacts       = @("A_baseline_head.log", "drill_A_pytest.log", "drill_A_failed.txt",
                            "A2_baseline_head.log", "drill_A2_pytest.log", "drill_A2_failed.txt",
                            "B_baseline_head.log", "drill_B_pytest.log", "drill_B_failed.txt",
                            "drill_B_lost_cases.txt", "drill_result.json", "drill_summary.md")
        scenarios       = @(
            [ordered]@{ name = "A_code_only"; expect = "red"; steps = @("隔离副本", "基线跑一遍", "覆盖 $($CodeFiles -join ', ')", "tests 保持 HEAD", "pytest -q") },
            [ordered]@{ name = "A2_code_only_keep_conftest"; expect = "red"; applicable_rule = "条件触发：conftest.py / pyproject.toml / metadata.yaml 至少一个内容真变（SHA256 归一化后非行尾差异）才跑；否则跳过，applicable=false，不据空对照报基建噪声"; steps = @("隔离副本", "基线跑一遍", "只覆盖 $($CodeFilesA2 -join ', ')", "conftest.py 与 tests 保持 HEAD", "pytest -q", "与 A 比失败集合差（= 基建噪声）") },
            [ordered]@{ name = "B_full"; expect = "green"; steps = @("隔离副本", "基线跑一遍", "覆盖代码", "tests 整套 git checkout $RollbackTarget -- tests", "pytest -q", "算 lost_cases（node id 集合差）") }
        )
    }
    Write-Utf8NoBomChecked (Join-Path $OutDir "drill_result.json") ($plan | ConvertTo-Json -Depth 6)
    $mdDry = @("# 回滚演练 DryRun", "", "- HEAD: $head", "- 回滚点: $RollbackTarget", "- 见 drill_result.json")
    Write-Utf8NoBomChecked (Join-Path $OutDir "drill_summary.md") (($mdDry -join "`r`n") + "`r`n")
    Write-Step "已写 $OutDir\drill_result.json（dry_run）"
    exit 0
}

# --------------------------------------------------------------------------
# 场景 A：只覆盖代码，tests 留 HEAD
# --------------------------------------------------------------------------
$workA = Join-Path $env:TEMP ("drill_A_" + [guid]::NewGuid().ToString("N"))
Write-Step "场景 A 开始（隔离副本 $workA）"
New-IsolatedCopy $workA | Out-Null
$baseA = Invoke-Pytest $workA "A_baseline_head" "A_baseline_head.log"
$headCollectedA = $baseA.collected
if ($baseA.failed -gt 0 -or $baseA.errors -gt 0) {
    # 基线不健康 = 副本本来就坏，此时 A 的红不能算回滚的账。显式喊，不静默。
    Write-Host ("[drill][基线异常] 场景 A 副本基线 {0} passed / {1} failed / {2} errors（collected {3}）：副本本身不干净，A 的红不可归因于回滚" -f $baseA.passed, $baseA.failed, $baseA.errors, $baseA.collected) -ForegroundColor Red
}
$resA = $null
$driftA = Get-CodeDrift
# drift 分量（回滚点 -> 当前的文件级增删行数）：此刻副本还是当前代码，比完再还原。
# 只给真变了的文件算行数，仅行尾差异的不掺和。
$driftStatA = @(Get-CodeDriftStat $workA $driftA.changed)
Restore-CodeFiles $workA
$resA = Invoke-Pytest $workA "A_code_only" "drill_A_pytest.log"
$resA["expect"] = "red"
$resA["baseline"] = $baseA
$resA["head_collected"] = $headCollectedA
$resA["code_drift"] = (@($driftA.changed) -join ", ")
$resA["code_drift_eol_only"] = (@($driftA.eol_only) -join ", ")
$resA["code_drift_note"] = "code_drift 只列 SHA256 归一化后真变的文件；code_drift_eol_only 是仅行尾/编码差异、内容零改动，不计入 drift"
$resA["code_drift_stat"] = $driftStatA
if ($resA.failed -eq 0 -and $resA.errors -gt 0) {
    # 只有收集期报错：接口被回退掉了也算红，但比断言失败弱，得说清楚
    $resA["actual"] = "error_red"
    $resA["alarm"] = "weak"
    Write-Host ("[drill][弱信号] 场景 A 只有 {0} 个 error、0 failed：多半是接口被退掉（收集期就挂），算红但不算断言级证据" -f $resA.errors) -ForegroundColor Yellow
} elseif ($resA.exit_code -ne 0) {
    $resA["actual"] = "red"
    $resA["alarm"] = "ok"
    Write-Step ("场景 A：红（退出码 {0}，failed={1}）—— 报警器有信号" -f $resA.exit_code, $resA.failed)
} elseif ($driftA.changed.Count -eq 0) {
    # 代码与回滚点逐字节相同：A 本来就该绿，不是报警器坏了
    $resA["actual"] = "green"
    $resA["alarm"] = "no_drift"
    Write-Step ("场景 A：绿（passed={0}）。本轮生产代码与回滚点逐字节相同（drift=0），A 无信息量，不算报警器失效" -f $resA.passed)
} else {
    $resA["actual"] = "green"
    $resA["alarm"] = "BLIND"
    Write-Alarm ("场景 A 全绿（passed={0}）：代码退回了 {1} 而测试毫无反应，报警器失效" -f $resA.passed, (@($driftA.changed) -join ","))
}

# 失败明细全量落盘（归因三分要等 B 跑完拿到「B 同 node id 是否通过」才能定，
# 所以分类挪到报告段，这里只落盘原始明细）
$failedIdsA = @($resA.failed_ids)
Write-ArtifactLines (Join-Path $OutDir "drill_A_failed.txt") $failedIdsA
$resA["failed_ids_file"] = "drill_A_failed.txt"
if (-not $KeepWork) { Remove-Item -Recurse -Force $workA -ErrorAction SilentlyContinue }

# --------------------------------------------------------------------------
# 场景 A2：只回退生产代码，测试基建留 HEAD —— 条件触发
# --------------------------------------------------------------------------
# 为什么要 A2：A 的漂移里含测试基建（conftest.py / pyproject.toml / metadata.yaml）。
# 夹具跟着退回，部分测试会崩在夹具装配而不是断言上，这些失败混在 A 的失败集合里，
# 会让「报警器有效」这个结论靠基建噪声撑数。A2 只退 ledger.py + main.py，与 A 的
# 失败集合做差：
#   A 有、A2 没有 = 由测试基建回退造成的基建噪声
#   A2 有、A 没有 = 不该出现的（说明两个场景判定不一致，得查）
# 但这个对照只有在基建文件内容真的变了时才有信息量：若被替换的基建文件与回滚点
# 内容相同（SHA256 归一化后无差异，仅行尾不同也算相同），A 覆盖它们等于什么都没
# 换，A 与 A2 唯一的区别就不存在，差集 0 是必然，推不出「基建噪声 0 条」。
# 所以 A2 条件触发：基建文件至少一个内容真变才跑；否则跳过并标 applicable=false，
# verdict 禁止基于空对照报「基建噪声 0 条」。
$InfraFiles = @("conftest.py", "pyproject.toml", "metadata.yaml")
$infraChanged = @($driftA.changed | Where-Object { $InfraFiles -contains $_ })
$infraEolOnly = @($driftA.eol_only | Where-Object { $InfraFiles -contains $_ })
$a2Applicable = ($infraChanged.Count -gt 0)
$resA2 = $null
if ($a2Applicable) {
    $workA2 = Join-Path $env:TEMP ("drill_A2_" + [guid]::NewGuid().ToString("N"))
    Write-Step ("场景 A2 开始（隔离副本 {0}；触发条件成立：测试基建内容真变 -> {1}）" -f $workA2, (@($infraChanged) -join ", "))
    New-IsolatedCopy $workA2 | Out-Null
    $baseA2 = Invoke-Pytest $workA2 "A2_baseline_head" "A2_baseline_head.log"
    if ($baseA2.failed -gt 0 -or $baseA2.errors -gt 0) {
        Write-Host ("[drill][基线异常] 场景 A2 副本基线 {0} passed / {1} failed / {2} errors：副本本身不干净，A2 不可归因" -f $baseA2.passed, $baseA2.failed, $baseA2.errors) -ForegroundColor Red
    }
    Restore-CodeFiles $workA2 $CodeFilesA2
    $resA2 = Invoke-Pytest $workA2 "A2_code_only_keep_conftest" "drill_A2_pytest.log"
    $resA2["expect"] = "red"
    $resA2["actual"] = $(if ($resA2.exit_code -ne 0) { "red" } else { "green" })
    $resA2["baseline"] = $baseA2
    $resA2["code_files_rolled_back"] = @($CodeFilesA2)
    $resA2["kept_at_head"] = @($InfraFiles)
    $resA2["applicable"] = $true
    $resA2["applicable_basis"] = "条件触发成立：测试基建文件内容真变（SHA256 归一化后非行尾差异）-> " + (@($infraChanged) -join ", ")
    Write-ArtifactLines (Join-Path $OutDir "drill_A2_failed.txt") @($resA2.failed_ids)
    $resA2["failed_ids_file"] = "drill_A2_failed.txt"
    if (-not $KeepWork) { Remove-Item -Recurse -Force $workA2 -ErrorAction SilentlyContinue }
} else {
    # 跳过时清掉旧轮残留的 A2 产物：时间戳还是上一轮的，留着会被误当本轮证据
    foreach ($stale in @("A2_baseline_head.log", "drill_A2_pytest.log", "drill_A2_failed.txt")) {
        $stalePath = Join-Path $OutDir $stale
        if (Test-Path -LiteralPath $stalePath) { Remove-Item -Force -LiteralPath $stalePath }
    }
    $resA2 = [ordered]@{
        applicable     = $false
        skip_reason    = "被替换的测试基建文件与回滚点内容相同（SHA256 归一化后 conftest.py / pyproject.toml / metadata.yaml 均无内容差异），A 与 A2 的失败集合对照为空，A2 本轮不适用"
        infra_changed  = @($infraChanged)
        infra_eol_only = @($infraEolOnly)
        infra_note     = "仅行尾/编码差异不算内容变化；这些文件被 A 覆盖与否不影响测试语义"
    }
    Write-Step ("场景 A2 跳过：{0}" -f $resA2.skip_reason)
}

# --------------------------------------------------------------------------
# 场景 B：全量覆盖（代码旧版 + tests 回滚点）
# --------------------------------------------------------------------------
$workB = Join-Path $env:TEMP ("drill_B_" + [guid]::NewGuid().ToString("N"))
Write-Step "场景 B 开始（隔离副本 $workB）"
New-IsolatedCopy $workB | Out-Null
$baseB = Invoke-Pytest $workB "B_baseline_head" "B_baseline_head.log"
$headCollectedB = $baseB.collected
if ($baseB.failed -gt 0 -or $baseB.errors -gt 0) {
    Write-Host ("[drill][基线异常] 场景 B 副本基线 {0} passed / {1} failed / {2} errors：副本本身不干净，B 的绿/红都不可信" -f $baseB.passed, $baseB.failed, $baseB.errors) -ForegroundColor Red
}
# lost_cases 口径（别让数字凭空变）：
#   lost_cases = 回滚点相对当前 test 集合的「净丢失用例数」
#              = |当前收集到的 node id 集合 − 回滚点收集到的 node id 集合|
#   用 node id 集合差，不用「收集总数相减」：总数相减会把同名文件里
#   「删掉的用例」和「新加的用例」相互抵消，也说不清丢的是哪个；
#   集合差逐条对得上，且只算当前有、回滚点没有的（净丢失）。
$headIds = Get-CollectedIds $workB
# 还原前先让 git 算清 tests 树相对回滚点的文件级差异（新增/内容改动/缺失），
# 用来替换上一版恒为 0 的 restored 口径，见 Get-TestsTreeDiff 注释
$testsTreeDiff = Get-TestsTreeDiff $workB $RollbackTarget
Restore-CodeFiles $workB
$testRestore = Restore-TestsToTarget $workB
$removed = $testRestore.removed.Count
$resB = Invoke-Pytest $workB "B_full" "drill_B_pytest.log"
$targetCollected = $resB.collected
$targetIds = Get-CollectedIds $workB
# 还原是否干净：副本 tests 树与回滚点逐文件比（多余/内容不同/缺失），空 = 完全一致。
# 只读校验，不改 B 的还原逻辑，但让「B 红到底是真回归还是脚本没还原干净」有据可查。
$testsMismatch = @(Get-TestsMismatch $workB $RollbackTarget)
if ($headIds.Count -gt 0 -and $targetIds.Count -gt 0) {
    $lostIds = @($headIds | Where-Object { -not $targetIds.Contains($_) })
    $lost = $lostIds.Count
} else {
    $lostIds = @()
    $lost = -1
}
$bGreen = ($resB.exit_code -eq 0)
$resB["expect"] = "green"
$resB["baseline"] = $baseB
$resB["head_collected"] = $headCollectedB
$resB["removed_test_files"] = $removed
$resB["removed_test_file_list"] = @($testRestore.removed)
$resB["restored_test_file_list"] = @($testRestore.restored)
$resB["tests_files_before"] = $testRestore.files_before
$resB["tests_files_after"] = $testRestore.files_after
# 文件级口径（git diff --name-status <回滚点> -- tests，还原前取）：
#   added            回滚点之后新增，还原时被删掉
#   content_modified 同名文件内容被改回旧版（上一版完全没报这一类）
#   missing_at_head  回滚点有、当前没有，还原时补回
$resB["tests_tree_diff"] = $testsTreeDiff
$resB["tests_tree_diff_rule"] = "git diff --name-status <回滚点> -- tests；A=回滚点之后新增(将被删) M=同名内容改动(将被回滚) D=当前缺失(将被补回)"
$resB["tests_tree_matches_target"] = ($testsMismatch.Count -eq 0)
$resB["tests_mismatch_vs_target"] = $testsMismatch
$resB["lost_cases"] = $lost
$resB["lost_cases_sample"] = @($lostIds | Select-Object -First 10)
# 净丢失全量落盘 + 按测试文件分组（文件→丢失条数，降序）：口径不变（node id 集合差），
# 只是把集合完整展示出来 + 看丢失集中在哪个文件，别再只看前 10 条样本
Write-ArtifactLines (Join-Path $OutDir "drill_B_lost_cases.txt") $lostIds
$resB["lost_cases_file"] = "drill_B_lost_cases.txt"
$lostByFile = [ordered]@{}
foreach ($g in @($lostIds | Group-Object { ($_ -split '::')[0] } | Sort-Object Count -Descending)) {
    $lostByFile[$g.Name] = $g.Count
}
$resB["lost_cases_by_file"] = $lostByFile
# B 的失败明细（B 绿时是空文件；红时这里就是排查起点）
Write-ArtifactLines (Join-Path $OutDir "drill_B_failed.txt") @($resB.failed_ids)
$resB["failed_ids_file"] = "drill_B_failed.txt"
if ($bGreen) {
    $resB["actual"] = "green"
    Write-Step ("场景 B：绿（passed={0}），净丢失用例 {1} 条（收集数 {2} -> {3}）" -f $resB.passed, $lost, $headCollectedB, $targetCollected)
} elseif ($resB.failed -eq 0 -and $resB.errors -gt 0) {
    $resB["actual"] = "error_red"
    Write-Host ("[drill][弱信号] 场景 B 只有 {0} 个 error、0 failed：回滚点代码收集期就挂，别退" -f $resB.errors) -ForegroundColor Yellow
} else {
    $resB["actual"] = "red"
    # 红了必须分清是「回滚点真不健康」还是「脚本没把 tests 还原干净」：
    # 用上面已算好的逐文件比对结果（多余/内容不同/缺失三种）
    $mismatch = $testsMismatch
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
# A 的失败归因三分 + A2 差集（证据要等 B 跑完才齐，所以放在报告段）
# --------------------------------------------------------------------------
# 「B 同 node id 通过」= 该 node id 在 B 场景被收集到且没失败
$bFailedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
foreach ($nid in @($resB.failed_ids)) { [void]$bFailedSet.Add([string]$nid) }
$bPassedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
foreach ($nid in $targetIds) { if (-not $bFailedSet.Contains([string]$nid)) { [void]$bPassedSet.Add([string]$nid) } }

$buckets = Get-FailureBuckets $resA.failed_detail $bPassedSet
$resA["classify_rule"] = "只认三条证据：(a) B 场景同 node id 通过；(b) 失败原因是 AssertionError 或业务异常（不是夹具/收集/导入层故障）；(c) 接口缺失：AttributeError / TypeError 且被测属性/方法在回滚点不存在——类型 c 为本轮新增、待确认，不并入 b 的计数。测试文件名与漂移模块对得上不给分"
$resA["confirmed_expected_count"] = @($buckets.confirmed_expected).Count
$resA["unconfirmed_count"] = @($buckets.unconfirmed).Count
$resA["real_unexpected_count"] = @($buckets.real_unexpected).Count
$resA["confirmed_expected"] = @($buckets.confirmed_expected)
$resA["unconfirmed"] = @($buckets.unconfirmed)
$resA["real_unexpected"] = @($buckets.real_unexpected)

# 证据构成逐类计数：每条 confirmed_expected 的证据类型分别报条数；
# (c) 接口缺失单独成类，不混进 (b)——上一版把它并进 (b) 报成「确认预期 19」，
# 口径被擅自扩展，这次拆开报，summary 标「本轮新增、待确认」
$resA["evidence_a_count"] = @($buckets.confirmed_expected | Where-Object { @($_.evidence) -contains "证据 (a)：B 场景同 node id 通过" }).Count
$resA["evidence_b_count"] = @($buckets.confirmed_expected | Where-Object { $_.reason_class -eq "assertion" -or $_.reason_class -eq "business" }).Count
$resA["evidence_c_count"] = @($buckets.confirmed_expected | Where-Object { $_.reason_class -eq "api_missing" }).Count
$resA["evidence_assertion_count"] = @($buckets.confirmed_expected | Where-Object { $_.reason_class -eq "assertion" }).Count
$resA["evidence_business_count"] = @($buckets.confirmed_expected | Where-Object { $_.reason_class -eq "business" }).Count
$resA["evidence_api_missing_count"] = @($buckets.confirmed_expected | Where-Object { $_.reason_class -eq "api_missing" }).Count
$resA["evidence_type_note"] = "证据类型 c（接口缺失）为本轮新增，待确认；不并入 b 的计数"

# 接口缺失的条目为什么没有证据 (a)：多半因为测试文件本身就是回滚点之后新增的，
# B 场景里根本没有这些用例。这里逐条标注，让「B 无对照」有据可查。
$prevEapLt = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { $targetTestTree = @((& git -C $RepoRoot ls-tree -r --name-only $RollbackTarget -- tests 2>&1) | Where-Object { $_ -like "tests/*" }) } finally { $ErrorActionPreference = $prevEapLt }
$targetTestSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
foreach ($p in $targetTestTree) { [void]$targetTestSet.Add([string]$p) }
$missingAtTarget = 0
foreach ($c in @($resA.confirmed_expected)) {
    $tf = ([string]$c.node_id -split '::')[0]
    $atTarget = $targetTestSet.Contains($tf)
    $c["test_file_at_target"] = $atTarget
    if (-not $atTarget) { $missingAtTarget++ }
}
$resA["failed_test_file_absent_at_target_count"] = $missingAtTarget

# A2 与 A 的失败集合差：只 A 有 = 测试基建回退带来的基建噪声；只 A2 有 = 场景自相矛盾。
# 仅在 A2 适用（基建文件内容真变）时才有信息量；applicable=false 时不算差集，
# 更不得出现「基建噪声 0 条」的表述（空对照推不出任何结论）
$aFailSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
foreach ($nid in $failedIdsA) { [void]$aFailSet.Add([string]$nid) }
if ($a2Applicable) {
    $a2FailSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($nid in @($resA2.failed_ids)) { [void]$a2FailSet.Add([string]$nid) }
    $onlyInA = @($failedIdsA | Where-Object { -not $a2FailSet.Contains([string]$_) })
    $onlyInA2 = @($resA2.failed_ids | Where-Object { -not $aFailSet.Contains([string]$_) })
    $resA2["only_in_a"] = $onlyInA          # 由测试基建回退造成 = 基建噪声
    $resA2["only_in_a2"] = $onlyInA2        # 不该出现，出现就说明两场景判定不一致
    $resA2["infra_noise_count"] = $onlyInA.Count
    # 未确认项若在 A2 里消失，说明失败只因测试基建回退（基建噪声），不是代码行为证据
    foreach ($u in @($resA.unconfirmed)) {
        if ($a2FailSet.Contains([string]$u.node_id)) { $u["a2"] = "A2 仍失败" }
        else { $u["a2"] = "A2 复跑转为通过（测试基建回退造成的基建噪声）" }
    }
} else {
    $onlyInA = $null
    $onlyInA2 = $null
}

if (@($buckets.real_unexpected).Count -gt 0) {
    Write-Host ("[drill][真意外] 场景 A 有 {0} 条失败是夹具/收集/导入层故障，与代码行为无关，需单独立项排查：{1}" -f @($buckets.real_unexpected).Count, ((@($buckets.real_unexpected) | ForEach-Object { $_.node_id }) -join ", ")) -ForegroundColor Yellow
}
if (@($buckets.unconfirmed).Count -gt 0) {
    Write-Host ("[drill][未确认] 场景 A 有 {0} 条失败两条证据都不满足，只标未确认，不硬判：{1}" -f @($buckets.unconfirmed).Count, ((@($buckets.unconfirmed) | ForEach-Object { $_.node_id }) -join ", ")) -ForegroundColor Yellow
}

# --------------------------------------------------------------------------
# 结果落盘
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$verdict = @()
switch ($resA.alarm) {
    "ok"        { $verdict += ("A 红，报警器有效（{0} 条失败：确认预期 {1} / 未确认 {2} / 真意外 {3}；其中证据 (c) 接口缺失 {4} 条，类型 c 为本轮新增、待确认，不并入 b）" -f $failedIdsA.Count, $resA.confirmed_expected_count, $resA.unconfirmed_count, $resA.real_unexpected_count, $resA.evidence_c_count) }
    "weak"      { $verdict += "A 只有收集期报错，算红但信号偏弱" }
    "no_drift"  { $verdict += "A 绿，但代码与回滚点零差异，本轮无法验证报警器" }
    "BLIND"     { $verdict += "A 绿且代码确有差异，报警器失效" }
}
if ($a2Applicable) {
    if ($onlyInA.Count -eq 0 -and $onlyInA2.Count -eq 0) {
        $verdict += ("A2 与 A 失败集合完全一致（{0} 条），测试基建内容确有变化而失败集合未受影响，基建噪声 0 条（对照有效）" -f $a2FailSet.Count)
    } else {
        $verdict += ("A2 与 A 失败集合差：仅 A 有 {0} 条（测试基建回退的基建噪声）、仅 A2 有 {1} 条（场景不一致，待查）" -f $onlyInA.Count, $onlyInA2.Count)
    }
} else {
    $verdict += "A2 本轮不适用：被替换的测试基建文件（conftest.py / pyproject.toml / metadata.yaml）与回滚点内容相同，A 与 A2 的对照为空，不据此下基建噪声结论"
}
if ($bGreen) { $verdict += ("B 绿，回滚点健康，净丢失 {0} 条用例" -f $lost) }
elseif ($resB.actual -eq "error_red") { $verdict += "B 收集期就报错，回滚点不可用" }
elseif ($resB.red_cause -eq "script_incomplete") { $verdict += "B 红，但 tests 未还原干净（脚本问题），不作为回滚点结论" }
else { $verdict += "B 红，tests 与回滚点一致，回滚点不健康" }

$jsonPath = Join-Path $OutDir "drill_result.json"

# 演练脚本自身的未提交改动 diff 落盘：上一轮交付声称完整 diff 在 .temp/drill-diff-v11.patch，
# 但从未生成（三处 .temp 与全盘搜索均为零结果），被验收打回。这次真写进产物目录：
# git diff HEAD -- tools/rollback_drill.ps1，报告里报路径 + 大小，路径必须真实存在。
# git 输出走子进程重定向直接写临时文件，保证 UTF-8 字节原样落盘、stderr 不污染正文。
$patchPath = Join-Path $OutDir "drill_diff.patch"
$tmpPatch = "$patchPath.tmp"
$tmpErr = Join-Path $env:TEMP ("drill_patch_err_" + [guid]::NewGuid().ToString("N") + ".txt")
$patchProc = Start-Process -FilePath "git" -ArgumentList @("diff", "HEAD", "--", "tools/rollback_drill.ps1") -WorkingDirectory $RepoRoot -RedirectStandardOutput $tmpPatch -RedirectStandardError $tmpErr -NoNewWindow -Wait -PassThru
$patchErrText = ""
if (Test-Path -LiteralPath $tmpErr) { $patchErrText = [System.IO.File]::ReadAllText($tmpErr); Remove-Item -Force -LiteralPath $tmpErr -ErrorAction SilentlyContinue }
if ($null -eq $patchProc -or $patchProc.ExitCode -ne 0) {
    throw ("生成 drill_diff.patch 失败：git diff 退出码 {0}，stderr：{1}" -f $patchProc.ExitCode, $patchErrText)
}
Move-Item -Force -LiteralPath $tmpPatch -Destination $patchPath
$patchSize = (Get-Item -LiteralPath $patchPath).Length
$patchGenerated = ($patchSize -gt 0)
if ($patchGenerated) { Write-Step ("已写 {0}（{1} 字节，git diff HEAD -- tools/rollback_drill.ps1）" -f $patchPath, $patchSize) }
else { Write-Step "drill_diff.patch 为空：演练脚本相对 HEAD 无未提交改动" }

$result = [ordered]@{
    dry_run             = $false
    generated_at        = (Get-Date).ToString("s")
    script_sha256       = $scriptSha
    repo                = $RepoRoot
    head                = $head
    rollback_target     = $RollbackTarget
    backup_dir          = $BackupDir
    python              = $Python
    python_version      = $pyVersion
    pytest_version      = $pytestVersion
    workdir_dirty       = [bool]$dirty
    workdir_dirty_text  = $dirty
    code_files          = $CodeFiles
    code_files_a2       = $CodeFilesA2
    scenario_a          = $resA
    scenario_a2         = $resA2
    scenario_b          = $resB
    script_patch        = [ordered]@{
        file        = "tools/drill_out/drill_diff.patch"
        generated   = $patchGenerated
        size_bytes  = $patchSize
        rule        = "git diff HEAD -- tools/rollback_drill.ps1（演练脚本相对 HEAD 的未提交改动，实跑时生成；generated=false 表示 diff 为空）"
    }
    verdict             = ($verdict -join "；")
}
Write-Utf8NoBomChecked $jsonPath ($result | ConvertTo-Json -Depth 8)

# md 各行用 -f 领进中间变量（数组字面量里 "串" + $变量 的尾逗号会被 PS
# 的高优先级逗号运算符拆成多个数组元素——v10 起的存量 bug，md 值全部断行；
# -f 与圆括号表达式都不受影响，顺手修掉）
function Format-Baseline($b) {
    if ($null -eq $b) { return "未跑" }
    return ('{0} passed / {1} failed / {2} errors（collected {3}）' -f $b.passed, $b.failed, $b.errors, $b.collected)
}
$driftParts = @()
foreach ($d in $driftStatA) {
    if ($d["note"]) { $driftParts += ('{0}（{1}）' -f $d["file"], $d["note"]) }
    else { $driftParts += ('{0} +{1}/-{2}' -f $d["file"], $d["add"], $d["del"]) }
}
$driftStatStr = ($driftParts -join "，")
if ([string]::IsNullOrWhiteSpace($driftStatStr)) { $driftStatStr = "无（逐字节相同）" }
$lostTopParts = @()
foreach ($k in @($lostByFile.Keys | Select-Object -First 5)) {
    $lostTopParts += ('{0} {1}' -f $k, $lostByFile[$k])
}
$lostTopStr = ($lostTopParts -join "，")
if ([string]::IsNullOrWhiteSpace($lostTopStr)) { $lostTopStr = "无" }

$treeDiff = $testsTreeDiff
$addedList = @($treeDiff.added)
$modifiedList = @($treeDiff.content_modified)
$missingList = @($treeDiff.missing_at_head)

# 产物清单按 A2 是否实际执行渲染：A2 跳过时其日志已清理，不能在清单里列不存在的文件
if ($a2Applicable) {
    $pyLogsLine = "A_baseline_head.log、drill_A_pytest.log、A2_baseline_head.log、drill_A2_pytest.log、B_baseline_head.log、drill_B_pytest.log"
    $failedFilesLine = "drill_A_failed.txt、drill_A2_failed.txt、drill_B_failed.txt"
} else {
    $pyLogsLine = "A_baseline_head.log、drill_A_pytest.log、B_baseline_head.log、drill_B_pytest.log（A2 未跑，无 A2 日志）"
    $failedFilesLine = "drill_A_failed.txt、drill_B_failed.txt（A2 未跑，无 A2 失败明细）"
}

$L_gen = ('- 生成时间：{0}' -f $result.generated_at)
$L_sha = ('- 脚本 SHA256：{0}' -f $scriptSha)
$L_dirty = ('- 工作树：{0}' -f $(if ($dirty) { "不干净（$dirty）" } else { "干净" }))
$L_head = ('- HEAD：{0}' -f $head)
$L_rb = ('- 回滚点：{0}' -f $RollbackTarget)
$L_py = ('- 解释器：{0}（Python {1}，{2}）' -f $Python, $pyVersion, $pytestVersion)
$L_a0 = ('- 副本基线：{0} —— 基线不绿则下面的红不可归因' -f (Format-Baseline $baseA))
$L_a1 = ('- 期望：红；实际：{0}' -f $resA.actual)
$L_a2 = ('- 退出码 {0}，passed {1}，failed {2}，errors {3}，耗时 {4}s' -f $resA.exit_code, $resA.passed, $resA.failed, $resA.errors, $resA.seconds)
$L_a3 = ('- 代码 drift（SHA256 归一化后真变）：{0}' -f $(if (@($driftA.changed).Count -eq 0) { "无" } else { @($driftA.changed) -join ", " }))
$L_a4 = ('- 不计入 drift 的行尾/编码差异：{0}' -f $(if (@($driftA.eol_only).Count -eq 0) { "无" } else { (@($driftA.eol_only) -join ", ") + "（内容逐字相同）" }))
$L_a5 = ('- drift 分量（回滚点 -> 当前）：{0}' -f $driftStatStr)
$L_a6 = ('- 失败三分：确认预期 {0} / 未确认 {1} / 真意外 {2}（共 {3} 条，明细见 drill_A_failed.txt）' -f $resA.confirmed_expected_count, $resA.unconfirmed_count, $resA.real_unexpected_count, $failedIdsA.Count)
$L_a7 = ('- 判断链条：{0} 条失败逐条对照证据——(a) B 场景同 node id 通过 {1} 条；(b) 失败在断言/业务调用层：断言 {2} 条、业务异常 {3} 条；(c) 接口缺失（AttributeError / TypeError，被测属性/方法在回滚点不存在）{4} 条——证据类型 c 为本轮新增，待确认，不并入 b。两条都不满足的 {5} 条标未确认，其中夹具/收集/导入层故障 {6} 条记真意外。确认预期里有 {7} 条的测试文件在回滚点不存在（v6 新增用例），故 (a) 结构上无从对照；文件名与漂移模块对得上不作为证据' -f $failedIdsA.Count, $resA.evidence_a_count, $resA.evidence_assertion_count, $resA.evidence_business_count, $resA.evidence_c_count, $resA.unconfirmed_count, $resA.real_unexpected_count, $missingAtTarget)
if ($resA.real_unexpected_count -gt 0) {
    $L_a8 = ('- 真意外清单（需单独立项，本脚本不改代码不改测试）：{0}' -f ((@($resA.real_unexpected) | ForEach-Object { $_.node_id + "（" + $_.exc_type + "）" }) -join "；"))
} else {
    $L_a8 = '- 真意外清单：无'
}
$L_a8b = ('- 未确认清单：{0}' -f $(if ($resA.unconfirmed_count -gt 0) { (@($resA.unconfirmed) | ForEach-Object { $_.node_id + "（" + $_.exc_type + "）" }) -join "；" } else { "无" }))
$L_a9 = ('- 报警器：{0}' -f $resA.alarm)
if ($a2Applicable) {
    $L_a2_0 = '- applicable：true（触发条件成立：测试基建文件内容真变，对照有效）'
    $L_a2_0b = ('- 触发依据：{0}' -f $resA2.applicable_basis)
    $L_a2_0c = ('- 副本基线：{0}' -f (Format-Baseline $baseA2))
    $L_a2_1 = ('- 回退范围：{0}；留在 HEAD：{1}' -f (@($CodeFilesA2) -join ", "), (($CodeFiles | Where-Object { $CodeFilesA2 -notcontains $_ }) -join ", "))
    $L_a2_2 = ('- 期望：红；实际：{0}；退出码 {1}，passed {2}，failed {3}，errors {4}' -f $resA2.actual, $resA2.exit_code, $resA2.passed, $resA2.failed, $resA2.errors)
    $L_a2_3 = ('- 与 A 的失败集合差：仅 A 有 {0} 条（测试基建回退的基建噪声）、仅 A2 有 {1} 条（场景不一致）' -f $onlyInA.Count, $onlyInA2.Count)
    $L_a2_4 = ('- 归因：{0}' -f $(if ($onlyInA.Count -eq 0 -and $onlyInA2.Count -eq 0) { "A 的红不依赖测试基建回退，失败集合在去掉基建回退后原样保留（对照有效）" } elseif ($onlyInA2.Count -eq 0) { "A 里有 $($onlyInA.Count) 条失败只因测试基建跟着回退，属基建噪声，不能当代码回退的证据" } else { "两场景失败集合互有出入，判定逻辑需复核，先别采信 A 的结论" }))
} else {
    $L_a2_0 = '- applicable：false（A2 本轮不适用，未执行）'
    $L_a2_0b = '- 原因：被替换的测试基建文件与回滚点内容相同（SHA256 归一化后 conftest.py / pyproject.toml / metadata.yaml 均无内容差异），A 与 A2 的失败集合对照为空'
    $L_a2_0c = '- 副本基线：不适用（A2 未跑）'
    $L_a2_1 = ('- 测试基建内容变化：无；仅行尾/编码差异（内容逐字相同）：{0}' -f $(if (@($infraEolOnly).Count -eq 0) { "无" } else { @($infraEolOnly) -join ", " }))
    $L_a2_2 = '- 结论口径：此条件下 A 与 A2 差集 0 是必然，推不出「基建噪声 0 条」，verdict 已禁止该表述'
    $L_a2_3 = '- 与 A 的失败集合差：不适用（A2 未跑，无对照）'
    $L_a2_4 = '- 归因：不基于空对照下结论'
}
$L_b0 = ('- 副本基线：{0} —— 基线不绿则 B 的绿不可信' -f (Format-Baseline $baseB))
$L_b1 = ('- 期望：绿；实际：{0}' -f $resB.actual)
$L_b2 = ('- 退出码 {0}，passed {1}，failed {2}，errors {3}，耗时 {4}s' -f $resB.exit_code, $resB.passed, $resB.failed, $resB.errors, $resB.seconds)
$L_b3 = ('- 用例数：{0} -> {1}，净丢失 {2} 条（node id 集合差）' -f $headCollectedB, $targetCollected, $lost)
$L_b4 = ('- tests 文件 {0} -> {1}：删除回滚点之后新增 {2} 个、内容回滚 {3} 个、补回当前缺失 {4} 个（口径：git diff --name-status {5} -- tests）' -f $testRestore.files_before, $testRestore.files_after, $addedList.Count, $modifiedList.Count, $missingList.Count, $RollbackTarget)
$L_b5 = ('- 内容被回滚的同名文件：{0}' -f $(if ($modifiedList.Count -eq 0) { "无" } else { $modifiedList -join ", " }))
$L_b6 = ('- 还原校验：副本 tests 与回滚点逐文件{0}' -f $(if ($resB.tests_tree_matches_target) { "一致" } else { "不一致（{0} 处，见 json tests_mismatch_vs_target）" -f $testsMismatch.Count }))
$L_b7 = ('- 净丢失明细：drill_B_lost_cases.txt 全量落盘；Top5 文件：{0}' -f $lostTopStr)

$md = @(
    "# 回滚演练摘要",
    "",
    $L_gen,
    $L_sha,
    $L_dirty,
    $L_head,
    $L_rb,
    $L_py,
    "",
    "## 场景 A（只覆盖代码，tests 留 HEAD）",
    $L_a0,
    $L_a1,
    $L_a2,
    $L_a3,
    $L_a4,
    $L_a5,
    $L_a6,
    $L_a7,
    $L_a8,
    $L_a8b,
    $L_a9,
    "",
    "## 场景 A2（只回退 ledger.py + main.py，测试基建留 HEAD；条件触发）",
    $L_a2_0,
    $L_a2_0b,
    $L_a2_0c,
    $L_a2_1,
    $L_a2_2,
    $L_a2_3,
    $L_a2_4,
    "",
    "## 场景 B（全量覆盖）",
    $L_b0,
    $L_b1,
    $L_b2,
    $L_b3,
    $L_b4,
    $L_b5,
    $L_b6,
    $L_b7,
    "- 失败明细：drill_B_failed.txt（B 绿时为空文件）",
    "",
    "## 结论",
    ($verdict -join "；"),
    "",
    "## 产物（tools\drill_out\，每次实跑整体重写）",
    "- 汇总：drill_result.json、drill_summary.md",
    ('- pytest 完整输出：{0}' -f $pyLogsLine),
    ('- 失败明细：{0}' -f $failedFilesLine),
    "- 净丢失全量：drill_B_lost_cases.txt",
    ('- 脚本未提交改动 diff：drill_diff.patch（{0} 字节，git diff HEAD -- tools/rollback_drill.ps1）' -f $patchSize),
    "- 覆盖率日志：coverage.log（由 tools\coverage_check.ps1 单独实跑产出，原始 stdout）"
)
$mdPath = Join-Path $OutDir "drill_summary.md"
Write-Utf8NoBomChecked $mdPath (($md -join "`r`n") + "`r`n")

Write-Step "已写 $jsonPath"
Write-Step "已写 $mdPath"
Write-Step ("结论：" + ($verdict -join "；"))
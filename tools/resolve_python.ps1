# -*- coding: utf-8 -*-
<#
公共解释器探测器：只定义函数，不做任何副作用，供 dot-source 使用。

  . "$PSScriptRoot\resolve_python.ps1"
  $py = Resolve-PythonExe -RequireModules @("coverage")
  if (-not $py) { throw "..." }

为什么单独抽出来：rollback_drill.ps1 与 coverage_check.ps1 都需要一个
「装了某个模块的 python」，曾经两个脚本各写一份硬编码路径，机器一换就废。
现在只有一个实现，两边共用。

候选顺序（先认环境变量/仓库自带，再认 PATH，少猜）：
  1) 环境变量 PYTHON
  2) $RepoRoot\.venv\Scripts\python.exe（如果给了 -RepoRoot）
  3) PATH 里的 python
  4) PATH 里的 python3
逐个真跑一次校验，第一个通过的就用；全不通过返回 $null，
由调用方显式报错（绝不静默降级到「随便找个 python」）。
#>

function Test-PythonModule {
    <#
    这个解释器能不能跑 `-m <Module> --version`。
    路径不存在、启动失败、返回码非 0，一律算不能。
    Module 传 $null 时只验证解释器本身能启动（python -V）。
    #>
    param(
        [string]$Exe,
        [string]$Module = "coverage"
    )
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        if ([string]::IsNullOrWhiteSpace($Module)) {
            $out = & $Exe -V 2>&1
            return ($LASTEXITCODE -eq 0)
        }
        $out = & $Exe -m $Module --version 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        return -not [string]::IsNullOrWhiteSpace(($out | Out-String))
    } catch {
        return $false
    }
}

function Get-PythonCandidates {
    <#
    按固定顺序列出候选绝对路径，去重，不做校验。
    #>
    param([string]$RepoRoot)

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $list = New-Object System.Collections.Generic.List[string]

    $add = {
        param([string]$p)
        if (-not [string]::IsNullOrWhiteSpace($p) -and $seen.Add($p)) { $list.Add($p) }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:PYTHON)) { & $add $env:PYTHON }
    if (-not [string]::IsNullOrWhiteSpace($RepoRoot)) {
        & $add (Join-Path $RepoRoot ".venv\Scripts\python.exe")
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { & $add $cmd.Source }
    }
    return $list
}

function Resolve-PythonExe {
    <#
    返回第一个满足 RequireModules 的候选绝对路径；没有则返回 $null。
    -RequireModules @("coverage")        需要 coverage
    -RequireModules @("coverage","pytest") 两个都要
    -RequireModules @()                  只要有 python 就行
    #>
    param(
        [string[]]$RequireModules = @("coverage"),
        [string]$RepoRoot
    )

    foreach ($exe in (Get-PythonCandidates -RepoRoot $RepoRoot)) {
        $ok = $true
        if ($RequireModules.Count -eq 0) {
            $ok = Test-PythonModule -Exe $exe -Module $null
        } else {
            foreach ($m in $RequireModules) {
                if (-not (Test-PythonModule -Exe $exe -Module $m)) { $ok = $false; break }
            }
        }
        if ($ok) { return (Resolve-Path -LiteralPath $exe).Path }
    }
    return $null
}

function Resolve-PythonExeOrThrow {
    <#
    与 Resolve-PythonExe 相同，但找不到时直接抛出可读错误。
    -Hint 用于告诉用户怎么手动指定。
    #>
    param(
        [string[]]$RequireModules = @("coverage"),
        [string]$RepoRoot,
        [string]$Hint = ""
    )
    $py = Resolve-PythonExe -RequireModules $RequireModules -RepoRoot $RepoRoot
    if ($py) { return $py }
    $mods = if ($RequireModules.Count -gt 0) { $RequireModules -join ", " } else { "python 本身" }
    $hintText = if ($Hint) { " $Hint" } else { "" }
    throw ("没找到能 import {0} 的 python（试过 PYTHON 环境变量、$RepoRoot\.venv、PATH 里的 python/python3）。{1}" -f $mods, $hintText)
}
# -*- coding: utf-8 -*-
<#
覆盖率门禁：只统计 ledger.py / main.py / conftest.py，行 + 分支双 100%。

口径：
  --branch                 分支覆盖也算，不只看行
  --source=.               只收本仓库的代码
  --include=...            只报这三个文件；tests/ 自身不计入
  report --fail-under=100  低于 100 就非 0 退出

为什么不用 pytest-cov：它不是必需的，装了 coverage 就能跑这套命令；
  所以这里要的是一个「装了 coverage 的 python」，谁装都行，脚本自己找：
    1) 环境变量 PYTHON
    2) PATH 里的 python
    3) PATH 里的 python3
  逐个用 -m coverage --version 验证，第一个能跑的就用；全失败才报错。

用法：
  .\tools\coverage_check.ps1
  .\tools\coverage_check.ps1 -Python "C:\path\to\python.exe"   # 显式指定，跳过探测
  .\tools\coverage_check.ps1 -KeepData     # 保留 .coverage，方便自己再跑 report -m
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Python,
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

function Test-CoveragePython {
    <#
    这个解释器能不能跑 `-m coverage --version`。
    路径不存在、启动失败、返回码非 0、输出里没有 Coverage.py，一律算不能。
    #>
    param([string]$Exe)
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        $out = & $Exe -m coverage --version 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        return (($out | Out-String) -match "Coverage\.py")
    } catch {
        return $false
    }
}

function Resolve-PythonExe {
    <#
    先认命令行名字（python / python3）成绝对路径，再交给 Test-CoveragePython 验。
    返回第一个能跑 coverage 的绝对路径；一个都没有返回 $null。
    #>
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHON)) { $candidates.Add($env:PYTHON) }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { $candidates.Add($cmd.Source) }
    }

    foreach ($exe in $candidates) {
        if (Test-CoveragePython -Exe $exe) { return $exe }
    }
    return $null
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Resolve-PythonExe
    if (-not $Python) {
        throw "没找到装了 coverage 的 python（试过 PYTHON 环境变量、python、python3）。用 -Python 指定解释器路径。"
    }
    Write-Host "[coverage_check] 使用解释器：$Python"
} elseif (-not (Test-CoveragePython -Exe $Python)) {
    throw "指定的 python 跑不了 -m coverage --version：$Python"
}

$env:PYTHONIOENCODING = "utf-8"
$dataFile = Join-Path $RepoRoot ".coverage"
if (-not $KeepData -and (Test-Path $dataFile)) { Remove-Item $dataFile -Force }

Push-Location $RepoRoot
try {
    & $Python -m coverage run --branch --source=. -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[coverage_check] 测试没全绿，先修测试再谈覆盖率"
        exit $LASTEXITCODE
    }

    & $Python -m coverage report --include="ledger.py,main.py,conftest.py" --fail-under=100
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        Write-Host "[coverage_check] 行 + 分支 100%，过"
    } else {
        Write-Host "[coverage_check] 未到 100%（退出码 $code）：用 coverage report -m 看缺哪条分支"
    }
    exit $code
}
finally {
    Pop-Location
}
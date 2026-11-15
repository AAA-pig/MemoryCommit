# -*- coding: utf-8 -*-
<#
覆盖率门禁：统计 ledger.py / main.py 的行 + 分支双 100%，conftest.py 单独标注。

口径：
  --branch                 分支覆盖也算，不只看行
  --source=.               只收本仓库的代码
  --include=...            只报统计范围内的文件；tests/ 自身不计入
  report --fail-under=100  低于 100 就非 0 退出

为什么 conftest.py 单独拿出来标注（而不是并进「生产代码三大件」）：
  conftest.py 是 pytest 的测试夹具/环境钩子，不是随插件发布的生产逻辑。
  把它算进同一个 100% 总账，只会让「生产代码覆盖率」这个数字好看，
  却掩盖了真正需要盯的 ledger.py / main.py。所以：
    - 生产门禁 = ledger.py + main.py，--fail-under=100，非 0 直接失败；
    - conftest.py 单独跑一次 report，只展示数字（附 CONFTEST_LINE 字样），
      不计入门禁、不影响退出码。它的行为仍由测试全绿兜底。
  需要老的「三件套一起卡 100」行为时加 -IncludeConftest。

为什么不用 pytest-cov：它不是必需的，装了 coverage 就能跑这套命令；
  所以这里要的是一个「装了 coverage 的 python」。
  探测逻辑在 tools/resolve_python.ps1，与 rollback_drill.ps1 共用：
    1) 环境变量 PYTHON
    2) $RepoRoot\.venv\Scripts\python.exe
    3) PATH 里的 python
    4) PATH 里的 python3
  逐个用 -m coverage --version 验证，第一个能跑的就用；全失败才报错。

用法：
  .\tools\coverage_check.ps1
  .\tools\coverage_check.ps1 -Python "C:\path\to\python.exe"   # 显式指定，跳过探测
  .\tools\coverage_check.ps1 -KeepData     # 保留 .coverage，方便自己再跑 report -m
  .\tools\coverage_check.ps1 -IncludeConftest   # conftest 也计入 100% 门禁
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Python,
    [switch]$KeepData,
    [switch]$IncludeConftest
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

. (Join-Path $PSScriptRoot "resolve_python.ps1")

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Resolve-PythonExeOrThrow -RequireModules @("coverage") -RepoRoot $RepoRoot -Hint "用 -Python 指定解释器路径。"
    Write-Host "[coverage_check] 使用解释器：$Python"
} elseif (-not (Test-PythonModule -Exe $Python -Module "coverage")) {
    throw "指定的 python 跑不了 -m coverage --version：$Python"
}

$env:PYTHONIOENCODING = "utf-8"
$dataFile = Join-Path $RepoRoot ".coverage"
if (-not $KeepData -and (Test-Path $dataFile)) { Remove-Item $dataFile -Force }

# 生产门禁文件：conftest.py 是测试夹具，默认不进总账
# 2026-11-15：persona_text.py（手写人格纯文本解析）并入生产门禁——
# 它是随插件发布的解析逻辑，不是测试夹具，解析错一行就会写进错误的人格。
$gateFiles = @("ledger.py", "main.py", "persona_text.py")
if ($IncludeConftest) { $gateFiles += "conftest.py" }
$gateInclude = ($gateFiles -join ",")

Push-Location $RepoRoot
try {
    & $Python -m coverage run --branch --source=. -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[coverage_check] 测试没全绿，先修测试再谈覆盖率"
        exit $LASTEXITCODE
    }

    Write-Host "[coverage_check] 生产代码门禁：$gateInclude"
    & $Python -m coverage report --include="$gateInclude" --fail-under=100
    $code = $LASTEXITCODE

    if (-not $IncludeConftest) {
        # conftest.py 单独展示，只做标注不影响退出码
        Write-Host "[coverage_check] 参考（不计入门禁）：conftest.py"
        & $Python -m coverage report --include="conftest.py"
    }

    if ($code -eq 0) {
        Write-Host "[coverage_check] $gateInclude 行 + 分支 100%，过"
    } else {
        Write-Host "[coverage_check] $gateInclude 未到 100%（退出码 $code）：用 coverage report -m 看缺哪条分支"
    }
    exit $code
}
finally {
    Pop-Location
}
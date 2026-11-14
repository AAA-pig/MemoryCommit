# clone 之后跑一次，把仓库里的 .githooks 接上。
#
# git 出于安全考虑，不会执行 clone 下来的仓库里的任何东西，
# 所以「自动启用」在 git 里不存在，只能手动跑一次这个脚本。
#
# 用法（仓库根目录，PowerShell）：
#   powershell -ExecutionPolicy Bypass -File tools/setup-hooks.ps1
#
# 本文件必须存成 UTF-8 with BOM：Windows PowerShell 5.1 读无 BOM 的
# UTF-8 脚本会按系统 ANSI 解码，中文全乱，甚至报「字符串缺少终止符」。

$ErrorActionPreference = "Stop"

git config core.hooksPath .githooks

$path = git config core.hooksPath
Write-Host "已启用提交拦截：core.hooksPath = $path"
Write-Host "暂存了 .db / .jsonl / data/ / 记忆账本 的文件会被拒绝提交。"
Write-Host "确实要放行：git commit --no-verify"

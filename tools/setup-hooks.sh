#!/bin/sh
# clone 之后跑一次，把仓库里的 .githooks 接上。
#
# git 出于安全考虑，不会执行 clone 下来的仓库里的任何东西，
# 所以「自动启用」在 git 里不存在，只能手动跑一次这个脚本。
#
# 用法（仓库根目录）：
#   sh tools/setup-hooks.sh

set -e

git config core.hooksPath .githooks

echo "已启用提交拦截：core.hooksPath = $(git config core.hooksPath)"
echo "暂存了 .db / .jsonl / data/ / 记忆账本 的文件会被拒绝提交。"
echo "确实要放行：git commit --no-verify"

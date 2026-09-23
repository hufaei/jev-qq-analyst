#!/bin/zsh
# 启动 jev-qq-analyst 悬浮窗（保留旧配置与日志路径）
cd "$(dirname "$0")" || exit 1
# uv installs to ~/.local/bin; a Finder-launched .command does not inherit a login shell
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# hud.py reads only Decision Infra settings from the existing config file.

LOG="$HOME/Library/Logs/jev-jarvis.log"
mkdir -p "$(dirname "$LOG")" || exit 1
source ./packaging/bootstrap_uv.sh || exit 1
if ! jev_check_arch; then
    print -r -- "$JEV_ARCH_ERROR"
    print -r -- "详情：$LOG"
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    print "未找到 uv，正在自动安装；进度日志：$LOG"
fi
if ! jev_ensure_uv "$LOG"; then
    print -r -- "$JEV_UV_ERROR"
    print -r -- "详情：$LOG。也可手动运行 brew install uv 后重试。"
    exit 1
fi

exec uv run python src/hud.py

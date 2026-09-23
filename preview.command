#!/bin/zsh
# Double-click to inspect the HUD with synthetic text; no QQ access or model request.
cd "$(dirname "$0")" || exit 1
export JEV_VISUAL_DEMO=1
exec ./start.command

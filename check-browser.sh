#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
task_demo_root="$PWD"
export LD_LIBRARY_PATH="$task_demo_root/.qa-libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="$task_demo_root/.qa-cache/fonts.conf"
export XDG_CACHE_HOME="$task_demo_root/.qa-cache"
export ASCEND_LAB_BROWSER_PATH="$task_demo_root/.browsers/qa-chromium-1169/chrome-linux/headless_shell"
export PLAYWRIGHT_BROWSERS_PATH="$task_demo_root/.browsers"
task_browser_check="${1:-tests/browser_check.py}"
if [ "$#" -gt 0 ]; then shift; fi
exec "$task_demo_root/.qa-venv/bin/python" "$task_browser_check" "$@"

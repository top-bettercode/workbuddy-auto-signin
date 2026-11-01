#!/bin/bash
# WorkBuddy 每日自动化：签到 + 成长中心，外加用 CodeBuddy 完成一次对话（hi）以完成每日登录/成长任务。
# 由 launchd（workbuddy-auto-signin）每日 00:05、每 4 小时、登录时调用。
# 注意：本任务会多次触发（每天 00:05 + 每 4 小时 + 登录时），但「对话」只做一次/天，避免浪费配额。

set -u

REPO="/Users/peter/data/repositories/workbuddy-auto-signin"
PYTHON="/Users/peter/.pyenv/versions/3.14.7/bin/python3"
CODEBUDDY="/Users/peter/.nvm/versions/node/v22.23.3/bin/codebuddy"
export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
CHAT_MARKER="/tmp/workbuddy-codebuddy-chat-${TODAY}.done"

date "+[%Y-%m-%d %H:%M:%S] [daily] start"

# 1) 核心：WorkBuddy 每日签到 + 成长中心（连登补登/兑换、Buddy 旅行礼物/派出、任务、盲盒、能量）
"$PYTHON" "$REPO/signin.py" auto
rc=$?
echo "[daily] signin auto exit=$rc"

# 2) 用 CodeBuddy 完成一次对话（内容 hi），完成「每日登录/成长」里的对话任务。
#    依赖 CodeBuddy CLI 已登录（凭据持久化在 ~/.codebuddy，本机已登录）。
#    未登录/失败不影响上面的签到；此步不致命。每天只做一次（CHAT_MARKER 跨当天多次触发去重）。
if [ -x "$CODEBUDDY" ]; then
  if [ -f "$CHAT_MARKER" ]; then
    echo "[daily] codebuddy 今天已完成对话，跳过"
  else
    echo "[daily] codebuddy conversation: hi"
    timeout 150 "$CODEBUDDY" -p 'hi' --tools '' 2>&1 | head -40
    echo "[daily] codebuddy exit=${PIPESTATUS[0]}"
    : > "$CHAT_MARKER"
  fi
else
  echo "[daily] codebuddy CLI 未找到（$CODEBUDDY），跳过对话步骤"
fi

date "+[%Y-%m-%d %H:%M:%S] [daily] done"

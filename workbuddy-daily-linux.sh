#!/bin/bash
# WorkBuddy 每日自动化（Linux / cron 版）：签到 + 成长中心，外加用 CodeBuddy 完成一次
# 对话（hi）以完成每日对话任务；运行结束后把结果通过飞书消息通知（feishu_notify.py）。
# cron：与 macOS launchd 三种触发保持一致——每天 00:05 全量跑；每 4 小时跑同一脚本
# 做补签 + 成长中心轮询；@reboot 开机补跑（对应 RunAtLoad）。幂等，重复触发不会重复领取。
# 注意：本任务一天触发多次。「对话」每天只做一次；
#       飞书通知：当天首次「成功完成」后标记停发；未成功则每次运行都发送，直到成功。

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=/usr/bin/python3
LOGDIR="$HOME/.local/share/workbuddy-auto-signin"

# cron 下 PATH 极小，确保 node / codebuddy 可用（nvm 升级版本后自动跟最新）
NODEBIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$NODEBIN" ] && export PATH="$NODEBIN:$PATH"
CODEBUDDY="$(command -v codebuddy 2>/dev/null || true)"

export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
CHAT_MARKER="$LOGDIR/.codebuddy-chat-${TODAY}.done"
NOTIFY_MARKER="$LOGDIR/.feishu-notify-${TODAY}.done"
RUN_LOG="$LOGDIR/daily-${TODAY}.log"

mkdir -p "$LOGDIR"

# 飞书凭据：cron 不继承终端环境，从 ~/.zshrc（含其 source 引用）文本提取。
. "$REPO/feishu_env.sh"

# 后续步骤统一 tee 到 RUN_LOG，便于排错。
exec > >(tee -a "$RUN_LOG") 2>&1

date "+[%Y-%m-%d %H:%M:%S] [daily] start"

# 1) 核心：签到 + 成长中心（连登补登/兑换、Buddy 旅行礼物/派出、任务、盲盒、能量）。
#    接口幂等，一天多次触发不会重复领取。
WB_OUT="$("$PYTHON" "$REPO/signin.py" auto)"
SIGNIN_RC=$?
printf '%s\n' "$WB_OUT"
echo "[daily] signin auto exit=$SIGNIN_RC"

# 从输出 JSON 提取 result / report / growth（report、growth 是面向人的中文描述）。
FIELDS="$(printf '%s\n' "$WB_OUT" | "$PYTHON" -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.lstrip().startswith("{")]
try:
    data = json.loads(lines[-1])
    for key in ("result", "report", "growth"):
        print(data.get(key) or "")
except Exception:
    pass
')"
RESULT="${FIELDS%%$'\n'*}"
REST="${FIELDS#*$'\n'}"
REPORT="${REST%%$'\n'*}"
GROWTH="${REST#*$'\n'}"

# 2) 用 CodeBuddy 完成一次对话（内容 hi）。每天只做一次（含失败也标记，次日重试），
#    未安装/失败不影响上面的签到；此步不致命。
CODEBUDDY_RC=0
CODEBUDDY_NOTE="CLI 未安装，已跳过对话"
if [ -n "$CODEBUDDY" ] && [ -x "$CODEBUDDY" ]; then
  if [ -f "$CHAT_MARKER" ]; then
    echo "[daily] codebuddy 今天已完成对话，跳过"
    CODEBUDDY_NOTE="今日已完成，跳过"
  else
    TS="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$TS] codebuddy conversation start: hi" >> "$LOGDIR/conversation.log"
    echo "[daily] codebuddy conversation: hi"
    # 与 macOS 一致：--tools '' 禁用全部工具，纯对话不碰文件，避免定时任务里的 Agent 改动仓库
    timeout 150 "$CODEBUDDY" -p 'hi' --tools '' 2>&1 | head -40
    CODEBUDDY_RC=${PIPESTATUS[0]}
    echo "[daily] codebuddy exit=$CODEBUDDY_RC"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] codebuddy conversation exit: $CODEBUDDY_RC" >> "$LOGDIR/conversation.log"
    : > "$CHAT_MARKER"
    if [ "$CODEBUDDY_RC" -eq 0 ]; then CODEBUDDY_NOTE="对话完成"
    else CODEBUDDY_NOTE="对话失败（退出码 ${CODEBUDDY_RC}）"; fi
  fi
else
  echo "[daily] codebuddy CLI 未找到，跳过对话步骤"
fi

date "+[%Y-%m-%d %H:%M:%S] [daily] done"

# 3) 飞书通知：汇总结果。失败（任一非 0）或今日尚未通知过 -> 发送；其余跳过。
OVERALL_RC=$SIGNIN_RC
if [ "$CODEBUDDY_RC" -ne 0 ]; then OVERALL_RC=$CODEBUDDY_RC; fi

if [ "$OVERALL_RC" -eq 0 ]; then
  ICON="✅"; TITLE="WorkBuddy 每日任务成功"
else
  ICON="🚨"; TITLE="WorkBuddy 每日任务异常"
fi

SUMMARY="$ICON $TITLE
📅 $TODAY $(date '+%H:%M')
🎯 ${REPORT:-签到无详细输出（退出码 ${SIGNIN_RC}）}"
[ -n "$GROWTH" ] && [ "$GROWTH" != "$REPORT" ] && SUMMARY="$SUMMARY
🌱 $GROWTH"
SUMMARY="$SUMMARY
💬 CodeBuddy 对话：$CODEBUDDY_NOTE"

if [ "$OVERALL_RC" -ne 0 ]; then
  SUMMARY="$SUMMARY
🔁 修复前每次运行都会重发本告警，恢复成功后会再报平安"
fi

if [ "$OVERALL_RC" -eq 0 ]; then
  if [ -f "$NOTIFY_MARKER" ]; then
    echo "[daily] 今日任务已成功完成并通知过，跳过飞书通知"
  else
    # 成功且尚未标记：发送，送达后才标记「今日已完成」。
    if "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY"; then
      : > "$NOTIFY_MARKER"
    else
      echo "[daily] 飞书通知未送达（见上），下次运行会重发"
    fi
  fi
else
  # 失败：每次运行都告警，并清除成功标记，恢复成功后会再次通知。
  "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY" || true
  rm -f "$NOTIFY_MARKER"
  echo "[daily] 本次运行异常，已发飞书告警；成功标记已清除，恢复成功后会再次通知"
fi

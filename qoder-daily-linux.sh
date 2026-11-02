#!/bin/bash
# Qoder CN 每日自动化（Linux / cron 版）：领取每日 Credits（接口幂等，重复触发不会
# 重复领取），运行结束后把结果通过飞书消息通知（feishu_notify.py）。
# cron：每天 10:05 主领取（Credits 每天 10:00 刷新），11:05/15:05/19:05/23:05 兜底，
# 补上因睡眠/关机错过的主领取；与 macOS launchd 的 5 次触发保持一致。
# 飞书通知：当天首次「成功完成」后标记停发；未成功则每次运行都
# 告警，直到成功；恢复成功后会再报平安。

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=/usr/bin/python3
LOGDIR="$HOME/.local/share/workbuddy-auto-signin"
ENVFILE="$HOME/.config/workbuddy-auto-signin/env"   # QODER_TOKEN（优先于桌面登录态）

[ -f "$ENVFILE" ] && { set -a; . "$ENVFILE"; set +a; }

export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
NOTIFY_MARKER="$LOGDIR/.qoder-feishu-notify-${TODAY}.done"
RUN_LOG="$LOGDIR/qoder-daily-${TODAY}.log"

mkdir -p "$LOGDIR"

# 飞书凭据：cron 不继承终端环境，从 ~/.zshrc（含其 source 引用）文本提取。
. "$REPO/feishu_env.sh"

# 后续步骤统一 tee 到 RUN_LOG，便于排错。
exec > >(tee -a "$RUN_LOG") 2>&1

date "+[%Y-%m-%d %H:%M:%S] [qoder] start"

QODER_OUT="$("$PYTHON" "$REPO/qoder_signin.py" auto)"
QODER_RC=$?
printf '%s\n' "$QODER_OUT"
echo "[qoder] qoder_signin auto exit=$QODER_RC"

date "+[%Y-%m-%d %H:%M:%S] [qoder] done"

# 从输出 JSON 提取 result / report（report 本身是面向人的中文描述），拼装通知文案。
FIELDS="$(printf '%s\n' "$QODER_OUT" | "$PYTHON" -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.lstrip().startswith("{")]
try:
    data = json.loads(lines[-1])
    print(data.get("result") or "")
    print(data.get("report") or "")
except Exception:
    pass
')"
RESULT="${FIELDS%%$'\n'*}"
REPORT="${FIELDS#*$'\n'}"

case "$RESULT" in
  CLAIMED)  ICON="✅"; TITLE="Qoder 积分领取成功" ;;
  ALREADY)  ICON="✅"; TITLE="Qoder 今日积分已领取，无需重复" ;;
  INACTIVE) ICON="ℹ️"; TITLE="Qoder 当前没有可领取的积分活动" ;;
  *)        ICON="🚨"; TITLE="Qoder 每日积分任务失败（退出码 ${QODER_RC}）" ;;
esac

SUMMARY="$ICON $TITLE
📅 $TODAY $(date '+%H:%M')
${REPORT:-脚本没有输出详细结果}"

if [ "$QODER_RC" -ne 0 ]; then
  SUMMARY="$SUMMARY
🔁 修复前每次运行都会重发本告警，恢复成功后会再报平安"
fi

if [ "$QODER_RC" -eq 0 ]; then
  if [ -f "$NOTIFY_MARKER" ]; then
    echo "[qoder] 今日任务已成功完成并通知过，跳过飞书通知"
  else
    # 成功且尚未标记：发送，送达后才标记「今日已完成」。
    if "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY"; then
      : > "$NOTIFY_MARKER"
    else
      echo "[qoder] 飞书通知未送达（见上），下次运行会重发"
    fi
  fi
else
  # 失败：每次运行都告警，并清除成功标记，恢复成功后会再次通知。
  "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY" || true
  rm -f "$NOTIFY_MARKER"
  echo "[qoder] 本次运行异常，已发飞书告警；成功标记已清除，恢复成功后会再次通知"
fi

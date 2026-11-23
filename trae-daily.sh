#!/bin/bash
# Trae CN 每日签到并把结果通过飞书通知。
# 当天首次成功通知后停发；失败持续告警，恢复成功后再次通知。
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${TRAE_CN_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  PYTHON="$(command -v python3 2>/dev/null)"
fi
export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
NOTIFY_MARKER="${TRAE_CN_NOTIFY_MARKER:-/tmp/trae-cn-feishu-notify-${TODAY}.done}"
LOGDIR="$HOME/Library/Logs/TraeCNDailyCheckin"
RUN_LOG="$LOGDIR/daily-${TODAY}.log"
mkdir -p "$LOGDIR"

# 只从配置文件提取并 export 飞书凭据，不写入 plist。
. "$REPO/feishu_env.sh"

exec > >(tee -a "$RUN_LOG") 2>&1

date "+[%Y-%m-%d %H:%M:%S] [trae-cn] start"
TRAE_OUT="$("$PYTHON" "$REPO/trae_signin.py" auto)"
TRAE_RC=$?
printf '%s\n' "$TRAE_OUT"
echo "[trae-cn] trae_signin auto exit=$TRAE_RC"
date "+[%Y-%m-%d %H:%M:%S] [trae-cn] done"

FIELDS="$(printf '%s\n' "$TRAE_OUT" | "$PYTHON" -c '
import json, sys
lines = [line for line in sys.stdin.read().splitlines() if line.lstrip().startswith("{")]
try:
    data = json.loads(lines[-1])
    print(data.get("result") or "")
    print(data.get("report") or "")
    credits = data.get("credits")
    print("%g" % credits if isinstance(credits, (int, float)) and not isinstance(credits, bool) else "")
except Exception:
    pass
')"
RESULT="${FIELDS%%$'\n'*}"
REST="${FIELDS#*$'\n'}"
REPORT="${REST%%$'\n'*}"
CREDITS="${REST#*$'\n'}"

case "$RESULT" in
  CLAIMED)  ICON="✅"; TITLE="Trae CN 积分签到成功" ;;
  ALREADY)  ICON="✅"; TITLE="Trae CN 今日已签到，无需重复" ;;
  INACTIVE) ICON="ℹ️"; TITLE="Trae CN 签到活动当前未开放" ;;
  *)        ICON="🚨"; TITLE="Trae CN 每日签到失败（退出码 ${TRAE_RC}）" ;;
esac

SUMMARY="$ICON $TITLE
📅 $TODAY $(date '+%H:%M')"
if [ "$RESULT" != "ALREADY" ] || [ -z "$CREDITS" ]; then
  SUMMARY="$SUMMARY
${REPORT:-脚本没有输出详细结果}"
fi
if [ -n "$CREDITS" ]; then
  SUMMARY="$SUMMARY
本周期已领取 ${CREDITS} 积分"
fi
if [ "$TRAE_RC" -ne 0 ]; then
  SUMMARY="$SUMMARY
🔁 修复前每次运行都会重发本告警，恢复成功后会再报平安"
fi

if [ "$TRAE_RC" -eq 0 ]; then
  if [ -f "$NOTIFY_MARKER" ]; then
    echo "[trae-cn] 今日已成功签到并通知过，跳过飞书通知"
  elif "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY"; then
    : > "$NOTIFY_MARKER"
  else
    echo "[trae-cn] 飞书通知未送达，下次运行会重发"
  fi
else
  "$PYTHON" "$REPO/feishu_notify.py" "$SUMMARY" || true
  rm -f "$NOTIFY_MARKER"
  echo "[trae-cn] 本次运行异常，已尝试发送飞书告警"
fi

exit "$TRAE_RC"

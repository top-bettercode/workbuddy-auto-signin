#!/bin/bash
# 卸载 Trae CN 每日签到任务，保留日志和仓库文件。
set -u

LABEL="trae-cn-daily-checkin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/TraeCNDailyCheckin"
is_loaded() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

if is_loaded; then
  if ! launchctl bootout "gui/$(id -u)/$LABEL"; then
    echo "✗ 注销失败，已保留配置：$PLIST" >&2
    exit 1
  fi
  if is_loaded; then
    echo "✗ 任务仍处于加载状态，已保留配置：$PLIST" >&2
    exit 1
  fi
  echo "✓ 已从 launchd 注销"
fi
if [ -f "$PLIST" ]; then
  rm -f "$PLIST" || exit 1
  echo "✓ 已删除 $PLIST"
fi
[ ! -f "$PLIST.bak" ] || rm -f "$PLIST.bak"
echo "日志保留在 $LOGDIR；仓库文件未改动。"

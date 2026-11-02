#!/bin/bash
# Qoder CN 每日自动化：领取每日 Credits（qoder_signin.py auto），并把运行结果通过飞书通知。
# 由 launchd（qoder-daily-credits）在 10:05/11:05/15:05/19:05/23:05 + 登录时调用。
# 飞书通知：当天任务首次「成功完成」后标记停发；未成功则每次运行都发送，直到成功。

set -u

REPO="/Users/peter/data/repositories/workbuddy-auto-signin"
PYTHON="/Users/peter/.pyenv/versions/3.14.7/bin/python3"
export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
NOTIFY_MARKER="/tmp/qoder-feishu-notify-${TODAY}.done"
RUN_LOG="$HOME/Library/Logs/QoderDailyCredits/daily-${TODAY}.log"

mkdir -p "$HOME/Library/Logs/QoderDailyCredits"

# 从 ~/.zshrc 提取飞书自建应用凭据与收信配置（launchd 不会自动 source .zshrc，
# 这里只做文本解析，不执行任何配置文件）。~/.zshrc 自身的定义优先；找不到时
# 跟进 . / source 指令引用的文件（如 $HOME/data/repositories/sys/conf/.zshrc）。
zshrc_export() {
  local name="$1" ref val=""
  val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$HOME/.zshrc" 2>/dev/null | tail -1 \
    | sed -E "s/^[^=]*=['\"]?([^'\"]*)['\"]?/\1/")
  if [ -z "$val" ]; then
    while IFS= read -r ref; do
      ref=$(printf '%s' "$ref" | sed -E "s/[\"']//g; s/[[:space:]]*#.*$//; s/[[:space:]]+$//")
      ref="${ref%% *}"
      ref="${ref//\$HOME/$HOME}"
      ref="${ref//\~/$HOME}"
      [ -f "$ref" ] || continue
      val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$ref" 2>/dev/null | tail -1 \
        | sed -E "s/^[^=]*=['\"]?([^'\"]*)['\"]?/\1/")
      [ -n "$val" ] && break
    done < <(grep -E "^[[:space:]]*(source|\.)[[:space:]]+" "$HOME/.zshrc" 2>/dev/null \
      | sed -E "s/^[[:space:]]*(source|\.)[[:space:]]+//")
  fi
  printf '%s\n' "$val"
}
export FEISHU_SELF_APP_ID="$(zshrc_export FEISHU_SELF_APP_ID)"
export FEISHU_SELF_APP_SECRET="$(zshrc_export FEISHU_SELF_APP_SECRET)"
export FEISHU_NOTIFY_RECEIVE_ID="${FEISHU_NOTIFY_RECEIVE_ID:-$(zshrc_export FEISHU_NOTIFY_RECEIVE_ID)}"
export FEISHU_NOTIFY_RECEIVE_ID_TYPE="${FEISHU_NOTIFY_RECEIVE_ID_TYPE:-user_id}"

exec > >(tee -a "$RUN_LOG") 2>&1

date "+[%Y-%m-%d %H:%M:%S] [qoder] start"

# 核心：领取每日 Credits（接口幂等，重复触发不会重复领取）。
"$PYTHON" "$REPO/qoder_signin.py" auto
QODER_RC=$?
echo "[qoder] qoder_signin auto exit=$QODER_RC"

date "+[%Y-%m-%d %H:%M:%S] [qoder] done"

SUMMARY="Qoder CN 每日任务 ${TODAY}
状态: $([ "$QODER_RC" -eq 0 ] && echo OK || echo 异常)
qoder_signin auto: exit=$QODER_RC
$(date '+%H:%M:%S') 完成"

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

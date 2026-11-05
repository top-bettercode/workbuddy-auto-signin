#!/bin/bash
# WorkBuddy 每日自动化：签到 + 成长中心，外加用 CodeBuddy 完成一次对话（hi）以完成每日登录/成长任务。
# 运行结束后把结果通过飞书消息通知（见底部 feishu 步骤）。
# 由 launchd（workbuddy-auto-signin）每日 00:05、每 4 小时、登录时调用。
# 注意：本任务会多次触发（每天 00:05 + 每 4 小时 + 登录时）。「对话」每天只做一次；
#       飞书通知：当天任务首次「成功完成」后标记停发；未成功则每次运行都发送，直到成功。

set -u

REPO="/Users/peter/data/repositories/workbuddy-auto-signin"
PYTHON="/Users/peter/.pyenv/versions/3.14.7/bin/python3"
# launchd 的 PATH 极小（/usr/bin:/bin:...），而 codebuddy 的 shebang 是
# 「#!/usr/bin/env node」——不补 PATH 会在启动时报 env: node: No such file
# or directory（退出码 127）。与 workbuddy-daily-linux.sh 一致：把最新 nvm
# node 加入 PATH（升级后自动跟随），CLI 位置交给 command -v 解析，不再写死
# 版本号路径。
NODEBIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$NODEBIN" ] && export PATH="$NODEBIN:$PATH"
CODEBUDDY="$(command -v codebuddy 2>/dev/null || true)"
export PYTHONIOENCODING="utf-8"

TODAY="$(date +%F)"
CHAT_MARKER="/tmp/workbuddy-codebuddy-chat-${TODAY}.done"
NOTIFY_MARKER="/tmp/workbuddy-feishu-notify-${TODAY}.done"
RUN_LOG="/tmp/workbuddy-daily-${TODAY}.log"

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

# 后续步骤统一 tee 到 RUN_LOG，便于末尾汇总发送。
exec > >(tee -a "$RUN_LOG") 2>&1

date "+[%Y-%m-%d %H:%M:%S] [daily] start"

# 1) 核心：WorkBuddy 每日签到 + 成长中心（连登补登/兑换、Buddy 旅行礼物/派出、任务、盲盒、能量）
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

# 2) 用 CodeBuddy 完成一次对话（内容 hi），完成「每日登录/成长」里的对话任务。
#    依赖 CodeBuddy CLI 已登录（凭据持久化在 ~/.codebuddy，本机已登录）。
#    未登录/失败不影响上面的签到；此步不致命。每天只做一次。
CODEBUDDY_RC=0
CODEBUDDY_NOTE="CLI 未安装，已跳过对话"
if [ -x "$CODEBUDDY" ]; then
  if [ -f "$CHAT_MARKER" ]; then
    echo "[daily] codebuddy 今天已完成对话，跳过"
    CODEBUDDY_NOTE="今日已完成，跳过"
  else
    echo "[daily] codebuddy conversation: hi"
    timeout 150 "$CODEBUDDY" -p 'hi' --tools '' 2>&1 | head -40
    CODEBUDDY_RC=${PIPESTATUS[0]}
    echo "[daily] codebuddy exit=$CODEBUDDY_RC"
    : > "$CHAT_MARKER"
    if [ "$CODEBUDDY_RC" -eq 0 ]; then CODEBUDDY_NOTE="对话完成"
    else CODEBUDDY_NOTE="对话失败（退出码 ${CODEBUDDY_RC}）"; fi
  fi
else
  echo "[daily] codebuddy CLI 未找到（${CODEBUDDY}），跳过对话步骤"
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

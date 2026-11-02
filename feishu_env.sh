#!/bin/bash
# 飞书通知凭据提取（供 Linux 定时任务的包装脚本 source）。
# cron / systemd 不会 source ~/.zshrc，这里只做文本解析，不执行任何配置文件：
# ~/.zshrc 自身的定义优先；找不到时跟进 . / source 指令引用的文件
# （如 $HOME/data/repositories/sys/conf/.zshrc）。
# 提供 feishu_notify.py 需要的四个环境变量：
#   FEISHU_SELF_APP_ID / FEISHU_SELF_APP_SECRET
#   FEISHU_NOTIFY_RECEIVE_ID / FEISHU_NOTIFY_RECEIVE_ID_TYPE（默认 user_id）

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
unset -f zshrc_export

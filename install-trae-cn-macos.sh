#!/bin/bash
# 安装 Trae CN 每日签到 launchd 任务；不影响 WorkBuddy/Qoder 任务。
set -u

LABEL="trae-cn-daily-checkin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BACKUP="$PLIST.bak"
LOGDIR="$HOME/Library/Logs/TraeCNDailyCheckin"
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/trae_signin.py"
WRAPPER="$DIR/trae-daily.sh"
TMPPLIST=""
ERRTMP=""

cleanup() {
  [ -n "$TMPPLIST" ] && rm -f "$TMPPLIST"
  [ -n "$ERRTMP" ] && rm -f "$ERRTMP"
}
trap cleanup EXIT
is_loaded() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }
die() { echo "✗ $*" >&2; exit 1; }

PYTHON="${TRAE_CN_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
              "$(command -v python3 2>/dev/null)"; do
    if [ -n "$cand" ] && [ -x "$cand" ] && [ "$cand" != "/usr/bin/python3" ]; then
      PYTHON="$cand"
      break
    fi
  done
fi
if [ -z "$PYTHON" ] && [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; fi
[ -n "$PYTHON" ] || die "找不到 Python 3，请先安装或用 TRAE_CN_PYTHON 指定"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
  2>/dev/null || die "需要 Python 3.8+"
[ -f "$SCRIPT" ] || die "找不到 $SCRIPT"
[ -f "$WRAPPER" ] || die "找不到 $WRAPPER"

absolute_path() {
  "$PYTHON" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}
if [ -n "${TRAE_CN_EXE:-}" ]; then
  TRAE_CN_EXE="$(absolute_path "$TRAE_CN_EXE")" || die "无法解析 TRAE_CN_EXE"
  export TRAE_CN_EXE
fi
if [ -n "${TRAE_CN_STORAGE:-}" ]; then
  TRAE_CN_STORAGE="$(absolute_path "$TRAE_CN_STORAGE")" || die "无法解析 TRAE_CN_STORAGE"
  export TRAE_CN_STORAGE
fi

echo "[0/3] 离线登录态自检（不联网、不领取）"
if DOCTOR="$("$PYTHON" "$SCRIPT" doctor 2>&1)"; then
  echo "  ✓ $DOCTOR"
else
  echo "  ✗ 自检失败：$DOCTOR"
  echo "    请先打开 Trae CN 并确认已登录。"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR" || die "无法创建配置或日志目录"
TMPPLIST="$(mktemp "${TMPDIR:-/tmp}/$LABEL.plist.XXXXXX")" || die "无法创建临时 plist"
ERRTMP="$(mktemp "${TMPDIR:-/tmp}/$LABEL.boot.XXXXXX")" || true

echo "[1/3] 生成 launchd 配置"
if ! PL_LABEL="$LABEL" PL_PYTHON="$PYTHON" PL_WRAPPER="$WRAPPER" PL_DIR="$DIR" \
     PL_LOGDIR="$LOGDIR" PL_EXE="${TRAE_CN_EXE:-}" PL_STORAGE="${TRAE_CN_STORAGE:-}" \
     "$PYTHON" - "$TMPPLIST" <<'PYEOF'
import os
import plistlib
import sys

env = {"PYTHONIOENCODING": "utf-8", "TRAE_CN_PYTHON": os.environ["PL_PYTHON"]}
if os.environ.get("PL_EXE"):
    env["TRAE_CN_EXE"] = os.environ["PL_EXE"]
if os.environ.get("PL_STORAGE"):
    env["TRAE_CN_STORAGE"] = os.environ["PL_STORAGE"]
plist = {
    "Label": os.environ["PL_LABEL"],
    "ProgramArguments": ["/bin/bash", os.environ["PL_WRAPPER"]],
    "WorkingDirectory": os.environ["PL_DIR"],
    "StartCalendarInterval": [
        {"Hour": hour, "Minute": 5} for hour in (9, 12, 15, 19, 23)
    ],
    "RunAtLoad": True,
    "ProcessType": "Background",
    "EnvironmentVariables": env,
    "StandardOutPath": os.path.join(os.environ["PL_LOGDIR"], "out.log"),
    "StandardErrorPath": os.path.join(os.environ["PL_LOGDIR"], "err.log"),
}
with open(sys.argv[1], "wb") as handle:
    plistlib.dump(plist, handle)
PYEOF
then
  die "生成 plist 失败"
fi
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TMPPLIST" >/dev/null 2>&1 || die "plist 校验失败，未改动现有任务"
fi

if [ -f "$PLIST" ]; then
  cp "$PLIST" "$BACKUP" || die "备份现有配置失败，未改动现有任务"
  echo "  ✓ 已备份现有配置"
fi
mv -f "$TMPPLIST" "$PLIST" || die "写入 $PLIST 失败"
TMPPLIST=""

echo "[2/3] 注册服务"
if is_loaded; then
  if ! launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; then
    [ ! -f "$BACKUP" ] || cp "$BACKUP" "$PLIST"
    die "注销旧任务失败，已恢复原配置"
  fi
fi
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>"$ERRTMP"; then
  if [ -f "$BACKUP" ]; then
    cp "$BACKUP" "$PLIST" || die "注册失败，且恢复备份失败"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    is_loaded && die "新配置注册失败，已回滚到原配置"
    die "新配置注册失败，自动回滚也未成功"
  fi
  rm -f "$PLIST"
  die "注册失败，未保留无效配置"
fi

echo "[3/3] 验证"
is_loaded || die "注册后未找到服务"
echo "✓ 已安装：每天 09:05 / 12:05 / 15:05 / 19:05 / 23:05，并在登录时补跑"
echo "日志目录：$LOGDIR"
echo "手动状态查询：$PYTHON $SCRIPT status"
echo "卸载：bash $DIR/uninstall-trae-cn-macos.sh"

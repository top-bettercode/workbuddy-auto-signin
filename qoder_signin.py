"""Qoder CN 每日 100 Credits 自动领取脚本。

作者：88lin
仓库：https://github.com/88lin/workbuddy-auto-signin
协议：MIT

Qoder CN 个人用户每天可在桌面端领取 100 通用 Credits。活动按 UTC+8 每天
10:00 刷新，没有补领。本脚本只读取本机 Qoder CN 桌面端的登录态，零内置密钥：

  GET  {base}/sash/api/v1/me/campaigns
  POST {base}/sash/api/v1/me/campaigns/{campaignId}/claim      （幂等）

Windows 上凭据由 DPAPI + AES-256-GCM 保护，macOS 上由钥匙串 + AES-128-CBC
保护，Linux 上由 Chromium OSCrypt（libsecret/KWallet/basic 兜底）+ AES-256-GCM
保护。解密只用标准库 + 平台自带能力：Windows 用 DPAPI / CNG（装了 cryptography
时优先用它），macOS/Linux 都把「解密」这一步交给客户端/系统自带的 Electron
运行时（safeStorage 内部就是 Chromium 的 OSCrypt），都不强依赖第三方包。三个
平台都不会打印令牌，也不会改写登录态文件。

用法：
  python qoder_signin.py auto      # 领取今日 Credits，输出一行 JSON
  python qoder_signin.py silent    # 同 auto，结果写 qoder_signin.log（配合 pythonw）
  python qoder_signin.py status    # 只查活动列表，不领取
  python qoder_signin.py doctor    # 离线检查登录态与设备组件，不联网
  python qoder_signin.py auto --dry-run   # 只报告会领到什么
  python qoder_signin.py auto --once      # 不做网络重试

退出码：0 已领/今日已领/无活动；1 失败；2 没有登录态或参数错误。
"""

import base64
import binascii
import shutil
import glob
import json
import os
import platform
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.1.0"

# 活动固定按 UTC+8 刷新，与运行机器的本地时区无关。
TZ_CN = timezone(timedelta(hours=8))
DEFAULT_BASE_URL = "https://openapi.qoder.com.cn"
AUTH_DIR_GLOB = "com.qodercn.app*"
AUTH_FILE = "auth.v1.dat"
LOCAL_STATE_FILE = "Local State"

# macOS：登录态目录与钥匙串条目。钥匙串里还有一条历史遗留的 "Qoder Safe
# Storage"（属于另一套安装），认错条目会解不出内容，所以写死带 "CN" 的。
MAC_KEYCHAIN_SERVICE = "Qoder CN App Safe Storage"
MAC_KEYCHAIN_ACCOUNT = "Qoder CN App Key"
MAC_HELPER_TIMEOUT = 20
AUTH_INPUT_LIMIT = 65536

# 缺这个头，服务端会返回空的活动列表。
BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Qoder/claim",
    "Cosy-ClientType": "10",
}

REQUEST_TIMEOUT = 20
# 重试只针对网络错误、429 和 5xx；401/403/业务错误重试也不会变好。
RETRY_DELAYS = (5, 15, 30)
# 计划任务的 ExecutionTimeLimit 是 PT5M；留 60s 给解释器启动和收尾。
BUDGET_SECONDS = 240.0

IS_WINDOWS = sys.platform == "win32"
_started_at = time.monotonic()
_sensitive_values = set()
_risk_cache = (0.0, {})
_risk_cache_root = None

FAILURE_RESULTS = (
    "ERROR", "NETWORK", "TIMEOUT", "NO_AUTH", "AUTH_ERROR",
    "AUTH_REJECTED", "FORBIDDEN", "PENDING",
)
QUIET_RESULTS = ("ALREADY", "INACTIVE")


class QoderAuthError(Exception):
    """登录态不可用。looked_in 用于诊断输出，不含令牌。"""

    def __init__(self, message, looked_in=None):
        super().__init__(message)
        self.looked_in = list(looked_in or [])


class NoAuthError(QoderAuthError):
    pass


class AuthFormatError(QoderAuthError):
    pass


# ---------------------------------------------------------------- 本地登录态

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    class _AeadInfo(ctypes.Structure):  # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
        _fields_ = [
            ("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
            ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", wt.ULONG),
            ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", wt.ULONG),
            ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", wt.ULONG),
            ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", wt.ULONG),
            ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG),
        ]


def _dpapi_unprotect(data):
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
    ):
        raise AuthFormatError("DPAPI 解不开密钥（%s）——需要同一 Windows 用户且已登录桌面"
                              % ctypes.WinError())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key, nonce, blob):
    """AES-GCM 解密，走 Windows 自带的 CNG，不依赖 cryptography。"""
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    ct, tag = blob[:-16], blob[-16:]

    def chk(res, what):
        if res != 0:
            raise AuthFormatError("CNG %s 失败 NTSTATUS=0x%08x" % (what, res & 0xFFFFFFFF))

    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    try:
        chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
        mode = "ChainingModeGCM"
        chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0),
            "SetProperty")
        kbuf = ctypes.create_string_buffer(key, len(key))
        chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0,
                                              kbuf, len(key), 0), "GenKey")
        nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _AeadInfo()
        info.cbSize = ctypes.sizeof(_AeadInfo)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)
        ibuf = ctypes.create_string_buffer(ct, len(ct))
        obuf = ctypes.create_string_buffer(len(ct))
        done = ctypes.c_ulong()
        chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                                 obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
        return obuf.raw[:done.value]
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _decrypt_gcm(key, nonce, blob):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_gcm_cng(key, nonce, blob)
    try:
        return AESGCM(key).decrypt(nonce, blob, None)
    except Exception as exc:
        raise AuthFormatError("AES-GCM 认证失败（%s）" % type(exc).__name__)


def find_auth_dirs():
    override = os.environ.get("QODER_AUTH_DIR")
    if override:
        return [Path(override).expanduser()]
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    try:
        roots = sorted(glob.glob(str(appdata / AUTH_DIR_GLOB)),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def _read_auth_store(root):
    raw = (root / AUTH_FILE).read_bytes()
    if raw[:3] != b"v10":
        raise AuthFormatError("%s 格式未知（%r）" % (AUTH_FILE, raw[:3]))
    if len(raw) < 3 + 12 + 16 + 2:
        raise AuthFormatError("%s 过短，可能正被客户端改写" % AUTH_FILE)

    state = json.loads((root / LOCAL_STATE_FILE).read_text(encoding="utf-8"))
    crypt = state.get("os_crypt") if isinstance(state, dict) else None
    if not isinstance(crypt, dict):
        raise AuthFormatError("%s 缺少 os_crypt 配置" % LOCAL_STATE_FILE)
    key_blob = base64.b64decode(crypt.get("encrypted_key") or "")
    if key_blob[:5] != b"DPAPI":
        raise AuthFormatError("%s 的 os_crypt 结构变了" % LOCAL_STATE_FILE)
    key = _dpapi_unprotect(key_blob[5:])
    plain = _decrypt_gcm(key, raw[3:15], raw[15:])
    sess = json.loads(plain.decode("utf-8"))
    if not isinstance(sess, dict):
        raise AuthFormatError("会话内容不是 JSON 对象")
    token = sess.get("token")
    if not isinstance(token, str) or not token.strip():
        raise AuthFormatError("解密成功但会话里没有 token")
    return sess


def _read_auth_store_retry(root, attempts=3):
    """客户端可能正在重写凭据，读到半截就短暂重试。"""
    last = None
    for i in range(attempts):
        try:
            return _read_auth_store(root)
        except (ValueError, OSError, AuthFormatError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.25)
    raise last


# ---------------------------------------------------------------- macOS 登录态
#
# macOS 上 Qoder CN 的 auth.v1.dat 同样是 "v10" 前缀，但格式与 Windows 不同：
#   Windows：v10 + 12 字节 nonce + AES-256-GCM（密钥由 DPAPI 保护）
#   macOS  ：v10 + AES-128-CBC 密文（IV 固定为 16 个空格，PKCS7 填充），
#            密钥 = PBKDF2-HMAC-SHA1(钥匙串密码, "saltysalt", 1003 轮, 16 字节)
# Python 标准库没有 AES，所以只把「解密」这一步交给客户端自带的 Electron
# 运行时（Node 自带 crypto），钥匙串读取仍在 Python 侧完成——这样两边都能
# 单独测试，也不引入 cryptography 依赖。

MAC_AUTH_HELPER_JS = r"""
'use strict';
const fs = require('fs');
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decrypt(request) {
  if (!object(request) || typeof request.password !== 'string' || !request.password)
    failure('INVALID_FORMAT');
  if (typeof request.authFile !== 'string' || !request.authFile) failure('INVALID_FORMAT');
  let raw;
  try { raw = fs.readFileSync(request.authFile); }
  catch (_) { failure('AUTH_MISSING'); }
  if (raw.slice(0, 3).toString() !== 'v10') failure('UNSUPPORTED_FORMAT');
  const key = crypto.pbkdf2Sync(request.password, 'saltysalt', 1003, 16, 'sha1');
  let plain;
  try {
    const decipher = crypto.createDecipheriv('aes-128-cbc', key, Buffer.alloc(16, 0x20));
    plain = Buffer.concat([decipher.update(raw.slice(3)), decipher.final()]);
  } catch (_) { failure('DECRYPT_FAILED'); }
  let session;
  try { session = JSON.parse(utf8(plain)); }
  catch (err) { failure(err.reason || 'UNSUPPORTED_FORMAT'); }
  if (!object(session)) failure('UNSUPPORTED_FORMAT');
  const token = session.token;
  if (typeof token !== 'string' || !token.length || token.length > 32768 ||
      !/^[A-Za-z0-9._~+\/-]+=*$/.test(token)) failure('TOKEN_MISSING');
  return {token: token,
          expiresAt: typeof session.expiresAt === 'string' ? session.expiresAt : null};
}
let chunks = [];
let size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}),
                       () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks)));
    chunks = [];
    reply({ok: true, session: decrypt(request)});
  } catch (err) {
    reply({ok: false, reason: (err && err.reason) || 'HELPER_PROTOCOL'});
  }
});
"""


def _mac_runtime(bundle):
    """从 .app 包解析主可执行文件；规则与 signin.py 保持一致。"""
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
            name = plistlib.load(handle).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") \
                or "/" in name or "\\" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def find_mac_qoder_runtime():
    override = os.environ.get("QODER_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise NoAuthError("QODER_EXE 不是可用的可执行文件：%s" % path)
        return path
    home = os.path.expanduser("~")
    candidates = [_mac_runtime(os.path.join(root, "Qoder CN.app"))
                  for root in ("/Applications", os.path.join(home, "Applications"))]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    raise NoAuthError("没找到 Qoder CN 客户端（/Applications/Qoder CN.app），"
                      "请先安装并登录，或用 QODER_EXE 指定其可执行文件")


def find_mac_auth_dirs():
    override = os.environ.get("QODER_AUTH_DIR")
    if override:
        return [Path(override).expanduser()]
    base = Path.home() / "Library" / "Application Support"
    try:
        roots = sorted(glob.glob(str(base / AUTH_DIR_GLOB)),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def _mac_keychain_password():
    """从登录钥匙串取加密密钥口令。进程首次访问可能弹出系统授权框。"""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", MAC_KEYCHAIN_SERVICE,
             "-a", MAC_KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10, shell=False)
    except (OSError, subprocess.SubprocessError):
        raise NoAuthError("读取钥匙串失败，请确认「钥匙串访问」可用") from None
    password = (result.stdout or "").strip()
    if result.returncode != 0 or not password:
        raise NoAuthError("钥匙串中没有 %s（Qoder CN 可能未登录，或钥匙串已锁定）"
                          % MAC_KEYCHAIN_SERVICE)
    return password


def _auth_error(reason):
    """把 Electron 解密助手的失败原因映射为异常，macOS 与 Linux 共用。"""
    if reason == "AUTH_MISSING":
        return NoAuthError("登录态文件不存在或不可读，请先打开 Qoder 桌面端完成登录")
    if reason == "KEYSTORE_UNAVAILABLE":
        return NoAuthError("系统密钥环不可用（libsecret/KWallet/钥匙串），"
                           "请在桌面会话内运行本脚本")
    if reason == "UNSUPPORTED_FORMAT":
        return AuthFormatError("登录态格式无法识别，客户端可能已升级；请更新脚本")
    if reason == "DECRYPT_FAILED":
        return AuthFormatError("登录态解密失败（密钥环密钥与凭据不匹配），请重新登录桌面端")
    if reason == "TOKEN_MISSING":
        return AuthFormatError("登录态中没有有效 token，请重新登录桌面端")
    return AuthFormatError("登录态读取失败（%s）" % (reason or "UNKNOWN"))


def _helper_diagnosis(process, limit=200):
    """解密助手 stdout 无法解析时，把退出码与 stderr 尾部带进错误便于定位。
    stdout 不落文本——成功回复里含 token，只报字节数。"""
    detail = "助手退出码 %s，stdout %d 字节" % (process.returncode, len(process.stdout))
    err = process.stderr.decode("utf-8", "replace").strip()
    if err:
        detail += "，stderr：%s" % _short(err, limit)
    return detail


def _run_mac_auth_helper(exe, request):
    """把密钥与凭据路径交给客户端自带的 Node 运行时解密，令牌经 stdout 返回。

    环境变量里剔除 NODE_/ELECTRON_/QODER_ 前缀，避免外层 shell 的配置
    （例如 ELECTRON_RUN_AS_NODE=0）干扰子进程。
    """
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthFormatError("登录态参数过大")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "QODER_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.run([exe, "-e", MAC_AUTH_HELPER_JS], input=data,
                                 capture_output=True, timeout=MAC_HELPER_TIMEOUT,
                                 env=env, shell=False)
    except subprocess.TimeoutExpired:
        raise NoAuthError("登录态解密超时，请稍后重试") from None
    except OSError:
        raise NoAuthError("Qoder CN 客户端无法作为运行时启动，请检查安装是否完整") from None

    try:
        reply = json.loads(process.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AuthFormatError("登录态助手返回了无法解析的结果（%s）"
                              % _helper_diagnosis(process)) from None
    if not isinstance(reply, dict) or reply.get("version") != 1:
        raise AuthFormatError("登录态助手返回格式不符")
    if reply.get("ok") is not True:
        raise _auth_error(reply.get("reason"))
    session = reply.get("session")
    if not isinstance(session, dict) or not isinstance(session.get("token"), str):
        raise AuthFormatError("登录态助手没有返回 token")
    return session


def _read_mac_session_retry(runtime, root, password, attempts=3):
    """客户端可能正在重写凭据，读到半截就短暂重试。

    只重试格式/解密类失败；运行时缺失、钥匙串不可用属于确定性错误，重试
    无意义（超时更不该重试三次，否则最坏要等 3 倍超时）。
    """
    last = None
    for i in range(attempts):
        try:
            return _run_mac_auth_helper(
                runtime, {"password": password, "authFile": str(root / AUTH_FILE)})
        except AuthFormatError as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.25)
    raise last


def _load_session_mac():
    runtime = find_mac_qoder_runtime()
    dirs = find_mac_auth_dirs()
    looked_in = [str(d) for d in dirs]
    if not dirs:
        raise NoAuthError("没找到 Qoder CN 登录态目录（~/Library/Application Support/%s），"
                          "请先安装并登录 Qoder CN 桌面端" % AUTH_DIR_GLOB, looked_in)

    candidates = [d for d in dirs if (d / AUTH_FILE).is_file()]
    if not candidates:
        raise NoAuthError("Qoder CN 桌面端尚未登录（%s）"
                          % "；".join("%s: 未登录" % d.name for d in dirs), looked_in)

    password = _mac_keychain_password()
    errors = []
    for root in candidates:
        try:
            session = _read_mac_session_retry(runtime, root, password)
        except QoderAuthError as exc:
            errors.append("%s: %s" % (root.name, exc))
            continue
        session["_auth_dir"] = str(root)
        return session

    raise AuthFormatError("Qoder CN 登录态读取失败（%s），请重新登录桌面端后重试"
                          % "；".join(errors), looked_in)


# ---------------------------------------------------------------- Linux 登录态
#
# Linux 上 Qoder 的 auth.v1.dat 是 "v11" 前缀 + 12 字节 nonce + AES-256-GCM，
# 密钥由 Chromium OSCrypt 管理：优先 libsecret，其次 KWallet（本机常见），
# 都不可用时回退到硬编码口令。密钥条目按应用名存取；桌面端的包名是
# qoder-cn（/usr/lib/qoder-cn/app.asar 里的 package.json），OSCrypt 用它
# 作为 libsecret 条目的 application 属性，助手必须同名才能取到同一个密钥。
# Python 标准库既没有 AES 也访问不了 KWallet，所以复用 macOS 的思路：把
# 「解密」整步交给系统里自带的 Electron 运行时——safeStorage API 内部就是
# Chromium 的 OSCrypt，让它自己去找密钥。必须以完整应用（而非
# ELECTRON_RUN_AS_NODE）方式启动，safeStorage 才存在；--headless 保证在
# 定时任务等无显示器的场景下也能运行；deepin/DTK 版 Electron 用 Qt 起窗，
# 无显示器时会因加载不了 xcb 插件而崩溃，--ozone-platform=headless 强制
# 走 Chromium 自带的无头平台，绕开 Qt 插件。

LINUX_HELPER_PACKAGE_JSON = {
    "name": "qoder-cn",
    "productName": "qoder-cn",
    "version": "1.0.0",
    "main": "main.js",
}

LINUX_HELPER_MAIN_JS = r"""
'use strict';
const { app, safeStorage } = require('electron');
const fs = require('fs');
const failure = reason => reply({ok: false, reason});
let replied = false;
function reply(value) {
  if (replied) return;
  replied = true;
  process.stdout.write(JSON.stringify({version: 1, ...value}),
                       () => app.exit(value.ok ? 0 : 1));
}
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function decrypt(file) {
  if (typeof file !== 'string' || !file) failure('INVALID_FORMAT');
  let raw;
  try { raw = fs.readFileSync(file); }
  catch (_) { failure('AUTH_MISSING'); }
  if (raw.slice(0, 3).toString() !== 'v11') failure('UNSUPPORTED_FORMAT');
  let text;
  try { text = safeStorage.decryptString(raw); }
  catch (_) { failure('DECRYPT_FAILED'); }
  let session;
  try { session = JSON.parse(text); }
  catch (err) { failure('UNSUPPORTED_FORMAT'); }
  if (!object(session)) failure('UNSUPPORTED_FORMAT');
  const token = session.token;
  if (typeof token !== 'string' || !token.length || token.length > 32768 ||
      !/^[A-Za-z0-9._~+\/-]+=*$/.test(token)) failure('TOKEN_MISSING');
  return {token: token,
          expiresAt: typeof session.expiresAt === 'string' ? session.expiresAt : null};
}
app.setPath('userData', require('os').tmpdir() + '/qoder-signin-helper');
app.whenReady().then(() => {
  if (!safeStorage.isEncryptionAvailable()) { failure('KEYSTORE_UNAVAILABLE'); return; }
  try { reply({ok: true, session: decrypt(process.argv[process.argv.length - 1])}); }
  catch (err) { failure((err && err.reason) || 'HELPER_PROTOCOL'); }
});
"""


def find_linux_runtime():
    override = os.environ.get("QODER_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise NoAuthError("QODER_EXE 不是可用的可执行文件：%s" % path)
        return path
    candidates = []
    for base in ("/usr/lib", "/usr/lib64", "/usr/local/lib"):
        candidates += glob.glob(os.path.join(base, "electron*", "electron"))
    candidates += glob.glob("/usr/bin/electron*")
    # electron43 > electron9：版本号大的排前面。
    candidates.sort(key=lambda p: [int(x) if x.isdigit() else 0
                                   for x in os.path.basename(os.path.dirname(p))
                                   .replace("electron", "").split(".")], reverse=True)
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    raise NoAuthError("没找到系统 Electron 运行时（/usr/lib/electron*/electron），"
                      "请安装 Qoder 桌面端依赖的 Electron 包，或用 QODER_EXE 指定")


def find_linux_auth_dirs():
    override = os.environ.get("QODER_AUTH_DIR")
    if override:
        return [Path(override).expanduser()]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        roots = sorted(glob.glob(str(base / AUTH_DIR_GLOB)),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def _run_linux_auth_helper(exe, auth_file):
    """把凭据路径交给系统 Electron 的 safeStorage 解密，令牌经 stdout 返回。

    safeStorage 只在完整应用进程里可用，所以先在临时目录写出 helper 应用
    （package.json 的 name 必须与桌面端一致，OSCrypt 才会取到同一个密钥）。
    环境变量里剔除 NODE_/ELECTRON_/QODER_ 前缀，避免外层 shell 的配置干扰。
    """
    import shutil
    import tempfile
    app_dir = tempfile.mkdtemp(prefix="qoder-signin-")
    try:
        with open(os.path.join(app_dir, "package.json"), "w", encoding="utf-8") as handle:
            json.dump(LINUX_HELPER_PACKAGE_JSON, handle)
        with open(os.path.join(app_dir, "main.js"), "w", encoding="utf-8") as handle:
            handle.write(LINUX_HELPER_MAIN_JS)
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("NODE_", "ELECTRON_", "QODER_"))}
        # Chromium 只在能识别出桌面环境时才启用 libsecret/KWallet 后端，
        # 定时任务等场景下 XDG_CURRENT_DESKTOP 为空会退回 basic_text，
        # 导致密钥环不可用。桌面端用什么后端加密，这里就得用同一个。
        if not env.get("XDG_CURRENT_DESKTOP"):
            env["XDG_CURRENT_DESKTOP"] = "Deepin"
        try:
            process = subprocess.run(
                [exe, "--headless", "--disable-gpu", "--ozone-platform=headless", app_dir, str(auth_file)],
                capture_output=True, timeout=MAC_HELPER_TIMEOUT, env=env, shell=False)
        except subprocess.TimeoutExpired:
            raise NoAuthError("登录态解密超时，请稍后重试") from None
        except OSError:
            raise NoAuthError("Electron 运行时无法启动，请检查安装是否完整") from None

        try:
            reply = json.loads(process.stdout.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AuthFormatError("登录态助手返回了无法解析的结果（%s）"
                                  % _helper_diagnosis(process)) from None
        if not isinstance(reply, dict) or reply.get("version") != 1:
            raise AuthFormatError("登录态助手返回格式不符")
        if reply.get("ok") is not True:
            raise _auth_error(reply.get("reason"))
        session = reply.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("token"), str):
            raise AuthFormatError("登录态助手没有返回 token")
        return session
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def _read_linux_session_retry(runtime, root, attempts=3):
    """客户端可能正在重写凭据，读到半截就短暂重试。

    只重试格式/解密类失败；运行时缺失、密钥环不可用属于确定性错误，重试
    无意义（超时更不该重试三次，否则最坏要等 3 倍超时）。
    """
    last = None
    for i in range(attempts):
        try:
            return _run_linux_auth_helper(runtime, root / AUTH_FILE)
        except AuthFormatError as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.25)
    raise last


def _load_session_linux():
    runtime = find_linux_runtime()
    dirs = find_linux_auth_dirs()
    looked_in = [str(d) for d in dirs]
    if not dirs:
        raise NoAuthError("没找到 Qoder 登录态目录（~/.config/%s），"
                          "请先安装并登录 Qoder 桌面端" % AUTH_DIR_GLOB, looked_in)

    candidates = [d for d in dirs if (d / AUTH_FILE).is_file()]
    if not candidates:
        raise NoAuthError("Qoder 桌面端尚未登录（%s）"
                          % "；".join("%s: 未登录" % d.name for d in dirs), looked_in)

    errors = []
    for root in candidates:
        try:
            session = _read_linux_session_retry(runtime, root)
        except QoderAuthError as exc:
            errors.append("%s: %s" % (root.name, exc))
            continue
        session["_auth_dir"] = str(root)
        return session

    raise AuthFormatError("Qoder 登录态读取失败（%s），请重新登录桌面端后重试"
                          % "；".join(errors), looked_in)


# ---------------------------------------------------------------- Linux CLI 登录态
#
# 桌面端之外，qoderclicn CLI 自己保存一份登录凭据（~/.qoder-cn/.auth/user）：
# AES-GCM 密文 + 随机 nonce，密钥由客户端自带的 qoder_auth_wasm.wasm 派生，
# 输入是 machine_id 文件的前 16 字节。Python 没法直接跑 WASM，所以把 wasm 从
# CLI 二进制里提出来（base64 blob，选中含 credential_storage_decrypt 的那个），
# 交给系统 node 实例化后调用解密导出，token 经 stdout 返回。凭据明文只进
# stdout，不落日志。

LINUX_CLI_HELPER_JS = r"""
'use strict';
const fs = require('fs');
const inputLimit = 1 << 22;
const failure = reason => { throw {reason}; };
let chunks = [];
let size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}),
                       () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', c => { size += c.length; if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'}); else chunks.push(c); });
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const wasm = fs.readFileSync(request.wasmFile);
    const machineId = fs.readFileSync(request.machineIdFile, 'utf8').trim();
    const encrypted = fs.readFileSync(request.userFile, 'utf8');
    if (!machineId) failure('MACHINE_ID_MISSING');
    const mod = new WebAssembly.Module(wasm);
    const heap = [];
    const addObject = v => (heap.push(v), heap.length - 1 + 1028);
    const getObject = i => heap[i - 1028];
    let ex;
    const takeStr = (ptr, len) => new TextDecoder('utf-8', {ignoreBOM: true, fatal: true})
      .decode(new Uint8Array(ex.memory.buffer).subarray(ptr, ptr + len));
    const imports = {};
    for (const imp of WebAssembly.Module.imports(mod)) {
      imports[imp.name] = function (...args) {
        if (imp.name.startsWith('__wbg_getRandomValues_')) {
          require('crypto').randomFillSync(new Uint8Array(ex.memory.buffer).subarray(args[1], args[1] + args[2]));
          return;
        }
        if (imp.name.startsWith('__wbg_crypto_')) return addObject(require('crypto'));
        if (imp.name.startsWith('__wbg_process_')) return addObject(process);
        if (imp.name.startsWith('__wbg_versions_')) return addObject(process.versions);
        if (imp.name.startsWith('__wbg_msCrypto_')) return addObject({});
        if (imp.name === '__wbindgen_throw') throw new Error(takeStr(args[0], args[1]));
        if (imp.name === '__wbindgen_object_drop_ref') return;
        if (imp.name.startsWith('__wbg___wbindgen_is_undefined_')) return getObject(args[0]) === undefined;
        if (imp.name.startsWith('__wbg___wbindgen_is_object_')) { const v = getObject(args[0]); return typeof v === 'object' && v !== null; }
        if (imp.name.startsWith('__wbg___wbindgen_is_function_')) return typeof getObject(args[0]) === 'function';
        if (imp.name.startsWith('__wbg___wbindgen_is_string_')) return typeof getObject(args[0]) === 'string';
        if (imp.name.startsWith('__wbg_Error_')) return addObject(new Error(takeStr(args[0], args[1])));
        if (imp.name.startsWith('__wbg_require_')) return addObject(require);
        if (imp.name.startsWith('__wbg_call_')) { try { return addObject(getObject(args[0]).apply(getObject(args[1]), args.slice(2).map(x => getObject(x)))); } catch (e) { return addObject(e); } }
        if (imp.name.startsWith('__wbg_new_')) return addObject({});
        if (imp.name.startsWith('__wbg_now_')) return Date.now();
        if (imp.name.startsWith('__wbindgen_object_clone_ref')) return addObject(getObject(args[0]));
        if (imp.name.startsWith('__wbindgen_cast_')) return args[0];
        if (imp.name.startsWith('__wbg_length_')) return getObject(args[0]).length;
        if (imp.name.startsWith('__wbg_new_with_length_')) return addObject(new Uint8Array(args[0]));
        if (imp.name.startsWith('__wbg_subarray_')) return addObject(getObject(args[0]).subarray(args[1], args[2]));
        if (imp.name.startsWith('__wbg_prototypesetcall_')) return;
        if (imp.name.startsWith('__wbg_set_')) return;
        failure('WASM_IMPORT_UNSUPPORTED');
      };
    }
    const instance = new WebAssembly.Instance(mod, { './qoder_auth_wasm_bg.js': imports });
    ex = instance.exports;
    const alloc = ex.__wbindgen_export2;
    const enc = new TextEncoder();
    const passString = s => {
      const bytes = enc.encode(s);
      const ptr = alloc(bytes.length, 1) >>> 0;
      new Uint8Array(ex.memory.buffer).set(bytes, ptr);
      return [ptr, bytes.length];
    };
    const [dPtr, dLen] = passString(encrypted);
    const [kPtr, kLen] = passString(machineId.slice(0, 16));
    const retptr = ex.__wbindgen_add_to_stack_pointer(-16);
    let plain;
    try {
      ex.credential_storage_decrypt(retptr, dPtr, dLen, kPtr, kLen);
      const dv = new DataView(ex.memory.buffer);
      const ptr = dv.getInt32(retptr, true) >>> 0;
      const len = dv.getInt32(retptr + 4, true) >>> 0;
      const isErr = dv.getInt32(retptr + 12, true);
      if (isErr) failure('DECRYPT_FAILED');
      plain = takeStr(ptr, len);
    } finally {
      ex.__wbindgen_add_to_stack_pointer(16);
    }
    const userInfo = JSON.parse(plain);
    const token = userInfo.access_token;
    if (typeof token !== 'string' || !token.length || token.length > 32768 ||
        !/^[A-Za-z0-9._~+\/-]+=*$/.test(token)) failure('TOKEN_MISSING');
    let expiresAt = null;
    if (typeof userInfo.expire_time === 'number' && userInfo.expire_time > 0) {
      const ms = userInfo.expire_time > 1e12 ? userInfo.expire_time : userInfo.expire_time * 1000;
      expiresAt = new Date(ms).toISOString();
    }
    reply({ok: true, session: {token, expiresAt}});
  } catch (err) {
    reply({ok: false, reason: (err && err.reason) || 'HELPER_PROTOCOL'});
  }
});
"""

LINUX_CLI_AUTH_DIR_ENV = "QODER_CLI_AUTH_DIR"
LINUX_CLI_EXE_ENV = "QODER_CLI_EXE"


def find_linux_cli_auth_dir():
    override = os.environ.get(LINUX_CLI_AUTH_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".qoder-cn" / ".auth"


def find_linux_cli_exe():
    override = os.environ.get(LINUX_CLI_EXE_ENV)
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path):
            raise NoAuthError("QODER_CLI_EXE 指向的文件不存在：%s" % path)
        return path
    located = shutil.which("qoderclicn")
    if located:
        return os.path.realpath(located)
    home = Path.home()
    candidates = sorted(glob.glob(str(home / ".qoder-cn" / "bin" / "qoderclicn" / "qoderclicn-*")))
    if candidates:
        return os.path.realpath(candidates[-1])
    for fallback in (home / ".local" / "bin" / "qoderclicn",):
        if fallback.is_file():
            return str(fallback)
    raise NoAuthError("没找到 qoderclicn CLI（~/.qoder-cn/bin/qoderclicn/），"
                      "请先安装 Qoder CLI 或用 QODER_CLI_EXE 指定")


def find_linux_cli_auth_wasm(exe):
    """从 CLI 二进制里提取 qoder_auth_wasm（base64 内嵌，选中带
    credential_storage_decrypt 导出的那个），按二进制特征缓存。"""
    st = os.stat(exe)
    cache_dir = Path.home() / ".cache" / "workbuddy-auto-signin"
    cache_path = cache_dir / ("cli-auth-wasm-%d-%d.wasm" % (st.st_size, int(st.st_mtime)))
    if cache_path.is_file():
        return cache_path
    marker = b"AGFzbQ"   # wasm 头部（\0asm 的 base64）前缀
    try:
        blob_data = Path(exe).read_bytes()
    except OSError:
        raise NoAuthError("无法读取 Qoder CLI 二进制：%s" % exe)
    b64chars = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    pos = blob_data.find(marker)
    while pos != -1:
        end = pos
        while end < len(blob_data) and blob_data[end] in b64chars:
            end += 1
        run = blob_data[pos:end]
        # 相邻 blob 可能被同为 base64 字符的分隔内容连成一个 run，逐个截断补
        # padding 重试，解不出就跳过这个魔数继续找下一个。
        for trim in range(0, 4):
            cut = run[:len(run) - trim] if trim else run
            try:
                candidate = base64.b64decode(cut + b"=" * (-len(cut) % 4))
            except (ValueError, binascii.Error):
                continue
            if b"credential_storage_decrypt" in candidate:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(candidate)
                return cache_path
        pos = blob_data.find(marker, pos + 8)
    raise AuthFormatError("CLI 二进制里没有找到 qoder_auth_wasm（客户端可能已变更），请更新脚本")


def find_node():
    node = os.environ.get("QODER_NODE") or shutil.which("node")
    if not node:
        raise NoAuthError("没找到 node 运行时（解密 CLI 凭据需要），请安装 node 或用 QODER_NODE 指定")
    return node


def _run_linux_cli_helper(node, wasm_path, auth_dir):
    request = {"wasmFile": str(wasm_path),
               "userFile": str(auth_dir / "user"),
               "machineIdFile": str(auth_dir / "machine_id")}
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthFormatError("登录态参数过大")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "QODER_"))}
    try:
        process = subprocess.run([node, "-e", LINUX_CLI_HELPER_JS], input=data,
                                 capture_output=True, timeout=MAC_HELPER_TIMEOUT,
                                 env=env, shell=False)
    except subprocess.TimeoutExpired:
        raise NoAuthError("CLI 登录态解密超时，请稍后重试") from None
    except OSError:
        raise NoAuthError("node 无法启动，请检查安装是否完整") from None
    try:
        reply = json.loads(process.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AuthFormatError("CLI 登录态助手返回了无法解析的结果（%s）"
                              % _helper_diagnosis(process)) from None
    if not isinstance(reply, dict) or reply.get("version") != 1:
        raise AuthFormatError("CLI 登录态助手返回格式不符")
    if reply.get("ok") is not True:
        raise _auth_error(reply.get("reason"))
    session = reply.get("session")
    if not isinstance(session, dict) or not isinstance(session.get("token"), str):
        raise AuthFormatError("CLI 登录态助手没有返回 token")
    return session


def _load_session_linux_cli():
    auth_dir = find_linux_cli_auth_dir()
    user_file = auth_dir / "user"
    machine_file = auth_dir / "machine_id"
    if not user_file.is_file() or not machine_file.is_file():
        raise NoAuthError("没找到 Qoder CLI 登录凭据（%s），请先用 qoderclicn login 登录"
                          % auth_dir, [str(auth_dir)])
    exe = find_linux_cli_exe()
    wasm_path = find_linux_cli_auth_wasm(exe)
    node = find_node()
    session = _run_linux_cli_helper(node, wasm_path, auth_dir)
    session["_auth_dir"] = str(auth_dir)
    session["_token_source"] = "cli"
    return session


def load_session():
    if sys.platform == "darwin":
        return _load_session_mac()
    if IS_WINDOWS:
        return _load_session_windows()
    if sys.platform.startswith("linux"):
        try:
            return _load_session_linux()
        except QoderAuthError as desktop_exc:
            # 桌面端登录态缺失/损坏时回退到 qoderclicn CLI 的本地凭据，
            # 两者独立存储：桌面端升级孤儿化凭据不影响 CLI。
            try:
                return _load_session_linux_cli()
            except NoAuthError as cli_exc:
                raise NoAuthError("%s；另外 %s" % (desktop_exc, cli_exc),
                                  desktop_exc.looked_in) from None
            except QoderAuthError as cli_exc:
                raise AuthFormatError("%s；CLI 凭据读取也失败：%s"
                                      % (desktop_exc, cli_exc),
                                      desktop_exc.looked_in) from None
    raise NoAuthError("自动读取 Qoder 登录态支持 Windows / macOS / Linux；"
                      "其他平台请设置 QODER_TOKEN")


def _load_session_windows():
    dirs = find_auth_dirs()
    looked_in = [str(d) for d in dirs]
    if not dirs:
        # 注意：消息里的 %APPDATA% 必须写成 %%APPDATA%%——这行只在没找到
        # 登录态目录时执行，% 格式化运算符会把单个 %A 当格式字符直接抛
        # ValueError，把干净的 NO_AUTH 变成“脚本运行异常”。装了桌面端的
        # 机器走不到这行，所以这个 bug 在正常环境不会暴露。
        raise NoAuthError("没找到 %%APPDATA%%\\%s，请先安装并登录 Qoder CN 桌面端"
                          % AUTH_DIR_GLOB, looked_in)

    errors = []
    saw_auth_file = False
    for root in dirs:
        if not (root / AUTH_FILE).is_file():
            errors.append("%s: 未登录" % root.name)
            continue
        saw_auth_file = True
        try:
            sess = _read_auth_store_retry(root)
        except AuthFormatError as exc:
            errors.append("%s: %s" % (root.name, exc))
            continue
        except (OSError, ValueError) as exc:
            errors.append("%s: %s" % (root.name, type(exc).__name__))
            continue
        sess["_auth_dir"] = str(root)
        return sess

    detail = "；".join(errors)
    if saw_auth_file:
        raise AuthFormatError("Qoder CN 登录态读取失败（%s），请重新登录桌面端后重试" % detail,
                              looked_in)
    raise NoAuthError("Qoder CN 桌面端尚未登录（%s）" % detail, looked_in)


def _parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _token_expired(sess):
    expires = _parse_iso(sess.get("expiresAt"))
    return expires is not None and expires <= datetime.now(timezone.utc)


# ---------------------------------------------------------------- 设备标识
#
# 服务端更愿意给带设备标识的请求下发活动。标识不编造：直接运行 Qoder CN
# 安装目录里自带的 runtime-info.exe，取客户端平时使用的那一份。找不到组件
# 就退回普通请求，不影响主流程。

UMID_REL_PATH = ("resources", "umid", "runtime-info.exe") if IS_WINDOWS \
    else ("umid", "runtime-info")


def _registry_roots():
    roots = []
    if not IS_WINDOWS:
        return roots
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        name = str(winreg.QueryValueEx(sub, "DisplayName")[0]).lower()
                        if "qoder" not in name:
                            continue
                        try:
                            loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip()
                        except OSError:
                            loc = ""
                        if loc:
                            roots.append(Path(loc))
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    return roots


def _running_roots():
    roots = []
    if not IS_WINDOWS:
        return roots
    try:
        kernel32 = ctypes.windll.kernel32

        class Entry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("th32Threads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (0, -1):
            return roots
        try:
            entry = Entry32()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                exe_name = entry.szExeFile.decode("utf-8", "replace").lower()
                if exe_name in ("qoder cn.exe", "qoder.exe"):
                    handle = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                    if handle:
                        try:
                            buf = ctypes.create_unicode_buffer(32768)
                            size = wt.DWORD(len(buf))
                            if kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                                   ctypes.byref(size)):
                                roots.append(Path(buf.value[:size.value]).parent)
                        finally:
                            kernel32.CloseHandle(handle)
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        pass
    return roots


def _umid_candidates():
    override = os.environ.get("QODER_UMID_EXE")
    if override:
        return [Path(override).expanduser()]
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        prog86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        roots = [
            local / "Programs" / "Qoder CN",
            prog / "Qoder CN",
            prog86 / "Qoder CN",
            local / "Programs" / "Qoder",
            prog / "Qoder",
            prog86 / "Qoder",
        ]
        roots += _registry_roots() + _running_roots()
    elif sys.platform.startswith("linux"):
        roots = [
            Path("/usr/lib/qoder-cn"),
            Path("/usr/lib/qoder-cn-ide"),
            Path("/usr/share/qoder-cn"),
            Path("/usr/share/qoder-cn-ide"),
            Path.home() / ".local" / "share" / "qoder-cn",
        ]
    else:
        return []
    seen, uniq = set(), []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(root)
    return [root.joinpath(*UMID_REL_PATH) for root in uniq]


def _platform_auth_dirs():
    if sys.platform == "darwin":
        return find_mac_auth_dirs()
    if IS_WINDOWS:
        return find_auth_dirs()
    return find_linux_auth_dirs()


def _machine_id(auth_dir):
    candidates = [Path(auth_dir)] if auth_dir else []
    candidates += [d for d in _platform_auth_dirs() if d not in candidates]
    for root in candidates:
        try:
            value = (root / "auth.machine-id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _run_umid(exe):
    try:
        proc = subprocess.run(
            [str(exe), "--account-stdin"], input=b"", capture_output=True,
            timeout=15, cwd=str(exe.parent),
            creationflags=0x08000000 if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def risk_headers(auth_dir=None):
    global _risk_cache, _risk_cache_root
    now = time.time()
    if _risk_cache_root == auth_dir and now - _risk_cache[0] < 1800:
        return dict(_risk_cache[1])

    headers = {}
    if IS_WINDOWS:
        arch = {"amd64": "x86_64", "x86_64": "x86_64",
                "aarch64": "aarch64", "arm64": "aarch64"}.get(
                    platform.machine().lower(), "x86_64")
        headers["Cosy-MachineOS"] = "%s_windows" % arch
        headers["Cosy-MachineHostname"] = socket.gethostname()
    elif sys.platform.startswith("linux"):
        arch = {"x86_64": "x86_64", "amd64": "x86_64",
                "aarch64": "aarch64", "arm64": "aarch64"}.get(
                    platform.machine().lower(), "x86_64")
        headers["Cosy-MachineOS"] = "%s_linux" % arch
        headers["Cosy-MachineHostname"] = socket.gethostname()

    exe = next((p for p in _umid_candidates() if p.is_file()), None)
    if exe:
        try:
            manifest = json.loads((exe.parent.parent / "build-manifest.json")
                                  .read_text(encoding="utf-8"))
            version = manifest.get("productVersion")
            if version:
                headers["Cosy-Version"] = str(version)
        except (OSError, ValueError):
            pass
        identity = _run_umid(exe)
        for src, dst in (("machineToken", "Cosy-MachineToken"),
                         ("machineCode", "Cosy-MachineCode"),
                         ("machineType", "Cosy-MachineType")):
            if identity.get(src):
                headers[dst] = str(identity[src])

    mid = _machine_id(auth_dir)
    if mid:
        headers["Cosy-MachineId"] = mid

    _risk_cache = (now, dict(headers))
    _risk_cache_root = auth_dir
    return headers


# ---------------------------------------------------------------- 服务端

class Api:
    def __init__(self, token, base_url, risk=None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.risk = risk or {}

    def _raw(self, path, method):
        url = self.base_url + path
        request = urllib.request.Request(
            url, method=method,
            headers={**BASE_HEADERS, **self.risk, "Authorization": "Bearer %s" % self.token},
            data=b"" if method == "POST" else None,
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read(1 << 20)
                code = resp.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(1 << 20)
            except OSError:
                raw = b""
            code = exc.code
        except (urllib.error.URLError, OSError) as exc:
            return 0, None, str(exc)

        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return code, body, None

    def call(self, path, method="GET", retry=True):
        attempt = 0
        while True:
            code, body, err = self._raw(path, method)
            retryable = code == 0 or code == 429 or 500 <= code < 600
            if not retryable or not retry or attempt >= len(RETRY_DELAYS):
                return code, body, err
            delay = RETRY_DELAYS[attempt]
            if _remaining() <= delay + REQUEST_TIMEOUT:
                return code, body, err
            time.sleep(delay)
            attempt += 1

    def campaigns(self, retry=True):
        return self.call("/sash/api/v1/me/campaigns", retry=retry)

    def claim(self, campaign_id, retry=True):
        return self.call("/sash/api/v1/me/campaigns/%s/claim" % campaign_id,
                         method="POST", retry=retry)


def _remaining():
    return BUDGET_SECONDS - (time.monotonic() - _started_at)


def _campaigns(payload):
    if not isinstance(payload, dict):
        return []
    rows = payload.get("campaigns")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _amount(campaign):
    benefit = campaign.get("benefit")
    if isinstance(benefit, dict):
        amount = benefit.get("amount")
        if isinstance(amount, (int, float)):
            return amount
    return None


def _validity(campaign):
    benefit = campaign.get("benefit")
    validity = benefit.get("validity") if isinstance(benefit, dict) else None
    if isinstance(validity, dict) and validity.get("mode") == "RELATIVE_DAYS":
        days = validity.get("days")
        if days:
            return "领取后 %s 天有效" % days
    return ""


def _fmt_ts(value):
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value, TZ_CN).strftime("%m-%d %H:%M")


def _window(campaign):
    start, end = _fmt_ts(campaign.get("startAt")), _fmt_ts(campaign.get("endAt"))
    if start and end:
        return "%s ~ %s" % (start, end)
    return ""


def _is_active(campaign, now):
    start = campaign.get("startAt")
    end = campaign.get("endAt")
    if isinstance(start, (int, float)) and now < start:
        return False
    if isinstance(end, (int, float)) and now >= end:
        return False
    return True


def select_campaigns(payload, now):
    """把活动列表分成：可领取 / 本窗口已领 / 其它发币活动。"""
    benefit = [c for c in _campaigns(payload) if c.get("actionType") == "CLAIM_BENEFIT"]
    active = [c for c in benefit if _is_active(c, now)]
    todo = [c for c in active if c.get("claimStatus") == "CLAIMABLE"]
    claimed = [c for c in active if c.get("claimStatus") == "CLAIMED"]
    return {"benefit": benefit, "active": active, "todo": todo, "claimed": claimed}


def _credits_text(total):
    return "%s Credits" % total if total is not None else "Credits"


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _short(value, limit=200):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _result(name, report, **extra):
    out = {"result": name, "report": report, "needs_attention": name in FAILURE_RESULTS}
    out.update(extra)
    return out


def _claim_all(api, campaigns, retry):
    total = None
    keys = []
    failures = []
    replayed_any = False
    last_http = None
    for campaign in campaigns:
        key = campaign.get("campaignKey") or campaign.get("campaignId") or "?"
        code, body, err = api.claim(campaign.get("campaignId"), retry=retry)
        last_http = code
        if code == 401:
            return _result("AUTH_REJECTED",
                           "领取时登录态被拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                           http=code, campaign=key)
        if code == 403:
            return _result("FORBIDDEN",
                           "领取被服务端拒绝（HTTP 403），请确认账号资格后重试",
                           http=code, campaign=key)
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) \
            else body
        ok = code == 200 and isinstance(data, dict) and data.get("status") == "CLAIMED"
        if not ok:
            detail = err or body
            failures.append("%s: HTTP %s %s" % (key, code, _short(detail, 120)))
            continue
        amount = _amount(data)
        if amount is None:
            amount = _amount(campaign)
        total = _sum([total, amount])
        keys.append(key)
        if data.get("replayed"):
            replayed_any = True

    if failures:
        return _result("ERROR", "领取失败：%s" % "；".join(failures),
                       http=last_http, campaign=", ".join(keys) or None, credits=total)
    report = "成功领取 %s" % _credits_text(total)
    if keys:
        report += "（%s" % "、".join(keys)
        if replayed_any:
            report += "，幂等重复"
        report += "）"
    valid = _validity(campaigns[0]) if campaigns else ""
    if valid:
        report += "，" + valid
    return _result("CLAIMED", report, http=last_http,
                   campaign=", ".join(keys) or None, credits=total)


def claim_once(api, retry=True, dry_run=False):
    code, payload, err = api.campaigns(retry=retry)
    if code == 401:
        return _result("AUTH_REJECTED",
                       "登录态被服务端拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                       http=code)
    if code == 403:
        return _result("FORBIDDEN", "查询被服务端拒绝（HTTP 403），请稍后重试", http=code)
    if code == 0:
        return _result("NETWORK", "网络请求失败：%s" % _short(err), http=0)
    if code != 200 or not isinstance(payload, dict):
        return _result("ERROR", "查询活动失败：HTTP %s %s" % (code, _short(payload)),
                       http=code)

    picked = select_campaigns(payload, time.time())
    todo, claimed = picked["todo"], picked["claimed"]
    if todo:
        amount = _sum(_amount(c) for c in todo)
        keys = "、".join(c.get("campaignKey") or "?" for c in todo)
        if dry_run:
            return _result("DRY_RUN",
                           "[dry-run] 可领取 %s（%s）" % (_credits_text(amount), keys),
                           http=code, campaign=keys, credits=amount)
        return _claim_all(api, todo, retry=retry)

    if claimed:
        amount = _sum(_amount(c) for c in claimed)
        keys = "、".join(c.get("campaignKey") or "?" for c in claimed)
        report = "本周期已领取 %s（%s" % (_credits_text(amount), keys)
        window = _window(claimed[0])
        if window:
            report += "，窗口 %s" % window
        report += "）"
        return _result("ALREADY", report, http=code, campaign=keys, credits=amount,
                       window=window or None)

    if picked["active"]:
        rows = []
        for campaign in picked["active"]:
            rows.append("%s status=%s" % (campaign.get("campaignKey") or "?",
                                          campaign.get("claimStatus") or "?"))
        return _result("PENDING", "活动存在但当前不可领取：%s" % "；".join(rows),
                       http=code)

    return _result("INACTIVE", "当前没有可领取的 Credits 活动（活动可能已结束）", http=code)


# ---------------------------------------------------------------- 输出

def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _redact(payload):
    for secret in _sensitive_values:
        payload = payload.replace(secret, "[REDACTED]")
    return payload


def emit(out, action, quiet=False):
    payload = _redact(json.dumps(out, ensure_ascii=False, default=str))
    silent = str(action).startswith("silent")
    if not silent and sys.stdout is not None:
        try:
            print(payload)
            return
        except Exception:
            pass
    if quiet and not _env_flag("QODER_SIGNIN_LOG_EMPTY"):
        return

    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload)
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "qoder_signin.log")
    for path in (os.environ.get("QODER_SIGNIN_LOG") or default_log, default_log):
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
            return
        except Exception:
            continue
    try:
        print(payload)
    except Exception:
        pass


def _resolve_token():
    token = (os.environ.get("QODER_TOKEN") or "").strip()
    if token:
        return token, {"source": "env", "expiresAt": None, "_auth_dir": None}
    sess = load_session()
    token = sess.get("token")
    _sensitive_values.add(token)
    return token, sess


def _auth_failure(exc, action):
    if isinstance(exc, NoAuthError):
        out = _result("NO_AUTH", str(exc), looked_in=exc.looked_in or None)
        emit(out, action)
        return 2
    out = _result("AUTH_ERROR", str(exc), looked_in=exc.looked_in or None)
    emit(out, action)
    return 1


def run_doctor():
    token = (os.environ.get("QODER_TOKEN") or "").strip()
    out = {"result": "OK", "version": VERSION, "platform": sys.platform,
           "token_source": "env" if token else "desktop", "needs_attention": False}
    doctor_token_source = "desktop"
    if token:
        _sensitive_values.add(token)
        out["report"] = "使用环境变量 QODER_TOKEN（无法离线校验有效期）"
        return out

    try:
        sess = load_session()
    except NoAuthError as exc:
        out.update(_result("NO_AUTH", str(exc), looked_in=exc.looked_in or None))
        return out
    except QoderAuthError as exc:
        out.update(_result("AUTH_ERROR", str(exc), looked_in=exc.looked_in or None))
        return out

    token = sess["token"]
    _sensitive_values.add(token)
    doctor_token_source = sess.get("_token_source", "desktop")
    expires = _parse_iso(sess.get("expiresAt"))
    out.update({
        "auth_dir": sess.get("_auth_dir"),
        "token_expires_at": sess.get("expiresAt"),
        "decrypted": True,
        "token_source": doctor_token_source})
    exe = next((p for p in _umid_candidates() if p.is_file()), None)
    out["device_component"] = str(exe) if exe else None
    out["device_identity"] = bool(risk_headers(sess.get("_auth_dir"))
                                  .get("Cosy-MachineToken"))

    if _token_expired(sess):
        out.update(_result("AUTH_REJECTED",
                           "登录态已于 %s 过期，请打开 Qoder CN 桌面端重新登录"
                           % sess.get("expiresAt")))
        return out

    report = "登录态可读"
    if expires:
        report += "，有效期至 %s" % expires.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M")
    if not out["device_identity"]:
        report += "；未取到设备标识，查询仍会尝试（通常不影响）"
    out["report"] = report
    return out


def run_status(api):
    code, payload, err = api.campaigns(retry=True)
    if code == 401:
        return _result("AUTH_REJECTED",
                       "登录态被服务端拒绝（HTTP 401），请打开 Qoder CN 桌面端重新登录",
                       http=code), 1
    if code == 403:
        return _result("FORBIDDEN", "查询被服务端拒绝（HTTP 403）", http=code), 1
    if code == 0:
        return _result("NETWORK", "网络请求失败：%s" % _short(err), http=0), 1
    if code != 200 or not isinstance(payload, dict):
        return _result("ERROR", "查询活动失败：HTTP %s %s" % (code, _short(payload)),
                       http=code), 1

    rows = []
    now = time.time()
    for campaign in _campaigns(payload):
        rows.append({
            "campaign": campaign.get("campaignKey"),
            "action": campaign.get("actionType"),
            "status": campaign.get("claimStatus"),
            "credits": _amount(campaign),
            "active": _is_active(campaign, now),
            "window": _window(campaign),
        })
    out = _result("OK", "共 %s 个活动" % len(rows), http=code, campaigns=rows,
                  show_campaign=payload.get("showCampaign"),
                  claimable=payload.get("claimable"))
    return out, 0


def run(action, dry_run=False, once=False, base_url=None):
    if action not in ("auto", "silent", "status", "doctor"):
        out = _result("ERROR", "未知命令：%s（可用：auto / silent / status / doctor）" % action)
        emit(out, action)
        return 2

    if action == "doctor":
        out = run_doctor()
        emit(out, action)
        if out.get("result") == "OK":
            return 0
        return 2 if out.get("result") == "NO_AUTH" else 1

    try:
        token, sess = _resolve_token()
    except QoderAuthError as exc:
        return _auth_failure(exc, action)

    if _token_expired(sess):
        out = _result("AUTH_REJECTED",
                      "登录态已于 %s 过期，请打开 Qoder CN 桌面端重新登录"
                      % sess.get("expiresAt"), token_expires_at=sess.get("expiresAt"))
        emit(out, action)
        return 1

    api = Api(token, base_url or os.environ.get("QODER_BASE_URL") or DEFAULT_BASE_URL,
              risk_headers(sess.get("_auth_dir")))

    if action == "status":
        out, code = run_status(api)
        emit(out, action)
        return code

    out = claim_once(api, retry=not once, dry_run=dry_run)
    if sess.get("expiresAt"):
        out["token_expires_at"] = sess.get("expiresAt")
    quiet = out.get("result") in QUIET_RESULTS and not out.get("needs_attention")
    emit(out, action, quiet=quiet)
    return 0 if out.get("result") in ("CLAIMED", "ALREADY", "INACTIVE", "DRY_RUN") else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    once = "--once" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--once")]
    action = argv[0] if argv else "auto"
    try:
        return run(action, dry_run=dry_run, once=once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit({"result": "ERROR",
              "report": "脚本运行异常（%s）" % type(exc).__name__,
              "needs_attention": True}, action)
        return 2


if __name__ == "__main__":
    sys.exit(main())

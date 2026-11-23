#!/usr/bin/env python3
"""Trae CN 每日积分签到：复用桌面端登录态，仅依赖 Python 标准库。"""

import json
import math
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
BASE_URL = "https://api.trae.cn"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
STORAGE_REL_PATH = Path("User") / "globalStorage" / "storage.json"
REQUEST_BODY = b'{"req_source": 1}'
REQUEST_TIMEOUT = 30
RETRY_DELAYS = (2, 5, 10)
HELPER_TIMEOUT = 20
_sensitive_values = set()


class TraeAuthError(Exception):
    pass


class NoAuthError(TraeAuthError):
    pass


class AuthFormatError(TraeAuthError):
    pass


# 在客户端自带 Electron 的 Node 运行时中解密，避免第三方 AES 依赖。
# stdout 仅通过管道返回给父进程；错误输出不包含令牌或明文。
AUTH_HELPER_JS = r"""
'use strict';
const fs = require('fs');
const crypto = require('crypto');
const AUTH_KEY = 'iCubeAuthInfo://icube.cloudide';
const DC_PREFIX = 'iCubeAuthInfo://icube-dc:';
const HEADER_AES = Buffer.from([116, 99, 5, 16, 0, 0]);
const SALT_A = Uint8Array.from([
  82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,
  124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,
  84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,
  8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37
]);
const SALT_B = Uint8Array.from([
  31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,
  96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,
  160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,
  23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125
]);
const failure = reason => { throw {reason}; };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function validId(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 &&
    !/[\r\n]/.test(value);
}
function deviceId(storage, machineIdFile) {
  const dcKey = Object.keys(storage).find(key => key.startsWith(DC_PREFIX));
  if (dcKey && validId(dcKey.slice(DC_PREFIX.length)))
    return {value: dcKey.slice(DC_PREFIX.length), source: 'icube-dc'};
  if (validId(storage['telemetry.devDeviceId']))
    return {value: storage['telemetry.devDeviceId'], source: 'telemetry.devDeviceId'};
  let machineId = storage['telemetry.machineId'];
  let source = 'telemetry.machineId';
  if (!validId(machineId)) {
    source = 'machineid';
    try { machineId = fs.readFileSync(machineIdFile, 'utf8').trim(); }
    catch (_) { machineId = ''; }
  }
  if (!validId(machineId)) failure('DEVICE_ID_MISSING');
  return {value: crypto.createHash('sha256').update(machineId).digest('hex').slice(0, 32),
          source: 'sha256:' + source};
}
function decrypt(encoded) {
  if (typeof encoded !== 'string' || !encoded || encoded.length > 1048576)
    failure('UNSUPPORTED_FORMAT');
  if (encoded.startsWith('{')) return encoded;
  const raw = Buffer.from(encoded, 'base64');
  if (raw.length <= 102 || !raw.subarray(0, 6).equals(HEADER_AES))
    failure('UNSUPPORTED_FORMAT');
  const random = raw.subarray(6, 38);
  const salt = Buffer.from(SALT_A.map((value, index) => value ^ SALT_B[index]));
  const first = crypto.createHash('sha512').update(random).digest();
  const derived = crypto.createHash('sha512')
    .update(Buffer.concat([first, salt])).digest();
  let plain;
  try {
    const decipher = crypto.createDecipheriv('aes-128-cbc', derived.subarray(0, 16),
                                              derived.subarray(16, 32));
    plain = Buffer.concat([decipher.update(raw.subarray(38)), decipher.final()]);
  } catch (_) { failure('DECRYPT_FAILED'); }
  if (plain.length <= 64) failure('UNSUPPORTED_FORMAT');
  const expected = plain.subarray(0, 64);
  const text = plain.subarray(64);
  const actual = crypto.createHash('sha512').update(text).digest();
  if (!crypto.timingSafeEqual(expected, actual)) failure('INTEGRITY_FAILED');
  return text.toString('utf8');
}
function readSession(request) {
  if (!object(request) || typeof request.storageFile !== 'string' ||
      typeof request.machineIdFile !== 'string') failure('INVALID_FORMAT');
  let storage;
  try { storage = JSON.parse(fs.readFileSync(request.storageFile, 'utf8')); }
  catch (err) { failure(err && err.code === 'ENOENT' ? 'AUTH_MISSING' : 'UNSUPPORTED_FORMAT'); }
  if (!object(storage)) failure('UNSUPPORTED_FORMAT');
  const auth = JSON.parse(decrypt(storage[AUTH_KEY]));
  if (!object(auth)) failure('UNSUPPORTED_FORMAT');
  const token = auth.token || auth.accessToken;
  if (typeof token !== 'string' || !token || token.length > 32768 || /[\r\n]/.test(token))
    failure('TOKEN_MISSING');
  const device = deviceId(storage, request.machineIdFile);
  const expiresAt = auth.expiresAt || auth.expires_at;
  return {token, deviceId: device.value, deviceIdSource: device.source,
          expiresAt: typeof expiresAt === 'string' ? expiresAt : null};
}
let chunks = [];
let size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}),
                       () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > 65536) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    chunks = [];
    reply({ok: true, session: readSession(request)});
  } catch (err) {
    reply({ok: false, reason: (err && err.reason) || 'UNSUPPORTED_FORMAT'});
  }
});
"""


def _mac_runtime(bundle):
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
            name = plistlib.load(handle).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") \
                or "/" in name or "\\" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def find_runtime():
    override = os.environ.get("TRAE_CN_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise NoAuthError("TRAE_CN_EXE 不是可执行文件：%s" % path)
    candidates = [
        _mac_runtime("/Applications/Trae CN.app"),
        _mac_runtime(str(Path.home() / "Applications" / "Trae CN.app")),
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    raise NoAuthError("没找到 Trae CN 客户端，请先安装并登录，或用 TRAE_CN_EXE 指定")


def storage_path():
    override = os.environ.get("TRAE_CN_STORAGE")
    if override:
        return Path(override).expanduser().absolute()
    return Path.home() / "Library" / "Application Support" / "Trae CN" / STORAGE_REL_PATH


def _run_auth_helper(runtime, path):
    request = json.dumps({
        "version": 1,
        "storageFile": str(path),
        "machineIdFile": str(path.parent.parent.parent / "machineid"),
    }).encode("utf-8")
    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.run(
            [runtime, "-e", AUTH_HELPER_JS], input=request, capture_output=True,
            timeout=HELPER_TIMEOUT, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NoAuthError("无法运行 Trae CN 解密组件（%s）" % type(exc).__name__)
    if len(process.stdout) > 65536:
        raise AuthFormatError("解密组件输出异常")
    try:
        reply = json.loads(process.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AuthFormatError("解密组件未返回有效结果")
    if not isinstance(reply, dict) or reply.get("version") != 1:
        raise AuthFormatError("解密组件协议版本不兼容")
    if reply.get("ok"):
        session = reply.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("token"), str) \
                or not isinstance(session.get("deviceId"), str) or not session["deviceId"]:
            raise AuthFormatError("解密组件返回的登录态不完整")
        return session
    reason = reply.get("reason")
    if reason == "AUTH_MISSING":
        raise NoAuthError("未找到 Trae CN 登录态，请先打开客户端登录")
    if reason == "DEVICE_ID_MISSING":
        raise AuthFormatError("登录态可读，但未找到设备 ID")
    raise AuthFormatError("读取 Trae CN 登录态失败（%s）" % (reason or "HELPER_PROTOCOL"))


def load_session(attempts=3):
    if sys.platform != "darwin":
        raise NoAuthError("当前脚本仅支持 macOS 版 Trae CN")
    path = storage_path()
    if not path.is_file():
        raise NoAuthError("未找到 Trae CN 登录态：%s" % path)
    runtime = find_runtime()
    last = None
    for attempt in range(attempts):
        try:
            session = _run_auth_helper(runtime, path)
            session["_storage"] = str(path)
            _sensitive_values.add(session["token"])
            return session
        except (TraeAuthError, ValueError) as exc:
            last = exc
            if isinstance(exc, NoAuthError) or attempt + 1 >= attempts:
                raise
            time.sleep(0.25)
    raise last


class Api:
    def __init__(self, token, device_id, base_url=BASE_URL):
        self.token = token
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        _sensitive_values.add(token)

    def _raw(self, path):
        request = urllib.request.Request(
            self.base_url + path,
            data=REQUEST_BODY,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Cloud-IDE-JWT %s" % self.token,
                "x-device-id": self.device_id,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(1 << 20)
                http = response.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(1 << 20)
            except OSError:
                raw = b""
            http = exc.code
        except (urllib.error.URLError, OSError) as exc:
            return 0, None, type(exc).__name__
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = None
        return http, body, None

    def status(self, retry=True):
        attempt = 0
        while True:
            response = self._raw(STATUS_PATH)
            http = response[0]
            retryable = http == 0 or http == 429 or 500 <= http < 600
            if not retry or not retryable or attempt >= len(RETRY_DELAYS):
                return response
            time.sleep(RETRY_DELAYS[attempt])
            attempt += 1

    def claim(self):
        # 与客户端一致：领取请求绝不自动重试，结果不明时只复核状态。
        return self._raw(CLAIM_PATH)


def _result(name, report, **extra):
    out = {"result": name, "report": report,
           "needs_attention": name not in ("OK", "CLAIMED", "ALREADY", "INACTIVE", "DRY_RUN")}
    out.update(extra)
    return out


def _business_code(body):
    if isinstance(body, dict) and isinstance(body.get("code"), int) \
            and not isinstance(body.get("code"), bool):
        return body["code"]
    return None


def _api_error(http, body, err, action):
    code = _business_code(body)
    if http in (401, 403) or code == 1002:
        return _result("AUTH_REJECTED", "%s时登录态被拒绝，请打开 Trae CN 重新登录" % action,
                       http=http, code=code)
    if code == 9090:
        return _result("INACTIVE", "签到活动当前未开放", http=http, code=code)
    if http == 0:
        return _result("NETWORK", "%s请求失败（%s）" % (action, err or "network"), http=0)
    if code is not None:
        return _result("ERROR", "%s失败：业务码 %s" % (action, code), http=http, code=code)
    return _result("ERROR", "%s失败：HTTP %s" % (action, http), http=http)


def parse_status(response):
    http, body, err = response
    code = _business_code(body)
    if http != 200 or code not in (None, 0):
        return None, _api_error(http, body, err, "查询签到状态")
    if not isinstance(body, dict) or not isinstance(body.get("enable"), bool) \
            or not isinstance(body.get("checked_in"), bool):
        return None, _result("ERROR", "状态接口返回格式不兼容", http=http)
    checked = body["checked_in"] or body.get("did_checked_in") is True
    state = "signed" if checked else "disabled" if not body["enable"] else "not_signed"
    credits = body.get("credits")
    if not isinstance(credits, (int, float)) or isinstance(credits, bool) \
            or not math.isfinite(credits) or credits <= 0:
        credits = None
    return {"state": state, "enable": body["enable"], "checked_in": checked,
            "credits": credits}, None


def claim_once(api, retry=True, dry_run=False):
    status, error = parse_status(api.status(retry=retry))
    if error:
        return error
    common = {"http": 200, "credits": status["credits"]}
    if status["state"] == "signed":
        return _result("ALREADY", "今日已签到，无需重复领取", **common)
    if status["state"] == "disabled":
        return _result("INACTIVE", "签到活动当前未开放", **common)
    if dry_run:
        return _result("DRY_RUN", "[dry-run] 今日可签到，未调用领取接口", **common)

    http, body, err = api.claim()
    code = _business_code(body)
    if http == 200 and isinstance(body, dict) and code in (None, 0):
        if isinstance(body.get("code"), (str, float, bool)):
            return _result("ERROR", "领取接口返回格式不兼容", http=http)
        return _result("CLAIMED", "签到成功，已领取今日积分", **common)
    if http not in (0, 429) and not 500 <= http < 600:
        return _api_error(http, body, err, "领取积分")

    # 网络错误/5xx 可能发生在服务端已领取之后，只查状态，不盲目重复 claim。
    confirmed, status_error = parse_status(api.status(retry=False))
    if confirmed and confirmed["state"] == "signed":
        return _result("CLAIMED", "领取结果曾不确定，状态复核确认今日已签到",
                       http=http, confirmed_by_status=True, credits=confirmed["credits"])
    detail = status_error["report"] if status_error else "复核后仍未签到"
    return _result("PENDING", "领取结果不确定（%s）；未重复领取，请稍后重跑" % detail,
                   http=http)


def _redact(text):
    for secret in _sensitive_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def emit(out):
    print(_redact(json.dumps(out, ensure_ascii=False, default=str)))


def run_doctor():
    try:
        session = load_session()
    except NoAuthError as exc:
        return _result("NO_AUTH", str(exc), version=VERSION), 2
    except TraeAuthError as exc:
        return _result("AUTH_ERROR", str(exc), version=VERSION), 1
    return _result("OK", "Trae CN 登录态与设备 ID 可离线读取",
                   version=VERSION, decrypted=True, device_id_present=True,
                   device_id_source=session.get("deviceIdSource"),
                   storage=session.get("_storage"),
                   token_expires_at=session.get("expiresAt")), 0


def run(action, dry_run=False, once=False):
    if action == "doctor":
        out, code = run_doctor()
        emit(out)
        return code
    try:
        session = load_session()
    except NoAuthError as exc:
        emit(_result("NO_AUTH", str(exc)))
        return 2
    except TraeAuthError as exc:
        emit(_result("AUTH_ERROR", str(exc)))
        return 1

    api = Api(session["token"], session["deviceId"])
    if action == "status":
        status, out = parse_status(api.status(retry=not once))
        if status:
            labels = {"signed": "今日已签到", "not_signed": "今日可签到", "disabled": "活动未开放"}
            out = _result("OK", labels[status["state"]], http=200, **status)
        emit(out)
        return 0 if out["result"] in ("OK", "INACTIVE") else 1
    out = claim_once(api, retry=not once, dry_run=dry_run)
    emit(out)
    return 0 if out["result"] in ("CLAIMED", "ALREADY", "INACTIVE", "DRY_RUN") else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    once = "--once" in argv
    argv = [arg for arg in argv if arg not in ("--dry-run", "--once")]
    action = argv[0] if argv else "auto"
    if len(argv) > 1 or action not in ("auto", "status", "doctor"):
        emit(_result("ERROR", "用法：trae_signin.py [auto|status|doctor] [--once] [--dry-run]"))
        return 2
    try:
        return run(action, dry_run=dry_run, once=once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit(_result("ERROR", "脚本运行异常（%s）" % type(exc).__name__))
        return 2


if __name__ == "__main__":
    sys.exit(main())

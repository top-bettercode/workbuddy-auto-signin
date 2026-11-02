#!/usr/bin/env python3
"""发送飞书消息通知（自建应用 + tenant_access_token）。

凭据与收信方从环境变量读取（由调用方 workbuddy-daily.sh / qoder-daily.sh 从
~/.zshrc 中提取并 export）：
  FEISHU_SELF_APP_ID           自建应用 app_id
  FEISHU_SELF_APP_SECRET       自建应用 app_secret
  FEISHU_NOTIFY_RECEIVE_ID     收信方 ID（由环境变量配置）
  FEISHU_NOTIFY_RECEIVE_ID_TYPE ID 类型（默认 user_id，可选 open_id/union_id/email/chat_id）

仅使用标准库（urllib），默认校验证书。失败一律打印到 stderr 并以非 0 退出，
由调用方决定是否致命（自动化任务里不致命）。
"""
import json
import os
import ssl
import sys
import time
import uuid
import urllib.request
import urllib.error

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TOKEN_CACHE = "/tmp/feishu_tenant_token_cache.json"
# 文本消息上限 150KB；这里远小于此，给个安全截断。
MAX_TEXT = 4000


def _post(url, payload, headers):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_tenant_token(app_id, app_secret):
    now = time.time()
    # 复用缓存（提前 5 分钟刷新）
    try:
        with open(TOKEN_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("token") and cache.get("expire_at", 0) - 300 > now:
            return cache["token"]
    except Exception:
        pass

    resp = _post(
        TOKEN_URL,
        {"app_id": app_id, "app_secret": app_secret},
        {"Content-Type": "application/json; charset=utf-8"},
    )
    if resp.get("code") != 0:
        raise RuntimeError("获取 tenant_access_token 失败: %s" % json.dumps(resp, ensure_ascii=False))
    token = resp["tenant_access_token"]
    expire = int(resp.get("expire", 7200))
    try:
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "expire_at": now + expire}, f)
        os.chmod(TOKEN_CACHE, 0o600)
    except Exception:
        pass
    return token


def send_text(token, receive_id, receive_id_type, text):
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n…(已截断)"
    content = json.dumps({"text": text}, ensure_ascii=False)
    body = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": content,
        "uuid": uuid.uuid4().hex,
    }
    url = "%s?receive_id_type=%s" % (SEND_URL, receive_id_type)
    headers = {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json; charset=utf-8",
    }
    resp = _post(url, body, headers)
    if resp.get("code") != 0:
        raise RuntimeError("发送消息失败: %s" % json.dumps(resp, ensure_ascii=False))
    return resp


def main():
    app_id = os.environ.get("FEISHU_SELF_APP_ID", "")
    app_secret = os.environ.get("FEISHU_SELF_APP_SECRET", "")
    receive_id = os.environ.get("FEISHU_NOTIFY_RECEIVE_ID", "")
    receive_id_type = os.environ.get("FEISHU_NOTIFY_RECEIVE_ID_TYPE", "user_id")

    if not app_id or not app_secret:
        print("feishu_notify: 缺少 FEISHU_SELF_APP_ID / FEISHU_SELF_APP_SECRET，跳过通知", file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print("用法: feishu_notify.py <消息文本>", file=sys.stderr)
        return 2

    msg = sys.argv[1]
    try:
        token = get_tenant_token(app_id, app_secret)
        send_text(token, receive_id, receive_id_type, msg)
        print("feishu_notify: 已发送 (to %s/%s)" % (receive_id_type, receive_id))
        return 0
    except Exception as e:  # noqa: BLE001
        print("feishu_notify: 发送失败: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

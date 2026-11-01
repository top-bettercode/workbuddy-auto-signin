import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import qoder_signin as qoder


NOW = int(time.time())
WINDOW = {"startAt": NOW - 3600, "endAt": NOW + 3600}


def campaign(key, status, action="CLAIM_BENEFIT", amount=100, **extra):
    row = {
        "campaignId": "id-" + key,
        "campaignKey": key,
        "actionType": action,
        "claimStatus": status,
        "benefit": {"kind": "CREDITS", "amount": amount,
                    "validity": {"mode": "RELATIVE_DAYS", "days": 30}},
    }
    row.update(WINDOW)
    row.update(extra)
    return row


class FakeApi:
    def __init__(self, payload, claim_reply=None, list_code=200, claim_code=200):
        self.payload = payload
        self.claim_reply = claim_reply
        self.list_code = list_code
        self.claim_code = claim_code
        self.claimed = []

    def campaigns(self, retry=True):
        return self.list_code, self.payload, None

    def claim(self, campaign_id, retry=True):
        self.claimed.append(campaign_id)
        return self.claim_code, self.claim_reply, None


class SelectionTests(unittest.TestCase):
    def test_only_active_claimable_benefits_are_claimed(self):
        payload = {"campaigns": [
            campaign("today", "CLAIMABLE"),
            campaign("done", "CLAIMED"),
            campaign("expired", "CLAIMABLE", startAt=NOW - 7200, endAt=NOW - 3600),
            campaign("future", "CLAIMABLE", startAt=NOW + 60, endAt=NOW + 3600),
            campaign("details", "CLAIMED", action="VIEW_DETAILS", amount=None),
        ]}
        picked = qoder.select_campaigns(payload, NOW)
        self.assertEqual([c["campaignKey"] for c in picked["todo"]], ["today"])
        self.assertEqual([c["campaignKey"] for c in picked["claimed"]], ["done"])
        self.assertEqual([c["campaignKey"] for c in picked["benefit"]],
                         ["today", "done", "expired", "future"])

    def test_missing_or_malformed_payload_is_empty(self):
        for payload in (None, {}, {"campaigns": None}, {"campaigns": ["x"]}):
            picked = qoder.select_campaigns(payload, NOW)
            self.assertEqual(picked["todo"], [])
            self.assertEqual(picked["benefit"], [])


class FormattingTests(unittest.TestCase):
    def test_amount_and_validity(self):
        row = campaign("today", "CLAIMABLE")
        self.assertEqual(qoder._amount(row), 100)
        self.assertEqual(qoder._validity(row), "领取后 30 天有效")
        self.assertIsNone(qoder._amount({"benefit": {"amount": "100"}}))
        self.assertEqual(qoder._validity({}), "")

    def test_window_uses_utc_plus_8(self):
        start = int(datetime(2026, 10, 20, 10, 0, tzinfo=qoder.TZ_CN).timestamp())
        end = start + 24 * 3600 - 60
        row = {"startAt": start, "endAt": end}
        self.assertEqual(qoder._window(row), "10-20 10:00 ~ 10-21 09:59")

    def test_token_expiry(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(qoder._token_expired({"expiresAt": future}))
        self.assertTrue(qoder._token_expired({"expiresAt": past}))
        self.assertFalse(qoder._token_expired({}))
        self.assertIsNone(qoder._parse_iso("not-a-date"))


class AuthDiscoveryTests(unittest.TestCase):
    def test_missing_auth_dirs_raises_no_auth_not_format_error(self):
        # 消息里的字面 %APPDATA% 若不转义，% 格式化会先抛 ValueError，
        # 用户看到的就不是 NO_AUTH 而是“脚本运行异常”。
        # load_session() 现在按平台分派，这里直接测 Windows 分支。
        with mock.patch.object(qoder, "find_auth_dirs", return_value=[]):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._load_session_windows()
        self.assertIn("%APPDATA%", str(ctx.exception))
        self.assertEqual(ctx.exception.looked_in, [])


class WindowsLoadSessionTests(unittest.TestCase):
    """Windows 分支的编排逻辑。

    这里不碰 DPAPI（只在 Windows 上可用），但把「扫描目录 → 逐个读凭据 →
    失败归并」这条骨架钉住：平台分派重构后，Windows 行为不应有任何变化。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "com.qodercn.app.stable"
        self.root.mkdir()
        (self.root / "auth.v1.dat").write_bytes(b"v10" + b"\x00" * 64)

    def test_returns_first_readable_session(self):
        with mock.patch.object(qoder, "find_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "_read_auth_store_retry",
                               return_value={"token": "dt-windows"}) as read:
            session = qoder._load_session_windows()
        self.assertEqual(session["token"], "dt-windows")
        self.assertEqual(session["_auth_dir"], str(self.root))
        read.assert_called_once_with(self.root)

    def test_directory_without_credentials_is_no_auth(self):
        (self.root / "auth.v1.dat").unlink()
        with mock.patch.object(qoder, "find_auth_dirs", return_value=[self.root]):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._load_session_windows()
        self.assertIn("尚未登录", str(ctx.exception))

    def test_format_error_is_aggregated_not_swallowed(self):
        with mock.patch.object(qoder, "find_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "_read_auth_store_retry",
                               side_effect=qoder.AuthFormatError("DECRYPT_FAILED")):
            with self.assertRaises(qoder.AuthFormatError) as ctx:
                qoder._load_session_windows()
        self.assertIn("DECRYPT_FAILED", str(ctx.exception))


class LinuxLoadSessionTests(unittest.TestCase):
    """Linux 分支的编排逻辑，与 WindowsLoadSessionTests 同构。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "com.qodercn.app.stable"
        self.root.mkdir()
        (self.root / "auth.v1.dat").write_bytes(b"v11" + b"\x00" * 64)

    def test_returns_first_readable_session(self):
        with mock.patch.object(qoder, "find_linux_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "find_linux_runtime", return_value="/usr/lib/electron43/electron"), \
             mock.patch.object(qoder, "_read_linux_session_retry",
                               return_value={"token": "dt-linux"}) as read:
            session = qoder._load_session_linux()
        self.assertEqual(session["token"], "dt-linux")
        self.assertEqual(session["_auth_dir"], str(self.root))
        read.assert_called_once_with("/usr/lib/electron43/electron", self.root)

    def test_directory_without_credentials_is_no_auth(self):
        (self.root / "auth.v1.dat").unlink()
        with mock.patch.object(qoder, "find_linux_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "find_linux_runtime", return_value="/fake/electron"):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._load_session_linux()
        self.assertIn("尚未登录", str(ctx.exception))

    def test_format_error_is_aggregated_not_swallowed(self):
        with mock.patch.object(qoder, "find_linux_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "find_linux_runtime", return_value="/fake/electron"), \
             mock.patch.object(qoder, "_read_linux_session_retry",
                               side_effect=qoder.AuthFormatError("DECRYPT_FAILED")):
            with self.assertRaises(qoder.AuthFormatError) as ctx:
                qoder._load_session_linux()
        self.assertIn("DECRYPT_FAILED", str(ctx.exception))


class LinuxRuntimeTests(unittest.TestCase):
    def test_qoder_exe_override_must_be_executable(self):
        with mock.patch.dict(os.environ, {"QODER_EXE": "/nonexistent/electron"}):
            with self.assertRaises(qoder.NoAuthError):
                qoder.find_linux_runtime()

    def test_no_runtime_found_is_no_auth(self):
        with mock.patch.dict(os.environ, {"QODER_EXE": ""}), \
             mock.patch.object(qoder.glob, "glob", return_value=[]):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder.find_linux_runtime()
        self.assertIn("Electron", str(ctx.exception))


class LinuxHelperTests(unittest.TestCase):
    def reply(self, payload, **kwargs):
        with mock.patch.object(qoder.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=json.dumps(payload).encode("utf-8"), **kwargs)
            return qoder._run_linux_auth_helper("/fake/electron", "/f/auth.v1.dat"), run

    def test_writes_helper_app_and_returns_session(self):
        captured = {}

        def record(argv, **kwargs):
            captured["argv"] = argv
            captured["package"] = json.loads(
                (Path(argv[-2]) / "package.json").read_text())
            return mock.Mock(stdout=json.dumps(
                {"ok": True, "version": 1, "session": {"token": "tk"}}).encode("utf-8"))

        with mock.patch.object(qoder.subprocess, "run", side_effect=record) as run:
            session = qoder._run_linux_auth_helper("/fake/electron", "/f/auth.v1.dat")
        self.assertEqual(session["token"], "tk")
        argv = captured["argv"]
        self.assertEqual(argv[0], "/fake/electron")
        self.assertIn("--headless", argv)
        self.assertEqual(argv[-1], "/f/auth.v1.dat")
        self.assertEqual(captured["package"]["name"], "Qoder CN App")

    def test_failure_reasons_map_to_exceptions(self):
        for reason, cls in (("AUTH_MISSING", qoder.NoAuthError),
                            ("KEYSTORE_UNAVAILABLE", qoder.NoAuthError),
                            ("DECRYPT_FAILED", qoder.AuthFormatError)):
            with mock.patch.object(qoder.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout=json.dumps(
                    {"ok": False, "version": 1, "reason": reason}).encode("utf-8"))
                with self.assertRaises(cls):
                    qoder._run_linux_auth_helper("/fake/electron", "/f/auth.v1.dat")

    def test_timeout_is_no_auth(self):
        with mock.patch.object(qoder.subprocess, "run",
                               side_effect=qoder.subprocess.TimeoutExpired(cmd="x", timeout=1)):
            with self.assertRaises(qoder.NoAuthError):
                qoder._run_linux_auth_helper("/fake/electron", "/f/auth.v1.dat")

    def test_helper_failure_is_retried_then_reported(self):
        with mock.patch.object(qoder, "_run_linux_auth_helper",
                               side_effect=qoder.AuthFormatError("DECRYPT_FAILED")) as helper:
            with self.assertRaises(qoder.AuthFormatError):
                qoder._read_linux_session_retry("/fake/electron", Path("/tmp/x"))
        self.assertEqual(helper.call_count, 3)


class ClaimTests(unittest.TestCase):
    def test_claims_claimable_campaign(self):
        api = FakeApi({"campaigns": [campaign("today", "CLAIMABLE")]},
                      {"status": "CLAIMED", "replayed": False,
                       "benefit": {"amount": 100}})
        out = qoder.claim_once(api)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertEqual(out["credits"], 100)
        self.assertEqual(out["campaign"], "today")
        self.assertEqual(api.claimed, ["id-today"])
        self.assertFalse(out["needs_attention"])

    def test_accepts_data_wrapper_and_replay(self):
        api = FakeApi({"campaigns": [campaign("today", "CLAIMABLE", amount=None)]},
                      {"data": {"status": "CLAIMED", "replayed": True,
                                "benefit": {"amount": 100}}})
        out = qoder.claim_once(api)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertEqual(out["credits"], 100)
        self.assertIn("幂等重复", out["report"])

    def test_already_claimed_in_current_window(self):
        api = FakeApi({"campaigns": [campaign("done", "CLAIMED")]})
        out = qoder.claim_once(api)
        self.assertEqual(out["result"], "ALREADY")
        self.assertEqual(out["credits"], 100)
        self.assertEqual(api.claimed, [])
        self.assertFalse(out["needs_attention"])

    def test_dry_run_does_not_claim(self):
        api = FakeApi({"campaigns": [campaign("today", "CLAIMABLE")]})
        out = qoder.claim_once(api, dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(out["credits"], 100)
        self.assertEqual(api.claimed, [])

    def test_pending_and_inactive(self):
        pending = qoder.claim_once(FakeApi({"campaigns": [campaign("soon", "COMING_SOON")]}))
        self.assertEqual(pending["result"], "PENDING")
        self.assertTrue(pending["needs_attention"])

        inactive = qoder.claim_once(FakeApi({"campaigns": []}))
        self.assertEqual(inactive["result"], "INACTIVE")
        self.assertFalse(inactive["needs_attention"])

        expired = qoder.claim_once(FakeApi({"campaigns": [
            campaign("old", "CLAIMABLE", startAt=NOW - 7200, endAt=NOW - 3600)]}))
        self.assertEqual(expired["result"], "INACTIVE")
        self.assertFalse(expired["needs_attention"])

    def test_auth_and_http_failures(self):
        rejected = qoder.claim_once(FakeApi({}, list_code=401))
        self.assertEqual(rejected["result"], "AUTH_REJECTED")
        forbidden = qoder.claim_once(FakeApi({}, list_code=403))
        self.assertEqual(forbidden["result"], "FORBIDDEN")
        network = qoder.claim_once(FakeApi({}, list_code=0))
        self.assertEqual(network["result"], "NETWORK")

    def test_multiple_claimable_campaigns_sum_credits(self):
        rows = [campaign("a", "CLAIMABLE", amount=60),
                campaign("b", "CLAIMABLE", amount=40)]
        api = FakeApi({"campaigns": rows},
                      {"status": "CLAIMED", "benefit": {"amount": 60}})
        out = qoder.claim_once(api)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertEqual(out["campaign"], "a, b")
        self.assertEqual(out["credits"], 120)

    def test_claim_business_error_is_reported(self):
        api = FakeApi({"campaigns": [campaign("today", "CLAIMABLE")]},
                      {"status": "FAILED"}, claim_code=500)
        out = qoder.claim_once(api)
        self.assertEqual(out["result"], "ERROR")
        self.assertTrue(out["needs_attention"])
        self.assertEqual(out["campaign"], None)


class OutputTests(unittest.TestCase):
    def test_redacts_registered_secret(self):
        qoder._sensitive_values.add("secret-token")
        try:
            payload = json.loads(qoder._redact(json.dumps(
                {"report": "token=secret-token"}, ensure_ascii=False)))
            self.assertEqual(payload["report"], "token=[REDACTED]")
        finally:
            qoder._sensitive_values.discard("secret-token")


if __name__ == "__main__":
    unittest.main()

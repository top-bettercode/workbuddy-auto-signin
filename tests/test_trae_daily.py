import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SUCCESS = """import json
print(json.dumps({'result': 'ALREADY', 'report': '今日已签到，无需重复领取', 'credits': 100}))
"""
FAILURE = """import json, sys
print(json.dumps({'result': 'NETWORK', 'report': '网络失败'}))
sys.exit(1)
"""
NOTIFY = """import os, sys
with open(os.environ['NOTIFY_TEST_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(sys.argv[1] + '\\n---\\n')
sys.exit(int(os.environ.get('NOTIFY_TEST_FAIL', '0')))
"""


@unittest.skipUnless(sys.platform == "darwin", "包装脚本依赖 macOS launchd 运行环境")
class DailyNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        shutil.copy(ROOT / "trae-daily.sh", self.repo / "trae-daily.sh")
        (self.repo / "feishu_env.sh").write_text("# synthetic environment\n", encoding="utf-8")
        (self.repo / "feishu_notify.py").write_text(NOTIFY, encoding="utf-8")
        self.signin = self.repo / "trae_signin.py"
        self.signin.write_text(SUCCESS, encoding="utf-8")
        self.notifications = self.root / "notifications.log"
        self.marker = self.root / "notified.done"

    def run_daily(self, notify_fail=False):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
               "TRAE_CN_PYTHON": sys.executable,
               "TRAE_CN_NOTIFY_MARKER": str(self.marker),
               "NOTIFY_TEST_LOG": str(self.notifications),
               "NOTIFY_TEST_FAIL": "1" if notify_fail else "0"}
        return subprocess.run(["/bin/bash", str(self.repo / "trae-daily.sh")],
                              env=env, capture_output=True, text=True, timeout=60)

    def messages(self):
        if not self.notifications.exists():
            return []
        return [part for part in self.notifications.read_text(encoding="utf-8").split("---\n")
                if part]

    def test_success_once_failure_repeats_and_recovery_notifies(self):
        first = self.run_daily()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_daily()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("Trae CN 今日已签到", self.messages()[0])
        self.assertIn("本周期已领取 100 积分", self.messages()[0])
        self.assertNotIn("今日已签到，无需重复领取", self.messages()[0])
        self.assertIn("跳过飞书通知", second.stdout)

        self.signin.write_text(FAILURE, encoding="utf-8")
        failed = self.run_daily()
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("Trae CN 每日签到失败", self.messages()[-1])
        self.assertFalse(self.marker.exists())

        self.signin.write_text(SUCCESS, encoding="utf-8")
        recovered = self.run_daily()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(len(self.messages()), 3)
        self.assertTrue(self.marker.exists())

    def test_delivery_failure_is_retried_without_marker(self):
        first = self.run_daily(notify_fail=True)
        second = self.run_daily(notify_fail=True)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(len(self.messages()), 2)
        self.assertFalse(self.marker.exists())
        self.assertIn("下次运行会重发", first.stdout + second.stdout)


if __name__ == "__main__":
    unittest.main()

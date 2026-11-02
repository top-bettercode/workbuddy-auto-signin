#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux 定时任务包装脚本的回归测试（需要 bash，其余平台跳过）

覆盖：
  1. 三个脚本 bash -n 语法合法
  2. feishu_env.sh 的凭据提取：跟进 ~/.zshrc 的 . / source 引用文件、
     ~/.zshrc 自身定义优先、RECEIVE_ID_TYPE 默认 user_id
  3. 包装脚本的关键接线：source feishu_env.sh、调用 feishu_notify.py、
     「成功标记 + 失败清除」的通知闸门、可执行位
不发送真实飞书消息、不触碰真实 ~/.zshrc。
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FEISHU_ENV = os.path.join(SCRIPT_DIR, "feishu_env.sh")
WORKBUDDY_DAILY = os.path.join(SCRIPT_DIR, "workbuddy-daily-linux.sh")
QODER_DAILY = os.path.join(SCRIPT_DIR, "qoder-daily-linux.sh")

BASH_ONLY = unittest.skipUnless(
    shutil.which("bash"), "需要 bash（bash -n 与 source 语义），缺失时跳过"
)


def bash(script, *args):
    return subprocess.run(["bash", script, *args], capture_output=True, text=True)


@BASH_ONLY
class SyntaxTests(unittest.TestCase):
    def test_scripts_pass_bash_n(self):
        for path in (FEISHU_ENV, WORKBUDDY_DAILY, QODER_DAILY):
            with self.subTest(path=path):
                self.assertEqual(bash("-n", path).returncode, 0, path)

    def test_wrappers_are_executable(self):
        for path in (WORKBUDDY_DAILY, QODER_DAILY):
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IXUSR, "缺少可执行位: %s" % path)


@BASH_ONLY
class FeishuEnvTests(unittest.TestCase):
    def _extract(self, home):
        env = dict(os.environ, HOME=home)
        proc = subprocess.run(
            ["bash", "-c",
             '. "%s"; printf "%%s\\n%%s\\n%%s\\n%%s" "$FEISHU_SELF_APP_ID" '
             '"$FEISHU_SELF_APP_SECRET" "$FEISHU_NOTIFY_RECEIVE_ID" '
             '"$FEISHU_NOTIFY_RECEIVE_ID_TYPE"' % FEISHU_ENV],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.splitlines()

    def test_follows_source_reference_in_zshrc(self):
        with tempfile.TemporaryDirectory() as home:
            conf = os.path.join(home, "sys", "conf")
            os.makedirs(conf)
            with open(os.path.join(conf, ".zshrc"), "w") as f:
                f.write('export FEISHU_SELF_APP_ID="app-123"\n'
                        "export FEISHU_SELF_APP_SECRET=sec-456\n"
                        'export FEISHU_NOTIFY_RECEIVE_ID="ou-789"\n')
            with open(os.path.join(home, ".zshrc"), "w") as f:
                f.write(". $HOME/sys/conf/.zshrc\n")
            app_id, secret, receive_id, id_type = self._extract(home)
            self.assertEqual((app_id, secret, receive_id), ("app-123", "sec-456", "ou-789"))
            self.assertEqual(id_type, "user_id")

    def test_direct_definition_wins_over_sourced_file(self):
        with tempfile.TemporaryDirectory() as home:
            conf = os.path.join(home, "sys")
            os.makedirs(conf)
            with open(os.path.join(conf, "other.zshrc"), "w") as f:
                f.write('export FEISHU_SELF_APP_ID="from-source"\n')
            with open(os.path.join(home, ".zshrc"), "w") as f:
                f.write('export FEISHU_SELF_APP_ID="direct"\n'
                        ". $HOME/sys/other.zshrc\n")
            self.assertEqual(self._extract(home)[0], "direct")


@BASH_ONLY
class WrapperWiringTests(unittest.TestCase):
    """不实跑脚本（会发真实消息），只静态断言关键接线与 macOS 版一致。"""

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_both_wrappers_source_feishu_env_and_notify(self):
        for path in (WORKBUDDY_DAILY, QODER_DAILY):
            with self.subTest(path=path):
                self.assertIn(". \"$REPO/feishu_env.sh\"", self._read(path))
                self.assertIn("feishu_notify.py", self._read(path))

    def test_notify_gate_marks_success_and_clears_on_failure(self):
        for path in (WORKBUDDY_DAILY, QODER_DAILY):
            content = self._read(path)
            with self.subTest(path=path):
                self.assertIn(': > "$NOTIFY_MARKER"', content,
                              "成功送达后必须写标记，当天停发")
                self.assertIn('rm -f "$NOTIFY_MARKER"', content,
                              "失败后必须清标记，恢复成功才能再报平安")

    def test_qoder_wrapper_sources_token_envfile(self):
        content = self._read(QODER_DAILY)
        self.assertIn(".config/workbuddy-auto-signin/env", content)
        self.assertIn("set -a", content)

    def test_conversation_disables_tools_like_macos(self):
        content = self._read(WORKBUDDY_DAILY)
        self.assertIn("--tools ''", content,
                      "CodeBuddy 对话必须禁用工具（与 macOS 一致），"
                      "避免定时任务里的 Agent 改动文件")


if __name__ == "__main__":
    unittest.main()

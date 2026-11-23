import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALLER = "install-trae-cn-macos.sh"
UNINSTALLER = "uninstall-trae-cn-macos.sh"
FAKE_DOCTOR = """#!/usr/bin/env python3
import sys
sys.exit(0 if sys.argv[1:] == ['doctor'] else 2)
"""
FAKE_LAUNCHCTL = """#!/bin/bash
STATE="${SHIM_STATE:?}"
mkdir -p "$STATE"
case "${1:-}" in
  print) [ -f "$STATE/loaded" ] && exit 0 || exit 1 ;;
  bootout)
    [ "${SHIM_BOOTOUT_FAIL:-0}" != "1" ] || exit 1
    rm -f "$STATE/loaded"
    ;;
  bootstrap)
    [ "${SHIM_BOOTSTRAP_FAIL:-0}" != "1" ] || exit 1
    touch "$STATE/loaded"
    ;;
esac
exit 0
"""


@unittest.skipUnless(sys.platform == "darwin", "launchd 测试仅在 macOS 运行")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo & tools"
        self.repo.mkdir()
        for name in (INSTALLER, UNINSTALLER, "trae-daily.sh"):
            shutil.copy(ROOT / name, self.repo / name)
        (self.repo / "trae_signin.py").write_text(FAKE_DOCTOR, encoding="utf-8")
        self.home = self.root / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.plist = self.home / "Library" / "LaunchAgents" / "trae-cn-daily-checkin.plist"
        self.shim = self.root / "bin"
        self.shim.mkdir()
        launchctl = self.shim / "launchctl"
        launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
        launchctl.chmod(launchctl.stat().st_mode | stat.S_IEXEC)
        self.state = self.root / "state"

    def env(self, **extra):
        env = {"HOME": str(self.home), "PATH": str(self.shim) + ":/usr/bin:/bin",
               "SHIM_STATE": str(self.state), "TRAE_CN_PYTHON": sys.executable,
               "TMPDIR": str(self.root)}
        env.update(extra)
        return env

    def run_script(self, name, **extra):
        return subprocess.run(["/bin/bash", str(self.repo / name)], cwd=self.repo,
                              env=self.env(**extra), capture_output=True, text=True,
                              timeout=60)

    def read_plist(self):
        with self.plist.open("rb") as handle:
            return plistlib.load(handle)

    def test_generates_valid_independent_schedule(self):
        result = self.run_script(INSTALLER, TRAE_CN_EXE="/tmp/Trae CN",
                                 TRAE_CN_STORAGE="/tmp/auth & files/storage.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.read_plist()
        self.assertEqual(data["Label"], "trae-cn-daily-checkin")
        self.assertEqual(data["ProgramArguments"][0], "/bin/bash")
        self.assertEqual(Path(data["ProgramArguments"][1]).name, "trae-daily.sh")
        self.assertEqual(len(data["ProgramArguments"]), 2)
        self.assertEqual(data["EnvironmentVariables"]["TRAE_CN_PYTHON"], sys.executable)
        self.assertEqual([row["Hour"] for row in data["StartCalendarInterval"]],
                         [9, 12, 15, 19, 23])
        self.assertTrue(data["RunAtLoad"])
        self.assertIn("&", data["WorkingDirectory"])
        self.assertEqual(data["EnvironmentVariables"]["TRAE_CN_EXE"], "/tmp/Trae CN")
        self.assertEqual(data["EnvironmentVariables"]["TRAE_CN_STORAGE"],
                         "/tmp/auth & files/storage.json")
        self.assertNotIn("Authorization", str(data))

    def test_bootout_failure_restores_original(self):
        original = b"<plist>ORIGINAL</plist>"
        self.plist.write_bytes(original)
        self.state.mkdir()
        (self.state / "loaded").touch()
        result = self.run_script(INSTALLER, SHIM_BOOTOUT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), original)

    def test_bootstrap_failure_rolls_back(self):
        original = b"<plist>ORIGINAL</plist>"
        self.plist.write_bytes(original)
        result = self.run_script(INSTALLER, SHIM_BOOTSTRAP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), original)

    def test_uninstall_failure_keeps_config(self):
        self.plist.write_bytes(b"<plist>x</plist>")
        self.state.mkdir()
        (self.state / "loaded").touch()
        result = self.run_script(UNINSTALLER, SHIM_BOOTOUT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.plist.exists())

    def test_uninstall_success_removes_config(self):
        self.plist.write_bytes(b"<plist>x</plist>")
        self.state.mkdir()
        (self.state / "loaded").touch()
        result = self.run_script(UNINSTALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.state / "loaded").exists())


if __name__ == "__main__":
    unittest.main()

"""本机维护工具回归：仅使用独立临时目录与虚构配置，不删除测试目录。"""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import config_history as history_module
import repo_guard


def fake_data(version):
    return {
        "ctrl.ini": ("[微信推送]\r\npushplus-token=TEST_NOTICE_VALUE_" + version + "\r\n"
                     "[测试]\r\n目录1=C:\\ExampleClient\\client.exe\r\n").encode("gbk"),
        "账号文本.txt": ("0|测试用户|TEST_ACCOUNT_" + version + "|TEST_PASSWORD_" + version +
                    "|未启动|0||0||测试|测试平台|0|0|5|6|TEST_BUYER_" + version + "\r\n").encode("gbk"),
    }


def write_data(project, data):
    for name, content in data.items():
        (project / name).write_bytes(content)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise AssertionError("测试 Git 操作失败（输出已隐藏）")
    return result.stdout


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="seer-maintenance-test-"))
        self.project = self.workspace / "project"
        self.project.mkdir()
        self.store = self.workspace / "private"
        self.manager = history_module.ConfigHistory(self.project, self.store)
        write_data(self.project, fake_data("A"))

    def init_code_repo(self):
        git(self.project, "init", "-b", "main")
        git(self.project, "config", "user.name", "Maintenance test")
        git(self.project, "config", "user.email", "test@example.invalid")
        git(self.project, "config", "core.autocrlf", "false")
        (self.project / "README.md").write_text("Test repository\n", encoding="utf-8")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-m", "Test baseline")

    def test_snapshot_preserves_gbk_crlf_and_unchanged_version(self):
        data = fake_data("A")
        sha, changed = self.manager.save("初始设置")
        self.assertTrue(changed)
        for name in history_module.FILES:
            self.assertEqual(git(self.store, "show", sha + ":" + name), data[name])
        again, changed = self.manager.save("无需重复版本")
        self.assertEqual(again, sha)
        self.assertFalse(changed)
        self.assertEqual(git(self.store, "rev-list", "--count", "HEAD").strip(), b"1")
        self.assertEqual(git(self.store, "remote"), b"")

    def test_message_cannot_contain_sensitive_value(self):
        with self.assertRaises(RuntimeError):
            self.manager.save("修改 TEST_PASSWORD_A")
        self.assertIsNone(self.manager.head())

    def test_remote_and_nested_store_are_rejected(self):
        self.manager.save("初始设置")
        git(self.store, "remote", "add", "origin", "https://example.invalid/private.git")
        with self.assertRaises(RuntimeError):
            self.manager.save("调整设置")
        with self.assertRaises(RuntimeError):
            history_module.ConfigHistory(self.project, self.project / "private")

    def test_store_validation_detects_missing_push_block(self):
        self.manager.save("初始设置")
        hook = self.store / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.is_file())
        hook.unlink()
        with self.assertRaisesRegex(RuntimeError, "拒绝推送"):
            self.manager.history()
        with self.assertRaisesRegex(RuntimeError, "拒绝推送"):
            self.manager.restore(self.manager.head())
        self.manager.save("重装拒绝推送 hook")
        self.assertTrue(self.manager.push_block_installed())
        self.manager.history()  # 重装后只读命令恢复可用

    def test_private_store_blocks_push_to_explicit_url(self):
        self.manager.save("初始设置")
        destination = self.workspace / "push-test.git"
        result = subprocess.run(["git", "init", "--bare", str(destination)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        result = subprocess.run(["git", "-C", str(self.store), "push", str(destination), "HEAD:refs/heads/main"],
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"local configuration history must not be pushed", result.stderr)
        self.assertEqual(git(destination, "for-each-ref"), b"")

    def test_preview_has_no_writes_and_restore_saves_current_bytes(self):
        old, _ = self.manager.save("第一版")
        newer = fake_data("B")
        write_data(self.project, newer)
        result = self.manager.restore(old)
        self.assertFalse(result["apply"])
        self.assertEqual(self.manager.head(), old)
        self.assertEqual(history_module.stable_read(self.project), newer)
        result = self.manager.restore(old, apply=True)
        self.assertEqual(history_module.stable_read(self.project), fake_data("A"))
        backup = self.manager.head()
        self.assertTrue(backup.startswith(result["backup_commit"]))
        for name in history_module.FILES:
            self.assertEqual(git(self.store, "show", backup + ":" + name), newer[name])

    def test_running_application_blocks_restore_before_changes(self):
        old, _ = self.manager.save("第一版")
        newer = fake_data("B")
        write_data(self.project, newer)
        with patch.object(self.manager, "application_running", return_value=True):
            with self.assertRaises(RuntimeError):
                self.manager.restore(old, apply=True)
        self.assertEqual(self.manager.head(), old)
        self.assertEqual(history_module.stable_read(self.project), newer)

    @unittest.skipUnless(os.name == "nt", "Windows 进程探测")
    def test_windows_process_probe_handles_module_launch_and_unknown_state(self):
        entry = self.project / "seer_console" / "main.py"
        entry.parent.mkdir()
        entry.write_text("# 虚构入口，不执行\n", encoding="utf-8")
        real_run = subprocess.run

        def probe_with(command_line, name="python.exe", executable_path=None):
            def literal(value):
                return "$null" if value is None else "'" + value.replace("'", "''") + "'"

            prefix = ("function Get-CimInstance { [pscustomobject]@{ Name=" + literal(name) +
                      "; CommandLine=" + literal(command_line) +
                      "; ExecutablePath=" + literal(executable_path) + " } }; ")

            def isolated_run(command, **kwargs):
                return real_run(command[:-1] + [prefix + command[-1]], **kwargs)

            with patch.object(history_module.subprocess, "run", side_effect=isolated_run):
                return self.manager.application_running()

        self.assertTrue(probe_with('python.exe -m seer_console.main'))
        self.assertTrue(probe_with('pythonw.exe "C:\\Example\\seer_console\\main.py"'))
        self.assertFalse(probe_with('python.exe unrelated_tool.py'))
        with self.assertRaises(RuntimeError):
            probe_with(None)
        # 项目目录内启动的可执行体（打包成 exe 的形式）也算在运行；
        # 探测脚本里的项目路径取自 resolve() 后的目录，这里必须用同一形态，
        # 否则 8.3 短名（K-SIR~1）与长名（K - Sir）不匹配，断言会假失败。
        self.assertTrue(probe_with(None, name="SeerConsole.exe",
                                   executable_path=str(self.manager.project / "dist" / "SeerConsole.exe")))
        self.assertFalse(probe_with('python.exe unrelated_tool.py', name="other.exe",
                                    executable_path="C:\\Windows\\System32\\other.exe"))

    def test_piped_history_output_is_utf8(self):
        message = "迁移前初始配置快照"
        self.manager.save(message)
        # 刻意去掉 PYTHONUTF8/PYTHONIOENCODING：管道下 Python 会退回系统 ANSI 代码页，
        # 工具必须自己切到 UTF-8，中文才不会被按 UTF-8 读取的程序解成乱码。
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "config_history.py"),
             "--project", str(self.project), "--store", str(self.store), "history"],
            capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(message, result.stdout.decode("utf-8"))

    def test_piped_guard_output_is_utf8(self):
        self.init_code_repo()
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "repo_guard.py"), "check",
             "--project", str(self.project)],
            capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("未输出敏感值", result.stdout.decode("utf-8"))

    def test_concurrent_change_blocks_apply_after_backup(self):
        old, _ = self.manager.save("第一版")
        original = fake_data("B")
        write_data(self.project, original)
        with patch.object(history_module, "stable_read", side_effect=[original, fake_data("C")]):
            with self.assertRaises(RuntimeError):
                self.manager.restore(old, apply=True)
        self.assertNotEqual(self.manager.head(), old)
        self.assertEqual(history_module.stable_read(self.project), original)

    def test_second_file_failure_rolls_back_first_file(self):
        old, _ = self.manager.save("第一版")
        newer = fake_data("B")
        write_data(self.project, newer)
        real_write = history_module.atomic_write
        state = {"failed": False}

        def fail_once(path, content):
            if path.resolve() == (self.project / "账号文本.txt").resolve() and not state["failed"]:
                state["failed"] = True
                raise OSError("模拟替换失败")
            return real_write(path, content)

        with patch.object(history_module, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "已回滚"):
                self.manager.restore(old, apply=True)
        self.assertTrue(state["failed"])
        self.assertEqual(history_module.stable_read(self.project), newer)

    def test_unstable_snapshot_and_invalid_revision_are_rejected(self):
        with patch.object(Path, "read_bytes", side_effect=[b"a", b"a", b"b", b"b"] * 3):
            with self.assertRaises(RuntimeError):
                history_module.stable_read(self.project)
        self.manager.save("第一版")
        with self.assertRaises(RuntimeError):
            self.manager.restore("--all")

    def test_guard_blocks_staged_secret_without_echoing_value(self):
        self.init_code_repo()
        (self.project / "README.md").write_text("TEST_PASSWORD_A\n", encoding="utf-8")
        git(self.project, "add", "README.md")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError):
            repo_guard.check(self.project)
        self.assertIn("README.md", stderr.getvalue())
        self.assertNotIn("TEST_PASSWORD_A", stderr.getvalue())

    def test_guard_checks_removed_file_in_old_history(self):
        self.init_code_repo()
        private_doc = self.project / "information"
        private_doc.mkdir()
        (private_doc / "notes.md").write_text("Local-only reference\n", encoding="utf-8")
        git(self.project, "add", "information/notes.md")
        git(self.project, "commit", "-m", "Test old private reference")
        git(self.project, "update-index", "--force-remove", "information/notes.md")
        git(self.project, "commit", "-m", "Remove from latest tree")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            repo_guard.check(self.project)

    def test_installed_hook_blocks_forbidden_commit(self):
        self.init_code_repo()
        tool_dir = self.project / "tools"
        tool_dir.mkdir()
        (tool_dir / "repo_guard.py").write_bytes((ROOT / "tools" / "repo_guard.py").read_bytes())
        repo_guard.install(self.project)
        git(self.project, "add", "ctrl.ini")
        before = git(self.project, "rev-parse", "HEAD")
        result = subprocess.run(["git", "-C", str(self.project), "commit", "-m", "Blocked test"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), before)
        self.assertNotIn(b"TEST_NOTICE_VALUE_A", result.stderr)

    def test_guard_allows_clean_repo_and_examples(self):
        self.init_code_repo()
        (self.project / "ctrl.example.ini").write_text("[微信推送]\npushplus-token=\n", encoding="gbk")
        git(self.project, "add", "ctrl.example.ini")
        result = repo_guard.check(self.project)
        self.assertEqual(result["commits"], 1)
        self.assertFalse(repo_guard.forbidden_path("账号文本.example.txt"))
        for name in ("AGENTS.md", "tools/AGENTS.md", "ctrl.ini.bak", ".env", "information/readme.md"):
            self.assertTrue(repo_guard.forbidden_path(name))

    def test_guard_forbids_media_and_notes_at_any_depth(self):
        for name in ("shot.png", "seer_console/ui/shot.png", "docs/note.md", "tools/notes.md",
                     "backup.zip", "docs/设计.docx", "docs/页面.html", "information/readme.md"):
            self.assertTrue(repo_guard.forbidden_path(name), name)
        for name in ("README.md", "tools/README.md", "LICENSE", "ctrl.example.ini",
                     "账号文本.example.txt", "seer_console/core/engine.py", "start-seer-console.ps1"):
            self.assertFalse(repo_guard.forbidden_path(name), name)

    def test_guard_blocks_staged_screenshot_in_subdirectory(self):
        self.init_code_repo()
        screenshot = self.project / "seer_console" / "ui" / "shot.png"
        screenshot.parent.mkdir(parents=True)
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n fake screenshot")
        git(self.project, "add", "seer_console/ui/shot.png")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError):
            repo_guard.check(self.project)
        self.assertIn("seer_console/ui/shot.png", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)

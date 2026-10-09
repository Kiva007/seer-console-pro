"""重启后的前台激活验证：不读真实配置，不启动或关闭账号进程。"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import win32con
import win32gui
from PySide6.QtWidgets import QApplication, QWidget
from seer_console import main as entry
from seer_console.ui.main_window import MainWindow, build_restart_relay_script


class RestartForegroundTests(unittest.TestCase):
    def test_relay_passes_flag_and_permissions_without_real_launch_or_delete(self):
        source = build_restart_relay_script("test-root", "test-main.py", "test-log.txt")
        namespace = {"__name__": "test_relay", "__file__": "test-relay.py"}
        exec(compile(source, "test-relay.py", "exec"), namespace)
        namespace["log"] = Mock()
        with patch("subprocess.Popen", return_value=SimpleNamespace(pid=123)) as launch, \
                patch("time.sleep"), patch("os.remove") as remove, \
                patch("ctypes.windll.user32.AllowSetForegroundWindow", return_value=1) as allow:
            namespace["main"]()
        self.assertEqual(launch.call_args.args[0], [sys.executable, "test-main.py", "--activate-after-restart"])
        allow.assert_called_once_with(123)
        remove.assert_called_once_with("test-relay.py")

    def test_activation_scheduled_after_show_and_only_for_restart(self):
        for restart in (False, True):
            events = []
            callbacks = []
            window = Mock()
            window.show.side_effect = lambda: events.append("show")
            window.activate_after_restart.side_effect = lambda: events.append("activate")
            app = Mock()

            def run_loop():
                events.append("event_loop")
                for callback in callbacks:
                    callback()
                return 0

            app.exec.side_effect = run_loop
            argv = ["main.py"] + (["--activate-after-restart"] if restart else [])
            with patch.object(entry, "MainWindow", return_value=window), \
                    patch.object(entry, "QApplication", return_value=app), \
                    patch.object(entry, "QFont"), \
                    patch.object(entry.QTimer, "singleShot", side_effect=lambda ms, cb: callbacks.append(cb)), \
                    patch.object(sys, "argv", argv), patch.object(sys, "exit"):
                entry.main()
            self.assertEqual(events, ["show", "event_loop"] + (["activate"] if restart else []))

    def test_foreground_denial_is_reported_without_topmost(self):
        window = Mock()
        window.isMinimized.return_value = True
        window.winId.return_value = 111
        with patch("seer_console.ui.main_window.win32gui.SetWindowPos") as position, \
                patch("seer_console.ui.main_window.WindowFinder.set_foreground_window", return_value=False), \
                patch("seer_console.ui.main_window.win32gui.GetForegroundWindow", return_value=222):
            self.assertFalse(MainWindow.activate_after_restart(window))
        window.showNormal.assert_called_once_with()
        self.assertEqual(position.call_args.args[1], win32con.HWND_TOP)
        self.assertEqual(position.call_args.args[-1],
                         win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        self.assertIn("被系统拒绝", window.log.call_args.args[0])

    def test_real_test_windows_activate_without_staying_on_top(self):
        # 仅创建两个空测试窗口，验证真实前台句柄和样式，不实例化业务主窗口。
        app = QApplication.instance() or QApplication([])

        class TestWindow(QWidget):
            activate_after_restart = MainWindow.activate_after_restart

            def log(self, message):
                self.messages.append(message)

        target, other = TestWindow(), QWidget()
        target.messages = []
        previous = win32gui.GetForegroundWindow()
        try:
            for window in (target, other):
                window.resize(180, 80)
                window.show()
            app.processEvents()
            target_hwnd, other_hwnd = int(target.winId()), int(other.winId())
            win32gui.SetForegroundWindow(other_hwnd)
            self.assertEqual(win32gui.GetForegroundWindow(), other_hwnd)
            self.assertTrue(target.activate_after_restart(), target.messages)
            app.processEvents()
            self.assertEqual(win32gui.GetForegroundWindow(), target_hwnd)
            self.assertFalse(win32gui.GetWindowLong(target_hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)
            # 后续激活另一窗口后，目标应允许它排在上面，不继续抢焦点。
            win32gui.SetForegroundWindow(other_hwnd)
            app.processEvents()
            self.assertEqual(win32gui.GetForegroundWindow(), other_hwnd)
            self.assertFalse(win32gui.GetWindowLong(target_hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)
        finally:
            target.close()
            other.close()
            if previous and win32gui.IsWindow(previous):
                try:
                    win32gui.SetForegroundWindow(previous)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()

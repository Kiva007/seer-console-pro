"""日志序号与运行页一致；所有进程、窗口操作使用桩对象。"""

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 导入仅定义类与 Win32 签名；不加载真实配置、账号，不实例化主窗口。
from PySide6.QtWidgets import QApplication
from seer_console.core.account import Account, AccountManager
from seer_console.core.engine import AutomationEngine, ClientType
from seer_console.core.watchdog import StallWatchdog, DONE, GONE
from seer_console.ui.tab_active import TabActiveAccount


class LogAccountSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 仓库第 2、3、5 个账号显示在运行页；第 2 个关闭日常开关。
        self.manager = AccountManager("unused-test-accounts.txt")
        self.manager.accounts.extend([
            Account(selected=False),
            Account(selected=True, daily_enabled=False, peak_enabled=True),
            Account(selected=True, daily_enabled=True, peak_enabled=False),
            Account(selected=False),
            Account(selected=True, daily_enabled=True, peak_enabled=True),
        ])
        self.logs = []
        self.engine = AutomationEngine(
            self.manager,
            SimpleNamespace(get_all_window_config=lambda: {}, get_seal_names=lambda: {}),
            Mock(), Mock(), log_callback=self.logs.append,
        )

    def test_matches_visible_table_and_refresh(self):
        page = TabActiveAccount()
        indices = [i for i, account in enumerate(self.manager.accounts) if account.selected]
        page.refresh_account_table(self.manager.get_active_accounts(), indices)
        for row, index in enumerate(indices):
            self.assertEqual(str(self.engine._account_seq(index)), page.account_table.item(row, 0).text())
        # 勾选及顺序改变后，重新取当前序号，不缓存旧映射。
        self.manager.accounts[0].selected = True
        self.assertEqual(self.engine._account_seq(4), 4)
        self.manager.accounts.reverse()
        self.assertEqual(self.engine._account_seq(0), 1)
        self.assertEqual(self.engine._account_seq(1), "未在运行页")
        self.assertEqual(self.engine._account_seq(-1), "未在运行页")
        self.assertEqual(self.engine._account_seq(99), "未在运行页")

    def test_close_logs_run_number_but_operates_warehouse_slot(self):
        window = SimpleNamespace(login_cancelled=threading.Event())
        self.engine._daily_windows[4] = window
        self.engine.stop_single(4, ClientType.LEI_XIAO_YI)
        self.assertEqual(self.logs[-1], "---关闭单个账号[3]---")
        self.engine._process_manager.kill.assert_called_once_with(window)
        self.assertTrue(window.login_cancelled.is_set())
        self.assertIsNone(self.engine._daily_windows[4])
        self.engine.stop_single(2, ClientType.CHONG_JU_PEAK)
        self.assertEqual(self.logs[-1], "序号[2]无运行中的窗口，跳过关闭")

    def test_tile_logs_use_same_sequence_for_daily_and_peak(self):
        with patch("seer_console.core.engine.set_window_pos"):
            self.engine._tile_lei_xiao_yi(4, 111)
            self.engine._tile_chong_ju(4, 222, is_peak=True)
            self.engine._tile_chong_ju(4, 333, is_peak=False)
        self.assertEqual(len(self.logs), 3)
        self.assertTrue(all(message.startswith("平铺序号[3]") for message in self.logs))

    def test_pending_login_and_failure_summary_use_run_numbers(self):
        window = SimpleNamespace(login_finished=threading.Event())
        for method, windows in (
            ("_retile_lei_selfcheck", self.engine._daily_windows),
            ("_retile_chongju_daily_selfcheck", self.engine._daily_windows),
            ("_retile_peak_selfcheck", self.engine._peak_windows),
        ):
            windows[4] = window
            getattr(self.engine, method)([4])
            self.assertIn("序号[3]", self.logs[-1])
        self.engine._notify_stall_unfinished("daily", "日常自检", [2, 4])
        self.assertIn("[2]未知、[3]未知", self.logs[-1])
        self.engine._notifier.notify_stall_unfinished.assert_called_once_with(
            "daily", "[2]未知、[3]未知")

    def test_watchdog_completion_and_closed_logs(self):
        states = {2: DONE, 4: GONE}
        probe = Mock(side_effect=lambda index: states[index])
        completed = Mock()
        StallWatchdog(
            poll_s=1, fallback_s=60, round1_s=10, round2_s=40,
            probe=probe, restart_round=Mock(), push_unfinished=Mock(),
            all_complete=completed, log=self.engine._log,
            sleep_wait=lambda _: False, clock=lambda: 0,
            account_seq=self.engine._account_seq,
        ).run([2, 4])
        self.assertIn("序号[2]任务完成", self.logs)
        self.assertIn("序号[3]窗口已关闭，停止跟踪", self.logs)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [2, 4])
        completed.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

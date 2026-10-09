"""主窗口：两个标签页、菜单、所有信号连接、自动化引擎"""

import os
import subprocess
import sys
import tempfile
import time
import threading
import ctypes
import win32con
import win32gui
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMenuBar, QMenu, QMessageBox,
    QApplication, QWidget, QVBoxLayout,
)
from PySide6.QtCore import Qt, Signal, QTimer, QFileSystemWatcher
from PySide6.QtGui import QAction, QFont

from .tab_active import TabActiveAccount
from .tab_config import TabConfig
from .tab_settings import TabSettings
from .widgets import LogWidget

from ..core.account import AccountManager, Account
from ..core.config import ConfigManager
from ..core.client import ProcessManager, GameWindow, WindowFinder
from ..core.engine import AutomationEngine, ClientType
from ..core.notifier import Notifier
from ..core.scheduler import Scheduler
from ..core.version import extract_version, get_latest_version_path


def build_restart_relay_script(root: str, target: str, log_path: str,
                               delay: float = 2.5) -> str:
    """生成重启中继脚本内容（纯 python，由 _restart_app 写入 %TEMP% 后用本解释器拉起）。

    为什么不用 cmd 中继（2026-10 实测翻车）：subprocess.Popen(["cmd","/c",relay])
    时 Python 的 list2cmdline 会给中继串整体加引号、把内嵌引号转义成 \\"，
    而 cmd 不认 \\" 转义 —— cd /d \\"...\\" 直接报「卷标语法错误」、
    start \\"\\" \\"...\\" 拿不到可执行文件，新实例永远起不来（用户症状：
    确认重启后只见 ping 命令行一闪即关、中控台没起来）。诊断期三变体
    （DETACHED / 无 DETACHED / 无 ping）全红，直接 Popen(pythonw) 即绿。
    纯 python 中继零 cmd、零 start、零引号转义；由本解释器（pythonw，
    GUI 子系统）拉起还顺带消除 ping 黑窗。

    行为：记日志 → sleep(delay) 等旧进程退出 → Popen([本解释器, target],
    cwd=root) 拉起新实例并传递前台激活权限 → 自删脚本。文本为合法 UTF-8 python 源码。
    """
    return f'''# -*- coding: utf-8 -*-
"""Seer restart relay: wait for old process to exit, then launch the new one."""
import ctypes
import os
import subprocess
import sys
import time

ROOT = {root!r}
TARGET = {target!r}
LOG = {log_path!r}
DELAY = {delay!r}


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + "\\n")
    except OSError:
        pass


def main():
    log("relay start pid=%d exe=%s" % (os.getpid(), sys.executable))
    log("target=%s exists=%s" % (TARGET, os.path.isfile(TARGET)))
    time.sleep(DELAY)
    try:
        p = subprocess.Popen([sys.executable, TARGET, "--activate-after-restart"], cwd=ROOT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        log("launched pid=%d" % p.pid)
    except Exception as e:
        log("launch FAILED: %r" % e)
        return
    # 旧实例授权中继，中继再仅授权新实例；权限失败不影响启动。
    try:
        allowed = ctypes.windll.user32.AllowSetForegroundWindow(p.pid)
        log("foreground permission pid=%d allowed=%d" % (p.pid, allowed))
    except Exception as e:
        log("foreground permission FAILED: %r" % e)
    try:
        os.remove(__file__)
    except OSError:
        pass


if __name__ == "__main__":
    main()
'''


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """Seer中控台Pro 主窗口"""

    # 信号：从工作线程安全输出日志
    log_signal = Signal(str)
    ui_update_signal = Signal(str, object)  # (action, data)
    refresh_signal = Signal()  # 引擎状态变化（如登录后账号名更新）→ 刷新运行页

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Seer中控台Pro")
        self.setMinimumSize(100, 70)
        # 高度容纳运行页：账号表格(上) + 客户端三列九行(上右) + 控制面板/日志(下)
        self.resize(905, 620)

        # ---- 核心模块 ----
        work_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._work_dir = work_dir

        self.account_manager = AccountManager(
            os.path.join(work_dir, "账号文本.txt")
        )
        self.config_manager = ConfigManager(
            os.path.join(work_dir, "ctrl.ini")
        )
        self.process_manager = ProcessManager()
        self.notifier = Notifier()
        self.scheduler = Scheduler()

        self._refreshing = False

        # 先加载配置与账号：引擎窗口数组按账号数预分配，必须在引擎创建前加载
        self.config_manager.load()
        self.account_manager.load()
        self.notifier.token = self.config_manager.get_wechat_token()

        # ---- 自动化引擎 ----
        self.engine = AutomationEngine(
            account_manager=self.account_manager,
            config_manager=self.config_manager,
            process_manager=self.process_manager,
            notifier=self.notifier,
            log_callback=self.log,
            on_state_changed=lambda: self.refresh_signal.emit(),
        )

        # ---- 先建 UI + 连信号，再加载数据 + 刷新 ----
        self._build_ui()
        self._connect_signals()
        self._init_data()
        self._refresh_ui()

        # 自检定时器（日常25分钟 / 巅峰5分钟，固定周期 MVP）
        self._daily_check_timer = QTimer(self)
        self._daily_check_timer.timeout.connect(self._run_daily_selfcheck)
        self._peak_check_timer = QTimer(self)
        self._peak_check_timer.timeout.connect(self._run_peak_selfcheck)
        self._sync_selfcheck_timers()

        self._start_scheduler()
        self._log_account_summary()
        self._setup_account_file_watcher()
        self.log("初始化完成")

    # ================================================================
    # 数据初始化
    # ================================================================

    def _init_data(self) -> None:
        """加载配置和账号数据"""
        # 加载配置
        self.config_manager.load()
        self.notifier.token = self.config_manager.get_wechat_token()

        # 加载账号
        self.account_manager.load()
        total = len(self.account_manager.accounts)
        self.log(f"已加载 {total} 个账号")

        # 初始化队列
        self.engine._load_config()

        # 定位各客户端目录下最新版本软件，自动切换到最新路径（版本目录扫描）
        # 首次运行时迁移旧节 → [客户端列表]，持久化
        self._check_client_versions()
        self.config_manager.save()

    def _check_client_versions(self) -> None:
        """检查客户端版本更新（启动时执行）：扫描客户端列表条目目录下最新版本。
        XM（XryM）用专属版本规则比较（X.X.XX 末段两位数 = X.X.X.X，如 2.5.21=2.5.2.1，
        保证服务器新版 2.5.3 正确大于旧版文件名 2.5.21）；其余客户端用通用规则。"""
        entries = self.config_manager.get_client_entries()
        changed = False
        for e in entries:
            if not e.path:
                continue
            # XM 用专属版本规则；其余客户端通用规则
            new_path = self._get_latest_version_path(e.path, "xrym" in e.name.lower())
            if new_path and new_path != e.path:
                e.path = new_path
                changed = True
                self.log(f"[版本更新] {e.name} → {new_path}")
        if changed:
            self.config_manager.set_client_entries(entries)
            self.config_manager.save()

    # ================================================================
    # UI构建
    # ================================================================

    def _build_ui(self) -> None:
        """构建主界面"""
        # 标签页：运行页 / 仓库页 / 配置页
        self.tab_widget = QTabWidget()

        self.tab_active = TabActiveAccount()
        self.tab_config = TabConfig()
        self.tab_settings = TabSettings()

        self.tab_widget.addTab(self.tab_active, "运行页")
        self.tab_widget.addTab(self.tab_config, "仓库页")
        self.tab_widget.addTab(self.tab_settings, "配置页")

        self.setCentralWidget(self.tab_widget)

        # 菜单
        self._build_menu()

    def _build_menu(self) -> None:
        """构建菜单栏"""
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu("文件")

        import_action = QAction("重新导入账号", self)
        import_action.triggered.connect(self._reload_accounts)
        file_menu.addAction(import_action)

        reload_config = QAction("重新加载配置", self)
        reload_config.triggered.connect(self._reload_config)
        file_menu.addAction(reload_config)

        file_menu.addSeparator()

        open_cfg_action = QAction("打开配置文件", self)
        open_cfg_action.triggered.connect(
            lambda: os.startfile(self.config_manager.file_path)
        )
        file_menu.addAction(open_cfg_action)

        # 工具菜单
        tools_menu = menubar.addMenu("工具")

        open_dir_action = QAction("打开工作目录", self)
        open_dir_action.triggered.connect(
            lambda: os.startfile(self._work_dir)
        )
        tools_menu.addAction(open_dir_action)

        clear_log_action = QAction("清空日志", self)
        clear_log_action.triggered.connect(
            lambda: self.tab_active.log_widget.clear()
        )
        tools_menu.addAction(clear_log_action)

        tools_menu.addSeparator()

        restart_action = QAction("重启", self)
        restart_action.setToolTip("保存配置并重启中控台（改完功能代码后方便立即生效）")
        restart_action.triggered.connect(self._restart_app)
        tools_menu.addAction(restart_action)

        # 帮助菜单
        help_menu = menubar.addMenu("帮助")

        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ================================================================
    # 信号连接
    # ================================================================

    def _connect_signals(self) -> None:
        """连接所有信号和槽"""
        # 日志信号跨线程安全
        self.log_signal.connect(self.tab_active.log)
        # 引擎状态变化（登录后账号名更新等）→ 刷新运行页（跨线程信号，主线程槽）
        self.refresh_signal.connect(self._refresh_run_table)

        # ====== 运行页标签页信号 ======
        ta = self.tab_active

        # 日常（雷小伊）批量操控
        ta.daily_batch_run.connect(self._on_daily_batch_run)
        ta.daily_batch_stop.connect(self._on_daily_batch_stop)
        ta.daily_batch_restart.connect(self._on_daily_batch_restart)
        # 巅峰（重聚）批量操控
        ta.peak_batch_run.connect(self._on_peak_batch_run)
        ta.peak_batch_stop.connect(self._on_peak_batch_stop)
        ta.peak_batch_restart.connect(self._on_peak_batch_restart)
        # 单号操控（右键菜单）
        ta.account_control.connect(self._on_account_control)

        # 排列按钮
        ta.btn_daily_tile.clicked.connect(self._on_daily_activate_tile)
        ta.btn_peak_tile.clicked.connect(self._on_peak_activate_tile)

        # 定时设置变更
        for cb in [ta.cb_daily_timed, ta.cb_daily_restart, ta.cb_peak_timed, ta.cb_peak_restart]:
            cb.toggled.connect(self._on_timing_changed)
        for cmb in [ta.cmb_daily_h, ta.cmb_daily_m, ta.cmb_peak_h, ta.cmb_peak_m]:
            cmb.currentTextChanged.connect(self._on_timing_changed)
        # 自动更新勾选 → 保存对应客户端的启动更新开关（日常三种/XryM/巅峰）
        ta.cb_daily_auto_update.toggled.connect(self._on_daily_auto_update_toggled)
        ta.cb_peak_auto_update.toggled.connect(self._on_peak_auto_update_toggled)
        # 现行日常/巅峰下拉框切换 → 写回配置并联动刷新
        ta.current_daily_changed.connect(self._on_current_daily_changed)
        ta.current_peak_changed.connect(self._on_current_peak_changed)

        # 客户端列表信号（两列三大行，按钮作用于当前分类行）
        ta.client_run_requested.connect(self._on_client_run)
        ta.client_path_set_requested.connect(self._on_client_path_set)
        ta.client_update_requested.connect(self._on_client_update)

        # ====== 配置标签页信号 ======
        self.tab_config.account_added.connect(self._on_account_added)
        self.tab_config.account_removed.connect(self._on_account_removed)
        self.tab_config.account_moved.connect(self._on_account_moved)
        self.tab_config.account_updated.connect(self._on_account_updated)
        self.tab_config.account_edited.connect(self._on_account_edited)
        self.tab_config.save_run_requested.connect(self._on_save_run)

        # ====== 配置页信号 ======
        ts = self.tab_settings
        ts.save_config_requested.connect(self._on_save_config_requested)

        # ====== 仓库页信号（打开账号文本按钮，原配置页辅助组移入）======
        self.tab_config.open_account_file.connect(
            lambda: os.startfile(self.account_manager.file_path)
        )

        # ====== 自检重启开关 → 同步自检定时器 ======
        ta.cb_daily_restart.toggled.connect(self._sync_selfcheck_timers)
        ta.cb_peak_restart.toggled.connect(self._sync_selfcheck_timers)

    # ================================================================
    # UI刷新
    # ================================================================

    def _refresh_ui(self) -> None:
        """刷新所有UI数据（不触发定时保存）"""
        self._refreshing = True
        ta = self.tab_active

        # --- 标签页①：运行页 ---
        self._refresh_run_table()

        # 客户端列表（三种角色）
        self._refresh_client_table()

        # 定时设置
        daily_cfg = self.config_manager.get_client_config("雷小伊")
        peak_cfg = self.config_manager.get_client_config("重聚")

        ta.cb_daily_timed.setChecked(daily_cfg.timed_start)
        ta.cb_daily_restart.setChecked(daily_cfg.auto_restart)
        ta.cmb_daily_h.setCurrentText(daily_cfg.start_hour.zfill(2))
        ta.cmb_daily_m.setCurrentText(daily_cfg.start_minute.zfill(2))

        ta.cb_peak_timed.setChecked(peak_cfg.timed_start)
        ta.cb_peak_restart.setChecked(peak_cfg.auto_restart)
        ta.cmb_peak_h.setCurrentText(peak_cfg.start_hour.zfill(2))
        ta.cmb_peak_m.setCurrentText(peak_cfg.start_minute.zfill(2))

        # 自动更新勾选：状态来自现行日常客户端的启动更新键（三种日常全开放）+
        # 巅峰启动更新键（勾选=批量启动检测到新版自动下载并切换新版继续登录；
        # 不勾选=点「取消」用当前版本继续运行；单号启动/自检重启不触发更新）
        self._sync_auto_update_checks()

        # --- 标签页②：仓库页（全部账号）---
        self.tab_config.refresh_all_accounts(
            self.account_manager.accounts, self.config_manager.get_seal_names())

        self._refreshing = False

        # --- 标签页③：配置页（客户端管理 + 雷小伊任务勾选 + 巅峰任务勾选）---
        self.tab_settings.set_client_entries(self.config_manager.get_client_entries())

        # 雷小伊任务勾选策略（多 profile：全局/新号/特定账号）
        ts_lei = self.tab_settings
        ts_lei.set_lei_task_accounts(self.account_manager.accounts)
        ts_lei.set_lei_task_profiles(
            self.config_manager.get_all_lei_task_profiles())

        # 巅峰运行选项（多 profile：全局/新号/特定账号，勾选项按账号生效）
        ts_lei.set_peak_run_accounts(self.account_manager.accounts)
        ts_lei.set_peak_run_profiles(
            self.config_manager.get_all_peak_run_profiles())

    def _refresh_run_table(self) -> None:
        """刷新运行页账号表格（数据源=get_active_accounts，记录 accounts 下标）。
        顺带刷新客户端列表：引擎 XM 自动更新会写回配置路径，版本号列需同步显示"""
        active = self.account_manager.get_active_accounts()
        accounts = self.account_manager.accounts
        index_map = {id(a): i for i, a in enumerate(accounts)}
        indices = [index_map.get(id(a), -1) for a in active]
        self.tab_active.refresh_account_table(active, indices)
        self._refresh_client_table()

    def _refresh_client_table(self) -> None:
        """刷新运行页客户端列表：按客户端列表条目分类为 登录器/日常/巅峰 三行，版本取自路径文件名"""
        def get_ver(path: str) -> str:
            if not path or not os.path.isfile(path):
                return "--"
            return extract_version(os.path.basename(path)) or "--"

        entries = self.config_manager.get_client_entries()
        data = []
        for e in entries:
            data.append({
                "name": e.name,
                "path": e.path,
                "ver": get_ver(e.path) if e.path else "--",
                "kind": e.kind,
            })
        self.tab_active.refresh_client_table(data)
        # 现行日常/巅峰下拉框选中（运行页选择，is_daily/is_peak 标记）
        daily_idx = next((i for i, e in enumerate(entries) if e.is_daily), None)
        peak_idx = next((i for i, e in enumerate(entries) if e.is_peak), None)
        self.tab_active.set_current_software(daily_idx, peak_idx)

    def _sync_auto_update_checks(self) -> None:
        """自动更新勾选框状态：来自现行日常客户端的启动更新键（三种日常全开放，
        现行日常 kind 切换时读对应键）+ 巅峰启动更新键。_refresh_ui 与运行页
        下拉框切换现行后调用。"""
        cm = self.config_manager
        ta = self.tab_active
        if self._daily_client_kind() == "xrym":
            ta.cb_daily_auto_update.setChecked(cm.get_xrym_auto_update())
        elif self._daily_client_kind() == "chongju":
            ta.cb_daily_auto_update.setChecked(cm.get_chongju_daily_auto_update())
        else:
            ta.cb_daily_auto_update.setChecked(cm.get_lei_auto_update())
        ta.cb_daily_auto_update.setEnabled(True)
        ta.cb_peak_auto_update.setChecked(cm.get_peak_auto_update())

    def _reload_accounts(self) -> None:
        """重新加载账号数据"""
        self.account_manager.load()
        self._touch_account_mtime()
        self._refresh_ui()
        self.log("账号数据已重新加载")

    # ================================================================
    # 账号文件外部变化监听（闲鱼自动发货写入账本 → 自动重载，防覆盖丢行）
    # ================================================================

    def _setup_account_file_watcher(self) -> None:
        """监听账号文本.txt 的外部变化，防抖 500ms 后自动重载。

        - 同时监听文件本身与所在目录：闲鱼侧用「临时文件 + os.replace」
          原子替换写入，文件路径监听会失效，靠目录监听兜底并在重载时补挂。
        - 自身保存通过 mtime 比对跳过（_save_accounts_self 会记录新 mtime）。
        """
        fp = self.account_manager.file_path
        self._account_loaded_mtime = None
        self._touch_account_mtime()

        self._account_fs_watcher = QFileSystemWatcher(self)
        watch_paths = [os.path.dirname(fp) or "."]
        if os.path.isfile(fp):
            watch_paths.append(fp)
        self._account_fs_watcher.addPaths(watch_paths)
        self._account_fs_watcher.directoryChanged.connect(self._on_account_file_changed)
        self._account_fs_watcher.fileChanged.connect(self._on_account_file_changed)

        self._account_reload_timer = QTimer(self)
        self._account_reload_timer.setSingleShot(True)
        self._account_reload_timer.setInterval(500)
        self._account_reload_timer.timeout.connect(self._reload_accounts_from_watch)

    def _on_account_file_changed(self, *_) -> None:
        """文件/目录变化信号 → 启动防抖定时器"""
        self._account_reload_timer.start()

    def _reload_accounts_from_watch(self) -> None:
        """防抖后重载账号（外部变化才触发：mtime 与上次加载/保存时不一致）"""
        fp = self.account_manager.file_path
        # 文件被原子替换（os.replace）后路径监听失效，补挂
        if os.path.isfile(fp) and fp not in self._account_fs_watcher.files():
            self._account_fs_watcher.addPath(fp)
        if not os.path.isfile(fp):
            return
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            return
        if mtime == self._account_loaded_mtime:
            return  # 自身保存或内容无变化
        self._account_loaded_mtime = mtime
        self.account_manager.load()
        self._refresh_run_table()
        self.tab_config.refresh_all_accounts(
            self.account_manager.accounts, self.config_manager.get_seal_names())
        self.log("检测到账号文件外部变化，已自动重载")
        self._log_account_summary()

    def _touch_account_mtime(self) -> None:
        """记录账号文件当前 mtime（加载/保存后调用，用于跳过自触发）"""
        try:
            self._account_loaded_mtime = os.path.getmtime(self.account_manager.file_path)
        except OSError:
            self._account_loaded_mtime = None

    def _save_accounts_self(self) -> None:
        """保存账号文件并记录 mtime（防止外部变化监听被自触发）"""
        self.account_manager.save()
        self._touch_account_mtime()

    def _reload_config(self) -> None:
        """重新加载配置"""
        self.config_manager.load()
        self._refresh_ui()
        self.log("配置数据已重新加载")

    def _restart_app(self) -> None:
        """重启中控台（工具菜单）：保存配置 → 拉起脱离父进程的延时新实例 → 退出。

        实现要点（2026-10 修复静默失败）：中继为纯 python 脚本（写入 %TEMP%，
        由本解释器 pythonw 拉起，GUI 子系统无窗口）。不用 cmd「中继串」——
        Popen 列表传参会把它转义成 cmd 不认的 \\" 引号，cd /d 与 start 双双
        解析失败，重启只剩 ping 黑窗一闪、新实例起不来（诊断实测全红）。
        新实例入口 seer_console\\main.py、cwd=仓库根（ctrl.ini 所在目录，
        从 config_manager.file_path 反推），与 start-seer-console.ps1 一致；
        中继 sleep 2.5s 等旧进程退出后再拉新，避免两实例短暂并存；
        中继自带日志（%TEMP%\\seer_restart_relay.log），失败可回读定位。
        """
        reply = QMessageBox.question(
            self, "重启",
            "确定重启中控台吗？\n\n重启前会自动保存配置；"
            "正在运行的批量任务会被中断。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.config_manager.save()
        except Exception as e:
            self.log(f"[重启] 配置保存失败：{e}")
        root = os.path.dirname(os.path.abspath(self.config_manager.file_path))
        script = os.path.join(root, "seer_console", "main.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "重启", f"未找到入口文件：\n{script}")
            return
        relay_path = os.path.join(tempfile.gettempdir(),
                                  "seer_restart_relay.py")
        log_path = os.path.join(tempfile.gettempdir(),
                                "seer_restart_relay.log")
        try:
            with open(relay_path, "w", encoding="utf-8") as f:
                f.write(build_restart_relay_script(root, script, log_path))
            relay = subprocess.Popen(
                [sys.executable, relay_path],
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                ),
                close_fds=True,
            )
        except Exception as e:
            QMessageBox.critical(self, "重启", f"拉起新进程失败：{e}")
            return
        try:
            if not ctypes.windll.user32.AllowSetForegroundWindow(relay.pid):
                self.log("[重启] 前台激活权限未获允许，新窗口仍会尝试置前")
        except Exception as e:
            self.log(f"[重启] 传递前台激活权限失败：{e}")
        self.log("正在重启中控台…")
        QApplication.quit()

    def activate_after_restart(self) -> bool:
        """新实例显示后一次性置前激活，不改变置顶状态、位置或尺寸。"""
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        hwnd = int(self.winId())
        try:
            # HWND_TOP 只调整普通 Z 序；NOACTIVATE 让焦点请求单独执行。
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
            WindowFinder.set_foreground_window(hwnd)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            self.log("[重启] 窗口已置前，但前台激活被系统拒绝")
        except Exception as e:
            self.log(f"[重启] 窗口置前激活失败：{e}")
        return False

    # ================================================================
    # 定时器
    # ================================================================

    def _start_scheduler(self) -> None:
        """启动定时器"""
        daily_cfg = self.config_manager.get_client_config("雷小伊")
        peak_cfg = self.config_manager.get_client_config("重聚")

        # 日常定时
        if daily_cfg.timed_start:
            try:
                h = int(daily_cfg.start_hour)
                m = int(daily_cfg.start_minute)
                self.scheduler.add_task(
                    "daily", h, m, True,
                    lambda: self._on_scheduled_daily(),
                )
            except ValueError:
                pass

        # 巅峰定时
        if peak_cfg.timed_start:
            try:
                h = int(peak_cfg.start_hour)
                m = int(peak_cfg.start_minute)
                self.scheduler.add_task(
                    "peak", h, m, True,
                    lambda: self._on_scheduled_peak(),
                )
            except ValueError:
                pass

        self.scheduler.start()

    def _on_scheduled_daily(self) -> None:
        """定时启动日常"""
        self.log("--- 定时启动日常 ---")
        self._on_daily_batch_run()

    def _on_scheduled_peak(self) -> None:
        """定时启动巅峰"""
        self.log("--- 定时启动巅峰 ---")
        self._on_peak_batch_run()

    # ================================================================
    # 公共日志方法（线程安全）
    # ================================================================

    def log(self, message: str) -> None:
        """线程安全的日志输出"""
        self.log_signal.emit(message)

    # ================================================================
    # 自动化操作回调 → 委托给引擎（全部子线程，不阻塞 UI）
    # ================================================================

    def _daily_client_kind(self) -> str:
        """现行日常客户端类型：'xrym' | 'chongju' | 'lei'（按客户端列表现行日常条目判断）"""
        if self.config_manager.is_xrym_daily():
            return "xrym"
        if self.config_manager.is_chongju_daily():
            return "chongju"
        return "lei"

    def _daily_client_type(self) -> ClientType:
        """现行日常的 ClientType（重聚日常→CHONG_JU_DAILY，否则→LEI_XIAO_YI）"""
        return ClientType.CHONG_JU_DAILY if self._daily_client_kind() == "chongju" else ClientType.LEI_XIAO_YI

    def _on_daily_batch_run(self) -> None:
        """日常批量运行：按现行日常客户端分发（雷小伊 / 重聚日常 / XryM）"""
        exe_path = self.config_manager.get_current_daily_path()
        if not exe_path:
            self.log("错误：现行日常客户端路径未配置")
            return
        selfcheck = self.tab_active.cb_daily_restart.isChecked()
        kind = self._daily_client_kind()
        if kind == "xrym":
            threading.Thread(
                target=self.engine.launch_xrym_all,
                args=(exe_path, selfcheck), daemon=True).start()
        else:
            ct = self._daily_client_type()
            threading.Thread(
                target=self.engine.launch_all,
                args=(ct, exe_path, selfcheck), daemon=True).start()

    def _on_daily_batch_stop(self) -> None:
        if self._daily_client_kind() == "xrym":
            threading.Thread(target=self.engine.stop_xrym, daemon=True).start()
        else:
            threading.Thread(target=self.engine.stop, args=(self._daily_client_type(),), daemon=True).start()

    def _on_daily_batch_restart(self) -> None:
        if self._daily_client_kind() == "xrym":
            exe_path = self.config_manager.get_current_daily_path()
            if exe_path:
                threading.Thread(target=self.engine.restart_xrym, args=(exe_path,), daemon=True).start()
        else:
            threading.Thread(target=self.engine.restart_selected, args=(self._daily_client_type(),), daemon=True).start()

    def _on_peak_batch_run(self) -> None:
        """巅峰（现行巅峰客户端）批量运行"""
        exe_path = self.config_manager.get_current_peak_path()
        if not exe_path:
            self.log("错误：现行巅峰客户端路径未配置")
            return
        selfcheck = self.tab_active.cb_peak_restart.isChecked()
        threading.Thread(
            target=self.engine.launch_all,
            args=(ClientType.CHONG_JU_PEAK, exe_path, selfcheck), daemon=True).start()

    def _on_peak_batch_stop(self) -> None:
        threading.Thread(target=self.engine.stop, args=(ClientType.CHONG_JU_PEAK,), daemon=True).start()

    def _on_peak_batch_restart(self) -> None:
        threading.Thread(target=self.engine.restart_selected, args=(ClientType.CHONG_JU_PEAK,), daemon=True).start()

    def _on_account_control(self, index: int, op: str, client: str) -> None:
        """单号操控（右键菜单）：index=accounts下标, op=run/stop/restart, client=日常/巅峰
        日常按现行日常客户端分发（雷小伊 / 重聚日常 / XryM）"""
        if client == "巅峰":
            ct = ClientType.CHONG_JU_PEAK
            exe_path = self.config_manager.get_current_peak_path()
            daily = False
        else:  # 日常
            daily = True
            kind = self._daily_client_kind()
            ct = self._daily_client_type()
            exe_path = self.config_manager.get_current_daily_path()
            xrym = (kind == "xrym")

        if op == "run":
            if not exe_path:
                self.log(f"错误：{client}客户端路径未配置")
                return
            if daily and xrym:
                threading.Thread(target=self.engine.launch_xrym_account, args=(index, exe_path), daemon=True).start()
            else:
                threading.Thread(target=self.engine.launch_single, args=(index, ct, exe_path), daemon=True).start()
        elif op == "stop":
            if daily and xrym:
                threading.Thread(target=self.engine.stop_xrym_single, args=(index,), daemon=True).start()
            else:
                threading.Thread(target=self.engine.stop_single, args=(index, ct), daemon=True).start()
        elif op == "restart":
            if not exe_path:
                self.log(f"错误：{client}客户端路径未配置")
                return
            if daily and xrym:
                threading.Thread(target=self.engine.restart_xrym_single, args=(index, exe_path), daemon=True).start()
            else:
                threading.Thread(target=self.engine.restart_single, args=(index, ct, exe_path), daemon=True).start()
        else:
            self.log(f"未知单号操作: {op}")

    def _on_daily_activate_tile(self) -> None:
        if self.config_manager.is_xrym_daily():
            threading.Thread(target=self.engine.activate_tile_xrym, daemon=True).start()
            return
        self.engine.activate_tile(self._daily_client_type())

    def _on_peak_activate_tile(self) -> None:
        self.engine.activate_tile(ClientType.CHONG_JU_PEAK)

    # ================================================================
    # 自检定时器（自检重启勾选开启）
    # ================================================================

    def _sync_selfcheck_timers(self) -> None:
        """按「自检重启」勾选状态处理自检开关。
        日常（雷小伊/XryM）与巅峰：不启用定时器，勾选=总开关，
        批量运行后由引擎启动自检流程（100秒查登录 → 失败重启 → 每轮查任务完成）。"""
        if getattr(self, "_refreshing", False):
            return
        ta = self.tab_active
        if ta.cb_daily_restart.isChecked():
            self._daily_check_timer.stop()
            self.log("日常自检已开启（批量运行后自动检查登录与任务完成）")
        else:
            self._daily_check_timer.stop()
        if ta.cb_peak_restart.isChecked():
            self._peak_check_timer.stop()
            self.log("巅峰自检已开启（批量运行后自动检查登录与任务完成）")
        else:
            self._peak_check_timer.stop()

    def _run_daily_selfcheck(self) -> None:
        threading.Thread(target=self.engine.check_and_restart_daily, daemon=True).start()

    def _run_peak_selfcheck(self) -> None:
        threading.Thread(target=self.engine.check_and_restart_peak, daemon=True).start()

    # ================================================================
    # 仓库页回调
    # ================================================================

    def _on_account_edited(self) -> None:
        """仓库页单元格/开关变化 → 刷新运行页表格（不写文件）"""
        self._refresh_run_table()

    # ================================================================
    # 辅助功能
    # ================================================================

    def _on_client_run(self, paths: list) -> None:
        """客户端列表-运行按钮：启动当前分类行全部已配置客户端"""
        for p in paths:
            if os.path.isfile(p):
                self.log(f"启动: {p}")
                os.startfile(p)
            else:
                self.log(f"文件不存在: {p}")

    def _on_client_path_set(self, idx: int, path: str) -> None:
        """客户端列表-读取按钮：应用新路径并写回配置"""
        entries = self.config_manager.get_client_entries()
        if not (0 <= idx < len(entries)):
            return
        entries[idx].path = path
        self.config_manager.set_client_entries(entries)
        self.config_manager.save()
        self._refresh_client_table()
        self.log(f"客户端路径已保存: {entries[idx].name} = {path}")

    def _on_client_update(self, idxs: list) -> None:
        """客户端列表-更新按钮：检查当前分类行全部条目的版本目录，有更新则写回并刷新版本号。
        XM 条目用专属版本规则（X.X.XX 末段两位数 = X.X.X.X），其余客户端通用规则；
        文件无变化提示「无更新」，不显示「已更新」。"""
        entries = self.config_manager.get_client_entries()
        updated = False
        for idx in idxs:
            if not (0 <= idx < len(entries)):
                continue
            e = entries[idx]
            if not e.path or not os.path.isfile(e.path):
                continue
            # XM 用专属版本规则比较（避免把路径切回数字更大的旧版如 2.5.21）
            new_path = self._get_latest_version_path(e.path, "xrym" in e.name.lower())
            if new_path != e.path:
                entries[idx].path = new_path
                updated = True
                self.log(f"已更新: {e.name} → {os.path.basename(new_path)}")
            else:
                self.log(f"无更新: {e.name}（{os.path.basename(e.path)} 已是最新版本）")
        if updated:
            self.config_manager.set_client_entries(entries)
            self.config_manager.save()
            self._refresh_client_table()

    def _on_save_config_requested(self) -> None:
        """统一「保存配置」按钮：把配置页三处编辑一次性写回 ctrl.ini。

        - 客户端管理表格（名称/路径/属性，含行内未提交的路径编辑）；
        - 雷小伊任务勾选全档（全局/新号/各特定账号，写 [雷小伊勾选] 节族）；
        - 巅峰任务勾选全档（写 [巅峰勾选] 节族）。
        引擎每次执行时现读，下一轮批量登录即生效，无需重启。日志统一一句。
        """
        ts = self.tab_settings
        ts.flush_all_caches()
        self.config_manager.set_client_entries(ts.get_client_entries())
        for key, cfg in ts.get_lei_profiles().items():
            self.config_manager.set_lei_task_profile(key, cfg)
        for key, cfg in ts.get_peak_profiles().items():
            self.config_manager.set_peak_run_profile(key, cfg)
        self.config_manager.save()
        self._refresh_client_table()
        self.log("客户端配置已保存")

    # ================================================================
    # 现行日常/巅峰切换（运行页下拉框）
    # ================================================================

    def _on_current_daily_changed(self, idx: int) -> None:
        """运行页「现行日常」下拉框切换：条目下标 → 写回 is_daily 并保存"""
        self._set_current_client("daily", idx)

    def _on_current_peak_changed(self, idx: int) -> None:
        """运行页「现行巅峰」下拉框切换：条目下标 → 写回 is_peak 并保存"""
        self._set_current_client("peak", idx)

    def _set_current_client(self, role: str, idx: int) -> None:
        """现行日常/巅峰切换写回（is_daily/is_peak 全局唯一）→ 保存 ctrl.ini →
        刷新客户端列表/下拉框选中，并联动刷新自动更新勾选态（现行日常 kind 变化时
        读对应启动更新键）。set_client_entries 顺带同步旧节键（现行日常按名称分流
        雷小伊目录7/重聚目录8、现行巅峰→重聚目录11），引擎路径读取自动跟随。"""
        entries = self.config_manager.get_client_entries()
        if not (0 <= idx < len(entries)):
            return
        for i, e in enumerate(entries):
            if role == "daily":
                e.is_daily = (i == idx)
            else:
                e.is_peak = (i == idx)
        self.config_manager.set_client_entries(entries)
        self.config_manager.save()
        self.log(f"现行{'日常' if role == 'daily' else '巅峰'}客户端已切换：{entries[idx].name}")
        self._refresh_client_table()
        self._sync_auto_update_checks()

    # ================================================================
    # 配置标签页回调
    # ================================================================

    def _on_account_added(self, new_acc: Account) -> None:
        """新增账号"""
        self.account_manager.add_account(new_acc)
        self.tab_config.refresh_all_accounts(
            self.account_manager.accounts, self.config_manager.get_seal_names())
        self._refresh_run_table()
        self.log(f"新增账号: {new_acc.mi_hao or '(空)'}")

    def _on_account_removed(self, row: int) -> None:
        """删除账号"""
        self.account_manager.remove_account(row)
        self.tab_config.refresh_all_accounts(
            self.account_manager.accounts, self.config_manager.get_seal_names())
        self._refresh_run_table()
        self.log(f"已删除第 {row + 1} 个账号")

    def _on_account_moved(self, src: int, dst: int) -> None:
        """上移/下移账号：交换 account_manager 顺序后刷新两页，保持选中被移动的行"""
        accounts = self.account_manager.accounts
        if not (0 <= src < len(accounts) and 0 <= dst < len(accounts)):
            return
        accounts[src], accounts[dst] = accounts[dst], accounts[src]
        self.tab_config.refresh_all_accounts(accounts, self.config_manager.get_seal_names())
        self.tab_config.select_account_row(dst)
        self._refresh_run_table()
        self.log(f"已调整账号顺序：第 {src + 1} 位 ↔ 第 {dst + 1} 位")

    def _on_account_updated(self) -> None:
        """保存账号"""
        self._save_accounts_self()
        self._refresh_ui()
        self.log("账号已保存")

    def _on_save_run(self) -> None:
        """保存运行：勾选账号写入账号文件并刷新运行页"""
        self._save_accounts_self()
        self._refresh_run_table()
        count = sum(1 for a in self.account_manager.accounts if a.selected)
        self.log(f"保存运行：{count} 个勾选账号已同步到运行页")
        self._log_account_summary()

    def _log_account_summary(self) -> None:
        """输出运行账号数量与临期账号日志（临期逻辑参照重构前「剩余天数」子程序）"""
        run = [a for a in self.account_manager.accounts if a.selected]
        daily = [a for a in run if a.daily_enabled]
        peak = [a for a in run if a.peak_enabled]
        self.log(f"运行账号：{len(run)} 个（日常 {len(daily)}、巅峰 {len(peak)}）")

        # 临期账号：剩余天数 < 4 天（参照重构前：巅峰/日常分别输出并推送）
        for a in run:
            if a.peak_enabled:
                d = a.peak_days_remaining()
                if 0 <= d < 4:
                    self.log(f"巅峰临期账号：{a.account_id}|{a.mi_hao}|平台名：{a.platform}|巅峰剩余天数：{d}天")
            if a.daily_enabled:
                d = a.daily_days_remaining()
                if 0 <= d < 4:
                    self.log(f"日常临期账号：{a.account_id}|{a.mi_hao}|平台名：{a.platform}|日常剩余天数：{d}天")

    def _on_timing_changed(self) -> None:
        """定时设置变更 → 写回配置 + 实时同步调度器（勾选/时间立即生效）"""
        if getattr(self, '_refreshing', False):
            return
        ta = self.tab_active
        self.config_manager.set_bool("雷小伊", "定时启动", ta.cb_daily_timed.isChecked())
        self.config_manager.set_bool("雷小伊", "自检重启", ta.cb_daily_restart.isChecked())
        self.config_manager.set("雷小伊", "时", ta.cmb_daily_h.currentText())
        self.config_manager.set("雷小伊", "分", ta.cmb_daily_m.currentText())

        self.config_manager.set_bool("重聚", "定时启动", ta.cb_peak_timed.isChecked())
        self.config_manager.set_bool("重聚", "自检重启", ta.cb_peak_restart.isChecked())
        self.config_manager.set("重聚", "时", ta.cmb_peak_h.currentText())
        self.config_manager.set("重聚", "分", ta.cmb_peak_m.currentText())

        self.config_manager.save()
        self.log("定时设置已保存")

        # 实时同步调度器：取消勾选立即停用任务，勾选/改时间立即更新（避免到点仍触发）
        self._sync_scheduler_task(
            "daily", ta.cb_daily_timed.isChecked(),
            ta.cmb_daily_h.currentText(), ta.cmb_daily_m.currentText(),
            self._on_scheduled_daily)
        self._sync_scheduler_task(
            "peak", ta.cb_peak_timed.isChecked(),
            ta.cmb_peak_h.currentText(), ta.cmb_peak_m.currentText(),
            self._on_scheduled_peak)

    def _on_daily_auto_update_toggled(self) -> None:
        """日常「自动更新」勾选变更 → 按现行日常客户端写回对应启动更新键（批量启动时：
        勾选=检测到新版自动下载并切换新版继续登录；不勾选=点「取消」用当前版本继续运行）。
        与自检一致：仅批量启动生效，单号启动不触发更新。"""
        if getattr(self, '_refreshing', False):
            return
        ta = self.tab_active
        on = ta.cb_daily_auto_update.isChecked()
        kind = self._daily_client_kind()
        if kind == "xrym":
            self.config_manager.set_bool("XryM", "xrym启动更新", on)
            name = "XM"
        elif kind == "chongju":
            self.config_manager.set_bool("重聚", "重聚日常启动更新", on)
            name = "重聚日常"
        else:
            self.config_manager.set_bool("雷小伊", "雷小伊启动更新", on)
            name = "雷小伊"
        self.config_manager.save()
        self.log(f"{name} 自动更新已开启（批量启动检测到新版将自动下载并切换新版继续登录）" if on
                 else f"{name} 自动更新已关闭（批量启动检测到新版将点「取消」用当前版本继续运行）")

    def _on_peak_auto_update_toggled(self) -> None:
        """巅峰「自动更新」勾选变更 → 写回 [重聚] 巅峰启动更新（语义同日常）。"""
        if getattr(self, '_refreshing', False):
            return
        on = self.tab_active.cb_peak_auto_update.isChecked()
        self.config_manager.set_bool("重聚", "巅峰启动更新", on)
        self.config_manager.save()
        self.log("巅峰 自动更新已开启（批量启动检测到新版将自动下载并切换新版继续登录）" if on
                 else "巅峰 自动更新已关闭（批量启动检测到新版将点「取消」用当前版本继续运行）")

    def _sync_scheduler_task(self, name: str, enabled: bool,
                             hour_text: str, minute_text: str, callback) -> None:
        """按勾选/时间实时更新调度器任务（非法时间则禁用）"""
        try:
            h = int(hour_text)
            m = int(minute_text)
            self.scheduler.add_task(name, h, m, enabled, callback)
        except ValueError:
            self.scheduler.set_enabled(name, False)

    # ================================================================
    # 版本管理
    # ================================================================

    def _get_latest_version_path(self, full_path: str, xrym_style: bool = False) -> str:
        """获取目录下最新版本的文件路径（委托 core.version 公共实现；XM 用专属版本规则）"""
        return get_latest_version_path(full_path, xrym_style)

    # ================================================================
    # 窗口关闭
    # ================================================================

    def _show_about(self) -> None:
        """显示关于对话框"""
        QMessageBox.about(
            self, "关于 Seer中控台Pro",
            "Seer中控台Pro\n"
            "赛尔号多账号中控工具\n\n"
            "从易语言到Python重构\n"
            "Powered by PySide6 + pywin32\n\n"
            "原版：8.3测试运行版.e (2352行)\n"
            "重构版：Python 3.13"
        )

    def closeEvent(self, event) -> None:
        """窗口关闭时保存配置、清理进程"""
        # 停止定时器
        self.scheduler.stop()

        # 保存配置
        try:
            self.config_manager.save()
        except Exception:
            pass

        self.log("程序已退出")
        event.accept()

"""Seer中控台自动化引擎：登录、平铺、掉线检测、重启"""

import math
import os
import time
import threading
import ctypes
import win32gui
import win32api
import win32con
import psutil
from enum import IntEnum
from typing import Optional, Callable

from .account import Account, AccountManager
from .config import ConfigManager, WINDOW_CONFIG_KEYS, LeiTaskConfig, PeakRunConfig
from .client import ProcessManager, GameWindow, WindowFinder
from .automation import (
    send_text, post_click, send_click, get_child_windows, get_control_id,
    get_control_text, switch_tab, mouse_click, set_window_pos, delay,
    check_box_set, clear_edit, external_edit_get_content,
    set_focus, select_all, input_real_keyboard,
    real_click_screen, real_click_control, foreground_window, move_window_safe,
    set_window_topmost,
)
from .notifier import Notifier
from .version import get_latest_version_path
from .watchdog import StallWatchdog, DONE, GONE, WAIT

# 重聚巅峰主窗口标题（登录成功后标题会变为「赛尔号重聚(脱机巅峰)[用户名]」形式）
_PEAK_MAIN_TITLE = "赛尔号重聚(脱机巅峰)"

# 重聚日常主窗口标题（登录成功后标题会变为用户名形式）
_CJ_DAILY_MAIN_TITLE = "重聚脱机"

# [窗口配置] 缺键/0 时的实测控件 ID 兜底表（索引 → ID）。
# 背景坑：ctrl.ini [窗口配置] 无界面入口只能手改，而 config.save() 整份按内存
# 重写（main_window 有 11 处调用）——中控台运行期间手改的 ini 会被该进程下次
# 保存时整份冲回内存旧值；缺键还会被 get_all_window_config 补成 0。
# 曾致「仅用第五」长期=0 → 匹配器 cid==0 误绑窗口真实存在的 id=0 假控件、
# set_check 静默失效且不打日志（commit 1b3b03f 加 cid=0 护栏 + 手改 ini）。
# 本表做最后防线：ini 值 0/缺键时仍用 2.8.6 实测 ID；用户显式填的非零值优先。
_WINDOW_CONFIG_DEFAULTS = {
    54: 1560,    # 54 仅用第五（2.8.1/2.8.6 实测一致：空文本 Button，标签 2080）
}


# ============================================================
# 客户端类型枚举
# ============================================================

class ClientType(IntEnum):
    LEI_XIAO_YI = 1       # 雷小伊（日常），WTWindow 窗口
    CHONG_JU_PEAK = 2     # 重聚巅峰，"赛尔号重聚(脱机巅峰)"
    CHONG_JU_DAILY = 3    # 重聚日常，"重聚脱机"


# ============================================================
# 自动化引擎
# ============================================================

class AutomationEngine:
    """赛尔号中控台自动化引擎"""

    # 超时参数（秒）
    WINDOW_WAIT_TIMEOUT = 30.0
    LOGIN_TIMEOUT_CHECKS = 11       # 11次 * 3秒 = 33秒
    LOGIN_CHECK_INTERVAL = 3.0

    def __init__(
        self,
        account_manager: AccountManager,
        config_manager: ConfigManager,
        process_manager: ProcessManager,
        notifier: Notifier,
        log_callback: Optional[Callable[[str], None]] = None,
        on_state_changed: Optional[Callable[[], None]] = None,
    ):
        self._account_manager = account_manager
        self._config_manager = config_manager
        self._process_manager = process_manager
        self._notifier = notifier
        self._log_callback = log_callback
        self._on_state_changed = on_state_changed

        # 窗口数组（按账号数量预分配）
        account_count = len(account_manager.accounts)
        self._daily_windows: list[Optional[GameWindow]] = [None] * account_count
        self._peak_windows: list[Optional[GameWindow]] = [None] * account_count
        self._xrym_windows: list[Optional[GameWindow]] = [None] * account_count  # XryM 日常

        # 线程同步
        self._key_lock = threading.Lock()
        self._exec_permit = True

        # 更新标志：-1=等待中，0=继续，1=跳过（检测到更新）
        self._update_flag = 0

        # XM 启动自动更新后记录的新路径（整批启动用新路径，避免每个账号都重复下载）
        self._xrym_updated_path = ""

        # 巅峰更新器下载新版后记录的新路径（整批后续账号/自检重启统一用新路径，
        # 对齐 XryM _xrym_updated_path 机制；每轮巅峰批量开始时清空，以配置为准）
        self._peak_updated_path = ""

        # 雷小伊自动更新下载新版后记录的新路径（整批后续账号/自检重启统一用新路径，
        # 对齐 XryM _xrym_updated_path 机制；每轮雷小伊日常批量开始时清空，以配置为准）
        self._lei_updated_path = ""

        # 重聚日常更新器下载新版后记录的新路径（机制同上；每轮重聚日常批量开始
        # 时清空，以配置为准；更新点「立即更新」后由更新器自启新版→杀孤儿→
        # 重扫目录写回配置，见 _persist_chongju_daily_path_after_update）
        self._cj_daily_updated_path = ""

        # 自检停止信号：点「全部关闭」时 set，自检线程据此退出（日常/巅峰分开，避免互相影响）
        # 新一轮批量运行开始时 clear，保证上一轮关闭留下的 set 状态不会误杀新自检
        self._daily_sc_stop = threading.Event()
        self._peak_sc_stop = threading.Event()

        # 窗口配置缓存（ctrl.ini [窗口配置] 节）
        self._config_vals: dict[int, int] = {}
        self._load_config()

    # ============================================================
    # 内部工具
    # ============================================================

    def _log(self, message: str) -> None:
        """输出日志（通过回调，格式同易语言输出_日志）"""
        if self._log_callback:
            self._log_callback(message)

    def _account_seq(self, index: int) -> int | str:
        """日志使用运行页序号；进程和队列仍使用仓库下标。"""
        accounts = self._account_manager.accounts
        if not 0 <= index < len(accounts) or not accounts[index].selected:
            return "未在运行页"
        # 与运行页一致：只按总勾选过滤，不按日常/巅峰开关分别编号。
        return sum(1 for account in accounts[:index + 1] if account.selected)

    def _load_config(self) -> None:
        """加载全部窗口配置到缓存"""
        self._config_vals = self._config_manager.get_all_window_config()
        # 巅峰刻印编号 → 名称表（[刻印名称] 节；缺项回退内置默认）
        self._seal_keywords = self._config_manager.get_seal_names()

    def _get_config(self, index: int, default: int = 0) -> int:
        """读取窗口配置值（1-based 索引）"""
        val = self._config_vals.get(index, default)
        # ini 缺键/0（含被陈旧进程冲回）时用实测控件 ID 兜底；
        # 用户显式填的非零值优先，见 _WINDOW_CONFIG_DEFAULTS 注释
        if not val and index in _WINDOW_CONFIG_DEFAULTS:
            return _WINDOW_CONFIG_DEFAULTS[index]
        return val

    def _count_queue_active_before(self, queue: list[int], up_to: int) -> int:
        """统计队列中 up_to（含）之前有多少个活动账号（queue值≥0的个数）"""
        return sum(1 for j in range(up_to + 1) if queue[j] >= 0)

    # ============================================================
    # 启动全部
    # ============================================================

    def launch_all(self, client_type: ClientType, exe_path: str, selfcheck: bool = False) -> None:
        """
        按队列依次启动所有账号。
        对应 EPL：_按钮_日常全部运行_被单击 / _按钮_巅峰全部运行_被单击 / _按钮_重聚日常全部运行_被单击
        selfcheck=True（批量运行 + 勾选「自检重启」，仅雷小伊日常生效）→ 全部启动完成后
        启动自检线程（100秒查登录 → 失败重启 → 每轮查任务完成）。
        """
        # 获取队列
        if client_type == ClientType.LEI_XIAO_YI:
            queue = self._account_manager.get_daily_queue()
            client_name = "日常"
        elif client_type == ClientType.CHONG_JU_PEAK:
            queue = self._account_manager.get_peak_queue()
            client_name = "巅峰"
        elif client_type == ClientType.CHONG_JU_DAILY:
            queue = self._account_manager.get_daily_queue()
            client_name = "重聚日常"
        else:
            self._log(f"未知的客户端类型: {client_type}")
            return

        self._log(f"---{client_name}开始运行---")
        self._exec_permit = True
        self._update_flag = -1
        # 新一轮巅峰批量：清掉上一轮的更新记录，以本轮配置路径为准（本轮再遇更新时重记）
        if client_type == ClientType.CHONG_JU_PEAK:
            self._peak_updated_path = ""
        elif client_type == ClientType.LEI_XIAO_YI:
            # 新一轮雷小伊日常批量：同上，以本轮配置路径为准
            self._lei_updated_path = ""
        elif client_type == ClientType.CHONG_JU_DAILY:
            # 新一轮重聚日常批量：同上，以本轮配置路径为准
            self._cj_daily_updated_path = ""

        # 新一轮批量运行 → 先确保旧自检线程已退出，再清除停止信号。
        # 若旧线程还在长睡眠中就 clear，旧线程睡醒后信号已清 →「复活」占住自检线程，
        # 新自检启动时被防重跳过（日志「上一轮自检仍在进行，跳过」）。
        if client_type == ClientType.CHONG_JU_PEAK:
            self._peak_sc_stop.set()
            self._join_selfcheck("_peak_selfcheck_thread", "巅峰")
            self._peak_sc_stop.clear()
        else:
            self._daily_sc_stop.set()
            self._join_selfcheck("_lei_selfcheck_thread", "日常")
            self._join_selfcheck("_xrym_selfcheck_thread", "XryM")
            self._join_selfcheck("_chongju_daily_selfcheck_thread", "重聚日常")
            self._daily_sc_stop.clear()

        # while 手动索引：巅峰更新后同账号重试（EPL :1619-1631 更新分支「到循环尾」
        # 不执行 i=i+1 → 同一账号用新版本再登录；Python 版 for+continue 会跳到下一
        # 账号，更新账号本批被漏掉，不符 EPL）
        idx = 0
        launched_any = False   # 本批是否已有账号实际启动（首个启动者负责检测更新）
        update_retries = 0     # 同账号连续更新次数（链式更新正常≤2-3次；上限防按钮失效死循环）
        while idx < len(queue):
            if not self._exec_permit:
                self._log("强制结束启动")
                return

            q_val = queue[idx]

            if q_val < 0:
                idx += 1
                continue

            account = self._account_manager.accounts[q_val]

            # 跳过已在运行的账号
            running_action = "运行巅峰" if client_type == ClientType.CHONG_JU_PEAK else "运行日常"
            if account.current_action == running_action:
                self._update_flag = 0
                idx += 1
                continue

            # 条件启动：本批第一个实际启动的账号 或 更新标志已就绪。
            # （旧版用 idx==0 判断「第一个」：队首账号不活跃被跳过后，第一个活跃
            #   账号永远不满足条件 → 不启动 → 下方 _update_flag==-1 等待无人置位
            #   → 批量启动永久挂起。改为跟踪 launched_any 修复）
            should_launch = (not launched_any or self._update_flag == 0)

            if client_type in (ClientType.CHONG_JU_PEAK, ClientType.CHONG_JU_DAILY):
                # 巅峰/重聚日常模式：每个非运行账号都启动线程（没有首账号的额外
                # 条件；重聚日常更新重试同账号时 _update_flag 置回 -1，若按
                # 「_update_flag==0 才启动」会漏启动 → 必须无条件重启）
                should_launch = True

            if should_launch:
                self._launch_one(idx, client_type, exe_path, account)
                launched_any = True

            # 等待更新检测完成（第一个账号的特殊等待期）
            while self._update_flag == -1:
                time.sleep(1)
                if not self._exec_permit:
                    self._log("强制结束启动")
                    return

            if self._update_flag == 1:
                if client_type in (ClientType.CHONG_JU_PEAK, ClientType.CHONG_JU_DAILY):
                    # 巅峰/重聚日常模式：同账号重试（巅峰对齐 EPL「到循环尾」不推进
                    # 索引；重聚日常 EPL 原为「更新后需重新运行！！」手动重跑，按
                    # 「与巅峰更新逻辑基本一样」的需求改为同款全自动重试）。
                    # 新版本路径已由 _handle_update 写回配置并记录到
                    # _peak_updated_path / _cj_daily_updated_path，重试经 _launch_one
                    # 自动切新路径（对齐 EPL 执行窗口线程每次创建进程都重读 ctrl.ini）。
                    # 置回 -1：重试账号重新走「更新检测门控」，检测通过才放行后续账号
                    update_retries += 1
                    if update_retries > 5:
                        # 链式更新正常 ≤2-3 次；连续 5 次仍提示更新 → 大概率按钮
                        # 点击失效（ID 配置错/界面改版），每次空耗 300 秒，停止批次
                        self._log(f"序号[{self._account_seq(idx)}]连续{update_retries-1}次更新后仍检测到新版本，"
                                  f"停止启动（请手动更新客户端或检查更新按钮ID配置）")
                        self._update_flag = 0
                        break
                    self._log(f"序号[{self._account_seq(idx)}]更新完成，用新版本重试登录")
                    self._update_flag = -1
                    continue
                else:
                    # 雷小伊日常：已是登录线程内全自动更新（下载+换进程，见
                    # _lei_handle_update），正常不会再走到该分支；兜底保留
                    self._log("更新后需重新运行！！")
                    break

            # 账号间隔（EPL 延迟(5000)）：分片等待，点「全部关闭」可立即中断，
            # 避免停止后最长 5 秒内又拉起一个无人管理的新进程
            if not self._wait_launch_interval(5.0):
                self._log("强制结束启动")
                return
            self._log(f"next: {idx}")

            idx += 1

        self._log(f"---{client_name}全部运行---")

        # 批量运行 → 日常/巅峰自检（勾选总开关开启时）；单号运行不进入
        if selfcheck:
            active = [i for i, q in enumerate(queue) if q >= 0]
            if active:
                if client_type == ClientType.LEI_XIAO_YI:
                    self._start_lei_selfcheck(exe_path, active)
                elif client_type == ClientType.CHONG_JU_PEAK:
                    self._start_peak_selfcheck(exe_path, active)
                elif client_type == ClientType.CHONG_JU_DAILY:
                    self._start_chongju_daily_selfcheck(exe_path, active)

    def _wait_launch_interval(self, seconds: float) -> bool:
        """批量启动的账号间隔等待（EPL 延迟(5000) 的可中断版）。
        返回 False 表示期间收到停止信号（_exec_permit=False），调用方应立即终止启动。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self._exec_permit:
                return False
            time.sleep(0.5)
        return True

    # ============================================================
    # 单账号操控（供运行页右键菜单调用）
    # ============================================================

    def _launch_one(self, i: int, client_type: ClientType, exe_path: str,
                    account: Account, allow_update: bool = True) -> Optional[GameWindow]:
        """
        启动单个账号：启动进程 → 建 GameWindow 存到对应数组 → 起登录线程。
        launch_all 与 launch_single 复用。allow_update：批量「全部运行」=True
        （开放登录期自动更新，雷小伊）；单号运行/自检重启=False（对齐 XryM 语义）。
        """
        if client_type == ClientType.CHONG_JU_PEAK:
            windows = self._peak_windows
            # 本批次中已更新过巅峰版本 → 统一用新路径启动（对齐 XryM _xrym_updated_path
            # 机制），避免后续账号/自检重启仍用旧版本反复触发更新弹窗
            if self._peak_updated_path and os.path.isfile(self._peak_updated_path):
                exe_path = self._peak_updated_path
        else:
            windows = self._daily_windows
            # 雷小伊/重聚日常：本批次中已更新过 → 统一用新路径启动（同巅峰/XryM 机制）
            if (client_type == ClientType.LEI_XIAO_YI
                    and self._lei_updated_path
                    and os.path.isfile(self._lei_updated_path)):
                exe_path = self._lei_updated_path
            elif (client_type == ClientType.CHONG_JU_DAILY
                    and self._cj_daily_updated_path
                    and os.path.isfile(self._cj_daily_updated_path)):
                exe_path = self._cj_daily_updated_path

        # 每次启动均从空的登录标识开始；不能让上一次窗口留下的昵称误导自检。
        account.display_name = ""
        gw = self._process_manager.launch(exe_path)
        if gw is None:
            err = getattr(self._process_manager, "last_error", "")
            hint = "（需以管理员身份运行）" if "740" in err or "提升" in err else ""
            self._log(f"启动进程失败: {exe_path} {err}{hint}")
            self._update_flag = 0
            return None

        windows[i] = gw
        # 记录实际启动路径（雷小伊自动更新做版本比较的基准）与更新许可
        gw.exe_path = exe_path
        gw.allow_login_update = allow_update

        if client_type == ClientType.CHONG_JU_PEAK:
            account.current_action = "运行巅峰"
        else:
            account.current_action = "运行日常"
        if self._on_state_changed:
            self._on_state_changed()  # 启动后即时刷新运行页「当前动作」列

        t = threading.Thread(
            target=self.login_window,
            args=(i, client_type, gw),
            daemon=True,
        )
        # 保留线程引用，便于诊断；自检实际以 GameWindow.login_finished/result 为准。
        gw.login_thread = t
        t.start()
        return gw

    def launch_single(self, index: int, client_type: ClientType, exe_path: str) -> None:
        """启动单个账号（供运行页右键菜单调用）"""
        if not (0 <= index < len(self._account_manager.accounts)):
            self._log(f"启动单个账号失败: 下标越界 index={index}")
            return

        account = self._account_manager.accounts[index]

        # 已在运行则跳过
        running_action = "运行巅峰" if client_type == ClientType.CHONG_JU_PEAK else "运行日常"
        if account.current_action == running_action:
            self._log(f"序号[{self._account_seq(index)}]已在运行，跳过启动")
            return

        self._update_flag = 0
        # 单号运行不开放登录期自动更新（对齐 XryM；批量「全部运行」才自动换版本）
        self._launch_one(index, client_type, exe_path, account, allow_update=False)

    def stop_single(self, index: int, client_type: ClientType) -> None:
        """关闭单个账号进程（供运行页右键菜单调用）"""
        if not (0 <= index < len(self._account_manager.accounts)):
            self._log(f"关闭单个账号失败: 下标越界 index={index}")
            return

        if client_type == ClientType.CHONG_JU_PEAK:
            windows = self._peak_windows
        else:
            windows = self._daily_windows

        if windows[index] is None:
            self._log(f"序号[{self._account_seq(index)}]无运行中的窗口，跳过关闭")
            return

        # 先取消并立刻杀掉旧进程，再等临界区释放：若先等锁，已在锁内的旧线程会
        # 继续换装/勾任务，导致日志虽然写“重启”但实际上旧窗口仍在执行。
        old_gw = windows[index]
        old_gw.login_cancelled.set()
        self._process_manager.kill(old_gw)
        # 等旧线程离开受保护的窗口操作区，再让后续重启启动新进程。
        self._key_lock.acquire()
        self._key_lock.release()

        account = self._account_manager.accounts[index]
        account.current_action = "未启动"
        account.display_name = ""
        if windows[index] is old_gw:
            windows[index] = None
        if self._on_state_changed:
            self._on_state_changed()  # 关闭后清空运行页账号名列
        self._log(f"---关闭单个账号[{self._account_seq(index)}]---")

    def restart_single(self, index: int, client_type: ClientType, exe_path: str) -> None:
        """重启单个账号 = stop_single + launch_single（供运行页右键菜单调用）"""
        self.stop_single(index, client_type)
        self.launch_single(index, client_type, exe_path)

    # ============================================================
    # XryM 日常批量/单号（现行日常=XryM 时与雷小伊共用日常控制按钮与右键菜单）
    # ============================================================

    def launch_xrym_all(self, exe_path: str, selfcheck: bool = False) -> None:
        """XryM 日常批量运行：按日常队列启动所有启用账号（账号列表账密自动登录）。
        selfcheck=True（批量运行 + 勾选「自检重启」）→ 全部账号流程完成后启动自检线程；
        右键单号运行（launch_xrym_account）不触发自检。"""
        if not os.path.isfile(exe_path):
            self._log(f"XryM 日常批量失败：客户端不存在 {exe_path}")
            return
        queue = self._account_manager.get_daily_queue()
        active = [i for i in queue if i >= 0]
        if not active:
            self._log("XryM 日常批量：没有启用日常的账号")
            return
        self._log(f"XryM 日常批量运行：{len(active)} 个账号")
        # 新一轮批量运行 → 先确保旧自检线程已退出，再清除停止信号（理由同 launch_all）
        self._daily_sc_stop.set()
        self._join_selfcheck("_xrym_selfcheck_thread", "XryM")
        self._join_selfcheck("_lei_selfcheck_thread", "日常")
        self._join_selfcheck("_chongju_daily_selfcheck_thread", "重聚日常")
        self._daily_sc_stop.clear()
        self._exec_permit = True
        threads = []
        for i in active:
            account = self._account_manager.accounts[i]
            t = threading.Thread(
                target=self._launch_xrym_one, args=(i, account, exe_path, True), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        # 批量运行 → 自检（勾选总开关开启时）；单号运行不进入
        if selfcheck and active:
            self._start_xrym_selfcheck(exe_path, active)

    def launch_xrym_account(self, index: int, exe_path: str) -> None:
        """XryM 日常单号运行（右键菜单）：启动指定账号（账号列表账密自动登录）。
        单号运行同样按「自动更新」勾选分流模式1/模式2（与批量一致）；仅自检重启不开放更新。
        重置批量关闭标志（_exec_permit/_daily_sc_stop），避免上次批量关闭的残留信号
        误拦截本次单号启动。"""
        if not (0 <= index < len(self._account_manager.accounts)):
            self._log(f"XryM 单号运行失败: 下标越界 index={index}")
            return
        account = self._account_manager.accounts[index]
        self._daily_sc_stop.clear()
        self._exec_permit = True
        t = threading.Thread(
            target=self._launch_xrym_one, args=(index, account, exe_path, True), daemon=True)
        t.start()

    def _launch_xrym_one(self, i: int, account: Account, exe_path: str,
                         allow_update: bool = True) -> None:
        """单个账号启动 XryM 客户端并登录。
        整个流程（启动进程→进入脱机→登录→换装→关闭弹窗→开始）在锁内串行执行：
        下一个软件的启动页/确认弹窗会遮挡前一个窗口的操作，必须等当前账号
        全部完成（关闭换装弹窗）后才启动下一个，避免互相遮挡。
        若本次批量中已发生过 XM 自动更新（_xrym_updated_path 非空），直接用新版本路径，
        避免后续账号重复下载更新。
        allow_update=True（批量运行/单号运行）→ 按 [XryM] xrym启动更新 勾选分流模式1/模式2；
        allow_update=False（自检重启）→ 不开放更新功能，即使勾选也只点
        「继续使用该版本」直接运行（自检不触发更新）。"""
        if not account.mi_hao or not account.password:
            self._log(f"序号[{self._account_seq(i)}]XryM日常_账号数据缺失，跳过")
            return
        with self._key_lock:
            # 批量关闭（stop_xrym）后：仍在等待锁的账号线程直接中止，不再启动新进程
            if not self._exec_permit or self._daily_sc_stop.is_set():
                self._log(f"序号[{self._account_seq(i)}]XryM日常_已收到关闭信号，跳过启动")
                return
            # XM 自动更新完成后整批使用新版本（必须在锁内读取：前一个账号在锁内完成
            # 更新后会写 _xrym_updated_path，锁外读取会拿到旧值导致后续账号仍用旧版本
            # 重新触发下载）
            if self._xrym_updated_path and os.path.isfile(self._xrym_updated_path):
                exe_path = self._xrym_updated_path
            gw = self._process_manager.launch(exe_path)
            if gw is None:
                err = getattr(self._process_manager, "last_error", "")
                hint = "（需以管理员身份运行）" if ("740" in err or "提升" in err) else ""
                self._log(f"序号[{self._account_seq(i)}]XryM日常_启动进程失败: {exe_path} {err}{hint}")
                self._update_flag = 0
                return
            gw.exe_path = exe_path  # 记录实际启动路径（更新流程定位新版 exe 的基准）
            self._xrym_windows[i] = gw
            account.current_action = "运行日常"
            if self._on_state_changed:
                self._on_state_changed()  # 启动后即时刷新运行页「当前动作」列
            self._log(f"序号[{self._account_seq(i)}]XryM日常_进程已启动 pid={gw.process_id}")
            try:
                self._login_xrym_daily(i, gw, account, allow_update)
            except Exception as e:
                self._log(f"序号[{self._account_seq(i)}]XryM日常_登录异常: {e}")
                self._update_flag = 0

    def stop_xrym(self) -> None:
        """XryM 日常批量关闭（同步 _exec_permit 与自检停止信号，与 stop() 一致）"""
        self._exec_permit = False
        self._daily_sc_stop.set()  # 通知日常自检线程退出
        self._stop_windows(self._xrym_windows, "XryM日常")
        self._join_selfcheck("_xrym_selfcheck_thread", "XryM")

    def stop_xrym_single(self, index: int) -> None:
        """XryM 日常单号关闭（右键菜单）"""
        if not (0 <= index < len(self._xrym_windows)):
            return
        if self._xrym_windows[index] is None:
            self._log(f"序号[{self._account_seq(index)}]无运行中的窗口，跳过关闭")
            return
        self._key_lock.acquire()
        try:
            if self._xrym_windows[index] is not None:
                self._process_manager.kill(self._xrym_windows[index])
        finally:
            self._key_lock.release()
        account = self._account_manager.accounts[index]
        account.current_action = "未启动"
        account.display_name = ""
        self._xrym_windows[index] = None
        if self._on_state_changed:
            self._on_state_changed()  # 关闭后清空运行页账号名列
        self._log(f"---关闭单个账号[{self._account_seq(index)}]---")

    def restart_xrym(self, exe_path: str) -> None:
        """XryM 日常批量重启 = 关闭 + 运行"""
        self.stop_xrym()
        self.launch_xrym_all(exe_path)

    def restart_xrym_single(self, index: int, exe_path: str) -> None:
        """XryM 日常单号重启（右键菜单）"""
        self.stop_xrym_single(index)
        self.launch_xrym_account(index, exe_path)

    def _login_xrym_daily(self, i: int, gw: GameWindow, account: Account,
                          allow_update: bool = False) -> None:
        """
        XryM 日常单账号登录（调用方需持有 _key_lock）：
        流程 = 启动更新处理 → 进入脱机 → 输入账密 → 登录 → 换装 → 开始任务。
        allow_update=False（单号启动/自检重启）→ 更新处理只点「继续使用该版本」不开放自动更新。
        控件操作用 Win32（XryM 的 Win32 控件 ID 与 UIA AutomationId 一致）：
        页面切换期间 UIA 遍历会抛 COMError(0x80040201) 并损坏 uiautomation 库状态，
        Win32 消息（EnumChildWindows/WM_SETTEXT/真实键盘）不受影响。
        Win32 控件 ID（2.5.21 实测）：160=进入脱机(F+U端) 1001=账号 580=密码 120=登录
        530=套装切换 350=开始 320=输出框 910=验证码输入 900=验证码提示
        更新组件页（同主窗口内嵌，2.5.21 实测）：110=本地下载 120=网盘下载 130=进度条
        140=下载状态 160=继续使用该版本 170=当前/最新版本文本
        """
        import uiautomation as ua

        mi_hao = account.mi_hao
        password = account.password
        pid = gw.process_id
        cfg = self._config_manager
        seq = self._account_seq(i)

        def cid(key: str, default: int = 0) -> int:
            try:
                return int(cfg.get_xrym_control(key, str(default)))
            except ValueError:
                return default

        self._log(f"序号[{seq}]XryM日常_登录中")

        # ① 等待主窗口（启动页）。注意：点击「进入脱机」后客户端会销毁旧窗口并重建
        #    新窗口（旧句柄失效），因此后续所有查找都要重新枚举顶层窗口（_xrym_wait_window）。
        main_hwnd = WindowFinder.find_window_by_pid(
            pid, "XM", "WTWindow", match_full_title=False, timeout=self.WINDOW_WAIT_TIMEOUT
        )
        if main_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_等待主窗口超时")
            self._update_flag = 0
            gw.timed_out = True
            return
        gw.window_handle = main_hwnd

        # ①.5 XM 启动更新组件页处理（组件内嵌于主窗口：id=110 本地下载 / id=160 继续使用）。
        #    批量运行（allow_update=True）按 [XryM] xrym启动更新 勾选分流：
        #      - 假（默认）：点「继续使用该版本」不更新直接运行，等窗口重建后继续登录
        #      - 真：点「本地下载」自动更新 → 等下载完成 → 点确定 → 关闭旧进程
        #           → 用新版本 exe 重新启动 → 继续登录流程
        #    单号启动/自检重启（allow_update=False）：不开放更新功能，只点「继续使用该版本」
        main_hwnd = self._xrym_handle_startup_update(i, gw, account, main_hwnd, allow_update)
        if main_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_更新处理失败，中止登录")
            self._update_flag = 0
            gw.timed_out = True
            return
        gw.window_handle = main_hwnd  # 更新处理可能重建了窗口/替换了进程
        pid = gw.process_id  # 自动更新可能已替换为新进程（新 pid）
        self._log(f"序号[{seq}]XryM日常_主窗口已出现，进入脱机中")

        # ② 点「进入脱机」(启动页 id=160)：真实鼠标点击首次只激活窗口，
        #    点击后窗口重建 → 重新枚举等新窗口出现账号框；未出现则重试点击
        acc_hwnd = None
        for attempt in range(4):
            mh = self._xrym_current_window(pid)
            if mh is None:
                delay(2000)
                continue
            enter_hwnd = self._find_child_by_id(mh, cid("xrym进入脱机id", 160))
            if enter_hwnd is None:
                delay(2000)
                continue
            foreground_window(mh)
            delay(400)
            real_click_control(enter_hwnd)
            new_mh, acc_hwnd = self._xrym_wait_window(
                pid, cid("xrym账号id", 1001), 15)
            if acc_hwnd is not None:
                main_hwnd = new_mh
                self._log(f"序号[{seq}]XryM日常_已进入脱机，登录区就绪(第{attempt+1}次点击)")
                # 登录区就绪后平铺窗口（照雷小伊：控件定位后、输入前；不移动也能定位，
                # 平铺仅为批量时窗口错开）
                self._tile_xrym(i, main_hwnd)
                foreground_window(main_hwnd)
                delay(300)
                break
            delay(2000)
        if acc_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_等待登录区超时")
            self._update_flag = 0
            return

        # ④ 输入账密（账号 WM_SETTEXT；密码真实键盘——拒绝窗口消息）。
        #    批量多窗口焦点竞争，输入前必须 TOPMOST 置顶当前窗口
        foreground_window(main_hwnd)
        set_window_topmost(main_hwnd)
        delay(300)
        send_text(acc_hwnd, mi_hao)
        delay(500)
        mh, pwd_hwnd = self._xrym_wait_window(pid, cid("xrym密码id", 580), 5)
        if pwd_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_未找到密码框")
            self._update_flag = 0
            return
        main_hwnd = mh
        foreground_window(main_hwnd)
        set_window_topmost(main_hwnd)
        delay(300)
        input_real_keyboard(main_hwnd, pwd_hwnd, password)
        delay(500)

        # ⑤ 点登录 (Win32 id=120)；点击后输出框出现加载/连接/结果即生效，未生效重试
        mh, login_hwnd = self._xrym_wait_window(pid, cid("xrym登录id", 120), 5)
        if login_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_未找到登录按钮")
            self._update_flag = 0
            return
        main_hwnd = mh

        def login_started() -> bool:
            _mh, _out = self._xrym_wait_window(pid, cid("xrym输出id", 320), 2)
            if _out is None:
                return False
            try:
                return any(k in get_control_text(_out) for k in (
                    "游戏数据加载", "连接成功", "登录成功", "密码错误", "登录失败", "账号不存在"))
            except Exception:
                return False
        self._xrym_click_retry(main_hwnd, login_hwnd, verify=login_started,
                               retries=3, wait_after=4.0)
        self._log(f"序号[{seq}]XryM日常_已点击登录，等待登录结果")

        # ⑥ 等登录成功（输出框 320 含「登录成功」）
        #    注：登录区常驻「请输入验证码」输入框(910)，不能作为验证码出现依据（曾误报）
        ok = False
        for _ in range(40):  # 40 × 3s = 120s
            delay(3000)
            mh, out_hwnd = self._xrym_wait_window(pid, cid("xrym输出id", 320), 2)
            if out_hwnd is None:
                continue
            main_hwnd = mh
            try:
                text = get_control_text(out_hwnd)
            except Exception:
                text = ""
            if "登录成功" in text:
                ok = True
                break
            if "密码错误" in text or "登录失败" in text or "账号不存在" in text:
                break
        if not ok:
            self._log(f"序号[{seq}]XryM日常_登录超时或失败！！")
            self._notifier.notify_login_timeout(i, "XryM")
            self._update_flag = 0
            gw.timed_out = True
            return
        # ⑥.5 用户信息卡 UID 匹配（最长10秒）：登录前/登录中信息卡显示临时账号
        #     （UID=3.1415926、名称=小赛尔），窗口 UID（id=280）与米米号一致才算
        #     登录成功；匹配失败 → 账号名为空，交由自检进入重启流程
        if not self._xrym_wait_uid_match(pid, mi_hao, 10):
            self._log(f"序号[{seq}]XryM日常_UID 匹配失败（窗口未显示本账号 UID），视为未登录")
            account.display_name = ""
            self._update_flag = 0
            gw.timed_out = True
            return
        self._log(f"序号[{seq}]XryM日常_UID 匹配通过（{mi_hao}）")

        # ⑦ 读取游戏账号名：用户信息卡「名称：」右侧静态文本 (id=290，登录后出现)。
        #    UID 已匹配通过（信息卡确已刷新为真实数据），昵称不作字符串过滤——
        #    万一用户昵称真叫「小赛尔」也照常采用；读到「小赛尔」时仅视为名称控件
        #    刷新滞后（UID 可能先刷新），额外重读一轮：连续两次同值即视为真实昵称
        _mh, name_hwnd = self._xrym_wait_window(pid, cid("xrym名称id", 290), 5)
        name_text = ""
        if name_hwnd is not None:
            main_hwnd = _mh
            consecutive = 0
            for _ in range(10):  # 10 × 1.5s = 15s 等昵称加载
                try:
                    name_text = get_control_text(name_hwnd).strip()
                except Exception:
                    name_text = ""
                if not name_text:
                    delay(1500)
                    continue
                if name_text != "小赛尔":
                    break
                consecutive += 1
                if consecutive >= 2:
                    break  # 连续两次「小赛尔」= 真实昵称（刷新已稳定）
                delay(1500)
        account.display_name = name_text or mi_hao
        self._log(f"序号[{seq}]XryM日常_已登录！账号名={account.display_name}")
        # 通知 GUI 刷新运行页（账号名列独立显示 display_name）
        if self._on_state_changed:
            self._on_state_changed()

        # ⑦ 换装：点套装切换 → 弹窗选中内置方案 → 切换 → 关闭弹窗
        self._xrym_switch_suit(pid, cfg, cid, seq)

        # ⑧ 点开始 (Win32 id=350)；验证输出框出现「开始执行/任务」即生效
        mh, start_hwnd = self._xrym_wait_window(pid, cid("xrym开始id", 350), 10)
        if start_hwnd is None:
            self._log(f"序号[{seq}]XryM日常_未找到开始按钮")
            self._update_flag = 0
            return
        main_hwnd = mh

        def task_started() -> bool:
            _mh, _out = self._xrym_wait_window(pid, cid("xrym输出id", 320), 2)
            if _out is None:
                return False
            try:
                return "开始执行" in get_control_text(_out) or "任务已开始" in get_control_text(_out)
            except Exception:
                return False
        self._xrym_click_retry(main_hwnd, start_hwnd, verify=task_started,
                               retries=3, wait_after=4.0)
        self._log(f"序号[{seq}]XryM日常_任务已开始")
        self._update_flag = 0

    # ============================================================
    # XM 启动更新（更新组件页内嵌于主窗口：id=110 本地下载 / 160 继续使用该版本）
    # 实测（2.2.6 / 2.5.21）：启动后若服务器有新版，主窗口即显示更新组件页：
    #   - id=160「继续使用该版本」= 不更新直接运行（点击后约5-10s窗口重建为登录页，160变「进入脱机」）
    #   - id=110「本地下载」= 自动下载（进度条130/状态文本140），下载完弹「信息：最新版本已下载完毕」
    #     弹窗（#32770，点确定 id=2）→ 旧进程自动退出 → 新版exe落同目录（ver X.Y.Z 命名）
    # ============================================================

    def _xrym_skip_update_page(self, i: int, gw: GameWindow,
                               main_hwnd: int) -> Optional[int]:
        """点「继续使用该版本」跳过更新页：等窗口重建后返回新主窗口（None=超时）。
        适用于：默认不更新直接运行；以及自动更新重启新版本后防误判（网络波动时新版
        启动也会短暂显示更新组件页，此时跳过即可，避免循环下载）。"""
        seq = self._account_seq(i)
        cfg = self._config_manager

        def cint(key: str, default: int) -> int:
            try:
                return int(cfg.get_xrym_control(key, str(default)))
            except ValueError:
                return default

        keep_id = cint("xrym更新继续id", 160)
        # 无更新页（无「继续使用该版本」）→ 直接返回
        keep_btn = None
        for ch in get_child_windows(main_hwnd):
            if get_control_id(ch) == keep_id:
                try:
                    text = get_control_text(ch)
                except Exception:
                    text = ""
                if "继续使用该版本" in text:
                    keep_btn = ch
                    break
        if keep_btn is None:
            return main_hwnd
        self._log(f"序号[{seq}]XryM更新_检测到更新，选择「继续使用该版本」直接运行")
        foreground_window(main_hwnd)
        delay(300)
        real_click_control(keep_btn)
        # 窗口重建（旧句柄销毁、新句柄出现，实测约 5-10s）：
        # 等「新句柄 + 160 按钮文本不再是 继续使用该版本」判定重建完成
        deadline = time.time() + 60
        while time.time() < deadline:
            mh = self._xrym_current_window(gw.process_id)
            if mh is not None and mh != main_hwnd:
                btn160 = self._find_child_by_id(mh, keep_id)
                if btn160 is not None:
                    try:
                        text = get_control_text(btn160)
                    except Exception:
                        text = ""
                    if "继续使用该版本" not in text:
                        return mh
            delay(2000)
        self._log(f"序号[{seq}]XryM更新_等待窗口重建超时")
        return None

    def _xrym_handle_startup_update(self, i: int, gw: GameWindow, account: Account,
                                    main_hwnd: int, allow_update: bool = True) -> Optional[int]:
        """检测并处理 XM 更新组件页，返回处理后的最新主窗口句柄（None=失败中止登录）。
        调用方（_login_xrym_daily）持有 _key_lock。
        allow_update=True（批量运行）按 [XryM] xrym启动更新 勾选分流：
          - 假（默认）：点「继续使用该版本」，等窗口重建后返回新主窗口（继续登录流程）
          - 真：点「本地下载」自动更新 → 等下载完成/信息弹窗 → 点确定 → 等旧进程退出
               → 定位新版 exe → 写回配置并替换进程 → 返回新主窗口
        allow_update=False（单号启动/自检重启，与自检一致不开放更新功能）：
          一律点「继续使用该版本」直接运行，不下载不换版本。
        """
        seq = self._account_seq(i)
        cfg = self._config_manager

        # 单号启动/自检重启：不开放自动更新，即使配置勾选也只点「继续使用该版本」
        if not allow_update:
            return self._xrym_skip_update_page(i, gw, main_hwnd)

        def cint(key: str, default: int) -> int:
            try:
                return int(cfg.get_xrym_control(key, str(default)))
            except ValueError:
                return default

        dl_id = cint("xrym更新下载id", 110)      # 本地下载按钮（2.5.3 起启动页常驻，即使已是最新）
        keep_id = cint("xrym更新继续id", 160)    # 继续使用该版本按钮（有更新时）/ 进入脱机（无更新时）
        ver_id = cint("xrym更新版本id", 170)     # 当前/最新版本文本
        info_title = cfg.get_xrym_control("xrym更新完成标题", "信息")

        # ① 更新组件页常驻判断：id=110「本地下载」按钮存在（2.5.3 实测：最新版启动页
        #    同样显示更新组件，不能仅凭按钮存在判定有更新）
        download_btn = None
        for ch in get_child_windows(main_hwnd):
            if get_control_id(ch) == dl_id:
                try:
                    text = get_control_text(ch)
                except Exception:
                    text = ""
                if "本地下载" in text:
                    download_btn = ch
                    break
        if download_btn is None:
            # 无更新组件 → 直接走登录流程
            return main_hwnd

        # ② 解析 id=170 版本对比，确认真有新版（XM 规则）：
        #    - 解析成功且 最新 > 当前 → 有更新
        #    - 解析成功且相等（如 v2.5.3 启动时 170=当前v2.5.3/最新v2.5.3）→ 无更新，直接登录
        #    - 解析失败（网络波动致公告/版本为空）→ 按 XM 温馨提示（id=180 原文）降级：
        #      不下载，点「继续使用该版本」直接运行，避免点击无效的下载按钮干等超时
        ver_text = ""
        for ch in get_child_windows(main_hwnd):
            if get_control_id(ch) == ver_id:
                try:
                    ver_text = get_control_text(ch) or ""
                except Exception:
                    ver_text = ""
                break
        import re as _re
        m_cur = _re.search(r"当前版本[:：]\s*v?([\d.]+)", ver_text)
        m_new = _re.search(r"最新版本[:：]\s*v?([\d.]+)", ver_text)
        if m_cur and m_new:
            from .version import compare_xrym_version
            cur_ver, new_ver = m_cur.group(1), m_new.group(1)
            if compare_xrym_version(new_ver, cur_ver) <= 0:
                self._log(f"序号[{seq}]XryM更新_已是最新版本（v{cur_ver}），无需更新")
                return main_hwnd
            self._log(f"序号[{seq}]XryM更新_检测到新版本 v{new_ver}（当前 v{cur_ver}）")
        else:
            self._log(f"序号[{seq}]XryM更新_版本信息不可用（可能网络波动），选择「继续使用该版本」直接运行")
            return self._xrym_skip_update_page(i, gw, main_hwnd)

        # ③ 确有新版：读开关分流
        if not cfg.get_xrym_auto_update():
            # —— 不更新直接运行：点「继续使用该版本」——
            return self._xrym_skip_update_page(i, gw, main_hwnd)

        # —— 自动更新：点「本地下载」→ 等下载完成 → 点确定 → 等退出 → 换新进程 ——
        self._log(f"序号[{seq}]XryM更新_检测到更新，开始自动更新（本地下载）")
        foreground_window(main_hwnd)
        delay(300)
        real_click_control(download_btn)
        self._log(f"序号[{seq}]XryM更新_已点击本地下载，等待下载完成…")

        # 等下载完成：出现「信息：最新版本已下载完毕」弹窗即完成（最多 600 秒）。
        # 实测下载到 100% 后必弹此框，以弹窗出现为完成判据；进度文本仅作日志。
        deadline = time.time() + 600
        info_hwnd = 0
        while time.time() < deadline:
            delay(5000)
            if not self._process_manager.is_alive(gw):
                break  # 进程意外退出
            # 检查信息弹窗（#32770，标题含「信息」）
            info_hwnd = WindowFinder.find_window_by_pid(
                gw.process_id, info_title, "#32770", match_full_title=False, timeout=1)
            if info_hwnd:
                break
            # 进度文本 100% → 日志提示，继续等弹窗
            mh = self._xrym_current_window(gw.process_id)
            if mh is not None:
                st = self._find_child_by_id(mh, cint("xrym更新状态id", 140))
                if st is not None:
                    try:
                        text = get_control_text(st)
                        if "100%" in text:
                            self._log(f"序号[{seq}]XryM更新_下载100%，等待完成提示弹窗")
                    except Exception:
                        pass
        if not info_hwnd:
            self._log(f"序号[{seq}]XryM更新_等待下载完成超时或进程退出")
            return None

        # 点「确定」（弹窗 id=2）
        ok_btn = None
        for ch in get_child_windows(info_hwnd):
            if get_control_id(ch) == 2:
                try:
                    if "确定" in get_control_text(ch):
                        ok_btn = ch
                        break
                except Exception:
                    ok_btn = ch
                    break
        if ok_btn is None:
            self._log(f"序号[{seq}]XryM更新_未找到确定按钮，弹窗直接关闭")
            WindowFinder.close_window(info_hwnd)
        else:
            real_click_control(ok_btn)
            self._log(f"序号[{seq}]XryM更新_已确认更新完成")
        # 等旧进程自动退出（实测点确定后约 10 秒退出；超时 60 秒兜底强杀）
        if not self._process_manager.wait_for_exit(gw, timeout=60):
            self._log(f"序号[{seq}]XryM更新_旧进程未自动退出，强制关闭")
            self._process_manager.kill(gw)
        self._log(f"序号[{seq}]XryM更新_旧进程已退出")

        # ③ 定位新版 exe：下载刚完成 → 同目录同前缀 exe 中版本最大者（XM 规则）。
        #    基准用「实际启动的 exe」（gw.exe_path），配置可能已被上次更新写为新版，
        #    以配置为基准会把刚下载的新版排除掉而选中旧版
        old_path = getattr(gw, "exe_path", "") or self._get_xrym_start_path()
        new_path = self._xrym_newest_exe(old_path)
        if not os.path.isfile(new_path) or os.path.abspath(new_path) == os.path.abspath(old_path):
            self._log(f"序号[{seq}]XryM更新_未发现新版本文件，跳过更新重启")
            return None
        self._log(f"序号[{seq}]XryM更新_新版本: {os.path.basename(new_path)}")

        # ④ 写回配置（客户端列表 XryM 日常条目 + [XryM] xrym目录9），整批后续账号用新路径
        self._persist_xrym_path(new_path)
        self._xrym_updated_path = new_path

        # ⑤ 用新版本重新启动，替换 gw（新 pid）
        new_gw = self._process_manager.launch(new_path)
        if new_gw is None:
            err = getattr(self._process_manager, "last_error", "")
            self._log(f"序号[{seq}]XryM更新_新版本启动失败: {new_path} {err}")
            return None
        # 保留旧窗口结构中的运行期字段，替换进程相关字段
        gw.process_id = new_gw.process_id
        gw.process = new_gw.process
        gw.window_handle = 0
        gw.timed_out = False
        self._log(f"序号[{seq}]XryM更新_新版本进程已启动 pid={gw.process_id}")

        # ⑥ 等新主窗口出现（新版不再显示更新页，直接登录页）
        mh = WindowFinder.find_window_by_pid(
            gw.process_id, "XM", "WTWindow", match_full_title=False,
            timeout=self.WINDOW_WAIT_TIMEOUT)
        if mh is None:
            self._log(f"序号[{seq}]XryM更新_等待新版本主窗口超时")
            return None
        gw.window_handle = mh
        # 新版本启动后若又弹更新组件页（服务器响应波动/误报），不再重复自动下载，
        # 点「继续使用该版本」跳过并等窗口重建（防止循环下载）
        return self._xrym_skip_update_page(i, gw, mh)

    def _xrym_newest_exe(self, current_path: str) -> str:
        """同目录、同基础名（去掉版本号）的所有 exe 中，版本最大者（XM 规则）。
        XM 版本号识别：末段两位数按 X.X.X.X 拆开（如 2.5.21 = 2.5.2.1），
        保证服务器新版 2.5.3 大于旧版文件名的 2.5.21；版本相同（重复下载）取修改时间最新。
        返回空串=未找到。"""
        import glob
        from .version import extract_base_name, compare_xrym_version, extract_version

        directory = os.path.dirname(current_path)
        base = extract_base_name(os.path.basename(current_path))
        best, best_ver, best_mtime = "", "", 0.0
        for f in glob.glob(os.path.join(directory, base + "*.exe")):
            if os.path.abspath(f).lower() == os.path.abspath(current_path).lower():
                continue
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            # 过滤下载中断的残件（实测中断下载只剩 ~348KB；完整 XM exe 约 20MB）
            try:
                if os.path.getsize(f) < 1024 * 1024:
                    continue
            except OSError:
                continue
            ver = extract_version(os.path.basename(f))
            if ver and best_ver:
                if compare_xrym_version(ver, best_ver) > 0:
                    best, best_ver, best_mtime = f, ver, mt
                elif compare_xrym_version(ver, best_ver) == 0 and mt > best_mtime:
                    best, best_ver, best_mtime = f, ver, mt  # 同版本取最新落盘
            elif ver and not best_ver:
                best, best_ver, best_mtime = f, ver, mt
        return best

    def _get_xrym_start_path(self) -> str:
        """获取当前 XM 客户端路径（优先已更新的内存路径，其次配置）"""
        if self._xrym_updated_path and os.path.isfile(self._xrym_updated_path):
            return self._xrym_updated_path
        return self._config_manager.get_xrym_daily_path()

    def _persist_xrym_path(self, new_path: str) -> None:
        """把 XM 新版本路径写回 ctrl.ini（[客户端列表] XryM 日常条目 + [XryM] xrym目录9）"""
        cfg = self._config_manager
        entries = cfg.get_client_entries()
        changed = False
        for e in entries:
            if e.kind == "日常" and "xrym" in e.name.lower():
                if e.path != new_path:
                    e.path = new_path
                    changed = True
                break
        if changed:
            cfg.set_client_entries(entries)
        cfg.set_client_path("XryM", "xrym目录9", new_path)
        try:
            cfg.save()
        except Exception as e:
            self._log(f"XryM更新_写回配置失败: {e}")

    # ---- XryM Win32 控件查找（不走 UIA，无 COMError 时序问题）----

    def _find_child_by_id(self, main_hwnd: int, cid: int) -> Optional[int]:
        """枚举主窗口子控件，按 Win32 控件 ID 匹配（XryM 的 ID 与 UIA AutomationId 一致）"""
        for child_hwnd in get_child_windows(main_hwnd):
            if get_control_id(child_hwnd) == cid:
                return child_hwnd
        return None

    def _xrym_current_window(self, pid: int) -> Optional[int]:
        """获取 XryM 主窗口最新句柄（点击「进入脱机」后窗口重建，旧句柄失效）"""
        return WindowFinder.find_window_by_pid(
            pid, "XM", "WTWindow", match_full_title=False, timeout=2)

    def _tile_xrym(self, i: int, main_hwnd: int) -> None:
        """XryM 日常窗口平铺（登录区就绪后调用，照雷小伊：右对齐从下往上排）。
        屏幕基准用系统工作区（SPI_GETWORKAREA，自动排除任务栏；引擎为 DPI-aware
        进程，返回物理像素），避免窗口摆到任务栏/屏幕外。配置：[XryM] 节
        xrym列数/xrym左右距/xrym上下距（物理像素）。"""
        queue = self._account_manager.get_daily_queue()
        n = self._count_queue_active_before(queue, i)
        if n == 0:
            return
        cfg = self._config_manager

        def cint(key: str, default: int = 0) -> int:
            try:
                return int(cfg.get_xrym_control(key, str(default)))
            except ValueError:
                return default

        cols = max(1, cint("xrym列数", 1))
        col = ((n - 1) % cols) + 1
        row = math.ceil(n / cols)
        spacing_x = cint("xrym左右距", 318)
        spacing_y = cint("xrym上下距", 240)

        import ctypes
        from ctypes import wintypes as wt
        work = wt.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
        sw = work.right - work.left   # 工作区宽（水平）
        sh = work.bottom - work.top   # 工作区高（垂直，任务栏之上）

        import win32gui
        try:
            r = win32gui.GetWindowRect(main_hwnd)
            window_w = r[2] - r[0]
            window_h = r[3] - r[1]
        except Exception:
            # 窗口已关闭（用户中途点「关闭」杀进程）→ 跳过本次平铺，避免线程抛异常
            return

        x = sw - window_w - (col - 1) * spacing_x
        y = sh - window_h - (row - 1) * spacing_y
        set_window_pos(main_hwnd, max(0, x), max(0, y))
        self._log(f"平铺序号[{self._account_seq(i)}] col={col} row={row} x={x} y={y} 工作区={sw}x{sh}")

    def _xrym_wait_window(self, pid: int, cid: int, timeout: float) -> tuple[Optional[int], Optional[int]]:
        """等待主窗口内指定控件出现；每次轮询重新枚举顶层窗口（窗口可能重建），
        返回 (最新窗口句柄, 控件句柄)，超时返回 (None, None)"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            mh = self._xrym_current_window(pid)
            if mh is not None:
                h = self._find_child_by_id(mh, cid)
                if h is not None:
                    return mh, h
            delay(1000)
        return None, None

    def activate_tile_xrym(self) -> None:
        """XryM 排列：按启动顺序（_xrym_windows 下标=账号序号，串行启动）依次激活窗口焦点。
        窗口可能在「进入脱机」后重建，每次重新枚举最新顶层窗口句柄。"""
        for i, gw in enumerate(self._xrym_windows):
            if gw is None:
                continue
            mh = self._xrym_current_window(gw.process_id)
            if mh is None:
                self._log(f"序号[{self._account_seq(i)}]XryM排列_窗口不存在，跳过")
                continue
            foreground_window(mh)
            delay(500)
        self._log("XryM排列_已按启动顺序激活全部窗口")

    def _notify_stall_unfinished(self, mode: str, label: str, pending: list) -> None:
        """卡死重启2轮后仍未完成的账号 → 微信推送（每批次一次，由看门狗保证）。"""
        parts = []
        for i in pending:
            acc = self._account_manager.accounts[i]
            parts.append(f"[{self._account_seq(i)}]{acc.display_name or acc.mi_hao or '未知'}")
        detail = "、".join(parts)
        self._log(f"{label}_重启2轮后仍未完成：{detail}，已推送微信提醒")
        try:
            self._notifier.notify_stall_unfinished(mode, detail)
        except Exception:
            pass

    # ---- XryM 日常自检（批量运行 + 勾选「自检重启」后触发；单号运行不触发）----
    # 业务（新）：
    #   1) 最后一个账号登录操作后 100 秒，检查所有窗口是否获取到游戏账号名
    #      （display_name 非空 = 登录成功，临时写入运行页账号名列）
    #   2) 登录失败的窗口重启再登录（最多 2 轮）
    #   3) 全部登录完成 → 每 10 分钟检查任务完成（输出框 320 含「任务已执行完毕」）
    #      + 卡死看门狗（固定窗口不刷新）：兜底60分无完成 / 首个完成后10分仍有未完成
    #      → 第1轮重启未完成窗口；重启后40分仍未完成 → 第2轮重启；再40分 → 推送
    #      未完成账号到微信（只推一次，此后只监测）；全部完成 → 完成通知+关闭全部窗口

    def _xrym_cid(self, key: str, default: int) -> int:
        try:
            return int(self._config_manager.get_xrym_control(key, str(default)))
        except ValueError:
            return default

    def _start_xrym_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """启动 XryM 自检线程（防重：已有自检线程存活则跳过）"""
        t = getattr(self, "_xrym_selfcheck_thread", None)
        if t is not None and t.is_alive():
            self._log("XryM自检_上一轮自检仍在进行，跳过")
            return
        t = threading.Thread(
            target=self._xrym_selfcheck, args=(exe_path, list(active)), daemon=True)
        self._xrym_selfcheck_thread = t
        t.start()
        self._log(f"XryM自检_已启动（{len(active)} 个账号，100 秒后检查登录）")

    def _xrym_wait_uid_match(self, pid: int, mi_hao: str, timeout: int = 10) -> bool:
        """轮询用户信息卡 UID（id=280）与账号米米号匹配，最长 timeout 秒。
        实测：登录前/登录中信息卡显示临时账号（UID=3.1415926、名称=小赛尔），
        登录成功后刷新为真实数据（刷新有数秒延迟）。窗口 UID 与米米号一致
        才算本账号登录成功；不匹配/超时返回 False（调用方按未登录处理）。"""
        uid_id = self._xrym_cid("xrym uid id", 280)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _mh, uid_hwnd = self._xrym_wait_window(pid, uid_id, 2)
            if uid_hwnd is not None:
                try:
                    text = (get_control_text(uid_hwnd) or "").strip()
                except Exception:
                    text = ""
                if text == mi_hao:
                    return True
            delay(1000)
        return False

    def _xrym_refresh_display_name(self, i: int, gw) -> None:
        """复查窗口用户信息卡：先做 UID 匹配（最长10秒，id=280 与米米号一致），
        匹配通过再读「名称：」昵称（id=290）更新 display_name；匹配失败 → 账号名
        置空（视为未登录，自检将重启该账号）。UID 匹配为准，昵称不做字符串过滤
        （用户昵称真叫「小赛尔」也照常采用）。"""
        try:
            account = self._account_manager.accounts[i]
            if not self._xrym_wait_uid_match(gw.process_id, account.mi_hao, 10):
                if account.display_name:
                    account.display_name = ""
                    if self._on_state_changed:
                        self._on_state_changed()
                return
            mh, name_hwnd = self._xrym_wait_window(gw.process_id, self._xrym_cid("xrym名称id", 290), 3)
            if name_hwnd is None:
                return
            text = get_control_text(name_hwnd).strip()
        except Exception:
            return
        if text:
            if account.display_name != text:
                account.display_name = text
                if self._on_state_changed:
                    self._on_state_changed()

    def _xrym_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """XryM 日常自检主流程（daemon 线程）"""
        # 阶段0：最后一个账号登录操作后 100 秒（wait 可被停止信号立即唤醒）
        self._log("XryM自检_等待100秒后检查登录状态…")
        if self._daily_sc_stop.wait(100):
            self._log("XryM自检_已收到停止信号，退出")
            return

        # 阶段1：登录检查（未获取到账号名 → 重启再登录，最多 2 轮）
        for round_no in range(1, 4):
            if self._daily_sc_stop.is_set():
                self._log("XryM自检_已收到停止信号，退出")
                return
            failed: list[int] = []
            for i in active:
                account = self._account_manager.accounts[i]
                gw = self._xrym_windows[i]
                if gw is None:
                    failed.append(i)
                    continue
                # 复查：登录流程中可能读取失败，窗口此刻可能已显示昵称
                self._xrym_refresh_display_name(i, gw)
                if not account.display_name:
                    failed.append(i)
            if not failed:
                self._log("XryM自检_全部账号已登录成功")
                break
            if round_no >= 3:
                names = ",".join(str(self._account_seq(i)) for i in failed)
                self._log(f"XryM自检_重试2轮后仍有账号未登录成功：序号[{names}]，本轮自检结束")
                return
            self._log(f"XryM自检_第{round_no}轮发现 {len(failed)} 个账号未登录成功，重启中")
            for i in failed:
                if self._daily_sc_stop.is_set():
                    self._log("XryM自检_已收到停止信号，不再重启")
                    return
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]XryM自检_重启并重新登录")
                self.stop_xrym_single(i)
                self._launch_xrym_one(i, account, exe_path, allow_update=False)  # 自检重启不开放更新
            self._retile_xrym_selfcheck(active)

        # 阶段2：任务完成检查（每 10 分钟；输出框 320 含「任务已执行完毕」）
        # + 卡死看门狗（固定窗口状态机，见 watchdog.py）
        out_id = self._xrym_cid("xrym输出id", 320)

        def probe(i: int) -> str:
            gw = self._xrym_windows[i]
            if gw is None:
                return GONE
            try:
                _mh, out_hwnd = self._xrym_wait_window(gw.process_id, out_id, 2)
                if out_hwnd is not None and "任务已执行完毕" in (get_control_text(out_hwnd) or ""):
                    return DONE
            except Exception:
                pass
            return WAIT

        def restart_round(round_no: int, pending: list) -> bool:
            self._log(f"XryM自检_第{round_no}轮卡死重启 {len(pending)} 个未完成账号")
            for i in pending:
                if self._daily_sc_stop.is_set():
                    self._log("XryM自检_已收到停止信号，不再重启")
                    return False
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]XryM自检_卡死重启并重新登录")
                self.stop_xrym_single(i)
                self._launch_xrym_one(i, account, exe_path, allow_update=False)  # 自检重启不开放更新
            self._retile_xrym_selfcheck(active)
            return True

        def all_complete() -> None:
            self._log("XryM自检_全部账号任务完成！！！")
            try:
                self._notifier.notify_all_complete("日常")
            except Exception:
                pass
            self._log("XryM自检_任务全部完成，自动关闭窗口")
            self._stop_windows(self._xrym_windows, "XryM日常")

        StallWatchdog(
            poll_s=self.LEI_SELFCHECK_INTERVAL_MS // 1000,  # 日常统一 10 分钟轮询
            fallback_s=self.DAILY_STALL_FALLBACK_MS / 1000,
            round1_s=self.DAILY_STALL_ROUND1_MS / 1000,
            round2_s=self.DAILY_STALL_ROUND2_MS / 1000,
            probe=probe, restart_round=restart_round,
            push_unfinished=lambda p: self._notify_stall_unfinished("日常", "XryM自检", p),
            all_complete=all_complete,
            account_seq=self._account_seq,
            log=lambda m: self._log(f"XryM自检_{m}"),
            sleep_wait=self._daily_sc_stop.wait,
        ).run(active)

    # ---- 雷小伊日常自检（批量运行 + 勾选「自检重启」后触发；单号运行不触发）----
    # 业务（新，与 XryM 同款结构）：
    #   1) 最后一个账号登录后 100 秒，检查所有窗口是否获取到游戏账号名（display_name 非空）
    #   2) 登录失败的窗口重启再登录（最多 2 轮）
    #   3) 全部登录完成 → 每轮检查任务完成（输出框 52 含「全部执行完毕」）
    #      + 卡死看门狗（固定窗口不刷新）：兜底60分无完成 / 首个完成后10分仍有未完成
    #      → 第1轮重启未完成窗口；重启后40分仍未完成 → 第2轮重启；再40分 → 推送
    #      未完成账号到微信（只推一次，此后只监测）；全部完成 → 完成通知+关闭全部窗口

    # 任务完成检查间隔（日常统一 10 分钟）
    LEI_SELFCHECK_INTERVAL_MS = 600_000
    # 首轮登录检查等待：首号 60 秒，每增加一个账号追加 30 秒；
    # 之后仅额外给仍在运行的登录线程 30 秒收尾，超时即纳入失败重启。
    LEI_INITIAL_LOGIN_BASE_SECONDS = 60
    LEI_INITIAL_LOGIN_PER_ACCOUNT_SECONDS = 30
    LEI_INITIAL_LOGIN_SETTLE_SECONDS = 30
    # 新账号初始化（Flash 16 项 + 因子前 8 项 + Unity 37 项）比常规勾选多约
    # 1.5 分钟，且串行登录队列会整体顺延——批次含新账号时收尾窗口追加本值，
    # 避免初始化进行到一半被误判登录失败而杀窗重启（幂等可恢复但浪费一轮）。
    LEI_NEW_INIT_EXTRA_SETTLE_SECONDS = 90

    # 任务完成卡死看门狗阈值·日常（固定窗口，不随完成事件刷新）
    # 兜底：进入任务完成检查后无任何完成的容忍时长（60 分钟）
    DAILY_STALL_FALLBACK_MS = 3_600_000
    # 第一轮：首个完成后等待其余窗口跟上（10 分钟）
    DAILY_STALL_ROUND1_MS = 600_000
    # 第二轮/末尾轮：重启后任务从头跑的完成时长上限（40 分钟）
    DAILY_STALL_ROUND2_MS = 2_400_000

    def _start_lei_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """启动雷小伊日常自检线程（防重：已有自检线程存活则跳过）"""
        t = getattr(self, "_lei_selfcheck_thread", None)
        if t is not None and t.is_alive():
            self._log("日常自检_上一轮自检仍在进行，跳过")
            return
        t = threading.Thread(
            target=self._lei_selfcheck, args=(exe_path, list(active)), daemon=True)
        self._lei_selfcheck_thread = t
        t.start()
        initial_wait = (self.LEI_INITIAL_LOGIN_BASE_SECONDS
                        + max(0, len(active) - 1) * self.LEI_INITIAL_LOGIN_PER_ACCOUNT_SECONDS)
        self._log(f"日常自检_已启动（{len(active)} 个账号，{initial_wait} 秒后检查登录）")

    def _lei_refresh_display_name(self, i: int, gw) -> None:
        """复查雷小伊窗口「账号名」控件（窗口配置26）：读到非空昵称 → 更新
        display_name（临时写入运行页账号名列）并通知 GUI 刷新。"""
        try:
            # 已明确超时的流程不可被客户端迟到的昵称“复活”为登录成功。
            if gw.timed_out:
                return
            name_id = self._get_config(26)
            mh = gw.window_handle
            if mh == 0 or not WindowFinder.is_window_valid(mh):
                return
            name_hwnd = None
            for child in get_child_windows(mh):
                if get_control_id(child) == name_id:
                    name_hwnd = child
                    break
            if name_hwnd is None:
                return
            text = get_control_text(name_hwnd).strip()
        except Exception:
            return
        if text:
            account = self._account_manager.accounts[i]
            if account.display_name != text:
                account.display_name = text
                if self._on_state_changed:
                    self._on_state_changed()

    def _clear_new_annotation(self, i: int) -> None:
        """日常任务完成时移除账号标注的「新」前缀（新账号初始化只走一次的门闩）。

        "新"→""；"新；时长待核对"→"时长待核对"（去"新"和全角分号，保留后续文字）。
        幂等：非新账号/已移除时不写文件。写盘后 GUI 的 QFileSystemWatcher 自动重载
        （与闲鱼发货侧写账本同一链路，已验证）。异常不抛出（probe 内调用）。
        """
        try:
            accounts = self._account_manager.accounts
            if not (0 <= i < len(accounts)):
                return
            account = accounts[i]
            old = account.annotation
            if account.clear_new_tag():
                self._account_manager.save()
                rest = account.annotation or "（空）"
                self._log(f"序号[{self._account_seq(i)}]日常自检_任务完成，移除新账号标注: 「{old}」→「{rest}」")
        except Exception as e:
            self._log(f"序号[{self._account_seq(i)}]日常自检_移除新账号标注异常: {e}")

    def _lei_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """雷小伊日常自检主流程（daemon 线程）"""
        # 阶段0：按批量账号数等待：首号 60 秒，每多一号追加 20 秒（可被停止信号唤醒）。
        initial_wait = (self.LEI_INITIAL_LOGIN_BASE_SECONDS
                        + max(0, len(active) - 1) * self.LEI_INITIAL_LOGIN_PER_ACCOUNT_SECONDS)
        self._log(f"日常自检_等待{initial_wait}秒后检查登录状态…")
        if self._daily_sc_stop.wait(initial_wait):
            self._log("日常自检_已收到停止信号，退出")
            return

        # 批量登录受 _key_lock 串行化，固定 100 秒后最后几个账号可能仍在执行
        # 套装/任务勾选。先给本轮原始登录线程一个有限收尾窗口，避免误杀正常登录。
        settle_s = self.LEI_INITIAL_LOGIN_SETTLE_SECONDS
        # 批次含新账号：初始化勾选量大（Flash 16 + 因子 8 + Unity 37）且排在
        # 其后的串行登录整体顺延，收尾窗口追加等待，避免误杀初始化中的账号。
        if any(self._account_manager.accounts[i].is_new_account() for i in active):
            settle_s += self.LEI_NEW_INIT_EXTRA_SETTLE_SECONDS
            self._log(f"日常自检_批次含新账号，登录收尾窗口延长至 {settle_s} 秒")
        settle_deadline = time.time() + settle_s
        while time.time() < settle_deadline:
            if self._daily_sc_stop.is_set():
                self._log("日常自检_已收到停止信号，退出")
                return
            pending_login = [i for i in active
                             if self._daily_windows[i] is not None
                             and not self._daily_windows[i].login_finished.is_set()]
            if not pending_login:
                break
            time.sleep(3)
        else:
            pending_names = ",".join(str(self._account_seq(i)) for i in pending_login)
            self._log(f"日常自检_等待初始登录收尾超时：序号[{pending_names}]，转入失败重启检查")

        # 阶段1：登录检查（未完成/失败 → 重启再登录，最多 2 轮）
        for round_no in range(1, 4):
            if self._daily_sc_stop.is_set():
                self._log("日常自检_已收到停止信号，退出")
                return
            failed: list[int] = []
            for i in active:
                account = self._account_manager.accounts[i]
                gw = self._daily_windows[i]
                if gw is None:
                    failed.append(i)
                    continue
                # 昵称会在“开始”按钮点击前就出现；必须同时确认该登录线程已完成且
                # 明确走到开始任务之后，避免自检把仍在登录中的窗口误判为成功。
                self._lei_refresh_display_name(i, gw)
                if not gw.login_finished.is_set() or not gw.login_succeeded:
                    failed.append(i)
            if not failed:
                self._log("日常自检_全部账号已登录成功")
                break
            if round_no >= 3:
                names = ",".join(str(self._account_seq(i)) for i in failed)
                self._log(f"日常自检_重试2轮后仍有账号未登录成功：序号[{names}]，本轮自检结束")
                return
            self._log(f"日常自检_第{round_no}轮发现 {len(failed)} 个账号未登录成功，重启中")
            for i in failed:
                if self._daily_sc_stop.is_set():
                    self._log("日常自检_已收到停止信号，不再重启")
                    return
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]日常自检_重启并重新登录")
                self.stop_single(i, ClientType.LEI_XIAO_YI)
                self._launch_one(i, ClientType.LEI_XIAO_YI, exe_path, account,
                                 allow_update=False)  # 自检重启不开放更新
                # 登录线程异步：等待“开始任务”后的最终结果（最多 180 秒）。
                # 不能只等昵称：昵称会在套装/任务勾选阶段提前出现。
                deadline = time.time() + 180
                login_ok = False
                while time.time() < deadline:
                    if self._daily_sc_stop.is_set():
                        self._log("日常自检_已收到停止信号，退出")
                        return
                    gw = self._daily_windows[i]
                    if gw is not None and gw.login_finished.wait(timeout=3):
                        login_ok = gw.login_succeeded
                        break
                if not login_ok:
                    self._log(f"序号[{self._account_seq(i)}]日常自检_重启登录未完成或失败，继续复查")
            self._retile_lei_selfcheck(active)

        # 重启账号可能在 180 秒等待中仍失败；绝不能带着失败状态进入任务完成检查。
        still_failed = []
        for i in active:
            gw = self._daily_windows[i]
            if gw is None or not gw.login_finished.is_set() or not gw.login_succeeded:
                still_failed.append(i)
        if still_failed:
            names = ",".join(str(self._account_seq(i)) for i in still_failed)
            self._log(f"日常自检_登录未完成或失败：序号[{names}]，本轮自检结束")
            return

        # 阶段2：任务完成检查（输出框 52 含「全部执行完毕」）+ 卡死看门狗
        # 完成时顺带移除「新」标注（新账号初始化门闩，watchdog discard 后不再
        # probe 该账号 → 天然只执行一次；clear 内部幂等跨批次安全）
        def probe(i: int) -> str:
            gw = self._daily_windows[i]
            if gw is None:
                return GONE
            try:
                if gw.output_handle and gw.output_handle > 0:
                    if "全部执行完毕" in external_edit_get_content(gw.output_handle):
                        self._clear_new_annotation(i)
                        return DONE
            except Exception:
                pass
            return WAIT

        def restart_round(round_no: int, pending: list) -> bool:
            self._log(f"日常自检_第{round_no}轮卡死重启 {len(pending)} 个未完成账号")
            for i in pending:
                if self._daily_sc_stop.is_set():
                    self._log("日常自检_已收到停止信号，不再重启")
                    return False
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]日常自检_卡死重启并重新登录")
                self.stop_single(i, ClientType.LEI_XIAO_YI)
                self._launch_one(i, ClientType.LEI_XIAO_YI, exe_path, account,
                                 allow_update=False)  # 自检重启不开放更新
                # 与阶段1一致：登录线程异步，等“开始任务”后的最终结果（最多 180 秒）
                deadline = time.time() + 180
                login_ok = False
                while time.time() < deadline:
                    if self._daily_sc_stop.is_set():
                        self._log("日常自检_已收到停止信号，退出")
                        return False
                    gw = self._daily_windows[i]
                    if gw is not None and gw.login_finished.wait(timeout=3):
                        login_ok = gw.login_succeeded
                        break
                if not login_ok:
                    self._log(f"序号[{self._account_seq(i)}]日常自检_卡死重启登录未完成或失败，继续复查")
            self._retile_lei_selfcheck(active)
            return True

        def all_complete() -> None:
            self._log("日常自检_全部账号任务完成！！！")
            try:
                self._notifier.notify_all_complete("日常")
            except Exception:
                pass
            self._log("日常自检_任务全部完成，自动关闭窗口")
            self._stop_windows(self._daily_windows, "日常")

        StallWatchdog(
            poll_s=self.LEI_SELFCHECK_INTERVAL_MS // 1000,
            fallback_s=self.DAILY_STALL_FALLBACK_MS / 1000,
            round1_s=self.DAILY_STALL_ROUND1_MS / 1000,
            round2_s=self.DAILY_STALL_ROUND2_MS / 1000,
            probe=probe, restart_round=restart_round,
            push_unfinished=lambda p: self._notify_stall_unfinished("日常", "日常自检", p),
            all_complete=all_complete,
            account_seq=self._account_seq,
            log=lambda m: self._log(f"日常自检_{m}"),
            sleep_wait=self._daily_sc_stop.wait,
        ).run(active)

    # ---- 重聚巅峰自检（批量运行 + 勾选「自检重启」后触发；单号运行不触发）----
    # 业务（新，与 XryM/雷小伊同款结构）：
    #   1) 最后一个账号登录后 100 秒，检查所有窗口是否获取到游戏账号名（display_name 非空）
    #   2) 登录失败的窗口重启再登录（最多 2 轮）
    #   3) 全部登录完成 → 每轮检查任务完成（输出框 51 含「日任务已完成」；
    #      巅峰日志从下往上输出，读取全文做包含判断）
    #      + 卡死看门狗（固定窗口不刷新）：兜底10分无完成 / 首个完成后5分仍有未完成
    #      → 第1轮重启未完成窗口；重启后5分仍未完成 → 第2轮重启；再5分 → 推送
    #      未完成账号到微信（只推一次，此后只监测）；全部完成 → 完成通知+关闭全部窗口

    # 任务完成检查间隔（巅峰 2 分钟）
    PEAK_SELFCHECK_INTERVAL_MS = 120_000

    # 任务完成卡死看门狗阈值·巅峰（固定窗口，不随完成事件刷新）
    # 兜底：进入任务完成检查后无任何完成的容忍时长（10 分钟）
    PEAK_STALL_FALLBACK_MS = 600_000
    # 第一轮：首个完成后等待其余窗口跟上（5 分钟）
    PEAK_STALL_ROUND1_MS = 300_000
    # 第二轮/末尾轮：重启后任务从头跑的完成时长上限（5 分钟）
    PEAK_STALL_ROUND2_MS = 300_000

    def _start_peak_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """启动重聚巅峰自检线程（防重：已有自检线程存活则跳过）"""
        t = getattr(self, "_peak_selfcheck_thread", None)
        if t is not None and t.is_alive():
            self._log("巅峰自检_上一轮自检仍在进行，跳过")
            return
        t = threading.Thread(
            target=self._peak_selfcheck, args=(exe_path, list(active)), daemon=True)
        self._peak_selfcheck_thread = t
        t.start()
        self._log(f"巅峰自检_已启动（{len(active)} 个账号，100 秒后检查登录）")

    def _peak_refresh_display_name(self, i: int, gw) -> None:
        """复查重聚巅峰窗口标题（登录后标题含游戏账号名）：
        提取到非空昵称 → 更新 display_name（临时写入运行页账号名列）并通知 GUI 刷新。"""
        try:
            mh = gw.window_handle
            if mh == 0 or not WindowFinder.is_window_valid(mh):
                return
            title = WindowFinder.get_window_title(mh)
            if title == _PEAK_MAIN_TITLE:
                return  # 未登录状态
            name = self._extract_username(title, 2)
        except Exception:
            return
        if name:
            account = self._account_manager.accounts[i]
            if account.display_name != name:
                account.display_name = name
                if self._on_state_changed:
                    self._on_state_changed()

    def _peak_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """重聚巅峰自检主流程（daemon 线程）"""
        # 阶段0：最后一个账号登录后 100 秒（wait 可被停止信号立即唤醒）
        self._log("巅峰自检_等待100秒后检查登录状态…")
        if self._peak_sc_stop.wait(100):
            self._log("巅峰自检_已收到停止信号，退出")
            return

        # 阶段1：登录检查（流程未结束或未获取到账号名 → 重启再登录，最多 2 轮）
        for round_no in range(1, 4):
            if self._peak_sc_stop.is_set():
                self._log("巅峰自检_已收到停止信号，退出")
                return
            failed: list[int] = []
            for i in active:
                account = self._account_manager.accounts[i]
                gw = self._peak_windows[i]
                if gw is None:
                    failed.append(i)
                    continue
                # 复查：登录流程中可能读取失败，窗口此刻可能已显示标题昵称
                self._peak_refresh_display_name(i, gw)
                if not gw.login_finished.is_set() or not account.display_name:
                    failed.append(i)
            if not failed:
                self._log("巅峰自检_全部账号已登录成功")
                break
            if round_no >= 3:
                names = ",".join(str(self._account_seq(i)) for i in failed)
                self._log(f"巅峰自检_重试2轮后仍有账号未登录成功：序号[{names}]，本轮自检结束")
                return
            self._log(f"巅峰自检_第{round_no}轮发现 {len(failed)} 个账号未登录成功，重启中")
            for i in failed:
                if self._peak_sc_stop.is_set():
                    self._log("巅峰自检_已收到停止信号，不再重启")
                    return
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]巅峰自检_重启并重新登录")
                self.stop_single(i, ClientType.CHONG_JU_PEAK)
                self._launch_one(i, ClientType.CHONG_JU_PEAK, exe_path, account)
                # 昵称早于勾选、兑换和开始任务出现；等整个登录流程结束再重排。
                deadline = time.time() + 180
                while time.time() < deadline:
                    if self._peak_sc_stop.is_set():
                        self._log("巅峰自检_已收到停止信号，退出")
                        return
                    gw = self._peak_windows[i]
                    if gw is None:
                        break
                    if gw.login_finished.wait(timeout=3):
                        self._peak_refresh_display_name(i, gw)
                        break
            self._retile_peak_selfcheck(active)

        # 阶段2：任务完成检查（输出框 51 含「日任务已完成」）+ 卡死看门狗
        def probe(i: int) -> str:
            gw = self._peak_windows[i]
            if gw is None:
                return GONE
            try:
                if gw.output_handle and gw.output_handle > 0:
                    if "日任务已完成" in external_edit_get_content(gw.output_handle):
                        return DONE
            except Exception:
                pass
            return WAIT

        def restart_round(round_no: int, pending: list) -> bool:
            self._log(f"巅峰自检_第{round_no}轮卡死重启 {len(pending)} 个未完成账号")
            for i in pending:
                if self._peak_sc_stop.is_set():
                    self._log("巅峰自检_已收到停止信号，不再重启")
                    return False
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]巅峰自检_卡死重启并重新登录")
                self.stop_single(i, ClientType.CHONG_JU_PEAK)
                self._launch_one(i, ClientType.CHONG_JU_PEAK, exe_path, account)
                # 与阶段1一致：等待登录流程全部结束（最多 180 秒）。
                deadline = time.time() + 180
                while time.time() < deadline:
                    if self._peak_sc_stop.is_set():
                        self._log("巅峰自检_已收到停止信号，退出")
                        return False
                    gw = self._peak_windows[i]
                    if gw is None:
                        break
                    if gw.login_finished.wait(timeout=3):
                        self._peak_refresh_display_name(i, gw)
                        break
            self._retile_peak_selfcheck(active)
            return True

        def all_complete() -> None:
            self._log("巅峰自检_全部账号任务完成！！！")
            try:
                self._notifier.notify_all_complete("巅峰")
            except Exception:
                pass
            self._log("巅峰自检_任务全部完成，自动关闭窗口")
            self._stop_windows(self._peak_windows, "巅峰")

        StallWatchdog(
            poll_s=self.PEAK_SELFCHECK_INTERVAL_MS // 1000,
            fallback_s=self.PEAK_STALL_FALLBACK_MS / 1000,
            round1_s=self.PEAK_STALL_ROUND1_MS / 1000,
            round2_s=self.PEAK_STALL_ROUND2_MS / 1000,
            probe=probe, restart_round=restart_round,
            push_unfinished=lambda p: self._notify_stall_unfinished("巅峰", "巅峰自检", p),
            all_complete=all_complete,
            account_seq=self._account_seq,
            log=lambda m: self._log(f"巅峰自检_{m}"),
            sleep_wait=self._peak_sc_stop.wait,
        ).run(active)

    def _start_chongju_daily_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """启动重聚日常自检线程（防重：已有自检线程存活则跳过）。
        重聚日常不检查任务完成，只要求登录流程结束并获取到登录名。"""
        t = getattr(self, "_chongju_daily_selfcheck_thread", None)
        if t is not None and t.is_alive():
            self._log("重聚日常自检_上一轮自检仍在进行，跳过")
            return
        t = threading.Thread(
            target=self._chongju_daily_selfcheck, args=(exe_path, list(active)), daemon=True)
        self._chongju_daily_selfcheck_thread = t
        t.start()
        self._log(f"重聚日常自检_已启动（{len(active)} 个账号，100 秒后检查登录）")

    def _chongju_daily_refresh_display_name(self, i: int, gw) -> None:
        """复查重聚日常窗口标题（登录后标题含游戏账号名）：
        提取到非空昵称 → 更新 display_name 并通知 GUI 刷新。"""
        try:
            mh = gw.window_handle
            if mh == 0 or not WindowFinder.is_window_valid(mh):
                return
            title = WindowFinder.get_window_title(mh)
            if title in ("", "重聚脱机"):
                return  # 未登录状态
            name = self._extract_username(title, 1)
        except Exception:
            return
        if name:
            account = self._account_manager.accounts[i]
            if account.display_name != name:
                account.display_name = name
                if self._on_state_changed:
                    self._on_state_changed()

    def _chongju_daily_selfcheck(self, exe_path: str, active: list[int]) -> None:
        """重聚日常自检主流程（daemon 线程）：
        阶段0 等100秒 → 阶段1 登录检查（流程结束且 display_name 非空）；
        失败重启再登录（最多 2 轮）；无任务完成检查（只认登录名）。"""
        # 阶段0：最后一个账号登录后 100 秒（wait 可被停止信号立即唤醒）
        self._log("重聚日常自检_等待100秒后检查登录状态…")
        if self._daily_sc_stop.wait(100):
            self._log("重聚日常自检_已收到停止信号，退出")
            return

        # 阶段1：登录检查（未获取到账号名 → 重启再登录，最多 2 轮）
        for round_no in range(1, 4):
            if self._daily_sc_stop.is_set():
                self._log("重聚日常自检_已收到停止信号，退出")
                return
            failed: list[int] = []
            for i in active:
                account = self._account_manager.accounts[i]
                gw = self._daily_windows[i]
                if gw is None:
                    failed.append(i)
                    continue
                # 复查：登录流程中可能读取失败，窗口此刻可能已显示标题昵称
                self._chongju_daily_refresh_display_name(i, gw)
                if not gw.login_finished.is_set() or not account.display_name:
                    failed.append(i)
            if not failed:
                self._log("重聚日常自检_全部账号已获取到登录名，完成")
                return
            if round_no >= 3:
                names = ",".join(str(self._account_seq(i)) for i in failed)
                self._log(f"重聚日常自检_重试2轮后仍有账号未获取到登录名：序号[{names}]，本轮自检结束")
                return
            self._log(f"重聚日常自检_第{round_no}轮发现 {len(failed)} 个账号未登录成功，重启中")
            for i in failed:
                if self._daily_sc_stop.is_set():
                    self._log("重聚日常自检_已收到停止信号，不再重启")
                    return
                account = self._account_manager.accounts[i]
                self._log(f"序号[{self._account_seq(i)}]重聚日常自检_重启并重新登录")
                self.stop_single(i, ClientType.CHONG_JU_DAILY)
                self._launch_one(i, ClientType.CHONG_JU_DAILY, exe_path, account)
                # 标题昵称会先于最后一次平铺出现；必须等整个登录流程收尾后再重排。
                deadline = time.time() + 180
                while time.time() < deadline:
                    if self._daily_sc_stop.is_set():
                        self._log("重聚日常自检_已收到停止信号，退出")
                        return
                    gw = self._daily_windows[i]
                    if gw is None:
                        break
                    if gw.login_finished.wait(timeout=3):
                        self._chongju_daily_refresh_display_name(i, gw)
                        break
            self._retile_chongju_daily_selfcheck(active)

    def _xrym_wait_hwnd(self, main_hwnd: int, cid: int, timeout: float) -> Optional[int]:
        """Win32 轮询等待控件出现（窗口句柄稳定的场景）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            h = self._find_child_by_id(main_hwnd, cid)
            if h is not None:
                return h
            delay(1000)
        return None

    def _xrym_click_retry(self, main_hwnd: int, hwnd: int, verify,
                          retries: int = 3, wait_after: float = 4.0) -> bool:
        """真实点击 + 验证重试：置前+TOPMOST置顶后点击，调用 verify() 检查是否生效。
        真实鼠标点击首次只激活窗口不产生点击事件，重试第二次即生效；
        批量多窗口场景下 TOPMOST 置顶比 SetForegroundWindow 可靠。"""
        for _ in range(retries):
            foreground_window(main_hwnd)
            set_window_topmost(main_hwnd)
            delay(400)
            real_click_control(hwnd)
            delay(wait_after)
            try:
                if verify():
                    return True
            except Exception:
                pass
        return False

    def _xrym_find_dialog(self, pid: int) -> Optional[int]:
        """找可见的套装切换弹窗句柄（独立顶层 WTWindow，标题含「套装切换」；
        残留的隐藏弹窗不计入，避免误判弹窗已出现）"""
        import win32gui
        for h in WindowFinder.find_all_windows_by_pid(pid):
            try:
                if WindowFinder.get_class_name(h) == "WTWindow" and \
                        "套装切换" in (get_control_text(h) or "") and \
                        win32gui.IsWindowVisible(h):
                    return h
            except Exception:
                continue
        return None

    def _listview_find_row(self, list_hwnd: int, text: str) -> int:
        """SysListView32 按首列文本查找行号（LVM_GETITEMCOUNT/LVM_GETITEMTEXT）"""
        import ctypes
        from ctypes import wintypes as wt

        LVM_GETITEMCOUNT = 0x1004
        LVM_GETITEMTEXT = 0x1073
        LVIF_TEXT = 0x0001
        user32 = ctypes.windll.user32

        class LVITEM(ctypes.Structure):
            _fields_ = [("mask", wt.UINT), ("iItem", ctypes.c_int),
                        ("iSubItem", ctypes.c_int), ("state", wt.UINT),
                        ("stateMask", wt.UINT), ("pszText", wt.LPWSTR),
                        ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
                        ("lParam", wt.LPARAM), ("iIndent", ctypes.c_int),
                        ("iGroupId", ctypes.c_int), ("cColumns", wt.UINT),
                        ("puColumns", ctypes.POINTER(wt.UINT)),
                        ("piColFmt", ctypes.POINTER(ctypes.c_int)),
                        ("iGroup", ctypes.c_int)]

        count = user32.SendMessageW(list_hwnd, LVM_GETITEMCOUNT, 0, 0)
        buf = ctypes.create_unicode_buffer(256)
        buf_p = ctypes.cast(buf, ctypes.c_wchar_p)
        for i in range(count):
            item = LVITEM()
            item.mask = LVIF_TEXT
            item.iItem = i
            item.iSubItem = 0
            item.pszText = buf_p
            item.cchTextMax = 256
            if user32.SendMessageW(list_hwnd, LVM_GETITEMTEXT, i, ctypes.byref(item)):
                if buf.value == text:
                    return i
        return -1

    def _listview_click_row(self, list_hwnd: int, row: int, main_hwnd: int) -> bool:
        """真实点击 ListView 指定行（LVM_GETITEMRECT → ClientToScreen → 点击）"""
        import ctypes
        from ctypes import wintypes as wt

        LVM_GETITEMRECT = 0x100E
        LVIR_BOUNDS = 0
        user32 = ctypes.windll.user32

        rect = wt.RECT()
        if not user32.SendMessageW(list_hwnd, LVM_GETITEMRECT, row, ctypes.byref(rect)):
            return False
        pt = wt.POINT(rect.left + (rect.right - rect.left) // 2,
                      rect.top + (rect.bottom - rect.top) // 2)
        user32.ClientToScreen(list_hwnd, ctypes.byref(pt))
        foreground_window(main_hwnd)
        delay(400)
        real_click_screen(pt.x, pt.y)
        return True

    def _xrym_wait(self, w, ctor, timeout: float = 30.0) -> Optional[object]:
        """容错等待 UIA 控件出现：页面切换期间 UIA 遍历偶发 COMError(0x80040201)，
        捕获重试；每次查找限时 1 秒（库默认 TIME_OUT_SECOND=10 会让重试变慢）。
        返回找到的 Control 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ctl = ctor()
                if ctl.Exists(1, 0.5):
                    return ctl
            except Exception:
                pass
            delay(500)
        return None

    def _xrym_switch_suit(self, pid: int, cfg, cid, seq: int) -> None:
        """XryM 换装：Win32 点「套装切换」打开弹窗 → 弹窗稳定后 UIA 选中内置方案
        （ctrl.ini xrym默认方案，默认「单银翼」）→ 点「切换套装」→ 关闭弹窗。
        说明：方案列表是虚拟列表，LVM_GETITEMTEXT 读不到行文本，必须用 UIA；
        弹窗已打开（无页面切换），UIA 深层遍历不会触发 COMError 损坏状态。
        未确认完成时整体重试一轮（实测偶发一次点击被遮挡/焦点干扰导致切换无响应，
        重试自愈；两轮仍失败则继续后续流程，不阻塞登录）。"""
        import uiautomation as ua

        plan_name = cfg.get_xrym_control("xrym默认方案", "单银翼") or "单银翼"

        for _round in range(2):
            # 0) 清理残留的套装弹窗（上次未关闭的隐藏弹窗会干扰本次验证）
            self._xrym_close_dialog(pid, seq)

            # ① 点「套装切换」(530)：点击后长轮询弹窗出现（实测弹窗显示可能延迟数秒，
            #    短验证窗口会误判"弹窗未出现"）
            mh, switch_hwnd = self._xrym_wait_window(pid, cid("xrym套装id", 530), 10)
            if switch_hwnd is None:
                self._log(f"序号[{seq}]XryM日常_未找到套装切换按钮")
                return
            ok_open = False
            for attempt in range(4):
                foreground_window(mh)
                set_window_topmost(mh)
                delay(400)
                real_click_control(switch_hwnd)
                for _ in range(10):  # 10 × 2.5s = 25s 长轮询弹窗出现
                    delay(2500)
                    if self._xrym_find_dialog(pid) is not None:
                        ok_open = True
                        break
                if ok_open:
                    self._log(f"序号[{seq}]XryM日常_套装弹窗已打开(第{attempt+1}次点击)")
                    break
            if not ok_open:
                if _round == 0:
                    self._log(f"序号[{seq}]XryM日常_套装切换弹窗未出现，重试一次")
                    continue
                self._log(f"序号[{seq}]XryM日常_套装切换弹窗未出现")
                return

            # ② 等弹窗稳定（动画完成后再用 UIA 深层遍历）
            delay(2000)

            # ③ UIA：找主窗口 → 弹窗 → 方案列表 → 选中「plan_name」。
            #    弹窗是独立顶层窗口，UIA 树里挂在根下（不从主窗口子级搜）。
            #    子线程使用 uiautomation 必须用 UIAutomationInitializerInThread 初始化 COM
            from uiautomation import UIAutomationInitializerInThread
            with UIAutomationInitializerInThread():
                dlg = ua.WindowControl(Name="套装切换", ClassName="WTWindow")
                if not dlg.Exists(3, 0.5):
                    if _round == 0:
                        self._log(f"序号[{seq}]XryM日常_套装切换弹窗未出现，重试一次")
                        continue
                    self._log(f"序号[{seq}]XryM日常_套装切换弹窗未出现")
                    return

                lst = dlg.ListControl(AutomationId=str(cid("xrym方案列表id", 230)))
                plan = None
                if lst.Exists(2, 1):
                    for item in lst.GetChildren():
                        try:
                            if item.ControlTypeName == "ListItemControl" and item.Name == plan_name:
                                plan = item
                                break
                        except Exception:
                            continue
                if plan is None:
                    if _round == 0:
                        self._log(f"序号[{seq}]XryM日常_方案列表未找到「{plan_name}」，重试一次")
                        continue
                    self._log(f"序号[{seq}]XryM日常_方案列表未找到「{plan_name}」")
                    return
                pr = plan.BoundingRectangle

            # 选中方案：置前最新弹窗 → 真实屏幕点击（UIA Click 首次只激活窗口）
            dlg_hwnd = self._xrym_find_dialog(pid)
            if dlg_hwnd is None:
                if _round == 0:
                    self._log(f"序号[{seq}]XryM日常_未找到弹窗句柄，重试一次")
                    continue
                self._log(f"序号[{seq}]XryM日常_未找到弹窗句柄")
                return
            foreground_window(dlg_hwnd)
            delay(400)
            real_click_screen((pr.left + pr.right) // 2, (pr.top + pr.bottom) // 2)
            delay(600)

            # ④ 点「切换套装」(140)：Win32 动态查找（选中方案后弹窗可能重建，
            #    每次重新拿最新弹窗句柄与控件坐标），验证输出框「套装已切换完成」
            switch_btn_hwnd = None
            for _ in range(5):
                dlg_hwnd = self._xrym_find_dialog(pid)
                if dlg_hwnd is not None:
                    for h in get_child_windows(dlg_hwnd):
                        if get_control_id(h) == cid("xrym切换套装id", 140):
                            switch_btn_hwnd = h
                            break
                if switch_btn_hwnd is not None:
                    break
                delay(1000)
            if switch_btn_hwnd is None:
                if _round == 0:
                    self._log(f"序号[{seq}]XryM日常_未出现切换套装按钮，重试一次")
                    continue
                self._log(f"序号[{seq}]XryM日常_未出现切换套装按钮")
                return

            def suit_done() -> bool:
                _mh, _out = self._xrym_wait_window(pid, cid("xrym输出id", 320), 2)
                if _out is None:
                    return False
                try:
                    text = get_control_text(_out)
                except Exception:
                    return False
                # 实测 XryM 输出框日志为「套装已切换完毕！」（完毕，非完成）
                return "套装已切换完毕" in text or "套装已切换完成" in text
            import win32gui
            sr = win32gui.GetWindowRect(switch_btn_hwnd)
            dlg_hwnd = self._xrym_find_dialog(pid) or dlg_hwnd
            foreground_window(dlg_hwnd)
            delay(400)
            real_click_screen((sr[0] + sr[2]) // 2, (sr[1] + sr[3]) // 2)
            self._log(f"序号[{seq}]XryM日常_已点击切换套装，等待完成...")
            for _ in range(10):  # 10 × 3s = 30s
                delay(3000)
                if suit_done():
                    self._log(f"序号[{seq}]XryM日常_已切换套装「{plan_name}」")
                    break
            else:
                if _round == 0:
                    self._log(f"序号[{seq}]XryM日常_切换套装未确认完成，重试一次")
                    continue
                self._log(f"序号[{seq}]XryM日常_切换套装未确认完成")
            break  # 成功或两轮失败 → 退出重试循环

        # ⑤ 关闭弹窗：WM_CLOSE 优先（不依赖鼠标），失败再坐标点击；
        #    确认弹窗消失后才返回，避免「开始任务」在弹窗遮挡下操作
        self._xrym_close_dialog(pid, seq)

    def _xrym_close_dialog(self, pid: int, seq: int) -> bool:
        """关闭套装切换弹窗并确认消失：WM_CLOSE 优先（不依赖鼠标/前台/Z序），
        失败再坐标点击右上角；最多重试 3 轮"""
        import ctypes
        from ctypes import wintypes as wt

        WM_CLOSE = 0x0010
        for attempt in range(3):
            dlg_hwnd = self._xrym_find_dialog(pid)
            if dlg_hwnd is None:
                return True
            ctypes.windll.user32.SendMessageW(dlg_hwnd, WM_CLOSE, 0, 0)
            for _ in range(8):  # 8 × 1s = 8s
                delay(1000)
                if self._xrym_find_dialog(pid) is None:
                    self._log(f"序号[{seq}]XryM日常_套装弹窗已关闭")
                    return True
            # 兜底：置前弹窗 → 点击右上角关闭按钮
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(dlg_hwnd, ctypes.byref(rect))
            foreground_window(dlg_hwnd)
            delay(400)
            real_click_screen(rect.right - 12, rect.top + 10)
            for _ in range(8):
                delay(1000)
                if self._xrym_find_dialog(pid) is None:
                    self._log(f"序号[{seq}]XryM日常_套装弹窗已关闭")
                    return True
            self._log(f"序号[{seq}]XryM日常_关闭套装弹窗失败，重试...")
        self._log(f"序号[{seq}]XryM日常_关闭套装弹窗失败！")
        return False

    # ============================================================
    # 登录调度
    # ============================================================

    def login_window(self, index: int, client_type: ClientType, gw: GameWindow) -> None:
        """
        窗口登录线程入口。根据 client_type 分发到具体登录流程。
        对应 EPL：执行窗口线程 → 主线程_日常登录 / 主线程_巅峰登录 / 主线程_重聚日常登录
        外层 try/except 兜底：窗口可能在登录中途被「关闭」杀死，避免线程以未捕获异常
        直接打印 traceback（不影响主程序，但会污染终端）。
        """
        try:
            if gw.login_cancelled.is_set():
                return
            if client_type == ClientType.LEI_XIAO_YI:
                self._login_lei_xiao_yi(index, gw)
            elif client_type == ClientType.CHONG_JU_PEAK:
                self._login_chong_ju_peak(index, gw)
            elif client_type == ClientType.CHONG_JU_DAILY:
                self._login_chong_ju_daily(index, gw)
        except Exception:
            # 窗口已被关闭等 Win32 异常：静默记录，不中断主程序
            self._log(f"序号[{self._account_seq(index)}]登录线程异常（可能窗口已关闭）：忽略")
        finally:
            # 自检必须等待本轮完整登录流程结束；昵称提前出现不等同于任务已成功启动。
            gw.login_finished.set()

    # ============================================================
    # 雷小伊日常登录
    # ============================================================

    def _lei_login_cancelled(self, i: int, gw: GameWindow, stage: str) -> bool:
        """判断本轮雷小伊登录是否已被关闭/重启取代。
        关闭时不能让旧线程继续换装、勾选或点击开始按钮。"""
        if gw.login_cancelled.is_set() or self._daily_windows[i] is not gw:
            self._log(f"序号[{self._account_seq(i)}]日常_登录已取消，停止{stage}")
            return True
        return False

    def _chong_ju_login_cancelled(self, i: int, gw: GameWindow, stage: str,
                                  main_hwnd: int = 0) -> bool:
        """判断重聚（巅峰/日常）登录是否已被「全部关闭」/单个关闭/重启取代。
        批量关闭此前只杀进程不设取消标记，登录线程会在已死窗口上空转
        （30秒控件匹配 + 30秒初始化 + 8秒等待逐段跑完），表现为点完
        「全部关闭」后日志还在持续输出「控件缺失」；_stop_windows 现已
        补设 login_cancelled，这里再加「窗口已失效」兜底（进程被杀后
        Win32 调用不一定抛异常，死等只会白耗时间）。"""
        if (
            gw.login_cancelled.is_set()
            or (self._peak_windows[i] is not gw and self._daily_windows[i] is not gw)
            or (main_hwnd and not WindowFinder.is_window_valid(main_hwnd))
        ):
            self._log(f"序号[{self._account_seq(i)}]登录已取消，停止{stage}")
            return True
        return False

    def _find_update_popup(self, pid: int, main_hwnd: int,
                           expected_title: str = _PEAK_MAIN_TITLE) -> int:
        """查找重聚客户端（巅峰/日常共用）的更新弹窗句柄（实测特征）：
        同 pid 顶层窗口、类名含 WTWindow、可见、标题非空且≠主窗口标题。
        实测弹窗（巅峰 2.8.x 与日常 2.2.x 完全一致）：类名 WTWindow、标题
        「检测到新版本 Vx.y.z」、置顶可见，子控件 ID=220「立即更新」；
        主窗口有更新时一直隐藏。返回 0 表示未发现。"""
        for h in WindowFinder.find_all_windows_by_pid(pid):
            if "WTWindow" not in WindowFinder.get_class_name(h):
                continue
            if h == main_hwnd:
                continue
            title = WindowFinder.get_window_title(h)
            if title and title != expected_title:
                return h
        return 0

    def _dismiss_update_popup(self, hwnd: int) -> bool:
        """点重聚客户端更新弹窗的「取消」按钮关闭弹窗（自动更新未开启时用，
        对齐 XryM 模式1「继续使用该版本」）。实测弹窗子控件：文本「取消」
        按钮 ID=130（巅峰 2.8.x 与日常 2.2.x 一致）；「×」ID=240 兜底。
        点取消后弹窗销毁、主窗口数秒内变可见，登录流程继续走。
        返回是否找到并点击了取消按钮。"""
        cancel_hwnd = 0
        for child_hwnd in get_child_windows(hwnd):
            text = get_control_text(child_hwnd).strip()
            if text == "取消":
                cancel_hwnd = child_hwnd
                break
            if not cancel_hwnd and get_control_id(child_hwnd) == 130:
                cancel_hwnd = child_hwnd  # ID 兜底（文本匹配优先，不 break）
        if not cancel_hwnd:
            self._log("更新弹窗内未找到「取消」按钮，弹窗将保留（登录可能超时）")
            return False
        post_click(cancel_hwnd)
        # 等弹窗销毁（点取消应立即销毁；上限 30 秒防死等）
        dismiss_deadline = time.time() + 30.0
        while WindowFinder.is_window_valid(hwnd) and time.time() < dismiss_deadline:
            time.sleep(0.5)
        return True

    def _lei_wait_task_start_response(self, gw: GameWindow, before: str,
                                      timeout: float = 12.0) -> bool:
        """确认“开始”被客户端响应：输出框内容在点击后产生变化。
        雷小伊没有可读的任务运行状态控件，输出日志追加是当前版本可验证的响应信号。"""
        if gw.output_handle <= 0:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if gw.login_cancelled.is_set():
                return False
            try:
                if external_edit_get_content(gw.output_handle) != before:
                    return True
            except Exception:
                return False
            time.sleep(0.5)
        return False

    def _resolve_update_flag(self) -> None:
        """取消/中止路径兜底：更新标志仍处等待态（-1）时放行为 0，
        防止批量主循环在「等待首账号更新检测」处永久挂起
        （场景：批量启动中首账号被右键单关/全部关闭，登录线程提前退出）。"""
        if self._update_flag == -1:
            self._update_flag = 0

    def _lei_handle_update(self, i: int, gw: GameWindow, hint_hwnd: int,
                           allow_update: bool) -> str:
        """处理雷小伊「提示」更新弹窗（2026-09 实测行为）。
        返回值：
        - "relaunched"：已下载新版并完成进程替换，调用方刷新 pid 重走①②
        - "dismissed" ：弹窗已关闭，用当前进程继续登录
                        （单号/自检运行/轮次用尽/下载失败回退/版本不升）
        - "cancelled" ：收到停止/取消信号，调用方直接 return
        - "aborted"   ：新版与旧版进程均启动失败，该账号登录中止
        实测（diag_lei_cancel.py）：点「取消」→ 弹窗销毁、主窗口变可见、
        进程存活、不开浏览器（「确定」会开下载页标签页，纯 HTTP 下载不需要）。"""
        seq = self._account_seq(i)
        account = self._account_manager.accounts[i]

        # 读弹窗正文（id=65535）首行做日志确认
        body = ""
        try:
            for ch in get_child_windows(hint_hwnd):
                if get_control_id(ch) == 65535:
                    body = get_control_text(ch) or ""
                    break
        except Exception:
            body = ""
        first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()),
                          "检测到新版本")
        self._log(f"序号[{seq}]日常更新_检测到更新弹窗：{first_line}")

        # 点「取消」(id=2) 关闭弹窗：纯 HTTP 下载不需要浏览器
        clicked = False
        try:
            for ch in get_child_windows(hint_hwnd):
                if get_control_id(ch) == 2:
                    try:
                        if "取消" in (get_control_text(ch) or ""):
                            real_click_control(ch)
                            clicked = True
                            break
                    except Exception:
                        pass
            if not clicked:
                WindowFinder.close_window(hint_hwnd)
        except Exception:
            try:
                WindowFinder.close_window(hint_hwnd)
            except Exception:
                pass
        delay(500)
        self._log(f"序号[{seq}]日常更新_已关闭更新弹窗")

        if self._lei_login_cancelled(i, gw, "自动更新"):
            return "cancelled"

        if not allow_update:
            self._log(f"序号[{seq}]日常更新_单号/自检运行不自动更新，用当前版本继续")
            return "dismissed"

        # —— 全自动更新：纯 HTTP 下载新版 → 写回配置 → 换进程 ——
        old_path = getattr(gw, "exe_path", "") or \
            self._config_manager.get_current_daily_path()
        dest_dir = os.path.dirname(old_path) or r"D:\Games\Seer\lxy"
        account.current_action = "日常更新"
        if self._on_state_changed:
            self._on_state_changed()

        try:
            from .lei_updater import (download_latest_lei, LeiUpdateError,
                                      LeiUpdateCancelled)
        except ImportError as e:
            self._log(f"序号[{seq}]日常更新_下载模块不可用（requests 未安装？）：{e}")
            self._lei_resume_run_action(account)
            return "dismissed"

        def _cancelled() -> bool:
            return gw.login_cancelled.is_set() or not self._exec_permit

        try:
            new_path = download_latest_lei(
                dest_dir, current_path=old_path,
                log=lambda m: self._log(f"序号[{seq}]日常更新_{m}"),
                cancel_check=_cancelled,
            )
        except LeiUpdateCancelled:
            self._log(f"序号[{seq}]日常更新_已取消下载")
            return "cancelled"
        except Exception as e:  # LeiUpdateError / 网络异常等：降级用旧版继续
            self._log(f"序号[{seq}]日常更新_自动下载失败：{e}")
            self._log(f"序号[{seq}]日常更新_用当前版本继续登录（可稍后手动更新）")
            self._lei_resume_run_action(account)
            return "dismissed"

        if _cancelled():
            return "cancelled"
        self._log(f"序号[{seq}]日常更新_新版已就绪：{os.path.basename(new_path)}")

        # 写回配置（客户端列表雷小伊日常条目 + [雷小伊] 雷小伊目录7）+ 批内路径缓存
        self._persist_lei_path(new_path)
        self._lei_updated_path = new_path

        # 关旧进程 → 用新版重启，替换 gw 进程字段（XryM 更新同款进程替换）
        self._process_manager.kill(gw)
        if _cancelled():
            return "cancelled"
        new_gw = self._process_manager.launch(new_path)
        if new_gw is not None:
            gw.process_id = new_gw.process_id
            gw.process = new_gw.process
            gw.window_handle = 0
            gw.timed_out = False
            gw.exe_path = new_path
            self._log(f"序号[{seq}]日常更新_新版进程已启动 pid={gw.process_id}，重走登录流程")
            self._lei_resume_run_action(account)
            return "relaunched"

        # 新版启动失败（罕见：文件已过 MZ/大小校验）→ 旧版兜底重启
        err = getattr(self._process_manager, "last_error", "")
        self._log(f"序号[{seq}]日常更新_新版启动失败：{err}，尝试用旧版重启")
        old_gw = self._process_manager.launch(old_path)
        if old_gw is None:
            err2 = getattr(self._process_manager, "last_error", "")
            self._log(f"序号[{seq}]日常更新_旧版重启也失败：{err2}，该账号登录中止")
            gw.timed_out = True
            return "aborted"
        gw.process_id = old_gw.process_id
        gw.process = old_gw.process
        gw.window_handle = 0
        gw.exe_path = old_path
        # 回退：批内后续账号继续用旧版（配置已指向新版，下轮批量自然切换）
        self._lei_updated_path = ""
        self._log(f"序号[{seq}]日常更新_已用旧版重启 pid={gw.process_id}")
        self._lei_resume_run_action(account)
        return "relaunched"

    def _lei_resume_run_action(self, account: Account) -> None:
        """自动更新流程结束后恢复账号「运行日常」显示状态。"""
        account.current_action = "运行日常"
        if self._on_state_changed:
            self._on_state_changed()

    def _persist_lei_path(self, new_path: str) -> None:
        """把雷小伊新版本路径写回 ctrl.ini
        （[客户端列表] 雷小伊日常条目 + [雷小伊] 雷小伊目录7，对齐 _persist_xrym_path）"""
        cfg = self._config_manager
        entries = cfg.get_client_entries()
        changed = False
        for e in entries:
            if e.kind == "日常" and "雷小伊" in e.name:
                if e.path != new_path:
                    e.path = new_path
                    changed = True
                break
        if changed:
            cfg.set_client_entries(entries)
        cfg.set_client_path("雷小伊", "雷小伊目录7", new_path)
        try:
            cfg.save()
        except Exception as e:
            self._log(f"雷小伊更新_写回配置失败: {e}")

    # ---- 雷小伊登录：就绪等待 + 消息点击 + 验证重试（2026-09-20 实测新增）----
    # 实测（真实账号 900000006 / pid=94308，证据 _exp_real_login.txt）：
    #   客户端启动后登录按钮（控件 21）约 22 秒才从 disabled 变 enabled；
    #   此前 PostMessage BM_CLICK 与真实鼠标点击都会被客户端丢弃（点击不生效），
    #   旧逻辑固定在此刻点击，正是首个账号必踩 33 秒超时的根因（日志中序号[1]
    #   三次失败都在客户端启动后 19~20 秒点击；序号 2~5 因串行排队 ≥32 秒才点，全成功）。
    #   按钮就绪后 PostMessage BM_CLICK 1~2 秒内即被客户端接收，且不依赖窗口前台/
    #   是否被遮挡；真实鼠标点击依赖 foreground_window 置前，置前失败即点空
    #   （多窗口重叠场景的偶发失效来源），故仅作兜底。

    # 等待登录按钮可用上限（实测约 22 秒就绪，留足余量）
    LEI_LOGIN_READY_TIMEOUT = 90.0
    # 单次点击后的响应判定窗口（实测点击到登录成功 6~10 秒，取 15 秒留余量；
    # 实测 04:21:25 点击 → 04:21:35 命中，正好卡在 10 秒边界上）
    LEI_LOGIN_RESPONSE_TIMEOUT = 15.0

    def _lei_find_dialog(self, main_hwnd: int, keyword: str) -> int:
        """查找该客户端进程的可见提示弹窗（#32770，正文含 keyword）。

        雷小伊登录类提示（「用户名或密码错误」「参数账号错误」等）都是 #32770
        顶层窗口，标题「信息：」，正文在子控件里。返回 0 表示未找到。
        """
        if not main_hwnd or not WindowFinder.is_window_valid(main_hwnd):
            return 0
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(main_hwnd, ctypes.byref(pid))
        try:
            for h in WindowFinder.find_all_windows_by_pid(pid.value):
                if WindowFinder.get_class_name(h) != "#32770":
                    continue
                for ch in get_child_windows(h):
                    try:
                        if keyword in (get_control_text(ch) or ""):
                            return h
                    except Exception:
                        continue
        except Exception:
            return 0
        return 0

    def _lei_wait_login_button_ready(self, i: int, gw: GameWindow, main_hwnd: int,
                                     login_hwnd: int) -> bool:
        """等待登录按钮可用（=客户端界面就绪）；窗口/流程失效时返回 False。"""
        seq = self._account_seq(i)
        start = time.time()
        while time.time() - start < self.LEI_LOGIN_READY_TIMEOUT:
            if gw.login_cancelled.is_set():
                return False
            if not WindowFinder.is_window_valid(login_hwnd):
                return False
            try:
                if win32gui.IsWindowEnabled(login_hwnd):
                    waited = time.time() - start
                    if waited >= 1.0:
                        self._log(f"序号[{seq}]日常_登录按钮就绪（等待{waited:.0f}秒）")
                    return True
            except Exception:
                return False
            time.sleep(0.5)
        self._log(f"序号[{seq}]日常_登录按钮未就绪（等待"
                  f"{self.LEI_LOGIN_READY_TIMEOUT:.0f}秒超时）")
        return False

    def _lei_read_output(self, gw: GameWindow) -> str:
        """读雷小伊输出框全文（失败返回空串）"""
        try:
            if gw.output_handle and gw.output_handle > 0:
                return external_edit_get_content(gw.output_handle) or ""
        except Exception:
            pass
        return ""

    def _lei_wait_login_response(self, gw: GameWindow, name_hwnd: int,
                                 before_output: str = "", timeout: float = 0) -> bool:
        """点击登录后的响应判定：账号名控件出现昵称，或输出框**新增**「登录成功」。

        密码框内容读不回来（WM_GETTEXT 恒返回空，实测真实键盘输入前后长度都是 0），
        因此不能用密码框判断密码是否输入成功。
        before_output：点击前输出框内容 —— 只认新增部分的「登录成功」，避免同进程
        重新登录时窗口内的历史日志造成误判。
        """
        if timeout <= 0:
            timeout = self.LEI_LOGIN_RESPONSE_TIMEOUT
        deadline = time.time() + timeout
        while time.time() < deadline:
            if gw.login_cancelled.is_set():
                return False
            try:
                if get_control_text(name_hwnd).strip():
                    return True
            except Exception:
                pass
            try:
                content = self._lei_read_output(gw)
                if content:
                    added = content[len(before_output):] if content.startswith(before_output) else content
                    if "登录成功" in added:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _lei_click_login(self, i: int, gw: GameWindow, main_hwnd: int, login_hwnd: int,
                         name_hwnd: int, account_hwnd: int, password_hwnd: int,
                         account: Account, retries: int = 3) -> bool:
        """点击登录按钮 + 验证重试：先消息点击，无响应再真实点击兜底。

        返回值仅用于日志；即使返回 False，调用方仍保留原有的账号名轮询兜底。
        """
        seq = self._account_seq(i)
        for attempt in range(1, retries + 1):
            if gw.login_cancelled.is_set() or not WindowFinder.is_window_valid(main_hwnd):
                return False
            before = self._lei_read_output(gw)
            post_click(login_hwnd)
            if self._lei_wait_login_response(gw, name_hwnd, before):
                self._log(f"序号[{seq}]日常_登录点击已生效（第{attempt}次·消息点击）")
                return True
            # 「用户名或密码错误」/「参数账号错误」= 点击其实已被接收，只是账号或密码
            # 没输进去（密码框内容读不回来，无法校验，只能重输）→ 关弹窗重输后重点击
            err_hwnd = self._lei_find_dialog(main_hwnd, "错误")
            if err_hwnd:
                self._log(f"序号[{seq}]日常_登录弹窗提示账号/密码错误 → 重输账号密码后重点击")
                WindowFinder.close_window(err_hwnd)
                time.sleep(0.5)
                send_text(account_hwnd, account.mi_hao)
                delay(300)
                foreground_window(main_hwnd)
                delay(200)
                input_real_keyboard(main_hwnd, password_hwnd, account.password)
                delay(500)
                continue
            # 消息点击无响应 → 兜底：置前 + 置顶 + 真实点击（旧逻辑方式）
            self._log(f"序号[{seq}]日常_登录点击第{attempt}次无响应，改真实点击兜底")
            foreground_window(main_hwnd)
            set_window_topmost(main_hwnd)
            delay(400)
            real_click_control(login_hwnd)
            if self._lei_wait_login_response(gw, name_hwnd, before):
                self._log(f"序号[{seq}]日常_登录点击已生效（第{attempt}次·真实点击）")
                return True
        self._log(f"序号[{seq}]日常_登录点击{retries}次均未确认生效，继续等待账号名")
        return False

    def _login_lei_xiao_yi(self, i: int, gw: GameWindow) -> None:
        """
        雷小伊日常登录。
        对应 EPL：主线程_日常登录
        控件ID索引：19=账号 20=密码 21=登录 22=套装 24=开始 26=账号名 52=输出框
        """
        pid = gw.process_id
        account = self._account_manager.accounts[i]
        self._log(f"序号[{self._account_seq(i)}]日常_登录中")

        # 更新处理轮次（新版启动后仍弹更新提示时最多 3 轮，防循环）：
        # 每轮 = 关欢迎弹窗 → 检「提示」更新弹窗 →（自动更新则下载+换进程后重走）
        update_rounds = 0
        while True:
            # ① 查找并关闭启动消息框（#32770 类名，标题为「信息:」半角冒号；只匹配「信息」以兼容全/半角）
            #    注意：实测该弹窗在客户端启动后约 41 秒才出现，必须给足等待时间；
            #    单次 60 秒阻塞查找期间无法响应「全部关闭」，改为 2 秒分片轮询
            info_hwnd = None
            info_deadline = time.time() + 60.0
            while time.time() < info_deadline:
                if self._lei_login_cancelled(i, gw, "更新检测"):
                    self._resolve_update_flag()
                    return
                info_hwnd = WindowFinder.find_window_by_pid(
                    pid, "信息", "#32770", match_full_title=False, timeout=2.0
                )
                if info_hwnd:
                    break
            if info_hwnd:
                WindowFinder.close_window(info_hwnd)
            if self._lei_login_cancelled(i, gw, "更新检测"):
                self._resolve_update_flag()
                return

            # ② 检测「提示」更新弹窗（所有账号都查）：
            #    - 首账号（i==0）+ 批量启动 + 轮次未满 + [雷小伊] 雷小伊启动更新
            #      勾选 → 全自动更新（纯 HTTP 下载新版 → 写回配置 → 换进程，
            #      见 _lei_handle_update）
            #    - 其余账号/单号/自检重启/轮次用尽/下载失败回退/未勾选自动更新
            #      → 只点「取消」关弹窗后用当前版本继续登录（实测点取消：弹窗
            #      销毁、主窗口变可见、进程存活、不开浏览器，见 diag_lei_cancel.py）
            allow_update = (
                getattr(gw, "allow_login_update", True)
                and i == 0
                and update_rounds < 3
                # [雷小伊] 雷小伊启动更新 勾选（默认真；未勾选=每账号点取消继续
                # 登录，对齐 XryM 模式1「继续使用该版本」）。getattr 兼容旧测试桩
                and getattr(self._config_manager, "get_lei_auto_update", lambda: True)()
            )
            hint_hwnd = 0
            deadline = time.time() + 5.0
            while time.time() < deadline:
                hint_hwnd = WindowFinder.find_window_by_pid(
                    pid, "提示", "#32770", match_full_title=False, timeout=0.5
                )
                if hint_hwnd:
                    break
                if self._lei_login_cancelled(i, gw, "更新检测"):
                    self._resolve_update_flag()
                    return
                time.sleep(0.5)

            if not hint_hwnd:
                if i == 0:
                    self._log("日常更新检测已过")
                self._update_flag = 0
                break

            result = self._lei_handle_update(i, gw, hint_hwnd, allow_update)
            if result == "cancelled":
                self._resolve_update_flag()
                return
            if result == "aborted":
                self._resolve_update_flag()
                gw.timed_out = True
                return
            if result == "relaunched":
                # 新版进程已替换 gw（pid 已变）→ 重走 ①②（新版的欢迎弹窗等）
                update_rounds += 1
                pid = gw.process_id
                continue
            # dismissed：弹窗已关（取消），用当前进程继续登录
            if i == 0:
                self._log("日常更新检测已过（用当前版本继续登录）")
            self._update_flag = 0
            break

        # ③ 进入临界区，查找 WTWindow
        with self._key_lock:
            main_hwnd = WindowFinder.find_window_by_pid(
                pid, "", "WTWindow", match_full_title=False, timeout=self.WINDOW_WAIT_TIMEOUT
            )
            if main_hwnd is None:
                self._log(f"序号[{self._account_seq(i)}]日常_等待WTWindow超时")
                self._update_flag = 0
                gw.timed_out = True
                return

            gw.window_handle = main_hwnd

            # 等待子窗口就绪
            children = self._wait_for_children(main_hwnd)
            gw.control_snapshot = {}

            # ④ 按控件ID匹配子窗口
            handles: dict[int, int] = {}
            for child_hwnd in children:
                cid = get_control_id(child_hwnd)
                if not cid:
                    continue  # 护栏：配置缺键补 0 时严禁误绑 id=0 子控件（同 _match_peak_controls）
                if cid == self._get_config(19):      # 账号
                    handles[1] = child_hwnd
                elif cid == self._get_config(20):    # 密码
                    handles[2] = child_hwnd
                elif cid == self._get_config(21):    # 登录
                    handles[3] = child_hwnd
                elif cid == self._get_config(22):    # 选择套装
                    handles[4] = child_hwnd
                elif cid == self._get_config(24):    # 开始
                    handles[5] = child_hwnd
                elif cid == self._get_config(26):    # 账号名
                    handles[8] = child_hwnd
                elif cid == self._get_config(52):    # 输出框
                    gw.output_handle = child_hwnd

            # ⑤ 平铺窗口
            self._tile_lei_xiao_yi(i, main_hwnd)

            # ⑥ 输入账号密码 → 登录
            # 批量时窗口已平铺各自位置，只置前不移位（真实键盘输入依赖前台窗口）
            foreground_window(main_hwnd)

            account_hwnd = handles.get(1)
            password_hwnd = handles.get(2)
            login_hwnd = handles.get(3)
            name_hwnd = handles.get(8)
            suit_hwnd = handles.get(4)
            start_hwnd = handles.get(5)

            if not all([account_hwnd, password_hwnd, login_hwnd, name_hwnd, suit_hwnd, start_hwnd]):
                missing = [k for k, v in [(1, account_hwnd), (2, password_hwnd),
                         (3, login_hwnd), (4, suit_hwnd), (5, start_hwnd), (8, name_hwnd)] if v is None]
                self._log(f"序号[{self._account_seq(i)}]日常_控件缺失: {missing}")
                self._update_flag = 0
                gw.timed_out = True
                return

            # 就绪等待：客户端启动初期登录按钮 disabled（实测约启动后 22 秒才可用），
            # 此时任何点击都会被客户端丢弃 —— 旧逻辑固定在此刻点击，是首个账号
            # 33 秒登录超时的根因。密码真实键盘输入也放到就绪之后，避免界面未就绪时丢字符。
            if not self._lei_wait_login_button_ready(i, gw, main_hwnd, login_hwnd):
                if gw.login_cancelled.is_set():
                    self._update_flag = 0
                    return
                if not WindowFinder.is_window_valid(main_hwnd):
                    self._log(f"序号[{self._account_seq(i)}]日常_主窗口已失效，登录中止")
                    self._update_flag = 0
                    gw.timed_out = True
                    return
                self._log(f"序号[{self._account_seq(i)}]日常_登录按钮未就绪，仍按原流程尝试点击")

            send_text(account_hwnd, account.mi_hao)
            delay(500)
            # 密码框拒绝 WM_SETTEXT/WM_CHAR，只接受真实键盘输入（实测确认）
            input_real_keyboard(main_hwnd, password_hwnd, account.password)
            delay(500)

            # 登录点击：优先消息点击（PostMessage BM_CLICK，实测按钮就绪后 1~2 秒即被
            # 客户端接收，且不依赖窗口前台/遮挡；旧注释「PostMessage 对按钮不可靠」
            # 已由 2026-09-20 实测推翻），无响应再由 _lei_click_login 内部兜底真实点击
            self._lei_click_login(i, gw, main_hwnd, login_hwnd, name_hwnd,
                                  account_hwnd, password_hwnd, account)
            delay(1000)

            # ⑦ 等待登录完成（账号名控件内容非空）
            n = 1
            while get_control_text(name_hwnd) == "":
                delay(3000)
                n += 1
                if n >= self.LOGIN_TIMEOUT_CHECKS:
                    self._log(f"序号[{self._account_seq(i)}]日常_登录超时！！")
                    self._notifier.notify_login_timeout(i, "日常")
                    self._update_flag = 0
                    gw.timed_out = True
                    return

            account.display_name = get_control_text(name_hwnd).strip()
            # 第一重确认：登录后账号名控件必须已有值；第二重将在点击开始后确认输出响应。
            if not account.display_name:
                self._log(f"序号[{self._account_seq(i)}]日常_账号名确认失败")
                gw.timed_out = True
                return
            self._log(f"序号[{self._account_seq(i)}]日常_账号名已确认: {account.display_name}")
            if self._on_state_changed:
                self._on_state_changed()  # 通知 GUI 刷新运行页（账号名列显示 display_name）
            delay(3500)

            # ⑧ 套装切换（UIA：选中默认方案 → 点击「切换此方案」）
            if self._lei_login_cancelled(i, gw, "套装切换"):
                return
            set_focus(main_hwnd)
            post_click(suit_hwnd)

            # 等待"套装切换"WTWindow
            suit_window = WindowFinder.find_window_by_pid(
                pid, "套装切换", "WTWindow", match_full_title=False, timeout=30.0
            )
            if suit_window is None:
                self._log(f"序号[{self._account_seq(i)}]日常_等待套装窗口超时")
                self._update_flag = 0
                gw.timed_out = True
                return

            # UIA 换装：必须选中方案后再切换（未选中就点「切换此方案」会清空当前穿戴，故选不中时跳过切换）
            if self._lei_login_cancelled(i, gw, "套装切换"):
                WindowFinder.close_window(suit_window)
                return
            self._switch_suit_with_uia(suit_window)

            # 关闭套装窗口
            WindowFinder.close_window(suit_window)
            if self._lei_login_cancelled(i, gw, "任务勾选"):
                return

            # ⑧.5 任务勾选：Unity 全选后取消"月华"+"日常-宿命王者的锻炼"；因子勾前两项
            #     雷小伊 1.8.13 任务列表为虚拟 SysListView32（LVS_OWNERDATA），
            #     Win32 LVM 消息读不到文本/状态，必须用 UIA。
            #     标签切换用 CPageControl TCM_SETCURSEL(0x130C)（TCM_SETCURFOCUS 无效）。
            #     复选框勾选用真实鼠标点击行左侧（UIA TogglePattern.Toggle 不生效）。
            #     列表头"开"列可点击触发全选/全不选（不满→全选，已满→全不选）。
            if not self._lei_xiao_yi_setup_tasks(main_hwnd, pid, i, gw):
                return
            if self._lei_login_cancelled(i, gw, "开始任务"):
                return

            # ⑨ 点击“开始”并确认输出框在点击后发生变化。
            # 账号名和按钮响应双重成立，才允许自检进入任务完成检查。
            before_output = ""
            try:
                if gw.output_handle > 0:
                    before_output = external_edit_get_content(gw.output_handle)
            except Exception:
                pass
            foreground_window(main_hwnd)
            delay(300)
            real_click_control(start_hwnd)
            if not self._lei_wait_task_start_response(gw, before_output):
                if not gw.login_cancelled.is_set():
                    self._log(f"序号[{self._account_seq(i)}]日常_开始任务未确认响应")
                    gw.timed_out = True
                return
            # 仅在正确账号名已读取、完整前置步骤和“开始”响应均确认后，才判为成功。
            gw.login_succeeded = True
            self._log(f"序号[{self._account_seq(i)}]日常_已登录且任务已启动！")

        # 退出临界区（with 语句自动释放）

    # ============================================================
    # 雷小伊任务勾选（UIA + 真实鼠标点击）
    # ============================================================

    # 勾选策略自 2026-09 起可配置：ctrl.ini [雷小伊勾选] 节（配置页「雷小伊任务勾选」
    # 分组编辑），默认值在 config.py（LEI_TASK_DEFAULT_*，= 迁移前的硬编码常量，
    # 老配置无该节时行为零变化）。2026-10 起支持每账号独立配置：
    # ctrl.ini 新增 [雷小伊勾选.<米米号>]（特定账号）与 [雷小伊勾选.新号]（新号），
    # 引擎按「特定账号 > 新号配置 > 全局默认」优先级解析。

    def _lei_task_config(self) -> LeiTaskConfig:
        """读取全局默认勾选策略。每次勾选执行时现读——配置页保存后，
        下一轮批量登录即生效，无需重启中控台。
        兼容桩配置（无 get_lei_task_config 方法，如单元测试 _FakeConfig）→ 默认策略。"""
        getter = getattr(self._config_manager, "get_lei_task_config", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return LeiTaskConfig()

    def _lei_task_config_for(self, is_new: bool, mi_hao: str = "") -> LeiTaskConfig:
        """按账号优先级读取勾选策略：特定账号 > 新号配置 > 全局默认。
        兼容桩配置（无 get_lei_task_config_for，如单元测试 _FakeConfig）：
        依次退化为读全局 → 再退化为默认策略。"""
        resolver = getattr(self._config_manager, "get_lei_task_config_for", None)
        if callable(resolver):
            try:
                return resolver(is_new, mi_hao)
            except Exception:
                pass
        return self._lei_task_config()

    def _peak_run_config(self) -> PeakRunConfig:
        """读取全局默认巅峰运行选项。每次登录现读——配置页保存后，
        下一轮启动即生效，无需重启中控台。
        兼容桩配置（无 get_peak_run_config，如单元测试 _FakeConfig）→ 全假。"""
        getter = getattr(self._config_manager, "get_peak_run_config", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return PeakRunConfig()

    def _peak_run_config_for(self, is_new: bool, mi_hao: str = "") -> PeakRunConfig:
        """按账号优先级读取巅峰运行选项：特定账号 > 新号配置 > 全局默认。
        兼容桩配置（无 get_peak_run_config_for）：依次退化为读全局 → 默认。"""
        resolver = getattr(self._config_manager, "get_peak_run_config_for", None)
        if callable(resolver):
            try:
                return resolver(is_new, mi_hao)
            except Exception:
                pass
        return self._peak_run_config()

    # Win32 常量
    _TCM_SETCURSEL = 0x130C
    _LVM_GETITEMCOUNT = 0x1004
    _LVM_ENSUREVISIBLE = 0x1013
    _LVM_SCROLL = 0x100F

    def _lei_find_child_by_class(self, hwnd: int, class_name: str) -> int:
        """递归查找指定类名的子控件（返回首个匹配句柄，0=未找到）"""
        for child in get_child_windows(hwnd):
            cls_buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 255)
            if cls_buf.value == class_name:
                return child
            found = self._lei_find_child_by_class(child, class_name)
            if found:
                return found
        return 0

    def _lei_find_child_by_cid(self, hwnd: int, cid: int) -> int:
        """递归查找指定控件ID的子控件"""
        for child in get_child_windows(hwnd):
            if get_control_id(child) == cid:
                return child
            found = self._lei_find_child_by_cid(child, cid)
            if found:
                return found
        return 0

    def _lei_uia_read_listview(self, lv_hwnd: int, max_n: int = 300) -> list:
        """UIA 读取 SysListView32 行数据（虚拟列表只能用 UIA）。
        返回 [{row, checked, name, cols}]"""
        import uiautomation as ua
        from uiautomation import UIAutomationInitializerInThread
        out = []
        with UIAutomationInitializerInThread():
            ctl = ua.ControlFromHandle(lv_hwnd)
            if ctl is None:
                return []
            try:
                children = ctl.GetChildren()
            except Exception:
                return []
            row = 0
            for item in children:
                if row >= max_n:
                    break
                try:
                    if item.ControlTypeName != "ListItemControl":
                        continue
                    name = item.Name or ""
                    cols = []
                    try:
                        for sc in item.GetChildren():
                            if sc.ControlTypeName in ("TextControl", "EditControl"):
                                cols.append(sc.Name or "")
                    except Exception:
                        pass
                    checked = None
                    try:
                        tp = item.GetTogglePattern()
                        if tp:
                            checked = (tp.ToggleState == 1)
                    except Exception:
                        pass
                    out.append({"row": row, "checked": checked, "name": name, "cols": cols})
                    row += 1
                except Exception:
                    row += 1
        return out

    def _lei_uia_toggle_row(self, lv_hwnd: int, row: int, main_hwnd: int,
                            target_checked: bool = True) -> bool:
        """真实鼠标点击行左侧复选框区域，切换到目标状态。
        已处于目标状态则跳过。返回 True=成功或跳过，False=失败。"""
        import uiautomation as ua
        from uiautomation import UIAutomationInitializerInThread
        with UIAutomationInitializerInThread():
            ctl = ua.ControlFromHandle(lv_hwnd)
            if ctl is None:
                return False
            try:
                children = ctl.GetChildren()
            except Exception:
                return False
            cur = 0
            target = None
            for item in children:
                if item.ControlTypeName != "ListItemControl":
                    continue
                if cur == row:
                    target = item
                    break
                cur += 1
            if target is None:
                return False

            # 读当前状态
            current_checked = None
            try:
                tp = target.GetTogglePattern()
                if tp:
                    current_checked = (tp.ToggleState == 1)
            except Exception:
                pass
            if current_checked == target_checked:
                return True  # 已处于目标状态

            # 滚动到可见
            user32 = ctypes.windll.user32
            user32.SendMessageW(lv_hwnd, self._LVM_ENSUREVISIBLE, row, 0)
            delay(300)
            # 重新读 BoundingRectangle（滚动后坐标会变）
            r = target.BoundingRectangle
            lv_rect = win32gui.GetWindowRect(lv_hwnd)
            cx = max(r.left + 10, lv_rect[0] + 10)
            cy = (r.top + r.bottom) // 2

            # 不在可见区域 → LVM_SCROLL 按像素滚动
            if cy < lv_rect[1] or cy > lv_rect[3]:
                scroll_pixels = cy - (lv_rect[1] + lv_rect[3]) // 2
                user32.SendMessageW(lv_hwnd, self._LVM_SCROLL, 0, scroll_pixels)
                delay(300)
                r = target.BoundingRectangle
                cy = (r.top + r.bottom) // 2

            cx = max(r.left + 10, lv_rect[0] + 10)
            foreground_window(main_hwnd)
            delay(500)
            real_click_screen(cx, cy)
            delay(800)
            return True

    def _lei_uia_click_header_select_all(self, lv_hwnd: int, main_hwnd: int) -> bool:
        """点击 SysListView32 列表头"开"列触发全选。
        定位方式：UIA 读 SysHeader32 的 col[0] BoundingRectangle（Win32 rect 不准）。
        全选逻辑：当前不满→点击→全选；已满→点击→全不选。
        本方法只在不满时调用（确保触发全选）。"""
        import uiautomation as ua
        from uiautomation import UIAutomationInitializerInThread

        header_hwnd = 0
        for child in get_child_windows(lv_hwnd):
            cls_buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 255)
            if cls_buf.value == "SysHeader32":
                header_hwnd = child
                break
        if not header_hwnd:
            return False

        with UIAutomationInitializerInThread():
            hctl = ua.ControlFromHandle(header_hwnd)
            if hctl is None:
                return False
            try:
                cols = hctl.GetChildren()
            except Exception:
                return False
            if not cols:
                return False
            r = cols[0].BoundingRectangle
            hx = (r.left + r.right) // 2
            hy = (r.top + r.bottom) // 2
            foreground_window(main_hwnd)
            delay(500)
            real_click_screen(hx, hy)
            delay(1000)
            return True

    def _lei_xiao_yi_setup_tasks(self, main_hwnd: int, pid: int, i: int,
                                  gw: Optional[GameWindow] = None) -> bool:
        """雷小伊日常任务勾选（在套装切换后、点开始前调用）。

        策略（ctrl.ini [雷小伊勾选] 可配，按账号档解析见 _lei_task_config_for；
        默认值=原硬编码）：
        - Unity 标签：点列表头"开"全选 → 取消命中排除关键字的项
        - Flash 标签：留空=不修改（连标签都不切）；有关键字=所有账号通用，
          按关键字勾选（未匹配的任务不动，宁可漏勾不误勾）
        - 因子标签：所有账号一致，按「因子顺序勾选项」顺序勾前 N 项

        控件映射（雷小伊 1.8.13 实测）：
        cid=340 CPageControl（标签页，TCM_SETCURSEL 切换 idx=0=Unity/1=Flash/2=因子）
        cid=480 SysListView32（Unity/Flash 任务列表，虚拟列表）
        cid=490 SysListView32（精灵因子列表，虚拟列表，250 项）
        cid=320 SysListView32（资源列表，不管）
        """
        seq = self._account_seq(i)

        def cancelled() -> bool:
            return gw is not None and self._lei_login_cancelled(i, gw, "任务勾选")

        if cancelled():
            return False

        # 先确定账号属性（is_new/mi_hao），再按优先级解析该账号的勾选策略
        account = (self._account_manager.accounts[i]
                   if 0 <= i < len(self._account_manager.accounts) else None)
        is_new = account is not None and account.is_new_account()
        mi_hao = account.mi_hao if account is not None else ""
        task_cfg = self._lei_task_config_for(is_new, mi_hao)

        if is_new:
            self._log(f"序号[{seq}]日常_新账号初始化"
                      f"（勾选策略按账号档解析，见 ctrl.ini [雷小伊勾选] 多档）")

        # 找 CPageControl 和 ListView
        tab_hwnd = self._lei_find_child_by_class(main_hwnd, "CPageControl")
        lv_unity = self._lei_find_child_by_cid(main_hwnd, 480)
        lv_factor = self._lei_find_child_by_cid(main_hwnd, 490)
        if not tab_hwnd or not lv_unity:
            self._log(f"序号[{seq}]日常_任务勾选：未找到 CPageControl 或 ListView，跳过")
            return True

        user32 = ctypes.windll.user32

        # ① Unity 标签：全选后取消排除项
        if cancelled():
            return False
        user32.SendMessageW(tab_hwnd, self._TCM_SETCURSEL, 0, 0)
        delay(1500)
        items = self._lei_uia_read_listview(lv_unity)
        total = len(items)
        checked_count = sum(1 for it in items if it.get("checked") is True)
        self._log(f"序号[{seq}]日常_Unity任务列表: {total}项, 已勾选{checked_count}项")

        if total == 0:
            self._log(f"序号[{seq}]日常_Unity任务列表为空，跳过")
        else:
            # 全选（不满时点 Header "开"列）
            if cancelled():
                return False
            if checked_count < total:
                self._log(f"序号[{seq}]日常_点击全选...")
                if self._lei_uia_click_header_select_all(lv_unity, main_hwnd):
                    delay(500)
                    items = self._lei_uia_read_listview(lv_unity)
                    checked_count = sum(1 for it in items if it.get("checked") is True)
                    self._log(f"序号[{seq}]日常_全选后: {checked_count}/{total}")

            # 取消排除项
            for it in items:
                if cancelled():
                    return False
                if "error" in it or not it.get("checked"):
                    continue
                task_name = it["cols"][1] if len(it.get("cols", [])) > 1 else it.get("name", "")
                if any(ex in task_name for ex in task_cfg.unity_exclude):
                    self._log(f"序号[{seq}]日常_取消勾选: {task_name}")
                    self._lei_uia_toggle_row(lv_unity, it["row"], main_hwnd, target_checked=False)
                    delay(500)

        # ①.5 Flash 标签（所有账号通用）：留空=不修改（连标签都不切）；
        #      有关键字=按关键字勾选命中项，未匹配的任务不动
        if task_cfg.flash_checks:
            if cancelled():
                return False
            user32.SendMessageW(tab_hwnd, self._TCM_SETCURSEL, 1, 0)
            delay(1500)
            fl_items = self._lei_uia_read_listview(lv_unity)
            fl_total = len(fl_items)
            fl_checked = sum(1 for it in fl_items if it.get("checked") is True)
            self._log(f"序号[{seq}]日常_Flash任务列表: {fl_total}项, 已勾选{fl_checked}项")
            fl_matched = 0
            for it in fl_items:
                if cancelled():
                    return False
                if "error" in it or it.get("checked"):
                    continue
                task_name = it["cols"][1] if len(it.get("cols", [])) > 1 else it.get("name", "")
                if any(kw in task_name for kw in task_cfg.flash_checks):
                    self._log(f"序号[{seq}]日常_勾选Flash: {task_name}")
                    self._lei_uia_toggle_row(lv_unity, it["row"], main_hwnd, target_checked=True)
                    fl_matched += 1
                    delay(500)
            self._log(f"序号[{seq}]日常_Flash勾选: 勾选{fl_matched}项")

        # ② 因子标签：按「因子顺序勾选项」顺序勾前 N 项（所有账号一致，
        #    新号如需不同勾数在「新号配置」档单独设置）
        if cancelled():
            return False
        if lv_factor:
            factor_count = task_cfg.factor_count
            user32.SendMessageW(tab_hwnd, self._TCM_SETCURSEL, 2, 0)
            delay(1500)
            fitems = self._lei_uia_read_listview(lv_factor, max_n=factor_count + 2)
            for row in range(min(factor_count, len(fitems))):
                if cancelled():
                    return False
                it = fitems[row]
                if "error" in it:
                    continue
                if not it.get("checked"):
                    nm = it["cols"][1] if len(it.get("cols", [])) > 1 else it.get("name", "")
                    self._log(f"序号[{seq}]日常_勾选因子: {nm}")
                    self._lei_uia_toggle_row(lv_factor, row, main_hwnd, target_checked=True)
                    delay(500)

            # 切回 Unity 标签（恢复用户可见状态）
            user32.SendMessageW(tab_hwnd, self._TCM_SETCURSEL, 0, 0)
            delay(500)

        return not cancelled()

    # ============================================================
    # 套装切换（UIA）
    # ============================================================

    def _switch_suit_with_uia(self, suit_window_hwnd: int) -> bool:
        """在「套装切换」窗口中选中默认方案并点击「切换此方案」。

        实测结论（雷小伊 1.8.13）：
        - 方案列表为 SysListView32：UIA SelectionItemPattern.Select() 只会高亮，应用不加载方案；
          必须【真实鼠标点击】方案行，应用才会加载预览/记录当前方案。
        - 「切换此方案」按钮：UIA Invoke / PostMessage BM_CLICK 均不可靠，必须真实鼠标点击。
        - 未选中方案就点切换会清空当前套装穿戴，因此定位不到方案时跳过切换。
        - 默认方案名由 ctrl.ini [窗口配置]「日常默认方案」配置，默认「常规攻」。
        返回 True=完成或跳过，False=异常。
        """
        import ctypes

        coinit_ok = False
        try:
            coinit_ok = ctypes.windll.ole32.CoInitializeEx(None, 2) == 0
        except Exception:
            pass

        try:
            try:
                import uiautomation as ua
            except ImportError:
                self._log("未安装 uiautomation，跳过套装切换")
                return False

            scheme_name = self._config_manager.get("窗口配置", "日常默认方案", "常规攻")
            try:
                # ① UIA 定位「常规攻」方案行的屏幕坐标（物理像素）
                win = ua.ControlFromHandle(suit_window_hwnd)
                schemelist = None
                for l in win.GetChildren():
                    if l.ControlTypeName == "ListControl":
                        items = l.GetChildren()
                        if items and items[0].Name == "标头控件":
                            schemelist = l
                            break
                if schemelist is None:
                    self._log("套装窗口未找到方案列表，跳过切换")
                    return False

                target = None
                for it in schemelist.GetChildren()[1:]:
                    sub_names = [ch.Name for ch in it.GetChildren()]
                    if scheme_name in sub_names:
                        target = it
                        break
                if target is None:
                    self._log(f"未找到方案「{scheme_name}」，跳过套装切换")
                    return False

                # ② 真实鼠标点击方案行（应用只在真实点击时才加载方案）
                r = target.BoundingRectangle
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                foreground_window(suit_window_hwnd)
                real_click_screen(cx, cy)
                delay(1200)

                # ③ 真实鼠标点击「切换此方案」按钮（Win32 定位，物理坐标）
                btn260 = None
                for ch in get_child_windows(suit_window_hwnd):
                    if get_control_id(ch) == self._get_config(23):  # 日常确认切换套装=260
                        btn260 = ch
                        break
                if btn260 is None:
                    self._log("未找到「切换此方案」按钮，跳过切换")
                    return False
                real_click_control(btn260)
                delay(1000)
                self._log(f"已切换套装方案「{scheme_name}」")
                return True
            except Exception as e:
                self._log(f"套装切换异常: {e}")
                return False
        finally:
            if coinit_ok:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    # ============================================================
    # 重聚巅峰登录
    # ============================================================

    def _login_chong_ju_peak(self, i: int, gw: GameWindow) -> None:
        """
        重聚巅峰登录（2.8.1 实测版）。
        对应 EPL：主线程_巅峰登录
        控件ID索引：5=账号 6=密码 7=登录 8=大选择夹 9=小选择夹
                   10=一键刷任务 14=兑换按钮 32=商店列表 35=静态轮播 36=通行证
                   37=匹配 38=不要胜利 39=做狂野任务勾选框 40=狂野6v6 51=输出框
                   54=仅用第五勾选框 55=不领段位 61/62=商店晶币 63=商店加载

        实测要点（2.8.1）：
        - 密码框拒绝 WM_SETTEXT → 真实键盘输入（input_real_keyboard 自带置前，不移位）
        - 登录按钮【只真实点击一次】（PostMessage+点击重复触发会误点「退出」）
        - 大选择夹页：0=登录页(含仅用第五勾选框1560) 1=巅峰页 2=资讯页(静态轮播1920)
        - 小选择夹页：0=巅峰任务 1=6V6(狂野按钮470) 4=巅峰商店(晶币2140/2130) 5=通行证(2060)
        - 刻印兑换：UIA 定位行 + 真实点击行左侧(行中间不选中) + 真实点击兑换按钮
        """
        pid = gw.process_id
        account = self._account_manager.accounts[i]
        self._log(f"序号[{self._account_seq(i)}]巅峰_登录中")

        # ① 查找 WTWindow
        main_hwnd = WindowFinder.find_window_by_pid(
            pid, "", "WTWindow", match_full_title=True, timeout=self.WINDOW_WAIT_TIMEOUT
        )
        if main_hwnd is None:
            self._update_flag = 0
            gw.timed_out = True
            return

        # ② 更新检测（2.8.x 实测：主窗口先创建——隐藏、标题正常、登录控件齐全，
        #    更新弹窗「检测到新版本 Vx.y.z」约 2 秒后才弹出。一次性标题检查会在
        #    弹窗出现前误判「无更新」放行后续账号 → 多个更新窗口 + 全部卡初始化）
        title = WindowFinder.get_window_title(main_hwnd)
        if title != _PEAK_MAIN_TITLE:
            # 首个 WTWindow 本身就是更新窗口（旧版客户端：更新窗口先于主窗口出现）
            popup = main_hwnd
        else:
            # 检测门控：轮询至 弹窗出现（→更新分支）或 主窗口变可见（→检测通过）。
            # 实测：无更新时主窗口启动后 ~6 秒变可见；有更新时弹窗 ~2 秒出现且
            # 主窗口一直隐藏。15 秒超时后默认放行（初始化等待仍会兜底）
            popup = 0
            gate_deadline = time.time() + 15.0
            while time.time() < gate_deadline:
                if self._chong_ju_login_cancelled(i, gw, "更新检测", main_hwnd):
                    self._update_flag = 0
                    return
                popup = self._find_update_popup(pid, main_hwnd)
                if popup:
                    break
                if win32gui.IsWindowVisible(main_hwnd):
                    break  # 主窗口已显示 → 检测通过（无更新）
                time.sleep(0.5)

        if popup:
            # [重聚] 巅峰启动更新 勾选（默认真）→ 自动更新；未勾选 → 点弹窗
            # 「取消」用当前版本继续登录（对齐 XryM 模式1）
            if getattr(self._config_manager, "get_peak_auto_update", lambda: True)():
                self._log("巅峰正在更新")
                account.current_action = "巅峰更新"
                ok = self._handle_update(popup, ClientType.CHONG_JU_PEAK, pid,
                                         i=i, gw=gw)
                self._update_flag = 1 if ok else 0
                return
            self._log("巅峰检测到新版本，自动更新未开启（用当前版本继续登录）")
            self._dismiss_update_popup(popup)

        self._update_flag = 0

        # ③ 进入临界区（批量串行，避免多窗口抢焦点/真实输入错乱）
        with self._key_lock:
            # 批量关闭后仍在排队等锁的线程：拿到锁先查取消，不再对已死窗口继续操作
            if self._chong_ju_login_cancelled(i, gw, "临界区等待", main_hwnd):
                self._update_flag = 0
                return
            gw.window_handle = main_hwnd
            gw.control_snapshot = {}

            # ④ 按控件ID匹配（客户端控件分批创建，缺失时循环重试直至齐全）
            handles: dict[int, int] = {}
            match_deadline = time.time() + 30.0
            while time.time() < match_deadline:
                handles = self._match_peak_controls(main_hwnd, gw)
                # 关键控件齐全（账号/密码/登录/大选择夹/小选择夹）即可继续
                if all(k in handles for k in (1, 2, 3, 4, 5)):
                    break
                if self._chong_ju_login_cancelled(i, gw, "控件匹配", main_hwnd):
                    self._update_flag = 0
                    return
                time.sleep(1)

            # ⑤ 平铺窗口
            self._tile_chong_ju(i, main_hwnd, is_peak=True)

            # ⑥ 等待客户端初始化完成（输出框出现「初始化资源完成」）
            #    实测 2.8.1 启动后约 5 秒才完成初始化，过早输入账号密码会丢失
            init_waited = 0.0
            while init_waited < 30.0:
                if gw.output_handle and "初始化资源完成" in external_edit_get_content(gw.output_handle):
                    break
                if self._chong_ju_login_cancelled(i, gw, "初始化等待", main_hwnd):
                    self._update_flag = 0
                    return
                time.sleep(1)
                init_waited += 1

            # ⑦ 客户端记住账号：初始化后会自动尝试登录（输出框出现昵称），
            #    此期间点击登录按钮无效，需等自动登录尝试结束（约 4 秒，等 8 秒）；
            #    分片等待：点「全部关闭」时提前退出，不再对死窗口空转跑完 8 秒
            auto_login_waited = 0.0
            while auto_login_waited < 8.0:
                if self._chong_ju_login_cancelled(i, gw, "自动登录等待", main_hwnd):
                    self._update_flag = 0
                    return
                time.sleep(0.5)
                auto_login_waited += 0.5

            # ⑧ 切登录页 → 输入账号密码 → 登录
            big_tab = handles.get(4)
            small_tab = handles.get(5)
            account_hwnd = handles.get(1)
            password_hwnd = handles.get(2)
            login_hwnd = handles.get(3)

            if not all([big_tab, small_tab, account_hwnd, password_hwnd, login_hwnd]):
                missing = [k for k, v in [(4, big_tab), (5, small_tab), (1, account_hwnd),
                         (2, password_hwnd), (3, login_hwnd)] if v is None]
                self._log(f"序号[{self._account_seq(i)}]巅峰_控件缺失: {missing}")
                self._update_flag = 0
                gw.timed_out = True
                return

            switch_tab(big_tab, 0)                # 切到登录页
            # 若客户端已自动登录成功（标题含用户名），跳过手动登录
            if "[" in WindowFinder.get_window_title(main_hwnd):
                self._log(f"序号[{self._account_seq(i)}]客户端已自动登录，跳过手动登录")
            else:
                send_text(account_hwnd, account.mi_hao)
                delay(500)
                # 密码框拒绝 WM_SETTEXT，真实键盘输入（函数内部置前台；不移位保持平铺）
                input_real_keyboard(main_hwnd, password_hwnd, account.password)
                delay(500)
                # 真实点击登录前确保窗口在前台（真实点击依赖；多窗口批量时必需）
                foreground_window(main_hwnd)
                delay(300)
                # 登录按钮只真实点击一次（重复触发会误点「退出」）
                real_click_control(login_hwnd)
                delay(1500)

            # ⑦ 等待标题变化（登录成功）
            n = 1
            new_title = WindowFinder.get_window_title(main_hwnd)
            while new_title == _PEAK_MAIN_TITLE:
                if self._chong_ju_login_cancelled(i, gw, "登录等待", main_hwnd):
                    self._update_flag = 0
                    return
                delay(3000)
                n += 1
                new_title = WindowFinder.get_window_title(main_hwnd)
                if n >= self.LOGIN_TIMEOUT_CHECKS:
                    self._log(f"序号[{self._account_seq(i)}]巅峰_登录超时！！")
                    self._notifier.notify_login_timeout(i, "巅峰")
                    self._update_flag = 0
                    gw.timed_out = True
                    return

            account.display_name = self._extract_username(new_title, 2)
            if self._on_state_changed:
                self._on_state_changed()  # 登录成功 → 刷新运行页账号名列
            delay(3000)

            # ⑧ 登录后客户端重建界面控件（登录前只有登录页控件），统一重新匹配
            handles = self._match_peak_controls(main_hwnd, gw)
            big_tab = handles.get(4)
            small_tab = handles.get(5)
            if not small_tab:
                self._log(f"序号[{self._account_seq(i)}]巅峰_登录后控件匹配失败")
                self._update_flag = 0
                gw.timed_out = True
                return
            switch_tab(big_tab, 1)                # 切到巅峰页
            self._log(f"序号[{self._account_seq(i)}]巅峰_已登录！")

            chong_ju_cfg = self._config_manager.get_client_config("重聚")
            # 运行选项按账号档解析（ctrl.ini [巅峰勾选] 三档：特定账号 > 新号 > 全局；
            # 每次登录现读，配置页保存后下一轮启动即生效，无需重启中控台）
            run_cfg = self._peak_run_config_for(
                account.is_new_account(), account.mi_hao)

            def set_check(key_id: int, target: bool, name: str) -> None:
                hw = handles.get(key_id)
                if hw:
                    check_box_set(hw, target)
                    delay(300)
                else:
                    self._log(f"序号[{self._account_seq(i)}]未找到勾选框 {name}")

            # ⑨ 勾选运行选项（BM_SETCHECK 直达控件，无需切页；按账号档显式设目标值）
            set_check(13, True, "刷完自动匹配")           # 必勾
            set_check(14, run_cfg.no_victory_quest, "不要胜利任务")
            set_check(17, run_cfg.no_rank_reward, "不领取段位奖励")
            set_check(18, run_cfg.do_wild_quest, "做狂野任务")
            set_check(12, True, "自动领取通行证奖励")      # 必勾
            set_check(16, run_cfg.only_fifth, "仅用第五")
            set_check(11, True, "静态轮播")              # 必勾

            # ⑩ 晶币检查与刻印兑换（商店页）
            switch_tab(small_tab, 4)
            delay(1500)
            # 商店页状态下重新枚举（刚登录后控件可能仍在重建，旧句柄读取为空）
            handles = self._match_peak_controls(main_hwnd, gw)
            blue_hwnd = handles.get(19)           # 2140
            purple_hwnd = handles.get(20)         # 2130
            if blue_hwnd:
                blue = self._extract_crystal(blue_hwnd)
                purple = self._extract_crystal(purple_hwnd) if purple_hwnd else 0
                self._log(f"序号[{self._account_seq(i)}]蓝晶币：{blue} 紫晶币：{purple}")

                if blue >= 2800:
                    self._exchange_peak_seals(handles, i)
                    account.blue_crystal = blue - 1800
                    self._log(f"序号[{self._account_seq(i)}]蓝晶币已消耗")
                else:
                    account.blue_crystal = blue

                account.purple_crystal = purple
                if purple >= 2800:
                    self._log(f"序号[{self._account_seq(i)}]紫晶币接近上限({purple}/3000)")
                if self._on_state_changed:
                    self._on_state_changed()  # 蓝紫晶币更新 → 刷新运行页对应列
            else:
                self._log(f"序号[{self._account_seq(i)}]未找到商店晶币控件")

            # ⑪ 开始任务（只点一次）
            self._peak_start_task(handles, small_tab, i)
            self._log(f"序号[{self._account_seq(i)}]巅峰_任务已开始")

        # 退出临界区

    def _peak_start_task(self, handles: dict[int, int],
                         small_tab: int, i: int) -> None:
        """⑪ 巅峰开始任务：切到巅峰任务页，点「一键刷任务」。

        做狂野任务/不要胜利任务/仅用第五/不领取段位奖励等选项，已由登录后的
        set_check 按账号档写入客户端勾选框，客户端自行据此安排任务——
        不再额外点「狂野6V6」按钮（旧逻辑遗留：早期没有做狂野任务勾选框，
        只能切 6V6 页手动点狂野开始；现已改成和其他选项一样只点一键刷任务）。
        """
        switch_tab(small_tab, 0)
        one_click = handles.get(6)            # 10 一键刷任务
        if one_click:
            real_click_control(one_click)
        else:
            self._log(f"序号[{self._account_seq(i)}]巅峰_开始按钮缺失")

    # ============================================================
    # 重聚日常登录
    # ============================================================

    def _login_chong_ju_daily(self, i: int, gw: GameWindow) -> None:
        """
        重聚日常登录。
        对应 EPL：主线程_重聚日常登录
        控件ID索引：56=账号 57=密码 58=选区 59=登录 60=输出框

        实测要点（2.2.1）：
        - 密码框拒绝 WM_SETTEXT → 真实键盘输入（input_real_keyboard 自带置前，不移位）
        - 平铺用巅峰式贴底排列（和重聚巅峰一样的排列）
        - 等"初始化资源完成"后再输入（和巅峰一样）
        - 更新检测与巅峰 2.8.x 完全一致：主窗口先创建（隐藏、标题正常「重聚脱机」），
          更新弹窗「检测到新版本 Vx.y.z」数秒后弹出（ID=220 立即更新，同巅峰）
        """
        pid = gw.process_id
        account = self._account_manager.accounts[i]
        self._log(f"序号[{self._account_seq(i)}]重聚日常_登录中")

        # ① 查找 WTWindow
        main_hwnd = WindowFinder.find_window_by_pid(
            pid, "", "WTWindow", match_full_title=True, timeout=self.WINDOW_WAIT_TIMEOUT
        )
        if main_hwnd is None:
            self._update_flag = 0
            gw.timed_out = True
            return

        # ② 更新检测（2.2.x 实测与巅峰 2.8.x 完全一致：主窗口先创建——隐藏、
        #    标题正常「重聚脱机」、控件齐全，更新弹窗「检测到新版本 V2.2.2」
        #    数秒后弹出且主窗口一直隐藏。一次性标题检查会在弹窗出现前误判
        #    「无更新」放行后续账号 → 多个更新窗口 + 全部卡初始化等待）
        title = WindowFinder.get_window_title(main_hwnd)
        if title != _CJ_DAILY_MAIN_TITLE:
            # 首个 WTWindow 本身就是更新窗口（旧版客户端：更新窗口先于主窗口出现）
            popup = main_hwnd
        else:
            # 检测门控：轮询至 弹窗出现（→更新分支）或 主窗口变可见（→检测通过）
            popup = 0
            gate_deadline = time.time() + 15.0
            while time.time() < gate_deadline:
                if self._chong_ju_login_cancelled(i, gw, "更新检测", main_hwnd):
                    self._update_flag = 0
                    return
                popup = self._find_update_popup(pid, main_hwnd, _CJ_DAILY_MAIN_TITLE)
                if popup:
                    break
                if win32gui.IsWindowVisible(main_hwnd):
                    break  # 主窗口已显示 → 检测通过（无更新）
                time.sleep(0.5)

        if popup:
            # [重聚] 重聚日常启动更新 勾选（默认真）→ 自动更新；未勾选 → 点弹窗
            # 「取消」用当前版本继续登录（对齐 XryM 模式1）
            if getattr(self._config_manager,
                       "get_chongju_daily_auto_update", lambda: True)():
                self._log("重聚日常正在更新")
                account.current_action = "重聚日常更新"
                # 日常暂不接网页兜底（本轮仅巅峰）；返回值恒 True，语义同旧版
                self._handle_update(popup, ClientType.CHONG_JU_DAILY, pid,
                                    i=i, gw=gw)
                self._update_flag = 1
                return
            self._log("重聚日常检测到新版本，自动更新未开启（用当前版本继续登录）")
            self._dismiss_update_popup(popup)

        self._update_flag = 0

        # ③ 进入临界区
        with self._key_lock:
            # 批量关闭后仍在排队等锁的线程：拿到锁先查取消，不再对已死窗口继续操作
            if self._chong_ju_login_cancelled(i, gw, "临界区等待", main_hwnd):
                self._update_flag = 0
                return
            gw.window_handle = main_hwnd
            gw.control_snapshot = {}

            # ④ 按控件ID匹配（控件分批创建，重试等待关键控件就绪）
            handles: dict[int, int] = {}
            match_deadline = time.time() + 30.0
            while time.time() < match_deadline:
                handles = {}
                gw.output_handle = 0
                for child_hwnd in self._wait_for_children(main_hwnd):
                    cid = get_control_id(child_hwnd)
                    if not cid:
                        continue  # 护栏：配置缺键补 0 时严禁误绑 id=0 子控件（同 _match_peak_controls）
                    if cid == self._get_config(56):       # 账号
                        handles[1] = child_hwnd
                    elif cid == self._get_config(57):     # 密码
                        handles[2] = child_hwnd
                    elif cid == self._get_config(58):     # 选区
                        handles[3] = child_hwnd
                    elif cid == self._get_config(59):     # 登录
                        handles[4] = child_hwnd
                    elif cid == self._get_config(60):     # 输出框
                        gw.output_handle = child_hwnd
                # 关键控件齐全（账号/密码/选区/登录）即可继续
                if all(k in handles for k in (1, 2, 3, 4)):
                    break
                if self._chong_ju_login_cancelled(i, gw, "控件匹配", main_hwnd):
                    self._update_flag = 0
                    return
                time.sleep(1)

            # ⑤ 平铺窗口（巅峰式贴底排列）
            self._tile_chong_ju(i, main_hwnd, is_peak=False)

            # ⑥ 等待客户端初始化完成（和巅峰一样）
            init_waited = 0.0
            while init_waited < 30.0:
                if gw.output_handle and "初始化资源完成" in external_edit_get_content(gw.output_handle):
                    break
                if self._chong_ju_login_cancelled(i, gw, "初始化等待", main_hwnd):
                    self._update_flag = 0
                    return
                time.sleep(1)
                init_waited += 1

            # ⑦ 输入账号密码 → 选区 → 登录
            account_hwnd = handles.get(1)
            password_hwnd = handles.get(2)
            zone_hwnd = handles.get(3)
            login_hwnd = handles.get(4)

            if not all([account_hwnd, password_hwnd, zone_hwnd, login_hwnd]):
                missing = [k for k, v in [(1, account_hwnd), (2, password_hwnd),
                         (3, zone_hwnd), (4, login_hwnd)] if v is None]
                self._log(f"序号[{self._account_seq(i)}]重聚日常_控件缺失: {missing}")
                self._update_flag = 0
                gw.timed_out = True
                return

            # 账号：WM_SETTEXT（实测可用）
            set_focus(account_hwnd)
            clear_edit(account_hwnd)
            delay(500)
            send_text(account_hwnd, account.mi_hao)
            delay(500)

            # 密码：真实键盘输入（密码框拒绝 WM_SETTEXT，和巅峰一样）
            input_real_keyboard(main_hwnd, password_hwnd, account.password)
            delay(500)

            # 选区 + 登录
            post_click(zone_hwnd)
            delay(1000)
            post_click(login_hwnd)
            delay(1500)

            # ⑧ 等待标题变化（登录成功）
            n = 1
            new_title = WindowFinder.get_window_title(main_hwnd)
            while new_title == "重聚脱机" or new_title == "":
                if self._chong_ju_login_cancelled(i, gw, "登录等待", main_hwnd):
                    self._update_flag = 0
                    return
                delay(3000)
                n += 1
                new_title = WindowFinder.get_window_title(main_hwnd)
                if n >= self.LOGIN_TIMEOUT_CHECKS:
                    self._log(f"序号[{self._account_seq(i)}]重聚日常_登录超时！！")
                    self._notifier.notify_login_timeout(i, "重聚日常")
                    self._update_flag = 0
                    gw.timed_out = True
                    return

            account.display_name = self._extract_username(new_title, 1)
            if self._on_state_changed:
                self._on_state_changed()  # 登录成功 → 刷新运行页账号名列
            delay(2000)

            # 登录成功后重新平铺：窗口此时已显示、高度稳定（实测984），
            # 用最终高度计算 1号底部贴任务栏、向右逐列升高10px 的左低右高阶梯
            self._tile_chong_ju(i, main_hwnd, is_peak=False)

            set_focus(main_hwnd)
            self._log(f"序号[{self._account_seq(i)}]重聚日常_已登录！")

        # 退出临界区

    # ============================================================
    # 自检重启后的统一窗口重排
    # ============================================================

    def _retile_lei_selfcheck(self, active: list[int]) -> None:
        """雷小伊自检批量重启完成后，按运行页顺序重新排列全部窗口。"""
        if self._daily_sc_stop.is_set():
            return
        # 等待超时不等于登录线程退出；未收尾时重排仍会被后续点击覆盖。
        pending = [self._account_seq(i) for i in active if self._daily_windows[i] is not None
                   and not self._daily_windows[i].login_finished.is_set()]
        if pending:
            self._log(f"日常自检_登录流程尚未结束，暂缓重排：序号{pending}")
            return
        for i in active:
            gw = self._daily_windows[i]
            if gw is not None and gw.window_handle:
                self._tile_lei_xiao_yi(i, gw.window_handle)
        # 重排后再依次置前一次（等同点一次「排列」按钮），保证最上层窗口是最后一个
        if self.activate_tile(ClientType.LEI_XIAO_YI):
            self._log("日常自检_全部重启完成，已重新排列窗口")

    def _retile_xrym_selfcheck(self, active: list[int]) -> None:
        """XryM 自检批量重启完成后，重新枚举并排列全部窗口。"""
        for i in active:
            gw = self._xrym_windows[i]
            if gw is None:
                continue
            mh = self._xrym_current_window(gw.process_id)
            if mh is not None:
                self._tile_xrym(i, mh)
        self._log("XryM自检_全部重启完成，已重新排列窗口")
        # 重排后再依次置前一次（等同点一次「排列」按钮；XryM 走 activate_tile_xrym，
        # 每个账号重新枚举最新顶层窗口句柄，避免用进入脱机后已重建的旧句柄）
        self.activate_tile_xrym()

    def _retile_peak_selfcheck(self, active: list[int]) -> None:
        """重聚巅峰自检批量重启完成后，按运行页顺序重新排列窗口。"""
        if self._peak_sc_stop.is_set():
            return
        pending = [self._account_seq(i) for i in active if self._peak_windows[i] is not None
                   and not self._peak_windows[i].login_finished.is_set()]
        if pending:
            self._log(f"巅峰自检_登录流程尚未结束，暂缓重排：序号{pending}")
            return
        for i in active:
            gw = self._peak_windows[i]
            if gw is not None and gw.window_handle:
                self._tile_chong_ju(i, gw.window_handle, is_peak=True)
        # 重排后再依次置前一次（等同点一次「排列」按钮），保证最上层窗口是最后一个
        if self.activate_tile(ClientType.CHONG_JU_PEAK):
            self._log("巅峰自检_全部重启完成，已重新排列窗口")

    def _retile_chongju_daily_selfcheck(self, active: list[int]) -> None:
        """重聚日常自检批量重启完成后，按运行页顺序重新排列窗口。"""
        if self._daily_sc_stop.is_set():
            return
        pending = [self._account_seq(i) for i in active if self._daily_windows[i] is not None
                   and not self._daily_windows[i].login_finished.is_set()]
        if pending:
            self._log(f"重聚日常自检_登录流程尚未结束，暂缓重排：序号{pending}")
            return
        for i in active:
            gw = self._daily_windows[i]
            if gw is not None and gw.window_handle:
                self._tile_chong_ju(i, gw.window_handle, is_peak=False)
        # 重排后再依次置前一次（等同点一次「排列」按钮），保证最上层窗口是最后一个
        if self.activate_tile(ClientType.CHONG_JU_DAILY):
            self._log("重聚日常自检_全部重启完成，已重新排列窗口")

    # ============================================================
    # 平铺算法
    # ============================================================

    def _tile_lei_xiao_yi(self, i: int, main_hwnd: int) -> None:
        """
        雷小伊日常窗口平铺。
        配置索引：
          1=屏幕宽  2=屏幕高  15=窗口宽  16=窗口高
          17=下间距  18=右间距  27=列数
        排列：x=屏幕高-窗口宽-(col-1)*右间距, y=屏幕宽-窗口高-(row-1)*下间距
        """
        queue = self._account_manager.get_daily_queue()
        n = self._count_queue_active_before(queue, i)
        if n == 0:
            return

        cols = max(1, self._get_config(27, 1))
        col = ((n - 1) % cols) + 1
        row = math.ceil(n / cols)

        screen_w = self._get_config(1)
        screen_h = self._get_config(2)
        window_w = self._get_config(15)
        window_h = self._get_config(16)
        right_spacing = self._get_config(18)
        bottom_spacing = self._get_config(17)

        x = screen_h - window_w - (col - 1) * right_spacing
        y = screen_w - window_h - (row - 1) * bottom_spacing

        set_window_pos(main_hwnd, max(0, x), max(0, y))
        self._log(f"平铺序号[{self._account_seq(i)}] col={col} row={row} x={x} y={y}")

    def _tile_chong_ju(self, i: int, main_hwnd: int, is_peak: bool) -> None:
        """
        重聚窗口平铺（巅峰和日常共用参数）。
        巅峰配置：1=屏幕宽 4=窗口高 29=列数 33=下间距 34=右间距
          排列：x=(col-1)*右间距, y=屏幕宽-窗口高-(row-1)*下间距
        日常配置：全部窗口同置（以1号当前高度为准，不做上移/下移）
          排列：x=(col-1)*右间距, y=任务栏顶部-窗口高度（底部齐平贴任务栏，所有窗口同一高度）
        """
        if is_peak:
            queue = self._account_manager.get_peak_queue()
        else:
            queue = self._account_manager.get_daily_queue()

        n = self._count_queue_active_before(queue, i)
        if n == 0:
            return

        cols = max(1, self._get_config(29, 1))
        col = ((n - 1) % cols) + 1
        row = math.ceil(n / cols)

        right_spacing = self._get_config(34)
        bottom_spacing = self._get_config(33)

        # 巅峰和日常都用贴底排列
        screen_w = self._get_config(1)
        window_h = self._get_config(4)
        x = (col - 1) * right_spacing
        y = screen_w - window_h - (row - 1) * bottom_spacing

        # 重聚日常单独配置：全部窗口以1号当前高度/位置为准，不做上移或下移
        # 所有窗口底部齐平，紧贴任务栏顶部；用1号当前高度（实测稳定值）作为统一高度
        # 用 SPI_GETWORKAREA 获取任务栏顶部（自动排除任务栏），窗口高度用实测值
        if not is_peak:
            import ctypes
            from ctypes import wintypes as _wt
            work = _wt.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
            taskbar_top = work.bottom
            # 窗口实际高度：等窗口可见且高度稳定后再取（初始隐藏929→显示后984）
            import win32gui as _wg
            win_h = 0
            for _ in range(40):  # 最多等 20 秒
                try:
                    wr = _wg.GetWindowRect(main_hwnd)
                    win_h = wr[3] - wr[1]
                    visible = bool(ctypes.windll.user32.IsWindowVisible(main_hwnd))
                except Exception:
                    # 窗口已被关闭（如用户中途点「关闭」杀进程）→ 停止等待，用兜底高度
                    break
                if visible and win_h >= 980:
                    break
                time.sleep(0.5)
            if win_h < 900:
                win_h = 984  # 兜底：实测稳定值
            # 全部窗口同置：以1号当前高度为基准，底部齐平贴住任务栏顶部，不做阶梯上移
            y = taskbar_top - win_h

        set_window_pos(main_hwnd, max(0, x), max(0, y))
        self._log(f"平铺序号[{self._account_seq(i)}] col={col} row={row} x={x} y={y}")

    # ============================================================
    # 更新处理
    # ============================================================

    def _read_update_popup_text(self, hwnd: int) -> str:
        """读取更新弹窗内全部文本（点「立即更新」前预读）。
        实测：下载链接藏在 id=270 Edit 里（GetWindowText 读不到 Edit 内容，
        必须 external_edit_get_content 跨进程 WM_GETTEXT），自带更新失败后
        弹窗销毁句柄失效就再也读不到，所以点击前先缓存。"""
        parts: list[str] = []
        for child_hwnd in get_child_windows(hwnd):
            try:
                text = external_edit_get_content(child_hwnd)
            except Exception:
                text = ""
            if not text:
                text = get_control_text(child_hwnd)
            text = (text or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _peak_web_update_fallback(self, update_text: str, cur_path: str,
                                  i: int = -1, gw=None) -> bool:
        """巅峰自带更新失败 → 网页兜底（2026-09 用户需求）。
        更新弹窗文本含「手动下载：<蓝奏云链接>」（实测 2.8.5 弹窗）→ 复用
        雷小伊同款蓝奏云七步链路（chong_ju_updater→lei_updater）下载新版
        → 落盘 nanliu 目录 → 写回配置 → launch_all 同账号重试切新路径。
        成功回 True；失败/取消回 False（调用方放行，日志含手动下载指引）。"""
        from .chong_ju_updater import (CjUpdateError, LeiUpdateCancelled,
                                       LeiUpdateError, download_peak_from_link,
                                       extract_update_link)
        link = extract_update_link(update_text)
        if not link:
            self._log("巅峰更新_弹窗内未找到蓝奏云下载链接，无法网页兜底"
                      "（可查看更新弹窗「手动下载」地址手动更新）")
            return False
        if not cur_path:
            cur_path = self._config_manager.get_current_peak_path()
        dest_dir = os.path.dirname(cur_path) if cur_path else ""
        if not dest_dir:
            self._log("巅峰更新_网页兜底放弃：未配置巅峰客户端路径")
            return False
        self._log(f"巅峰更新_尝试网页兜底下载: {link}")
        try:
            download_peak_from_link(
                dest_dir, link, cur_path,
                log=lambda m: self._log(f"巅峰网页下载 | {m}"),
                cancel_check=lambda: i >= 0 and self._chong_ju_login_cancelled(
                    i, gw, "网页兜底下载"),
            )
        except LeiUpdateCancelled:
            self._log("巅峰更新_网页兜底已取消（全部关闭）")
            return False
        except (CjUpdateError, LeiUpdateError) as e:
            self._log(f"巅峰更新_网页兜底失败: {e}（可手动下载: {link}）")
            return False
        # 下载成功 → 重扫目录写回配置（此时目录已有新版本，persist 会切换路径）
        self._load_config()
        self._persist_peak_path_after_update()
        self._log("巅峰更新完成（网页下载）")
        return True

    def _handle_update(self, hwnd: int, client_type: ClientType, pid: int = 0,
                       i: int = -1, gw=None) -> bool:
        """
        处理客户端更新弹窗（2026-09 实测 2.8.x 全流程）。
        对应 EPL：巅峰_程序更新
        1. （仅巅峰）点更新前预读弹窗更新说明文本：自带更新失败后弹窗即销毁，
           其中的「手动下载」蓝奏云链接供网页兜底用（见 _peak_web_update_fallback）
        2. 在弹窗内点「立即更新」（控件ID=配置索引31，实测 220）
        3. 等弹窗销毁（实测点击后 ~4 秒：新版本 exe 落盘、旧进程自动退出、
           更新器自启新版本进程，随后弹窗销毁；上限 300 秒防死等。
           2026-09 实测自带更新失败时弹窗也会很快销毁——成败以「目录是否
           落盘新版本」判定，不以销毁快慢判定）
        4. 清理孤儿进程：更新器自启的新版本进程不归中控台管理（自由进程，
           无人平铺/登录/停止），杀掉后由 launch_all 同账号重试受管拉起
        5. 重扫目录最新版本并写回配置（EPL：目录_自动更新(11)）
        6. （仅巅峰）落盘无新版本=自带更新失败 → 网页兜底下载

        返回 True=更新完成（调用方 _update_flag=1 同账号重试新版本）；
        False=更新失败（调用方 _update_flag=0 放行/降级继续）。
        """
        type_name = "巅峰" if client_type == ClientType.CHONG_JU_PEAK else "重聚日常"
        # ① 预读弹窗更新说明（仅巅峰：失败链接在 Edit 里，销毁后读不到）
        update_text = ""
        if client_type == ClientType.CHONG_JU_PEAK:
            update_text = self._read_update_popup_text(hwnd)
        # ② 点「立即更新」（配置索引 31 = 巅峰更新按钮ID）
        children = get_child_windows(hwnd)
        update_btn_id = self._get_config(31)
        clicked = False
        for child_hwnd in children:
            if get_control_id(child_hwnd) == update_btn_id:
                post_click(child_hwnd)
                clicked = True
                break
        if not clicked:
            self._log(f"更新弹窗内未找到更新按钮（ID={update_btn_id}），放弃自动更新")

        # ③ 等弹窗销毁（实测点击后 ~4-7 秒销毁；弹窗与旧进程同生命周期，
        #    「全部关闭」杀进程也会使弹窗销毁，循环自然退出）
        update_wait_start = time.time()
        while WindowFinder.is_window_valid(hwnd) and time.time() - update_wait_start < 300:
            time.sleep(0.5)
        if WindowFinder.is_window_valid(hwnd):
            self._log("更新弹窗 300 秒未关闭，放弃等待（可能下载失败）")

        # ④ 清理孤儿进程（更新器自启的新版本进程在弹窗销毁前已拉起；
        #    重聚日常与巅峰同在 nanliu 目录，按客户端类型选基准路径）
        time.sleep(1.0)
        if client_type == ClientType.CHONG_JU_DAILY:
            ref = (self._cj_daily_updated_path
                   or self._config_manager.get_chongju_daily_path())
            self._kill_orphan_peak_processes(ref)
        else:
            self._kill_orphan_peak_processes()

        # ⑤ 重扫目录最新版本并写回配置（EPL：目录_自动更新；EPL 原版写死巅峰
        #    目录 11 是缺陷，Python 版按客户端类型写各自的键）
        self._load_config()
        if client_type == ClientType.CHONG_JU_DAILY:
            self._persist_chongju_daily_path_after_update()
            self._log(f"{type_name}更新完成")
            return True

        # ⑥ 巅峰：判定自带更新是否成功（目录是否落盘比当前更新的版本）
        cur_path = self._config_manager.get_current_peak_path()
        landed = False
        if cur_path:
            cand = get_latest_version_path(cur_path)
            landed = os.path.abspath(cand) != os.path.abspath(cur_path)
        if landed:
            self._persist_peak_path_after_update()
            self._log(f"{type_name}更新完成")
            return True
        # 自带更新失败（未落盘新版本）→ 网页兜底
        self._log(f"{type_name}自带更新失败（未检测到新版本落盘）")
        return self._peak_web_update_fallback(update_text, cur_path, i, gw)

    def _kill_orphan_peak_processes(self, ref_path: str = "") -> None:
        """杀掉重聚客户端目录下不受中控台管理的孤儿进程。
        更新器点「立即更新」后自启的新版本进程（2026-09 实测）不在
        _peak_windows/_daily_windows/_xrym_windows 里，无人管理；杀掉后由
        launch_all 更新分支同账号重试用新路径正常拉起（受管窗口）。
        ref_path：目录基准路径（重聚日常更新时传日常路径；缺省用巅峰路径，
        两者同在 nanliu 目录时等价）。"""
        ref = (ref_path or self._peak_updated_path
               or self._config_manager.get_current_peak_path())
        if not ref:
            return
        try:
            target_dir = os.path.normcase(os.path.dirname(os.path.abspath(ref)))
        except Exception:
            return
        keep: set[int] = set()
        for arr in (self._peak_windows, self._daily_windows, self._xrym_windows):
            for gw in arr:
                if gw is not None:
                    keep.add(gw.process_id)
        killed: list[int] = []
        for proc in psutil.process_iter(["pid", "exe"]):
            exe = proc.info["exe"]
            if not exe:
                continue
            try:
                if os.path.normcase(os.path.dirname(exe)) != target_dir:
                    continue
                if proc.info["pid"] in keep:
                    continue
                for v in proc.children(recursive=True) + [proc]:
                    v.kill()
                killed.append(proc.info["pid"])
            except psutil.Error:
                continue
        if killed:
            self._log(f"已清理更新自启的孤儿进程: {killed}")

    def _persist_peak_path_after_update(self) -> None:
        """重扫巅峰客户端目录最新版本并写回 ctrl.ini（[客户端列表] 现行巅峰条目
        + [重聚] 重聚目录11），对应 EPL：目录_自动更新(11)。
        同目录同基础名（去版本号）的 exe 中版本号最大者胜出；无更新则只记日志。"""
        cfg = self._config_manager
        old_path = cfg.get_current_peak_path()
        if not old_path:
            return
        new_path = get_latest_version_path(old_path)
        if os.path.abspath(new_path) == os.path.abspath(old_path):
            self._log(f"巅峰目录暂无更新（{os.path.basename(old_path)} 已是最新版本）")
            return
        entries = cfg.get_client_entries()
        changed = False
        for e in entries:
            if e.is_peak:
                e.path = new_path
                changed = True
        if changed:
            cfg.set_client_entries(entries)  # 内部会同步写回 [重聚] 重聚目录11
        else:
            cfg.set_client_path("重聚", "重聚目录11", new_path)  # 无现行巅峰条目时兜底
        try:
            cfg.save()
        except Exception as e:
            self._log(f"巅峰更新_写回配置失败: {e}")
        # 整批后续账号/自检重启统一用新路径（_launch_one 巅峰分支读取）
        self._peak_updated_path = new_path
        self._log(f"巅峰版本已切换: {os.path.basename(old_path)} → {os.path.basename(new_path)}")

    def _persist_chongju_daily_path_after_update(self) -> None:
        """重扫重聚日常客户端目录最新版本并写回 ctrl.ini（[客户端列表] 重聚日常
        条目 + [重聚] 重聚目录8），对应 EPL：目录_自动更新(8)（EPL 原版在
        重聚日常更新时也写死巅峰目录 11，是缺陷不照搬，各写各的键）。
        注意：set_client_entries 只同步现行日常（雷小伊）→雷小伊目录7、
        现行巅峰→重聚目录11，重聚目录8 必须显式写。
        同目录同基础名（去版本号）的 exe 中版本号最大者胜出；无更新则只记日志。"""
        cfg = self._config_manager
        old_path = cfg.get_chongju_daily_path()
        if not old_path:
            return
        new_path = get_latest_version_path(old_path)
        if os.path.abspath(new_path) == os.path.abspath(old_path):
            self._log(f"重聚日常目录暂无更新（{os.path.basename(old_path)} 已是最新版本）")
            return
        entries = cfg.get_client_entries()
        for e in entries:
            if e.kind == "日常" and "重聚" in e.name:
                e.path = new_path
        cfg.set_client_entries(entries)  # 只同步雷小伊目录7/重聚目录11（值未变，无副作用）
        cfg.set_client_path("重聚", "重聚目录8", new_path)  # 现行日常≠重聚，显式写
        try:
            cfg.save()
        except Exception as e:
            self._log(f"重聚日常更新_写回配置失败: {e}")
        # 整批后续账号/自检重启统一用新路径（_launch_one 重聚日常分支读取）
        self._cj_daily_updated_path = new_path
        self._log(f"重聚日常版本已切换: {os.path.basename(old_path)} → {os.path.basename(new_path)}")

    # ============================================================
    # 掉线检测与重启
    # ============================================================

    def check_and_restart_daily(self) -> None:
        """
        日常掉线检测与重启。
        对应 EPL：日常_检测掉线
        检测条件：超时 / 窗口不存在 / 输出框为空 / 输出含掉线关键字
        掉线关键字："游戏断开" "登录超时" "数据无需更新" "登录成功[H5]"
        """
        # 仅检查「正在运行日常」的账号；未启动/已关闭的跳过（避免空闲时误判掉线并拉起）
        queue = self._account_manager.get_daily_queue()
        running = [i for i, q in enumerate(queue)
                   if q >= 0 and self._account_manager.accounts[i].current_action == "运行日常"]
        if not running:
            return

        self._log("本轮日常自检_重启中")
        disconnected: list[int] = []

        for i in running:
            account = self._account_manager.accounts[i]
            gw = self._daily_windows[i]

            is_offline = False

            if gw is None:
                is_offline = True
            elif gw.timed_out:
                is_offline = True
            elif account.display_name == "":
                is_offline = True
            elif not WindowFinder.is_window_valid(gw.window_handle):
                is_offline = True
            elif gw.output_handle <= 0:
                is_offline = True
            else:
                # 检查输出框内容
                content = external_edit_get_content(gw.output_handle)
                lines = content.split("\n")
                line_count = len(lines)

                keywords = ["游戏断开", "登录超时", "数据无需更新", "登录成功[H5]"]

                # 检查最后一行
                if not is_offline and line_count >= 1:
                    is_offline = any(kw in lines[-1] for kw in keywords)

                # 检查倒数第三行
                if not is_offline and line_count >= 3:
                    is_offline = any(kw in lines[-3] for kw in keywords)

                # 检查倒数第五行
                if not is_offline and line_count >= 5:
                    is_offline = any(kw in lines[-5] for kw in keywords)

                # "游戏断开，90秒后重登" → 在最后一行或倒数行检查
                if not is_offline and line_count >= 1:
                    if "游戏断开" in lines[-1]:
                        is_offline = True

            if is_offline:
                disconnected.append(i)

        if not disconnected:
            # 全部完成
            self._log("日常自检_全部完成！！！")
            self._notifier.notify_all_complete("日常")
            self.activate_tile(ClientType.LEI_XIAO_YI)
        else:
            # 有掉线，对每个掉线账号逐个重启
            exe_path = self._config_manager.get_current_daily_path()
            for i in disconnected:
                self.restart_single(i, ClientType.LEI_XIAO_YI, exe_path)
            interval_min = self._next_check_interval(len(disconnected))
            self._log(f"日常{interval_min}分钟后自检（掉线{len(disconnected)}个）")

        self._log("本轮日常自检_结束")

    def check_and_restart_peak(self) -> None:
        """
        巅峰掉线检测与重启。
        对应 EPL：巅峰_检测掉线
        检测条件：超时 / 窗口不存在 / 输出框含"已掉线"
        """
        # 仅检查「正在运行巅峰」的账号；未启动/已关闭的跳过（避免空闲时误判掉线并拉起）
        queue = self._account_manager.get_peak_queue()
        running = [i for i, q in enumerate(queue)
                   if q >= 0 and self._account_manager.accounts[i].current_action == "运行巅峰"]
        if not running:
            return

        self._log("本轮巅峰自检_重启中")
        disconnected: list[int] = []

        for i in running:
            account = self._account_manager.accounts[i]
            gw = self._peak_windows[i]

            is_offline = False

            if gw is None:
                is_offline = True
            elif gw.timed_out:
                is_offline = True
            elif account.display_name == "":
                is_offline = True
            elif not WindowFinder.is_window_valid(gw.window_handle):
                is_offline = True
            elif gw.output_handle > 0:
                content = external_edit_get_content(gw.output_handle)
                if "已掉线" in content:
                    is_offline = True

            if is_offline:
                disconnected.append(i)

        if not disconnected:
            # 全部完成
            self._log("巅峰自检_全部完成！！！")
            self._notifier.notify_all_complete("巅峰")
            self.activate_tile(ClientType.CHONG_JU_PEAK)
        else:
            # 有掉线，对每个掉线账号逐个重启
            exe_path = self._config_manager.get_current_peak_path()
            for i in disconnected:
                self.restart_single(i, ClientType.CHONG_JU_PEAK, exe_path)
            interval_min = self._next_check_interval(len(disconnected))
            self._log(f"巅峰{interval_min}分钟后自检（掉线{len(disconnected)}个）")

        self._log("本轮巅峰自检_结束")

    @staticmethod
    def _next_check_interval(disconnected: int) -> float:
        """
        根据掉线数量决定下次自检间隔（分钟）。
        对应 EPL 中时钟周期的动态调整：
          n>4 → 5分钟, n>3 → 3分钟, n>2 → 2.5分钟, 其他 → 2分钟
        """
        if disconnected > 4:
            return 5
        elif disconnected > 3:
            return 3
        elif disconnected > 2:
            return 2.5
        else:
            return 2

    def restart_selected(self, client_type: ClientType) -> None:
        """
        关闭所有运行中的窗口，重新启动勾选的账号。
        对应 EPL：_按钮_日常重启选中_被单击 / _按钮_巅峰重启选中_被单击
        """
        if client_type in (ClientType.LEI_XIAO_YI, ClientType.CHONG_JU_DAILY):
            mode_name = "日常"
            windows = self._daily_windows
        else:
            mode_name = "巅峰"
            windows = self._peak_windows

        self._log(f"--{mode_name}重启选中---")
        self._exec_permit = False

        # 等待所有登录线程结束（key_lock 释放）
        time.sleep(1)
        self._key_lock.acquire()

        try:
            # 关闭所有运行中的进程
            for gw in windows:
                if gw is not None:
                    self._process_manager.kill(gw)
        finally:
            self._key_lock.release()

        # 清空窗口数组（重置运行状态，确保 launch_all 不会把重启账号当"已在运行"跳过）
        for i in range(len(windows)):
            if windows[i] is not None:
                acc = self._account_manager.accounts[i]
                acc.current_action = "未启动"
                acc.display_name = ""
                windows[i] = None

        # 读取 exe 路径并重新启动（现行日常 / 现行巅峰）
        if client_type == ClientType.LEI_XIAO_YI:
            exe_path = self._config_manager.get_current_daily_path()
        elif client_type == ClientType.CHONG_JU_PEAK:
            exe_path = self._config_manager.get_current_peak_path()
        else:  # CHONG_JU_DAILY（重聚日常）：用现行日常路径
            exe_path = self._config_manager.get_current_daily_path()

        if not exe_path:
            self._log(f"{mode_name}目录不存在")
            return

        self._exec_permit = True
        self._update_flag = -1 if client_type != ClientType.CHONG_JU_PEAK else 0

        # 重新启动
        self.launch_all(client_type, exe_path)
        self._log(f"---{mode_name}重启完成---")

    # ============================================================
    # 激活平铺
    # ============================================================

    def activate_tile(self, client_type: ClientType) -> bool:
        """
        按账号数组顺序尝试激活，并显式调整窗口 Z 序；返回是否全部置前成功。
        对应 EPL：_按钮_日常激活排列_被单击 / _按钮_巅峰激活排列_被单击
        """
        if client_type in (ClientType.LEI_XIAO_YI, ClientType.CHONG_JU_DAILY):
            windows = self._daily_windows
        else:
            windows = self._peak_windows

        success = True
        for i, gw in enumerate(windows):
            if gw is not None and gw.window_handle != 0:
                try:
                    # 前台锁可能拒绝激活；堆叠顺序单独由 HWND_TOP 保证。
                    # 不改变位置/大小，也不将窗口设为永久 TOPMOST。
                    activated = WindowFinder.set_foreground_window(gw.window_handle)
                    win32gui.SetWindowPos(
                        gw.window_handle, win32con.HWND_TOP, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                    )
                    if not activated:
                        self._log(f"序号[{self._account_seq(i)}]排列_前台激活被拒绝，已调整窗口堆叠顺序")
                except Exception as exc:
                    success = False
                    self._log(f"序号[{self._account_seq(i)}]排列_窗口置前失败：{exc}")
                time.sleep(0.5)
        return success

    # ============================================================
    # 工具函数
    # ============================================================

    @staticmethod
    def _extract_username(title: str, mode: int) -> str:
        """
        从窗口标题中提取用户名。
        对应 EPL：重聚_提取用户名

        mode=1：分隔符为单个空格（用于"重聚脱机"）
        mode=2：分隔符为两个连续空格（用于"赛尔号重聚(脱机巅峰)"）
        格式："前缀-用户名 [后缀]" 或 "前缀-用户名【后缀"
        """
        separator = " " if mode == 1 else "  "
        sep_pos = title.find(separator)

        if sep_pos != -1:
            start = sep_pos + mode
        else:
            # 尝试中文括号作为分隔
            sep_pos = title.find("】")
            if sep_pos != -1:
                start = sep_pos + mode
            else:
                return ""

        # 查找结束标记
        end = title.find(" [", start)
        if end == -1:
            end = title.find("【", start)

        if end != -1:
            return title[start:end]
        else:
            return title[start:]

    def _match_peak_controls(self, main_hwnd: int, gw: GameWindow) -> dict[int, int]:
        """枚举重聚巅峰窗口子控件，按 ctrl.ini 控件ID匹配，返回 {键: 句柄} 映射"""
        handles: dict[int, int] = {}
        for child_hwnd in get_child_windows(main_hwnd):
            cid = get_control_id(child_hwnd)
            if not cid:
                # 护栏：[窗口配置] 缺键时 _get_config 补 0，而窗口里真实存在
                # id=0 的子控件（Afx 渲染面/SysHeader32），cid==0 会误绑假控件，
                # 让 set_check 静默写进无效句柄（曾致「仅用第五」长期失效且无日志）
                continue
            if cid == self._get_config(5):       # 账号
                handles[1] = child_hwnd
            elif cid == self._get_config(6):     # 密码
                handles[2] = child_hwnd
            elif cid == self._get_config(7):     # 登录/退出
                handles[3] = child_hwnd
            elif cid == self._get_config(8):     # 大选择夹
                handles[4] = child_hwnd
            elif cid == self._get_config(9):     # 小选择夹
                handles[5] = child_hwnd
            elif cid == self._get_config(10):    # 一键刷任务
                handles[6] = child_hwnd
            elif cid == self._get_config(32):    # 商店列表
                handles[7] = child_hwnd
            elif cid == self._get_config(14):    # 道具兑换按钮
                handles[8] = child_hwnd
            elif cid == self._get_config(35):    # 静态轮播
                handles[11] = child_hwnd
            elif cid == self._get_config(36):    # 自动领取通行证奖励
                handles[12] = child_hwnd
            elif cid == self._get_config(37):    # 刷完自动匹配
                handles[13] = child_hwnd
            elif cid == self._get_config(38):    # 不要胜利任务
                handles[14] = child_hwnd
            elif cid == self._get_config(40):    # 狂野6v6（仅留档，启动不再点它）
                handles[15] = child_hwnd
            elif cid == self._get_config(54):    # 仅用第五勾选框(1560)
                handles[16] = child_hwnd
            elif cid == self._get_config(55):    # 不领取段位奖励
                handles[17] = child_hwnd
            elif cid == self._get_config(39):    # 做狂野任务勾选框(1600)
                handles[18] = child_hwnd
            elif cid == self._get_config(61):    # 商店蓝晶币(2140)
                handles[19] = child_hwnd
            elif cid == self._get_config(62):    # 商店紫晶币(2130)
                handles[20] = child_hwnd
            elif cid == self._get_config(63):    # 商店加载拥有数量(1090)
                handles[21] = child_hwnd
            elif cid == self._get_config(51):    # 巅峰输出框
                gw.output_handle = child_hwnd
        return handles

    def _seals_to_exchange(self, i: int, account: Account) -> list[tuple[int, str]]:
        """账号刻印1/2 → 待兑换 [(编号, 匹配关键字)] 列表。
        编号 0（仓库页清空，不兑换）与 [刻印名称] 字典里没有的编号直接跳过
        ——只有读取到字典里有的才进行兑换。"""
        out: list[tuple[int, str]] = []
        for no in (account.engrave_y1, account.engrave_y2):
            if not no:
                continue  # 空项：不兑换
            kw = self._seal_keywords.get(no, "")
            if not kw:
                self._log(f"序号[{self._account_seq(i)}]刻印编号{no}不在[刻印名称]表，跳过该项兑换")
                continue
            out.append((no, kw))
        return out

    def _exchange_peak_seals(self, handles: dict[int, int], i: int) -> None:
        """
        巅峰商店页兑换刻印1/刻印2（2.8.1 实测版）。
        实测结论：
        - 商店列表为 SysListView32：LVM 消息跨进程不可靠（挂起/崩溃），必须 UIA 定位行。
        - 点击行【左侧】区域才选中（点行中间不选中）。
        - 兑换按钮必须真实鼠标点击（PostMessage BM_CLICK 不可靠）。
        - 刻印行名如「圣战之渊α（攻体）」，按账号字段13/14 的编号匹配关键字。
        """
        store_list = handles.get(7)      # 商店列表 1330
        exchange_btn = handles.get(8)    # 兑换按钮 1100
        load_btn = handles.get(21)       # 加载拥有数量 1090
        account = self._account_manager.accounts[i]

        if not store_list or not exchange_btn:
            self._log(f"序号[{self._account_seq(i)}]兑换控件缺失，跳过兑换")
            return

        if load_btn:
            real_click_control(load_btn)
            delay(1500)

        import ctypes
        coinit_ok = False
        try:
            coinit_ok = ctypes.windll.ole32.CoInitializeEx(None, 2) == 0
        except Exception:
            pass

        try:
            try:
                import uiautomation as ua
            except ImportError:
                self._log("未安装 uiautomation，跳过刻印兑换")
                return

            for engrave_no, kw in self._seals_to_exchange(i, account):
                target = None
                try:
                    items = ua.ControlFromHandle(store_list).GetChildren()
                    for it in items:
                        if kw and kw in it.Name:
                            target = it
                            break
                except Exception as e:
                    self._log(f"序号[{self._account_seq(i)}]商店列表读取异常: {e}")
                    return

                if target is None:
                    self._log(f"序号[{self._account_seq(i)}]商店列表未找到刻印编号{engrave_no}({kw})，跳过")
                    continue

                r = target.BoundingRectangle
                # 点行左侧区域才选中（实测行中间不选中）
                real_click_screen(r.left + 30, r.top + r.height() // 2)
                delay(800)
                real_click_control(exchange_btn)
                delay(1500)
                self._log(f"序号[{self._account_seq(i)}]已兑换{target.Name}")
        finally:
            if coinit_ok:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _extract_crystal(hwnd: int) -> int:
        """
        从晶币标签控件提取数值（WM_GETTEXT 跨进程可靠）。
        格式："蓝晶币：3075" / "紫晶币：1955"
        """
        title = get_control_text(hwnd)
        if "：" in title:
            parts = title.split("：", 1)
            try:
                return int(parts[1])
            except (ValueError, IndexError):
                return 0
        return 0

    def _wait_for_children(self, hwnd: int, timeout: float = 30.0) -> list[int]:
        """等待窗口子控件就绪，返回子窗口句柄列表（超时返回空列表，调用方按控件缺失处理）"""
        start = time.time()
        while time.time() - start < timeout:
            children = get_child_windows(hwnd)
            if len(children) > 0:
                return children
            time.sleep(0.3)
        return []

    # ============================================================
    # 停止
    # ============================================================

    def _join_selfcheck(self, thread_attr: str, label: str, timeout: float = 20.0) -> None:
        """等待指定自检线程退出（调用方需先 set 对应停止信号）。
        线程正常在 Event.wait 中被唤醒后立即退出；若正卡在窗口操作则最长等
        timeout 秒，超时仅告警不阻塞（daemon 线程，信号已置位迟早退出）。"""
        t = getattr(self, thread_attr, None)
        if t is None or not t.is_alive() or t is threading.current_thread():
            return
        t.join(timeout)
        if t.is_alive():
            self._log(f"{label}自检_已收到停止信号但线程{int(timeout)}秒内未退出（可能正卡在窗口操作）")

    def stop(self, client_type: Optional[ClientType] = None) -> None:
        """
        停止引擎。
        对应 EPL：_按钮_日常全部关闭_被单击 / _按钮_巅峰全部关闭_被单击
        自检线程的长睡眠为 Event.wait，set 信号后会被立即唤醒退出，
        再 join 等待确认，保证「全部关闭」真正结束自检线程。
        """
        self._exec_permit = False

        if client_type is None:
            self._daily_sc_stop.set()
            self._peak_sc_stop.set()
            self._stop_windows(self._daily_windows, "日常")
            self._stop_windows(self._peak_windows, "巅峰")
            self._join_selfcheck("_lei_selfcheck_thread", "日常")
            self._join_selfcheck("_xrym_selfcheck_thread", "XryM")
            self._join_selfcheck("_chongju_daily_selfcheck_thread", "重聚日常")
            self._join_selfcheck("_peak_selfcheck_thread", "巅峰")
        elif client_type in (ClientType.LEI_XIAO_YI, ClientType.CHONG_JU_DAILY):
            self._daily_sc_stop.set()  # 通知日常自检线程退出
            self._stop_windows(self._daily_windows, "日常")
            self._join_selfcheck("_lei_selfcheck_thread", "日常")
            self._join_selfcheck("_xrym_selfcheck_thread", "XryM")
            self._join_selfcheck("_chongju_daily_selfcheck_thread", "重聚日常")
        else:
            self._peak_sc_stop.set()  # 通知巅峰自检线程退出
            self._stop_windows(self._peak_windows, "巅峰")
            self._join_selfcheck("_peak_selfcheck_thread", "巅峰")

    def _stop_windows(self, windows: list[Optional[GameWindow]], mode: str) -> None:
        """关闭指定窗口数组中的所有进程"""
        accounts = self._account_manager.accounts
        changed = False
        for i, gw in enumerate(windows):
            if gw is not None:
                # 先设取消标记再杀进程（与 stop_single 一致）：登录线程在
                # 控件匹配/初始化等待等循环里查到此标记会立即退出，
                # 不会再对已死窗口空转刷「控件缺失」日志
                gw.login_cancelled.set()
                self._process_manager.kill(gw)
                acc = accounts[i]
                acc.display_name = ""  # 清空账号名列
                acc.current_action = "未启动"
                windows[i] = None
                changed = True

        if changed and self._on_state_changed:
            self._on_state_changed()  # 关闭后清空运行页账号名列
        self._log(f"---{mode}全部关闭---")

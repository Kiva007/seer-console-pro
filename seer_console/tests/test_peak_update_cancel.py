# -*- coding: utf-8 -*-
"""重聚巅峰更新逻辑 + 登录取消机制单元测试（2026-09 巅峰更新卡住修复）。

覆盖需求（诊断结论 → 修复方案）：
  1. _persist_peak_path_after_update：更新后重扫目录最新版本并写回配置
     （对应 EPL 巅峰_程序更新收尾的 目录_自动更新(11)）
  2. _chong_ju_login_cancelled：登录取消判定（login_cancelled 标记 /
     窗口数组被替换清除 / 主窗口句柄失效 / 句柄缺省跳过）
  3. _stop_windows：杀进程前先设 login_cancelled（对齐 stop_single）
  4. _wait_launch_interval：批量账号间隔可中断等待
  5. _launch_one：巅峰批量更新后统一用新路径启动（_peak_updated_path 覆盖）
  6. _find_update_popup：更新弹窗识别（同 pid、WTWindow、可见、标题≠主标题）
     （第二轮：2.8.x 主窗口先创建，弹窗后出，一次性标题检查失效）
  7. launch_all：巅峰更新后同账号重试（EPL「到循环尾」不推进索引）；
     队首账号不活跃时首个活跃账号正常启动（修复永久挂起）
  8. _handle_update：点弹窗按钮 → 等销毁 → 清孤儿 → 重扫写回
  9. _kill_orphan_peak_processes：只杀重聚目录下不受管的进程

运行（无 pytest 依赖，同 test_stall_watchdog.py 风格）：
    python seer_console/tests/test_peak_update_cancel.py
"""

import os
import sys
import time
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.account import AccountManager
from seer_console.core.config import ConfigManager, ClientEntry
from seer_console.core.engine import (AutomationEngine, ClientType,
                                      _PEAK_MAIN_TITLE, _CJ_DAILY_MAIN_TITLE)


# ============================================================
# 断言工具
# ============================================================

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ============================================================
# 测试环境
# ============================================================

LEDGER_LINES = [
    "1|*|900000019|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6",
    "1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6",
]


def make_ledger() -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="gbk")
    tmp.write("\n".join(LEDGER_LINES) + "\n")
    tmp.close()
    return tmp.name


class _FakeConfig:
    """构造 AutomationEngine 所需的最小 ConfigManager 桩（不含更新写回测试）。"""

    @staticmethod
    def get_all_window_config() -> dict:
        return {}

    @staticmethod
    def get_seal_names() -> dict:
        return {}


class _FakePM:
    """记录 kill/launch 调用的进程管理器桩。"""

    def __init__(self):
        self.kills: list = []
        self.launches: list[str] = []
        self.last_error = ""

    def kill(self, gw) -> None:
        self.kills.append(gw)

    def launch(self, exe_path: str):
        self.launches.append(exe_path)
        return SimpleNamespace(
            process_id=1234, login_cancelled=threading.Event(),
            login_finished=threading.Event(), login_thread=None,
            window_handle=0, output_handle=0, timed_out=False,
        )


def make_engine(pm=None, cfg=None):
    ledger = make_ledger()
    am = AccountManager(ledger)
    am.load()
    logs: list[str] = []
    engine = AutomationEngine(am, cfg or _FakeConfig(), pm or _FakePM(),
                              None, log_callback=logs.append)
    return engine, am, logs, ledger


# ============================================================
# 1. _persist_peak_path_after_update（更新后重扫写回）
# ============================================================

def test_persist_peak_path() -> None:
    print("\n[1] _persist_peak_path_after_update（重扫最新版本写回配置）")
    tmpdir = tempfile.mkdtemp(prefix="peak_upd_")
    old_exe = os.path.join(tmpdir, "赛尔号重聚(脱机巅峰) 2.8.1.exe")
    new_exe = os.path.join(tmpdir, "赛尔号重聚(脱机巅峰) 2.8.2.exe")
    for f in (old_exe, new_exe):
        open(f, "wb").close()  # 空文件即可（版本扫描不看大小，仅 XM 规则过滤残件）

    # 1a. 有新版本 → 写回 [客户端列表] 现行巅峰条目 + [重聚] 重聚目录11
    ini = os.path.join(tmpdir, "ctrl.ini")
    cfg = ConfigManager(ini)
    cfg.set_client_entries([
        ClientEntry(name="重聚 巅峰", path=old_exe, kind="巅峰", is_peak=True),
    ])
    cfg.save()
    engine, _, logs, ledger = make_engine(cfg=cfg)
    try:
        engine._persist_peak_path_after_update()
        reread = ConfigManager(ini)
        reread.load()
        check("1a 配置现行巅峰路径切换为新版本",
              reread.get_current_peak_path() == new_exe,
              f"got {reread.get_current_peak_path()!r}")
        check("1a 旧键 [重聚]重聚目录11 同步写回",
              reread.get_client_path("重聚", "重聚目录11") == new_exe,
              f"got {reread.get_client_path('重聚', '重聚目录11')!r}")
        check("1a _peak_updated_path 已记录",
              engine._peak_updated_path == new_exe,
              f"got {engine._peak_updated_path!r}")
        check("1a 日志含版本切换",
              any("巅峰版本已切换" in m for m in logs), f"logs={logs}")
    finally:
        os.unlink(ledger)

    # 1b. 无新版本 → 配置不变、只记「暂无更新」
    ini2 = os.path.join(tmpdir, "ctrl2.ini")
    cfg2 = ConfigManager(ini2)
    cfg2.set_client_entries([
        ClientEntry(name="重聚 巅峰", path=new_exe, kind="巅峰", is_peak=True),
    ])
    cfg2.save()
    engine2, _, logs2, ledger2 = make_engine(cfg=cfg2)
    try:
        engine2._persist_peak_path_after_update()
        reread2 = ConfigManager(ini2)
        reread2.load()
        check("1b 无更新时路径保持不变",
              reread2.get_current_peak_path() == new_exe)
        check("1b 日志含「暂无更新」",
              any("暂无更新" in m for m in logs2), f"logs={logs2}")
        check("1b 无更新时 _peak_updated_path 不变（仍为初始空串）",
              engine2._peak_updated_path == "")
    finally:
        os.unlink(ledger2)

    # 1c. 无现行巅峰条目但有旧键 → 走兜底分支写回旧键
    ini3 = os.path.join(tmpdir, "ctrl3.ini")
    cfg3 = ConfigManager(ini3)
    cfg3.set_client_entries([
        ClientEntry(name="备用登录器", path=os.path.join(tmpdir, "x.exe"), kind="登录器"),
    ])
    cfg3.set_client_path("重聚", "重聚目录11", old_exe)
    cfg3.save()
    engine3, _, logs3, ledger3 = make_engine(cfg=cfg3)
    try:
        engine3._persist_peak_path_after_update()
        reread3 = ConfigManager(ini3)
        reread3.load()
        check("1c 兜底分支：旧键切到新版本",
              reread3.get_client_path("重聚", "重聚目录11") == new_exe,
              f"got {reread3.get_client_path('重聚', '重聚目录11')!r}")
        check("1c _peak_updated_path 已记录",
              engine3._peak_updated_path == new_exe)
    finally:
        os.unlink(ledger3)

    # 1d. 配置无巅峰路径 → 无操作不抛异常
    ini4 = os.path.join(tmpdir, "ctrl4.ini")
    cfg4 = ConfigManager(ini4)
    cfg4.save()
    engine4, _, _, ledger4 = make_engine(cfg=cfg4)
    try:
        engine4._persist_peak_path_after_update()  # 不应抛异常
        check("1d 空路径无操作不抛异常", True)
    except Exception as e:
        check("1d 空路径无操作不抛异常", False, repr(e))
    finally:
        os.unlink(ledger4)

    for f in (old_exe, new_exe, ini, ini2, ini3, ini4):
        try:
            os.unlink(f)
        except OSError:
            pass
    os.rmdir(tmpdir)


# ============================================================
# 2. _chong_ju_login_cancelled（登录取消判定）
# ============================================================

def test_login_cancelled() -> None:
    print("\n[2] _chong_ju_login_cancelled（登录取消判定）")
    engine, _, logs, ledger = make_engine()
    try:
        gw = SimpleNamespace(login_cancelled=threading.Event())
        engine._peak_windows[0] = gw
        check("2a 存活巅峰窗口 → 不取消",
              not engine._chong_ju_login_cancelled(0, gw, "测试"))

        gw2 = SimpleNamespace(login_cancelled=threading.Event())
        engine._daily_windows[1] = gw2
        check("2b 存活日常窗口 → 不取消",
              not engine._chong_ju_login_cancelled(1, gw2, "测试"))

        gw.login_cancelled.set()
        check("2c login_cancelled 已设 → 取消",
              engine._chong_ju_login_cancelled(0, gw, "测试"))
        check("2c 取消日志已输出",
              any("登录已取消" in m for m in logs), f"logs={logs}")

        engine._peak_windows[0] = None  # _stop_windows 清除数组
        gw3 = SimpleNamespace(login_cancelled=threading.Event())
        check("2d 窗口数组被清除 → 取消",
              engine._chong_ju_login_cancelled(0, gw3, "测试"))

        gw4 = SimpleNamespace(login_cancelled=threading.Event())
        engine._daily_windows[1] = gw4
        # 0x1DEADBEE 不可能是本机窗口句柄 → IsWindow 为假
        check("2e 主窗口句柄失效 → 取消",
              engine._chong_ju_login_cancelled(1, gw4, "测试", 0x1DEADBEE))
        check("2e 句柄缺省(0)跳过失效检查 → 不取消",
              not engine._chong_ju_login_cancelled(1, gw4, "测试", 0))
    finally:
        os.unlink(ledger)


# ============================================================
# 3. _stop_windows（杀进程前设取消标记）
# ============================================================

def test_stop_windows_sets_cancel() -> None:
    print("\n[3] _stop_windows（先设 login_cancelled 再杀进程）")
    pm = _FakePM()
    engine, am, _, ledger = make_engine(pm=pm)
    try:
        gws = [SimpleNamespace(login_cancelled=threading.Event()) for _ in range(2)]
        engine._peak_windows[0] = gws[0]
        engine._peak_windows[1] = gws[1]

        engine._stop_windows(engine._peak_windows, "巅峰")

        check("3a 两个进程均被杀", len(pm.kills) == 2 and pm.kills == gws,
              f"kills={len(pm.kills)}")
        check("3b 杀进程前 login_cancelled 已设",
              all(g.login_cancelled.is_set() for g in gws))
        check("3c 窗口数组已清空",
              engine._peak_windows[0] is None and engine._peak_windows[1] is None)
        check("3d 账号状态复位",
              all(a.current_action == "未启动" and a.display_name == ""
                  for a in am.accounts))
    finally:
        os.unlink(ledger)


# ============================================================
# 4. _wait_launch_interval（可中断账号间隔）
# ============================================================

def test_wait_launch_interval() -> None:
    print("\n[4] _wait_launch_interval（可中断账号间隔）")
    engine, _, _, ledger = make_engine()
    try:
        engine._exec_permit = True
        t0 = time.time()
        ok = engine._wait_launch_interval(0.6)
        check("4a 许可时等待完整返回 True", ok and 0.5 <= time.time() - t0 < 2.0)

        engine._exec_permit = False
        t0 = time.time()
        ok = engine._wait_launch_interval(0.6)
        check("4b 已停止时立即返回 False（≤0.6秒）",
              (not ok) and time.time() - t0 < 0.6)

        engine._exec_permit = True
        threading.Timer(0.3, lambda: setattr(engine, "_exec_permit", False)).start()
        t0 = time.time()
        ok = engine._wait_launch_interval(5.0)
        check("4c 等待中途收到停止信号 → 提前返回 False（≤1秒）",
              (not ok) and time.time() - t0 < 1.0)
    finally:
        os.unlink(ledger)


# ============================================================
# 5. _launch_one（巅峰批量更新后统一用新路径）
# ============================================================

def test_launch_one_peak_override() -> None:
    print("\n[5] _launch_one（_peak_updated_path 覆盖启动路径）")
    tmpdir = tempfile.mkdtemp(prefix="peak_ovr_")
    old_exe = os.path.join(tmpdir, "赛尔号重聚(脱机巅峰) 2.8.1.exe")
    new_exe = os.path.join(tmpdir, "赛尔号重聚(脱机巅峰) 2.8.2.exe")
    for f in (old_exe, new_exe):
        open(f, "wb").close()

    pm = _FakePM()
    engine, am, _, ledger = make_engine(pm=pm)
    # 不真正登录：屏蔽 login_window 线程入口
    engine.login_window = lambda *a, **k: None
    try:
        account = am.accounts[0]

        # 5a. 无更新记录 → 用传入路径
        engine._launch_one(0, ClientType.CHONG_JU_PEAK, old_exe, account)
        check("5a 无更新记录用传入路径", pm.launches[-1] == old_exe,
              f"got {pm.launches[-1]!r}")
        check("5a 账号标记运行巅峰", account.current_action == "运行巅峰")

        # 5b. 有更新记录且文件存在 → 覆盖为新路径
        engine._peak_updated_path = new_exe
        engine._launch_one(0, ClientType.CHONG_JU_PEAK, old_exe, account)
        check("5b 更新后统一用新路径", pm.launches[-1] == new_exe,
              f"got {pm.launches[-1]!r}")

        # 5c. 更新记录文件不存在 → 仍用传入路径（防呆）
        engine._peak_updated_path = os.path.join(tmpdir, "不存在.exe")
        engine._launch_one(0, ClientType.CHONG_JU_PEAK, old_exe, account)
        check("5c 更新文件缺失时回退传入路径", pm.launches[-1] == old_exe,
              f"got {pm.launches[-1]!r}")

        # 5d. 日常类型不受更新记录影响
        engine._peak_updated_path = new_exe
        account2 = am.accounts[1]
        engine.login_window = lambda *a, **k: None
        engine._launch_one(1, ClientType.CHONG_JU_DAILY, old_exe, account2)
        check("5d 日常启动不读巅峰更新记录", pm.launches[-1] == old_exe,
              f"got {pm.launches[-1]!r}")
    finally:
        os.unlink(ledger)
        for f in (old_exe, new_exe):
            try:
                os.unlink(f)
            except OSError:
                pass
        os.rmdir(tmpdir)


# ============================================================
# 6. _find_update_popup（更新弹窗识别）
# ============================================================

def test_find_update_popup() -> None:
    print("\n[6] _find_update_popup（同 pid / WTWindow / 可见 / 标题≠主标题）")
    engine, _, _, ledger = make_engine()
    try:
        from seer_console.core.client import WindowFinder
        # 假窗口表：hwnd → (类名, 标题)。find_all_windows_by_pid 返回可见窗口列表
        fake_wins = {
            100: ("WTWindow", "检测到新版本 V2.8.2"),   # 更新弹窗
            200: ("WTWindow", _PEAK_MAIN_TITLE),        # 主窗口（已可见）
            300: ("Comet.Shadow", ""),                  # 阴影窗口
            400: ("WTWindow", ""),                      # 无标题 WTWindow
        }
        orig = (WindowFinder.find_all_windows_by_pid,
                WindowFinder.get_class_name, WindowFinder.get_window_title)
        try:
            WindowFinder.find_all_windows_by_pid = staticmethod(lambda pid: list(fake_wins))
            WindowFinder.get_class_name = staticmethod(lambda h: fake_wins[h][0])
            WindowFinder.get_window_title = staticmethod(lambda h: fake_wins[h][1])

            got = engine._find_update_popup(1234, 200)
            check("6a 识别更新弹窗（标题≠主标题）", got == 100, f"got {got}")

            del fake_wins[100]
            got = engine._find_update_popup(1234, 200)
            check("6b 无弹窗时返回 0", got == 0, f"got {got}")

            fake_wins[100] = ("WTWindow", "检测到新版本 V2.8.2")
            fake_wins[200] = ("WTWindow", "检测到新版本 V2.8.2")  # main_hwnd 同名也该被排除
            got = engine._find_update_popup(1234, 200)
            check("6c main_hwnd 自身被排除", got == 100, f"got {got}")
        finally:
            WindowFinder.find_all_windows_by_pid = staticmethod(orig[0])
            WindowFinder.get_class_name = staticmethod(orig[1])
            WindowFinder.get_window_title = staticmethod(orig[2])
    finally:
        os.unlink(ledger)


# ============================================================
# 7. launch_all（同账号重试 + 首个活跃账号启动）
# ============================================================

def test_launch_all_peak_retry() -> None:
    print("\n[7] launch_all（更新后同账号重试 / 首个活跃账号启动）")
    engine, am, logs, ledger = make_engine()
    try:
        engine._account_manager.get_peak_queue = lambda: [0, 1]
        calls: list[tuple] = []

        def fake_launch_one(i, ct, exe_path, account):
            calls.append((i, os.path.basename(exe_path)))
            if len(calls) == 1:
                # 首账号登录线程：检测到更新（EPL：账号状态改为「巅峰更新」，标志=1）
                account.current_action = "巅峰更新"
                engine._update_flag = 1
            else:
                account.current_action = "运行巅峰"
                engine._update_flag = 0

        engine._launch_one = fake_launch_one
        engine._wait_launch_interval = lambda s: True
        engine.launch_all(ClientType.CHONG_JU_PEAK, r"C:\dir\旧版.exe", selfcheck=False)

        check("7a 更新后同账号重试（0,0,1 顺序）",
              calls == [(0, "旧版.exe"), (0, "旧版.exe"), (1, "旧版.exe")],
              f"calls={calls}")
        check("7b 重试日志已输出",
              any("更新完成，用新版本重试登录" in m for m in logs), f"logs={logs}")
        check("7c 全部运行完成", any("---巅峰全部运行---" in m for m in logs))
    finally:
        os.unlink(ledger)

    # 7d：队首账号不活跃 → 首个活跃账号正常启动（修复永久挂起）
    engine2, am2, logs2, ledger2 = make_engine()
    try:
        engine2._account_manager.get_daily_queue = lambda: [-1, 1]
        calls2: list[int] = []

        def fake_launch_one2(i, ct, exe_path, account):
            calls2.append(i)
            account.current_action = "运行日常"
            engine2._update_flag = 0  # 模拟登录线程检测结果

        engine2._launch_one = fake_launch_one2
        engine2._wait_launch_interval = lambda s: True
        engine2.launch_all(ClientType.LEI_XIAO_YI, r"C:\dir\lei.exe", selfcheck=False)
        check("7d 队首不活跃时首个活跃账号正常启动", calls2 == [1],
              f"calls={calls2}")
    finally:
        os.unlink(ledger2)

    # 7e：更新持续失败（每次都检测到新版本）→ 连续 5 次后熔断停止，不再无限重试
    engine3, am3, logs3, ledger3 = make_engine()
    try:
        engine3._account_manager.get_peak_queue = lambda: [0, 1]
        calls3: list[int] = []

        def fake_launch_one3(i, ct, exe_path, account):
            calls3.append(i)
            account.current_action = "巅峰更新"
            engine3._update_flag = 1  # 每次都「有更新」→ 模拟按钮失效死循环

        engine3._launch_one = fake_launch_one3
        engine3._wait_launch_interval = lambda s: True
        engine3.launch_all(ClientType.CHONG_JU_PEAK, r"C:\dir\旧.exe", selfcheck=False)
        check("7e 连续更新 5 次后熔断（共启动 6 次）", len(calls3) == 6,
              f"calls3={len(calls3)}")
        check("7e 熔断日志已输出",
              any("停止启动" in m and "仍检测到新版本" in m for m in logs3),
              f"logs3={logs3}")
    finally:
        os.unlink(ledger3)


# ============================================================
# 8. _handle_update（弹窗点击 → 等销毁 → 清孤儿 → 重扫写回）
# ============================================================

def test_handle_update_flow() -> None:
    print("\n[8] _handle_update（点按钮/等销毁/清孤儿/写回配置）")
    import seer_console.core.engine as eng_mod
    engine, _, logs, ledger = make_engine()
    try:
        # 配置索引31 = 更新按钮ID 220（_FakeConfig.get_all_window_config 返回 {} → 补上）
        engine._config_vals = {31: 220}
        clicked: list[int] = []
        orphans: list[int] = []
        persists: list[bool] = []
        # 模块级函数打桩（engine 内以全局名引用）；结束后必须恢复
        orig_fns = (eng_mod.get_child_windows, eng_mod.get_control_id,
                    eng_mod.post_click, eng_mod.WindowFinder.is_window_valid,
                    eng_mod.get_latest_version_path,
                    eng_mod.external_edit_get_content)
        # 巅峰自带更新成功判定：目录「落盘」了比当前更新的版本
        engine._config_manager.get_current_peak_path = lambda: r"C:\dir\旧版【2.8.4】.exe"
        try:
            eng_mod.get_child_windows = lambda h: [555]
            eng_mod.get_control_id = lambda h: 220
            eng_mod.post_click = clicked.append
            eng_mod.get_latest_version_path = (
                lambda p: r"C:\dir\新版【2.8.5】.exe")
            eng_mod.external_edit_get_content = lambda h: ""
            eng_mod.WindowFinder.is_window_valid = staticmethod(lambda h: False)  # 弹窗已销毁
            engine._kill_orphan_peak_processes = lambda: orphans.append(1)
            engine._persist_peak_path_after_update = lambda: persists.append(True)

            ok = engine._handle_update(999, ClientType.CHONG_JU_PEAK, pid=1234)
            check("8a 点击了 ID=220 更新按钮", clicked == [555], f"clicked={clicked}")
            check("8b 孤儿进程清理已执行", orphans == [1])
            check("8c 重扫写回已执行", persists == [True])
            check("8d 日志含「巅峰更新完成」",
                  any("巅峰更新完成" in m for m in logs), f"logs={logs}")
            check("8d2 自带更新成功返回 True", ok is True)

            # 8e. 按钮缺失 → 记日志不崩溃，流程继续
            eng_mod.get_child_windows = lambda h: []
            engine._handle_update(999, ClientType.CHONG_JU_PEAK, pid=1234)
            check("8e 按钮缺失时记日志继续",
                  any("未找到更新按钮" in m for m in logs))
        finally:
            (eng_mod.get_child_windows, eng_mod.get_control_id,
             eng_mod.post_click) = orig_fns[:3]
            (eng_mod.WindowFinder.is_window_valid,
             eng_mod.get_latest_version_path,
             eng_mod.external_edit_get_content) = (
                staticmethod(orig_fns[3]), orig_fns[4], orig_fns[5])
    finally:
        os.unlink(ledger)


# ============================================================
# 9. _kill_orphan_peak_processes（只杀不受管的目录进程）
# ============================================================

def test_kill_orphan_processes() -> None:
    print("\n[9] _kill_orphan_peak_processes（排除受管窗口/其他目录）")
    import seer_console.core.engine as eng_mod
    tmpdir = tempfile.mkdtemp(prefix="orphan_")
    otherdir = tempfile.mkdtemp(prefix="orphan_other_")
    peak_exe = os.path.join(tmpdir, "重聚脱机巅峰【2.8.2】.exe")
    open(peak_exe, "wb").close()
    other_exe = os.path.join(otherdir, "别的东西.exe")
    open(other_exe, "wb").close()
    engine, _, logs, ledger = make_engine()
    try:
        class _Cfg:
            def get_current_peak_path(self):
                return peak_exe
        engine._config_manager = _Cfg()

        killed: list[int] = []

        class _FakeProc:
            def __init__(self, pid, exe):
                self.info = {"pid": pid, "exe": exe}
            def children(self, recursive=True):
                return []
            def kill(self):
                killed.append(self.info["pid"])

        # 100=目录内孤儿（杀）；200=目录内但受管（保留）；300=其他目录（保留）
        procs = [_FakeProc(100, peak_exe), _FakeProc(200, peak_exe),
                 _FakeProc(300, other_exe)]
        fake_psutil = SimpleNamespace(
            process_iter=lambda attrs: procs,
            Error=type("Error", (Exception,), {}),
        )
        orig_psutil = eng_mod.psutil
        eng_mod.psutil = fake_psutil
        try:
            gw = SimpleNamespace(process_id=200)
            engine._peak_windows[0] = gw
            engine._kill_orphan_peak_processes()
        finally:
            eng_mod.psutil = orig_psutil
        check("9a 只杀目录内不受管进程", killed == [100], f"killed={killed}")
        check("9b 清理日志已输出", any("孤儿进程" in m for m in logs))
    finally:
        os.unlink(ledger)
        for f, d in ((peak_exe, tmpdir), (other_exe, otherdir)):
            try:
                os.unlink(f)
            except OSError:
                pass
            try:
                os.rmdir(d)
            except OSError:
                pass


# ============================================================
# 10. 刻印空项语义（0=不兑换，贯穿 表格编辑/显示/账本/引擎兑换）
# ============================================================

def test_engrave_empty_semantics() -> None:
    print("\n[10] 刻印空项（0=不兑换）")
    # 10a. 表格输入解析：空→0；数字0-10；名称查表；非法→None
    from seer_console.ui.tab_config import parse_engrave_text
    names = {5: "渊", 6: "烬"}
    check("10a 空输入 → 0（不兑换）", parse_engrave_text("", names) == 0)
    check("10a 编号 0 → 0", parse_engrave_text("0", names) == 0)
    check("10a 编号 5 → 5", parse_engrave_text("5", names) == 5)
    check("10a 名称「渊」→ 5", parse_engrave_text("渊", names) == 5)
    check("10a 编号 11 → None（非法不写回）", parse_engrave_text("11", names) is None)
    check("10a 未知名称 → None", parse_engrave_text("不存在", names) is None)

    # 10b. 账本持久化：清空（0）保存后重读仍是 0，不再回退默认 5/6
    ledger = make_ledger()
    try:
        am = AccountManager(ledger)
        am.load()
        am.accounts[0].engrave_y1 = 0
        am.accounts[0].engrave_y2 = 0
        am.save()
        am2 = AccountManager(ledger)
        am2.load()
        check("10b 清空刻印保存后重读为 0",
              am2.accounts[0].engrave_y1 == 0 and am2.accounts[0].engrave_y2 == 0,
              f"got {am2.accounts[0].engrave_y1},{am2.accounts[0].engrave_y2}")
        # 账本字段为空串时也按 0（不兑换）加载
        with open(ledger, "w", encoding="gbk") as f:
            f.write("1|*|900000019|pwd|未启动|0||1|2026年12月30日||星海|0|0||\n")
        am3 = AccountManager(ledger)
        am3.load()
        check("10b 账本空刻印字段加载为 0",
              am3.accounts[0].engrave_y1 == 0 and am3.accounts[0].engrave_y2 == 0)
    finally:
        os.unlink(ledger)

    # 10c. 引擎兑换过滤：空项(0)跳过；[刻印名称]字典没有的编号跳过；只留字典里有的
    engine, am, logs, ledger = make_engine()
    try:
        engine._seal_keywords = {5: "渊"}
        acc = am.accounts[0]
        acc.engrave_y1 = 0      # 清空 → 不兑换
        acc.engrave_y2 = 5      # 字典有 → 兑换
        seals = engine._seals_to_exchange(0, acc)
        check("10c 空项被过滤，仅剩字典项",
              seals == [(5, "渊")], f"seals={seals}")

        acc.engrave_y1 = 7      # 字典没有的编号
        acc.engrave_y2 = 0
        seals = engine._seals_to_exchange(0, acc)
        check("10c 字典缺失编号被过滤", seals == [], f"seals={seals}")
        check("10c 字典缺失有日志", any("不在[刻印名称]表" in m for m in logs))

        # 两项都空 → 完全不兑换（无日志噪音）
        logs.clear()
        acc.engrave_y1 = 0
        acc.engrave_y2 = 0
        seals = engine._seals_to_exchange(0, acc)
        check("10c 两项全空不兑换且无日志", seals == [] and not logs,
              f"seals={seals} logs={logs}")
    finally:
        os.unlink(ledger)


# ============================================================
# 11. 重聚日常自动更新（2026-09：与巅峰 2.8.x 弹窗结构实测完全一致）
# ============================================================

def test_find_update_popup_daily() -> None:
    print("\n[11a] _find_update_popup（expected_title 参数化：重聚日常）")
    engine, _, _, ledger = make_engine()
    try:
        from seer_console.core.client import WindowFinder
        # 日常场景：主窗口「重聚脱机」+ 更新弹窗「检测到新版本 V2.2.2」
        fake_wins = {
            100: ("WTWindow", "检测到新版本 V2.2.2"),
            200: ("WTWindow", _CJ_DAILY_MAIN_TITLE),
            300: ("Comet.Shadow", ""),
        }
        orig = (WindowFinder.find_all_windows_by_pid,
                WindowFinder.get_class_name, WindowFinder.get_window_title)
        try:
            WindowFinder.find_all_windows_by_pid = staticmethod(lambda pid: list(fake_wins))
            WindowFinder.get_class_name = staticmethod(lambda h: fake_wins[h][0])
            WindowFinder.get_window_title = staticmethod(lambda h: fake_wins[h][1])
            check("11a 日常主标题下识别更新弹窗",
                  engine._find_update_popup(1234, 200, _CJ_DAILY_MAIN_TITLE) == 100)
            check("11a 缺省仍按巅峰主标题匹配（向后兼容）",
                  engine._find_update_popup(1234, 200) == 100)
            # 主窗口标题被误当弹窗的情况：expected_title=日常时，巅峰主窗口
            # 标题≠日常标题也会命中——同 pid 约束下（一个客户端一个 pid）安全
            del fake_wins[100]
            check("11a 无弹窗时返回 0",
                  engine._find_update_popup(1234, 200, _CJ_DAILY_MAIN_TITLE) == 0)
        finally:
            WindowFinder.find_all_windows_by_pid = staticmethod(orig[0])
            WindowFinder.get_class_name = staticmethod(orig[1])
            WindowFinder.get_window_title = staticmethod(orig[2])
    finally:
        os.unlink(ledger)


def test_get_chongju_daily_path() -> None:
    print("\n[11b] ConfigManager.get_chongju_daily_path（条目匹配 / 回退目录8）")
    tmpdir = tempfile.mkdtemp(prefix="cjd_cfg_")
    lei_exe = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.14.exe")
    cj_exe = os.path.join(tmpdir, "重聚脱机日常【2.2.1】.exe")
    open(lei_exe, "wb").close()
    open(cj_exe, "wb").close()
    ini = os.path.join(tmpdir, "ctrl.ini")
    ini2 = os.path.join(tmpdir, "ctrl2.ini")
    try:
        # 条目匹配：kind=日常 且名称含「重聚」（雷小伊日常不会被误选）
        cfg = ConfigManager(ini)
        cfg.set_client_entries([
            ClientEntry(name="雷小伊 日常", path=lei_exe, kind="日常", is_daily=True),
            ClientEntry(name="重聚 日常", path=cj_exe, kind="日常"),
        ])
        cfg.save()
        reread = ConfigManager(ini)
        reread.load()
        check("11b 条目匹配返回重聚日常路径",
              reread.get_chongju_daily_path() == cj_exe,
              f"got {reread.get_chongju_daily_path()!r}")
        check("11b 现行日常（雷小伊）不受影响",
              reread.get_current_daily_path() == lei_exe)

        # 回退：无重聚日常条目时读 [重聚] 重聚目录8
        cfg2 = ConfigManager(ini2)
        cfg2.set_client_entries([
            ClientEntry(name="备用登录器", path=os.path.join(tmpdir, "x.exe"), kind="登录器"),
        ])
        cfg2.set_client_path("重聚", "重聚目录8", cj_exe)
        cfg2.save()
        reread2 = ConfigManager(ini2)
        reread2.load()
        check("11b 无条目时回退重聚目录8",
              reread2.get_chongju_daily_path() == cj_exe,
              f"got {reread2.get_chongju_daily_path()!r}")

        # 同步规则按名称分流（2026-09 修复）：现行日常=重聚日常时写重聚目录8，
        # 不再把重聚路径污染进雷小伊目录7（真实 ctrl.ini 曾出现此污染）
        ini3 = os.path.join(tmpdir, "ctrl3.ini")
        lei2_exe = os.path.join(tmpdir, "lei2.exe")
        cj2_exe = os.path.join(tmpdir, "cj2.exe")
        open(lei2_exe, "wb").close()
        open(cj2_exe, "wb").close()
        cfg3 = ConfigManager(ini3)
        cfg3.set_client_entries([
            ClientEntry(name="重聚 日常", path=cj2_exe, kind="日常", is_daily=True),
        ])
        cfg3.set_client_path("雷小伊", "雷小伊目录7", lei2_exe)
        cfg3.save()
        reread3 = ConfigManager(ini3)
        reread3.load()
        check("11b 现行日常=重聚时同步写重聚目录8",
              reread3.get_client_path("重聚", "重聚目录8") == cj2_exe,
              f"got {reread3.get_client_path('重聚', '重聚目录8')!r}")
        check("11b 雷小伊目录7 不被污染",
              reread3.get_client_path("雷小伊", "雷小伊目录7") == lei2_exe,
              f"got {reread3.get_client_path('雷小伊', '雷小伊目录7')!r}")
        try:
            os.unlink(ini3)
            os.unlink(lei2_exe)
            os.unlink(cj2_exe)
        except OSError:
            pass
    finally:
        for f in (lei_exe, cj_exe, ini, ini2):
            try:
                os.unlink(f)
            except OSError:
                pass
        os.rmdir(tmpdir)


def test_persist_chongju_daily_path() -> None:
    print("\n[11c] _persist_chongju_daily_path_after_update（重扫写回/不串键）")
    tmpdir = tempfile.mkdtemp(prefix="cjd_upd_")
    old_exe = os.path.join(tmpdir, "重聚脱机日常【2.2.1】.exe")
    new_exe = os.path.join(tmpdir, "重聚脱机日常【2.2.2】.exe")
    lei_exe = os.path.join(tmpdir, "雷小伊脱机日常 Beta 1.8.14.exe")
    peak_exe = os.path.join(tmpdir, "重聚脱机巅峰【2.8.2】.exe")
    for f in (old_exe, new_exe, lei_exe, peak_exe):
        open(f, "wb").close()

    ini = os.path.join(tmpdir, "ctrl.ini")
    cfg = ConfigManager(ini)
    cfg.set_client_entries([
        ClientEntry(name="雷小伊 日常", path=lei_exe, kind="日常", is_daily=True),
        ClientEntry(name="重聚 日常", path=old_exe, kind="日常"),
        ClientEntry(name="重聚 巅峰", path=peak_exe, kind="巅峰", is_peak=True),
    ])
    cfg.save()
    engine, _, logs, ledger = make_engine(cfg=cfg)
    try:
        engine._persist_chongju_daily_path_after_update()
        reread = ConfigManager(ini)
        reread.load()
        check("11c 重聚日常条目切到新版本",
              reread.get_chongju_daily_path() == new_exe,
              f"got {reread.get_chongju_daily_path()!r}")
        check("11c [重聚]重聚目录8 已写回",
              reread.get_client_path("重聚", "重聚目录8") == new_exe,
              f"got {reread.get_client_path('重聚', '重聚目录8')!r}")
        check("11c [雷小伊]雷小伊目录7 不受影响",
              reread.get_client_path("雷小伊", "雷小伊目录7") == lei_exe,
              f"got {reread.get_client_path('雷小伊', '雷小伊目录7')!r}")
        check("11c [重聚]重聚目录11（巅峰）不受影响",
              reread.get_client_path("重聚", "重聚目录11") == peak_exe,
              f"got {reread.get_client_path('重聚', '重聚目录11')!r}")
        check("11c _cj_daily_updated_path 已记录",
              engine._cj_daily_updated_path == new_exe)
        check("11c 日志含版本切换",
              any("重聚日常版本已切换" in m for m in logs), f"logs={logs}")
    finally:
        os.unlink(ledger)

    # 真实配置场景：现行日常=重聚日常（is_daily=真）→ 更新写回不污染雷小伊目录7
    ini3 = os.path.join(tmpdir, "ctrl3.ini")
    cfg3 = ConfigManager(ini3)
    cfg3.set_client_entries([
        ClientEntry(name="重聚 日常", path=old_exe, kind="日常", is_daily=True),
        ClientEntry(name="雷小伊 日常", path=lei_exe, kind="日常"),
        ClientEntry(name="重聚 巅峰", path=peak_exe, kind="巅峰", is_peak=True),
    ])
    cfg3.set_client_path("雷小伊", "雷小伊目录7", lei_exe)
    cfg3.save()
    engine3, _, logs3, ledger3 = make_engine(cfg=cfg3)
    try:
        engine3._persist_chongju_daily_path_after_update()
        reread3 = ConfigManager(ini3)
        reread3.load()
        check("11c 真实场景：重聚日常条目切到新版本",
              reread3.get_chongju_daily_path() == new_exe,
              f"got {reread3.get_chongju_daily_path()!r}")
        check("11c 真实场景：现行日常=重聚 → 同步写重聚目录8",
              reread3.get_client_path("重聚", "重聚目录8") == new_exe,
              f"got {reread3.get_client_path('重聚', '重聚目录8')!r}")
        check("11c 真实场景：雷小伊目录7 不被污染",
              reread3.get_client_path("雷小伊", "雷小伊目录7") == lei_exe,
              f"got {reread3.get_client_path('雷小伊', '雷小伊目录7')!r}")
        check("11c 真实场景：_cj_daily_updated_path 已记录",
              engine3._cj_daily_updated_path == new_exe)
    finally:
        os.unlink(ledger3)

    # 无新版本 → 只记日志，路径不变
    ini2 = os.path.join(tmpdir, "ctrl2.ini")
    cfg2 = ConfigManager(ini2)
    cfg2.set_client_entries([
        ClientEntry(name="重聚 日常", path=new_exe, kind="日常"),
    ])
    cfg2.save()
    engine2, _, logs2, ledger2 = make_engine(cfg=cfg2)
    try:
        engine2._persist_chongju_daily_path_after_update()
        check("11c 无更新时路径保持不变",
              engine2._cj_daily_updated_path == "")
        check("11c 无更新日志「暂无更新」",
              any("暂无更新" in m for m in logs2), f"logs={logs2}")
    finally:
        os.unlink(ledger2)
        for f in (old_exe, new_exe, lei_exe, peak_exe, ini, ini2, ini3):
            try:
                os.unlink(f)
            except OSError:
                pass
        os.rmdir(tmpdir)


def test_launch_one_cj_daily_override() -> None:
    print("\n[11d] _launch_one（_cj_daily_updated_path 覆盖启动路径）")
    tmpdir = tempfile.mkdtemp(prefix="cjd_ovr_")
    old_exe = os.path.join(tmpdir, "重聚脱机日常【2.2.1】.exe")
    new_exe = os.path.join(tmpdir, "重聚脱机日常【2.2.2】.exe")
    for f in (old_exe, new_exe):
        open(f, "wb").close()

    pm = _FakePM()
    engine, am, _, ledger = make_engine(pm=pm)
    engine.login_window = lambda *a, **k: None
    try:
        account = am.accounts[0]
        engine._launch_one(0, ClientType.CHONG_JU_DAILY, old_exe, account)
        check("11d 无更新记录用传入路径", pm.launches[-1] == old_exe,
              f"got {pm.launches[-1]!r}")

        engine._cj_daily_updated_path = new_exe
        engine._launch_one(0, ClientType.CHONG_JU_DAILY, old_exe, account)
        check("11d 更新后统一用新路径", pm.launches[-1] == new_exe,
              f"got {pm.launches[-1]!r}")
        check("11d gw.exe_path 已记录新路径",
              engine._daily_windows[0].exe_path == new_exe)
        check("11d 账号标记运行日常", account.current_action == "运行日常")

        engine._cj_daily_updated_path = os.path.join(tmpdir, "不存在.exe")
        engine._launch_one(0, ClientType.CHONG_JU_DAILY, old_exe, account)
        check("11d 更新文件缺失时回退传入路径", pm.launches[-1] == old_exe,
              f"got {pm.launches[-1]!r}")
    finally:
        os.unlink(ledger)
        for f in (old_exe, new_exe):
            try:
                os.unlink(f)
            except OSError:
                pass
        os.rmdir(tmpdir)


def test_launch_all_cj_daily_retry() -> None:
    print("\n[11e] launch_all（重聚日常更新后同账号重试）")
    engine, am, logs, ledger = make_engine()
    try:
        engine._account_manager.get_daily_queue = lambda: [0, 1]
        calls: list[tuple] = []

        def fake_launch_one(i, ct, exe_path, account, allow_update=True):
            calls.append((i, os.path.basename(exe_path)))
            if len(calls) == 1:
                # 首账号登录线程：检测到更新（账号状态→「重聚日常更新」，标志=1）
                account.current_action = "重聚日常更新"
                engine._update_flag = 1
            else:
                account.current_action = "运行日常"
                engine._update_flag = 0

        engine._launch_one = fake_launch_one
        engine._wait_launch_interval = lambda s: True
        engine.launch_all(ClientType.CHONG_JU_DAILY, r"C:\dir\旧版.exe", selfcheck=False)

        check("11e 更新后同账号重试（0,0,1 顺序）",
              calls == [(0, "旧版.exe"), (0, "旧版.exe"), (1, "旧版.exe")],
              f"calls={calls}")
        check("11e 重试日志已输出",
              any("更新完成，用新版本重试登录" in m for m in logs), f"logs={logs}")
        check("11e 批次日志正确",
              any("---重聚日常开始运行---" in m for m in logs)
              and any("---重聚日常全部运行---" in m for m in logs))
    finally:
        os.unlink(ledger)


def test_handle_update_daily_branch() -> None:
    print("\n[11f] _handle_update（重聚日常分流：日常路径孤儿清理+写回）")
    import seer_console.core.engine as eng_mod
    engine, _, logs, ledger = make_engine()
    daily_ref = r"D:\x\重聚脱机日常【2.2.2】.exe"
    try:
        engine._config_vals = {31: 220}

        class _Cfg:
            def get_chongju_daily_path(self):
                return daily_ref

            def get_all_window_config(self):
                return {}

            def get_seal_names(self):
                return {}

        engine._config_manager = _Cfg()
        engine._cj_daily_updated_path = daily_ref
        clicked: list[int] = []
        orphan_refs: list[str] = []
        daily_persists: list[int] = []
        peak_persists: list[int] = []
        orig_fns = (eng_mod.get_child_windows, eng_mod.get_control_id,
                    eng_mod.post_click, eng_mod.WindowFinder.is_window_valid)
        try:
            eng_mod.get_child_windows = lambda h: [555]
            eng_mod.get_control_id = lambda h: 220
            eng_mod.post_click = clicked.append
            eng_mod.WindowFinder.is_window_valid = staticmethod(lambda h: False)
            engine._kill_orphan_peak_processes = lambda ref="": orphan_refs.append(ref)
            engine._persist_chongju_daily_path_after_update = lambda: daily_persists.append(1)
            engine._persist_peak_path_after_update = lambda: peak_persists.append(1)

            engine._handle_update(999, ClientType.CHONG_JU_DAILY, pid=1234)
        finally:
            (eng_mod.get_child_windows, eng_mod.get_control_id,
             eng_mod.post_click) = orig_fns[:3]
            eng_mod.WindowFinder.is_window_valid = staticmethod(orig_fns[3])
        check("11f 点击了 ID=220 更新按钮", clicked == [555], f"clicked={clicked}")
        check("11f 孤儿清理以日常路径为基准",
              orphan_refs == [daily_ref], f"refs={orphan_refs}")
        check("11f 重聚日常写回已执行（巅峰写回未执行）",
              daily_persists == [1] and peak_persists == [],
              f"daily={daily_persists} peak={peak_persists}")
        check("11f 日志含「重聚日常更新完成」",
              any("重聚日常更新完成" in m for m in logs), f"logs={logs}")
    finally:
        os.unlink(ledger)


class _FastClock:
    """time.time/sleep 快进桩：跳过门控/控件匹配循环的真实等待。"""

    def __init__(self, start: float = 1000.0):
        self._now = start

    def time(self) -> float:
        self._now += 0.5
        return self._now

    def sleep(self, s: float) -> None:
        self._now += max(float(s), 0.5)


def test_login_chongju_daily_update_gate() -> None:
    print("\n[11g] _login_chong_ju_daily ②更新检测门控（弹窗/旧版标题/无更新放行）")
    import seer_console.core.engine as eng_mod
    from seer_console.core.client import WindowFinder
    engine, am, logs, ledger = make_engine()
    try:
        handled: list[tuple] = []
        orig = (WindowFinder.find_window_by_pid, WindowFinder.get_window_title,
                WindowFinder.is_window_valid, eng_mod.time, eng_mod.win32gui)
        try:
            WindowFinder.find_window_by_pid = staticmethod(
                lambda pid, title, cls, match_full_title=False, timeout=0: 200)
            WindowFinder.is_window_valid = staticmethod(lambda h: True)
            eng_mod.time = _FastClock()
            eng_mod.win32gui = SimpleNamespace(IsWindowVisible=lambda h: True)
            engine._handle_update = (
                lambda hwnd, ct, pid=0, i=-1, gw=None:
                    handled.append((hwnd, ct, pid)))

            # g1. 门控中发现弹窗 → 更新分支（_handle_update(CHONG_JU_DAILY)+标志1）
            WindowFinder.get_window_title = staticmethod(lambda h: "重聚脱机")
            gw1 = SimpleNamespace(process_id=1234, login_cancelled=threading.Event(),
                                  timed_out=False, window_handle=0)
            engine._daily_windows[0] = gw1
            engine._find_update_popup = lambda pid, mh, expected="": 100
            engine._login_chong_ju_daily(0, gw1)
            check("11g 门控发现弹窗→更新分支",
                  handled == [(100, ClientType.CHONG_JU_DAILY, 1234)],
                  f"handled={handled}")
            check("11g _update_flag=1 触发批量同账号重试", engine._update_flag == 1)
            check("11g 账号状态置「重聚日常更新」",
                  am.accounts[0].current_action == "重聚日常更新")
            check("11g 日志含「重聚日常正在更新」",
                  any("重聚日常正在更新" in m for m in logs), f"logs={logs}")

            # g2. 首个 WTWindow 本身就是更新窗口（旧版：标题≠「重聚脱机」）
            handled.clear()
            WindowFinder.get_window_title = staticmethod(lambda h: "检测到新版本 V2.2.2")
            gw2 = SimpleNamespace(process_id=1235, login_cancelled=threading.Event(),
                                  timed_out=False, window_handle=0)
            engine._daily_windows[0] = gw2
            engine._login_chong_ju_daily(0, gw2)
            check("11g 旧版弹窗先于主窗口→直接进更新分支",
                  handled == [(200, ClientType.CHONG_JU_DAILY, 1235)],
                  f"handled={handled}")

            # g3. 无更新（门控超时前主窗口变可见）→ 放行走登录（控件缺失退出）
            handled.clear()
            WindowFinder.get_window_title = staticmethod(lambda h: "重聚脱机")
            gw3 = SimpleNamespace(process_id=1236, login_cancelled=threading.Event(),
                                  timed_out=False, window_handle=0)
            engine._daily_windows[0] = gw3
            engine._find_update_popup = lambda pid, mh, expected="": 0
            engine._wait_for_children = lambda h: []
            engine._login_chong_ju_daily(0, gw3)
            check("11g 无更新时不进更新分支", handled == [], f"handled={handled}")
            check("11g 无更新时 _update_flag=0 放行后续账号", engine._update_flag == 0)
            check("11g 放行后正常进入登录流程（控件缺失退出）",
                  gw3.timed_out and any("控件缺失" in m for m in logs))
        finally:
            WindowFinder.find_window_by_pid = staticmethod(orig[0])
            WindowFinder.get_window_title = staticmethod(orig[1])
            WindowFinder.is_window_valid = staticmethod(orig[2])
            eng_mod.time = orig[3]
            eng_mod.win32gui = orig[4]
    finally:
        os.unlink(ledger)


# ============================================================
# 12. 启动更新开关（雷小伊/重聚日常/巅峰勾选框，2026-09）
# ============================================================

def test_auto_update_config_keys() -> None:
    print("\n[12a] 启动更新配置键（默认值 + 写读）")
    tmpdir = tempfile.mkdtemp(prefix="upd_sw_")
    ini = os.path.join(tmpdir, "ctrl.ini")
    try:
        cfg = ConfigManager(ini)
        cfg.save()
        check("12a 雷小伊启动更新默认真", cfg.get_lei_auto_update() is True)
        check("12a 重聚日常启动更新默认真",
              cfg.get_chongju_daily_auto_update() is True)
        check("12a 巅峰启动更新默认真", cfg.get_peak_auto_update() is True)
        check("12a XryM启动更新默认假（历史行为）", cfg.get_xrym_auto_update() is False)
        cfg.set_bool("雷小伊", "雷小伊启动更新", False)
        cfg.set_bool("重聚", "重聚日常启动更新", False)
        cfg.set_bool("重聚", "巅峰启动更新", False)
        cfg.save()
        reread = ConfigManager(ini)
        reread.load()
        check("12a 写假后重读为假",
              reread.get_lei_auto_update() is False
              and reread.get_chongju_daily_auto_update() is False
              and reread.get_peak_auto_update() is False)
    finally:
        try:
            os.unlink(ini)
        except OSError:
            pass
        os.rmdir(tmpdir)


def test_dismiss_update_popup() -> None:
    print("\n[12b] _dismiss_update_popup（文本「取消」优先 / ID=130 兜底 / 无按钮）")
    import seer_console.core.engine as eng_mod
    engine, _, logs, ledger = make_engine()
    try:
        clicked: list[int] = []
        orig_fns = (eng_mod.get_child_windows, eng_mod.get_control_text,
                    eng_mod.get_control_id, eng_mod.post_click,
                    eng_mod.WindowFinder.is_window_valid)
        try:
            # 文本匹配优先：多个按钮中选「取消」
            eng_mod.get_child_windows = lambda h: [555, 666, 777]
            eng_mod.get_control_text = staticmethod(
                lambda h: {555: "立即更新", 666: "取消", 777: "重试"}.get(h, ""))
            eng_mod.get_control_id = lambda h: 999
            eng_mod.post_click = clicked.append
            eng_mod.WindowFinder.is_window_valid = staticmethod(lambda h: False)
            ok = engine._dismiss_update_popup(100)
            check("12b 文本「取消」被点击", clicked == [666], f"clicked={clicked}")
            check("12b 找到取消按钮返回 True", ok is True)

            # 文本拿不到 → ID=130 兜底
            clicked.clear()
            eng_mod.get_control_text = staticmethod(lambda h: "")
            eng_mod.get_control_id = lambda h: 130 if h == 555 else 999
            ok = engine._dismiss_update_popup(100)
            check("12b ID=130 兜底被点击", clicked == [555], f"clicked={clicked}")
            check("12b 兜底也返回 True", ok is True)

            # 既无文本也无 ID → 日志 + False
            clicked.clear()
            logs.clear()
            eng_mod.get_control_id = lambda h: 999
            ok = engine._dismiss_update_popup(100)
            check("12b 无取消按钮返回 False（不点击）",
                  ok is False and clicked == [], f"ok={ok} clicked={clicked}")
            check("12b 无按钮日志已输出",
                  any("未找到「取消」按钮" in m for m in logs), f"logs={logs}")
        finally:
            (eng_mod.get_child_windows, eng_mod.get_control_text,
             eng_mod.get_control_id, eng_mod.post_click) = orig_fns[:4]
            eng_mod.WindowFinder.is_window_valid = staticmethod(orig_fns[4])
    finally:
        os.unlink(ledger)


class _UpdateSwitchEnv:
    """巅峰/重聚日常登录②步弹窗分支测试环境（开关关→点取消继续登录）。
    hwnd 200=主窗口（标题按 expected 动态返回主标题）、100=更新弹窗。"""

    def __init__(self, engine, auto_update: bool, main_title: str):
        import seer_console.core.engine as eng_mod
        from seer_console.core.client import WindowFinder
        self._eng_mod = eng_mod
        self._wf = WindowFinder
        self.engine = engine
        self.handled: list = []
        self.dismissed: list[int] = []
        self._orig = (WindowFinder.find_window_by_pid,
                      WindowFinder.get_window_title,
                      WindowFinder.is_window_valid,
                      eng_mod.time, eng_mod.win32gui,
                      engine._find_update_popup, engine._handle_update,
                      engine._dismiss_update_popup, engine._wait_for_children,
                      engine._match_peak_controls, engine._config_manager)
        WindowFinder.find_window_by_pid = staticmethod(
            lambda pid, title, cls, match_full_title=False, timeout=0: 200)
        # hwnd 200=主窗口（标题随客户端类型）、100=更新弹窗「检测到新版本」
        WindowFinder.get_window_title = staticmethod(
            lambda h: main_title if h == 200 else "检测到新版本 V2.2.2")
        WindowFinder.is_window_valid = staticmethod(lambda h: True)
        eng_mod.time = _FastClock()
        eng_mod.win32gui = SimpleNamespace(IsWindowVisible=lambda h: True)
        engine._find_update_popup = lambda pid, mh, expected="": 100
        engine._handle_update = (
            lambda hwnd, ct, pid=0, i=-1, gw=None:
                (self.handled.append((hwnd, ct, pid)), True)[1])
        engine._dismiss_update_popup = lambda h: self.dismissed.append(h)
        engine._wait_for_children = lambda h: []
        engine._match_peak_controls = lambda mh, gw: {}  # 巅峰④：控件匹配超时退出
        # 按客户端类型暴露对应开关（默认 True 走更新分支；测试改 False）
        self.auto_update = auto_update
        engine._config_manager = SimpleNamespace(
            get_peak_auto_update=lambda: self.auto_update,
            get_chongju_daily_auto_update=lambda: self.auto_update,
        )

    def set(self, on: bool) -> None:
        self.auto_update = on

    def restore(self) -> None:
        (wf, eng_mod, engine, orig) = (self._wf, self._eng_mod, self.engine, self._orig)
        wf.find_window_by_pid = staticmethod(orig[0])
        wf.get_window_title = staticmethod(orig[1])
        wf.is_window_valid = staticmethod(orig[2])
        eng_mod.time = orig[3]
        eng_mod.win32gui = orig[4]
        engine._find_update_popup = orig[5]
        engine._handle_update = orig[6]
        engine._dismiss_update_popup = orig[7]
        engine._wait_for_children = orig[8]
        engine._match_peak_controls = orig[9]
        engine._config_manager = orig[10]


def test_peak_update_switch() -> None:
    print("\n[12c] 巅峰启动更新开关（关→点取消继续登录；开→自动更新）")
    engine, am, logs, ledger = make_engine()
    env = _UpdateSwitchEnv(engine, auto_update=False,
                           main_title=_PEAK_MAIN_TITLE)
    try:
        gw = SimpleNamespace(process_id=1234, login_cancelled=threading.Event(),
                             timed_out=False, window_handle=0, output_handle=0)
        engine._peak_windows[0] = gw
        engine._login_chong_ju_peak(0, gw)
        check("12c 开关关：不进自动更新分支", env.handled == [],
              f"handled={env.handled}")
        check("12c 开关关：弹窗点了取消", env.dismissed == [100],
              f"dismissed={env.dismissed}")
        check("12c 开关关：放行后续账号", engine._update_flag == 0)
        check("12c 开关关：日志提示未开启自动更新",
              any("自动更新未开启" in m for m in logs), f"logs={logs}")
        check("12c 开关关：继续登录流程（控件匹配超时退出）",
              gw.timed_out and any("控件缺失" in m for m in logs))

        # 开关开 → 走自动更新分支（_update_flag=1 触发同账号重试）
        env.set(True)
        logs.clear()
        gw2 = SimpleNamespace(process_id=1235, login_cancelled=threading.Event(),
                              timed_out=False, window_handle=0, output_handle=0)
        engine._peak_windows[0] = gw2
        engine._login_chong_ju_peak(0, gw2)
        check("12c 开关开：进自动更新分支",
              env.handled == [(100, ClientType.CHONG_JU_PEAK, 1235)],
              f"handled={env.handled}")
        check("12c 开关开：_update_flag=1", engine._update_flag == 1)
        check("12c 开关开：不点取消", env.dismissed == [100])
    finally:
        env.restore()
        os.unlink(ledger)


def test_cj_daily_update_switch() -> None:
    print("\n[12d] 重聚日常启动更新开关（关→点取消继续登录；开→自动更新）")
    engine, am, logs, ledger = make_engine()
    env = _UpdateSwitchEnv(engine, auto_update=False, main_title="重聚脱机")
    try:
        gw = SimpleNamespace(process_id=1234, login_cancelled=threading.Event(),
                             timed_out=False, window_handle=0, output_handle=0)
        engine._daily_windows[0] = gw
        engine._login_chong_ju_daily(0, gw)
        check("12d 开关关：不进自动更新分支", env.handled == [],
              f"handled={env.handled}")
        check("12d 开关关：弹窗点了取消", env.dismissed == [100],
              f"dismissed={env.dismissed}")
        check("12d 开关关：放行后续账号", engine._update_flag == 0)
        check("12d 开关关：日志提示未开启自动更新",
              any("自动更新未开启" in m for m in logs), f"logs={logs}")
        check("12d 开关关：继续登录流程（控件匹配超时退出）",
              gw.timed_out and any("控件缺失" in m for m in logs))

        env.set(True)
        logs.clear()
        gw2 = SimpleNamespace(process_id=1235, login_cancelled=threading.Event(),
                              timed_out=False, window_handle=0, output_handle=0)
        engine._daily_windows[0] = gw2
        engine._login_chong_ju_daily(0, gw2)
        check("12d 开关开：进自动更新分支",
              env.handled == [(100, ClientType.CHONG_JU_DAILY, 1235)],
              f"handled={env.handled}")
        check("12d 开关开：_update_flag=1", engine._update_flag == 1)
    finally:
        env.restore()
        os.unlink(ledger)


# ============================================================
# main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("重聚巅峰/日常更新逻辑 + 登录取消机制测试")
    print("=" * 60)

    test_persist_peak_path()
    test_login_cancelled()
    test_stop_windows_sets_cancel()
    test_wait_launch_interval()
    test_launch_one_peak_override()
    test_find_update_popup()
    test_launch_all_peak_retry()
    test_handle_update_flow()
    test_kill_orphan_processes()
    test_engrave_empty_semantics()
    test_find_update_popup_daily()
    test_get_chongju_daily_path()
    test_persist_chongju_daily_path()
    test_launch_one_cj_daily_override()
    test_launch_all_cj_daily_retry()
    test_handle_update_daily_branch()
    test_login_chongju_daily_update_gate()
    test_auto_update_config_keys()
    test_dismiss_update_popup()
    test_peak_update_switch()
    test_cj_daily_update_switch()

    print("\n" + "=" * 60)
    if _failed:
        print(f"FAILED: {len(_failed)} 项")
        for f in _failed:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL PASSED: {_passed} 项")

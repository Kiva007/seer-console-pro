# -*- coding: utf-8 -*-
"""自检重启重排后置前（等同点一次「排列」按钮）单元测试。

背景（2026-10）：_retile_*_selfcheck 原本只做平铺摆位
（_tile_* → set_window_pos 到计算坐标），用户要求自检重启后
把全部窗口依次置到最前面。实现：四个 _retile_*_selfcheck 末尾
复用 activate_tile / activate_tile_xrym（即 UI「排列」按钮的
引擎实现，尝试激活并显式调整堆叠，不改位置）。

覆盖：
   1. 雷小伊日常：平铺全部 → activate_tile(LEI_XIAO_YI)
   2. XryM：平铺全部 → activate_tile_xrym（用重新枚举的最新
      顶层句柄，避开「进入脱机」后已重建的旧句柄）
   3. 重聚巅峰：平铺全部（is_peak=True）→ activate_tile(CHONG_JU_PEAK)
   4. 重聚日常：平铺全部（is_peak=False）→ activate_tile(CHONG_JU_DAILY)
   5. 顺序铁律：置前发生在全部平铺之后（最后置前的是最后一个窗口），
      且 gw 为 None 的槽位跳过平铺
   6. 巅峰登录失败和卡死重启都等待登录收尾，不能因昵称提前出现就重排。
   7. 雷小伊/重聚登录尚未结束或已停止时跳过重排，置前失败不误报完成。

运行（无 pytest 依赖，同 test_peak_run_config.py 风格）：
    python seer_console/tests/test_retile_selfcheck_activate.py
"""

import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.engine import AutomationEngine, ClientType


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
# 测试辅助
# ============================================================

class _StubGW:
    """最小 GameWindow 桩：窗口句柄、进程 ID 和登录完成事件。"""

    def __init__(self, handle: int = 111, pid: int = 222):
        self.window_handle = handle
        self.process_id = pid
        self.login_finished = threading.Event()
        self.login_finished.set()


def _make_engine() -> AutomationEngine:
    """裸 AutomationEngine：只装本文件涉及的槽位，避开 __init__ 重依赖。"""
    eng = object.__new__(AutomationEngine)
    eng._account_manager = SimpleNamespace(accounts=[SimpleNamespace(selected=True) for _ in range(3)])
    eng._daily_windows = []
    eng._peak_windows = []
    eng._xrym_windows = []
    eng._log = lambda msg: None
    eng._daily_sc_stop = threading.Event()
    eng._peak_sc_stop = threading.Event()
    eng.events = []          # 统一事件流（平铺/置前按实际发生顺序记录）
    eng.xrym_activate_calls = 0
    return eng


def _wire_engine(eng: AutomationEngine) -> AutomationEngine:
    """把所有副作用方法换成记录型桩（平铺/置前/句柄重枚举）。"""
    eng._tile_lei_xiao_yi = lambda i, h: eng.events.append(("tile_lei", i, h))
    eng._tile_xrym = lambda i, mh: eng.events.append(("tile_xrym", i, mh))
    eng._tile_chong_ju = lambda i, h, is_peak: eng.events.append(
        ("tile_cj", i, h, is_peak))
    eng._xrym_current_window = lambda pid: 999     # 模拟重新枚举到的新句柄

    def _activate(ct):
        eng.events.append(("activate", ct))
        return True

    def _activate_xrym():
        eng.events.append(("activate_xrym",))
        eng.xrym_activate_calls += 1

    eng.activate_tile = _activate
    eng.activate_tile_xrym = _activate_xrym
    return eng


# ============================================================
# 用例
# ============================================================

def test_lei_selfcheck_activate() -> None:
    print("\n[1] 雷小伊日常：平铺全部 → activate_tile(LEI_XIAO_YI)")
    eng = _wire_engine(_make_engine())
    eng._daily_windows = [_StubGW(111), None, _StubGW(333)]
    eng._retile_lei_selfcheck([0, 1, 2])
    tiles = [e for e in eng.events if e[0] == "tile_lei"]
    check("平铺跳过 gw=None 且覆盖全部有效窗口",
          tiles == [("tile_lei", 0, 111), ("tile_lei", 2, 333)], str(tiles))
    check("末尾调 activate_tile(LEI_XIAO_YI)",
          eng.events[-1] == ("activate", ClientType.LEI_XIAO_YI), str(eng.events[-1]))
    check("置前在全部平铺之后",
          eng.events.index(("activate", ClientType.LEI_XIAO_YI))
          == len(eng.events) - 1 and len(tiles) == 2, str(eng.events))


def test_xrym_selfcheck_activate() -> None:
    print("\n[2] XryM：平铺用最新句柄 → activate_tile_xrym")
    eng = _wire_engine(_make_engine())
    eng._xrym_windows = [_StubGW(111, pid=222)]
    eng._retile_xrym_selfcheck([0])
    tiles = [e for e in eng.events if e[0] == "tile_xrym"]
    check("平铺用重新枚举的最新句柄 999（非陈旧 111）",
          tiles == [("tile_xrym", 0, 999)], str(tiles))
    check("末尾调 activate_tile_xrym（不走通用 activate_tile）",
          eng.xrym_activate_calls == 1
          and eng.events[-1] == ("activate_xrym",)
          and not any(e[0] == "activate" for e in eng.events), str(eng.events))


def test_peak_selfcheck_activate() -> None:
    print("\n[3] 重聚巅峰：平铺（is_peak=True）→ activate_tile(CHONG_JU_PEAK)")
    eng = _wire_engine(_make_engine())
    eng._peak_windows = [_StubGW(111), None]
    eng._retile_peak_selfcheck([0, 1])
    tiles = [e for e in eng.events if e[0] == "tile_cj"]
    check("平铺带 is_peak=True 且跳过 gw=None",
          tiles == [("tile_cj", 0, 111, True)], str(tiles))
    check("末尾调 activate_tile(CHONG_JU_PEAK)",
          eng.events[-1] == ("activate", ClientType.CHONG_JU_PEAK), str(eng.events[-1]))


def test_chongju_daily_selfcheck_activate() -> None:
    print("\n[4] 重聚日常：平铺（is_peak=False）→ activate_tile(CHONG_JU_DAILY)")
    eng = _wire_engine(_make_engine())
    eng._daily_windows = [None, _StubGW(222)]
    eng._retile_chongju_daily_selfcheck([0, 1])
    tiles = [e for e in eng.events if e[0] == "tile_cj"]
    check("平铺带 is_peak=False 且跳过 gw=None",
          tiles == [("tile_cj", 1, 222, False)], str(tiles))
    check("末尾调 activate_tile(CHONG_JU_DAILY)",
          eng.events[-1] == ("activate", ClientType.CHONG_JU_DAILY), str(eng.events[-1]))


def test_no_duplicate_activate_on_empty_active() -> None:
    print("\n[5] active 为空时也走一次置前（与旧版「一句日志」行为对齐）")
    eng = _wire_engine(_make_engine())
    eng._retile_lei_selfcheck([])
    check("无平铺但有且仅有一次 activate_tile(LEI_XIAO_YI)",
          eng.events == [("activate", ClientType.LEI_XIAO_YI)], str(eng.events))


def test_nickname_before_login_cleanup() -> None:
    print("\n[6] 昵称先出现、登录线程晚平铺：必须收尾后再重排")
    eng = _wire_engine(_make_engine())
    account = SimpleNamespace(display_name="", selected=True)
    eng._account_manager = SimpleNamespace(accounts=[account])
    eng._daily_windows = [None]
    eng._daily_sc_stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)

    class LoginFinished:
        done = False

        def is_set(self):
            return self.done

        def wait(self, timeout):
            eng.events.append(("login_final_tile",))
            self.done = True
            return True

    def launch(*args):
        gw = _StubGW()
        gw.login_finished = LoginFinished()
        eng._daily_windows[0] = gw
        return gw

    eng.stop_single = lambda *args: None
    eng._launch_one = launch
    eng._chongju_daily_refresh_display_name = lambda *args: setattr(account, "display_name", "昵称")
    with patch("seer_console.core.engine.time.sleep"):
        eng._chongju_daily_selfcheck("unused.exe", [0])
    check("登录收尾平铺先于最终批量平铺与激活",
          eng.events == [("login_final_tile",), ("tile_cj", 0, 111, False),
                         ("activate", ClientType.CHONG_JU_DAILY)], str(eng.events))


def test_foreground_denied_still_orders_windows() -> None:
    print("\n[7] 前台激活被拒绝时，仍显式按账号顺序调整 Z 序")
    eng = _make_engine()
    eng._daily_windows = [_StubGW(111), None, _StubGW(333)]
    with patch("seer_console.core.engine.WindowFinder.set_foreground_window", return_value=False), \
            patch("seer_console.core.engine.win32gui.SetWindowPos") as position, \
            patch("seer_console.core.engine.time.sleep"):
        eng.activate_tile(ClientType.CHONG_JU_DAILY)
    check("前台失败仍依次置前有效窗口", [c.args[0] for c in position.call_args_list] == [111, 333])
    check("只调整 Z 序，不移动、缩放或永久置顶",
          all(c.args[1:] == (0, 0, 0, 0, 0, 0x13) for c in position.call_args_list))


def test_pending_or_stopped_skips_retile() -> None:
    print("\n[8] 登录超时仍在执行或收到停止信号：暂缓重排")
    for method, peak in (("_retile_chongju_daily_selfcheck", False),
                         ("_retile_lei_selfcheck", False), ("_retile_peak_selfcheck", True)):
        for stopped in (False, True):
            eng = _wire_engine(_make_engine())
            gw = _StubGW()
            if stopped:
                (eng._peak_sc_stop if peak else eng._daily_sc_stop).set()
            else:
                gw.login_finished.clear()
            if peak:
                eng._peak_windows = [gw]
            else:
                eng._daily_windows = [gw]
            logs = []
            eng._log = logs.append
            getattr(eng, method)([0])
            check(f"{method} stopped={stopped} 不进行平铺或激活", not eng.events, str(eng.events))
            check(f"{method} stopped={stopped} 不误报重排完成",
                  not any("已重新排列" in msg for msg in logs), str(logs))


def test_peak_waits_for_cleanup_in_both_restart_paths() -> None:
    print("\n[10] 巅峰登录失败/卡死重启：昵称先出现时仍等登录收尾")
    for watchdog_restart in (False, True):
        eng = _wire_engine(_make_engine())
        account = SimpleNamespace(display_name="昵称", selected=True)
        eng._account_manager = SimpleNamespace(accounts=[account])
        eng._peak_sc_stop = SimpleNamespace(wait=lambda _: False, is_set=lambda: False)
        old_gw = _StubGW()
        if not watchdog_restart:
            old_gw.login_finished.clear()  # 有昵称但旧登录流程仍未完成，也应进入失败检查
        eng._peak_windows = [old_gw]

        class LoginFinished:
            done = False

            def is_set(self):
                return self.done

            def wait(self, timeout):
                eng.events.append(("login_cleanup",))
                self.done = True
                return True

        def launch(*args):
            gw = _StubGW()
            gw.login_finished = LoginFinished()
            eng._peak_windows[0] = gw
            return gw

        class Watchdog:
            def __init__(self, **kwargs):
                self.restart_round = kwargs["restart_round"]

            def run(self, active):
                if watchdog_restart:
                    self.restart_round(1, active)

        eng.stop_single = lambda *args: None
        eng._launch_one = launch
        eng._peak_refresh_display_name = lambda *args: None
        with patch("seer_console.core.engine.StallWatchdog", Watchdog), \
                patch("seer_console.core.engine.time.sleep"):
            eng._peak_selfcheck("unused.exe", [0])
        check(f"watchdog_restart={watchdog_restart} 收尾后才批量平铺和激活",
              eng.events == [("login_cleanup",), ("tile_cj", 0, 111, True),
                             ("activate", ClientType.CHONG_JU_PEAK)], str(eng.events))


def test_failed_activation_does_not_report_retile_success() -> None:
    print("\n[11] 巅峰/雷小伊排列失败：不误报重排完成")
    for method, peak in (("_retile_lei_selfcheck", False), ("_retile_peak_selfcheck", True)):
        eng = _wire_engine(_make_engine())
        if peak:
            eng._peak_windows = [_StubGW()]
        else:
            eng._daily_windows = [_StubGW()]
        logs = []
        eng._log = logs.append
        eng.activate_tile = lambda _: False
        getattr(eng, method)([0])
        check(f"{method} 失败不输出完成", not any("已重新排列" in msg for msg in logs), str(logs))


def test_invalid_window_does_not_abort_remaining() -> None:
    print("\n[9] 窗口失效：记录失败，继续排列剩余窗口")
    eng = _make_engine()
    eng._daily_windows = [_StubGW(111), _StubGW(333)]
    logs = []
    eng._log = logs.append
    with patch("seer_console.core.engine.WindowFinder.set_foreground_window", return_value=True), \
            patch("seer_console.core.engine.win32gui.SetWindowPos", side_effect=[RuntimeError("gone"), None]) as position, \
            patch("seer_console.core.engine.time.sleep"):
        result = eng.activate_tile(ClientType.CHONG_JU_DAILY)
    check("单窗失败仍处理后续窗口", [c.args[0] for c in position.call_args_list] == [111, 333])
    check("返回失败且日志可诊断", result is False and any("置前失败" in msg for msg in logs))


def main() -> None:
    test_lei_selfcheck_activate()
    test_xrym_selfcheck_activate()
    test_peak_selfcheck_activate()
    test_chongju_daily_selfcheck_activate()
    test_no_duplicate_activate_on_empty_active()
    test_nickname_before_login_cleanup()
    test_foreground_denied_still_orders_windows()
    test_pending_or_stopped_skips_retile()
    test_invalid_window_does_not_abort_remaining()
    test_peak_waits_for_cleanup_in_both_restart_paths()
    test_failed_activation_does_not_report_retile_success()
    print(f"\n===== 结果: {_passed} 通过 / {len(_failed)} 失败 =====")
    for f in _failed:
        print(f"  [FAIL] {f}")
    if _failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

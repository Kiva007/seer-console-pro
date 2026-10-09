# -*- coding: utf-8 -*-
"""重聚巅峰「运行选项」勾选配置单元测试。

背景（2026-10）：原实现把四个选项（不要胜利任务/做狂野任务/仅用第五/
不领取段位奖励）硬读 [重聚] 客户端节，配置页 UI 有 set/get 却从未接线
写回（保存配置按钮无连接、复选框无 toggled 连接），导致「配置页改勾选 →
单选启动巅峰不生效」。本文件锁定修复并覆盖每账号三档配置（与雷小伊
勾选同构：[巅峰勾选] / [巅峰勾选.新号] / [巅峰勾选.<米米号>]，
优先级：特定 > 新号 > 全局；全局档节缺失时从 [重聚] 旧键兜底迁移）。

覆盖：
   1. 默认全假 + [重聚] 旧键兜底 + set/save/reload 往返（GBK）
   2. 多 profile 读写 + 优先级（has/get_all + 特定>新号>全局 + reload + 节名落盘）
   3. engine._peak_run_config/_peak_run_config_for：真配置三档 / 桩配置默认
   4. TabSettings UI offscreen：四项 roundtrip + 下拉/切换缓存/flush 全档/新增档
   5. bug 回归：UI 勾选 → 「保存配置」flush 全档 → 写 config → reload → 按账号解析生效
      （模拟用户 900000006：取消「不要胜利任务」+勾「仅用第五」后单选启动巅峰）
   6. bug 回归（2026-10-05）：[窗口配置] 仅用第五=0 时 _match_peak_controls
      严禁把勾选框绑到窗口里真实存在的 id=0 子控件（Afx 渲染面/SysHeader32）
      ——曾致「仅用第五」勾选静默失效且无「未找到勾选框」日志；
      另锁定 ini 值 0/缺键时引擎走实测 ID 兜底（防陈旧进程把 ini 冲回旧值），
      树中无真控件时宁缺勿假，用户显式填的非零值优先

运行（无 pytest 依赖，同 test_lei_task_config.py 风格）：
    python seer_console/tests/test_peak_run_config.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.config import ConfigManager, PeakRunConfig


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


def _make_cfg(content: str = "") -> tuple[ConfigManager, str]:
    """临时 ctrl.ini（GBK）→ load 好的 ConfigManager"""
    path = os.path.join(tempfile.mkdtemp(), "ctrl.ini")
    if content:
        with open(path, "w", encoding="gbk") as f:
            f.write(content)
    cfg = ConfigManager(path)
    cfg.load()
    return cfg, path


# ============================================================
# 1. 默认值 + [重聚] 旧键兜底 + GBK 往返
# ============================================================

def test_defaults_and_roundtrip() -> None:
    print("\n[1] 默认值 / 旧键兜底 / 往返")
    cfg, path = _make_cfg()
    c = cfg.get_peak_run_profile("")
    check("1a 空配置 → 全假（不强制任何勾选）",
          c == PeakRunConfig(), f"got {c}")

    # 旧配置（[重聚] 节旧键，无 [巅峰勾选] 节）→ 全局档兜底读，行为零变化
    cfg_b, _ = _make_cfg(
        "[重聚]\n不要胜利任务 = 真\n做狂野任务 = 真\n仅用第五 = 假\n")
    c = cfg_b.get_peak_run_profile("")
    check("1b [重聚] 旧键兜底（无 [巅峰勾选] 节）",
          c.no_victory_quest and c.do_wild_quest
          and not c.only_fifth and not c.no_rank_reward,
          f"got {c}")

    # set → save → reload → get 往返
    custom = PeakRunConfig(no_victory_quest=True, only_fifth=True)
    cfg.set_peak_run_config(custom)
    cfg.save()
    cfg2 = ConfigManager(path)
    cfg2.load()
    check("1c set/save/reload 往返（GBK）",
          cfg2.get_peak_run_profile("") == custom,
          f"got {cfg2.get_peak_run_profile('')}")

    # [巅峰勾选] 节写入后不再走 [重聚] 兜底（全假即全假，不是旧键的真）
    cfg_c, _ = _make_cfg("[重聚]\n不要胜利任务 = 真\n")
    cfg_c.set_peak_run_config(PeakRunConfig())
    cfg_c.save()
    cfg_d = ConfigManager(cfg_c.file_path)
    cfg_d.load()
    check("1d 新节优先于 [重聚] 旧键",
          not cfg_d.get_peak_run_profile("").no_victory_quest)


# ============================================================
# 2. 多 profile 读写 + 优先级（特定 > 新号 > 全局）
# ============================================================

def test_multi_profile() -> None:
    print("\n[2] 多 profile 读写与优先级")
    cfg, path = _make_cfg(
        "[巅峰勾选]\n"
        "不要胜利任务 = 真\n"
        "仅用第五 = 假\n"
        "\n"
        "[巅峰勾选.新号]\n"
        "做狂野任务 = 真\n"
        "仅用第五 = 真\n"
        "\n"
        "[巅峰勾选.111]\n"
        "不领取段位奖励 = 真\n")

    check("2a has_peak_run_profile 判定",
          cfg.has_peak_run_profile("") and cfg.has_peak_run_profile("新号")
          and cfg.has_peak_run_profile("111")
          and not cfg.has_peak_run_profile("999"))

    profiles = cfg.get_all_peak_run_profiles()
    check("2b get_all 返回三档",
          set(profiles.keys()) == {"", "新号", "111"},
          f"got {sorted(profiles.keys())}")
    check("2c get_all 值正确",
          profiles[""].no_victory_quest
          and profiles["新号"].do_wild_quest
          and profiles["111"].no_rank_reward)

    check("2d 特定账号优先（新号也走专属）",
          cfg.get_peak_run_config_for(True, "111").no_rank_reward
          and not cfg.get_peak_run_config_for(True, "111").no_victory_quest
          and not cfg.get_peak_run_config_for(True, "111").only_fifth)
    check("2e 新号无专属 → 新号配置",
          cfg.get_peak_run_config_for(True, "222").do_wild_quest
          and cfg.get_peak_run_config_for(True, "222").only_fifth)
    check("2f 老号无专属 → 全局",
          cfg.get_peak_run_config_for(False, "222").no_victory_quest
          and not cfg.get_peak_run_config_for(False, "222").do_wild_quest)
    check("2g 空米米号 → 不按特定账号处理",
          cfg.get_peak_run_config_for(False, "").no_victory_quest)

    # 新账号节 round-trip
    new_acc = PeakRunConfig(only_fifth=True, no_rank_reward=True)
    cfg.set_peak_run_profile("222", new_acc)
    cfg.save()
    cfg2 = ConfigManager(path)
    cfg2.load()
    check("2h 新账号节 round-trip",
          cfg2.get_peak_run_profile("222") == new_acc)
    with open(path, "r", encoding="gbk") as f:
        raw = f.read()
    check("2i 节名落盘正确", "[巅峰勾选.222]" in raw)
    check("2j reload 后优先级保持",
          cfg2.get_peak_run_config_for(False, "222") == new_acc)

    # 老配置（无新号节）：新号回退全局，行为零变化
    cfg_b, _ = _make_cfg("[巅峰勾选]\n不要胜利任务 = 真\n")
    check("2k 无新号节 → 新号回退全局",
          cfg_b.get_peak_run_config_for(True, "222").no_victory_quest)


# ============================================================
# 3. engine wrapper（真配置三档 / 桩配置默认）
# ============================================================

class _FakeConfig:
    """最小 ConfigManager 桩（无 get_peak_run_profile*，模拟旧测试桩/外部注入）"""

    @staticmethod
    def get_all_window_config() -> dict:
        return {}

    @staticmethod
    def get_seal_names() -> dict:
        return {}


def test_engine_reads_config() -> None:
    print("\n[3] engine._peak_run_config(_for)：真配置 / 桩配置")
    from seer_console.core.account import AccountManager
    from seer_console.core.engine import AutomationEngine

    ledger = os.path.join(tempfile.mkdtemp(), "ledger.txt")
    with open(ledger, "w", encoding="gbk") as f:
        f.write("1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6\n")
    am = AccountManager(ledger)
    am.load()

    # 桩配置 → 默认（全假）
    engine = AutomationEngine(am, _FakeConfig(), None, None,
                              log_callback=lambda m: None)
    check("3a 桩配置 → 默认策略",
          engine._peak_run_config() == PeakRunConfig()
          and engine._peak_run_config_for(True, "111") == PeakRunConfig())

    cfg, path = _make_cfg(
        "[巅峰勾选]\n不要胜利任务 = 真\n"
        "[巅峰勾选.新号]\n仅用第五 = 真\n"
        "[巅峰勾选.111]\n做狂野任务 = 真\n")
    engine2 = AutomationEngine(am, cfg, None, None,
                               log_callback=lambda m: None)
    check("3b 全局默认",
          engine2._peak_run_config().no_victory_quest)
    check("3c 老号无专属 → 全局",
          engine2._peak_run_config_for(False, "999").no_victory_quest)
    check("3d 新号无专属 → 新号配置",
          engine2._peak_run_config_for(True, "222").only_fifth)
    check("3e 特定账号 > 新号 > 全局",
          engine2._peak_run_config_for(True, "111").do_wild_quest)

    # 改配置即时生效（无需重启中控台，引擎每次登录现读）
    cfg.set_peak_run_config(PeakRunConfig(no_rank_reward=True))
    check("3f 改配置即时生效",
          engine2._peak_run_config().no_rank_reward)


# ============================================================
# 4. TabSettings UI offscreen（roundtrip + 多 profile）
# ============================================================

def test_ui_roundtrip() -> None:
    print("\n[4] TabSettings UI roundtrip（offscreen）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("  [SKIP] PySide6 不可用，跳过 UI 测试")
        return
    app = QApplication.instance() or QApplication([])

    from seer_console.core.account import Account
    from seer_console.ui.tab_settings import TabSettings

    accounts = [
        Account(mi_hao="111", account_id="甲号"),
        Account(mi_hao="222", account_id="乙号"),
    ]
    tab = TabSettings()
    tab.set_peak_run_accounts(accounts)
    tab.set_peak_run_profiles({
        "": PeakRunConfig(no_victory_quest=True),
        "新号": PeakRunConfig(only_fifth=True),
        "111": PeakRunConfig(do_wild_quest=True),
    })

    check("4a 下拉三项（全局/新号/111）",
          tab.combo_peak_profile.count() == 3,
          f"got {tab.combo_peak_profile.count()}")
    check("4b 默认选中全局", tab.combo_peak_profile.currentData() == "")
    check("4c UI 载入全局（勾选反映）",
          tab.cb_no_victory.isChecked()
          and not tab.cb_only_fifth.isChecked()
          and not tab.cb_wild_quest.isChecked()
          and not tab.cb_no_rank_reward.isChecked())

    # 四项 roundtrip
    tab.cb_wild_quest.setChecked(True)
    tab.cb_no_rank_reward.setChecked(True)
    c = tab._ui_to_peak()
    check("4d 四项 roundtrip",
          c == PeakRunConfig(no_victory_quest=True,
                             do_wild_quest=True,
                             no_rank_reward=True),
          f"got {c}")
    # 还原全局档编辑（保存按钮才写全档；这里保持后续断言干净）
    tab.cb_wild_quest.setChecked(False)
    tab.cb_no_rank_reward.setChecked(False)

    # 切到账号 111 → 载入专属配置
    tab.combo_peak_profile.setCurrentIndex(2)
    check("4e 切到账号111 载入专属",
          tab.cb_wild_quest.isChecked() and not tab.cb_no_victory.isChecked())
    check("4f _current_peak_key 跟随", tab._current_peak_key == "111")

    # 编辑后保存 → dict 含全部档、编辑落对键、各档互不污染
    tab.cb_no_rank_reward.setChecked(True)
    tab.flush_all_caches()
    profiles = tab.get_peak_profiles()
    ok = (set(profiles.keys()) == {"", "新号", "111"}
          and profiles["111"].do_wild_quest
          and profiles["111"].no_rank_reward
          and profiles[""].no_victory_quest
          and not profiles[""].no_rank_reward
          and profiles["新号"].only_fifth)
    check("4g flush 全档、编辑落对键、不串档", ok)

    # 切回全局时 111 的编辑已被缓存，不丢失
    tab.combo_peak_profile.setCurrentIndex(0)
    check("4h 切回全局载入全局配置", tab.cb_no_victory.isChecked())
    tab.combo_peak_profile.setCurrentIndex(2)
    check("4i 再切111编辑仍在（切换时落缓存）",
          tab.cb_no_rank_reward.isChecked())

    # 新增账号配置（核心逻辑：建档/克隆全局/空与新号 key 跳过/已存在不覆盖）
    tab._add_peak_run_profile("222")
    tab._rebuild_peak_profile_combo()
    check("4j 新增后缓存与下拉含 222",
          "222" in tab._peak_profile_cache
          and tab.combo_peak_profile.count() == 4)
    check("4k 新档初值克隆全局",
          tab._peak_profile_cache["222"] == PeakRunConfig(no_victory_quest=True))
    tab._add_peak_run_profile("111")
    check("4l 已存在档不覆盖",
          tab._peak_profile_cache["111"].do_wild_quest)
    tab._add_peak_run_profile("")
    tab._add_peak_run_profile("新号")
    check("4m 空/新号 key 不入档",
          set(tab._peak_profile_cache.keys()) == {"", "新号", "111", "222"})

    tab.deleteLater()


# ============================================================
# 5. bug 回归：UI 勾选 → 信号 → 写 config → reload → 按账号生效
#    （用户 900000006：取消「不要胜利任务」+勾「仅用第五」，单选启动巅峰）
# ============================================================

def test_bugfix_ui_to_config_to_effect() -> None:
    print("\n[5] bug 回归：UI 勾选 → 写盘 → 引擎按账号读到")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("  [SKIP] PySide6 不可用，跳过 UI 测试")
        return
    app = QApplication.instance() or QApplication([])

    from seer_console.core.account import Account
    from seer_console.ui.tab_settings import TabSettings

    # 旧配置：[重聚] 节旧键「不要胜利任务=真」，[巅峰勾选] 节不存在
    # → 引擎原样会设「不要胜利任务」；用户在配置页取消它并勾「仅用第五」
    cfg, path = _make_cfg("[重聚]\n不要胜利任务 = 真\n")
    accounts = [Account(mi_hao="900000006", account_id="测试号")]
    tab = TabSettings()
    tab.set_peak_run_accounts(accounts)
    tab.set_peak_run_profiles(cfg.get_all_peak_run_profiles())

    # 用户操作一：取消「不要胜利任务」（只改 UI，不落缓存——只有统一「保存配置」才写）
    tab.cb_no_victory.setChecked(False)
    check("5a 取消勾选未保存前缓存不动",
          tab._peak_profile_cache[""].no_victory_quest is True)

    # 用户操作二：勾「仅用第五」（同上）
    tab.cb_only_fifth.setChecked(True)
    check("5b 勾选未保存前缓存不动",
          tab._peak_profile_cache[""].only_fifth is False)

    # 用户操作三：统一「保存配置」→ flush 全档（含前两步编辑值）
    tab.flush_all_caches()
    profiles = tab.get_peak_profiles()
    got = profiles.get("")
    check("5c flush 全档且含编辑值",
          got is not None
          and got.no_victory_quest is False and got.only_fifth is True
          and set(profiles.keys()) >= {""},
          f"got {got}")

    # 模拟 main_window._on_save_config_requested：逐档写 config + save
    for key, peak_cfg in profiles.items():
        cfg.set_peak_run_profile(key, peak_cfg)
    cfg.save()

    # reload 后按账号解析（900000006 无专属档 → 全局）
    cfg2 = ConfigManager(path)
    cfg2.load()
    eff = cfg2.get_peak_run_config_for(False, "900000006")
    check("5d 引擎侧生效：仅用第五=真、不要胜利=假",
          eff.only_fifth and not eff.no_victory_quest,
          f"got {eff}")

    # 单账号 900000006 建专属档后，专属覆盖全局
    eff2_cfg = ConfigManager(path)
    eff2_cfg.load()
    eff2_cfg.set_peak_run_profile(
        "900000006", PeakRunConfig(only_fifth=False, no_rank_reward=True))
    eff2_cfg.save()
    eff3 = ConfigManager(path)
    eff3.load()
    check("5e 专属档覆盖全局",
          eff3.get_peak_run_config_for(False, "900000006").no_rank_reward
          and not eff3.get_peak_run_config_for(False, "900000006").only_fifth)

    tab.deleteLater()


# ============================================================
# 6. bug 回归：[窗口配置] 仅用第五=0 误绑 id=0 假控件护栏 + 缺键兜底
#    现场一：ctrl.ini [窗口配置] 仅用第五 = 0（移植迁移时从未填过真实 ID
#    1560），而 2.8.6 窗口里真实存在 id=0 的子控件（Afx 渲染面/SysHeader32），
#    _match_peak_controls 拿 cid==0 匹配会把 handles[16] 绑到假控件上，
#    set_check 静默无效且不打「未找到勾选框」日志——用户勾「仅用第五」
#    启动后目标客户端永远不勾上。修复：config.save() 整份重写会把手改的
#    ini 冲回旧值（中控台运行期间手改无效），故双保险：ctrl.ini 值改
#    1560 + 引擎 _get_config 对 0/缺键走 _WINDOW_CONFIG_DEFAULTS 实测兜底
#    + 匹配循环对 cid==0 子控件一律跳过（宁缺勿假，让缺失打日志）。
# ============================================================

def test_match_peak_controls_cid0_guard() -> None:
    print("\n[6] bug 回归：仅用第五=0 → 兜底 1560，绝不误绑 id=0 假控件")
    from seer_console.core.account import AccountManager
    from seer_console.core import engine as engine_mod
    from seer_console.core.engine import AutomationEngine

    ledger = os.path.join(tempfile.mkdtemp(), "ledger.txt")
    with open(ledger, "w", encoding="gbk") as f:
        f.write("1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6\n")
    am = AccountManager(ledger)
    am.load()

    # 复刻现场（含被陈旧进程冲回后的形态）：各键正常、唯独 仅用第五 = 0
    cfg, _ = _make_cfg(
        "[窗口配置]\n"
        "巅峰账号id = 1001\n巅峰密码id = 230\n巅峰登录id = 250\n"
        "巅峰大选择夹id = 200\n巅峰小选择夹id = 520\n巅峰一键刷任务 = 1610\n"
        "巅峰商店列表 = 1330\n巅峰商店兑换 = 1100\n"
        "静态轮播 = 1920\n自动领取通行证奖励 = 2060\n刷完自动匹配 = 1660\n"
        "不要胜利任务 = 1930\n做狂野任务 = 1600\n狂野6v6 = 470\n"
        "仅用第五 = 0\n不领取段位奖励 = 2070\n"
        "巅峰商店蓝晶币 = 2140\n巅峰商店紫晶币 = 2130\n巅峰商店加载 = 1090\n"
        "巅峰输出框 = 850\n")

    # 假窗口树：混入两个 id=0 子控件（同 2.8.6 实测的 Afx/SysHeader32）
    fake_windows = {
        0xA00: 0,      # Afx 渲染面（id=0，缺兜底前会被误绑成 handles[16]）
        0xA01: 0,      # SysHeader32（id=0）
        0xA02: 2070,   # 不领取段位奖励
        0xA03: 1560,   # 仅用第五（真实勾选框，2.8.6 实测 ID）
    }
    orig_children = engine_mod.get_child_windows
    orig_ctlid = engine_mod.get_control_id
    engine_mod.get_child_windows = lambda hwnd: list(fake_windows.keys())
    engine_mod.get_control_id = lambda hwnd: fake_windows[hwnd]

    class _GW:  # 只需承载 output_handle 属性
        output_handle = 0

    try:
        # ini 里 仅用第五=0 → 引擎兜底按 1560 匹配 → 绑真实勾选框
        # （2026-10-05 现场：陈旧进程把 ini 冲回 0 后使用者重启仍失败，
        #  根治要靠此默认，不能只依赖手改 ini）
        eng = AutomationEngine(am, cfg, None, None,
                               log_callback=lambda m: None)
        handles = eng._match_peak_controls(0x999, _GW())
        check("6a ini 写 0 → 兜底默认 1560，handles[16] 绑真实勾选框",
              handles.get(16) == 0xA03, f"got {handles.get(16)}")
        check("6b 正常 ID 照常绑定（不领取段位 2070→17）",
              handles.get(17) == 0xA02, f"got {handles.get(17)}")

        # 缺真控件树（只有 id=0 假控件、没有 1560）→ 宁缺勿假不打日志失效
        fake_windows.pop(0xA03)          # 去掉 1560 真控件，只留 id=0/x
        handles2 = eng._match_peak_controls(0x999, _GW())
        check("6c 树中无 1560 真控件 → 绝不绑 id=0 假控件（handles[16] 缺）",
              16 not in handles2, f"got {handles2.get(16)}")
        fake_windows[0xA03] = 1560       # 还原供下一场景

        # 用户显式填非零值（如新版改了 ID）→ 以 ini 为准，不被默认覆盖
        cfg.set_window_config(54, 777)
        eng3 = AutomationEngine(am, cfg, None, None,
                                log_callback=lambda m: None)
        fake_windows[0xA04] = 777        # 新版假想控件 ID
        fake_windows.pop(0xA03)          # 旧默认值控件不在树中
        handles3 = eng3._match_peak_controls(0x999, _GW())
        check("6d 用户显式填 777 → 以 ini 为准绑 777（不被默认覆盖）",
              handles3.get(16) == 0xA04, f"got {handles3.get(16)}")
    finally:
        engine_mod.get_child_windows = orig_children
        engine_mod.get_control_id = orig_ctlid


# ============================================================
# 7. bug 回归（2026-10-05）：巅峰开始任务只点「一键刷任务」
#    现场：用户勾「做狂野任务(150)」启动后，引擎 set_check(18) 勾选成功，
#    却又切 6V6 页点了「狂野6V6」按钮（旧逻辑遗留——早期没有做狂野任务
#    勾选框，只能手动点 6V6）。现语义：勾选即向客户端声明任务类型，
#    开始只点「一键刷任务」与其他选项一致；_peak_start_task 内不再有
#    do_wild_quest 分支，也绝不碰 handles[15] 狂野6v6。
# ============================================================

def test_peak_start_task_only_one_click() -> None:
    print("\n[7] bug 回归：巅峰开始任务只点一键刷任务、不碰狂野6v6")
    from seer_console.core.account import AccountManager
    from seer_console.core import engine as engine_mod
    from seer_console.core.engine import AutomationEngine

    ledger = os.path.join(tempfile.mkdtemp(), "ledger.txt")
    with open(ledger, "w", encoding="gbk") as f:
        f.write("1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6\n")
    am = AccountManager(ledger)
    am.load()
    cfg, _ = _make_cfg("[窗口配置]\n巅峰一键刷任务 = 1610\n")

    logs = []
    eng = AutomationEngine(am, cfg, None, None, log_callback=logs.append)

    clicks, tabs = [], []
    orig_click, orig_switch = engine_mod.real_click_control, engine_mod.switch_tab
    engine_mod.real_click_control = lambda h: clicks.append(h)
    engine_mod.switch_tab = lambda h, idx: tabs.append(idx)

    try:
        # handles[15] 狂野6v6 与 handles[6] 一键刷任务同时在场：
        # 只点一键刷任务（此前 run_cfg.do_wild_quest=True 时会改道点 6V6）
        eng._peak_start_task({6: 0x1610, 15: 0x470}, 0x520, 2)
        check("7a 只点一键刷任务", clicks == [0x1610], f"got {clicks}")
        check("7b 绝不碰狂野6v6按钮", 0x470 not in clicks, f"got {clicks}")
        check("7c 切到巅峰任务页(index 0)", tabs == [0], f"got {tabs}")

        # handles[6] 缺失 → 不点击、打「开始按钮缺失」日志
        clicks.clear()
        tabs.clear()
        logs.clear()
        eng._peak_start_task({15: 0x470}, 0x520, 4)
        check("7d 缺一键刷任务时不点击且打日志",
              clicks == [] and any("开始按钮缺失" in m for m in logs),
              f"clicks={clicks} logs={logs}")
    finally:
        engine_mod.real_click_control = orig_click
        engine_mod.switch_tab = orig_switch


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    test_defaults_and_roundtrip()
    test_multi_profile()
    test_engine_reads_config()
    test_ui_roundtrip()
    test_bugfix_ui_to_config_to_effect()
    test_match_peak_controls_cid0_guard()
    test_peak_start_task_only_one_click()

    print(f"\n===== 结果: {_passed} 通过, {len(_failed)} 失败 =====")
    if _failed:
        for f in _failed:
            print(f"  FAIL: {f}")
        sys.exit(1)

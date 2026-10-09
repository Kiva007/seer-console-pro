# -*- coding: utf-8 -*-
"""雷小伊任务勾选策略单元测试：2026-09 从 engine 硬编码常量迁移至
ConfigManager 可配置；2026-10 扩展为每账号独立配置（[雷小伊勾选.<米米号>]
特定账号 / [雷小伊勾选.新号] 新号，优先级：特定 > 新号 > 全局）。

覆盖：
  1. 默认策略（空配置/无该节）= 迁移前硬编码值，老配置零变化
  2. set → save → reload → get GBK 往返（多行关键字）
  3. 键存在但非法（负数/非数字）→ 回退默认；0 合法（=不勾）
  4. 键存在但值为空 → 空策略（=清空该项，如 Unity 不排除任何项）
  5. 只写部分键 → 其余键默认
  6. engine._lei_task_config：真配置读到自定义值；桩配置（无该方法）→ 默认
  7. TabSettings UI roundtrip（offscreen）：set → get 所见即所得
  8. 多 profile 读写 + 优先级（get/set/has/get_all + 特定>新号>全局）
  9. engine._lei_task_config_for：按账号解析；桩配置 → 默认
  10. TabSettings UI 多 profile（offscreen）：档切换 / 保存 dict / 新增账号配置

运行（无 pytest 依赖，同 test_new_account_init.py 风格）：
    python seer_console/tests/test_lei_task_config.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.config import (
    ConfigManager, LeiTaskConfig,
    LEI_TASK_DEFAULT_UNITY_EXCLUDE, LEI_TASK_DEFAULT_FLASH_CHECKS,
)


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
# 1-2. 默认值 + GBK 往返
# ============================================================

def test_defaults_and_roundtrip() -> None:
    print("\n[1] 默认策略 = 迁移前硬编码值（空配置）")
    cfg, path = _make_cfg()
    c = cfg.get_lei_task_config()
    check("1a Unity 默认排除", c.unity_exclude == ("月华", "日常-宿命王者的锻炼"),
          f"got {c.unity_exclude}")
    check("1b Flash 默认16项",
          c.flash_checks == LEI_TASK_DEFAULT_FLASH_CHECKS
          and len(c.flash_checks) == 16)
    check("1c 因子顺序勾选默认2", c.factor_count == 2)

    print("\n[2] set → save → reload → get 往返（GBK + 多行关键字）")
    custom = LeiTaskConfig(
        unity_exclude=("王者", "甲", "乙丙丁"),
        flash_checks=("签到",),
        factor_count=3,
    )
    cfg.set_lei_task_config(custom)
    cfg.save()
    cfg2 = ConfigManager(path)
    cfg2.load()
    c2 = cfg2.get_lei_task_config()
    check("2a Unity 排除往返", c2.unity_exclude == ("王者", "甲", "乙丙丁"),
          f"got {c2.unity_exclude}")
    check("2b Flash 往返", c2.flash_checks == ("签到",))
    check("2c 因子顺序勾选往返3", c2.factor_count == 3)

    # 默认值原样落盘后再读 = 默认（等价性）
    cfg3 = ConfigManager(path)
    cfg3.load()
    cfg3.set_lei_task_config(LeiTaskConfig())
    cfg3.save()
    cfg4 = ConfigManager(path)
    cfg4.load()
    c4 = cfg4.get_lei_task_config()
    check("2e 默认值落盘往返等价",
          c4 == LeiTaskConfig(
              unity_exclude=LEI_TASK_DEFAULT_UNITY_EXCLUDE,
              flash_checks=LEI_TASK_DEFAULT_FLASH_CHECKS,
              factor_count=2))


# ============================================================
# 3-5. 非法值回退 / 空值语义 / 部分键
# ============================================================

def test_invalid_and_partial() -> None:
    print("\n[3] 非法值回退默认；0 合法")
    cfg, _ = _make_cfg(
        "[雷小伊勾选]\n"
        "Unity排除项 = 月华\n"
        "Flash勾选项 = 刻印抽奖\n"
        "因子顺序勾选项 = -3\n"
        "因子新号勾数 = abc\n")
    c = cfg.get_lei_task_config()
    check("3a 负数回退默认2", c.factor_count == 2, f"got {c.factor_count}")
    check("3c 正常键不受影响", c.unity_exclude == ("月华",)
          and c.flash_checks == ("刻印抽奖",))

    cfg_b, _ = _make_cfg("[雷小伊勾选]\n因子顺序勾选项 = 0\n")
    check("3d 0 合法（=不勾）", cfg_b.get_lei_task_config().factor_count == 0)

    # 旧键名兼容：2026-10 整合前的 ctrl.ini（Flash新号勾选项/因子常规勾数）
    # 应直接读进新字段；废弃的因子新号勾数不再生效
    cfg_old, _ = _make_cfg(
        "[雷小伊勾选]\n"
        "Flash新号勾选项 = 旧键A\n"
        "因子常规勾数 = 7\n"
        "因子新号勾数 = 8\n")
    c_old = cfg_old.get_lei_task_config()
    check("3e 旧键名→新字段（迁移兼容）",
          c_old.flash_checks == ("旧键A",) and c_old.factor_count == 7)

    print("\n[4] 键存在但值为空 → 空策略（清空该项）")
    cfg2, _ = _make_cfg(
        "[雷小伊勾选]\n"
        "Unity排除项 = \n"
        "Flash勾选项 =\n")
    c2 = cfg2.get_lei_task_config()
    check("4a Unity 排除项空 = 不排除任何项", c2.unity_exclude == ())
    check("4b Flash 空 = 不修改 Flash 页", c2.flash_checks == ())
    check("4c 未写的键仍默认", c2.factor_count == 2)

    print("\n[5] 只有空白行的值 = 空策略")
    # 多行值在 ini 里为缩进续行（configparser 语义，set() 落盘自动缩进）
    cfg3, _ = _make_cfg("[雷小伊勾选]\nUnity排除项 =\n\t \n\t月华 \n\n")
    c3 = cfg3.get_lei_task_config()
    check("5a 空白行剔除、保留有效行（首尾空格也剔除）",
          c3.unity_exclude == ("月华",), f"got {c3.unity_exclude}")

    print("\n[6] get_client_sections 不含新节")
    cfg4, _ = _make_cfg(
        "[雷小伊勾选]\nUnity排除项 = 月华\n"
        "[刻印名称]\n1 = 巅\n")
    sections = cfg4.get_client_sections()
    check("6a 雷小伊勾选/刻印名称 不进客户端节",
          "雷小伊勾选" not in sections and "刻印名称" not in sections,
          f"got {sections}")


# ============================================================
# 6. engine._lei_task_config（真配置 / 桩配置）
# ============================================================

class _FakeConfig:
    """最小 ConfigManager 桩（无 get_lei_task_config，模拟旧测试桩/外部注入）"""

    @staticmethod
    def get_all_window_config() -> dict:
        return {}

    @staticmethod
    def get_seal_names() -> dict:
        return {}


def test_engine_reads_config() -> None:
    print("\n[7] engine._lei_task_config：真配置 / 桩配置")
    from seer_console.core.account import AccountManager
    from seer_console.core.engine import AutomationEngine

    ledger = os.path.join(tempfile.mkdtemp(), "ledger.txt")
    with open(ledger, "w", encoding="gbk") as f:
        f.write("1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6\n")
    am = AccountManager(ledger)
    am.load()

    # 桩配置（无该方法）→ 默认策略
    engine = AutomationEngine(am, _FakeConfig(), None, None,
                              log_callback=lambda m: None)
    c = engine._lei_task_config()
    check("7a 桩配置 → 默认策略", c == LeiTaskConfig())

    # 真配置 → 自定义值（改配置不重启，下次调用即读到新值）
    cfg, path = _make_cfg(
        "[雷小伊勾选]\nUnity排除项 = 王者\n因子顺序勾选项 = 5\n")
    engine2 = AutomationEngine(am, cfg, None, None,
                               log_callback=lambda m: None)
    c2 = engine2._lei_task_config()
    check("7b 真配置 → 自定义值",
          c2.unity_exclude == ("王者",) and c2.factor_count == 5)
    # 同一 engine 实例，改配置后再读 → 立即生效
    cfg.set_lei_task_config(LeiTaskConfig(unity_exclude=("新关键字",)))
    c3 = engine2._lei_task_config()
    check("7c 改配置即时生效（无需重启）", c3.unity_exclude == ("新关键字",))


# ============================================================
# 7. TabSettings UI roundtrip（offscreen）
# ============================================================

def test_ui_roundtrip() -> None:
    print("\n[8] TabSettings UI set/get roundtrip（offscreen）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("  [SKIP] PySide6 不可用，跳过 UI 测试")
        return
    app = QApplication.instance() or QApplication([])

    from seer_console.ui.tab_settings import TabSettings
    tab = TabSettings()

    # 默认值填充 → 取回等价
    tab.set_lei_task_config(LeiTaskConfig())
    check("8a 默认填充 roundtrip", tab.get_lei_task_config() == LeiTaskConfig())

    # 自定义值
    custom = LeiTaskConfig(
        unity_exclude=("王者", "月华"),
        flash_checks=("签到+宝箱", "刻印抽奖", "VIP礼包"),
        factor_count=4,
    )
    tab.set_lei_task_config(custom)
    check("8b 自定义 roundtrip", tab.get_lei_task_config() == custom)

    # 清空文本框 = 空策略（所见即所得，允许「全选后不排除」）
    tab.txt_lei_unity_exclude.setPlainText("")
    tab.txt_lei_flash.setPlainText("\n  \n")
    c = tab.get_lei_task_config()
    check("8c 清空 = 空策略（空白行忽略）",
          c.unity_exclude == () and c.flash_checks == ()
          and c.factor_count == 4)

    # flush 全档缓存（统一「保存配置」入口）：含全局默认键""，值为当前 UI 收集
    tab.flush_all_caches()
    profiles = tab.get_lei_profiles()
    check("8d flush 携带 profile dict（含全局键）",
          profiles and "" in profiles
          and profiles[""] == tab.get_lei_task_config())

    tab.deleteLater()


# ============================================================
# 8. 多 profile 读写 + 优先级（特定 > 新号 > 全局）
# ============================================================

def test_multi_profile() -> None:
    print("\n[9] 多 profile 读写与优先级（特定 > 新号 > 全局）")
    cfg, path = _make_cfg(
        "[雷小伊勾选]\n"
        "Unity排除项 = 全局排除\n"
        "因子顺序勾选项 = 2\n"
        "\n"
        "[雷小伊勾选.新号]\n"
        "Unity排除项 = 新号排除\n"
        "因子顺序勾选项 = 8\n"
        "\n"
        "[雷小伊勾选.111]\n"
        "Unity排除项 = 账号111排除\n"
        "因子顺序勾选项 = 3\n")

    check("9a has_lei_task_profile 判定",
          cfg.has_lei_task_profile("") and cfg.has_lei_task_profile("新号")
          and cfg.has_lei_task_profile("111")
          and not cfg.has_lei_task_profile("999"))

    profiles = cfg.get_all_lei_task_profiles()
    check("9b get_all 返回三档", set(profiles.keys()) == {"", "新号", "111"},
          f"got {sorted(profiles.keys())}")
    check("9c get_all 值正确",
          profiles["111"].unity_exclude == ("账号111排除",)
          and profiles["新号"].unity_exclude == ("新号排除",))

    check("9d 特定账号优先（新号也走专属）",
          cfg.get_lei_task_config_for(True, "111").unity_exclude == ("账号111排除",))
    check("9e 新号无专属 → 新号配置",
          cfg.get_lei_task_config_for(True, "222").unity_exclude == ("新号排除",))
    check("9f 老号无专属 → 全局",
          cfg.get_lei_task_config_for(False, "222").unity_exclude == ("全局排除",))
    check("9g 空米米号 → 不按特定账号处理",
          cfg.get_lei_task_config_for(False, "").unity_exclude == ("全局排除",))

    # 新账号节 round-trip（GBK + 多行关键字）
    new_acc = LeiTaskConfig(unity_exclude=("甲", "乙"),
                            flash_checks=("刻印抽奖",),
                            factor_count=5)
    cfg.set_lei_task_profile("222", new_acc)
    cfg.save()
    cfg2 = ConfigManager(path)
    cfg2.load()
    check("9h 新账号节 round-trip", cfg2.get_lei_task_profile("222") == new_acc)
    with open(path, "r", encoding="gbk") as f:
        raw = f.read()
    check("9i 节名落盘正确", "[雷小伊勾选.222]" in raw)
    check("9j reload 后优先级保持",
          cfg2.get_lei_task_config_for(False, "222").unity_exclude == ("甲", "乙"))

    # 老配置（无新号节）：新号回退全局，行为零变化
    cfg_b, _ = _make_cfg("[雷小伊勾选]\nUnity排除项 = 全局排除\n")
    check("9k 无新号节 → 新号回退全局",
          cfg_b.get_lei_task_config_for(True, "222").unity_exclude == ("全局排除",))


# ============================================================
# 9. engine._lei_task_config_for（按账号解析 / 桩配置）
# ============================================================

def test_engine_per_account() -> None:
    print("\n[10] engine._lei_task_config_for：按账号优先级解析")
    from seer_console.core.account import AccountManager
    from seer_console.core.engine import AutomationEngine

    ledger = os.path.join(tempfile.mkdtemp(), "ledger.txt")
    with open(ledger, "w", encoding="gbk") as f:
        f.write("1|*|111|pwd|未启动|0||1|2026年12月30日||星海|0|0|5|6\n")
    am = AccountManager(ledger)
    am.load()

    cfg, path = _make_cfg(
        "[雷小伊勾选]\nUnity排除项 = 全局排除\n因子顺序勾选项 = 2\n"
        "[雷小伊勾选.新号]\nUnity排除项 = 新号排除\n因子顺序勾选项 = 8\n"
        "[雷小伊勾选.111]\nUnity排除项 = 账号111排除\n因子顺序勾选项 = 3\n")
    engine = AutomationEngine(am, cfg, None, None,
                              log_callback=lambda m: None)

    c = engine._lei_task_config_for(False, "999")
    check("10a 老号无专属 → 全局",
          c.unity_exclude == ("全局排除",) and c.factor_count == 2)
    c = engine._lei_task_config_for(True, "111")
    check("10b 特定账号 > 新号 > 全局",
          c.unity_exclude == ("账号111排除",) and c.factor_count == 3)
    c = engine._lei_task_config_for(True, "222")
    check("10c 新号无专属 → 新号配置",
          c.unity_exclude == ("新号排除",) and c.factor_count == 8)

    engine2 = AutomationEngine(am, _FakeConfig(), None, None,
                               log_callback=lambda m: None)
    check("10d 桩配置 → 默认策略",
          engine2._lei_task_config_for(True, "111") == LeiTaskConfig())


# ============================================================
# 10. TabSettings UI 多 profile（offscreen）
# ============================================================

def test_ui_multi_profile() -> None:
    print("\n[11] TabSettings UI 多 profile（offscreen）")
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
    tab.set_lei_task_accounts(accounts)
    tab.set_lei_task_profiles({
        "": LeiTaskConfig(unity_exclude=("全局排除",)),
        "新号": LeiTaskConfig(unity_exclude=("新号排除",), factor_count=8),
        "111": LeiTaskConfig(unity_exclude=("账号111排除",), factor_count=3),
    })

    check("11a 下拉三项（全局/新号/111）",
          tab.combo_lei_profile.count() == 3,
          f"got {tab.combo_lei_profile.count()}")
    check("11b 默认选中全局", tab.combo_lei_profile.currentData() == "")
    check("11c UI 载入全局",
          tab.txt_lei_unity_exclude.toPlainText() == "全局排除")

    # 切到账号 111 → 载入专属配置
    tab.combo_lei_profile.setCurrentIndex(2)
    check("11d 切到账号111 载入专属",
          tab.txt_lei_unity_exclude.toPlainText() == "账号111排除")
    check("11e 因子勾数=3", tab.spin_lei_factor.value() == 3)

    # 编辑后保存 → dict 含全部档、编辑落对键、各档互不污染
    tab.txt_lei_unity_exclude.setPlainText("账号111改后")
    tab.flush_all_caches()
    profiles = tab.get_lei_profiles()
    ok = (set(profiles.keys()) == {"", "新号", "111"}
          and profiles["111"].unity_exclude == ("账号111改后",)
          and profiles[""].unity_exclude == ("全局排除",)
          and profiles["新号"].unity_exclude == ("新号排除",))
    check("11f flush 全档、编辑落对键、不串档", ok)

    # 切回全局时 111 的编辑已被缓存，不丢失
    tab.combo_lei_profile.setCurrentIndex(0)
    check("11g 切回全局载入全局配置",
          tab.txt_lei_unity_exclude.toPlainText() == "全局排除")
    tab.combo_lei_profile.setCurrentIndex(2)
    check("11h 再切111编辑仍在（切换时落缓存）",
          tab.txt_lei_unity_exclude.toPlainText() == "账号111改后")

    # 新增账号配置（核心逻辑）
    tab._add_lei_task_profile("222")
    tab._rebuild_profile_combo()
    check("11i 新增后缓存与下拉含 222",
          "222" in tab._profile_cfg_cache
          and tab.combo_lei_profile.count() == 4)
    check("11j 新档初值克隆全局",
          tab._profile_cfg_cache["222"].unity_exclude == ("全局排除",))
    check("11k 已存在档不覆盖",
          (tab._add_lei_task_profile("111"),
           tab._profile_cfg_cache["111"].unity_exclude == ("账号111改后",))[1])
    # 空/新号 key 不入档
    tab._add_lei_task_profile("")
    tab._add_lei_task_profile("新号")
    check("11l 空/新号 key 不入档",
          set(tab._profile_cfg_cache.keys()) == {"", "新号", "111", "222"})

    tab.deleteLater()


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    test_defaults_and_roundtrip()
    test_invalid_and_partial()
    test_engine_reads_config()
    test_ui_roundtrip()
    test_multi_profile()
    test_engine_per_account()
    test_ui_multi_profile()

    print(f"\n===== 结果: {_passed} 通过, {len(_failed)} 失败 =====")
    if _failed:
        for f in _failed:
            print(f"  FAIL: {f}")
        sys.exit(1)

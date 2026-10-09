# -*- coding: utf-8 -*-
"""新账号初始化（雷小伊）单元测试：标注门闩 + Flash 关键字匹配 + 完成移除标注。

覆盖需求（2026-09 用户需求）：
  1. 新账号判定：标注「新」或「新；xxx」（全角分号，闲鱼发货侧统一写入格式）
  2. 完成后移除标注：「新」→空、「新；时长待核对」→「时长待核对」（去"新"和分号，
     保留后续文字），幂等
  3. Flash 页 15 项勾选关键字：15 个截图任务名全命中；7 个不勾项全不命中
  4. 因子勾选行数常量：常规 2 / 新账号 8
  5. _clear_new_annotation：写盘 GBK 往返 + 幂等 + 越界/非新账号安全

运行（无 pytest 依赖，同 test_stall_watchdog.py 风格）：
    python seer_console/tests/test_new_account_init.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.account import Account, AccountManager
from seer_console.core.engine import AutomationEngine
from seer_console.core.config import LeiTaskConfig


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
# 1. Account.is_new_account / clear_new_tag 标注组合矩阵
# ============================================================

def test_annotation_matrix() -> None:
    print("\n[1] 标注组合矩阵（is_new_account / clear_new_tag）")

    new_cases = ["新", "新；时长待核对", "新；服务待核对", "新；"]
    for ann in new_cases:
        acc = Account(annotation=ann)
        check(f"is_new_account({ann!r})", acc.is_new_account())

    # 半角分号是手工误写格式，闲鱼侧只会写全角——判定为非新（与写入侧约定一致）
    not_new_cases = ["", "因子", "无", "小号", "新手", "新;", "续费"]
    for ann in not_new_cases:
        acc = Account(annotation=ann)
        check(f"not is_new_account({ann!r})", not acc.is_new_account())

    # 病态输入文档化行为：「新；；双分号」以「新；」开头 → 判为新账号
    # （前缀匹配语义；闲鱼侧只会写「新」/「新；note」，此格式仅手工误写会出现）
    acc = Account(annotation="新；；双分号")
    check("is_new_account 前缀匹配（病态输入文档化）", acc.is_new_account())

    # clear_new_tag：转换 + 幂等
    transitions = [
        ("新", ""),
        ("新；时长待核对", "时长待核对"),
        ("新；服务待核对", "服务待核对"),
        ("新；", ""),
    ]
    for old, rest in transitions:
        acc = Account(annotation=old)
        changed = acc.clear_new_tag()
        check(f"clear_new_tag({old!r})→{rest!r}", changed and acc.annotation == rest,
              f"got changed={changed}, annotation={acc.annotation!r}")
        again = acc.clear_new_tag()
        check(f"clear_new_tag 幂等({rest!r})", not again and acc.annotation == rest)

    for ann in ["", "因子", "无", "小号"]:
        acc = Account(annotation=ann)
        check(f"clear_new_tag 非新账号不动({ann!r})",
              not acc.clear_new_tag() and acc.annotation == ann)


# ============================================================
# 2. AccountManager GBK 文件往返
# ============================================================

def test_ledger_roundtrip() -> None:
    print("\n[2] 账本 GBK 往返（写→读→移除→写→读）")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="gbk")
    tmp.write("1|*|900000019|pwd|未启动|0||1|2026年12月30日|新；时长待核对|星海|0|0|5|6\n")
    tmp.close()
    try:
        am = AccountManager(tmp.name)
        am.load()
        acc = am.accounts[0]
        check("账本加载: 米米号", acc.mi_hao == "900000019")
        check("账本加载: is_new_account", acc.is_new_account())

        assert acc.clear_new_tag()
        am.save()

        am2 = AccountManager(tmp.name)
        am2.load()
        acc2 = am2.accounts[0]
        check("移除后重读: 标注=时长待核对", acc2.annotation == "时长待核对",
              f"got {acc2.annotation!r}")
        check("移除后重读: 不再是新账号", not acc2.is_new_account())
        check("移除后重读: 其他字段不丢（米米号/到期日）",
              acc2.mi_hao == "900000019" and acc2.daily_expiry == "2026年12月30日")
    finally:
        os.unlink(tmp.name)


# ============================================================
# 3. Flash 新账号勾选关键字匹配
# ============================================================

# 截图确认要勾选的 16 项（完整任务名，来自用户 Flash 页截图，2026-08-31 补「各种签到+宝箱」）
FLASH_SHOULD_CHECK = [
    "各种签到+宝箱", "刻印抽奖", "VIP礼包", "荣誉令牌(技能石)", "※借绿火", "泰坦源脉",
    "太空站功勋任务", "钻石月卡", "剧情-FLASH", "[默认自爆弹伤]新精灵觉醒",
    "每日5个全面药(圣谐)", "战队贡献+任务+兑换", "星愿礼盒/许愿", "星光密藏",
    "周年珍藏馆", "通行证任务领取",
]

# 截图确认不勾选的 7 项
FLASH_SHOULD_NOT_CHECK = [
    "六界帝神翻牌", "机盖点灯+三件套", "圣甲卡修斯第四关", "堕落泰坦第四关",
    "猛虎王&幽灵猛虎王", "擎空凡尔斯", "每日2个全面药",
]


def test_flash_keywords() -> None:
    print("\n[3] Flash 关键字匹配（16 勾选项 / 7 不勾项；策略默认值）")

    # 默认策略完整性（2026-09 起常量迁至 config.LeiTaskConfig，值不变；
    # 2026-10 整合后 Flash 所有账号通用、因子仅单一「顺序勾选项」）
    default = LeiTaskConfig()
    check("因子默认: 顺序勾选=2", default.factor_count == 2)
    check("Unity 默认排除2项",
          default.unity_exclude == ("月华", "日常-宿命王者的锻炼"))
    check("Flash 默认关键字共16个", len(default.flash_checks) == 16,
          f"got {len(default.flash_checks)}")

    for name in FLASH_SHOULD_CHECK:
        check(f"勾选: {name}",
              any(kw in name for kw in default.flash_checks))

    for name in FLASH_SHOULD_NOT_CHECK:
        check(f"不勾: {name}",
              not any(kw in name for kw in default.flash_checks))

    # 关键字子串匹配方向：任务名包含关键字即命中（应对列表里带前后缀的变体）
    check("变体命中: （活动）VIP礼包·豪华",
          any(kw in "（活动）VIP礼包·豪华" for kw in default.flash_checks))
    check("空名不命中", not any(kw in "" for kw in default.flash_checks))


# ============================================================
# 4. AutomationEngine._clear_new_annotation（写盘 + 幂等 + 安全）
# ============================================================

class _FakeConfig:
    """构造 AutomationEngine 所需的最小 ConfigManager 桩。"""

    @staticmethod
    def get_all_window_config() -> dict:
        return {}

    @staticmethod
    def get_seal_names() -> dict:
        return {}


def _make_engine(ledger_lines: list[str], logs: list[str]) -> tuple[AutomationEngine, str]:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="gbk")
    tmp.write("\n".join(ledger_lines) + "\n")
    tmp.close()
    am = AccountManager(tmp.name)
    am.load()
    engine = AutomationEngine(am, _FakeConfig(), None, None,
                              log_callback=logs.append)
    return engine, tmp.name


def test_clear_new_annotation() -> None:
    print("\n[4] _clear_new_annotation（完成时移除标注）")

    # 4a. 标注「新」→ 完成后清空并写盘
    logs: list[str] = []
    engine, path = _make_engine([
        "1|*|900000019|pwd|未启动|0||1|2026年12月30日|新|星海|0|0|5|6",
        "1|*|111|pwd|未启动|0||1|2026年12月30日|因子|星海|0|0|5|6",
    ], logs)
    try:
        engine._clear_new_annotation(0)
        am2 = AccountManager(path)
        am2.load()
        check("4a 移除「新」: 标注清空", am2.accounts[0].annotation == "",
              f"got {am2.accounts[0].annotation!r}")
        check("4a 日志含移除提示", any("移除新账号标注" in m for m in logs),
              f"logs={logs}")

        # 4b. 幂等：再调一次不写盘、不重复记日志
        n_logs = len(logs)
        engine._clear_new_annotation(0)
        check("4b 幂等: 不再产生日志", len(logs) == n_logs)

        # 4c. 非新账号不动
        engine._clear_new_annotation(1)
        am3 = AccountManager(path)
        am3.load()
        check("4c 非新账号「因子」保持不变", am3.accounts[1].annotation == "因子")

        # 4d. 越界索引安全
        try:
            engine._clear_new_annotation(99)
            engine._clear_new_annotation(-1)
            check("4d 越界索引不抛异常", True)
        except Exception as e:
            check("4d 越界索引不抛异常", False, repr(e))
    finally:
        os.unlink(path)

    # 4e. 「新；时长待核对」→「时长待核对」
    logs2: list[str] = []
    engine2, path2 = _make_engine([
        "1|*|222|pwd|未启动|0||1|2026年12月30日|新；时长待核对|星海|0|0|5|6",
    ], logs2)
    try:
        engine2._clear_new_annotation(0)
        am4 = AccountManager(path2)
        am4.load()
        check("4e 「新；时长待核对」→「时长待核对」",
              am4.accounts[0].annotation == "时长待核对",
              f"got {am4.accounts[0].annotation!r}")
    finally:
        os.unlink(path2)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    test_annotation_matrix()
    test_ledger_roundtrip()
    test_flash_keywords()
    test_clear_new_annotation()

    print(f"\n===== 结果: {_passed} 通过, {len(_failed)} 失败 =====")
    if _failed:
        for f in _failed:
            print(f"  FAIL: {f}")
        sys.exit(1)

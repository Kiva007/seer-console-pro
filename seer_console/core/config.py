"""配置管理：读取/写入 ctrl.ini（GBK 编码）"""

import os
import configparser
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 配置键常量映射
# ============================================================

# 窗口配置项的索引 → 键名映射（ctrl.ini [窗口配置] 节 1–60，与文件实际键名/顺序一致）
WINDOW_CONFIG_KEYS = {
    1: "工作屏幕高",
    2: "工作屏幕宽",
    3: "巅峰窗口宽",
    4: "巅峰窗口高",
    5: "巅峰账号id",
    6: "巅峰密码id",
    7: "巅峰登录id",
    8: "巅峰大选择夹id",
    9: "巅峰小选择夹id",
    10: "巅峰一键刷任务",
    11: "巅峰蓝晶币",
    12: "巅峰紫晶币",
    13: "巅峰商店页",
    14: "巅峰商店兑换",
    15: "日常窗口宽",
    16: "日常窗口高",
    17: "17日常上下距",
    18: "18日常左右距",
    19: "日常账号id",
    20: "日常密码id",
    21: "日常登录id",
    22: "日常切换默认套装",
    23: "日常确认切换套装",
    24: "日常开始按钮",
    25: "日常套装列表",
    26: "日常账号名",
    27: "27日常列数",
    28: "28日常行数",
    29: "29巅峰列数",
    30: "30巅峰行数",
    31: "巅峰更新按钮",
    32: "巅峰商店列表",
    33: "33巅峰上下距",
    34: "34巅峰左右距",
    35: "静态轮播",
    36: "自动领取通行证奖励",
    37: "刷完自动匹配",
    38: "不要胜利任务",
    39: "做狂野任务",
    40: "狂野6v6",
    41: "巅α(攻速1)",
    42: "绝α(特速2)",
    43: "锋α(爆攻3)",
    44: "舞α(爆特4)",
    45: "渊α(攻体5)",
    46: "鸿α(特体6)",
    47: "界α(攻盾7)",
    48: "阵α(特盾8)",
    49: "究极α(无体9)",
    50: "无限α(无速10)",
    51: "巅峰输出框",
    52: "日常输出框",
    53: "中控置顶",
    54: "仅用第五",
    55: "不领取段位奖励",
    56: "重聚日常账号id",
    57: "重聚日常密码id",
    58: "重聚日常u区id",
    59: "重聚日常登录id",
    60: "重聚日常输出框",
    61: "巅峰商店蓝晶币",
    62: "巅峰商店紫晶币",
    63: "巅峰商店加载",
    64: "重聚日常上移",
}

# 反向映射：键名 → 索引
WINDOW_CONFIG_INDEX = {v: k for k, v in WINDOW_CONFIG_KEYS.items()}

# 客户端分类（配置页「属性」列下拉选项）
CLIENT_KINDS = ("登录器", "日常", "巅峰")

# ---- 雷小伊任务勾选默认策略（2026-09 从 engine 硬编码常量迁移，值不变保证行为零变化）----

# Unity 页全选后取消勾选的任务名关键字（子串匹配）
LEI_TASK_DEFAULT_UNITY_EXCLUDE = ("月华", "日常-宿命王者的锻炼")
# Flash 页要【勾选】的关键字（子串匹配，未匹配不动；所有账号通用，
# 新号差异由「新号配置」档承载）。留空=不修改 Flash 页（保持原样）
# 来源：用户截图确认的 16 项；关键词取足够特征子串并规避歧义
#（如"每日5个全面药"区分"每日2个全面药"；"剧情-FLASH"避免泛匹配"剧情"）
LEI_TASK_DEFAULT_FLASH_CHECKS = (
    "各种签到+宝箱", "刻印抽奖", "VIP礼包", "荣誉令牌", "借绿火", "泰坦源脉",
    "太空站功勋", "钻石月卡", "剧情-FLASH", "新精灵觉醒", "每日5个全面药",
    "战队贡献", "星愿礼盒", "星光密藏", "周年珍藏馆", "通行证任务",
)
# 因子页顺序勾选行数：所有账号统一勾前 N 项（0=不勾）
# （新号如需不同勾数，在「新号配置」档单独设置）
LEI_TASK_DEFAULT_FACTOR_CHECK_COUNT = 2


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ClientEntry:
    """客户端列表条目（配置页自定义，存储于 ctrl.ini [客户端列表] 节）

    name:    自定义名称
    path:    软件路径
    kind:    属性分类（登录器/日常/巅峰）
    is_daily:现行日常（主业务日常使用的软件，全局唯一）
    is_peak: 现行巅峰（主业务巅峰使用的软件，全局唯一）
    """
    name: str = ""
    path: str = ""
    kind: str = "登录器"
    is_daily: bool = False
    is_peak: bool = False

    def copy(self) -> "ClientEntry":
        return ClientEntry(
            name=self.name, path=self.path, kind=self.kind,
            is_daily=self.is_daily, is_peak=self.is_peak,
        )


@dataclass
class ClientConfig:
    """单个客户端配置"""
    name: str = ""  # 配置节名（CHIKA/雷小伊/重聚/TO/XryM）
    exe_paths: dict[str, str] = field(default_factory=dict)  # {目录1: path, 目录N: path}
    timed_start: bool = False  # 定时启动
    auto_restart: bool = False  # 自检重启
    start_hour: str = "03"  # 定时启动小时
    start_minute: str = "00"  # 定时启动分钟
    # 重聚特有运行选项
    no_victory_quest: bool = False
    do_wild_quest: bool = False
    only_fifth: bool = False
    no_rank_reward: bool = False


@dataclass
class LeiTaskConfig:
    """雷小伊日常任务勾选策略（配置页「雷小伊任务勾选」分组，存 ctrl.ini
    [雷小伊勾选] 节；每账号/新号另有 [雷小伊勾选.<key>] 子节）。

    unity_exclude: Unity 页全选后要【取消勾选】的任务名关键字（子串匹配）
    flash_checks:  Flash 页要【勾选】的关键字（子串匹配，未匹配不动）；
                  留空 = 不修改 Flash 页（保持原样，连标签都不切）
    factor_count:  因子页顺序勾选前 N 项（0=不勾）；所有账号一致，
                  新号如需不同勾数在「新号配置」档单独设
    """
    unity_exclude: tuple = LEI_TASK_DEFAULT_UNITY_EXCLUDE
    flash_checks: tuple = LEI_TASK_DEFAULT_FLASH_CHECKS
    factor_count: int = LEI_TASK_DEFAULT_FACTOR_CHECK_COUNT


@dataclass
class PeakRunConfig:
    """重聚巅峰「巅峰任务勾选」配置（配置页「巅峰任务勾选」分组，存 ctrl.ini
    [巅峰勾选] 节；每账号/新号另有 [巅峰勾选.<key>] 子节）。

    与雷小伊勾选同构的三档结构（优先级：特定账号 > 新号 > 全局默认）；
    但内容是勾选项（真/假），不是关键字列表。
    全局默认档节缺失时，从 [重聚] 客户端节旧键（不要胜利任务/做狂野任务/
    仅用第五/不领取段位奖励）兜底读取——2026-10 前的纯手改配置零迁移。
    """
    no_victory_quest: bool = False  # 不要胜利任务
    do_wild_quest: bool = False     # 做狂野任务
    only_fifth: bool = False        # 仅用第五
    no_rank_reward: bool = False    # 不领取段位奖励


# ============================================================
# 配置管理器
# ============================================================

class ConfigManager:
    """管理 ctrl.ini 配置文件"""

    def __init__(self, file_path: str):
        self._file_path = file_path
        self._config = configparser.ConfigParser()

    @property
    def file_path(self) -> str:
        return self._file_path

    def load(self) -> None:
        """加载配置文件（GBK编码）"""
        if os.path.isfile(self._file_path):
            # configparser 不原生支持指定编码读文件，手动处理
            with open(self._file_path, "r", encoding="gbk") as f:
                self._config.read_file(f)
        # 否则为空配置

    def save(self) -> None:
        """保存配置到文件（GBK编码）"""
        with open(self._file_path, "w", encoding="gbk") as f:
            self._config.write(f)

    # ---- 通用读写 ----

    def get(self, section: str, key: str, default: str = "") -> str:
        """读取字符串配置"""
        try:
            return self._config.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        """读取布尔配置（=真/假）"""
        val = self.get(section, key, str(default))
        return val == "真" or val.lower() == "true"

    def set(self, section: str, key: str, value: str) -> None:
        """写入字符串配置"""
        if not self._config.has_section(section):
            self._config.add_section(section)
        self._config.set(section, key, value)

    def set_bool(self, section: str, key: str, value: bool) -> None:
        """写入布尔配置（存为'真'/'假'）"""
        self.set(section, key, "真" if value else "假")

    # ---- 窗口配置（控件ID + 坐标） ----

    def get_window_config(self, index: int, default: int = 0) -> int:
        """根据索引读取窗口配置值"""
        key_name = WINDOW_CONFIG_KEYS.get(index, "")
        if not key_name:
            return default
        val = self.get("窗口配置", key_name, str(default))
        try:
            return int(val)
        except ValueError:
            return default

    def set_window_config(self, index: int, value: int) -> None:
        """写入窗口配置值"""
        key_name = WINDOW_CONFIG_KEYS.get(index)
        if key_name:
            self.set("窗口配置", key_name, str(value))

    def get_all_window_config(self) -> dict[int, int]:
        """读取所有窗口配置"""
        result = {}
        for idx, key_name in WINDOW_CONFIG_KEYS.items():
            val = self.get("窗口配置", key_name, "0")
            try:
                result[idx] = int(val)
            except ValueError:
                result[idx] = 0
        return result

    # ---- 客户端路径 ----

    def get_client_path(self, section: str, dir_key: str) -> str:
        """读取客户端exe路径，如 ConfigManager.get_client_path('雷小伊', '雷小伊目录7')"""
        return self.get(section, dir_key, "")

    def set_client_path(self, section: str, dir_key: str, path: str) -> None:
        """写入客户端exe路径"""
        self.set(section, dir_key, path)

    # ---- 微信推送 ----

    def get_wechat_token(self) -> str:
        """读取pushplus token"""
        return self.get("微信推送", "pushplus-token", "")

    def set_wechat_token(self, token: str) -> None:
        """写入pushplus token"""
        self.set("微信推送", "pushplus-token", token)

    # ---- 客户端配置（批量） ----

    def get_client_config(self, section: str) -> ClientConfig:
        """读取某个客户端节的完整配置"""
        cfg = ClientConfig(name=section)
        for key in self._config.options(section) if self._config.has_section(section) else []:
            val = self._config.get(section, key)
            if "目录" in key:
                cfg.exe_paths[key] = val
            elif key == "定时启动":
                cfg.timed_start = (val == "真")
            elif key == "自检重启":
                cfg.auto_restart = (val == "真")
            elif key == "时":
                cfg.start_hour = val
            elif key == "分":
                cfg.start_minute = val
            elif key == "不要胜利任务":
                cfg.no_victory_quest = (val == "真")
            elif key == "做狂野任务":
                cfg.do_wild_quest = (val == "真")
            elif key == "仅用第五":
                cfg.only_fifth = (val == "真")
            elif key == "不领取段位奖励":
                cfg.no_rank_reward = (val == "真")
        return cfg

    # ---- 客户端列表（配置页自定义条目） ----

    def get_client_entries(self) -> list[ClientEntry]:
        """读取 [客户端列表] 节；无该节时从旧「节/目录N」键迁移生成默认条目"""
        if not self._config.has_section("客户端列表"):
            entries = self._migrate_client_entries()
            self.set_client_entries(entries)
            return entries
        try:
            count = int(self.get("客户端列表", "数量", "0"))
        except ValueError:
            count = 0
        entries = []
        for i in range(1, count + 1):
            kind = self.get("客户端列表", f"{i}_类型", "登录器")
            entries.append(ClientEntry(
                name=self.get("客户端列表", f"{i}_名称", ""),
                path=self.get("客户端列表", f"{i}_路径", ""),
                kind=kind if kind in CLIENT_KINDS else "登录器",
                is_daily=self.get_bool("客户端列表", f"{i}_现行日常", False),
                is_peak=self.get_bool("客户端列表", f"{i}_现行巅峰", False),
            ))
        return entries

    def set_client_entries(self, entries: list[ClientEntry]) -> None:
        """写入 [客户端列表] 节，并同步写回旧节键（现行日常→雷小伊目录7，现行巅峰→重聚目录11）"""
        if not self._config.has_section("客户端列表"):
            self._config.add_section("客户端列表")
        self._config.set("客户端列表", "数量", str(len(entries)))
        for i, e in enumerate(entries, start=1):
            self._config.set("客户端列表", f"{i}_名称", e.name)
            self._config.set("客户端列表", f"{i}_路径", e.path)
            self._config.set("客户端列表", f"{i}_类型", e.kind)
            self._config.set("客户端列表", f"{i}_现行日常", "真" if e.is_daily else "假")
            self._config.set("客户端列表", f"{i}_现行巅峰", "真" if e.is_peak else "假")
        # 清除超出列表长度的残留编号键：configparser 的 set 是增量操作，历史残留
        # （如旧的 8_/9_）会一直留在内存并随 save() 反复写回文件，造成
        # 「数量=N 却存在更大编号条目」的脏数据（YoSo/重聚巅峰 2.8.1 重复即此因）。
        for key in list(self._config.options("客户端列表")):
            head = key.split("_", 1)[0]
            if head.isdigit() and int(head) > len(entries):
                self._config.remove_option("客户端列表", key)
        # 同步旧节键（向后兼容：引擎/旧调用仍可读）。
        # 按条目名称分流（2026-09 修复：现行日常可能是重聚日常/XryM，
        # 一刀切写雷小伊目录7 会把重聚日常路径污染进雷小伊键）
        daily = next((e for e in entries if e.is_daily), None)
        if daily:
            name_lower = daily.name.lower()
            if "xrym" in name_lower:
                self.set_client_path("XryM", "xrym目录9", daily.path)
            elif "重聚" in daily.name:
                self.set_client_path("重聚", "重聚目录8", daily.path)
            else:
                self.set_client_path("雷小伊", "雷小伊目录7", daily.path)
        peak = next((e for e in entries if e.is_peak), None)
        if peak:
            self.set_client_path("重聚", "重聚目录11", peak.path)

    def get_current_daily_path(self) -> str:
        """现行日常客户端路径（主业务日常使用的软件）；无标记时回退旧键"""
        for e in self.get_client_entries():
            if e.is_daily and e.path:
                return e.path
        return self.get_client_path("雷小伊", "雷小伊目录7")

    def get_current_peak_path(self) -> str:
        """现行巅峰客户端路径（主业务巅峰使用的软件）；无标记时回退旧键"""
        for e in self.get_client_entries():
            if e.is_peak and e.path:
                return e.path
        return self.get_client_path("重聚", "重聚目录11")

    def get_xrym_daily_path(self) -> str:
        """XryM 日常客户端路径（[客户端列表] 中 kind=日常 且名称含 xrym 的条目，回退 XryM目录9）"""
        for e in self.get_client_entries():
            if e.kind == "日常" and "xrym" in e.name.lower() and e.path:
                return e.path
        return self.get_client_path("XryM", "XryM目录9")

    def get_chongju_daily_path(self) -> str:
        """重聚日常客户端路径（[客户端列表] 中 kind=日常 且名称含「重聚」的条目，
        回退 [重聚] 重聚目录8）。注意与 get_current_daily_path 不同：后者返回
        现行日常（雷小伊），本方法专指重聚日常客户端。"""
        for e in self.get_client_entries():
            if e.kind == "日常" and "重聚" in e.name and e.path:
                return e.path
        return self.get_client_path("重聚", "重聚目录8")

    def is_xrym_daily(self) -> bool:
        """现行日常是否为 XryM 客户端（[客户端列表] 现行日常条目名称含 xrym）"""
        for e in self.get_client_entries():
            if e.is_daily and e.path:
                return "xrym" in e.name.lower()
        return False

    def is_chongju_daily(self) -> bool:
        """现行日常是否为 重聚日常 客户端（[客户端列表] 现行日常条目名称含「重聚」）"""
        for e in self.get_client_entries():
            if e.is_daily and e.path:
                return "重聚" in e.name or "chongju" in e.name.lower()
        return False

    def get_xrym_control(self, key: str, default: str = "") -> str:
        """XryM 客户端 UIA 控件 AutomationId（[XryM] 节 xrym*id 键）"""
        return self.get("XryM", key, default)

    def get_xrym_auto_update(self) -> bool:
        """XM 启动自动更新开关（[XryM] 节 xrym启动更新 = 真/假，默认假=弹窗时点「继续使用该版本」直接运行；
        真=点「本地下载」自动更新→下载→关闭旧进程→用新版本重新启动→继续登录）"""
        return self.get_bool("XryM", "xrym启动更新", False)

    def get_lei_auto_update(self) -> bool:
        """雷小伊启动自动更新开关（[雷小伊] 节 雷小伊启动更新 = 真/假）。
        默认真：保持自动更新行为（纯 HTTP 下载新版→写回配置→换进程）；
        假=检测到新版弹窗点「取消」用当前版本继续登录。仅批量启动生效。"""
        return self.get_bool("雷小伊", "雷小伊启动更新", True)

    def get_chongju_daily_auto_update(self) -> bool:
        """重聚日常启动自动更新开关（[重聚] 节 重聚日常启动更新 = 真/假）。
        默认真：保持自动更新行为（点「立即更新」→清孤儿→重扫目录写回→同账号重试）；
        假=弹窗点「取消」用当前版本继续登录。仅批量启动生效。"""
        return self.get_bool("重聚", "重聚日常启动更新", True)

    def get_peak_auto_update(self) -> bool:
        """巅峰启动自动更新开关（[重聚] 节 巅峰启动更新 = 真/假）。
        默认真：保持自动更新行为（同重聚日常）；假=弹窗点「取消」用当前版本继续登录。
        仅批量启动生效，单号/自检不触发。"""
        return self.get_bool("重聚", "巅峰启动更新", True)

    # ---- 雷小伊任务勾选策略（[雷小伊勾选] 节，+ 每账号/新号子节）----

    @staticmethod
    def _lei_section_name(key: str) -> str:
        """key → ctrl.ini 节名：""=全局="雷小伊勾选"；其他="雷小伊勾选.<key>" """
        if not key:
            return "雷小伊勾选"
        return f"雷小伊勾选.{key}"

    def _get_keyword_lines(self, key, fallback: tuple,
                           section: str = "雷小伊勾选") -> tuple:
        """读多行关键字键：key 可为 str，或按优先级排列的 tuple
        （新键名在前、旧键名在后，兼容 2026-10 整合前的旧配置）。
        首个存在的键（哪怕值为空）→ 用键值（空=清空该项策略，
        如 Unity 排除项清空=全选后不取消任何任务）；都不存在 → 回退默认。
        行首尾空白剔除、空行忽略。"""
        keys = (key,) if isinstance(key, str) else tuple(key)
        for k in keys:
            if self._config.has_option(section, k):
                return tuple(v.strip() for v in
                             self.get(section, k).splitlines() if v.strip())
        return fallback

    def _get_int_key(self, key, fallback: int,
                     section: str = "雷小伊勾选") -> int:
        """读整数键：key 可为 str 或优先级 tuple（新键名优先，兼容旧配置）。
        键不存在或非数字/负数 → 回退默认；0 合法（=不勾）"""
        keys = (key,) if isinstance(key, str) else tuple(key)
        for k in keys:
            if not self._config.has_option(section, k):
                continue
            try:
                n = int(self.get(section, k))
            except ValueError:
                return fallback
            return n if n >= 0 else fallback
        return fallback

    def _read_lei_config(self, section: str) -> LeiTaskConfig:
        """从指定节读出 LeiTaskConfig（节/键缺失 → 回退默认常量）。
        兼容 2026-10 整合前的旧键名：Flash新号勾选项→Flash勾选项、
        因子常规勾数→因子顺序勾选项（旧"因子新号勾数"废弃，
        新号差异由「新号配置」档承载）"""
        return LeiTaskConfig(
            unity_exclude=self._get_keyword_lines(
                "Unity排除项", LEI_TASK_DEFAULT_UNITY_EXCLUDE, section=section),
            flash_checks=self._get_keyword_lines(
                ("Flash勾选项", "Flash新号勾选项"),
                LEI_TASK_DEFAULT_FLASH_CHECKS, section=section),
            factor_count=self._get_int_key(
                ("因子顺序勾选项", "因子常规勾数"),
                LEI_TASK_DEFAULT_FACTOR_CHECK_COUNT, section=section),
        )

    def _write_lei_config(self, section: str, cfg: LeiTaskConfig) -> None:
        """写入 LeiTaskConfig 到指定节（只写新键名；旧键名读取兼容）"""
        self.set(section, "Unity排除项", "\n".join(cfg.unity_exclude))
        self.set(section, "Flash勾选项", "\n".join(cfg.flash_checks))
        self.set(section, "因子顺序勾选项", str(cfg.factor_count))

    # ---- 全局默认（向后兼容）----

    def get_lei_task_config(self) -> LeiTaskConfig:
        """读取全局默认 [雷小伊勾选] 节"""
        return self._read_lei_config(self._lei_section_name(""))

    def set_lei_task_config(self, cfg: LeiTaskConfig) -> None:
        """写入全局默认 [雷小伊勾选] 节"""
        self._write_lei_config(self._lei_section_name(""), cfg)

    # ---- 多 profile 读写（每账号/新号 = 独立子节）----

    def get_lei_task_profile(self, key: str) -> LeiTaskConfig:
        """按 key 读取配置：""=全局,"新号"=新号初始化, 米米号=特定账号。
        节不存在时回退全局默认值。"""
        return self._read_lei_config(self._lei_section_name(key))

    def set_lei_task_profile(self, key: str, cfg: LeiTaskConfig) -> None:
        """按 key 写入配置（不存在节则自动创建）"""
        self._write_lei_config(self._lei_section_name(key), cfg)

    def has_lei_task_profile(self, key: str) -> bool:
        """指定 key 的节是否在 ini 中存在（用于引擎判断"有专属配置"vs"需回退"）"""
        return self._config.has_section(self._lei_section_name(key))

    def get_all_lei_task_profiles(self) -> "dict[str, LeiTaskConfig]":
        """返回所有已存在的配置档：{key: LeiTaskConfig}，全局("")必定在内。
        新号("新号")与各米米号只在节存在时才出现。"""
        profiles: dict[str, LeiTaskConfig] = {}
        # 全局总是存在（至少回退默认值）
        profiles[""] = self._read_lei_config(self._lei_section_name(""))
        prefix = "雷小伊勾选."
        for section in self._config.sections():
            if section.startswith(prefix):
                key = section[len(prefix):]
                if key:  # 排除恰好以 prefix 开头但无后缀的歧义
                    profiles[key] = self._read_lei_config(section)
        return profiles

    def get_lei_task_config_for(self, is_new: bool, mi_hao: str) -> LeiTaskConfig:
        """按优先级解析当前账号应使用的勾选策略：
        1. 米米号有专属节 → 用专属配置
        2. 新账号（标注"新"）且有新号节 → 用新号配置
        3. 否则 → 全局默认
        """
        # 1. 特定账号
        if mi_hao and self.has_lei_task_profile(mi_hao):
            return self.get_lei_task_profile(mi_hao)
        # 2. 新号配置
        if is_new and self.has_lei_task_profile("新号"):
            return self.get_lei_task_profile("新号")
        # 3. 全局默认
        return self.get_lei_task_config()

    # ---- 重聚巅峰运行选项（[巅峰勾选] 节，+ 每账号/新号子节）----
    # 键名与 [重聚] 客户端节旧键一致（不要胜利任务/做狂野任务/仅用第五/
    # 不领取段位奖励），值 真/假；全局档节缺失时从 [重聚] 旧键兜底。

    @staticmethod
    def _peak_section_name(key: str) -> str:
        """key → ctrl.ini 节名：""=全局="巅峰勾选"；其他="巅峰勾选.<key>" """
        if not key:
            return "巅峰勾选"
        return f"巅峰勾选.{key}"

    def _read_peak_run_config(self, section: str) -> PeakRunConfig:
        """从指定节读出 PeakRunConfig（节/键缺失 → 对应项假）"""
        return PeakRunConfig(
            no_victory_quest=self.get_bool(section, "不要胜利任务"),
            do_wild_quest=self.get_bool(section, "做狂野任务"),
            only_fifth=self.get_bool(section, "仅用第五"),
            no_rank_reward=self.get_bool(section, "不领取段位奖励"),
        )

    def _write_peak_run_config(self, section: str, cfg: PeakRunConfig) -> None:
        """写入 PeakRunConfig 到指定节（四项显式写真/假）"""
        self.set_bool(section, "不要胜利任务", cfg.no_victory_quest)
        self.set_bool(section, "做狂野任务", cfg.do_wild_quest)
        self.set_bool(section, "仅用第五", cfg.only_fifth)
        self.set_bool(section, "不领取段位奖励", cfg.no_rank_reward)

    def _peak_run_global_fallback(self) -> PeakRunConfig:
        """全局默认档兜底：[巅峰勾选] 节不存在 → 读 [重聚] 客户端节旧键
        （2026-10 前手改配置零迁移；两处都没有 → 全假）"""
        if self._config.has_section("巅峰勾选"):
            return self._read_peak_run_config("巅峰勾选")
        cc = self.get_client_config("重聚")
        return PeakRunConfig(
            no_victory_quest=cc.no_victory_quest,
            do_wild_quest=cc.do_wild_quest,
            only_fifth=cc.only_fifth,
            no_rank_reward=cc.no_rank_reward,
        )

    # ---- 全局默认（向后兼容）----

    def get_peak_run_config(self) -> PeakRunConfig:
        """读取全局默认（[巅峰勾选] 节缺失 → [重聚] 旧键兜底）"""
        return self._peak_run_global_fallback()

    def set_peak_run_config(self, cfg: PeakRunConfig) -> None:
        """写入全局默认 [巅峰勾选] 节"""
        self._write_peak_run_config(self._peak_section_name(""), cfg)

    # ---- 多 profile 读写（每账号/新号 = 独立子节）----

    def get_peak_run_profile(self, key: str) -> PeakRunConfig:
        """按 key 读取配置：""=全局,"新号"=新号初始化, 米米号=特定账号"""
        if not key:
            return self.get_peak_run_config()
        return self._read_peak_run_config(self._peak_section_name(key))

    def set_peak_run_profile(self, key: str, cfg: PeakRunConfig) -> None:
        """按 key 写入配置（不存在节则自动创建）"""
        if not key:
            self.set_peak_run_config(cfg)
            return
        self._write_peak_run_config(self._peak_section_name(key), cfg)

    def has_peak_run_profile(self, key: str) -> bool:
        """指定 key 的节是否在 ini 中存在（全局档含 [重聚] 旧键兜底也算存在）"""
        if self._config.has_section(self._peak_section_name(key)):
            return True
        if not key:
            cc = self.get_client_config("重聚")
            return (cc.no_victory_quest or cc.do_wild_quest
                    or cc.only_fifth or cc.no_rank_reward)
        return False

    def get_all_peak_run_profiles(self) -> "dict[str, PeakRunConfig]":
        """返回所有已存在的配置档：{key: PeakRunConfig}，全局("")必定在内"""
        profiles: dict[str, PeakRunConfig] = {}
        profiles[""] = self.get_peak_run_config()
        prefix = "巅峰勾选."
        for section in self._config.sections():
            if section.startswith(prefix):
                key = section[len(prefix):]
                if key:  # 排除恰好以 prefix 开头但无后缀的歧义
                    profiles[key] = self._read_peak_run_config(section)
        return profiles

    def get_peak_run_config_for(self, is_new: bool, mi_hao: str) -> PeakRunConfig:
        """按优先级解析当前账号应使用的巅峰运行选项：
        1. 米米号有专属节 → 用专属配置
        2. 新账号（标注"新"）且有新号节 → 用新号配置
        3. 否则 → 全局默认（含 [重聚] 旧键兜底）
        """
        if mi_hao and self.has_peak_run_profile(mi_hao):
            return self.get_peak_run_profile(mi_hao)
        if is_new and self.has_peak_run_profile("新号"):
            return self.get_peak_run_profile("新号")
        return self.get_peak_run_config()

    # 巅峰刻印编号 → 名称（[刻印名称] 节 1-10；缺项回退默认表）。
    # 引擎刻印兑换（商店列表行名匹配）与仓库页刻印列显示共用此表
    DEFAULT_SEAL_NAMES: dict[int, str] = {
        1: "巅", 2: "绝", 3: "锋", 4: "舞", 5: "渊",
        6: "鸿", 7: "界", 8: "阵", 9: "究极", 10: "无限",
    }

    def get_seal_names(self) -> dict[int, str]:
        """读取 [刻印名称] 节 → {编号: 名称}；缺失编号回退内置默认表"""
        names: dict[int, str] = {}
        for no in range(1, 11):
            name = self.get("刻印名称", str(no), "").strip()
            names[no] = name or self.DEFAULT_SEAL_NAMES.get(no, "")
        return names

    def _migrate_client_entries(self) -> list[ClientEntry]:
        """从旧「节/目录N」键迁移出默认 9 条客户端列表（现行日常=雷小伊日常，现行巅峰=重聚巅峰）"""
        default_specs = [
            # (名称, 节, 目录键, 类型, 现行日常, 现行巅峰)
            ("CHIKA 登录器", "CHIKA", "CHIKA目录1", "登录器", False, False),
            ("雷小伊 登录器", "雷小伊", "雷小伊目录2", "登录器", False, False),
            ("重聚 登录器", "重聚", "重聚目录3", "登录器", False, False),
            ("TO 登录器", "TO", "TO目录4", "登录器", False, False),
            ("CHIKA 日常", "CHIKA", "CHIKA目录6", "日常", False, False),
            ("雷小伊 日常", "雷小伊", "雷小伊目录7", "日常", True, False),
            ("重聚 日常", "重聚", "重聚目录8", "日常", False, False),
            ("XryM 日常", "XryM", "XryM目录9", "日常", False, False),
            ("重聚 巅峰", "重聚", "重聚目录11", "巅峰", False, True),
        ]
        entries = []
        for name, section, key, kind, is_daily, is_peak in default_specs:
            entries.append(ClientEntry(
                name=name,
                path=self.get_client_path(section, key),
                kind=kind, is_daily=is_daily, is_peak=is_peak,
            ))
        return entries

    # ---- 获取所有客户端节名 ----

    def get_client_sections(self) -> list[str]:
        """获取所有客户端配置节名（排除窗口配置、微信推送、占位等非客户端节）"""
        exclude = {"窗口配置", "微信推送", "占位", "客户端列表",
                   "雷小伊勾选", "巅峰勾选", "刻印名称", "DEFAULT"}
        return [s for s in self._config.sections() if s not in exclude]

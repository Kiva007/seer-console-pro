"""账号数据模型 + 文件读写（账号文本.txt，GBK编码，管道符分隔）

字段映射:
  [0]选中 [1]账号名(*) [2]账号(米米号) [3]密码 [4]当前状态
  [5]巅峰开关 [6]巅峰到期 [7]日常开关 [8]日常到期
  [9]标注 [10]平台名 [11]蓝晶币 [12]紫晶币 [13]刻印Y1 [14]刻印Y2
  [15]闲鱼id（闲鱼买家id，追加在行尾，兼容旧15字段文件与闲鱼发货侧读写）
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Account:
    """单个账号数据（对应账号文本.txt一行15个字段）"""

    row_index: int = 0
    selected: bool = True        # [0] 选中状态
    account_id: str = ""          # [1] 账号名（*占位，登录后替换为 display_name）
    mi_hao: str = ""              # [2] 账号（米米号）
    password: str = ""            # [3] 密码
    status: str = ""              # [4] 当前状态（如"未启动"）
    peak_enabled: bool = False    # [5] 巅峰开关
    peak_expiry: str = ""         # [6] 巅峰到期日
    daily_enabled: bool = False   # [7] 日常开关
    daily_expiry: str = ""        # [8] 日常到期日
    annotation: str = ""          # [9] 标注（如"因子"）
    platform: str = ""            # [10] 平台名（如"星海"）
    blue_crystal: int = 0         # [11] 蓝晶币
    purple_crystal: int = 0       # [12] 紫晶币
    engrave_y1: int = 5           # [13] 刻印1编号（0=不兑换；1-10，名称表见 ctrl.ini [刻印名称] 节）
    engrave_y2: int = 6           # [14] 刻印2编号（0=不兑换；1-10，名称表见 ctrl.ini [刻印名称] 节）
    xianyu_id: str = ""           # [15] 闲鱼买家id（追加字段；平台名/标注对应用户昵称可查）
    # 运行时状态（不持久化）
    current_action: str = ""      # "运行日常"/"运行巅峰"等
    display_name: str = ""        # 登录后获取的用户名，显示在"账号名"列

    @property
    def account_name(self) -> str:
        """显示的账号名：登录后=display_name，否则=account_id"""
        return self.display_name or self.account_id

    def to_line(self) -> str:
        """序列化为管道符分隔的一行文本"""
        return (
            f"{1 if self.selected else 0}|"
            f"{self.account_id}|"
            f"{self.mi_hao}|"
            f"{self.password}|"
            f"{self.status}|"
            f"{1 if self.peak_enabled else 0}|"
            f"{self.peak_expiry}|"
            f"{1 if self.daily_enabled else 0}|"
            f"{self.daily_expiry}|"
            f"{self.annotation}|"
            f"{self.platform}|"
            f"{self.blue_crystal}|"
            f"{self.purple_crystal}|"
            f"{self.engrave_y1}|"
            f"{self.engrave_y2}|"
            f"{self.xianyu_id}"
        )

    def is_new_account(self) -> bool:
        """新账号判定：标注为「新」或以「新；」开头（如"新；时长待核对"）。

        闲鱼发货侧统一写入（seer_ledger.py）：新账号标注 = "新" 或 "新；<兜底标注>"。
        """
        return self.annotation == "新" or self.annotation.startswith("新；")

    def clear_new_tag(self) -> bool:
        """移除「新」标注前缀（新账号初始化完成的门闩）：
        "新"→""；"新；时长待核对"→"时长待核对"（去"新"和分号，保留后续待核对文字）。
        返回 True=有修改（调用方负责持久化）；False=非新账号或已移除（幂等）。
        """
        if not self.is_new_account():
            return False
        rest = self.annotation[1:]          # 去掉开头的"新"
        if rest.startswith("；"):
            rest = rest[1:]                 # 去掉分隔分号
        self.annotation = rest
        return True

    def is_active_peak(self) -> bool:
        if not self.peak_enabled:
            return False
        return self._days_remaining(self.peak_expiry) >= 0

    def is_active_daily(self) -> bool:
        if not self.daily_enabled:
            return False
        return self._days_remaining(self.daily_expiry) >= 0

    def is_active(self) -> bool:
        # 运行页显示由仓库页「选中」勾选框手动控制（保存运行后生效），不再看开关/到期日
        return self.selected

    def peak_days_remaining(self) -> int:
        return self._days_remaining(self.peak_expiry)

    def daily_days_remaining(self) -> int:
        return self._days_remaining(self.daily_expiry)

    @staticmethod
    def _days_remaining(expiry_str: str) -> int:
        if not expiry_str:
            return -1
        dt = Account._parse_chinese_date(expiry_str)
        if dt is None:
            return -1
        return (dt.date() - date.today()).days

    @staticmethod
    def _parse_chinese_date(date_str: str) -> Optional[datetime]:
        m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
        if not m:
            return None
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None


# ============================================================
# 账号文件管理
# ============================================================

class AccountManager:
    """管理账号文本.txt的读取、修改、写回"""

    def __init__(self, file_path: str):
        self._file_path = file_path
        self._accounts: list[Account] = []

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def accounts(self) -> list[Account]:
        return self._accounts

    def load(self) -> list[Account]:
        """从文件加载全部账号"""
        if not os.path.isfile(self._file_path):
            self._accounts = []
            return self._accounts

        with open(self._file_path, "r", encoding="gbk") as f:
            raw = f.read()

        accounts = []
        for i, line in enumerate(raw.strip().split("\n")):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 15:
                continue
            acc = Account(
                row_index=i,
                selected=(parts[0].strip() == "1"),
                account_id=parts[1].strip(),
                mi_hao=parts[2].strip(),
                password=parts[3].strip(),
                status=parts[4].strip(),
                peak_enabled=(parts[5].strip() == "1"),
                peak_expiry=parts[6].strip(),
                daily_enabled=(parts[7].strip() == "1"),
                daily_expiry=parts[8].strip(),
                annotation=parts[9].strip(),
                platform=parts[10].strip(),
                blue_crystal=int(parts[11].strip()) if parts[11].strip() else 0,
                purple_crystal=int(parts[12].strip()) if parts[12].strip() else 0,
                engrave_y1=int(parts[13].strip()) if parts[13].strip() else 0,
                engrave_y2=int(parts[14].strip()) if parts[14].strip() else 0,
                xianyu_id=parts[15].strip() if len(parts) > 15 else "",
            )
            accounts.append(acc)

        self._accounts = accounts
        return accounts

    def save(self) -> None:
        """写回全部账号到文件（覆盖）"""
        lines = [acc.to_line() for acc in self._accounts]
        with open(self._file_path, "w", encoding="gbk") as f:
            f.write("\n".join(lines) + "\n")

    def add_account(self, account: Account) -> None:
        self._accounts.append(account)

    def remove_account(self, row_index: int) -> None:
        if 0 <= row_index < len(self._accounts):
            self._accounts.pop(row_index)

    def get_active_accounts(self) -> list[Account]:
        return [a for a in self._accounts if a.is_active()]

    def get_peak_queue(self) -> list[int]:
        # 只运行「总勾选已选 + 巅峰开关开启」的账号；与运行页显示(selected)保持一致
        queue = []
        for i, acc in enumerate(self._accounts):
            if acc.selected and acc.peak_enabled:
                queue.append(i)
            else:
                queue.append(-1)
        return queue

    def get_daily_queue(self) -> list[int]:
        # 只运行「总勾选已选 + 日常开关开启」的账号；与运行页显示(selected)保持一致
        queue = []
        for i, acc in enumerate(self._accounts):
            if acc.selected and acc.daily_enabled:
                queue.append(i)
            else:
                queue.append(-1)
        return queue

    def check_expiring(self, days_threshold: int = 4) -> list[dict]:
        warnings = []
        for acc in self._accounts:
            if not acc.selected:
                continue
            if acc.peak_enabled:
                d = acc.peak_days_remaining()
                if 0 <= d < days_threshold:
                    warnings.append({"type": "巅峰", "account": acc, "days": d})
            if acc.daily_enabled:
                d = acc.daily_days_remaining()
                if 0 <= d < days_threshold:
                    warnings.append({"type": "日常", "account": acc, "days": d})
        return warnings

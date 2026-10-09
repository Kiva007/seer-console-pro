"""自定义控件：日志文本框、账号表格等"""

import logging
from datetime import datetime

from PySide6.QtWidgets import (
    QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QMenu, QWidget, QHBoxLayout, QVBoxLayout, QCheckBox, QPushButton,
    QComboBox, QLabel, QSpacerItem, QSizePolicy, QFrame,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QColor, QAction, QTextCursor


# ============================================================
# 日志输出控件
# ============================================================

class LogWidget(QTextEdit):
    """日志输出文本框，线程安全，格式：[MM-DD HH:MM:SS] 消息"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Microsoft YaHei", 9))
        self.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #cccccc;
                border: 1px solid #333;
                border-radius: 4px;
                padding: 4px;
            }
        """)
        self.document().setMaximumBlockCount(5000)  # 最多保留5000行

    def log(self, message: str) -> None:
        """添加一条日志 [MM-DD HH:MM:SS] message"""
        ts = datetime.now().strftime("%m-%d %H:%M:%S")
        self.append(f"[{ts}] {message}")
        # 自动滚动到底部
        self.moveCursor(QTextCursor.MoveOperation.End)


# ============================================================
# 表格失焦清除选中
# ============================================================

class FocusClearTable(QTableWidget):
    """点击表格区域外（表格失焦，而非窗口失焦）时清除选中高亮。

    注意：Qt 6 的 QSS `::item:selected:!focus` 组合不生效（实测），
    故用 focusOutEvent 清除选中；currentIndex 保留，按钮等逻辑不受影响。
    """

    def focusOutEvent(self, event) -> None:
        self.clearSelection()
        super().focusOutEvent(event)


# ============================================================
# 账号表格基类
# ============================================================

class AccountTableWidget(FocusClearTable):
    """账号表格基类，提供通用功能"""

    # 列宽倍率（列号 → 倍率，相对该列实际渲染宽度）；子类可按需覆盖
    WIDTH_FACTORS: dict[int, float] = {}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._width_factors = dict(self.WIDTH_FACTORS)
        self._orig_widths: dict[int, int] = {}  # 列号 → 首次布局时的实际宽度（“原宽”）

    def apply_column_width_factors(self) -> None:
        """把指定列宽设为「实际渲染宽度 × 倍率」。

        基准取该列首次布局完成（可见且有真实宽度）后的列宽，
        之后始终按同一原宽 × 倍率，避免刷新时重复缩放或采集到未布局值。
        """
        if not self._width_factors:
            return
        if not self.isVisible() or self.columnWidth(0) <= 0:
            return  # 尚未布局，等待 showEvent 再应用
        header = self.horizontalHeader()
        for col, factor in self._width_factors.items():
            if col < 0 or col >= self.columnCount():
                continue
            if col not in self._orig_widths:
                self._orig_widths[col] = max(self.columnWidth(col), 16)
            width = int(self._orig_widths[col] * factor)
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(col, max(width, 16))

    def showEvent(self, event):
        super().showEvent(event)
        # 首次显示、布局就绪后，以实际宽度为基准应用列宽倍率
        if self._width_factors:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.apply_column_width_factors)

    def setup_columns(self, headers: list[str], stretch_last: bool = True) -> None:
        """设置表头"""
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)

        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        # 让所有列自适应内容宽度
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # 启用水平滚动
        self.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)

        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(True)
        self.setStyleSheet("""
            QTableWidget {
                gridline-color: #ddd;
                font-size: 13px;
            }
            QTableWidget::item:selected {
                background-color: #0078d4;
                color: white;
            }
            QHeaderView::section {
                background-color: #f0f0f0;
                padding: 4px;
                border: 1px solid #ddd;
                font-weight: bold;
            }
        """)

    def set_row_data(self, row: int, data,
                     left_columns: set[int] | None = None) -> None:
        """设置一整行数据（支持 list 或 {列号: 文本}）；left_columns 列内容左对齐（表头保持居中）"""
        items = data.items() if isinstance(data, dict) else enumerate(data)
        for col, text in items:
            item = QTableWidgetItem(str(text))
            if left_columns and col in left_columns:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            else:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, col, item)


# ============================================================
# 活动账号表格（标签页①）
# ============================================================

class ActiveAccountTable(AccountTableWidget):
    """运行页账号表格：只读，密码密文，右键单号操控"""

    # 信号
    account_double_clicked = Signal(int)  # 行号
    right_clicked = Signal(int, QMenu)  # 行号, 菜单
    account_control_requested = Signal(int, str, str)  # (accounts下标, 操作, 客户端)

    HEADERS = [
        "序号", "账号名", "账号", "密码", "当前动作",
        "日常开关", "巅峰开关", "标注", "平台名", "蓝晶币", "紫晶币",
    ]

    # 账号名（账户名）列宽度 → ×2
    WIDTH_FACTORS = {1: 2.0}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_columns(self.HEADERS, stretch_last=True)
        self._row_accounts: list = []      # 行 → Account 对象
        self._row_indices: list[int] = []  # 行 → accounts 下标
        self.cellDoubleClicked.connect(self._on_double_click)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_row_accounts(self, accounts: list, indices: list[int]) -> None:
        """记录每行对应的 Account 对象与 accounts 下标（供右键单号操控）"""
        self._row_accounts = accounts
        self._row_indices = indices

    def _on_double_click(self, row: int, col: int) -> None:
        self.account_double_clicked.emit(row)

    def _on_context_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if row < 0:
            return
        menu = self.build_context_menu(row)
        if menu is None:
            return
        self.right_clicked.emit(row, menu)
        menu.exec(self.viewport().mapToGlobal(pos))

    def build_context_menu(self, row: int):
        """构建右键菜单（单号操控），供冒烟测试直接调用"""
        if not (0 <= row < len(self._row_accounts)):
            return None
        account = self._row_accounts[row]
        index = self._row_indices[row] if row < len(self._row_indices) else row
        menu = QMenu(self)

        # 日常（雷小伊）单号操控
        if account.daily_enabled:
            menu.addAction(
                "运行日常",
                lambda: self.account_control_requested.emit(index, "run", "日常"),
            )
            menu.addAction(
                "关闭日常",
                lambda: self.account_control_requested.emit(index, "stop", "日常"),
            )
            menu.addAction(
                "重启日常",
                lambda: self.account_control_requested.emit(index, "restart", "日常"),
            )
            menu.addSeparator()

        # 巅峰（重聚）单号操控
        if account.peak_enabled:
            menu.addAction(
                "运行巅峰",
                lambda: self.account_control_requested.emit(index, "run", "巅峰"),
            )
            menu.addAction(
                "关闭巅峰",
                lambda: self.account_control_requested.emit(index, "stop", "巅峰"),
            )
            menu.addAction(
                "重启巅峰",
                lambda: self.account_control_requested.emit(index, "restart", "巅峰"),
            )
            menu.addSeparator()

        # 复制账号 / 复制密码（明文，从 Account 取）
        copy_account = QAction("复制账号", menu)
        copy_account.triggered.connect(lambda: self._copy_text(account.mi_hao))
        menu.addAction(copy_account)
        copy_password = QAction("复制密码", menu)
        copy_password.triggered.connect(lambda: self._copy_text(account.password))
        menu.addAction(copy_password)
        return menu

    @staticmethod
    def _copy_text(text: str) -> None:
        """复制明文到剪贴板"""
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    def add_account_row(self, seq: int, account_name: str, account_id: str,
                         password: str, action: str, daily_switch: bool,
                         peak_switch: bool, annotation: str,
                         platform: str, blue: int, purple: int) -> None:
        """添加一行运行页账号（密码密文 ******）"""
        row = self.rowCount()
        self.insertRow(row)
        data = [
            str(seq),
            account_name,
            account_id,
            "******",  # 密码密文
            action,
            "✓" if daily_switch else "✗",
            "✓" if peak_switch else "✗",
            annotation,
            platform,
            str(blue),
            str(purple),
        ]
        # 账号/密码内容左对齐（表头保持居中）
        self.set_row_data(row, data, left_columns={2, 3})

    def update_row(self, row: int, account_name: str, action: str,
                   blue: int, purple: int) -> None:
        """更新行数据（仅更新可变字段）"""
        if row >= self.rowCount():
            return
        updates = {
            1: account_name,
            4: action,
            9: str(blue),
            10: str(purple),
        }
        for col, val in updates.items():
            if self.item(row, col):
                self.item(row, col).setText(val)


# ============================================================
# 全部账号表格（标签页②）
# ============================================================

class AllAccountTable(AccountTableWidget):
    """仓库页全部账号表格：密码明文，可编辑，开关列用复选框"""

    HEADERS = [
        "选中", "账号", "密码", "标注",
        "巅峰开关", "巅峰到期", "日常开关", "日常到期",
        "平台名", "闲鱼id", "刻印1", "刻印2",
    ]

    # 文本列 → Account 字段名（不含选中列，选中列为勾选框）
    TEXT_COLUMNS = {
        1: "mi_hao",
        2: "password",
        3: "annotation",
        5: "peak_expiry",
        7: "daily_expiry",
        8: "platform",
        9: "xianyu_id",
        10: "engrave_y1",
        11: "engrave_y2",
    }
    # 复选框开关列 → Account 字段名（0=选中，4=巅峰开关，6=日常开关）
    SWITCH_COLUMNS = {0: "selected", 4: "peak_enabled", 6: "daily_enabled"}

    # 信号
    switch_toggled = Signal(int, int, bool)  # (row, col, checked)
    cell_edited = Signal(int, int)           # (row, col) 文本单元格被编辑

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_columns(self.HEADERS, stretch_last=True)
        # 可编辑：双击 / 回车键
        self.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked |
            QTableWidget.EditTrigger.EditKeyPressed
        )
        self.cellChanged.connect(self._on_cell_changed)
        self._populating = False  # 批量填充期间抑制信号
        # 巅峰刻印编号 → 名称（由仓库页注入，来自 ctrl.ini [刻印名称] 节）
        self.seal_names: dict[int, str] = {}

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._populating or col in self.SWITCH_COLUMNS:
            return
        self.cell_edited.emit(row, col)

    def _emit_switch(self, row: int, col: int, state: int) -> None:
        if self._populating:
            return
        self.switch_toggled.emit(row, col, bool(state))

    def _seal_text(self, engrave_no: int) -> str:
        """刻印编号 → 显示文本：0=不兑换 → 空白；否则名称表名称（缺项回退编号）"""
        if not engrave_no:
            return ""
        return self.seal_names.get(engrave_no, str(engrave_no))

    def add_account_row(self, account: "Account") -> None:
        """添加一行完整账号（密码明文，开关列复选框；刻印列显示名称）"""
        from ..core.account import Account

        row = self.rowCount()
        self.insertRow(row)

        text_data = {
            1: account.mi_hao,
            2: account.password,  # 明文
            3: account.annotation,
            5: account.peak_expiry,
            7: account.daily_expiry,
            8: account.platform,
            9: account.xianyu_id,
            10: self._seal_text(account.engrave_y1),
            11: self._seal_text(account.engrave_y2),
        }
        # 账号/密码/日期内容左对齐（表头保持居中）
        self.set_row_data(row, text_data, left_columns={1, 2, 5, 7})

        # 开关列：QCheckBox 单元格控件（居中显示；0=选中，4=巅峰开关，6=日常开关）
        for col, attr in self.SWITCH_COLUMNS.items():
            cb = QCheckBox(self)
            cb.setChecked(getattr(account, attr))
            cb.stateChanged.connect(
                lambda state, r=row, c=col: self._emit_switch(r, c, state)
            )
            # 居中容器（QCheckBox 默认靠左）
            container = QWidget(self)
            lay = QHBoxLayout(container)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cb)
            self.setCellWidget(row, col, container)


# ============================================================
# 控制面板（标签页①右侧）
# ============================================================

class ControlPanel(QWidget):
    """自动化控制面板 + 定时设置"""

    # 信号
    daily_all_start = Signal(str)  # 客户端类型（雷小伊/重聚/XryM）
    daily_all_stop = Signal()
    daily_restart_selected = Signal()
    daily_activate_tile = Signal()
    peak_all_start = Signal()
    peak_all_stop = Signal()
    peak_restart_selected = Signal()
    peak_activate_tile = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        # ====== 日常控制 ======
        daily_group = QWidget()
        daily_layout = QHBoxLayout(daily_group)
        daily_layout.setContentsMargins(0, 0, 0, 0)

        daily_label = QLabel("日常:")
        daily_label.setStyleSheet("font-weight: bold;")
        daily_layout.addWidget(daily_label)

        # 客户端选择器
        self.daily_client_combo = QComboBox()
        self.daily_client_combo.addItems(["雷小伊", "重聚", "XryM"])
        self.daily_client_combo.setCurrentIndex(0)  # 默认雷小伊
        daily_layout.addWidget(self.daily_client_combo)

        # 按钮（紧凑）
        self.btn_daily_start = QPushButton("全部运行")
        self.btn_daily_start.setStyleSheet("background-color: #28a745; color: white;")
        self.btn_daily_start.setMaximumWidth(65)
        self.btn_daily_start.clicked.connect(self._on_daily_start)
        daily_layout.addWidget(self.btn_daily_start)

        self.btn_daily_stop = QPushButton("全部关闭")
        self.btn_daily_stop.setStyleSheet("background-color: #dc3545; color: white;")
        self.btn_daily_stop.setMaximumWidth(65)
        self.btn_daily_stop.clicked.connect(lambda: self.daily_all_stop.emit())
        daily_layout.addWidget(self.btn_daily_stop)

        self.btn_daily_restart = QPushButton("重启")
        self.btn_daily_restart.setMaximumWidth(45)
        self.btn_daily_restart.clicked.connect(lambda: self.daily_restart_selected.emit())
        daily_layout.addWidget(self.btn_daily_restart)

        self.btn_daily_tile = QPushButton("排列")
        self.btn_daily_tile.setMaximumWidth(45)
        self.btn_daily_tile.clicked.connect(lambda: self.daily_activate_tile.emit())
        daily_layout.addWidget(self.btn_daily_tile)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        daily_layout.addWidget(sep)

        # ====== 巅峰控制 ======
        peak_group = QWidget()
        peak_layout = QHBoxLayout(peak_group)
        peak_layout.setContentsMargins(0, 0, 0, 0)

        peak_label = QLabel("巅峰:")
        peak_label.setStyleSheet("font-weight: bold;")
        peak_layout.addWidget(peak_label)

        self.btn_peak_start = QPushButton("全部运行")
        self.btn_peak_start.setStyleSheet("background-color: #28a745; color: white;")
        self.btn_peak_start.setMaximumWidth(65)
        self.btn_peak_start.clicked.connect(lambda: self.peak_all_start.emit())
        peak_layout.addWidget(self.btn_peak_start)

        self.btn_peak_stop = QPushButton("全部关闭")
        self.btn_peak_stop.setStyleSheet("background-color: #dc3545; color: white;")
        self.btn_peak_stop.setMaximumWidth(65)
        self.btn_peak_stop.clicked.connect(lambda: self.peak_all_stop.emit())
        peak_layout.addWidget(self.btn_peak_stop)

        self.btn_peak_restart = QPushButton("重启")
        self.btn_peak_restart.setMaximumWidth(45)
        self.btn_peak_restart.clicked.connect(lambda: self.peak_restart_selected.emit())
        peak_layout.addWidget(self.btn_peak_restart)

        self.btn_peak_tile = QPushButton("排列")
        self.btn_peak_tile.setMaximumWidth(45)
        self.btn_peak_tile.clicked.connect(lambda: self.peak_activate_tile.emit())
        peak_layout.addWidget(self.btn_peak_tile)

        # 垂直布局：日常一行 + 巅峰一行
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(6)
        main_layout.addWidget(daily_group)
        main_layout.addWidget(peak_group)
        main_layout.addStretch()

    def _on_daily_start(self) -> None:
        client_type = self.daily_client_combo.currentText()
        self.daily_all_start.emit(client_type)

    def get_selected_daily_client(self) -> str:
        return self.daily_client_combo.currentText()


# ============================================================
# 定时设置面板
# ============================================================

class TimerSettingsPanel(QWidget):
    """定时启动设置"""

    timing_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        # 日常定时
        layout.addWidget(QLabel("日常定时:"))

        self.cb_daily_timed = QCheckBox("启动")
        self.cb_daily_timed.setToolTip("启用定时启动日常")
        layout.addWidget(self.cb_daily_timed)

        self.cb_daily_restart = QCheckBox("自检")
        self.cb_daily_restart.setToolTip("启用日常自检重启")
        layout.addWidget(self.cb_daily_restart)

        self.sb_daily_hour = QComboBox()
        self.sb_daily_hour.addItems([f"{h:02d}" for h in range(24)])
        layout.addWidget(QLabel("时"))
        layout.addWidget(self.sb_daily_hour)

        self.sb_daily_minute = QComboBox()
        self.sb_daily_minute.addItems([f"{m:02d}" for m in range(60)])
        layout.addWidget(QLabel("分"))
        layout.addWidget(self.sb_daily_minute)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # 巅峰定时
        layout.addWidget(QLabel("巅峰定时:"))

        self.cb_peak_timed = QCheckBox("启动")
        self.cb_peak_timed.setToolTip("启用定时启动巅峰")
        layout.addWidget(self.cb_peak_timed)

        self.cb_peak_restart = QCheckBox("自检")
        self.cb_peak_restart.setToolTip("启用巅峰自检重启")
        layout.addWidget(self.cb_peak_restart)

        self.sb_peak_hour = QComboBox()
        self.sb_peak_hour.addItems([f"{h:02d}" for h in range(24)])
        layout.addWidget(QLabel("时"))
        layout.addWidget(self.sb_peak_hour)

        self.sb_peak_minute = QComboBox()
        self.sb_peak_minute.addItems([f"{m:02d}" for m in range(60)])
        layout.addWidget(QLabel("分"))
        layout.addWidget(self.sb_peak_minute)

        layout.addStretch()

    def get_daily_timing(self) -> tuple:
        """返回 (启用定时, 启用自检, 时, 分)"""
        return (
            self.cb_daily_timed.isChecked(),
            self.cb_daily_restart.isChecked(),
            self.sb_daily_hour.currentText(),
            self.sb_daily_minute.currentText(),
        )

    def get_peak_timing(self) -> tuple:
        return (
            self.cb_peak_timed.isChecked(),
            self.cb_peak_restart.isChecked(),
            self.sb_peak_hour.currentText(),
            self.sb_peak_minute.currentText(),
        )

    def set_daily_timing(self, timed: bool, restart: bool, hour: str, minute: str) -> None:
        self.cb_daily_timed.setChecked(timed)
        self.cb_daily_restart.setChecked(restart)
        self.sb_daily_hour.setCurrentText(hour.zfill(2))
        self.sb_daily_minute.setCurrentText(minute.zfill(2))

    def set_peak_timing(self, timed: bool, restart: bool, hour: str, minute: str) -> None:
        self.cb_peak_timed.setChecked(timed)
        self.cb_peak_restart.setChecked(restart)
        self.sb_peak_hour.setCurrentText(hour.zfill(2))
        self.sb_peak_minute.setCurrentText(minute.zfill(2))

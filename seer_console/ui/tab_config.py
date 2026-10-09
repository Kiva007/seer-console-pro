"""标签页②：仓库页 — 全部账号管理（客户端路径/运行选项已移至配置页）"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QMessageBox,
)

from PySide6.QtCore import Signal

from .widgets import AllAccountTable


def parse_engrave_text(text: str, seal_names: dict) -> int | None:
    """刻印单元格文本 → 编号。
    空 → 0（不兑换）；数字 0-10 直接取；名称查 seal_names 表反向查找；
    其他非法输入 → None（调用方不写回）。"""
    text = (text or "").strip()
    if not text:
        return 0
    if text.isdigit():
        no = int(text)
        return no if 0 <= no <= 10 else None
    for no, name in (seal_names or {}).items():
        if name == text:
            return no
    return None


class TabConfig(QWidget):
    """标签页②：仓库页 — 全部账号管理（新增/删除/保存/保存运行）"""

    # 信号
    account_added = Signal(object)  # Account对象
    account_removed = Signal(int)   # 行号
    account_moved = Signal(int, int)  # (旧行号, 新行号) 上移/下移单行
    account_updated = Signal()      # 保存账号
    account_edited = Signal()       # 单元格/开关变化（未保存）→ 运行页刷新
    save_run_requested = Signal()   # 保存运行：勾选账号写入运行页
    open_account_file = Signal()    # 打开账号文本（账本，原配置页辅助按钮移入）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accounts: list = []  # 当前表格对应的 Account 列表（写回用）
        self._build_ui()
        self._connect_table_signals()

    def _connect_table_signals(self) -> None:
        """表格编辑 → 写回 Account 模型 → 通知运行页刷新"""
        table = self.all_account_table
        table.cell_edited.connect(self._on_cell_edited)
        table.switch_toggled.connect(self._on_switch_toggled)

    def _on_cell_edited(self, row: int, col: int) -> None:
        """文本单元格编辑 → 写回对应 Account 字段（刻印列支持名称/编号输入）"""
        if not (0 <= row < len(self._accounts)):
            return
        field = self.all_account_table.TEXT_COLUMNS.get(col)
        if field is None:
            return
        acc = self._accounts[row]
        item = self.all_account_table.item(row, col)
        text = item.text().strip() if item else ""
        if field in ("engrave_y1", "engrave_y2"):
            # 刻印列：输入名称（如「渊」）/编号（如「5」）→ 存编号 1-10；
            # 清空 → 存 0（不兑换；此前清空被当非法输入丢弃，旧值清不掉）
            no = self._parse_engrave(text)
            if no is None:
                return  # 非法输入，不写回
            setattr(acc, field, no)
        else:
            setattr(acc, field, text)
        self.account_edited.emit()

    def _parse_engrave(self, text: str) -> int | None:
        """刻印文本 → 编号：空=0（不兑换）；数字直接取（0-10）；名称查表"""
        return parse_engrave_text(text, self.all_account_table.seal_names)

    def _on_switch_toggled(self, row: int, col: int, checked: bool) -> None:
        """选中/巅峰/日常开关变化 → 写回 Account"""
        if not (0 <= row < len(self._accounts)):
            return
        field = self.all_account_table.SWITCH_COLUMNS.get(col)
        if field is None:
            return
        setattr(self._accounts[row], field, checked)
        self.account_edited.emit()

    def _build_ui(self) -> None:
        # 无边框无标题：表格直接贴窗口内沿；边距与运行页一致
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # ====== 全部账号表格（撑满窗口）======
        self.all_account_table = AllAccountTable()
        main_layout.addWidget(self.all_account_table, stretch=1)

        # 增删按钮
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(4, 0, 4, 4)
        self.btn_add_account = QPushButton("新增账号")
        self.btn_add_account.clicked.connect(self._on_add_account)
        btn_layout.addWidget(self.btn_add_account)

        self.btn_remove_account = QPushButton("删除选中")
        self.btn_remove_account.clicked.connect(self._on_remove_account)
        btn_layout.addWidget(self.btn_remove_account)

        self.btn_move_up = QPushButton("上移")
        self.btn_move_up.setToolTip("把选中行向上移动一格")
        self.btn_move_up.clicked.connect(lambda: self._on_move_row(-1))
        btn_layout.addWidget(self.btn_move_up)

        self.btn_move_down = QPushButton("下移")
        self.btn_move_down.setToolTip("把选中行向下移动一格")
        self.btn_move_down.clicked.connect(lambda: self._on_move_row(1))
        btn_layout.addWidget(self.btn_move_down)

        self.btn_save_accounts = QPushButton("保存账号")
        self.btn_save_accounts.setStyleSheet("background-color: #0078d4; color: white;")
        self.btn_save_accounts.clicked.connect(lambda: self.account_updated.emit())
        btn_layout.addWidget(self.btn_save_accounts)

        self.btn_save_run = QPushButton("保存运行")
        self.btn_save_run.setToolTip("勾选的账号将显示在运行页（保存到账号文本）")
        self.btn_save_run.setStyleSheet("background-color: #28a745; color: white;")
        self.btn_save_run.clicked.connect(lambda: self.save_run_requested.emit())
        btn_layout.addWidget(self.btn_save_run)

        self.btn_open_account_file = QPushButton("打开账号文本")
        self.btn_open_account_file.setToolTip("用默认编辑器打开账号文本（账本）")
        self.btn_open_account_file.clicked.connect(lambda: self.open_account_file.emit())
        btn_layout.addWidget(self.btn_open_account_file)

        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)

    # ================================================================
    # 公共接口
    # ================================================================

    def get_all_account_table(self) -> AllAccountTable:
        return self.all_account_table

    def select_account_row(self, row: int) -> None:
        """选中指定行（移动后保持选中，便于连续上移/下移）"""
        table = self.all_account_table
        if 0 <= row < table.rowCount():
            table.setCurrentCell(row, 0)

    def refresh_all_accounts(self, accounts: list, seal_names: dict | None = None) -> None:
        """用全部账号列表刷新表格（密码明文，开关复选框；seal_names 为刻印编号→名称表）"""
        self._accounts = list(accounts)
        table = self.all_account_table
        if seal_names:
            table.seal_names = seal_names
        table._populating = True
        try:
            table.setRowCount(0)
            for acc in accounts:
                table.add_account_row(acc)
        finally:
            table._populating = False

    def _on_add_account(self) -> None:
        """新增空白账号行"""
        from ..core.account import Account

        new_acc = Account(
            selected=True,
            account_id="*",
            mi_hao="",
            password="",
            status="未启动",
            peak_enabled=False,
            peak_expiry="2033年1月1日",
            daily_enabled=False,
            daily_expiry="2033年1月1日",
            platform="",
            engrave_y1=5,
            engrave_y2=6,
        )
        self.account_added.emit(new_acc)

    def _on_remove_account(self) -> None:
        """删除选中行（需二次确认）"""
        current = self.all_account_table.currentRow()
        if current < 0:
            QMessageBox.warning(self, "提示", "请先选择要删除的账号")
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除第 {current + 1} 个账号吗？\n此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.account_removed.emit(current)

    def _on_move_row(self, delta: int) -> None:
        """上移/下移选中行：向主窗口发出 (旧行号, 新行号)，由主窗口交换顺序并刷新两页"""
        row = self.all_account_table.currentRow()
        new = row + delta
        if not (0 <= row < self.all_account_table.rowCount()
                and 0 <= new < self.all_account_table.rowCount()):
            return  # 未选中 / 已在最上或最下
        self.account_moved.emit(row, new)

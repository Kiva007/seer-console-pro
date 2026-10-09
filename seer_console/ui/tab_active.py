"""标签页①：活动账号（全宽） + 客户端列表（两列三大行） + 控制面板 + 日志"""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QCheckBox, QComboBox, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt, Signal

from .widgets import ActiveAccountTable, LogWidget, FocusClearTable


class TabActiveAccount(QWidget):
    """标签页①：运行页 — 账号表格（只读）+ 控制面板 + 日志"""

    log_message = Signal(str)
    client_run_requested = Signal(list)          # 运行：exe 路径列表（当前行全部客户端）
    client_path_set_requested = Signal(int, str)  # 读取：客户端条目下标, 新路径
    client_update_requested = Signal(list)        # 更新：客户端条目下标列表

    # 现行日常/巅峰切换（运行页下拉框选中，参数=客户端条目下标）
    current_daily_changed = Signal(int)
    current_peak_changed = Signal(int)

    # 批量操控（日常=雷小伊 / 巅峰=重聚）
    daily_batch_run = Signal()
    daily_batch_stop = Signal()
    daily_batch_restart = Signal()
    peak_batch_run = Signal()
    peak_batch_stop = Signal()
    peak_batch_restart = Signal()
    # 单号操控（右键菜单）：(accounts下标, 操作run/stop/restart, 客户端日常/巅峰)
    account_control = Signal(int, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client_data: list[dict] = []  # [{name, path, ver, kind}]
        self._client_rows: list[list[int]] = []  # 表格行 → 条目下标列表（登录器/日常/巅峰）
        self._build_ui()
        self._connect_control_signals()

    def _connect_control_signals(self) -> None:
        """批量按钮 → 批量信号；表格右键 → account_control"""
        self.btn_daily_run.clicked.connect(lambda: self.daily_batch_run.emit())
        self.btn_daily_stop.clicked.connect(lambda: self.daily_batch_stop.emit())
        self.btn_daily_restart.clicked.connect(lambda: self.daily_batch_restart.emit())
        self.btn_peak_run.clicked.connect(lambda: self.peak_batch_run.emit())
        self.btn_peak_stop.clicked.connect(lambda: self.peak_batch_stop.emit())
        self.btn_peak_restart.clicked.connect(lambda: self.peak_batch_restart.emit())
        self.account_table.account_control_requested.connect(self.account_control)
        # 现行日常/巅峰下拉框切换 → 发条目下标（占位「（未设置）」不发）
        self.cmb_daily_current.currentIndexChanged.connect(self._emit_daily_current)
        self.cmb_peak_current.currentIndexChanged.connect(self._emit_peak_current)

    def _emit_daily_current(self, index: int) -> None:
        idx = self.cmb_daily_current.itemData(index)
        if isinstance(idx, int):
            self.current_daily_changed.emit(idx)

    def _emit_peak_current(self, index: int) -> None:
        idx = self.cmb_peak_current.itemData(index)
        if isinstance(idx, int):
            self.current_peak_changed.emit(idx)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # 紧凑面板样式（缩小 GroupBox 内边距）
        compact_group_qss = """
            QGroupBox { font-weight: bold; margin-top: 6px; border: 1px solid #c8c8c8;
                        border-radius: 4px; padding: 2px 4px 4px 4px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        """

        # ======== 第一行：左=账号表格，右=游戏客户端控制 ========
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self.account_table = ActiveAccountTable()
        row1.addWidget(self.account_table, stretch=1)

        # 游戏客户端（右侧，两列三大行：登录器/日常/巅峰，名称+版本）
        client_grp = QGroupBox("游戏客户端")
        client_grp.setMaximumWidth(420)
        client_grp.setStyleSheet(compact_group_qss)
        self.client_grp = client_grp
        client_layout = QVBoxLayout(client_grp)
        client_layout.setSpacing(3)
        client_layout.setContentsMargins(6, 4, 6, 4)

        self.client_table = FocusClearTable(0, 3)
        self.client_table.setHorizontalHeaderLabels(["分类", "名称", "版本"])
        self.client_table.verticalHeader().setVisible(False)
        # 单单元格选中（不整行整块高亮），一次只选一个
        self.client_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self.client_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.client_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.client_table.setAlternatingRowColors(True)
        self.client_table.setShowGrid(True)
        self.client_table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        # 构建期保持自然布局：分类/版本=内容自适应、名称=拉伸；窗口显示后按实际宽度×倍率定宽
        self.client_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.client_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.client_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.client_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.client_table.verticalHeader().setDefaultSectionSize(24)
        self.client_table.setStyleSheet("""
            QTableWidget { font-size: 12px; gridline-color: #ddd; }
            QTableWidget::item:selected { background-color: #0078d4; color: white; }
            QHeaderView::section { background-color: #f0f0f0; padding: 3px;
                                   border: 1px solid #ddd; font-weight: bold; }
        """)
        client_layout.addWidget(self.client_table, stretch=1)

        # 共用按钮行：运行 / 更新 / 目录（作用于当前选中行=整个分类）
        # 统一内边距 + 自然高度：避免 24px 固定高度把中文字顶部裁切（高 DPI 下字体更高）
        # 三个按钮 stretch=1 等宽均布，占满面板整行宽度
        client_btn_qss = "QPushButton { padding: 3px 5px; }"
        client_btns = QHBoxLayout()
        client_btns.setSpacing(4)
        self.btn_client_run = QPushButton("运行")
        self.btn_client_run.setStyleSheet("background-color:#28a745;color:white;" + client_btn_qss)
        self.btn_client_run.clicked.connect(self._on_client_btn_run)
        client_btns.addWidget(self.btn_client_run, 1)
        self.btn_client_update = QPushButton("更新")
        self.btn_client_update.setStyleSheet(client_btn_qss)
        self.btn_client_update.clicked.connect(self._on_client_btn_update)
        client_btns.addWidget(self.btn_client_update, 1)
        self.btn_client_dir = QPushButton("目录")
        self.btn_client_dir.setStyleSheet(client_btn_qss)
        self.btn_client_dir.clicked.connect(self._on_client_btn_dir)
        client_btns.addWidget(self.btn_client_dir, 1)
        client_layout.addLayout(client_btns)

        row1.addWidget(client_grp)
        outer.addLayout(row1, stretch=2)

        # ======== 第二行：左=日常/巅峰控制+定时+辅助，右=日志 ========
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        # ---- 左侧：控制面板 ----
        left_panel = QVBoxLayout()
        left_panel.setSpacing(4)

        # 控制按钮统一高度
        ctrl_btn_qss = "min-width:52px"
        combo_qss = "QComboBox { min-width: 0; }"

        # 日常控制（雷小伊）：行1标签 / 行2左运行关闭+右定时自检 / 行3左重启排列+右时分
        dg = QGroupBox("日常控制")
        dg.setStyleSheet(compact_group_qss)
        dv = QVBoxLayout(dg); dv.setSpacing(2); dv.setContentsMargins(6, 2, 6, 4)
        # 行1：现行日常下拉框（过滤 kind=日常 的客户端，切换即写回配置）
        cur_daily = QHBoxLayout(); cur_daily.setSpacing(4)
        self.lbl_daily_caption = QLabel("现行日常")
        self.lbl_daily_caption.setStyleSheet("color:#666; font-size:12px; padding:0; margin:0;")
        cur_daily.addWidget(self.lbl_daily_caption)
        self.cmb_daily_current = QComboBox()
        self.cmb_daily_current.setToolTip("选择现行日常客户端（仅属性=日常 的条目）")
        self.cmb_daily_current.setStyleSheet(combo_qss)
        cur_daily.addWidget(self.cmb_daily_current, 1)
        cur_daily.addStretch(1)  # 与右侧留白等权：下拉框只占可用宽度的一半
        dv.addLayout(cur_daily)
        # 行2：左=运行/关闭，右=自动更新（定时启动正上方）+ 定时启动/自检
        d = QHBoxLayout(); d.setSpacing(4)
        self.btn_daily_run = QPushButton("运行")
        self.btn_daily_run.setStyleSheet("background-color:#28a745;color:white;" + ctrl_btn_qss)
        self.btn_daily_run.setFixedHeight(24)
        d.addWidget(self.btn_daily_run)
        self.btn_daily_stop = QPushButton("关闭")
        self.btn_daily_stop.setStyleSheet("background-color:#dc3545;color:white;" + ctrl_btn_qss)
        self.btn_daily_stop.setFixedHeight(24)
        d.addWidget(self.btn_daily_stop)
        d.addStretch()
        # 右侧竖排：自动更新（定时启动正上方），控制现行日常客户端的启动更新开关
        right_col = QVBoxLayout(); right_col.setSpacing(0)
        self.cb_daily_auto_update = QCheckBox("自动更新")
        self.cb_daily_auto_update.setToolTip(
            "现行日常客户端（雷小伊/重聚日常/XM）启动更新开关：勾选=批量启动检测到新版"
            "自动下载并切换新版继续登录；不勾选=点「取消」用当前版本继续运行。"
            "单号启动/自检重启不触发更新")
        right_col.addWidget(self.cb_daily_auto_update)
        timed_row = QHBoxLayout(); timed_row.setSpacing(4)
        self.cb_daily_timed = QCheckBox("定时启动"); timed_row.addWidget(self.cb_daily_timed)
        self.cb_daily_restart = QCheckBox("自检"); timed_row.addWidget(self.cb_daily_restart)
        right_col.addLayout(timed_row)
        d.addLayout(right_col)
        dv.addLayout(d)
        # 行3：左=重启/排列，右=设置时分
        d2 = QHBoxLayout(); d2.setSpacing(4)
        self.btn_daily_restart = QPushButton("重启")
        self.btn_daily_restart.setFixedHeight(24)
        self.btn_daily_restart.setStyleSheet(ctrl_btn_qss)
        d2.addWidget(self.btn_daily_restart)
        self.btn_daily_tile = QPushButton("排列")
        self.btn_daily_tile.setFixedHeight(24)
        self.btn_daily_tile.setStyleSheet(ctrl_btn_qss)
        d2.addWidget(self.btn_daily_tile)
        d2.addStretch()
        d2.addWidget(QLabel("时"))
        self.cmb_daily_h = QComboBox(); self.cmb_daily_h.addItems([f"{h:02d}" for h in range(24)])
        self.cmb_daily_h.setMaximumWidth(56); self.cmb_daily_h.setStyleSheet(combo_qss); d2.addWidget(self.cmb_daily_h)
        d2.addWidget(QLabel("分"))
        self.cmb_daily_m = QComboBox(); self.cmb_daily_m.addItems([f"{m:02d}" for m in range(60)])
        self.cmb_daily_m.setMaximumWidth(56); self.cmb_daily_m.setStyleSheet(combo_qss); d2.addWidget(self.cmb_daily_m)
        dv.addLayout(d2)
        left_panel.addWidget(dg, stretch=1)

        # 巅峰控制（重聚）：行1标签 / 行2左运行关闭+右定时自检 / 行3左重启排列+右时分
        pg = QGroupBox("巅峰控制")
        pg.setStyleSheet(compact_group_qss)
        pv = QVBoxLayout(pg); pv.setSpacing(2); pv.setContentsMargins(6, 2, 6, 4)
        # 行1：现行巅峰下拉框（过滤 kind=巅峰 的客户端，切换即写回配置）
        cur_peak = QHBoxLayout(); cur_peak.setSpacing(4)
        self.lbl_peak_caption = QLabel("现行巅峰")
        self.lbl_peak_caption.setStyleSheet("color:#666; font-size:12px; padding:0; margin:0;")
        cur_peak.addWidget(self.lbl_peak_caption)
        self.cmb_peak_current = QComboBox()
        self.cmb_peak_current.setToolTip("选择现行巅峰客户端（仅属性=巅峰 的条目）")
        self.cmb_peak_current.setStyleSheet(combo_qss)
        cur_peak.addWidget(self.cmb_peak_current, 1)
        cur_peak.addStretch(1)  # 与右侧留白等权：下拉框只占可用宽度的一半
        pv.addLayout(cur_peak)
        # 行2：左=运行/关闭，右=定时启动/自检
        p = QHBoxLayout(); p.setSpacing(4)
        self.btn_peak_run = QPushButton("运行")
        self.btn_peak_run.setStyleSheet("background-color:#28a745;color:white;" + ctrl_btn_qss)
        self.btn_peak_run.setFixedHeight(24)
        p.addWidget(self.btn_peak_run)
        self.btn_peak_stop = QPushButton("关闭")
        self.btn_peak_stop.setStyleSheet("background-color:#dc3545;color:white;" + ctrl_btn_qss)
        self.btn_peak_stop.setFixedHeight(24)
        p.addWidget(self.btn_peak_stop)
        p.addStretch()
        # 右侧竖排（同日常样式）：自动更新在上，定时启动+自检在下
        peak_right_col = QVBoxLayout(); peak_right_col.setSpacing(0)
        self.cb_peak_auto_update = QCheckBox("自动更新")
        self.cb_peak_auto_update.setToolTip(
            "巅峰客户端启动更新开关：勾选=批量启动检测到新版自动下载并切换新版继续登录；"
            "不勾选=点「取消」用当前版本继续运行。单号启动/自检重启不触发更新")
        peak_right_col.addWidget(self.cb_peak_auto_update)
        peak_timed_row = QHBoxLayout(); peak_timed_row.setSpacing(4)
        self.cb_peak_timed = QCheckBox("定时启动"); peak_timed_row.addWidget(self.cb_peak_timed)
        self.cb_peak_restart = QCheckBox("自检"); peak_timed_row.addWidget(self.cb_peak_restart)
        peak_right_col.addLayout(peak_timed_row)
        p.addLayout(peak_right_col)
        pv.addLayout(p)
        # 行3：左=重启/排列，右=设置时分
        p2 = QHBoxLayout(); p2.setSpacing(4)
        self.btn_peak_restart = QPushButton("重启")
        self.btn_peak_restart.setFixedHeight(24)
        self.btn_peak_restart.setStyleSheet(ctrl_btn_qss)
        p2.addWidget(self.btn_peak_restart)
        self.btn_peak_tile = QPushButton("排列")
        self.btn_peak_tile.setFixedHeight(24)
        self.btn_peak_tile.setStyleSheet(ctrl_btn_qss)
        p2.addWidget(self.btn_peak_tile)
        p2.addStretch()
        p2.addWidget(QLabel("时"))
        self.cmb_peak_h = QComboBox(); self.cmb_peak_h.addItems([f"{h:02d}" for h in range(24)])
        self.cmb_peak_h.setMaximumWidth(56); self.cmb_peak_h.setStyleSheet(combo_qss); p2.addWidget(self.cmb_peak_h)
        p2.addWidget(QLabel("分"))
        self.cmb_peak_m = QComboBox(); self.cmb_peak_m.addItems([f"{m:02d}" for m in range(60)])
        self.cmb_peak_m.setMaximumWidth(56); self.cmb_peak_m.setStyleSheet(combo_qss); p2.addWidget(self.cmb_peak_m)
        pv.addLayout(p2)
        left_panel.addWidget(pg, stretch=1)

        row2.addLayout(left_panel, stretch=1)

        # ---- 右侧日志（宽度加大缓解中文截断，高度保持减半）----
        self.log_widget = LogWidget()
        self.log_widget.setFixedSize(500, 280)
        row2.addWidget(self.log_widget, stretch=1)

        outer.addLayout(row2, stretch=1)

    # ================================================================
    # 公共接口
    # ================================================================

    def get_log_widget(self):
        return self.log_widget

    def set_current_software(self, daily_index: int | None = None,
                             peak_index: int | None = None) -> None:
        """设置现行日常/巅峰下拉框的选中项（条目下标；None=停在「（未设置）」占位）。
        blockSignals 避免刷新时误触发切换信号。"""
        for combo, idx in ((self.cmb_daily_current, daily_index),
                           (self.cmb_peak_current, peak_index)):
            combo.blockSignals(True)
            if idx is None:
                combo.setCurrentIndex(0)
            else:
                i = combo.findData(idx)
                combo.setCurrentIndex(i if i >= 0 else 0)
            combo.blockSignals(False)

    def _rebuild_current_combos(self) -> None:
        """按属性过滤重建现行日常/巅峰下拉框选项：
        首项「（未设置）」占位（不可选），其后为该属性的客户端条目（userData=条目下标）。
        无该属性条目时下拉框禁用。"""
        for combo, kind in ((self.cmb_daily_current, "日常"),
                            (self.cmb_peak_current, "巅峰")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（未设置）")
            combo.model().item(0).setEnabled(False)
            n = 0
            for i, d in enumerate(self._client_data):
                if d.get("kind", "登录器") == kind:
                    name = (d.get("name") or "").strip() or "（未命名）"
                    combo.addItem(name, i)
                    n += 1
            combo.setEnabled(n > 0)
            combo.blockSignals(False)

    def get_account_table(self):
        return self.account_table

    def log(self, message: str) -> None:
        self.log_widget.log(message)

    def refresh_account_table(self, active_accounts: list, account_indices: list[int] | None = None) -> None:
        """刷新运行页表格。

        active_accounts: get_active_accounts() 结果（手动启用的账号）
        account_indices: 与 active_accounts 平行的 accounts 下标列表（右键单号操控用）
        """
        table = self.account_table
        table.setRowCount(0)
        if account_indices is None:
            account_indices = list(range(len(active_accounts)))
        table.set_row_accounts(list(active_accounts), list(account_indices))
        if not active_accounts:
            return
        seq = 1
        for acc in active_accounts:
            table.add_account_row(
                seq=seq,
                account_name=acc.display_name,
                account_id=acc.mi_hao,
                password=acc.password,
                action=acc.current_action or acc.status,
                daily_switch=acc.daily_enabled,
                peak_switch=acc.peak_enabled,
                annotation=acc.annotation,
                platform=acc.platform,
                blue=acc.blue_crystal,
                purple=acc.purple_crystal,
            )
            seq += 1
        # 账号名列×2 由 showEvent（布局完成后）触发 apply_column_width_factors

    def refresh_client_table(self, client_data: list[dict]) -> None:
        """客户端列表：三列三大行 — 分类列按组合并居中（登录器/日常/巅峰），名称与版本为独立行。
        顺带重建现行日常/巅峰下拉框选项（选中项由主窗口 set_current_software 设置）。"""
        self._client_data = list(client_data)
        self._rebuild_current_combos()
        # 行序：登录器组 → 日常组 → 巅峰组（组内保持条目顺序）
        rows: list[tuple[int, str]] = []  # (条目下标, 分类)
        for kind in ["登录器", "日常", "巅峰"]:
            for i, d in enumerate(client_data):
                if d.get("kind", "登录器") == kind:
                    rows.append((i, kind))
        self.client_table.setRowCount(0)
        self.client_table.clearSpans()
        self.client_table.setRowCount(len(rows))
        # 分类列：按组合并单元格（合并居中显示 = 三大行），不可选中
        kind_start: dict[str, int] = {}
        for row, (i, kind) in enumerate(rows):
            kind_start.setdefault(kind, row)
        for kind in ["登录器", "日常", "巅峰"]:
            if kind not in kind_start:
                continue
            start = kind_start[kind]
            span = sum(1 for _, k in rows if k == kind)
            cat_item = QTableWidgetItem(kind)
            cat_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.client_table.setItem(start, 0, cat_item)
            if span > 1:
                self.client_table.setSpan(start, 0, span, 1)
        # 名称 + 版本（每个软件独立一行，不合并）；版本列不可选中
        self._client_rows = []
        for row, (i, kind) in enumerate(rows):
            d = self._client_data[i]
            self._client_rows.append([i])
            name_item = QTableWidgetItem(self._plain_name(d))
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ver_item = QTableWidgetItem(d.get("ver") or "--")
            ver_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ver_item.setFlags(ver_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.client_table.setItem(row, 1, name_item)
            self.client_table.setItem(row, 2, ver_item)

    def showEvent(self, event):
        """窗口首次显示、布局完成后，一次性应用客户端列表列宽倍率"""
        super().showEvent(event)
        if not getattr(self, "_client_width_done", False):
            self._apply_client_column_widths()

    # ---- 客户端列表列宽 ----

    def _apply_client_column_widths(self) -> None:
        """分类列×1.5、名称列×0.5（以各自实际渲染宽度为基准）、版本列保持自适应，
        并把客户端面板宽度缩到刚好容纳表格，腾出左侧空间让主表最右列（紫晶币）完整显示。

        名称列原为 Stretch 拉伸列，其“原宽”= 可用宽度 − 分类 − 版本；
        分类列为 ResizeToContents，其“原宽”= 实际渲染列宽。
        窗口首次显示（布局完成）后调用一次；_client_width_done 保证不重复缩放。
        """
        if getattr(self, "_client_width_done", False):
            return
        table = self.client_table
        if not table.isVisible() or table.viewport().width() <= 0:
            return
        avail = table.viewport().width()
        w_cat = table.columnWidth(0)   # 分类实际渲染宽
        w_ver = table.columnWidth(2)   # 版本实际渲染宽
        w_name = max(avail - w_cat - w_ver, 10)  # 名称原宽（拉伸基准）
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, max(int(w_cat * 1.5), 20))
        table.setColumnWidth(1, max(int(w_name * 0.5), 20))

        # 面板宽度 = 列宽和 + 垂直滚动条宽 + 表格边框 + 组内左右边距(6+6)；关闭水平滚动条避免预留宽度
        from PySide6.QtCore import Qt as _Qt
        table.setHorizontalScrollBarPolicy(_Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        total_w = header.length()
        sb_w = table.verticalScrollBar().sizeHint().width()
        frame_w = table.frameWidth() * 2
        need_w = total_w + sb_w + frame_w
        self.client_table.setMinimumWidth(need_w)
        self.client_table.setMaximumWidth(need_w)
        self.client_grp.setMinimumWidth(need_w + 12)
        self.client_grp.setMaximumWidth(need_w + 12)

        # 底部三个按钮等宽均布：面板内宽 − 组内左右边距(12) − 按钮间距(2×4=8)，再除以 3
        btn_w = max((need_w - 20) // 3, 20)
        for b in (self.btn_client_run, self.btn_client_update, self.btn_client_dir):
            b.setFixedWidth(btn_w)
        self._client_width_done = True

    # ---- 客户端共用按钮（作用于当前选中行所属分类）----

    @staticmethod
    def _plain_name(d: dict) -> str:
        """纯名字：去掉名字尾部已带的分类后缀（分类已在独立列显示，避免重复）"""
        kind = d.get("kind", "登录器")
        name = (d.get("name") or "").strip()
        for suffix in (f" {kind}", kind):
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)].rstrip()
                break
        return name or "（未命名）"

    def _current_row_indices(self) -> list[int]:
        """当前选中单元格对应的条目下标列表：
        点分类合并列 → 整个分类的全部条目；点名称/版本列 → 该行单个条目"""
        table = self.client_table
        row = table.currentRow()
        if not (0 <= row < len(self._client_rows)):
            return []
        if table.currentColumn() == 0:
            # 分类合并单元格：返回该分类全部条目
            kind = self._client_data[self._client_rows[row][0]].get("kind", "登录器")
            idxs = []
            for entry_idxs in self._client_rows:
                for i in entry_idxs:
                    if self._client_data[i].get("kind", "登录器") == kind:
                        idxs.append(i)
            return idxs
        return list(self._client_rows[row])

    def _current_row_entries(self) -> list[dict]:
        return [self._client_data[i] for i in self._current_row_indices()]

    def _on_client_btn_run(self) -> None:
        paths = [e["path"] for e in self._current_row_entries() if e.get("path")]
        if paths:
            self.client_run_requested.emit(paths)

    def _on_client_btn_update(self) -> None:
        idxs = self._current_row_indices()
        if idxs:
            self.client_update_requested.emit(idxs)

    def _on_client_btn_dir(self) -> None:
        for e in self._current_row_entries():
            if e.get("path"):
                self._open_dir(e["path"])
                return

    @staticmethod
    def _open_dir(exe_path: str) -> None:
        if exe_path and os.path.isfile(exe_path):
            os.startfile(os.path.dirname(exe_path))

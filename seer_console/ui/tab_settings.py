"""标签页③：配置页 — 客户端管理列表 + 雷小伊任务勾选 + 巅峰任务勾选"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLineEdit, QLabel, QCheckBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout,
    QPlainTextEdit, QSpinBox,
)
from PySide6.QtCore import Signal, Qt

from ..core.config import ClientEntry, CLIENT_KINDS, LeiTaskConfig, PeakRunConfig
from .widgets import FocusClearTable


class TabSettings(QWidget):
    """标签页③：配置页 — 客户端管理（自定义列表）+ 雷小伊任务勾选 + 巅峰任务勾选"""

    save_config_requested = Signal()  # 统一「保存配置」按钮（主窗口把客户端表格+雷小伊全档+巅峰全档写回 ctrl.ini）

    HEADERS = ["名称", "软件路径", "属性"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[ClientEntry] = []
        self._path_edits: list[QLineEdit] = []
        self._build_ui()
        self._rebuild_table()
        # 多 profile 勾选策略缓存（key: ""=全局/"新号"=新号/米米号=特定账号）
        # _current_profile_key=None 表示尚未初始化（切换/保存均不 flush UI→缓存，
        # 避免 set_lei_task_profiles 首次装载时把 UI 旧内容写进全局档）
        self._profile_cfg_cache: dict[str, LeiTaskConfig] = {"": LeiTaskConfig()}
        self._current_profile_key: str | None = None
        self._lei_accounts: list = []           # Account 对象列表
        self._account_names: dict[str, str] = {}  # mi_hao → account_name
        # 巅峰运行选项多 profile 缓存（key: ""=全局/"新号"=新号/米米号=特定账号；
        # _current_peak_key=None 表示尚未初始化，切换/保存均不 flush UI→缓存，
        # 避免 set_peak_run_profiles 首次装载时把 UI 旧内容写进全局档）
        self._peak_profile_cache: dict[str, PeakRunConfig] = {"": PeakRunConfig()}
        self._current_peak_key: str | None = None

    # ================================================================
    # UI构建
    # ================================================================

    def _build_ui(self) -> None:
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # ====== 左侧：客户端管理 + 巅峰任务勾选（上/下）======
        left = QVBoxLayout()
        left.setSpacing(4)

        # ====== 客户端管理（高度压缩：固定可视区约 6 行，多余条目滚动）======
        client_group = QGroupBox("客户端管理")
        client_layout = QVBoxLayout(client_group)
        client_layout.setContentsMargins(6, 4, 6, 4)
        client_layout.setSpacing(4)

        self.client_table = FocusClearTable(0, len(self.HEADERS))
        self.client_table.setHorizontalHeaderLabels(self.HEADERS)
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.client_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.client_table.setAlternatingRowColors(True)
        # 名称/属性列双击编辑，路径行内 QLineEdit 常可编辑
        self.client_table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked |
            QTableWidget.EditTrigger.EditKeyPressed
        )
        header = self.client_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.client_table.cellChanged.connect(self._on_cell_changed)
        client_layout.addWidget(self.client_table, stretch=1)

        # 按钮行：新增 / 修改 / 删除 / 上移 / 下移（保存统一走左下角「保存配置」）
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.btn_add = QPushButton("新增")
        self.btn_add.clicked.connect(self._on_add)
        btn_row.addWidget(self.btn_add)
        self.btn_edit = QPushButton("修改")
        self.btn_edit.clicked.connect(self._on_edit)
        btn_row.addWidget(self.btn_edit)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)
        self.btn_up = QPushButton("上移")
        self.btn_up.clicked.connect(lambda: self._move_row(-1))
        btn_row.addWidget(self.btn_up)
        self.btn_down = QPushButton("下移")
        self.btn_down.clicked.connect(lambda: self._move_row(1))
        btn_row.addWidget(self.btn_down)
        btn_row.addStretch()
        client_layout.addLayout(btn_row)

        # 客户端管理按内容自然取高：表格 250px 定高（约 8 行）+ 按钮行；下方接巅峰任务勾选
        self.client_table.setFixedHeight(250)
        left.addWidget(client_group)

        # ====== 雷小伊任务勾选（勾选策略定制，写 [雷小伊勾选] 节）======
        lei_group = QGroupBox("雷小伊任务勾选")
        lei_layout = QVBoxLayout(lei_group)
        lei_layout.setContentsMargins(6, 4, 6, 4)
        lei_layout.setSpacing(4)

        # 账号设置选择器（下拉 + 新增按钮）
        profile_row = QHBoxLayout()
        profile_row.setSpacing(4)
        profile_row.addWidget(QLabel("账号设置"))
        self.combo_lei_profile = QComboBox()
        self.combo_lei_profile.setMinimumContentsLength(20)
        self.combo_lei_profile.setToolTip("选择编辑哪份勾选策略：全局默认 / 新号配置 / 特定账号")
        self.combo_lei_profile.currentIndexChanged.connect(self._on_lei_profile_changed)
        profile_row.addWidget(self.combo_lei_profile, stretch=1)
        self.btn_lei_profile_add = QPushButton("新增账号配置")
        self.btn_lei_profile_add.setToolTip("从当前账号列表选择并创建专属勾选档")
        self.btn_lei_profile_add.clicked.connect(self._on_lei_profile_add)
        profile_row.addWidget(self.btn_lei_profile_add)
        lei_layout.addLayout(profile_row)

        # 竖排：Unity 排除项 / Flash 勾选项 上下堆叠（右栏布局，每框 full width）
        col_unity = QVBoxLayout()
        col_unity.setSpacing(2)
        lbl_unity = QLabel("Unity 排除项")
        lbl_unity.setToolTip("全选后取消勾选的任务（子串匹配，每行一个）。\n清空=全选后不取消任何项；恢复默认=月华 / 日常-宿命王者的锻炼")
        col_unity.addWidget(lbl_unity)
        self.txt_lei_unity_exclude = QPlainTextEdit()
        self.txt_lei_unity_exclude.setPlaceholderText("月华\n日常-宿命王者的锻炼")
        self.txt_lei_unity_exclude.setMinimumHeight(90)
        col_unity.addWidget(self.txt_lei_unity_exclude, stretch=1)
        lei_layout.addLayout(col_unity, stretch=1)

        col_flash = QVBoxLayout()
        col_flash.setSpacing(2)
        lbl_flash = QLabel("Flash 勾选项")
        lbl_flash.setToolTip("Flash 页按关键字勾选（子串匹配，每行一个），未匹配的不动。\n留空=不修改 Flash 页（连标签都不切）；所有账号通用，新号差异在「账号设置」下拉选「新号配置」单独设")
        col_flash.addWidget(lbl_flash)
        self.txt_lei_flash = QPlainTextEdit()
        self.txt_lei_flash.setPlaceholderText("留空=不修改 Flash 页\n各种签到+宝箱\n刻印抽奖\nVIP礼包\n…")
        self.txt_lei_flash.setMinimumHeight(90)
        col_flash.addWidget(self.txt_lei_flash, stretch=1)
        lei_layout.addLayout(col_flash, stretch=1)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("因子顺序勾选项"))
        self.spin_lei_factor = QSpinBox()
        self.spin_lei_factor.setRange(0, 50)
        self.spin_lei_factor.setToolTip("因子页顺序勾选前 N 项（0=不勾）。\n所有账号一致；新号如需不同勾数，在「账号设置」下拉里选「新号配置」单独设置")
        spin_row.addWidget(self.spin_lei_factor)
        spin_row.addStretch()
        lei_layout.addLayout(spin_row)

        # 巅峰任务勾选组 options_group 在下方构造（原「运行选项」组改名），
        # 与雷小伊任务勾选互换位置后加入左列（客户端管理下方）

        right = QVBoxLayout()
        right.setSpacing(4)

        options_group = QGroupBox("巅峰任务勾选")
        options_layout = QVBoxLayout(options_group)
        options_layout.setSpacing(4)

        # 账号设置选择器（与雷小伊勾选同构：全局默认 / 新号配置 / 特定账号）
        peak_profile_row = QHBoxLayout()
        peak_profile_row.setSpacing(4)
        peak_profile_row.addWidget(QLabel("账号设置"))
        self.combo_peak_profile = QComboBox()
        self.combo_peak_profile.setMinimumContentsLength(20)
        self.combo_peak_profile.setToolTip(
            "选择编辑哪份巅峰任务勾选：全局默认 / 新号配置 / 特定账号")
        self.combo_peak_profile.currentIndexChanged.connect(
            self._on_peak_profile_changed)
        peak_profile_row.addWidget(self.combo_peak_profile)
        self.btn_peak_profile_add = QPushButton("新增账号配置")
        self.btn_peak_profile_add.clicked.connect(self._on_peak_profile_add)
        peak_profile_row.addWidget(self.btn_peak_profile_add)
        peak_profile_row.addStretch()
        options_layout.addLayout(peak_profile_row)

        self.cb_no_victory = QCheckBox("不要胜利任务")
        self.cb_wild_quest = QCheckBox("做狂野任务")
        self.cb_only_fifth = QCheckBox("仅用第五")
        self.cb_no_rank_reward = QCheckBox("不领取段位奖励")
        for cb in (self.cb_no_victory, self.cb_wild_quest,
                   self.cb_only_fifth, self.cb_no_rank_reward):
            # 不即时保存：勾选只改 UI，点「保存」才写盘（避免每次勾选刷一行日志）
            options_layout.addWidget(cb)

        # 巅峰任务勾选 → 左列（客户端管理下方）；雷小伊任务勾选 → 右列（竖向排布）
        left.addWidget(options_group)

        # 统一「保存配置」按钮：左列底部（巅峰任务勾选组下一行），
        # 客户端表格 + 雷小伊全档 + 巅峰全档 一次写盘
        save_row = QHBoxLayout()
        self.btn_save_config = QPushButton("保存配置")
        self.btn_save_config.setStyleSheet("background-color: #28a745; color: white;")
        self.btn_save_config.setToolTip(
            "把客户端列表与两份勾选配置（雷小伊/巅峰，含全部账号档）一次性写入 ctrl.ini")
        self.btn_save_config.clicked.connect(
            lambda: self.save_config_requested.emit())
        save_row.addWidget(self.btn_save_config)
        save_row.addStretch()
        left.addLayout(save_row)

        left.addStretch()
        main_layout.addLayout(left, stretch=1)

        right.addWidget(lei_group)
        right.addStretch()
        main_layout.addLayout(right)

    # ================================================================
    # 表格构建
    # ================================================================

    def _rebuild_table(self) -> None:
        """按 self._entries 重建表格（现行日常/巅峰选择已移至运行页下拉框，此处不展示）"""
        table = self.client_table
        table.blockSignals(True)
        table.setRowCount(0)
        table.setRowCount(len(self._entries))
        self._path_edits = []
        for row, e in enumerate(self._entries):
            # 名称（可编辑文本）
            item = QTableWidgetItem(e.name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 0, item)
            # 软件路径：行内 QLineEdit（选择路径走「修改」按钮的对话框）
            path_edit = QLineEdit(e.path)
            path_edit.setFrame(False)
            path_edit.editingFinished.connect(lambda r=row: self._sync_path_from_ui(r))
            table.setCellWidget(row, 1, path_edit)
            self._path_edits.append(path_edit)
            # 属性：下拉（登录器/日常/巅峰）
            combo = QComboBox(table)
            combo.addItems(CLIENT_KINDS)
            combo.setCurrentText(e.kind if e.kind in CLIENT_KINDS else "登录器")
            combo.currentIndexChanged.connect(lambda idx, r=row: self._sync_kind_from_ui(r, idx))
            table.setCellWidget(row, 2, combo)
        table.blockSignals(False)

    # ================================================================
    # UI → 数据同步
    # ================================================================

    def _on_cell_changed(self, row: int, col: int) -> None:
        """名称列编辑 → 写回 _entries"""
        if col == 0 and 0 <= row < len(self._entries):
            item = self.client_table.item(row, 0)
            if item:
                self._entries[row].name = item.text().strip()

    def _sync_path_from_ui(self, row: int) -> None:
        if 0 <= row < len(self._entries):
            self._entries[row].path = self._path_edits[row].text().strip()

    def _sync_kind_from_ui(self, row: int, idx: int) -> None:
        if 0 <= row < len(self._entries) and 0 <= idx < len(CLIENT_KINDS):
            self._entries[row].kind = CLIENT_KINDS[idx]

    # ================================================================
    # 按钮操作
    # ================================================================

    def _on_add(self) -> None:
        """新增：末尾追加一条默认条目"""
        self._entries.append(ClientEntry(name="新客户端", kind="登录器"))
        self._rebuild_table()
        self.client_table.selectRow(len(self._entries) - 1)

    def _on_edit(self) -> None:
        """修改：弹出对话框编辑选中行的名称与路径"""
        row = self.client_table.currentRow()
        if not (0 <= row < len(self._entries)):
            return
        e = self._entries[row]
        result = self._edit_dialog(e.name, e.path)
        if result is not None:
            name, path = result
            e.name = name
            e.path = path
            self._rebuild_table()
            self.client_table.selectRow(row)

    def _edit_dialog(self, name: str, path: str) -> tuple[str, str] | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("修改客户端")
        form = QFormLayout(dlg)
        name_edit = QLineEdit(name)
        path_edit = QLineEdit(path)
        form.addRow("名称", name_edit)
        path_row = QWidget()
        pl = QHBoxLayout(path_row)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(path_edit)
        browse = QPushButton("...")
        browse.clicked.connect(lambda: self._pick_path_into(path_edit))
        pl.addWidget(browse)
        form.addRow("路径", path_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return name_edit.text().strip(), path_edit.text().strip()
        return None

    def _on_delete(self) -> None:
        row = self.client_table.currentRow()
        if 0 <= row < len(self._entries):
            self._entries.pop(row)
            self._rebuild_table()

    def _move_row(self, delta: int) -> None:
        row = self.client_table.currentRow()
        new = row + delta
        if not (0 <= row < len(self._entries) and 0 <= new < len(self._entries)):
            return
        self._entries[row], self._entries[new] = self._entries[new], self._entries[row]
        self._rebuild_table()
        self.client_table.selectRow(new)

    @staticmethod
    def _pick_path_into(line_edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            line_edit, "选择客户端EXE", "",
            "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if path:
            line_edit.setText(path)

    # ================================================================
    # 雷小伊任务勾选 — 多 profile 管理
    # ================================================================

    def set_lei_task_accounts(self, accounts: list) -> None:
        """外部推送账号列表（用于下拉与新增对话框）"""
        self._lei_accounts = list(accounts)
        self._account_names = {
            a.mi_hao: a.account_name for a in accounts if a.mi_hao
        }

    def set_lei_task_profiles(self, profiles: dict[str, LeiTaskConfig]) -> None:
        """外部推送全部配置档：{key: LeiTaskConfig}；重建下拉并选中当前档"""
        self._profile_cfg_cache = dict(profiles)
        if "" not in self._profile_cfg_cache:
            self._profile_cfg_cache[""] = LeiTaskConfig()
        self._rebuild_profile_combo()

    # ---- 内部 ----

    def _rebuild_profile_combo(self) -> None:
        """根据 _profile_cfg_cache 重建下拉列表（保持选中项）"""
        cur = (self.combo_lei_profile.currentData()
               if self.combo_lei_profile.count() > 0
               else self._current_profile_key or "")
        self.combo_lei_profile.blockSignals(True)
        self.combo_lei_profile.clear()
        self.combo_lei_profile.addItem("全局默认", "")
        self.combo_lei_profile.addItem("新号配置", "新号")
        for key in sorted(self._profile_cfg_cache.keys()):
            if key in ("", "新号"):
                continue
            label = (
                f"{key} ({self._account_names[key]})"
                if self._account_names.get(key) else key
            )
            self.combo_lei_profile.addItem(label, key)
        idx = self.combo_lei_profile.findData(cur)
        self.combo_lei_profile.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_lei_profile.blockSignals(False)
        self._on_lei_profile_changed(self.combo_lei_profile.currentIndex())

    def _on_lei_profile_changed(self, index: int) -> None:
        """切换 profile：旧编辑落缓存 → 新 profile 载入 UI"""
        if index < 0:
            return
        if self._current_profile_key is not None:
            self._profile_cfg_cache[self._current_profile_key] = self._ui_to_config()
        new_key = self.combo_lei_profile.itemData(index)
        self._current_profile_key = new_key
        cfg = self._profile_cfg_cache.get(new_key, LeiTaskConfig())
        self._populate_ui_from_config(cfg)

    def _on_lei_profile_add(self) -> None:
        """「新增账号配置」：弹出账号选择器 → 为选中账号创建专属勾选档"""
        if not self._lei_accounts:
            return
        candidates = [a for a in self._lei_accounts if a.mi_hao]
        if not candidates:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("新增账号配置")
        layout = QFormLayout(dlg)
        combo = QComboBox()
        for a in candidates:
            display = (
                f"{a.mi_hao} ({a.account_name})" if a.account_name else a.mi_hao
            )
            combo.addItem(display, a.mi_hao)
        layout.addRow("选择账号", combo)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addRow(btn_box)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._add_lei_task_profile(combo.itemData(combo.currentIndex()))
        self._rebuild_profile_combo()

    def _add_lei_task_profile(self, mi_hao: str) -> None:
        """为指定米米号创建专属勾选档（已存在/无米米号则跳过；初值=克隆全局默认）"""
        if not mi_hao or mi_hao in ("", "新号"):
            return
        if mi_hao in self._profile_cfg_cache:
            return
        global_cfg = self._profile_cfg_cache.get("", LeiTaskConfig())
        self._profile_cfg_cache[mi_hao] = LeiTaskConfig(
            unity_exclude=global_cfg.unity_exclude,
            flash_checks=global_cfg.flash_checks,
            factor_count=global_cfg.factor_count,
        )

    def _populate_ui_from_config(self, cfg: LeiTaskConfig) -> None:
        """填充 UI 文本框/勾数（不改变当前选中 profile）"""
        self.txt_lei_unity_exclude.setPlainText("\n".join(cfg.unity_exclude))
        self.txt_lei_flash.setPlainText("\n".join(cfg.flash_checks))
        self.spin_lei_factor.setValue(cfg.factor_count)

    def _ui_to_config(self) -> LeiTaskConfig:
        """从 UI 控件收集当前勾选策略（文本框空=空策略=清空该项）"""
        def lines(edit: QPlainTextEdit) -> tuple:
            return tuple(k.strip() for k in edit.toPlainText().splitlines()
                         if k.strip())
        return LeiTaskConfig(
            unity_exclude=lines(self.txt_lei_unity_exclude),
            flash_checks=lines(self.txt_lei_flash),
            factor_count=self.spin_lei_factor.value(),
        )

    # ---- 向后兼容（旧调用方不感知多 profile，仅为"全局默认"的快捷入口）----

    def set_lei_task_config(self, cfg: LeiTaskConfig) -> None:
        """向后兼容：直接填充 UI 为给定配置（不改变当前 profile）"""
        self._populate_ui_from_config(cfg)

    def get_lei_task_config(self) -> LeiTaskConfig:
        """向后兼容：从 UI 收集当前配置（等于 _ui_to_config）"""
        return self._ui_to_config()

    # ================================================================
    # 公共接口
    # ================================================================

    def set_client_entries(self, entries: list) -> None:
        """外部设置客户端列表（不触发保存）"""
        self._entries = [e.copy() if isinstance(e, ClientEntry) else ClientEntry(
            name=e.get("name", ""), path=e.get("path", ""),
            kind=e.get("kind", "登录器"),
            is_daily=e.get("is_daily", False), is_peak=e.get("is_peak", False))
            for e in entries]
        self._rebuild_table()

    def get_client_entries(self) -> list[ClientEntry]:
        """获取当前列表（深拷贝）"""
        return [e.copy() for e in self._entries]

    def flush_all_caches(self) -> None:
        """统一「保存配置」前调用：把各编辑源落回内部缓存。

        - 客户端表格：同步行内未提交的路径编辑到 _entries（名称/属性编辑时已实时同步）；
        - 雷小伊勾选：当前选中档 UI → _profile_cfg_cache（未初始化按全局""处理）；
        - 巅峰勾选：当前选中档 UI → _peak_profile_cache（同上）。
        之后主窗口经 get_client_entries/get_lei_profiles/get_peak_profiles
        读取全量并逐档写回 ctrl.ini。
        """
        for row, edit in enumerate(self._path_edits):
            if 0 <= row < len(self._entries):
                self._entries[row].path = edit.text().strip()
        key = self._current_profile_key if self._current_profile_key is not None else ""
        self._profile_cfg_cache[key] = self._ui_to_config()
        key = self._current_peak_key if self._current_peak_key is not None else ""
        self._peak_profile_cache[key] = self._ui_to_peak()

    def get_lei_profiles(self) -> dict:
        """雷小伊勾选全档缓存（{key: LeiTaskConfig}，flush_all_caches 之后读取）"""
        return dict(self._profile_cfg_cache)

    def get_peak_profiles(self) -> dict:
        """巅峰勾选全档缓存（{key: PeakRunConfig}，flush_all_caches 之后读取）"""
        return dict(self._peak_profile_cache)

    # ================================================================
    # 巅峰运行选项 — 多 profile 管理（与雷小伊勾选同构）
    # ================================================================

    def set_peak_run_accounts(self, accounts: list) -> None:
        """外部推送账号列表（用于下拉与新增对话框；与雷小伊共用账号源）"""
        self._lei_accounts = list(accounts)
        self._account_names = {
            a.mi_hao: a.account_name for a in accounts if a.mi_hao
        }

    def set_peak_run_profiles(self, profiles: dict[str, PeakRunConfig]) -> None:
        """外部推送全部配置档：{key: PeakRunConfig}；重建下拉并选中当前档"""
        self._peak_profile_cache = dict(profiles)
        if "" not in self._peak_profile_cache:
            self._peak_profile_cache[""] = PeakRunConfig()
        self._rebuild_peak_profile_combo()

    # ---- 内部 ----

    def _rebuild_peak_profile_combo(self) -> None:
        """根据 _peak_profile_cache 重建下拉列表（保持选中项）"""
        cur = (self.combo_peak_profile.currentData()
               if self.combo_peak_profile.count() > 0
               else self._current_peak_key or "")
        self.combo_peak_profile.blockSignals(True)
        self.combo_peak_profile.clear()
        self.combo_peak_profile.addItem("全局默认", "")
        self.combo_peak_profile.addItem("新号配置", "新号")
        for key in sorted(self._peak_profile_cache.keys()):
            if key in ("", "新号"):
                continue
            label = (
                f"{key} ({self._account_names[key]})"
                if self._account_names.get(key) else key
            )
            self.combo_peak_profile.addItem(label, key)
        idx = self.combo_peak_profile.findData(cur)
        self.combo_peak_profile.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_peak_profile.blockSignals(False)
        self._on_peak_profile_changed(self.combo_peak_profile.currentIndex())

    def _on_peak_profile_changed(self, index: int) -> None:
        """切换 profile：旧编辑落缓存 → 新 profile 载入 UI"""
        if index < 0:
            return
        if self._current_peak_key is not None:
            self._peak_profile_cache[self._current_peak_key] = self._ui_to_peak()
        new_key = self.combo_peak_profile.itemData(index)
        self._current_peak_key = new_key
        cfg = self._peak_profile_cache.get(new_key, PeakRunConfig())
        self._populate_peak_ui(cfg)

    def _on_peak_profile_add(self) -> None:
        """「新增账号配置」：弹出账号选择器 → 为选中账号创建专属运行选项档"""
        if not self._lei_accounts:
            return
        candidates = [a for a in self._lei_accounts if a.mi_hao]
        if not candidates:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("新增账号配置")
        layout = QFormLayout(dlg)
        combo = QComboBox()
        for a in candidates:
            display = (
                f"{a.mi_hao} ({a.account_name})" if a.account_name else a.mi_hao
            )
            combo.addItem(display, a.mi_hao)
        layout.addRow("选择账号", combo)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addRow(btn_box)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._add_peak_run_profile(combo.itemData(combo.currentIndex()))
        self._rebuild_peak_profile_combo()

    def _add_peak_run_profile(self, mi_hao: str) -> None:
        """为指定米米号创建专属运行选项档（已存在/无米米号则跳过；初值=克隆全局默认）"""
        if not mi_hao or mi_hao in ("", "新号"):
            return
        if mi_hao in self._peak_profile_cache:
            return
        self._peak_profile_cache[mi_hao] = PeakRunConfig(
            **vars(self._peak_profile_cache.get("", PeakRunConfig())))

    def _populate_peak_ui(self, cfg: PeakRunConfig) -> None:
        """填充四个复选框（blockSignals 防装载过程误触发 toggled）"""
        for cb in (self.cb_no_victory, self.cb_wild_quest,
                   self.cb_only_fifth, self.cb_no_rank_reward):
            cb.blockSignals(True)
        self.cb_no_victory.setChecked(cfg.no_victory_quest)
        self.cb_wild_quest.setChecked(cfg.do_wild_quest)
        self.cb_only_fifth.setChecked(cfg.only_fifth)
        self.cb_no_rank_reward.setChecked(cfg.no_rank_reward)
        for cb in (self.cb_no_victory, self.cb_wild_quest,
                   self.cb_only_fifth, self.cb_no_rank_reward):
            cb.blockSignals(False)

    def _ui_to_peak(self) -> PeakRunConfig:
        """从四个复选框收集当前配置"""
        return PeakRunConfig(
            no_victory_quest=self.cb_no_victory.isChecked(),
            do_wild_quest=self.cb_wild_quest.isChecked(),
            only_fifth=self.cb_only_fifth.isChecked(),
            no_rank_reward=self.cb_no_rank_reward.isChecked(),
        )

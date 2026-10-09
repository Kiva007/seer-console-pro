#!/usr/bin/env python3
"""Seer中控台Pro — 赛尔号多账号中控工具（Python重构版）"""

import sys
import os

# 注意：不要手动调用 SetProcessDPIAware()——Qt 6 默认使用
# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2，手动设置会触发
# "SetProcessDpiAwarenessContext() failed: 拒绝访问" 报错。
# Qt 初始化后进程即为 DPI-aware，GetWindowRect/UIA/SendInput 均使用物理像素坐标。

# 确保项目根目录在 path 中（main.py 位于 seer_console/ 内，需上溯两级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import QTimer

from seer_console.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Seer中控台Pro")
    app.setOrganizationName("SeerConsole")

    # 设置默认字体
    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    # 暗色主题样式
    app.setStyleSheet("""
        QMainWindow {
            background-color: #f5f5f5;
        }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #ddd;
            border-radius: 6px;
            margin-top: 8px;
            padding-top: 16px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
        }
        QPushButton {
            padding: 5px 12px;
            border: 1px solid #ccc;
            border-radius: 4px;
            background-color: #fff;
        }
        QPushButton:hover {
            background-color: #e6e6e6;
        }
        QPushButton:pressed {
            background-color: #d4d4d4;
        }
        QComboBox, QLineEdit {
            padding: 4px 8px;
            border: 1px solid #ccc;
            border-radius: 4px;
            background-color: #fff;
        }
        QCheckBox {
            spacing: 4px;
        }
        QTabWidget::pane {
            border: 1px solid #ccc;
            border-top: 2px solid #0078d4;
        }
        QTabBar::tab {
            padding: 6px 20px;
            margin-right: 2px;
        }
        QTabBar::tab:selected {
            background-color: #0078d4;
            color: white;
        }
    """)

    window = MainWindow()
    window.show()
    if "--activate-after-restart" in sys.argv:
        # 等显示事件进入事件循环，再对已有原生句柄置前激活一次。
        QTimer.singleShot(0, window.activate_after_restart)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

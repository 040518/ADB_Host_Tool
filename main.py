# -*- coding: utf-8 -*-
"""ADB 上位机 —— 程序入口。

直接运行: python main.py
打包后:   ADB_Host_Tool.exe
"""

from __future__ import annotations

import os
import sys

# 保证从任意工作目录启动都能 import 到本项目的包
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main() -> int:
    import ctypes

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont, QPalette, QColor
    from PySide6.QtWidgets import QApplication

    from ui import theme
    from ui.context import AppContext
    from ui.main_window import MainWindow

    # 固定 Windows 任务栏身份，避免 PyInstaller onefile 使用通用白块图标。
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                theme.WINDOWS_APP_ID)
        except Exception:  # noqa: BLE001
            pass

    # 高 DPI：Qt6 默认已启用缩放，这里只设置取整策略
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:  # noqa: BLE001
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(theme.APP_NAME)
    app.setOrganizationName(theme.APP_ID)

    # 图标（任务栏 / 窗口）
    app.setWindowIcon(theme.app_icon())

    # 深色主题
    app.setStyle("Fusion")
    app.setStyleSheet(theme.load_stylesheet())

    settings = theme.load_settings()

    # 基础字体
    fam = settings.get("font_family") or ""
    size = int(settings.get("font_size", 13) or 13)
    f = QFont(fam) if fam else QFont()
    f.setPointSize(size)
    app.setFont(f)

    # 调色板兜底（避免原生控件在深色下变白）
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.COLORS["bg"]))
    pal.setColor(QPalette.WindowText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Base, QColor("#252526"))
    pal.setColor(QPalette.AlternateBase, QColor("#2A2A2C"))
    pal.setColor(QPalette.Text, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Button, QColor(theme.COLORS["bg_toolbar"]))
    pal.setColor(QPalette.ButtonText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Highlight, QColor(theme.COLORS["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ToolTipBase, QColor("#252526"))
    pal.setColor(QPalette.ToolTipText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#6A6A6A"))
    app.setPalette(pal)

    ctx = AppContext(settings)
    win = MainWindow(ctx)

    def show_in_foreground():
        # 每次启动都允许新窗口，但必须恢复并切到前台，避免只出现在任务栏。
        win.showNormal()
        win.raise_()
        win.activateWindow()
        if sys.platform.startswith("win"):
            try:
                hwnd = int(win.winId())
                ctypes.windll.user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass

    show_in_foreground()
    QTimer.singleShot(200, show_in_foreground)

    rc = app.exec()
    try:
        theme.save_settings(ctx.settings)
    except Exception:  # noqa: BLE001
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())

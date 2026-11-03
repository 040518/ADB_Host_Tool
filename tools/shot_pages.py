# -*- coding: utf-8 -*-
"""依次打开每个功能页并截图，用于人工检查界面。

用法: python tools/shot_pages.py [输出目录]
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QPalette, QColor
from PySide6.QtWidgets import QApplication

from ui import theme
from ui.context import AppContext
from ui.main_window import MainWindow, PAGES


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "shots")
    os.makedirs(out_dir, exist_ok=True)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.load_stylesheet())
    settings = theme.load_settings()
    f = QFont()
    f.setPointSize(int(settings.get("font_size", 13) or 13))
    app.setFont(f)

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.COLORS["bg"]))
    pal.setColor(QPalette.WindowText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Base, QColor("#252526"))
    pal.setColor(QPalette.Text, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Button, QColor(theme.COLORS["bg_toolbar"]))
    pal.setColor(QPalette.ButtonText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Highlight, QColor(theme.COLORS["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)

    ctx = AppContext(settings)
    win = MainWindow(ctx)
    win.resize(1440, 900)
    win.show()

    state = {"i": 0}
    keys = [k for k, _t, _i in PAGES]

    def step():
        i = state["i"]
        if i >= len(keys):
            win.close()
            app.quit()
            return
        key = keys[i]
        title = dict((k, t) for k, t, _x in PAGES)[key]
        try:
            win._switch_page(key)
        except Exception as e:  # noqa: BLE001
            print("页面 %s 打开失败: %s" % (key, e))
        app.processEvents()
        QTimer.singleShot(900, lambda: grab(key, title, i))

    def grab(key, title, i):
        try:
            pm = win.grab()
            path = os.path.join(out_dir, "%02d_%s.png" % (i + 1, key))
            pm.save(path, "PNG")
            print("已截图 %-10s -> %s" % (key, path))
        except Exception as e:  # noqa: BLE001
            print("截图失败 %s: %s" % (key, e))
        state["i"] = i + 1
        QTimer.singleShot(200, step)

    QTimer.singleShot(1200, step)
    rc = app.exec()
    print("完成，输出目录:", out_dir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
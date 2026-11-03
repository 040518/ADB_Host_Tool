# -*- coding: utf-8 -*-
"""设置页：外观、终端、adb、日志、布局。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        s = ctx.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel("设置")
        title.setStyleSheet("font-size:16px;font-weight:600;color:#E8E8E8;")
        root.addWidget(title)

        # 表单区固定宽度，避免控件被拉伸成长条
        holder = QWidget()
        holder.setMaximumWidth(700)
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        root.addWidget(holder, 0, Qt.AlignLeft | Qt.AlignTop)
        root.addStretch(1)

        # ---- 外观 ----
        gb_ui = QGroupBox("外观")
        f1 = QFormLayout(gb_ui)
        f1.setContentsMargins(14, 16, 14, 14)
        f1.setSpacing(8)

        self.cmb_theme = QComboBox()
        self.cmb_theme.addItems(["深色（默认）"])
        self.cmb_theme.setFixedWidth(220)
        f1.addRow("主题", self.cmb_theme)

        self.sp_font = QSpinBox()
        self.sp_font.setRange(9, 22)
        self.sp_font.setValue(int(s.get("font_size", 13)))
        self.sp_font.setFixedWidth(220)
        f1.addRow("界面字号", self.sp_font)

        self.cmb_mono = QComboBox()
        self.cmb_mono.setEditable(True)
        self.cmb_mono.addItems(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"])
        self.cmb_mono.setCurrentText(s.get("mono_font_family") or "Cascadia Mono")
        self.cmb_mono.setFixedWidth(220)
        f1.addRow("等宽字体", self.cmb_mono)

        self.sp_mono = QSpinBox()
        self.sp_mono.setRange(8, 20)
        self.sp_mono.setValue(int(s.get("mono_font_size", 12)))
        self.sp_mono.setFixedWidth(220)
        f1.addRow("终端字号", self.sp_mono)
        outer.addWidget(gb_ui)

        # ---- ADB ----
        gb_adb = QGroupBox("ADB")
        f2 = QFormLayout(gb_adb)
        f2.setContentsMargins(14, 16, 14, 14)
        f2.setSpacing(8)
        row = QHBoxLayout()
        self.ed_adb = QLineEdit(s.get("adb_path", "") or self.ctx.adb.exe)
        self.ed_adb.setMinimumWidth(300)
        self.ed_adb.setCursorPosition(0)
        row.addWidget(self.ed_adb, 1)
        b = QPushButton("浏览…")
        b.setObjectName("Secondary")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        wrap = QWidget()
        wrap.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        wrap.setMaximumWidth(520)
        f2.addRow("adb 路径", wrap)
        wrap.setMaximumWidth(520)

        self.sp_refresh = QSpinBox()
        self.sp_refresh.setRange(0, 120)
        self.sp_refresh.setValue(int(s.get("auto_refresh_sec", 6)))
        self.sp_refresh.setSuffix(" 秒（0=关闭）")
        self.sp_refresh.setFixedWidth(220)
        f2.addRow("设备自动刷新", self.sp_refresh)
        outer.addWidget(gb_adb)

        # ---- 终端 ----
        gb_term = QGroupBox("终端")
        f3 = QFormLayout(gb_term)
        f3.setContentsMargins(14, 16, 14, 14)
        f3.setSpacing(8)
        self.cmb_term = QComboBox()
        self.cmb_term.addItems(["xterm-256color", "xterm", "vt100", "linux"])
        self.cmb_term.setCurrentText(s.get("terminal_type", "xterm-256color"))
        self.cmb_term.setFixedWidth(220)
        f3.addRow("终端类型", self.cmb_term)
        outer.addWidget(gb_term)

        # ---- 日志 ----
        gb_log = QGroupBox("日志")
        f4 = QFormLayout(gb_log)
        f4.setContentsMargins(14, 16, 14, 14)
        f4.setSpacing(8)
        self.sp_logmax = QSpinBox()
        self.sp_logmax.setRange(500, 100000)
        self.sp_logmax.setSingleStep(500)
        self.sp_logmax.setValue(int(s.get("log_max_lines", 5000)))
        self.sp_logmax.setFixedWidth(220)
        f4.addRow("最大行数", self.sp_logmax)
        self.chk_autoscroll = QCheckBox("自动滚动到最新")
        self.chk_autoscroll.setChecked(bool(s.get("log_autoscroll", True)))
        f4.addRow("", self.chk_autoscroll)
        outer.addWidget(gb_log)

        # ---- 按钮 ----
        btns = QHBoxLayout()
        b_save = QPushButton("保存设置")
        b_save.setIcon(theme.load_icon("save"))
        b_save.clicked.connect(self.save)
        btns.addWidget(b_save)

        b_reset = QPushButton("恢复默认布局")
        b_reset.setObjectName("Secondary")
        b_reset.clicked.connect(self.reset_layout)
        btns.addWidget(b_reset)

        btns.addStretch(1)
        self.lbl_msg = QLabel("")
        self.lbl_msg.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        btns.addWidget(self.lbl_msg)
        outer.addLayout(btns)
        outer.addStretch(1)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 adb", self.ed_adb.text(),
                                           "adb (adb.exe adb);;所有文件 (*)")
        if p:
            self.ed_adb.setText(p)

    def save(self):
        s = self.ctx.settings
        s["font_size"] = self.sp_font.value()
        s["mono_font_family"] = self.cmb_mono.currentText().strip()
        s["mono_font_size"] = self.sp_mono.value()
        s["adb_path"] = self.ed_adb.text().strip()
        s["auto_refresh_sec"] = self.sp_refresh.value()
        s["terminal_type"] = self.cmb_term.currentText()
        s["log_max_lines"] = self.sp_logmax.value()
        s["log_autoscroll"] = self.chk_autoscroll.isChecked()

        ok, path = theme.save_settings(s)
        if ok:
            self.lbl_msg.setText("已保存到 %s" % path)
            self.lbl_msg.setStyleSheet("color:%s;" % theme.COLORS["ok"])
            self.ctx.log("设置已保存: %s" % path, "ok")
            # 立即应用
            win = self.window()
            if hasattr(win, "apply_settings"):
                win.apply_settings()
            else:
                self._apply_inline()
        else:
            self.lbl_msg.setText("保存失败: %s" % path)
            self.lbl_msg.setStyleSheet("color:%s;" % theme.COLORS["error"])

    def _apply_inline(self):
        f = QFont()
        f.setPointSize(self.sp_font.value())
        self.window().setFont(f)

    def reset_layout(self):
        win = self.window()
        try:
            win.splitter_main.setSizes([270, 1130])
            win.splitter_vert.setSizes([700, 190])
        except Exception:  # noqa: BLE001
            pass
        self.lbl_msg.setText("布局已恢复默认")
        self.lbl_msg.setStyleSheet("color:%s;" % theme.COLORS["ok"])
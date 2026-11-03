# -*- coding: utf-8 -*-
"""左侧资源管理器：设备列表 + 当前设备信息 + 快捷操作。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from ui import theme
from ui.context import AppContext


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("HLine")
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    return f


class DeviceExplorer(QWidget):
    """VS Code Explorer 风格的设备侧边栏。"""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("Sidebar")
        # QWidget 子类默认不绘制 QSS 背景，必须开启此属性
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(220)
        self.setMaximumWidth(460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        title = QLabel("设备资源管理器")
        title.setObjectName("PanelTitle")
        lay.addWidget(title)

        # ---- 当前设备卡片 ----
        card = QWidget()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(10, 4, 10, 8)
        cl.setSpacing(3)

        self.lbl_cur = QLabel("未选择设备")
        self.lbl_cur.setObjectName("DeviceName")
        self.lbl_cur.setWordWrap(True)
        cl.addWidget(self.lbl_cur)

        self.lbl_state = QLabel("状态：—")
        self.lbl_state.setObjectName("DeviceMeta")
        cl.addWidget(self.lbl_state)

        self.lbl_mode = QLabel("模式：本地 adb")
        self.lbl_mode.setObjectName("DeviceMeta")
        cl.addWidget(self.lbl_mode)

        lay.addWidget(card)
        lay.addWidget(hline())

        # ---- 设备列表 ----
        t2 = QLabel("可用设备")
        t2.setObjectName("PanelTitle")
        lay.addWidget(t2)

        self.list = QListWidget()
        self.list.setIconSize(QSize(16, 16))
        self.list.setAlternatingRowColors(False)
        self.list.itemSelectionChanged.connect(self._item_selected)
        lay.addWidget(self.list, 1)

        lay.addWidget(hline())

        # ---- 快捷操作 ----
        t3 = QLabel("操作")
        t3.setObjectName("PanelTitle")
        lay.addWidget(t3)

        ops = QWidget()
        ol = QVBoxLayout(ops)
        ol.setContentsMargins(10, 2, 10, 10)
        ol.setSpacing(5)

        self.btn_refresh = QPushButton("刷新设备")
        self.btn_refresh.setObjectName("Secondary")
        self.btn_refresh.setIcon(theme.load_icon("refresh"))
        self.btn_refresh.clicked.connect(lambda: self.ctx.refresh_devices())
        ol.addWidget(self.btn_refresh)

        self.btn_restart = QPushButton("重启 adb 服务")
        self.btn_restart.setObjectName("Secondary")
        self.btn_restart.setIcon(theme.load_icon("plug"))
        self.btn_restart.clicked.connect(self._restart)
        ol.addWidget(self.btn_restart)

        self.btn_term = QPushButton("打开终端")
        self.btn_term.setIcon(theme.load_icon("terminal"))
        self.btn_term.clicked.connect(lambda: self._goto("terminal"))
        ol.addWidget(self.btn_term)

        self.btn_files = QPushButton("文件管理")
        self.btn_files.setObjectName("Secondary")
        self.btn_files.setIcon(theme.load_icon("files"))
        self.btn_files.clicked.connect(lambda: self._goto("files"))
        ol.addWidget(self.btn_files)

        lay.addWidget(ops)

        ctx.devices_changed.connect(self._on_devices)
        ctx.device_selected.connect(self._on_device)
        ctx.remote_changed.connect(lambda _x: self._refresh_mode())

    # ---- 槽 ------------------------------------------------------------
    def _goto(self, key: str):
        win = self.window()
        if hasattr(win, "_switch_page"):
            win._switch_page(key)

    def _restart(self):
        win = self.window()
        if hasattr(win, "restart_adb_server"):
            win.restart_adb_server()

    def _on_devices(self, devs):
        self.list.blockSignals(True)
        self.list.clear()
        for d in devs:
            icon = theme.load_icon("device", theme.COLORS["ok"] if d.usable else theme.COLORS["error"], 16)
            state_txt = "在线" if d.usable else d.state
            it = QListWidgetItem(icon, "%s\n%s" % (d.serial, state_txt))
            it.setData(Qt.UserRole, d)
            it.setToolTip(d.raw or d.serial)
            self.list.addItem(it)
        self.list.blockSignals(False)
        # 选中当前设备
        if self.ctx.current:
            for i in range(self.list.count()):
                d = self.list.item(i).data(Qt.UserRole)
                if d and d.serial == self.ctx.current.serial:
                    self.list.setCurrentRow(i)
                    break
        elif self.list.count():
            self.list.setCurrentRow(0)

    def _item_selected(self):
        it = self.list.currentItem()
        if it is None:
            return
        d = it.data(Qt.UserRole)
        if d is not None and (self.ctx.current is None or self.ctx.current.serial != d.serial):
            self.ctx.select_device(d)

    def _on_device(self, dev):
        if dev is None:
            self.lbl_cur.setText("未选择设备")
            self.lbl_state.setText("状态：—")
        else:
            self.lbl_cur.setText(dev.model or dev.serial)
            ok = dev.usable
            self.lbl_state.setText("状态：%s" % ("在线" if ok else dev.state))
            self.lbl_state.setStyleSheet("color:%s;" % (theme.COLORS["ok"] if ok else theme.COLORS["error"]))
            self.lbl_state.setObjectName("DeviceMeta")
        self._refresh_mode()

    def _refresh_mode(self):
        if self.ctx.is_remote and self.ctx.remote:
            self.lbl_mode.setText("模式：远程 SSH\n%s@%s"
                                  % (self.ctx.remote.user, self.ctx.remote.host))
            self.lbl_mode.setStyleSheet("color:%s;" % theme.COLORS["accent_light"])
        else:
            self.lbl_mode.setText("模式：本地 adb")
            self.lbl_mode.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        self.lbl_mode.setObjectName("DeviceMeta")
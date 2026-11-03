# -*- coding: utf-8 -*-
"""设备管理页：设备卡片、详细信息、无线连接、adb 路径设置。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext


class DeviceCard(QFrame):
    """单个设备的卡片。"""

    def __init__(self, dev: ab.DeviceInfo, current: bool = False, parent=None):
        super().__init__(parent)
        self.dev = dev
        self.setObjectName("DeviceCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame#DeviceCard{background:%s;border:1px solid %s;border-radius:6px;}"
            % (theme.COLORS["bg_sidebar"],
               theme.COLORS["accent"] if current else theme.COLORS["border"]))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(theme.load_pixmap("device",
                                        theme.COLORS["ok"] if dev.usable else theme.COLORS["error"], 30))
        icon.setFixedSize(34, 34)
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setSpacing(2)
        name = QLabel(dev.model or dev.serial)
        name.setObjectName("DeviceName")
        col.addWidget(name)

        meta = QLabel("序列号 %s   ·   %s" % (dev.serial, dev.raw or dev.state))
        meta.setObjectName("DeviceMeta")
        meta.setWordWrap(True)
        col.addWidget(meta)
        lay.addLayout(col, 1)

        badge = QLabel("在线" if dev.usable else dev.state)
        badge.setObjectName("BadgeOk" if dev.usable else "BadgeErr")
        lay.addWidget(badge, 0, Qt.AlignTop)

        self.setMinimumHeight(74)


class DevicePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("设备管理")
        title.setStyleSheet("font-size:16px;font-weight:600;color:#E8E8E8;")
        head.addWidget(title)
        head.addStretch(1)
        self.lbl_count = QLabel("0 个设备")
        self.lbl_count.setObjectName("Hint")
        head.addWidget(self.lbl_count)
        outer.addLayout(head)

        # 设备卡片滚动区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.card_host = QWidget()
        self.card_lay = QVBoxLayout(self.card_host)
        self.card_lay.setContentsMargins(0, 0, 0, 0)
        self.card_lay.setSpacing(8)
        self.card_lay.addStretch(1)
        self.scroll.setWidget(self.card_host)
        outer.addWidget(self.scroll, 1)

        # 连接方式
        gb_wifi = QGroupBox("无线 / TCP 连接")
        gl = QHBoxLayout(gb_wifi)
        gl.setContentsMargins(12, 14, 12, 12)
        gl.setSpacing(8)
        self.ed_wifi = QLineEdit()
        self.ed_wifi.setPlaceholderText("IP:端口，例如 192.168.1.100:5555")
        gl.addWidget(self.ed_wifi, 1)
        b1 = QPushButton("连接")
        b1.setIcon(theme.load_icon("network"))
        b1.clicked.connect(self._wifi_connect)
        gl.addWidget(b1)
        b2 = QPushButton("断开")
        b2.setObjectName("Secondary")
        b2.clicked.connect(self._wifi_disconnect)
        gl.addWidget(b2)
        outer.addWidget(gb_wifi)

        # adb 配置
        gb_adb = QGroupBox("ADB 配置")
        al = QGridLayout(gb_adb)
        al.setContentsMargins(12, 14, 12, 12)
        al.setHorizontalSpacing(8)
        al.setVerticalSpacing(8)
        al.addWidget(QLabel("adb 可执行文件"), 0, 0)
        self.ed_adb = QLineEdit(self.ctx.adb.exe)
        al.addWidget(self.ed_adb, 0, 1)
        b_browse = QPushButton("浏览…")
        b_browse.setObjectName("Secondary")
        b_browse.clicked.connect(self._browse_adb)
        al.addWidget(b_browse, 0, 2)
        b_apply = QPushButton("应用")
        b_apply.clicked.connect(self._apply_adb)
        al.addWidget(b_apply, 0, 3)

        al.addWidget(QLabel("adb 版本"), 1, 0)
        self.lbl_ver = QLabel("检测中…")
        self.lbl_ver.setObjectName("Hint")
        al.addWidget(self.lbl_ver, 1, 1, 1, 3)

        b_restart = QPushButton("重启 adb 服务")
        b_restart.setObjectName("Secondary")
        b_restart.setIcon(theme.load_icon("plug"))
        b_restart.clicked.connect(self._restart)
        al.addWidget(b_restart, 2, 1, 1, 1, Qt.AlignLeft)
        al.setColumnStretch(1, 1)
        outer.addWidget(gb_adb)

        ctx.devices_changed.connect(self._on_devices)
        ctx.device_selected.connect(lambda d: self._on_devices(self.ctx.devices))
        ctx.remote_changed.connect(lambda _x: self._load_version())
        self._load_version()

    # ---- 数据 ----------------------------------------------------------
    def _on_devices(self, devs):
        # 清空旧卡片（保留末尾 stretch）
        while self.card_lay.count() > 1:
            it = self.card_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        if not devs:
            empty = QLabel("未发现设备\n\n"
                           "· USB：确认数据线已连接，设备已开启调试授权\n"
                           "· 无线：用上方输入 IP:端口 后点「连接」\n"
                           "· 板子插在 Ubuntu 上：用右上角「SSH 连接 Ubuntu…」")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:%s;padding:40px;" % theme.COLORS["text_dim"])
            self.card_lay.insertWidget(0, empty)
        else:
            for i, d in enumerate(devs):
                cur = bool(self.ctx.current and self.ctx.current.serial == d.serial)
                self.card_lay.insertWidget(i, DeviceCard(d, cur))

        self.lbl_count.setText("%d 个设备" % len(devs))

    def _load_version(self):
        def work():
            if self.ctx.remote is not None:
                return self.ctx.remote.adb_version()
            ok, ver = self.ctx.adb.version()
            return ver if ok else ""

        def done(res, err):
            txt = (res or "").strip() if not err else ""
            if txt:
                first = txt.splitlines()
                self.lbl_ver.setText("  |  ".join(first[:2]))
            else:
                self.lbl_ver.setText("未检测到可用的 adb")

        self.ctx.run_async(work, done)

    # ---- 动作 ----------------------------------------------------------
    def _wifi_connect(self):
        addr = self.ed_wifi.text().strip()
        if not addr:
            return
        if ":" not in addr:
            addr += ":5555"

        def work():
            if self.ctx.remote is not None:
                return self.ctx.remote.adb(["connect", addr], timeout=25)
            return self.ctx.adb.connect_tcp(addr)

        def done(res, err):
            self.ctx.log("adb connect %s: %s" % (addr, res if not err else err))
            self.ctx.refresh_devices()

        self.ctx.run_async(work, done, busy_text="连接无线设备…")

    def _wifi_disconnect(self):
        addr = self.ed_wifi.text().strip()
        if not addr:
            return

        def work():
            if self.ctx.remote is not None:
                return self.ctx.remote.adb(["disconnect", addr], timeout=20)
            return self.ctx.adb.disconnect_tcp(addr)

        def done(res, err):
            self.ctx.log("adb disconnect %s: %s" % (addr, res if not err else err))
            self.ctx.refresh_devices()

        self.ctx.run_async(work, done)

    def _browse_adb(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 adb 可执行文件", self.ed_adb.text(),
                                              "adb (adb.exe adb);;所有文件 (*)")
        if path:
            self.ed_adb.setText(path)

    def _apply_adb(self):
        path = self.ed_adb.text().strip()
        if path and not __import__("os").path.isfile(path):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "adb 路径", "文件不存在：\n%s" % path)
            return
        self.ctx.adb = ab.Adb(path or ab.find_adb())
        self.ctx.settings["adb_path"] = path
        self.ctx.log("切换 adb: %s" % self.ctx.adb.exe)
        self._load_version()
        self.ctx.refresh_devices()

    def _restart(self):
        win = self.window()
        if hasattr(win, "restart_adb_server"):
            win.restart_adb_server()
# -*- coding: utf-8 -*-
"""主窗口：VS Code 风格布局（菜单 / 工具栏 / ActivityBar / 侧边栏 / 中央标签页 / 日志面板 / 状态栏）。"""

from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
                               QStatusBar, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from ui import theme
from ui.context import AppContext
from ui.widgets import FlatSplitter

# 页面在首次点击时才创建，避免启动变慢
PAGES = [
    ("device",   "设备管理", "devices"),
    ("terminal", "交互终端", "terminal"),
    ("command",  "命令执行", "command"),
    ("data",     "数据收发", "data"),
    ("serial",   "串口助手", "wave"),
    ("tcp",      "TCP 通道", "tcp"),
    ("files",    "文件管理", "files"),
    ("logcat",   "Logcat",  "log"),
    ("shot",     "截图",     "camera"),
    ("settings", "设置",     "settings"),
]


class ActivityBar(QWidget):
    """最左侧图标导航栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ActivityBar")
        # QWidget 子类默认不绘制 QSS 背景，必须开启此属性，否则会透出桌面
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(52)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(2)
        self.buttons = {}
        self.group = None
        for key, title, icon in PAGES:
            btn = QToolButton(self)
            btn.setObjectName("ActivityButton")
            btn.setToolTip(title)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setIconSize(QSize(22, 22))
            btn.setFixedSize(52, 46)
            btn.setProperty("pageKey", key)
            btn.setIcon(theme.load_icon(icon, "", 22))
            lay.addWidget(btn)
            self.buttons[key] = btn
        lay.addStretch(1)

    def set_current(self, key: str):
        for k, b in self.buttons.items():
            b.setChecked(k == key)
            b.setIcon(theme.load_icon(dict((p[0], p[2]) for p in PAGES)[k],
                                      theme.COLORS["accent_light"] if k == key else "",
                                      22))


class LogPanel(QWidget):
    """底部日志面板：折叠 / 清空 / 复制 / 自动滚动。"""

    LEVEL_COLORS = {
        "info": "#D4D4D4",
        "ok": "#4EC9B0",
        "warn": "#DCDCAA",
        "error": "#F44747",
        "cmd": "#4FC1FF",
    }

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("LogPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._autoscroll = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(30)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 6, 0)
        hl.setSpacing(6)
        self.title = QLabel("日志输出")
        self.title.setObjectName("PanelTitle")
        hl.addWidget(self.title)
        hl.addStretch(1)

        self.btn_auto = QPushButton("自动滚动")
        self.btn_auto.setObjectName("Flat")
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(True)
        self.btn_auto.setFixedHeight(22)
        self.btn_auto.clicked.connect(self._toggle_auto)
        hl.addWidget(self.btn_auto)

        for text, slot, tip in (("清空", self.clear, "清空日志"),
                                ("复制", self.copy_all, "复制全部日志")):
            b = QPushButton(text)
            b.setObjectName("Flat")
            b.setFixedHeight(22)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            hl.addWidget(b)

        self.btn_collapse = QPushButton("收起")
        self.btn_collapse.setObjectName("Flat")
        self.btn_collapse.setFixedWidth(44)
        self.btn_collapse.setToolTip("折叠 / 展开日志面板")
        self.btn_collapse.clicked.connect(self.toggle_collapse)
        hl.addWidget(self.btn_collapse)
        lay.addWidget(head)

        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(int(ctx.settings.get("log_max_lines", 5000)))
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.view, 1)

        ctx.log_message.connect(self.append)

    # ---- 行为 ----------------------------------------------------------
    def append(self, level: str, text: str):
        color = self.LEVEL_COLORS.get(level, self.LEVEL_COLORS["info"])
        esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.view.appendHtml('<span style="color:%s;white-space:pre">%s</span>' % (color, esc))
        if self._autoscroll:
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _toggle_auto(self):
        self._autoscroll = self.btn_auto.isChecked()

    def clear(self):
        self.view.clear()

    def copy_all(self):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self.view.toPlainText())

    def toggle_collapse(self):
        vis = self.view.isVisible()
        self.view.setVisible(not vis)
        self.btn_collapse.setText("展开" if vis else "收起")


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.pages = {}          # key -> widget
        self.page_index = {}     # key -> tab index
        self._current_page = "device"

        self.setWindowTitle("%s  v%s" % (theme.APP_NAME, theme.APP_VERSION))
        self.setWindowIcon(theme.app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.ctx.devices_changed.connect(self._on_devices)
        self.ctx.device_selected.connect(self._on_device)
        self.ctx.remote_changed.connect(self._on_remote)
        self.ctx.status_changed.connect(self._on_status)
        self.ctx.busy_changed.connect(self._on_busy)

        self._switch_page("device")
        QTimer.singleShot(150, self.ctx.refresh_devices)
        QTimer.singleShot(2500, self._greet)

        # 设备列表自动刷新
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_refresh)
        self.apply_settings()

    def place_on_screen(self, screen):
        """在首次显示前，将窗口边框和标题栏一起放入屏幕工作区。"""
        self.setScreen(screen)
        self.winId()  # 创建原生窗口以获取该屏幕缩放比例下的边框尺寸。
        # 留出边距，容纳 Windows 在分数缩放下还原窗口时的边框偏差。
        work_area = screen.availableGeometry().adjusted(12, 12, -12, -12)
        content_area = work_area.marginsRemoved(self.windowHandle().frameMargins())
        self.setMinimumSize(self.minimumSize().boundedTo(content_area.size()))
        geometry = QRect(QPoint(), self.size().boundedTo(content_area.size()))
        geometry.moveCenter(content_area.center())
        self.setGeometry(geometry)

    # ------------------------------------------------------------------ 菜单
    def _build_menu(self):
        mb = self.menuBar()

        m_file = mb.addMenu("文件(&F)")
        a = QAction("刷新设备列表", self)
        a.setShortcut(QKeySequence("F5"))
        a.triggered.connect(self.ctx.refresh_devices)
        m_file.addAction(a)
        m_file.addSeparator()
        m_file.addAction(QAction("退出", self, triggered=self.close))

        m_view = mb.addMenu("视图(&V)")
        for key, title, _ in PAGES:
            act = QAction(title, self)
            act.triggered.connect(lambda _c=False, k=key: self._switch_page(k))
            m_view.addAction(act)
        m_view.addSeparator()
        m_view.addAction(QAction("清空日志", self, triggered=lambda: self.log_panel.clear()))
        m_view.addAction(QAction("折叠日志面板", self, triggered=lambda: self.log_panel.toggle_collapse()))

        m_dev = mb.addMenu("设备(&D)")
        m_dev.addAction(QAction("刷新", self, triggered=self.ctx.refresh_devices))
        m_dev.addAction(QAction("重启 adb 服务", self, triggered=self.restart_adb_server))
        m_dev.addSeparator()
        m_dev.addAction(QAction("无线连接…", self, triggered=self._focus_wifi))

        m_help = mb.addMenu("帮助(&H)")
        m_help.addAction(QAction("关于", self, triggered=self._about))

    # ---------------------------------------------------------------- 工具栏
    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setObjectName("MainToolBar")
        tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        self.addToolBar(tb)

        # 应用标识
        logo = QLabel()
        logo.setPixmap(theme.load_pixmap("chip", theme.COLORS["accent_light"], 20))
        logo.setContentsMargins(2, 0, 6, 0)
        tb.addWidget(logo)
        name = QLabel(theme.APP_NAME)
        name.setStyleSheet("font-weight:600;color:#E8E8E8;font-size:13px;")
        tb.addWidget(name)
        tb.addSeparator()

        lbl = QLabel("设备")
        lbl.setStyleSheet("color:#9D9D9D;padding-left:2px;")
        tb.addWidget(lbl)
        self.cmb_dev = QComboBox()
        self.cmb_dev.setMinimumWidth(230)
        self.cmb_dev.setMaximumWidth(360)
        self.cmb_dev.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_dev.setMinimumContentsLength(18)
        self.cmb_dev.setPlaceholderText("未发现设备")
        self.cmb_dev.setToolTip("选择要操作的设备")
        self.cmb_dev.currentIndexChanged.connect(self._combo_changed)
        tb.addWidget(self.cmb_dev)

        act = QAction(theme.load_icon("refresh"), "刷新", self)
        act.setToolTip("刷新设备列表 (F5)")
        act.triggered.connect(self.ctx.refresh_devices)
        tb.addAction(act)

        act = QAction(theme.load_icon("plug"), "重启 adb 服务", self)
        act.setToolTip("重启 adb 服务（设备异常时使用）")
        act.triggered.connect(self.restart_adb_server)
        tb.addAction(act)

        tb.addSeparator()
        lbl = QLabel("无线")
        lbl.setStyleSheet("color:#9D9D9D;")
        tb.addWidget(lbl)
        self.ed_wifi = QComboBox()
        self.ed_wifi.setEditable(True)
        self.ed_wifi.setMinimumWidth(170)
        self.ed_wifi.setToolTip("输入 IP:端口 后点连接（如 192.168.1.100:5555）")
        self.ed_wifi.addItems(["192.168.1.100:5555"])
        tb.addWidget(self.ed_wifi)
        act = QAction(theme.load_icon("network"), "连接", self)
        act.setToolTip("通过 TCP 连接无线调试设备")
        act.triggered.connect(self._wifi_connect)
        tb.addAction(act)
        act = QAction(theme.load_icon("unplug"), "断开", self)
        act.setToolTip("断开无线设备")
        act.triggered.connect(self._wifi_disconnect)
        tb.addAction(act)

        # 右侧：SSH 远程
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.btn_ssh = QPushButton("  SSH 连接 Ubuntu…")
        self.btn_ssh.setIcon(theme.load_icon("ssh"))
        self.btn_ssh.setMinimumWidth(150)
        self.btn_ssh.setToolTip("连接 Ubuntu，在远端执行 adb（板子插在 Ubuntu 上时使用）")
        self.btn_ssh.clicked.connect(self._ssh_dialog)
        tb.addWidget(self.btn_ssh)

        self.btn_ssh_exit = QPushButton("退出远程")
        self.btn_ssh_exit.setObjectName("Secondary")
        self.btn_ssh_exit.setMinimumWidth(96)
        self.btn_ssh_exit.setEnabled(False)
        self.btn_ssh_exit.clicked.connect(self._ssh_exit)
        tb.addWidget(self.btn_ssh_exit)

    # ------------------------------------------------------------------ 主体
    def _build_body(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.activity = ActivityBar()
        root.addWidget(self.activity)
        for key, btn in self.activity.buttons.items():
            btn.clicked.connect(lambda _c=False, k=key: self._switch_page(k))

        self.splitter_main = FlatSplitter(Qt.Horizontal)
        self.splitter_main.setChildrenCollapsible(False)

        # 侧边栏
        from ui.sidebar import DeviceExplorer
        self.sidebar = DeviceExplorer(self.ctx)
        self.splitter_main.addWidget(self.sidebar)

        # 右侧：标签页 + 日志
        right = FlatSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.splitter_vert = right

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(False)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        right.addWidget(self.tabs)

        self.log_panel = LogPanel(self.ctx)
        right.addWidget(self.log_panel)
        right.setSizes([700, 190])
        self.splitter_vert = right

        self.splitter_main.addWidget(right)
        self.splitter_main.setSizes([270, 1130])
        self.splitter_main.setStretchFactor(0, 0)
        self.splitter_main.setStretchFactor(1, 1)
        root.addWidget(self.splitter_main, 1)

        self.setCentralWidget(central)

    # ---------------------------------------------------------------- 状态栏
    def _build_statusbar(self):
        sb = QStatusBar()
        sb.setSizeGripEnabled(True)
        self.setStatusBar(sb)

        def mk(text="", obj="StatusLabel"):
            lb = QLabel(text)
            lb.setObjectName(obj)
            return lb

        self.sb_state = mk("就绪")
        self.sb_devcount = mk("设备 0")
        self.sb_device = mk("未选择设备")
        self.sb_mode = mk("本地模式")
        self.sb_remoteinfo = mk("")
        self.sb_task = mk("")

        sb.addWidget(self.sb_state, 1)
        sb.addPermanentWidget(self.sb_task)
        sb.addPermanentWidget(self.sb_device)
        sb.addPermanentWidget(self.sb_devcount)
        sb.addPermanentWidget(self.sb_mode)
        sb.addPermanentWidget(self.sb_remoteinfo)
        self._status_default_style = "color:#FFFFFF;padding:2px 10px;"

    # ------------------------------------------------------------------ 页面
    def _ensure_page(self, key: str):
        if key in self.pages:
            return self.pages[key]
        from ui.device_page import DevicePage
        from ui.terminal_page import TerminalPage
        from ui.command_page import CommandPage
        from ui.data_page import DataPage
        from ui.serial_page import SerialPage
        from ui.tcp_page import TcpPage
        from ui.file_page import FilePage
        from ui.logcat_page import LogcatPage
        from ui.screenshot_page import ScreenshotPage
        from ui.settings_page import SettingsPage

        factories = {
            "device": DevicePage, "terminal": TerminalPage, "command": CommandPage,
            "data": DataPage, "serial": SerialPage, "tcp": TcpPage, "files": FilePage,
            "logcat": LogcatPage, "shot": ScreenshotPage, "settings": SettingsPage,
        }
        titles = {k: t for k, t, _ in PAGES}
        widget = factories[key](self.ctx)
        idx = self.tabs.addTab(widget, titles[key])
        self.pages[key] = widget
        self.page_index[key] = idx
        return widget

    def _switch_page(self, key: str):
        self._ensure_page(key)
        self._current_page = key
        self.tabs.setCurrentIndex(self.page_index[key])
        self.activity.set_current(key)
        self._apply_page_layout(key)
        try:
            self.ctx.settings["last_page"] = self.page_index[key]
        except Exception:  # noqa: BLE001
            pass

    def _tab_changed(self, idx: int):
        for key, i in self.page_index.items():
            if i == idx:
                self._current_page = key
                self.activity.set_current(key)
                self._apply_page_layout(key)
                break

    def _apply_page_layout(self, key: str):
        """串口页本身已有 VOFA+ 式参数侧栏，隐藏 ADB 设备树避免双侧栏挤压波形。"""
        serial_mode = key == "serial"
        self.sidebar.setVisible(not serial_mode)
        self.log_panel.setVisible(not serial_mode)
        if not serial_mode:
            width = max(800, self.splitter_main.width())
            self.splitter_main.setSizes([270, max(530, width - 270)])
            self.splitter_vert.setSizes([700, 190])

    # ------------------------------------------------------------------ 槽
    def _on_devices(self, devs):
        self.cmb_dev.blockSignals(True)
        self.cmb_dev.clear()
        for d in devs:
            self.cmb_dev.addItem(d.label(), d.serial)
        self.cmb_dev.blockSignals(False)
        self.sb_devcount.setText("设备 %d" % len(devs))
        if devs:
            usable = [d for d in devs if d.usable]
            target = usable[0] if usable else devs[0]
            self.cmb_dev.setCurrentIndex(devs.index(target))
            self.ctx.select_device(target)
        else:
            self.ctx.select_device(None)
            self._on_status("未发现设备，请检查 USB 连接 / 授权")

    def _combo_changed(self, idx: int):
        if 0 <= idx < len(self.ctx.devices):
            self.ctx.select_device(self.ctx.devices[idx])
            page = self.pages.get("files")
            if page is not None:
                page.refresh_remote()

    def _on_device(self, dev):
        if dev is None:
            self.sb_device.setText("SSH 目标可用 · 无 ADB" if self.ctx.remote else "未选择设备")
            self.sb_device.setStyleSheet("color:#FFE9A8;padding:2px 10px;")
        else:
            self.sb_device.setText(dev.label())
            self.sb_device.setStyleSheet("color:#FFFFFF;padding:2px 10px;")

    def _on_remote(self, on: bool):
        if on:
            self.sb_mode.setText("远程 SSH")
            info = ""
            if self.ctx.remote:
                info = "%s@%s" % (self.ctx.remote.user, self.ctx.remote.host)
            self.sb_remoteinfo.setText(info)
            self.sb_mode.setStyleSheet("color:#C7F0E4;padding:2px 10px;font-weight:600;")
            self.btn_ssh.setText(" 切换 Ubuntu…")
            self.btn_ssh_exit.setEnabled(True)
        else:
            self.sb_mode.setText("本地模式")
            self.sb_remoteinfo.setText("")
            self.sb_mode.setStyleSheet("color:#FFFFFF;padding:2px 10px;")
            self.btn_ssh.setText("  SSH 连接 Ubuntu…")
            self.btn_ssh_exit.setEnabled(False)
        self._on_device(self.ctx.current)
    def _on_status(self, text: str):
        self.sb_state.setText(text)

    def _on_busy(self, busy: bool, text: str):
        self.sb_task.setText(text if busy else "")
        if busy:
            self.ctx.status(text)

    def _greet(self):
        exe = self.ctx.adb.exe if self.ctx.adb else "adb"
        self.ctx.log("%s v%s 启动" % (theme.APP_NAME, theme.APP_VERSION))
        self.ctx.log("adb 可执行文件: %s" % exe)
        self.ctx.status("就绪")

    def apply_settings(self):
        """应用设置（字号、自动刷新间隔、日志行数等）。"""
        s = self.ctx.settings
        # 界面字号
        try:
            from PySide6.QtGui import QFont
            f = QFont()
            f.setPointSize(int(s.get("font_size", 13) or 13))
            self.setFont(f)
        except Exception:  # noqa: BLE001
            pass
        # 日志行数
        try:
            self.log_panel.view.setMaximumBlockCount(int(s.get("log_max_lines", 5000)))
        except Exception:  # noqa: BLE001
            pass
        # 自动刷新
        sec = int(s.get("auto_refresh_sec", 6) or 0)
        if sec > 0:
            self._auto_timer.start(sec * 1000)
        else:
            self._auto_timer.stop()

    def _auto_refresh(self):
        # 正在执行任务时不打扰
        if self.ctx._workers:
            return
        self.ctx.refresh_devices()

    # ------------------------------------------------------------------ 动作
    def restart_adb_server(self):
        def work():
            if self.ctx.remote is not None:
                rc, out = self.ctx.remote.exec("%s kill-server; sleep 1; %s start-server"
                                               % (self.ctx.remote.adb_remote, self.ctx.remote.adb_remote))
                return rc, out
            self.ctx.adb.kill_server()
            return self.ctx.adb.start_server()

        def done(res, err):
            if err:
                self.ctx.log("重启 adb 服务失败: %s" % err, "error")
            else:
                self.ctx.log("重启 adb 服务: %s" % (res,))
            self.ctx.refresh_devices()

        self.ctx.run_async(work, done, busy_text="重启 adb 服务…")

    def _wifi_connect(self):
        addr = self.ed_wifi.currentText().strip()
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
        addr = self.ed_wifi.currentText().strip()
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

    def _focus_wifi(self):
        self.ed_wifi.setFocus()

    # SSH 对话框
    def _ssh_dialog(self):
        from ui.ssh_dialog import SshDialog
        dlg = SshDialog(self.ctx, self)
        if dlg.exec():
            prof = dlg.result_profile()
            if prof:
                self.ctx.connect_remote(prof, on_done=lambda ok, _x: self._after_remote(ok))

    def _after_remote(self, ok: bool):
        if ok:
            self._on_remote(True)
            page = self.pages.get("files")
            if page is not None:
                page.on_remote_changed()

    def _ssh_exit(self):
        self.ctx.disconnect_remote()
        self._on_remote(False)
        page = self.pages.get("files")
        if page is not None:
            page.on_remote_changed()

    def _about(self):
        QMessageBox.information(
            self, "关于",
            "%s  v%s\n\n"
            "嵌入式设备调试工具，支持设备管理、交互终端、数据收发、串口波形、TCP 通道、\n"
            "双栏文件管理、logcat、截图，支持 SSH 远程驱动 Ubuntu 上的 adb。\n\n"
            "Python %s / PySide6 %s"
            % (theme.APP_NAME, theme.APP_VERSION,
               ".".join(map(str, __import__("sys").version_info[:3])),
               __import__("PySide6").__version__))

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, e):
        try:
            self.ctx.stop_all_workers()
            for page in self.pages.values():
                for m in ("shutdown",):
                    fn = getattr(page, m, None)
                    if callable(fn):
                        try:
                            fn()
                        except Exception:  # noqa: BLE001
                            pass
            if self.ctx.remote is not None:
                self.ctx.remote.close()
        finally:
            super().closeEvent(e)

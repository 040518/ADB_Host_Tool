# -*- coding: utf-8 -*-
"""交互终端页：支持多个独立、持久的 adb shell 标签。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from core import terminal_backend as tb
from ui import theme
from ui.context import AppContext
from ui.terminal_view import TerminalView

QUICK = [
    ("ls", "ls -la"),
    ("pwd", "pwd"),
    ("cd /tmp", "cd /tmp && pwd"),
    ("系统信息", "uname -a"),
    ("进程", "ps -A 2>/dev/null | head -20"),
    ("磁盘", "df -h"),
    ("网络", "ip -4 addr 2>/dev/null || ifconfig"),
    ("环境变量", "env | sort | head -30"),
]


class TerminalPane(QWidget):
    """一个标签对应一个 TerminalView 和一条独立会话。"""

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.number = number
        self.session = None
        self.connecting = False
        self.closed = False
        self.generation = 0
        self.state_text = "未连接"
        self.state_color = theme.COLORS["text_dim"]
        self.pending_commands = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = TerminalView(None, self)
        self.view.setMinimumHeight(260)
        lay.addWidget(self.view, 1)

    @property
    def title(self):
        return "终端 %d" % self.number


class TerminalPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._next_number = 1

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_new = QPushButton("新建终端")
        self.btn_new.setIcon(theme.load_icon("plus"))
        self.btn_new.clicked.connect(lambda: self.new_terminal(auto_connect=True))
        bar.addWidget(self.btn_new)

        self.btn_conn = QPushButton("连接当前")
        self.btn_conn.setIcon(theme.load_icon("terminal"))
        self.btn_conn.clicked.connect(self.connect_terminal)
        bar.addWidget(self.btn_conn)

        self.btn_disc = QPushButton("断开当前")
        self.btn_disc.setObjectName("Secondary")
        self.btn_disc.setEnabled(False)
        self.btn_disc.clicked.connect(self.disconnect_terminal)
        bar.addWidget(self.btn_disc)

        for text, slot in (("清屏", self.clear), ("复制", self.copy), ("粘贴", self.paste)):
            button = QPushButton(text)
            button.setObjectName("Secondary")
            button.clicked.connect(slot)
            bar.addWidget(button)

        bar.addSpacing(8)
        bar.addWidget(QLabel("终端类型"))
        self.cmb_term = QComboBox()
        self.cmb_term.addItems(["xterm-256color", "xterm", "vt100", "linux"])
        self.cmb_term.setCurrentText(ctx.settings.get("terminal_type", "xterm-256color"))
        self.cmb_term.setFixedWidth(150)
        bar.addWidget(self.cmb_term)

        bar.addStretch(1)
        self.lbl_count = QLabel("0 个终端")
        self.lbl_count.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        bar.addWidget(self.lbl_count)
        self.lbl_state = QLabel("未连接")
        self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        bar.addWidget(self.lbl_state)
        lay.addLayout(bar)

        quick = QHBoxLayout()
        quick.setSpacing(4)
        label = QLabel("快捷")
        label.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        quick.addWidget(label)
        for text, command in QUICK:
            button = QPushButton(text)
            button.setObjectName("Flat")
            button.setFixedHeight(24)
            button.clicked.connect(lambda _checked=False, c=command: self.run_command(c))
            quick.addWidget(button)
        quick.addStretch(1)
        lay.addLayout(quick)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._current_changed)
        self.tabs.tabCloseRequested.connect(self.close_terminal)
        lay.addWidget(self.tabs, 1)

        hint = QLabel("每个标签是一条独立持久 shell，可同时运行不同命令。支持 Tab 补全、"
                      "Ctrl+C、vi/vim、python3；鼠标拖选，Ctrl+Shift+C 复制，"
                      "右键在有选区时复制、无选区时粘贴；滚轮/右侧滚动条可回看历史。")
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        self.new_terminal(auto_connect=False)

        if not tb.HAS_PYTE:
            self.ctx.log("未安装 pyte，终端为简化显示模式（vim 等全屏程序显示不完整）", "warn")

        ctx.remote_changed.connect(lambda _x: self._on_mode_changed())

    # ---- 兼容旧调用：始终指向当前标签 -------------------------------
    @property
    def current_pane(self):
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, TerminalPane) else None

    @property
    def session(self):
        pane = self.current_pane
        return pane.session if pane is not None else None

    @property
    def view(self):
        pane = self.current_pane
        return pane.view if pane is not None else None

    def panes(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())
                if isinstance(self.tabs.widget(i), TerminalPane)]

    # ---- 标签 --------------------------------------------------------
    def new_terminal(self, auto_connect=True):
        pane = TerminalPane(self._next_number, self)
        self._next_number += 1
        index = self.tabs.addTab(pane, pane.title)
        self.tabs.setCurrentIndex(index)
        self._update_count()
        self._refresh_toolbar()
        if auto_connect:
            self.connect_terminal(pane)
        return pane

    def close_terminal(self, index):
        pane = self.tabs.widget(index)
        if not isinstance(pane, TerminalPane):
            return
        pane.closed = True
        pane.generation += 1
        sess = pane.session
        pane.session = None
        pane.view.set_session(None)
        self.tabs.removeTab(index)
        pane.deleteLater()
        if sess is not None:
            self.ctx.run_async(lambda: sess.stop(), lambda _r, _e: None)
        if self.tabs.count() == 0:
            self.new_terminal(auto_connect=False)
        self._update_count()
        self._refresh_toolbar()

    def _current_changed(self, _index):
        self._refresh_toolbar()
        pane = self.current_pane
        if pane is not None:
            pane.view.setFocus()

    def _update_count(self):
        connected = sum(1 for pane in self.panes()
                        if pane.session is not None and getattr(pane.session, "alive", False))
        self.lbl_count.setText("%d 个终端 · %d 已连接" % (self.tabs.count(), connected))

    # ---- 状态 --------------------------------------------------------
    def _set_pane_state(self, pane, text, color=""):
        if pane is None:
            return
        pane.state_text = text
        pane.state_color = color or theme.COLORS["text_dim"]
        if pane is self.current_pane:
            self._refresh_toolbar()

    def _refresh_toolbar(self):
        pane = self.current_pane
        if pane is None:
            self.btn_conn.setEnabled(False)
            self.btn_disc.setEnabled(False)
            self.lbl_state.setText("无终端")
            return
        alive = pane.session is not None and getattr(pane.session, "alive", False)
        self.btn_conn.setEnabled(not pane.connecting)
        self.btn_conn.setText("聚焦当前" if alive else
                              ("连接中…" if pane.connecting else "连接当前"))
        self.btn_disc.setEnabled(alive or pane.connecting)
        self.lbl_state.setText(pane.state_text)
        self.lbl_state.setStyleSheet("color:%s;" % pane.state_color)
        self._update_count()

    def _on_mode_changed(self):
        self.disconnect_all(async_stop=True)
        for pane in self.panes():
            self._set_pane_state(pane, "模式已切换，请重新连接")

    # ---- 连接 --------------------------------------------------------
    def connect_terminal(self, pane=None):
        pane = pane if isinstance(pane, TerminalPane) else self.current_pane
        if pane is None or pane.closed or pane.connecting:
            return
        if pane.session is not None and getattr(pane.session, "alive", False):
            pane.view.setFocus()
            return
        if self.ctx.current is None:
            QMessageBox.warning(self, "提示", "请先选择/连接一个设备")
            return

        pane.connecting = True
        pane.generation += 1
        generation = pane.generation
        self._set_pane_state(pane, "连接中…", theme.COLORS["warn"])
        self._refresh_toolbar()
        current = self.ctx.current
        remote = self.ctx.remote
        adb_exe = self.ctx.adb.exe
        grid_rows, grid_cols = pane.view._grid
        grid_rows = grid_rows if grid_rows > 0 else 30
        grid_cols = grid_cols if grid_cols > 0 else 100

        def work():
            if remote is not None:
                sess = tb.RemoteAdbShellSession(
                    remote, current.serial, rows=grid_rows, cols=grid_cols)
            else:
                sess = tb.TerminalSession(
                    adb_exe, current.serial, rows=grid_rows, cols=grid_cols)
            sess.start()
            return sess

        def done(sess, err):
            pane.connecting = False
            if pane.closed or generation != pane.generation:
                if sess is not None:
                    self.ctx.run_async(lambda: sess.stop(), lambda _r, _e: None)
                return
            if err:
                self._set_pane_state(pane, "连接失败", theme.COLORS["error"])
                self.ctx.log("终端连接失败: %s" % err, "error")
                QMessageBox.critical(self, "连接终端", "无法启动终端：\n%s" % err)
                return
            pane.session = sess
            pane.view.set_session(sess)
            pane.view.setFocus()
            mode = "设备终端（经 SSH）" if remote is not None else "设备终端"
            self._set_pane_state(pane, "已连接 · %s" % mode, theme.COLORS["ok"])
            self.ctx.log("%s 已连接: %s (%s)" % (pane.title, current.serial, mode))
            pending = list(pane.pending_commands)
            pane.pending_commands.clear()
            for command in pending:
                sess.send_line(command)
            self._update_count()

        self.ctx.run_async(work, done, busy_text="打开%s…" % pane.title)

    def disconnect_terminal(self, pane=None):
        pane = pane if isinstance(pane, TerminalPane) else self.current_pane
        if pane is None:
            return
        sess = pane.session
        pane.generation += 1
        pane.session = None
        pane.connecting = False
        pane.pending_commands.clear()
        pane.view.set_session(None)
        self._set_pane_state(pane, "已断开")
        if sess is not None:
            self.ctx.run_async(lambda: sess.stop(), lambda _r, _e: None)
        self._update_count()

    def disconnect_all(self, async_stop=True):
        sessions = []
        for pane in self.panes():
            pane.generation += 1
            if pane.session is not None:
                sessions.append(pane.session)
            pane.session = None
            pane.connecting = False
            pane.pending_commands.clear()
            pane.view.set_session(None)
            self._set_pane_state(pane, "已断开")
        if sessions:
            def stop_all():
                for sess in sessions:
                    try:
                        sess.stop()
                    except Exception:  # noqa: BLE001
                        pass
            if async_stop:
                self.ctx.run_async(stop_all, lambda _r, _e: None)
            else:
                stop_all()
        self._update_count()

    # ---- 当前标签操作 ------------------------------------------------
    def run_command(self, cmd: str):
        pane = self.current_pane
        if pane is None:
            pane = self.new_terminal(auto_connect=False)
        if pane.session is None or not getattr(pane.session, "alive", False):
            pane.pending_commands.append(cmd)
            self.connect_terminal(pane)
            return
        pane.session.send_line(cmd)
        pane.view.setFocus()

    def _send(self, cmd: str):
        pane = self.current_pane
        if pane is not None and pane.session is not None:
            pane.session.send_line(cmd)
            pane.view.setFocus()

    def clear(self):
        pane = self.current_pane
        if pane is not None and pane.session is not None:
            pane.session.write_text("clear\r")

    def copy(self):
        pane = self.current_pane
        if pane is not None:
            if pane.view.copy_selection():
                self.ctx.status("已复制终端选中内容")
            else:
                self.ctx.status("请先用鼠标拖选终端文字")

    def paste(self):
        pane = self.current_pane
        if pane is not None:
            pane.view.paste()

    def shutdown(self):
        for pane in self.panes():
            pane.closed = True
        self.disconnect_all(async_stop=False)

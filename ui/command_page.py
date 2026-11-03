# -*- coding: utf-8 -*-
"""命令执行页：单条命令执行 + 实时回显（VS Code 输出面板风格）。"""

from __future__ import annotations

import threading

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext

PRESETS = [
    ("设备信息", "uname -a; id; df -h | head -20"),
    ("目录列表", "ls -la"),
    ("进程列表", "ps -A 2>/dev/null | head -30 || ps aux | head -30"),
    ("挂载信息", "cat /proc/mounts"),
    ("网络接口", "ip -4 addr 2>/dev/null || ifconfig"),
    ("系统时间", "date"),
    ("内核版本", "uname -r"),
    ("CPU 信息", "cat /proc/cpuinfo | head -30"),
]


class CommandPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.proc = None
        self._remote_cancel = None
        self._run_generation = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # 命令输入
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("命令")
        lbl.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        row.addWidget(lbl)
        self.cmb_cmd = QComboBox()
        self.cmb_cmd.setEditable(True)
        self.cmb_cmd.addItems(["uname -a", "ls -la", "df -h", "ps -A | head -20"])
        self.cmb_cmd.setInsertPolicy(QComboBox.NoInsert)
        self.cmb_cmd.lineEdit().returnPressed.connect(self.run)
        row.addWidget(self.cmb_cmd, 1)

        self.btn_run = QPushButton("执行")
        self.btn_run.setIcon(theme.load_icon("play"))
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Secondary")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        row.addWidget(self.btn_stop)

        b_clear = QPushButton("清空")
        b_clear.setObjectName("Secondary")
        b_clear.clicked.connect(self.clear)
        row.addWidget(b_clear)
        lay.addLayout(row)

        # 预设
        pre = QHBoxLayout()
        pre.setSpacing(4)
        p = QLabel("常用")
        p.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        pre.addWidget(p)
        for title, cmd in PRESETS:
            b = QPushButton(title)
            b.setObjectName("Flat")
            b.setFixedHeight(24)
            b.clicked.connect(lambda _c=False, c=cmd: self._set_and_run(c))
            pre.addWidget(b)
        pre.addStretch(1)
        lay.addLayout(pre)

        # 输出
        self.out = QPlainTextEdit()
        self.out.setObjectName("MonitorView")
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(8000)
        self.out.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.out.setFont(f)
        lay.addWidget(self.out, 1)

        self.lbl_rc = QLabel("就绪")
        self.lbl_rc.setObjectName("Hint")
        lay.addWidget(self.lbl_rc)

        ab_ctx = ctx
        ab_ctx.device_selected.connect(lambda _d: self._update_enabled())
        ab_ctx.remote_changed.connect(lambda _x: self._update_enabled())
        self._update_enabled()

    def _update_enabled(self):
        ok = self.ctx.current is not None
        self.btn_run.setEnabled(ok)
        if not ok:
            self.lbl_rc.setText("未选择设备")

    def _set_and_run(self, cmd: str):
        self.cmb_cmd.setCurrentText(cmd)
        self.run()

    def _append(self, text: str, color: str = ""):
        if not text:
            return
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if color:
            self.out.appendHtml('<span style="color:%s">%s</span>' % (color, esc))
        else:
            self.out.appendPlainText(text.rstrip("\n"))
        sb = self.out.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---- 执行 ----------------------------------------------------------
    def run(self):
        cmd = self.cmb_cmd.currentText().strip()
        if not cmd:
            return
        if self.ctx.current is None:
            return
        self._append("\n$ %s" % cmd, theme.COLORS["accent_light"])
        self.lbl_rc.setText("执行中…")
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._run_generation += 1
        generation = self._run_generation

        # 远程模式：独立 SSH channel 执行，停止按钮可关闭该 channel。
        if self.ctx.remote is not None:
            remote = self.ctx.remote
            cancel_event = threading.Event()
            self._remote_cancel = cancel_event

            def work():
                return remote.adb_cancellable(["shell", cmd], cancel_event, timeout=300)

            def done(res, err):
                if generation != self._run_generation:
                    return
                self._remote_cancel = None
                self.btn_run.setEnabled(True)
                self.btn_stop.setEnabled(False)
                if err:
                    self._append("[错误] %s" % err, theme.COLORS["error"])
                    self.lbl_rc.setText("执行失败")
                    return
                rc, out = res
                self._append(out)
                if rc == 130:
                    self._append("[已停止]", theme.COLORS["warn"])
                    self.lbl_rc.setText("已停止")
                    return
                self._append("[退出码 %s]" % rc,
                             theme.COLORS["ok"] if rc == 0 else theme.COLORS["error"])
                self.lbl_rc.setText("退出码 %s" % rc)

            self.ctx.run_async(work, done)
            return

        # 本地模式：流式输出
        self.proc = QProcess(self)
        self.proc.setProgram(self.ctx.adb.exe)
        self.proc.setArguments(["-s", self.ctx.current.serial, "shell", cmd])
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.start()

    def _on_output(self):
        if self.proc is None:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            self._append(line)

    def _on_finished(self, rc, _status):
        self._append("[退出码 %s]" % rc, theme.COLORS["ok"] if rc == 0 else theme.COLORS["error"])
        self.lbl_rc.setText("退出码 %s" % rc)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.proc = None

    def stop(self):
        if self._remote_cancel is not None:
            self._remote_cancel.set()
            self.lbl_rc.setText("正在停止…")
            self.btn_stop.setEnabled(False)
            return
        if self.proc is not None:
            self.proc.kill()
            self._append("[已停止]", theme.COLORS["warn"])
            self.proc = None
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def clear(self):
        self.out.clear()
        self.lbl_rc.setText("就绪")

    def shutdown(self):
        if self._remote_cancel is not None:
            self._remote_cancel.set()
            self._remote_cancel = None
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.waitForFinished(1000)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None

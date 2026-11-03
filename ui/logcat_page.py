# -*- coding: utf-8 -*-
"""Logcat 页：实时日志、级别着色、关键字过滤、自动滚动、保存。"""

from __future__ import annotations

import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QTextCharFormat, QColor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ui import theme
from ui.context import AppContext

# logcat 行格式： 07-12 10:00:00.000  1234  1234 I Tag     : msg
RE_LINE = re.compile(r"^\s*(\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*)$")

LEVEL_COLORS = {
    "V": "#9D9D9D", "D": "#4FC1FF", "I": "#4EC9B0",
    "W": "#DCDCAA", "E": "#F44747", "F": "#FF6B6B",
}
LEVEL_NAMES = {"V": "Verbose", "D": "Debug", "I": "Info", "W": "Warn", "E": "Error", "F": "Fatal"}


class LogcatPage(QWidget):
    MAX_LINES = 4000

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.proc = None
        self._buffer = []          # 待追加的行
        self._filter = ""
        self._paused = False
        self._remote_poll_pending = False
        self._remote_generation = 0
        self._remote_last_lines = []

        # 高频日志：批量刷新，避免每行都重绘导致卡顿
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(120)
        self.flush_timer.timeout.connect(self._flush)
        self.flush_timer.start()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_start = QPushButton("开始抓取")
        self.btn_start.setIcon(theme.load_icon("play"))
        self.btn_start.setCheckable(True)
        self.btn_start.clicked.connect(self.toggle)
        bar.addWidget(self.btn_start)

        b_clear = QPushButton("清空")
        b_clear.setObjectName("Secondary")
        b_clear.clicked.connect(self.clear)
        bar.addWidget(b_clear)

        b_save = QPushButton("保存")
        b_save.setObjectName("Secondary")
        b_save.setIcon(theme.load_icon("save"))
        b_save.clicked.connect(self.save)
        bar.addWidget(b_save)

        bar.addSpacing(8)
        bar.addWidget(QLabel("过滤"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("关键字或标签，支持正则")
        self.ed_filter.setMinimumWidth(220)
        self.ed_filter.textChanged.connect(self._on_filter)
        bar.addWidget(self.ed_filter)

        self.chk_auto = QCheckBox("自动滚动")
        self.chk_auto.setChecked(True)
        bar.addWidget(self.chk_auto)

        self.lbl_count = QLabel("0 行")
        self.lbl_count.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        bar.addWidget(self.lbl_count)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.view.setFont(f)
        lay.addWidget(self.view, 1)

        hint = QLabel("部分嵌入式设备（Buildroot 等）没有 logcat，此时可用「命令执行」页查看 dmesg / journalctl。")
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        ctx.device_selected.connect(lambda _d: self.btn_start.setEnabled(ctx.current is not None))
        ctx.remote_changed.connect(lambda _x: self.btn_start.setEnabled(ctx.current is not None))
        self.btn_start.setEnabled(False)

    # ---- 控制 ----------------------------------------------------------
    def toggle(self):
        if self.btn_start.isChecked():
            self.start()
        else:
            self.stop()

    def start(self):
        if self.ctx.current is None:
            self.btn_start.setChecked(False)
            return
        if self.proc is not None:
            return
        self.clear()
        self.ctx.log("开始抓取 logcat")

        if self.ctx.remote is not None:
            # 远程模式使用异步轮询；SSH/adb 绝不能在 GUI 定时器槽里同步阻塞。
            self._remote_generation += 1
            self._remote_last_lines = []
            self._remote_poll_pending = False
            self._remote_timer = QTimer(self)
            self._remote_timer.setInterval(1500)
            self._remote_timer.timeout.connect(self._remote_poll)
            self._remote_timer.start()
            self._remote_poll()
            self.btn_start.setText("停止抓取")
            return

        self.proc = self.ctx.adb.popen(["logcat", "-v", "time"])
        self._proc = self.proc
        from PySide6.QtCore import QThread
        import threading as _th
        self._thread_stop = _th.Event()

        def reader():
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            while not self._thread_stop.is_set():
                try:
                    chunk = proc.stdout.read(4096)
                except Exception:  # noqa: BLE001
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                for line in text.splitlines():
                    self._buffer.append(line)
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass

        self._thread_stop = _th.Event()
        self._thread = _th.Thread(target=reader, daemon=True)
        self._thread.start()
        self.btn_start.setText("停止抓取")

    def _remote_poll(self):
        if self.ctx.remote is None:
            self.stop()
            return
        if self._remote_poll_pending:
            return
        self._remote_poll_pending = True
        generation = self._remote_generation
        remote = self.ctx.remote

        def work():
            return remote.adb(["logcat", "-d", "-v", "time", "-t", "120"],
                              timeout=25)

        def done(res, err):
            self._remote_poll_pending = False
            if (generation != self._remote_generation or
                    self.ctx.remote is not remote or not self.btn_start.isChecked()):
                return
            if err:
                self.ctx.log("远程 logcat 轮询失败: %s" % err, "error")
                return
            rc, out = res
            if rc != 0:
                return
            current = (out or "").splitlines()
            fresh = self._new_remote_lines(self._remote_last_lines, current)
            self._remote_last_lines = current
            self._buffer.extend(fresh)

        self.ctx.run_async(work, done)

    @staticmethod
    def _new_remote_lines(previous, current):
        """从滚动窗口中只取新增后缀，避免每次重复追加最近 120 行。"""
        if not previous:
            return list(current)
        limit = min(len(previous), len(current))
        for overlap in range(limit, 0, -1):
            if previous[-overlap:] == current[:overlap]:
                return list(current[overlap:])
        return [] if previous == current else list(current)

    def stop(self):
        self._remote_generation += 1
        self.btn_start.setChecked(False)
        self.btn_start.setText("开始抓取")
        if self.proc is not None:
            try:
                self._thread_stop.set()
                from core.adb_backend import kill_tree
                kill_tree(self.proc)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
            self._proc = None
        if hasattr(self, "_remote_timer"):
            self._remote_timer.stop()
            self._remote_timer = None
        self.ctx.log("logcat 抓取结束")

    # ---- 显示 ----------------------------------------------------------
    def _on_filter(self, text: str):
        self._filter = text.strip()

    def _flush(self):
        if not self._buffer:
            return
        lines, self._buffer = self._buffer, []
        view = self.view
        cursor = view.textCursor()
        cursor.movePosition(cursor.End)
        view.setTextCursor(cursor)
        pat = None
        if self._filter:
            try:
                pat = re.compile(self._filter, re.I)
            except re.error:
                pat = None
        txt_filter = self._filter.lower()
        added = 0
        view.setUpdatesEnabled(False)
        try:
            for line in lines:
                if txt_filter:
                    if pat is not None:
                        if not pat.search(line):
                            continue
                    elif txt_filter not in line.lower():
                        continue
                m = RE_LINE.match(line)
                if m:
                    lvl = m.group(4)
                    color = LEVEL_COLORS.get(lvl, theme.COLORS["text"])
                else:
                    color = theme.COLORS["text"]
                esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                view.appendHtml('<span style="color:%s;white-space:pre">%s</span>' % (color, esc))
                added += 1
        finally:
            view.setUpdatesEnabled(True)
        if added:
            if self.chk_auto.isChecked():
                sb = view.verticalScrollBar()
                sb.setValue(sb.maximum())
            self.lbl_count.setText("%d 行" % view.blockCount())

    def clear(self):
        self.view.clear()
        self._buffer.clear()
        self.lbl_count.setText("0 行")

    def save(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", "logcat_%s.log"
                                              % time.strftime("%Y%m%d_%H%M%S"),
                                              "日志文件 (*.log *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText())
            self.ctx.log("日志已保存: %s" % path, "ok")
        except OSError as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def shutdown(self):
        self.stop()
        self.flush_timer.stop()

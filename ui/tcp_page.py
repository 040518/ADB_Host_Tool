# -*- coding: utf-8 -*-
"""TCP 通道页：adb forward / reverse + 原始 socket 双向收发。"""

from __future__ import annotations

import binascii
import re
import socket
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from ui import theme
from ui.context import AppContext


def mono(size: int = 10) -> QFont:
    f = QFont("Cascadia Mono")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


class TcpPage(QWidget):
    recv_signal = Signal(bytes)
    log_signal = Signal(str)

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.sock = None
        self.fwd_active = ""
        self.fwd_reverse = False
        self._reader = None
        self._stop = threading.Event()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # ---- 通道配置 ----
        cfg = QGroupBox("通道配置")
        cl = QVBoxLayout(cfg)
        cl.setContentsMargins(10, 12, 10, 10)
        cl.setSpacing(6)

        r1 = QHBoxLayout()
        r1.setSpacing(6)
        r1.addWidget(QLabel("本机端口"))
        self.ed_lport = QLineEdit("18765")
        self.ed_lport.setFixedWidth(78)
        r1.addWidget(self.ed_lport)
        r1.addWidget(QLabel("设备端口"))
        self.ed_rport = QLineEdit("9000")
        self.ed_rport.setFixedWidth(78)
        r1.addWidget(self.ed_rport)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["forward（设备服务推送到本机）", "reverse（本机服务拉取）"])
        self.cmb_mode.setMinimumWidth(240)
        r1.addWidget(self.cmb_mode)
        r1.addStretch(1)
        cl.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.btn_fwd = QPushButton("建立转发")
        self.btn_fwd.clicked.connect(self.do_forward)
        r2.addWidget(self.btn_fwd)
        self.btn_conn = QPushButton("TCP 连接")
        self.btn_conn.setIcon(theme.load_icon("network"))
        self.btn_conn.clicked.connect(self.do_connect)
        r2.addWidget(self.btn_conn)
        self.btn_disc = QPushButton("断开")
        self.btn_disc.setObjectName("Secondary")
        self.btn_disc.setEnabled(False)
        self.btn_disc.clicked.connect(self.do_disconnect)
        r2.addWidget(self.btn_disc)
        self.btn_unfwd = QPushButton("移除转发")
        self.btn_unfwd.setObjectName("Secondary")
        self.btn_unfwd.clicked.connect(self.do_remove_forward)
        r2.addWidget(self.btn_unfwd)
        r2.addStretch(1)
        self.lbl_state = QLabel("未连接")
        self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        r2.addWidget(self.lbl_state)
        cl.addLayout(r2)

        hint = QLabel("设备上有监听端口的程序时（如串口转 TCP 服务、调试 socket），"
                      "用 forward 把设备端口映射到本机端口，再点「TCP 连接」即可双向收发原始数据。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        cl.addWidget(hint)
        lay.addWidget(cfg)

        # ---- 收发区 ----
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)

        up = QWidget()
        ul = QVBoxLayout(up)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.setSpacing(6)
        sr = QHBoxLayout()
        sr.setSpacing(6)
        sr.addWidget(QLabel("发送"))
        self.ed_send = QLineEdit("hello")
        self.ed_send.returnPressed.connect(self.do_send)
        sr.addWidget(self.ed_send, 1)
        self.chk_hex = QCheckBox("HEX")
        sr.addWidget(self.chk_hex)
        self.chk_crlf = QCheckBox("+\\r\\n")
        self.chk_crlf.setChecked(True)
        sr.addWidget(self.chk_crlf)
        b = QPushButton("发送")
        b.setIcon(theme.load_icon("send"))
        b.clicked.connect(self.do_send)
        sr.addWidget(b)
        ul.addLayout(sr)

        self.txt_rx = QPlainTextEdit()
        self.txt_rx.setObjectName("MonitorView")
        self.txt_rx.setFont(mono(10))
        self.txt_rx.setReadOnly(True)
        ul.addWidget(self.txt_rx, 1)
        split.addWidget(up)

        down = QWidget()
        dl = QVBoxLayout(down)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(4)
        tr = QHBoxLayout()
        tr.addWidget(QLabel("通道日志"))
        tr.addStretch(1)
        b_clear = QPushButton("清空日志")
        b_clear.setObjectName("Flat")
        b_clear.setFixedHeight(24)
        b_clear.clicked.connect(lambda: self.txt_log.clear())
        tr.addWidget(b_clear)
        dl.addLayout(tr)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setObjectName("LogView")
        self.txt_log.setMaximumBlockCount(2000)
        self.txt_log.setReadOnly(True)
        dl.addWidget(self.txt_log, 1)
        split.addWidget(down)
        split.setSizes([420, 160])
        lay.addWidget(split, 1)

        self.recv_signal.connect(self._append_rx)
        self.log_signal.connect(self._log)

        ctx.device_selected.connect(lambda _d: self._update_enabled())
        ctx.remote_changed.connect(lambda _x: self._update_enabled())
        self._update_enabled()

    def _update_enabled(self):
        ok = self.ctx.current is not None
        for b in (self.btn_fwd, self.btn_conn, self.btn_unfwd):
            b.setEnabled(ok)

    # ---- 日志 ----------------------------------------------------------
    def _log(self, text: str):
        import time as _t
        self.txt_log.appendPlainText("[%s] %s" % (_t.strftime("%H:%M:%S"), text))
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log(self, text: str):
        self.log_signal.emit(text)

    # ---- 转发 ----------------------------------------------------------
    def _run_adb(self, args, timeout=25):
        if self.ctx.remote is not None:
            return self.ctx.remote.adb(args, timeout=timeout)
        # forward/reverse 都是设备级命令，多设备时必须沿用当前序列号。
        return self.ctx.adb.run(args, timeout=timeout)

    @staticmethod
    def _remove_args(spec, reverse):
        return ["reverse" if reverse else "forward", "--remove", spec]

    def do_forward(self):
        lport = self.ed_lport.text().strip()
        rport = self.ed_rport.text().strip()
        reverse = self.cmb_mode.currentIndex() == 1
        spec = "tcp:%s" % (rport if reverse else lport)
        old_spec = self.fwd_active
        old_reverse = self.fwd_reverse

        def work():
            if old_spec and (old_spec != spec or old_reverse != reverse):
                self._run_adb(self._remove_args(old_spec, old_reverse))
            if reverse:
                return self._run_adb(["reverse", "tcp:%s" % rport, "tcp:%s" % lport])
            return self._run_adb(["forward", "tcp:%s" % lport, "tcp:%s" % rport])

        def done(res, err):
            if err:
                self.log("建立转发失败: %s" % err)
                return
            rc, out = res
            ok = rc == 0
            self.log("%s %s <-> 设备 %s : %s"
                     % ("reverse" if reverse else "forward", lport, rport, (out or "").strip()))
            if ok:
                self.fwd_active = spec
                self.fwd_reverse = reverse
                self.lbl_state.setText(
                    ("反向转发已建立 设备:%s → 本机:%s" if reverse else
                     "转发已建立 本机:%s → 设备:%s") %
                    ((rport, lport) if reverse else (lport, rport)))
                self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["ok"])

        self.ctx.run_async(work, done, busy_text="建立转发…")

    def do_remove_forward(self):
        if not self.fwd_active:
            return
        spec = self.fwd_active
        reverse = self.fwd_reverse

        def work():
            return self._run_adb(self._remove_args(spec, reverse))

        def done(res, err):
            self.log("移除转发 %s: %s" % (spec, res if not err else err))
            self.fwd_active = ""
            self.fwd_reverse = False
            self.lbl_state.setText("转发已移除")
            self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])

        self.ctx.run_async(work, done)

    # ---- socket --------------------------------------------------------
    def do_connect(self):
        if self.sock is not None:
            self.log("已有连接，先断开")
            return
        try:
            lport = int(self.ed_lport.text().strip())
        except ValueError:
            self.log("本机端口无效")
            return

        def work():
            if self.ctx.remote is not None:
                # adb forward 建在 Ubuntu 上，Windows 的 127.0.0.1 并不是
                # 同一台机器；通过现有 SSH transport 访问 Ubuntu 本地端口。
                s = self.ctx.remote.open_tcp_channel("127.0.0.1", lport, timeout=5)
            else:
                s = socket.create_connection(("127.0.0.1", lport), timeout=5)
            s.settimeout(0.5)
            return s

        def done(res, err):
            if err:
                self.log("连接 127.0.0.1:%d 失败: %s" % (lport, err))
                self.lbl_state.setText("连接失败")
                self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["error"])
                return
            self.sock = res
            self._stop.clear()
            self.lbl_state.setText("已连接 127.0.0.1:%d" % lport)
            self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["ok"])
            self.btn_disc.setEnabled(True)
            self.btn_conn.setEnabled(False)
            self.log("已连接 127.0.0.1:%d" % lport)
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()

        self.ctx.run_async(work, done, busy_text="连接 TCP…")

    def _read_loop(self):
        s = self.sock
        while not self._stop.is_set() and s is not None:
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self.log_signal.emit("接收异常: %s" % e)
                break
            if not data:
                self.log_signal.emit("对端关闭连接")
                break
            self.recv_signal.emit(data)
        self.recv_signal.emit(b"")

    def _append_rx(self, data: bytes):
        if not data:
            self.do_disconnect()
            return
        self.txt_rx.moveCursor(QTextCursor.End)
        self.txt_rx.insertPlainText(data.decode("utf-8", "replace"))
        sb = self.txt_rx.verticalScrollBar()
        sb.setValue(sb.maximum())

    def do_send(self):
        if self.sock is None:
            self.log("请先建立转发并点击「TCP 连接」")
            return
        text = self.ed_send.text()
        if self.chk_hex.isChecked():
            clean = re.sub(r"[^0-9a-fA-F]", "", text)
            if len(clean) % 2:
                self.log("HEX 字符数为奇数")
                return
            payload = binascii.unhexlify(clean)
        else:
            payload = text.encode("utf-8")
        if self.chk_crlf.isChecked():
            payload += b"\r\n"
        try:
            self.sock.sendall(payload)
            self.log("已发送 %d 字节: %r" % (len(payload), payload[:64]))
        except OSError as e:
            self.log("发送失败: %s" % e)

    def do_disconnect(self):
        self._stop.set()
        s = self.sock
        self.sock = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        self.btn_disc.setEnabled(False)
        self.btn_conn.setEnabled(True)
        self.lbl_state.setText("已断开")
        self.lbl_state.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])

    def shutdown(self):
        self.do_disconnect()
        if self.fwd_active:
            try:
                self._run_adb(self._remove_args(self.fwd_active, self.fwd_reverse))
            except Exception:  # noqa: BLE001
                pass

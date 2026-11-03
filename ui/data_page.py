# -*- coding: utf-8 -*-
"""数据收发页（VOFA+ 风格）：文本/HEX 发送、接收轮询、stdin 发送。"""

from __future__ import annotations

import binascii
import os
import re
import tempfile
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext


def mono(size: int = 10) -> QFont:
    f = QFont("Cascadia Mono")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


class DataPage(QWidget):
    POLL_MS = 600

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rx_running = False
        self.rx_buf = b""
        self._force = False
        self._read_pending = False

        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_MS)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # ---------------- 左：发送 ----------------
        left = QGroupBox("发送到设备")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 12, 10, 10)
        ll.setSpacing(6)

        r1 = QHBoxLayout()
        r1.setSpacing(6)
        r1.addWidget(QLabel("发送方式"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["文本", "HEX"])
        self.cmb_mode.setFixedWidth(88)
        r1.addWidget(self.cmb_mode)
        self.chk_cr = QCheckBox("+\\r")
        self.chk_lf = QCheckBox("+\\n")
        self.chk_zero = QCheckBox("+\\0")
        self.chk_lf.setChecked(True)
        for c in (self.chk_cr, self.chk_lf, self.chk_zero):
            r1.addWidget(c)
        r1.addStretch(1)
        ll.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        r2.addWidget(QLabel("目标文件"))
        self.ed_dest = QLineEdit("/data/local/tmp/adb_rx_from_host.txt")
        self.ed_dest.setMinimumWidth(120)
        r2.addWidget(self.ed_dest, 1)
        b = QPushButton("浏览")
        b.setObjectName("Secondary")
        b.setFixedWidth(52)
        b.clicked.connect(self._pick_dest)
        r2.addWidget(b)
        ll.addLayout(r2)

        r2b = QHBoxLayout()
        r2b.setSpacing(6)
        self.chk_append = QCheckBox("追加写入（不覆盖原内容）")
        self.chk_append.setChecked(True)
        r2b.addWidget(self.chk_append)
        r2b.addStretch(1)
        ll.addLayout(r2b)

        self.txt_send = QPlainTextEdit()
        self.txt_send.setFont(mono(10))
        self.txt_send.setObjectName("MonitorView")
        self.txt_send.setPlainText("你好，RK3568！\r\n第二行数据")
        ll.addWidget(self.txt_send, 1)

        r3 = QHBoxLayout()
        r3.setSpacing(6)
        b_send = QPushButton("发送数据")
        b_send.setIcon(theme.load_icon("send"))
        b_send.clicked.connect(self.send_data)
        r3.addWidget(b_send)
        b_clear = QPushButton("清空")
        b_clear.setObjectName("Secondary")
        b_clear.clicked.connect(lambda: self.txt_send.clear())
        r3.addWidget(b_clear)
        b_preview = QPushButton("预览目标文件")
        b_preview.setObjectName("Secondary")
        b_preview.clicked.connect(self.preview_dest)
        r3.addWidget(b_preview)
        r3.addStretch(1)
        ll.addLayout(r3)

        gb_stdin = QGroupBox("写入进程标准输入")
        sl = QHBoxLayout(gb_stdin)
        sl.setContentsMargins(10, 12, 10, 10)
        sl.setSpacing(6)
        sl.addWidget(QLabel("设备命令"))
        self.ed_stdin = QLineEdit("cat > /data/local/tmp/stdin_demo.txt")
        sl.addWidget(self.ed_stdin, 1)
        b_stdin = QPushButton("执行并发送")
        b_stdin.clicked.connect(self.send_stdin)
        sl.addWidget(b_stdin)
        ll.addWidget(gb_stdin)

        split.addWidget(left)

        # ---------------- 右：接收 ----------------
        right = QGroupBox("接收自设备")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 12, 10, 10)
        rl.setSpacing(6)

        rr = QHBoxLayout()
        rr.setSpacing(6)
        rr.addWidget(QLabel("接收文件"))
        self.ed_rx = QLineEdit("/data/local/tmp/adb_rx_from_host.txt")
        rr.addWidget(self.ed_rx, 1)
        b = QPushButton("浏览")
        b.setObjectName("Secondary")
        b.setFixedWidth(52)
        b.clicked.connect(self._pick_rx)
        rr.addWidget(b)
        rl.addLayout(rr)

        rr2 = QHBoxLayout()
        rr2.setSpacing(6)
        self.btn_rx = QPushButton("开始接收")
        self.btn_rx.setCheckable(True)
        self.btn_rx.clicked.connect(self.toggle_rx)
        rr2.addWidget(self.btn_rx)
        b_once = QPushButton("立即读取")
        b_once.setObjectName("Secondary")
        b_once.clicked.connect(self.read_once)
        rr2.addWidget(b_once)
        self.chk_hex = QCheckBox("HEX 显示")
        self.chk_hex.stateChanged.connect(lambda _s: self._render())
        rr2.addWidget(self.chk_hex)
        b_clear = QPushButton("清空")
        b_clear.setObjectName("Secondary")
        b_clear.clicked.connect(lambda: self.txt_rx.clear())
        rr2.addWidget(b_clear)
        b_save = QPushButton("保存")
        b_save.setObjectName("Secondary")
        b_save.clicked.connect(self.save_rx)
        rr2.addWidget(b_save)
        rr2.addStretch(1)
        self.lbl_rx = QLabel("未开始")
        self.lbl_rx.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        rr2.addWidget(self.lbl_rx)
        rl.addLayout(rr2)

        self.txt_rx = QPlainTextEdit()
        self.txt_rx.setFont(mono(10))
        self.txt_rx.setObjectName("MonitorView")
        self.txt_rx.setReadOnly(True)
        rl.addWidget(self.txt_rx, 1)

        hint = QLabel("接收原理：定时 adb pull 增量读取设备文件，二进制安全。"
                      "需要双向实时原始数据时请用「TCP 通道」页。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        rl.addWidget(hint)

        split.addWidget(right)
        split.setSizes([520, 520])
        lay.addWidget(split, 1)

        ctx.device_selected.connect(lambda _d: self._update_enabled())
        ctx.remote_changed.connect(lambda _x: self._update_enabled())
        self._update_enabled()

    def _update_enabled(self):
        ok = self.ctx.current is not None
        for w in (self.btn_rx,):
            w.setEnabled(ok)

    # ---- 工具 ----------------------------------------------------------
    def _pick_dest(self):
        p, _ = QFileDialog.getSaveFileName(self, "选择设备上的目标文件", self.ed_dest.text())
        if p:
            self.ed_dest.setText(p.replace("\\", "/"))

    def _pick_rx(self):
        p, _ = QFileDialog.getSaveFileName(self, "选择设备上的接收文件", self.ed_rx.text())
        if p:
            self.ed_rx.setText(p.replace("\\", "/"))

    def _payload(self) -> bytes:
        text = self.txt_send.toPlainText()
        if self.cmb_mode.currentText() == "HEX":
            clean = re.sub(r"(0x)|[^0-9a-fA-F]", "", text)
            if len(clean) % 2:
                raise ValueError("HEX 字符数为奇数，无法组成完整字节")
            data = binascii.unhexlify(clean) if clean else b""
        else:
            data = text.encode("utf-8")
        if self.chk_cr.isChecked():
            data += b"\r"
        if self.chk_lf.isChecked():
            data += b"\n"
        if self.chk_zero.isChecked():
            data += b"\x00"
        return data

    # ---- 发送 ----------------------------------------------------------
    def send_data(self):
        if self.ctx.current is None:
            return
        try:
            payload = self._payload()
        except ValueError as e:
            QMessageBox.warning(self, "发送", str(e))
            return
        dest = self.ed_dest.text().strip()
        append = self.chk_append.isChecked()

        def work():
            # 远程模式下 adb 运行在 Ubuntu 上：
            #   1) 先把数据经 SFTP 写到 Ubuntu 临时文件（adb 才看得见）
            #   2) 再用远端 adb push 到设备
            #   3) 追加模式再在设备上 cat >> 目标
            if self.ctx.remote is not None:
                sess = self.ctx.remote
                stamp = int(time.time() * 1000)
                utmp = "/tmp/.adbhost_send_%d" % stamp
                dtmp = "/data/local/tmp/.adbhost_send_%d" % stamp
                up_ok, up_msg = sess.upload_bytes(payload, utmp)
                if not up_ok:
                    return False, "写入 Ubuntu 临时文件失败: %s" % up_msg
                target = dtmp if append else dest
                rc, out = sess.adb(["push", utmp, target], timeout=600)
                sess.exec("rm -f %s" % ab.quote(utmp), timeout=30)
                if rc != 0:
                    return False, out
                if append:
                    rc2, out2 = sess.adb(
                        ["shell", "cat %s >> %s; rm -f %s; echo OK"
                         % (ab.quote(dtmp), ab.quote(dest), ab.quote(dtmp))], timeout=120)
                    return ("OK" in (out2 or "")), (out + out2)
                return True, out

            tmpdir = tempfile.mkdtemp(prefix="adbtool_")
            local = os.path.join(tmpdir, "payload.bin")
            try:
                with open(local, "wb") as f:
                    f.write(payload)
                rtmp = "/data/local/tmp/.adbhost_send_%d" % int(time.time() * 1000)
                rc, out = self.ctx.adb.push(local, rtmp)
                if rc != 0:
                    return False, out
                op = ">>" if append else ">"
                cmd = "cat %s %s %s; rm -f %s; echo OK" % (
                    ab.quote(rtmp), op, ab.quote(dest), ab.quote(rtmp))
                rc2, out2 = self.ctx.adb_shell(cmd, timeout=120)
                return ("OK" in out2), (out + out2)
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

        def done(res, err):
            if err:
                QMessageBox.critical(self, "发送失败", str(err))
                return
            ok, out = res
            self.ctx.log("发送 %d 字节 -> %s (%s模式) %s"
                         % (len(payload), dest, "追加" if append else "覆盖",
                            "成功" if ok else "失败"),
                         "ok" if ok else "error")
            if ok:
                self.ed_rx.setText(dest)
                self.read_once()
            else:
                QMessageBox.critical(self, "发送失败", (out or "")[-1200:])

        self.ctx.run_async(work, done, busy_text="发送数据…")

    def send_stdin(self):
        if self.ctx.current is None:
            return
        try:
            payload = self._payload()
        except ValueError as e:
            QMessageBox.warning(self, "发送", str(e))
            return
        cmd = self.ed_stdin.text().strip()
        if self.ctx.remote is not None:
            def work():
                return self.ctx.remote.adb_with_stdin(payload, cmd, timeout=120)

            def done(res, err):
                if err:
                    self.ctx.log("stdin 发送失败: %s" % err, "error")
                    return
                ok, msg = res
                self.ctx.log("stdin 发送 %d 字节: %s"
                             % (len(payload), "成功" if ok else (msg or "失败")[:200]),
                             "ok" if ok else "error")

            self.ctx.run_async(work, done)
            return

        def work():
            proc = self.ctx.adb.popen_input(["shell", cmd])
            if proc.stdin is None:
                ab.kill_tree(proc)
                return False, "无法打开 stdin"
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                proc.stdin.close()
            except Exception as e:  # noqa: BLE001
                ab.kill_tree(proc)
                return False, "写入失败: %s" % e
            deadline = time.time() + 1.5
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            finished = proc.poll() is not None
            if not finished:
                ab.kill_tree(proc)
            return True, "" if finished else "设备端命令未退出（adbd 不转发 EOF），数据已写入"

        def done(res, err):
            if err:
                self.ctx.log("stdin 发送失败: %s" % err, "error")
                return
            ok, msg = res
            self.ctx.log("stdin 发送 %d 字节: %s" % (len(payload), msg or "完成"),
                         "ok" if ok else "error")

        self.ctx.run_async(work, done)

    def preview_dest(self):
        if self.ctx.current is None:
            return
        path = self.ed_dest.text().strip()

        def work():
            if self.ctx.remote is not None:
                return self.ctx.remote.adb_read_bytes(path, timeout=60)
            return self.ctx.adb.read_file_bytes(path)

        def done(res, err):
            if err:
                QMessageBox.warning(self, "预览", str(err))
                return
            ok, data, msg = res
            if not ok:
                QMessageBox.warning(self, "预览", msg)
                return
            text = data.decode("utf-8", "replace")
            dlg = QMessageBox(self)
            dlg.setWindowTitle("设备文件内容")
            dlg.setText("文件：%s" % path)
            dlg.setDetailedText(text[:20000])
            dlg.exec()

        self.ctx.run_async(work, done)

    # ---- 接收 ----------------------------------------------------------
    def toggle_rx(self):
        if self.btn_rx.isChecked():
            if self.ctx.current is None:
                self.btn_rx.setChecked(False)
                return
            self.rx_running = True
            self.btn_rx.setText("停止接收")
            self.lbl_rx.setText("接收中…")
            self.ctx.log("开始轮询接收 %s" % self.ed_rx.text().strip())
        else:
            self.rx_running = False
            self.btn_rx.setText("开始接收")
            self.lbl_rx.setText("已停止")

    def _poll(self):
        if self.rx_running:
            self.read_once(force=False)

    def read_once(self, force: bool = True):
        if self.ctx.current is None or self._read_pending:
            return
        remote = self.ed_rx.text().strip()
        if not remote:
            return
        self._force = force
        self._read_pending = True
        remote_session = self.ctx.remote
        device_serial = self.ctx.current.serial

        def work():
            if remote_session is not None:
                return remote_session.adb_read_bytes(remote, timeout=60)
            return self.ctx.adb.read_file_bytes(remote)

        def done(res, err):
            self._read_pending = False
            if (self.ctx.remote is not remote_session or self.ctx.current is None or
                    self.ctx.current.serial != device_serial):
                return
            if err:
                self.lbl_rx.setText("读取失败")
                return
            ok, data, msg = res
            if not ok:
                self.lbl_rx.setText("等待文件")
                return
            if self._force or data != self.rx_buf:
                self.rx_buf = data
                self._render()

        self.ctx.run_async(work, done)

    def _render(self):
        data = self.rx_buf
        if self.chk_hex.isChecked():
            lines = []
            for i in range(0, len(data), 16):
                chunk = data[i:i + 16]
                hx = " ".join("%02X" % b for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append("%08X  %-47s  |%s|" % (i, hx, asc))
            self.txt_rx.setPlainText("\n".join(lines))
        else:
            self.txt_rx.setPlainText(data.decode("utf-8", "replace"))
        self.lbl_rx.setText("已接收 %s" % ab.human_size(len(data)))

    def save_rx(self):
        if not self.rx_buf:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存接收数据")
        if path:
            with open(path, "wb") as f:
                f.write(self.rx_buf)
            self.ctx.log("已保存接收数据 %d 字节 -> %s" % (len(self.rx_buf), path), "ok")

    def shutdown(self):
        self.timer.stop()

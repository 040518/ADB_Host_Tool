# -*- coding: utf-8 -*-
"""画面页：设备截图与 RK3568 V4L2 摄像头实时预览。"""

from __future__ import annotations

import os
import time
import re
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap, QTransform
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext


class CameraStreamWorker(QThread):
    """从独立 SSH channel 连续读取 MJPEG，不为每一帧重复建连接。"""

    frame_ready = Signal(bytes, int)       # jpeg, generation
    stream_ended = Signal(int, str, int)   # frame_count, message, generation

    def __init__(self, remote, start_command, command, cleanup_command,
                 forward_spec, generation, parent=None, serial=""):
        super().__init__(parent)
        self.remote = remote
        self.start_command = start_command
        self.command = command
        self.cleanup_command = cleanup_command
        self.forward_spec = forward_spec
        self.generation = generation
        self.serial = serial
        self._channel = None
        self._control_channel = None

    def _adb(self, args, timeout=10):
        return self.remote.adb(["-s", self.serial] + args, timeout=timeout)

    def stop(self):
        self.requestInterruption()
        channel = self._channel
        if channel is not None:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass
        control = self._control_channel
        if control is not None:
            try:
                control.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self):
        count = 0
        message = ""
        channel = None
        control = None
        try:
            if self.forward_spec:
                self._adb(["forward", "--remove", self.forward_spec])
                rc, output = self._adb(["forward", self.forward_spec, self.forward_spec])
                if rc != 0:
                    raise RuntimeError("ADB 端口转发失败: %s" % output.strip())
            transport = self.remote.client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH 连接不可用")
            if self.start_command:
                control = transport.open_session(timeout=10)
                self._control_channel = control
                control.exec_command(self.start_command)
                self.msleep(350)
            if control is not None and control.exit_status_ready():
                errors = bytearray()
                while control.recv_stderr_ready() and len(errors) < 4096:
                    errors.extend(control.recv_stderr(4096))
                raise RuntimeError("摄像头流启动失败: %s" %
                                   errors.decode("utf-8", "replace").strip())
            channel = transport.open_session(timeout=10)
            self._channel = channel
            channel.exec_command(self.command)
            data = bytearray()

            while not self.isInterruptionRequested():
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    while True:
                        start = data.find(b"\xff\xd8")
                        if start < 0:
                            if len(data) > 2:
                                del data[:-2]
                            break
                        end = data.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            if start:
                                del data[:start]
                            break
                        frame = bytes(data[start:end + 2])
                        del data[:end + 2]
                        count += 1
                        self.frame_ready.emit(frame, self.generation)
                    if len(data) > 8 * 1024 * 1024:
                        data.clear()
                        message = "MJPEG 缓冲区异常，已重置"
                    continue
                # BusyBox setsid 可能先退出父进程，实际采集子进程仍持有 stdout。
                # 必须等输出 EOF，不能把父进程状态当成视频结束。
                if channel.exit_status_ready() and (
                        getattr(channel, "eof_received", True) or
                        getattr(channel, "closed", False)):
                    break
                self.msleep(5)

            if not self.isInterruptionRequested() and channel is not None:
                errors = bytearray()
                while channel.recv_stderr_ready() and len(errors) < 4096:
                    errors.extend(channel.recv_stderr(4096))
                if errors:
                    message = errors.decode("utf-8", "replace").strip()
                elif not message:
                    message = "摄像头数据流已结束"
        except Exception as exc:  # noqa: BLE001
            if not self.isInterruptionRequested():
                message = str(exc)
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
            if count == 0 and control is not None:
                errors = bytearray()
                try:
                    while control.recv_stderr_ready() and len(errors) < 4096:
                        errors.extend(control.recv_stderr(4096))
                except Exception:  # noqa: BLE001
                    pass
                details = errors.decode("utf-8", "replace").strip()
                if details:
                    message = "%s; %s" % (message, details[-1000:])
            if control is not None:
                try:
                    control.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if self.forward_spec:
                    self._adb(["shell", self.cleanup_command])
                else:
                    cleanup = getattr(self.remote, "exec_independent", self.remote.exec)
                    cleanup(self.cleanup_command, timeout=10)
            except Exception:  # noqa: BLE001
                pass
            try:
                if self.forward_spec:
                    self._adb(["forward", "--remove", self.forward_spec])
            except Exception:  # noqa: BLE001
                pass
            self._channel = None
            self._control_channel = None
            self.stream_ended.emit(count, message, self.generation)


class ScreenshotPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._pixmap = None
        self._camera_source_pixmap = None
        self._preview_active = False
        self._preview_pending = False
        self._preview_errors = 0
        self._preview_generation = 0
        self._frame_times = []
        self._stream_worker = None
        self._owned_stream_workers = set()

        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._request_camera_frame)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.cmb_target = QComboBox()
        self.cmb_target.addItem("自动（ADB 优先，否则 SSH）", "auto")
        self.cmb_target.addItem("SSH 目标", "ssh")
        self.cmb_target.addItem("ADB 设备", "adb")
        self.cmb_target.setMaximumWidth(220)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["设备截图", "实时摄像头"])
        self.cmb_mode.setMinimumWidth(130)
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        bar.addWidget(self.cmb_mode)

        self.cmb_camera = QComboBox()
        self.cmb_camera.addItems(["/dev/video0", "/dev/video1"])
        self.cmb_camera.setEditable(True)
        self.cmb_camera.setToolTip("RKISP mainpath / selfpath")
        self.cmb_camera.setVisible(False)
        bar.addWidget(self.cmb_camera)

        self.cmb_size = QComboBox()
        self.cmb_size.addItems(["640×480", "1280×720"])
        self.cmb_size.setVisible(False)
        bar.addWidget(self.cmb_size)

        self.cmb_rotation = QComboBox()
        self.cmb_rotation.addItem("原始方向", 0)
        self.cmb_rotation.addItem("向左旋转 90°", -90)
        self.cmb_rotation.addItem("向右旋转 90°", 90)
        self.cmb_rotation.addItem("旋转 180°", 180)
        saved_rotation = int(ctx.settings.get("camera_rotation", -90))
        saved_index = self.cmb_rotation.findData(saved_rotation)
        self.cmb_rotation.setCurrentIndex(saved_index if saved_index >= 0 else 1)
        self.cmb_rotation.setToolTip("只校正预览/保存方向，不修改摄像头驱动")
        self.cmb_rotation.setVisible(False)
        self.cmb_rotation.currentIndexChanged.connect(self._rotation_changed)
        bar.addWidget(self.cmb_rotation)

        self.btn_cap = QPushButton("截取当前画面")
        self.btn_cap.setIcon(theme.load_icon("camera"))
        self.btn_cap.clicked.connect(self.capture)
        bar.addWidget(self.btn_cap)

        self.btn_save = QPushButton("保存当前画面")
        self.btn_save.setObjectName("Secondary")
        self.btn_save.setIcon(theme.load_icon("save"))
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save)
        bar.addWidget(self.btn_save)

        bar.addStretch(1)
        self.lbl_info = QLabel("尚未截图")
        self.lbl_info.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        bar.addWidget(self.lbl_info)
        lay.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.preview = QLabel("点击「截取当前画面」获取设备屏幕\n\n"
                              "使用 screencap 抓图后拉取到本机")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("color:%s;font-size:13px;" % theme.COLORS["text_dim"])
        self.scroll.setWidget(self.preview)
        lay.addWidget(self.scroll, 1)

        ctx.device_selected.connect(lambda _d: self._device_state_changed())
        ctx.remote_changed.connect(lambda _x: self._device_state_changed(stop=True))
        self.cmb_target.currentIndexChanged.connect(lambda: self._device_state_changed())
        self._target_identity = None
        self.lbl_target = QLabel()
        target_bar = QHBoxLayout()
        target_bar.addWidget(self.cmb_target)
        target_bar.addWidget(self.lbl_target, 1)
        lay.insertLayout(1, target_bar)
        self._device_state_changed()
        self._update_controls()

    def capture(self):
        if self.cmb_mode.currentIndex() == 1:
            if self._preview_active:
                self.stop_preview()
            else:
                self.start_preview()
            return
        self._capture_screen()

    def _capture_screen(self):
        if self.ctx.current is None:
            return
        remote_png = "/data/local/tmp/.adbhost_shot.png"
        tmpdir = os.path.join(os.environ.get("TEMP", "/tmp"), "adbhost_shot")

        def work():
            os.makedirs(tmpdir, exist_ok=True)
            local = os.path.join(tmpdir, "shot_%d.png" % int(time.time() * 1000))
            rc, out = self.ctx.adb_shell("screencap -p %s" % remote_png, timeout=60)
            if self.ctx.remote is not None:
                # adb 在 Ubuntu 上，先拉到 Ubuntu 临时文件，再经 SFTP 取回 Windows
                staging = "/tmp/.adbhost_shot_%d.png" % int(time.time() * 1000)
                rc2, out2 = self.ctx.remote.adb(["pull", remote_png, staging], timeout=120)
                self.ctx.remote.adb(["shell", "rm -f %s" % remote_png], timeout=30)
                if rc2 != 0:
                    return None, out2
                ok, msg = self.ctx.remote.download_file(staging, tmpdir)
                self.ctx.remote.exec("rm -f %s" % staging, timeout=20)
                pulled = os.path.join(tmpdir, os.path.basename(staging))
                if not ok or not os.path.isfile(pulled):
                    return None, "从 Ubuntu 取回截图失败: %s" % msg
                import shutil as _sh
                _sh.move(pulled, local)
                return local, out2
            rc2, out2 = self.ctx.adb.pull(remote_png, local, timeout=120)
            self.ctx.adb.shell("rm -f %s" % remote_png)
            if rc2 != 0 or not os.path.isfile(local):
                return None, out2
            return local, out2

        def done(res, err):
            if err:
                QMessageBox.critical(self, "截图失败", str(err))
                return
            path, msg = res
            if not path:
                QMessageBox.warning(self, "截图失败", str(msg)[:800])
                self.lbl_info.setText("截图失败")
                return
            pm = QPixmap(path)
            if pm.isNull():
                QMessageBox.warning(self, "截图失败", "无法解析截图数据（设备可能不支持 screencap）")
                return
            self._pixmap = pm
            self._camera_source_pixmap = None
            self._display_pixmap(pm)
            self.btn_save.setEnabled(True)
            self.lbl_info.setText("已截图 %d × %d" % (pm.width(), pm.height()))
            self.ctx.log("设备截图成功 %d×%d" % (pm.width(), pm.height()), "ok")

        self.ctx.run_async(work, done, busy_text="正在截图…")

    def _mode_changed(self, index):
        self.stop_preview(update_text=False)
        camera = index == 1
        self.cmb_camera.setVisible(camera)
        self.cmb_size.setVisible(camera)
        self.cmb_rotation.setVisible(camera)
        self._pixmap = None
        self._camera_source_pixmap = None
        self.btn_save.setEnabled(False)
        self.btn_cap.setText("开始预览" if camera else "截取当前画面")
        self.btn_cap.setIcon(theme.load_icon("play" if camera else "camera"))
        if camera:
            self.preview.clear()
            self.preview.setText("选择摄像头节点后点击「开始预览」\n\n"
                                 "RK3568: V4L2 NV12 → FFmpeg JPEG → Windows")
            self.lbl_info.setText("摄像头预览就绪")
        else:
            self.preview.clear()
            self.preview.setText("点击「截取当前画面」获取设备屏幕\n\n"
                                 "使用 screencap 抓图后拉取到本机")
            self.lbl_info.setText("尚未截图")
        self.preview.setStyleSheet("color:%s;font-size:13px;" % theme.COLORS["text_dim"])
        self._update_controls()

    def _device_state_changed(self, stop=False):
        identity = self.ctx.target_identity(self.cmb_target.currentData())
        if stop or identity != self._target_identity:
            self.stop_preview(update_text=self.cmb_mode.currentIndex() == 1)
            self._pixmap = None
            self._camera_source_pixmap = None
            self.preview.clear()
            self.preview.setText("目标已切换，点击「开始预览」获取当前目标的画面")
            key = "camera_rotation_ssh" if identity[0] == "ssh" else "camera_rotation"
            default = 0 if identity[0] == "ssh" else -90
            index = self.cmb_rotation.findData(int(self.ctx.settings.get(key, default)))
            self.cmb_rotation.blockSignals(True)
            self.cmb_rotation.setCurrentIndex(max(0, index))
            self.cmb_rotation.blockSignals(False)
        self._target_identity = identity
        target = identity[0]
        if target == "ssh":
            self.lbl_target.setText("摄像头目标：SSH · %s@%s（无需 ADB）" %
                                    (self.ctx.remote.user, self.ctx.remote.host))
            if self.cmb_mode.currentIndex() == 0:
                self.cmb_mode.setCurrentIndex(1)
        else:
            self.lbl_target.setText("摄像头目标：%s" %
                                    ("ADB · " + self.ctx.current.serial if target else "未连接"))
        self._update_controls()

    def _update_controls(self):
        target = self.ctx.resolve_target(self.cmb_target.currentData())
        connected = target is not None
        camera = self.cmb_mode.currentIndex() == 1
        self.btn_cap.setEnabled(connected and (camera or target == "adb") and
                                (self._preview_active or not self._owned_stream_workers))
        self.cmb_camera.setEnabled(connected and camera and not self._preview_active)
        self.cmb_size.setEnabled(connected and camera and not self._preview_active)
        self.cmb_rotation.setEnabled(connected and camera)
        self.btn_save.setEnabled(self._pixmap is not None)

    def start_preview(self):
        if self.ctx.resolve_target(self.cmb_target.currentData()) is None or self._preview_active:
            return
        if not re.fullmatch(r"/dev/video\d+", self.cmb_camera.currentText()):
            QMessageBox.warning(self, "摄像头节点", "请输入 /dev/video0 这样的摄像头节点")
            return
        self._preview_generation += 1
        self._preview_active = True
        self._preview_errors = 0
        self._frame_times = []
        self.btn_cap.setText("停止预览")
        self.btn_cap.setIcon(theme.load_icon("stop"))
        self.lbl_info.setText("正在启动摄像头…")
        self._update_controls()
        self._preview_timer.start()
        if not self._start_streaming_preview():
            self._request_camera_frame()
        self.ctx.log("开始摄像头预览: %s %s %s" %
                     (self.cmb_camera.currentText(), self.cmb_size.currentText(),
                      self.cmb_rotation.currentText()))

    def stop_preview(self, update_text=True):
        was_active = self._preview_active
        self._preview_generation += 1
        self._preview_active = False
        self._preview_timer.stop()
        worker = self._stream_worker
        self._stream_worker = None
        if worker is not None:
            worker.stop()
        self.btn_cap.setText("开始预览" if self.cmb_mode.currentIndex() == 1
                             else "截取当前画面")
        self.btn_cap.setIcon(theme.load_icon("play" if self.cmb_mode.currentIndex() == 1
                                             else "camera"))
        if update_text and was_active:
            self.lbl_info.setText("预览已停止")
        self._update_controls()

    def _camera_pipeline(self):
        node = self.cmb_camera.currentText()
        size = self.cmb_size.currentText().replace("×", "x")
        width, height = [int(x) for x in size.split("x", 1)]
        if self.ctx.resolve_target(self.cmb_target.currentData()) == "ssh":
            generic = ("ffmpeg -nostdin -hide_banner -loglevel error -f v4l2 "
                        "-video_size %dx%d -i %s -frames:v 1 -q:v 5 -f image2pipe pipe:1"
                        % (width, height, ab.quote(node)))
            multiplanar = self._nv12_pipeline(node, width, height, single=True)
            return node, width, height, self._ssh_camera_route(node, multiplanar, generic)
        pipeline = (
            "v4l2-ctl -d %s --set-fmt-video=width=%d,height=%d,pixelformat=NV12 "
            "--stream-mmap=4 --stream-count=1 --stream-to=- 2>/dev/null | "
            "ffmpeg -hide_banner -loglevel error -f rawvideo -pix_fmt nv12 "
            "-video_size %dx%d -i pipe:0 -frames:v 1 -q:v 5 "
            "-f image2pipe pipe:1"
        ) % (node, width, height, width, height)
        return node, width, height, pipeline

    def _camera_stream_pipeline(self, port=None):
        node = self.cmb_camera.currentText()
        size = self.cmb_size.currentText().replace("×", "x")
        width, height = [int(x) for x in size.split("x", 1)]
        if self.ctx.resolve_target(self.cmb_target.currentData()) == "ssh":
            encoded = (
                "ffmpeg -nostdin -hide_banner -loglevel error -f v4l2 "
                "-video_size %dx%d -i %s -q:v 7 -f mjpeg pipe:1" %
                (width, height, ab.quote(node)))
            native = (
                "ffmpeg -nostdin -hide_banner -loglevel error -f v4l2 "
                "-input_format mjpeg -video_size %dx%d -i %s "
                "-c:v copy -f mjpeg pipe:1" % (width, height, ab.quote(node)))
            if width <= 640 and height <= 480:
                generic = encoded
            else:
                generic = "(%s) || (%s)" % (native, encoded)
            # USB 摄像头原生 MJPEG 直接传输，避免在 ARM 主机上解码后再次编码。
            # 不支持 MJPEG 的 V4L2 设备仍用通用编码路径。
            multiplanar = self._nv12_pipeline(node, width, height, single=False)
            return width, height, self._ssh_camera_route(node, multiplanar, generic)
        output = (("-f mjpeg -listen 1 tcp://127.0.0.1:%d" % port)
                  if port else "-f mjpeg pipe:1")
        pipeline = (
            "v4l2-ctl -d %s --set-fmt-video=width=%d,height=%d,pixelformat=NV12 "
            "--stream-mmap=4 --stream-count=0 --stream-to=- 2>/dev/null | "
            "ffmpeg -hide_banner -loglevel error -f rawvideo -pix_fmt nv12 "
            "-framerate 30 -video_size %dx%d -i pipe:0 -q:v 7 "
            "%s"
        ) % (node, width, height, width, height, output)
        return width, height, pipeline

    @staticmethod
    def _ssh_camera_route(node, multiplanar, generic):
        """选择目标真实接口；USB 无 v4l2-ctl 时仍可走 FFmpeg 原生采集。"""
        return ("if v4l2-ctl -d %s --get-fmt-video 2>/dev/null | "
                "grep -q Multiplanar; then %s; else %s; fi" %
                (ab.quote(node), multiplanar, generic))

    @staticmethod
    def _nv12_pipeline(node, width, height, single=False):
        return (
            "v4l2-ctl -d %s --set-fmt-video=width=%d,height=%d,pixelformat=NV12 "
            "--stream-mmap=4 --stream-count=%d --stream-to=- 2>/dev/null | "
            "ffmpeg -nostdin -hide_banner -loglevel error -f rawvideo -pix_fmt nv12 "
            "-framerate 30 -video_size %dx%d -i pipe:0 %s-q:v 7 -f mjpeg pipe:1"
        ) % (ab.quote(node), width, height, 1 if single else 0,
             width, height, "-frames:v 1 " if single else "")

    @staticmethod
    def _ssh_session_command(body):
        quoted = ab.quote(body)
        return ("if setsid --help 2>&1 | grep -q -- '--wait'; then "
                "exec setsid -w sh -c %s; else exec setsid sh -c %s; fi" %
                (quoted, quoted))

    def _start_streaming_preview(self):
        """Paramiko 可用时启用长连接；plink/本机模式保留逐帧兼容路径。"""
        remote = self.ctx.remote
        if (remote is None or remote.backend != "paramiko" or
                remote.client is None):
            return False
        generation = self._preview_generation
        target = self.ctx.resolve_target(self.cmb_target.currentData())
        if target == "ssh":
            width, height, pipeline = self._camera_stream_pipeline()
            pidfile = "/tmp/.adbhost_camera_%s.pid" % uuid.uuid4().hex
            body = "echo $$ > %s; trap 'rm -f %s' EXIT; %s" % (
                ab.quote(pidfile), ab.quote(pidfile), pipeline)
            cleanup = ("p=$(cat %s 2>/dev/null); if [ -n \"$p\" ]; then "
                       "kill -TERM -$p 2>/dev/null || kill -TERM $p 2>/dev/null; fi; rm -f %s"
                       % (ab.quote(pidfile), ab.quote(pidfile)))
            # GNU setsid 会在会话首进程场景 fork；-w 保持 SSH channel 存活到采集结束。
            worker = CameraStreamWorker(remote, "", self._ssh_session_command(body),
                                        cleanup, None, generation, self)
            self._attach_stream_worker(worker, width, height)
            return True
        port = 27180 + (generation % 100)
        width, height, pipeline = self._camera_stream_pipeline(port)
        pidfile = "/data/local/tmp/.adbhost_camera_%s.pid" % uuid.uuid4().hex
        body = ("echo $$ > %s; trap 'rm -f %s' EXIT; %s" %
                (ab.quote(pidfile), ab.quote(pidfile), pipeline))
        # 图像走 adb forward 的 TCP 通道，不经过旧 adbd 的 PTY，避免二进制
        # JPEG 中的换行字节被改写。采集进程独立成组，停止时按 PID 精确清理。
        device_command = "setsid sh -c %s" % ab.quote(body)
        start_command = "%s -s %s shell %s" % (ab.quote(remote.adb_remote),
                                           ab.quote(self.ctx.current.serial),
                                           ab.quote(device_command))
        cleanup_command = (
            "p=$(cat %s 2>/dev/null); "
            "if [ -n \"$p\" ]; then kill -TERM -$p 2>/dev/null || "
            "kill -TERM $p 2>/dev/null; fi; rm -f %s"
        ) % (ab.quote(pidfile), ab.quote(pidfile))
        forward_spec = "tcp:%d" % port
        command = "nc 127.0.0.1 %d" % port
        worker = CameraStreamWorker(remote, start_command, command,
                                    cleanup_command, forward_spec,
                                    generation, self, serial=self.ctx.current.serial)
        self._attach_stream_worker(worker, width, height)
        return True

    def _attach_stream_worker(self, worker, width, height):
        worker.frame_ready.connect(
            lambda data, gen: self._stream_frame_ready(data, gen, width, height))
        worker.stream_ended.connect(self._stream_ended)
        self._owned_stream_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._stream_worker_finished(w))
        self._stream_worker = worker
        self._preview_timer.stop()
        worker.start()

    def _stream_worker_finished(self, worker):
        if worker not in self._owned_stream_workers:
            return
        self._owned_stream_workers.remove(worker)
        if self._stream_worker is worker:
            self._stream_worker = None
        worker.deleteLater()
        self._update_controls()

    def _stream_frame_ready(self, data, generation, width, height):
        if not self._preview_active or generation != self._preview_generation:
            return
        pm = QPixmap()
        if not pm.loadFromData(data, "JPG"):
            return
        self._preview_errors = 0
        pm = self._accept_camera_pixmap(pm)
        self.btn_save.setEnabled(True)
        now = time.monotonic()
        self._frame_times.append(now)
        self._frame_times = self._frame_times[-60:]
        fps = 0.0
        if len(self._frame_times) > 1:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0.001:
                fps = (len(self._frame_times) - 1) / span
        self.lbl_info.setText("%d×%d  %.1f FPS  长连接流式 · %s" %
                              (pm.width(), pm.height(), fps,
                               self.cmb_rotation.currentText()))

    def _stream_ended(self, frame_count, message, generation):
        if generation != self._preview_generation or not self._preview_active:
            return
        self._stream_worker = None
        if frame_count == 0:
            self.ctx.log("长连接预览不可用，切换兼容模式: %s" % message, "warning")
            self.lbl_info.setText("长连接不可用，正在切换兼容模式…")
            self._preview_timer.start()
            self._request_camera_frame()
            return
        self.stop_preview(update_text=False)
        self.lbl_info.setText("预览已停止：%s" % (message or "摄像头数据流已结束")[:180])

    def _request_camera_frame(self):
        target = self.ctx.resolve_target(self.cmb_target.currentData())
        if not self._preview_active or self._preview_pending or target is None:
            return
        self._preview_pending = True
        _node, width, height, pipeline = self._camera_pipeline()
        remote = self.ctx.remote
        serial = self.ctx.current.serial if target == "adb" else ""
        generation = self._preview_generation
        started = time.monotonic()

        def work():
            stamp = "%d_%d" % (int(time.time() * 1000), generation)
            tmpdir = os.path.join(os.environ.get("TEMP", "/tmp"), "adbhost_camera")
            os.makedirs(tmpdir, exist_ok=True)
            local_jpg = os.path.join(tmpdir, ".adbhost_camera_%s.jpg" % stamp)
            if remote is not None:
                ubuntu_jpg = "/tmp/.adbhost_camera_%s.jpg" % stamp
                try:
                    device_cmd = "timeout 12 sh -c %s" % ab.quote(pipeline)
                    remote_cmd = ("%s > %s" % (device_cmd, ab.quote(ubuntu_jpg))
                                  if target == "ssh" else
                                  "%s -s %s shell %s > %s" % (
                                      ab.quote(remote.adb_remote), ab.quote(serial),
                                      ab.quote(device_cmd), ab.quote(ubuntu_jpg)))
                    rc, message = remote.exec(remote_cmd, timeout=15)
                    if rc != 0:
                        return rc, b"", message
                    ok, transfer = remote.download_file(ubuntu_jpg, tmpdir)
                    pulled = os.path.join(tmpdir, os.path.basename(ubuntu_jpg))
                    if not ok or not os.path.isfile(pulled):
                        return 1, b"", transfer
                    if pulled != local_jpg:
                        os.replace(pulled, local_jpg)
                    with open(local_jpg, "rb") as f:
                        return 0, f.read(), message
                finally:
                    remote.exec("rm -f %s" % ab.quote(ubuntu_jpg), timeout=10)
                    try:
                        os.remove(local_jpg)
                    except OSError:
                        pass
            device_jpg = "/data/local/tmp/.adbhost_camera_%s.jpg" % stamp
            capture_cmd = "timeout 12 sh -c %s" % ab.quote(
                pipeline + " > " + ab.quote(device_jpg))
            try:
                rc, message = self.ctx.adb.run(["shell", capture_cmd], serial=serial, timeout=15)
                if rc != 0:
                    return rc, b"", message
                rc, message = self.ctx.adb.run(["pull", device_jpg, local_jpg], serial=serial, timeout=30)
                if rc != 0 or not os.path.isfile(local_jpg):
                    return rc or 1, b"", message
                with open(local_jpg, "rb") as f:
                    return 0, f.read(), message
            finally:
                self.ctx.adb.run(["shell", "rm -f %s" % ab.quote(device_jpg)], serial=serial, timeout=10)
                try:
                    os.remove(local_jpg)
                except OSError:
                    pass

        def done(res, err):
            self._preview_pending = False
            if not self._preview_active or generation != self._preview_generation:
                return
            if err:
                self._camera_frame_failed(str(err))
                return
            rc, data, message = res
            if rc != 0 or not data.startswith(b"\xff\xd8"):
                self._camera_frame_failed(message or "设备未返回有效 JPEG")
                return
            pm = QPixmap()
            if not pm.loadFromData(data, "JPG"):
                self._camera_frame_failed("无法解析摄像头 JPEG")
                return
            self._preview_errors = 0
            pm = self._accept_camera_pixmap(pm)
            self.btn_save.setEnabled(True)
            now = time.monotonic()
            self._frame_times.append(now)
            self._frame_times = self._frame_times[-20:]
            fps = 0.0
            if len(self._frame_times) > 1:
                fps = (len(self._frame_times) - 1) / (self._frame_times[-1] - self._frame_times[0])
            self.lbl_info.setText("%d×%d  %.1f FPS  延迟 %.0f ms · %s" %
                                  (pm.width(), pm.height(), fps,
                                   (now - started) * 1000.0,
                                   self.cmb_rotation.currentText()))

        self.ctx.run_async(work, done)

    def _camera_frame_failed(self, message):
        self._preview_errors += 1
        self.lbl_info.setText("取帧失败 %d/3: %s" %
                              (self._preview_errors, (message or "")[:120]))
        if self._preview_errors >= 3:
            self.stop_preview(update_text=False)
            self.lbl_info.setText("预览已停止: %s" % (message or "连续取帧失败")[:180])
            self.ctx.log("摄像头预览失败: %s" % message, "error")

    def _rotation_degrees(self):
        value = self.cmb_rotation.currentData()
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _orient_camera_pixmap(self, pm):
        """按用户选择校正原始 NV12 帧方向；不改变底层驱动和采集 FPS。"""
        degrees = self._rotation_degrees()
        if pm is None or pm.isNull() or degrees == 0:
            return pm
        return pm.transformed(QTransform().rotate(degrees), Qt.FastTransformation)

    def _accept_camera_pixmap(self, source):
        # 保留未经旋转的共享副本，切换方向时无需重启摄像头或等待下一帧。
        self._camera_source_pixmap = source
        oriented = self._orient_camera_pixmap(source)
        self._pixmap = oriented
        self._display_pixmap(oriented)
        return oriented

    def _rotation_changed(self, _index):
        degrees = self._rotation_degrees()
        key = ("camera_rotation_ssh" if self.ctx.resolve_target(self.cmb_target.currentData()) == "ssh"
               else "camera_rotation")
        self.ctx.settings[key] = degrees
        theme.save_settings(self.ctx.settings)
        if self.cmb_mode.currentIndex() == 1 and self._camera_source_pixmap is not None:
            self._pixmap = self._orient_camera_pixmap(self._camera_source_pixmap)
            self._display_pixmap(self._pixmap)
            self.btn_save.setEnabled(True)
            self.lbl_info.setText("%d×%d · %s" %
                                  (self._pixmap.width(), self._pixmap.height(),
                                   self.cmb_rotation.currentText()))

    def _display_pixmap(self, pm):
        viewport = self.scroll.viewport().size()
        width = max(320, viewport.width() - 12)
        height = max(240, viewport.height() - 12)
        self.preview.setPixmap(pm.scaled(width, height, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        self.preview.setStyleSheet("")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap is not None:
            self._display_pixmap(self._pixmap)

    def hideEvent(self, event):
        self.stop_preview(update_text=self._preview_active)
        super().hideEvent(event)

    def shutdown(self):
        self.stop_preview(update_text=False)
        for worker in list(self._owned_stream_workers):
            worker.stop()
            if not worker.wait(12000):
                worker.remote.close()
                worker.wait(3000)
            if not worker.isRunning():
                self._stream_worker_finished(worker)

    def save(self):
        if self._pixmap is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存当前画面",
            ("camera_%s.png" if self.cmb_mode.currentIndex() == 1 else "screenshot_%s.png")
            % time.strftime("%Y%m%d_%H%M%S"),
            "PNG 图片 (*.png);;所有文件 (*)")
        if not path:
            return
        if self._pixmap.save(path, "PNG"):
            self.ctx.log("截图已保存: %s" % path, "ok")
            self.lbl_info.setText("已保存到 %s" % path)
        else:
            QMessageBox.critical(self, "保存失败", "无法写入 %s" % path)

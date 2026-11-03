# -*- coding: utf-8 -*-
"""应用上下文：集中管理后端会话、当前设备、设置，并通过信号通知各页面。

所有页面只依赖本对象，不直接互相引用，便于维护与测试。
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from PySide6.QtCore import QObject, Signal, QThread, QTimer

from core import adb_backend as ab
from core import ssh_backend


class Worker(QThread):
    """把一个可调用对象放到后台线程执行，结果通过信号回到主线程。"""

    done = Signal(object, object)     # (result, error)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            res = self._fn()
            self.done.emit(res, None)
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, e)


class AppContext(QObject):
    # 设备
    devices_changed = Signal(list)          # List[DeviceInfo]
    device_selected = Signal(object)        # DeviceInfo | None
    # 远程
    remote_changed = Signal(bool)           # 是否处于远程模式
    # 日志
    log_message = Signal(str, str)          # (level, text)
    # 状态栏
    status_changed = Signal(str)            # 简短状态文本
    busy_changed = Signal(bool, str)        # 是否忙碌 + 描述

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        exe = settings.get("adb_path") or ""
        self.adb = ab.Adb(exe if exe and os.path.isfile(exe) else ab.find_adb())
        self.devices: List[ab.DeviceInfo] = []
        self.current: Optional[ab.DeviceInfo] = None
        self.remote: Optional[ssh_backend.RemoteSession] = None
        self._workers: List[Worker] = []
        self.cwd = "/data/local/tmp"            # 设备侧当前目录
        self.local_cwd = os.path.expanduser("~")
        self.remote_cwd = ""
        self._refresh_generation = 0

    # ---- 后台任务 ------------------------------------------------------
    def run_async(self, fn, on_done=None, busy_text: str = ""):
        """在后台线程执行 fn，完成后在主线程调用 on_done(result, error)。"""
        w = Worker(fn, self)
        self._workers.append(w)

        def _cleanup(res, err):
            try:
                if on_done:
                    on_done(res, err)
            finally:
                if w in self._workers:
                    self._workers.remove(w)
                w.deleteLater()

        w.done.connect(_cleanup)
        if busy_text:
            self.busy_changed.emit(True, busy_text)
            w.done.connect(lambda *_: self.busy_changed.emit(False, ""))
        w.start()
        return w

    def stop_all_workers(self):
        for w in list(self._workers):
            try:
                w.requestInterruption()
                w.quit()
                if not w.wait(1500):
                    # Worker.run() 执行的是阻塞式外部 I/O，quit() 无法中断；
                    # 程序退出时兜底终止，避免 QThread 尚在运行就被销毁。
                    w.terminate()
                    w.wait(1000)
            except Exception:  # noqa: BLE001
                pass
        self._workers.clear()

    # ---- 日志 ----------------------------------------------------------
    def log(self, text: str, level: str = "info"):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        self.log_message.emit(level, line)

    def status(self, text: str):
        self.status_changed.emit(text)

    # ---- 远程模式 ------------------------------------------------------
    @property
    def is_remote(self) -> bool:
        return self.remote is not None

    def connect_remote(self, prof: dict, on_done=None):
        """建立 SSH 远程连接。"""
        def work():
            sess = ssh_backend.RemoteSession(
                prof.get("host", ""), prof.get("port", 22),
                prof.get("user", ""), prof.get("password", ""),
                prof.get("adb", ""))
            sess.connect()
            return sess

        def done(sess, err):
            if err:
                self.log("SSH 连接失败: %s" % err, "error")
                if on_done:
                    on_done(False, err)
                return
            # 会话类型切换属于上下文状态变化；先清空旧模式的设备选择，
            # 再通知界面并刷新。UI 状态槽可能被重复调用，不能承担此副作用。
            self.current = None
            self.remote = sess
            self.remote_cwd = sess.remote_home or "~"
            self.log("已进入 SSH 远程模式: %s，远端 adb=%s" % (sess.describe(), sess.adb_remote))
            self.remote_changed.emit(True)
            self.refresh_devices()
            if on_done:
                on_done(True, sess)

        self.run_async(work, done, busy_text="正在连接 SSH…")

    def disconnect_remote(self):
        sess = self.remote
        self.current = None
        self.remote = None
        if sess:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
        self.log("已退出远程模式，回到本机")
        self.remote_changed.emit(False)
        self.refresh_devices()

    # ---- 设备 ----------------------------------------------------------
    def refresh_devices(self, on_done=None):
        self._refresh_generation += 1
        generation = self._refresh_generation
        remote = self.remote

        def work():
            if remote is not None:
                ver = remote.adb_version()
                devs = []
                if ver:
                    for serial, state, raw in remote.adb_devices():
                        devs.append(ab.DeviceInfo(serial, state, raw))
                return ver, devs
            ok, ver = self.adb.version()
            devs = self.adb.devices() if ok else []
            return ver, devs

        def done(res, err):
            # 自动刷新、手动刷新和模式切换可能交叠；只接受最新且仍属于
            # 同一连接模式的结果，避免旧 SSH 结果覆盖本地设备列表。
            if generation != self._refresh_generation or self.remote is not remote:
                return
            if err:
                self.log("刷新设备失败: %s" % err, "error")
                if on_done:
                    on_done([])
                return
            ver, devs = res or ("", [])
            self.devices = devs
            if self.current is not None:
                current = next((dev for dev in devs
                                if dev.serial == self.current.serial and dev.usable), None)
                self.current = current
            serial = self.current.serial if self.current is not None else None
            self.adb.serial = serial
            if self.remote is not None:
                self.remote.serial = serial or ""
            self.device_selected.emit(self.current)
            self.devices_changed.emit(devs)
            if devs:
                self.status("找到 %d 个设备" % len(devs))
            else:
                self.status("未发现设备")
            if on_done:
                on_done(devs)

        self.run_async(work, done, busy_text="刷新设备…")

    def select_device(self, dev: Optional[ab.DeviceInfo]):
        self.current = dev if (dev and dev.usable) else None
        serial = self.current.serial if self.current is not None else None
        self.adb.serial = serial
        if self.remote is not None:
            self.remote.serial = serial or ""
        if dev:
            self.log("当前设备: %s" % dev.label())
        self.device_selected.emit(self.current)

    # ---- 统一执行入口（区分本地/远程）----------------------------------
    def adb_shell(self, cmd: str, timeout: float = 60):
        """执行 adb shell 命令，返回 (rc, output)；自动适配远程模式。"""
        if self.remote is not None:
            return self.remote.adb(["shell", cmd], timeout=timeout)
        return self.adb.shell(cmd, timeout=timeout)

    def adb_ls(self, path: str):
        if self.remote is not None:
            rc, out = self.remote.adb(["shell", "ls -la %s" % ab.quote(path)], timeout=40)
            ok = rc == 0 and "No such file" not in out and "not found" not in (out.split("\n")[0] if out else "")
            return ok, out
        return self.adb.ls(path)

    def abs_remote(self, name: str) -> str:
        """设备侧绝对路径（始终 POSIX 风格）。"""
        n = (name or "").rstrip("/")
        if n.startswith("/"):
            return n
        base = (self.cwd or "/").rstrip("/")
        return (base + "/" + n) if base else ("/" + n)

"""Independent Windows / SSH / ADB endpoints and copy routes (no Qt)."""
from __future__ import annotations

import os
import posixpath
import shutil
import uuid
from dataclasses import dataclass

from core import adb_backend as ab


@dataclass(frozen=True)
class FileEndpoint:
    kind: str
    remote: object = None
    adb: object = None
    serial: str = ""

    @property
    def identity(self):
        return self.kind, id(self.remote), self.serial

    def require(self):
        if self.kind == "ssh" and self.remote is None:
            raise RuntimeError("请先连接 SSH 目标")
        if self.kind == "adb" and not self.serial:
            raise RuntimeError("请先选择可用的 ADB 设备；Windows 与 SSH 传输无需 ADB")

    def join(self, directory, name):
        return (os.path.join if self.kind == "windows" else posixpath.join)(directory, name)

    def command(self, command, timeout=120):
        self.require()
        if self.kind == "ssh":
            rc, out = self.remote.exec(command, timeout=timeout)
        elif self.remote is not None:
            rc, out = self.remote.adb(["-s", self.serial, "shell", command], timeout=timeout)
        else:
            rc, out = self.adb.run(["shell", command], serial=self.serial, timeout=timeout)
        if rc:
            raise RuntimeError((out or "远端操作失败").strip())
        return out

    def adb_copy(self, direction, source, destination):
        self.require()
        args = ["-s", self.serial, direction, source, destination]
        if self.remote is not None:
            rc, out = self.remote.adb(args, timeout=1800)
        else:
            rc, out = self.adb.run(args, timeout=1800, serial=None)
        if rc:
            raise RuntimeError((out or "ADB 传输失败").strip())

    def exists(self, path):
        self.require()
        if self.kind == "windows":
            return os.path.lexists(path)
        out = self.command("if [ -e %s ] || [ -L %s ]; then echo EXISTS; fi" %
                           (ab.quote(path), ab.quote(path)), timeout=30)
        return "EXISTS" in out.split()

    def mkdir(self, path):
        self.require()
        if self.kind == "windows":
            os.makedirs(path, exist_ok=True)
        else:
            self.command("mkdir -p %s" % ab.quote(path))

    def rename(self, old, new):
        if self.exists(new):
            raise FileExistsError("目标已存在：%s" % new)
        if self.kind == "windows":
            os.rename(old, new)
        else:
            self.command("mv %s %s" % (ab.quote(old), ab.quote(new)))

    def delete(self, paths):
        self.require()
        for path in paths:
            if not path or path in ("/", "\\"):
                raise ValueError("不能删除文件系统根目录")
            if self.kind == "windows":
                resolved = os.path.abspath(path)
                if os.path.dirname(resolved) == resolved:
                    raise ValueError("不能删除磁盘根目录")
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            else:
                self.command("rm -rf %s" % ab.quote(path))

    def read_bytes(self, path, limit=2 * 1024 * 1024):
        self.require()
        if self.kind == "windows":
            with open(path, "rb") as f:
                return f.read(limit)
        if self.kind == "ssh":
            ok, text = self.remote.read_text(path, max_bytes=limit)
            if not ok:
                raise RuntimeError(text or "无法读取文件")
            return text.encode("utf-8")
        if self.remote is not None:
            # 绑定快照序列号，避免传输期间选择变化导致读取另一台设备。
            import base64
            text = self.command("head -c %d %s | base64" % (limit, ab.quote(path)))
            return base64.b64decode("".join(text.split()), validate=True)
        rc, text = self.adb.run(["shell", "head -c %d %s | base64" % (limit, ab.quote(path))],
                                serial=self.serial, timeout=60)
        if rc:
            raise RuntimeError(text)
        import base64
        return base64.b64decode("".join(text.split()), validate=True)


def endpoint_for(ctx, kind):
    current = ctx.current
    return FileEndpoint(kind, ctx.remote, ctx.adb,
                        current.serial if current is not None and current.usable else "")


def _check_result(result):
    ok, message = result
    if not ok:
        raise RuntimeError(message or "SFTP 传输失败")


def _copy_windows(source, destination):
    if os.path.isdir(source):
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def transfer_paths(source, destination, paths, destination_dir):
    """Copy selected absolute paths. Refuse existing targets before any writes.

    Remote ADB uses a unique Ubuntu staging directory only for Windows bridges.
    Transfer/session identities are frozen before dispatch to background workers.
    """
    source.require()
    destination.require()
    if not paths:
        return []
    if source.kind == "adb" and destination.kind == "adb":
        raise ValueError("请选择 Windows 或 SSH 作为另一侧")
    names = [(os.path.basename(p.rstrip("/\\")) if source.kind == "windows"
              else posixpath.basename(p.rstrip("/"))) for p in paths]
    if any(n in ("", ".", "..") or "/" in n or "\\" in n for n in names):
        raise ValueError("无效的文件名，或 Windows 不支持的路径名称")
    if len(set(n.casefold() if destination.kind == "windows" else n for n in names)) != len(names):
        raise ValueError("所选文件在目标端存在重名，请分开传输")
    targets = [destination.join(destination_dir, n) for n in names]
    for target in targets:
        if destination.exists(target):
            raise FileExistsError("目标已存在，请先重命名后传输：%s" % target)
    if source.kind == destination.kind:
        pathmod = os.path if source.kind == "windows" else posixpath
        for path, target in zip(paths, targets):
            parent = pathmod.normcase(pathmod.abspath(path))
            child = pathmod.normcase(pathmod.abspath(target))
            if child == parent or child.startswith(parent.rstrip("/\\") + pathmod.sep):
                raise ValueError("不能复制到自身或自己的子目录")
    messages = []
    for path, name, target in zip(paths, names, targets):
        if source.kind == "windows" and destination.kind == "windows":
            _copy_windows(path, target)
        elif source.kind == "windows" and destination.kind == "ssh":
            _check_result(destination.remote.upload_file(path, destination_dir))
        elif source.kind == "ssh" and destination.kind == "windows":
            _check_result(source.remote.download_file(path, destination_dir))
        elif source.kind == "ssh" and destination.kind == "ssh":
            if source.remote is not destination.remote:
                raise ValueError("两个 SSH 端必须属于当前连接")
            source.command("cp -a %s %s" % (ab.quote(path), ab.quote(target)), timeout=1800)
        elif source.kind == "ssh" and destination.kind == "adb":
            destination.adb_copy("push", path, destination_dir)
        elif source.kind == "adb" and destination.kind == "ssh":
            source.adb_copy("pull", path, destination_dir)
        else:
            device = source if source.kind == "adb" else destination
            if device.remote is None:
                device.adb_copy("pull" if source.kind == "adb" else "push",
                                path, destination_dir)
            else:
                stage = "/tmp/adbhost_transfer_%s" % uuid.uuid4().hex
                rc, out = device.remote.exec("mkdir %s" % ab.quote(stage), timeout=20)
                if rc:
                    raise RuntimeError(out or "无法创建传输临时目录")
                try:
                    staged = posixpath.join(stage, name)
                    if source.kind == "windows":
                        _check_result(device.remote.upload_file(path, stage))
                        device.adb_copy("push", staged, destination_dir)
                    else:
                        device.adb_copy("pull", path, stage)
                        _check_result(device.remote.download_file(staged, destination_dir))
                finally:
                    device.remote.exec("rm -rf %s" % ab.quote(stage), timeout=30)
        messages.append(name)
    return messages

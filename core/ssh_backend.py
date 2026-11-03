# -*- coding: utf-8 -*-
"""
SSH 远程后端：让上位机在 Windows 上运行，但**实际的 adb 操作都在 Ubuntu 上执行**。

使用场景
--------
开发板往往插在 Ubuntu 虚拟机/服务器上，Windows 这边没有 USB 直通。
开启 SSH 远程模式后：

    Windows 上位机  --ssh-->  Ubuntu  --adb-->  开发板

此时界面上的：
* 设备列表、shell 命令            -> 在 Ubuntu 上跑 `adb ...`
* 文件管理右侧（设备）            -> Ubuntu 上的 adb
* 文件管理左侧（本机）            -> **Ubuntu 的目录**（不再是 Windows 目录）
* 交互终端                        -> SSH PTY，等价于在 Ubuntu 终端里敲命令

退出远程模式后一切回到 Windows 本地。

实现方式
--------
优先用 paramiko（纯 Python，Windows/Linux/macOS 通用，支持密码与密钥）；
没有安装 paramiko 时自动回退到 PuTTY 的 plink/pscp（需要本机装有 PuTTY）。
"""

import os
import base64
import re
import shutil
import socket
import subprocess
import threading
import time

IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

try:
    import paramiko
    HAS_PARAMIKO = True
except Exception:  # noqa: BLE001
    paramiko = None
    HAS_PARAMIKO = False


class SSHError(RuntimeError):
    pass


def _which(name):
    p = shutil.which(name)
    if p:
        return p
    if IS_WINDOWS:
        for cand in (name + ".exe", name + ".bat"):
            p = shutil.which(cand)
            if p:
                return p
    return None


def find_putty_tools():
    """返回 (plink, pscp) 路径，找不到为 None。"""
    home = os.path.expanduser("~")
    dirs = [
        r"C:\Program Files\PuTTY",
        r"C:\Program Files (x86)\PuTTY",
        os.path.join(home, "AppData", "Local", "Programs", "PuTTY"),
        "/usr/bin", "/usr/local/bin",
    ]
    plink = _which("plink")
    pscp = _which("pscp")
    if not plink or not pscp:
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for exe in ("plink.exe", "plink"):
                p = os.path.join(d, exe)
                if not plink and os.path.isfile(p):
                    plink = p
            for exe in ("pscp.exe", "pscp"):
                p = os.path.join(d, exe)
                if not pscp and os.path.isfile(p):
                    pscp = p
    return plink, pscp


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------

DEFAULT_PROFILES = {
    "last": 0,
    # 首次运行不预置任何真实地址或凭据。用户保存的记录只写入本机配置目录，
    # 不属于源码仓库的一部分。
    "profiles": [],
}


def config_path():
    base = os.environ.get("APPDATA") if IS_WINDOWS else os.path.expanduser("~/.config")
    if not base:
        base = os.path.expanduser("~")
    d = os.path.join(base, "AdbHostTool")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return os.path.join(d, "ssh_profiles.json")


def load_config():
    import json
    p = config_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "profiles" not in data:
            return dict(DEFAULT_PROFILES)
        data.setdefault("last", 0)
        return data
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_PROFILES)


def save_config(cfg):
    import json
    p = config_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, p
    except OSError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 远程会话
# ---------------------------------------------------------------------------

class RemoteSession:
    """一条到 Ubuntu 的 SSH 连接，并在其上执行 adb / 读写远端文件。"""

    def __init__(self, host, port=22, user="", password="", adb_hint="", keyfile=""):
        self.host = host
        self.port = int(port or 22)
        self.user = user
        self.password = password
        self.adb_hint = adb_hint or ""
        self.keyfile = keyfile or ""
        self.client = None
        self.backend = ""          # "paramiko" | "plink"
        self._plink = None
        self._pscp = None
        self.adb_remote = ""       # Ubuntu 上 adb 的绝对路径
        self.serial = ""           # 当前设备；设备级 adb 命令自动附加 -s
        self.remote_home = ""
        self.remote_os = ""
        # 串行化远端命令：界面里多个后台线程会同时调 exec()/adb()，
        # 并发往同一条 SSH 通道发命令会互相干扰（典型表现：push 偶发失败）。
        self._cmd_lock = threading.Lock()

    # ---- 连接 ----------------------------------------------------------
    def connect(self, timeout=15):
        if HAS_PARAMIKO:
            self._connect_paramiko(timeout)
            self.backend = "paramiko"
        else:
            self._connect_plink(timeout)
            self.backend = "plink"
        self._probe_remote()
        return True

    def _connect_paramiko(self, timeout):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(port=self.port, username=self.user, timeout=timeout,
                  banner_timeout=timeout, auth_timeout=timeout,
                  allow_agent=False, look_for_keys=False)
        if self.password:
            kw["password"] = self.password
        if self.keyfile:
            kw["key_filename"] = self.keyfile
            kw["look_for_keys"] = not self.password
        try:
            c.connect(self.host, **kw)
        except Exception as e:  # noqa: BLE001
            raise SSHError("无法连接 %s@%s:%s —— %s" % (self.user, self.host, self.port, e))
        self.client = c

    def _connect_plink(self, timeout):
        self._plink, self._pscp = find_putty_tools()
        if not self._plink:
            raise SSHError(
                "未找到可用的 SSH 客户端。\n"
                "请任选其一：\n"
                "  1) pip install paramiko      （推荐，纯 Python）\n"
                "  2) 安装 PuTTY 并把 plink.exe 加入 PATH")
        rc, out = self.exec("echo SSH_OK", timeout=timeout)
        if "SSH_OK" not in out:
            raise SSHError("plink 连接失败：%s" % out.strip()[:400])

    def _probe_remote(self):
        rc, out = self.exec("echo HOME=$HOME; uname -s 2>/dev/null || echo Windows", timeout=15)
        m = re.search(r"HOME=(\S+)", out or "")
        if m:
            self.remote_home = m.group(1)
        m = re.search(r"^(Linux|Darwin|Windows|FreeBSD|MINGW.*|CYGWIN.*)$", out or "", re.M)
        self.remote_os = (m.group(1) if m else "").strip()
        self.adb_remote = self._locate_adb()

    def _locate_adb(self):
        """在远端定位 adb：优先用户指定，其次 PATH，再次常见位置，最后挑版本最高的。"""
        # 注意：这些候选里有 $HOME 之类需要展开的变量，所以**不能用单引号**包裹，
        # 统一用双引号；用户显式填写的路径则单独做安全转义。
        cands = []
        if self.adb_hint:
            cands.append(_shq(self.adb_hint))
        cands += [
            '"$(command -v adb 2>/dev/null)"',
            '"$HOME/tools/platform-tools-new/platform-tools/adb"',
            '"$HOME/platform-tools/adb"',
            '"/usr/lib/android-sdk/platform-tools/adb"',
            '"/opt/platform-tools/adb"',
            '"/opt/android-sdk/platform-tools/adb"',
        ]
        best, best_ver = "", ""
        for c in cands:
            rc, out = self.exec(
                'p=%s; if [ -x "$p" ]; then readlink -f "$p"; fi' % c,
                timeout=20)
            real = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
            if not real or not real.startswith("/"):
                continue
            rc2, vout = self.exec('%s version 2>/dev/null | head -2' % _shq(real), timeout=20)
            m = re.search(r"Version\s+(\S+)", vout or "")
            if not m:
                continue
            ver = m.group(1)
            key = tuple(int(x) for x in re.findall(r"\d+", ver)) or (0,)
            cur = tuple(int(x) for x in re.findall(r"\d+", best_ver)) or (0,)
            if not best or key > cur:
                # 存**展开后的绝对路径**，后续 adb() 直接使用，避免二次转义
                best, best_ver = real, ver
        return best or "adb"

    # ---- 执行 ----------------------------------------------------------
    def exec(self, command, timeout=60):
        """在远端执行一条 shell 命令，返回 (rc, 合并输出)。"""
        # 串行执行：多个后台线程同时打同一条 SSH 通道会互相干扰
        with self._cmd_lock:
            if self.backend == "paramiko" or (self.client is not None):
                return self._exec_paramiko(command, timeout)
            return self._exec_plink(command, timeout)

    def exec_cancellable(self, command, cancel_event, timeout=60):
        """执行可取消命令；取消返回码为 130，超时返回 124。"""
        deadline = time.monotonic() + float(timeout)
        with self._cmd_lock:
            if self.backend == "paramiko" or self.client is not None:
                channel = None
                try:
                    transport = self.client.get_transport()
                    if transport is None or not transport.is_active():
                        return 1, "SSH 连接不可用"
                    channel = transport.open_session(timeout=min(10, timeout))
                    channel.exec_command(command)
                    stdout = bytearray()
                    stderr = bytearray()
                    while True:
                        while channel.recv_ready():
                            stdout.extend(channel.recv(65536))
                        while channel.recv_stderr_ready():
                            stderr.extend(channel.recv_stderr(65536))
                        if cancel_event.is_set():
                            channel.close()
                            return 130, (stdout + stderr).decode("utf-8", "replace")
                        if channel.exit_status_ready():
                            while channel.recv_ready():
                                stdout.extend(channel.recv(65536))
                            while channel.recv_stderr_ready():
                                stderr.extend(channel.recv_stderr(65536))
                            return channel.recv_exit_status(), (stdout + stderr).decode(
                                "utf-8", "replace")
                        if time.monotonic() >= deadline:
                            channel.close()
                            return 124, "远程命令超时"
                        time.sleep(0.03)
                except Exception as exc:  # noqa: BLE001
                    return 1, "远端执行失败: %s" % exc
                finally:
                    if channel is not None:
                        try:
                            channel.close()
                        except Exception:  # noqa: BLE001
                            pass

            args = [self._plink, "-ssh", "-batch", "-no-antispoof",
                    "-P", str(self.port), "-l", self.user, "-pw", self.password,
                    "-hostkey", self._hostkey_fp or "accept-new", self.host, command]
            try:
                proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=NO_WINDOW if IS_WINDOWS else 0)
                while proc.poll() is None:
                    if cancel_event.is_set() or time.monotonic() >= deadline:
                        proc.kill()
                        out, _ = proc.communicate(timeout=5)
                        return (130 if cancel_event.is_set() else 124), out.decode(
                            "utf-8", "replace")
                    time.sleep(0.05)
                out, _ = proc.communicate(timeout=5)
                return proc.returncode, out.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                return 1, "plink 执行失败: %s" % exc

    def _exec_paramiko(self, command, timeout):
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
            return rc, out + err
        except Exception as e:  # noqa: BLE001
            return 1, "远端执行失败: %s" % e

    def _exec_plink(self, command, timeout):
        try:
            p = subprocess.run(
                [self._plink, "-ssh", "-batch", "-no-antispoof",
                 "-P", str(self.port), "-l", self.user, "-pw", self.password,
                 "-hostkey", self._hostkey_fp or "accept-new", self.host, command],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, creationflags=NO_WINDOW if IS_WINDOWS else 0)
            return p.returncode, p.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return 124, "超时: %s" % command
        except Exception as e:  # noqa: BLE001
            return 1, "plink 执行失败: %s" % e

    # plink 需要 hostkey；首次用 -hostkey 接受
    _hostkey_fp = ""

    # ---- adb 相关 ------------------------------------------------------
    _ADB_HOST_COMMANDS = {
        "devices", "version", "start-server", "kill-server", "connect",
        "disconnect", "pair", "track-devices", "server-status", "mdns",
        "host-features", "features",
    }

    def _scoped_adb_args(self, args):
        """为设备级命令绑定当前序列号，主机级命令保持原样。"""
        values = [str(value) for value in args]
        if not values or not self.serial:
            return values
        if values[0] in ("-s", "-d", "-e", "-t"):
            return values
        if values[0] in self._ADB_HOST_COMMANDS:
            return values
        return ["-s", self.serial] + values

    def _adb_command(self, args):
        values = self._scoped_adb_args(args)
        return "%s %s" % (_shq(self.adb_remote),
                           " ".join(_shq(value) for value in values))

    def adb(self, args, timeout=120, want_rc=True):
        """在远端执行 adb 子命令，args 为列表。返回 (rc, 输出)。

USB/adb 链路偶发抖动时（典型报错 cannot stat / device offline / closed），
自动重试一次，避免界面里出现莫名其妙的失败。
        """
        cmd = self._adb_command(args) + " 2>&1"

        rc, out = self.exec(cmd, timeout=timeout)
        if rc == 0 or not self._is_transient_adb_error(out):
            return rc, out
        # 只对"链路级"错误重试；真正的业务错误（文件不存在等）不重试
        for attempt in range(2):
            time.sleep(0.6 * (attempt + 1))
            rc2, out2 = self.exec(cmd, timeout=timeout)
            if rc2 == 0 or not self._is_transient_adb_error(out2):
                return rc2, out2
            rc, out = rc2, out2
        return rc, out

    def adb_cancellable(self, args, cancel_event, timeout=120):
        return self.exec_cancellable(self._adb_command(args) + " 2>&1",
                                     cancel_event, timeout=timeout)

    def adb_read_bytes(self, remote_path, max_bytes=32 * 1024 * 1024, timeout=120):
        """通过设备端 base64 二进制安全读取文件，并限制最大内存占用。

        当前 RK3568 的旧 adbd 对 ``adb exec-out`` 返回 ``error: closed``，而
        ``adb shell cat`` 又会改写二进制换行；设备端先转成 ASCII base64 可同时
        避开这两个问题，并继续复用 adb() 的瞬态错误重试。
        """
        quoted = _shq(remote_path)
        size_cmd = ("if [ -f %s ]; then wc -c < %s; "
                    "else echo __ADBHOST_NOT_FILE__; exit 2; fi") % (quoted, quoted)
        rc, out = self.adb(["shell", size_cmd], timeout=min(timeout, 60))
        if rc != 0:
            return False, b"", (out or "不是普通文件").strip()
        try:
            size = int((out or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False, b"", "无法读取设备文件大小: %s" % (out or "").strip()
        if size > int(max_bytes):
            return False, b"", "文件过大(%d 字节)，请到“文件管理”页下载" % size
        rc, out = self.adb(["shell", "base64 < %s" % quoted], timeout=timeout)
        if rc != 0:
            return False, b"", (out or "读取失败").strip()
        try:
            return True, base64.b64decode("".join((out or "").splitlines()),
                                          validate=True), ""
        except Exception as exc:  # noqa: BLE001
            return False, b"", "二进制数据解码失败: %s" % exc

    def adb_with_stdin(self, payload, shell_command, timeout=120):
        """把二进制 payload 送入当前设备命令的 stdin。

        旧 adbd 不会可靠转发主机侧管道 EOF，因此不能使用
        ``cat ubuntu.tmp | adb shell ...``。这里先 push 到设备临时文件，再让
        设备 shell 用本地 ``<`` 重定向，命令能稳定收到 EOF 并退出。
        """
        if not self.serial:
            return False, "未选择目标设备"
        tag = "%d_%d" % (int(time.time() * 1000), threading.get_ident())
        ubuntu_temp = "/tmp/.adbhost_stdin_%s" % tag
        device_temp = "/data/local/tmp/.adbhost_stdin_%s" % tag
        ok, message = self.upload_bytes(payload, ubuntu_temp)
        if not ok:
            return False, message
        try:
            rc, out = self.adb(["push", ubuntu_temp, device_temp], timeout=timeout)
            if rc != 0:
                return False, out or "adb push 失败"
            device_command = (
                "%s < %s; rc=$?; rm -f %s; exit $rc"
                % (shell_command, _shq(device_temp), _shq(device_temp)))
            rc, out2 = self.adb(["shell", device_command], timeout=timeout)
            return rc == 0, (out or "") + (out2 or "")
        finally:
            self.exec("rm -f %s" % _shq(ubuntu_temp), timeout=20)
            self.adb(["shell", "rm -f %s" % _shq(device_temp)], timeout=20)

    def open_tcp_channel(self, host, port, timeout=5):
        """通过现有 SSH transport 打开到 Ubuntu 侧地址的 TCP 通道。"""
        if self.backend != "paramiko" or self.client is None:
            raise SSHError("远程 TCP 通道需要 paramiko 后端")
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise SSHError("SSH 连接不可用")
        channel = transport.open_channel(
            "direct-tcpip", (str(host), int(port)), ("127.0.0.1", 0),
            timeout=float(timeout))
        channel.settimeout(0.5)
        return channel

    @staticmethod
    def _is_transient_adb_error(out: str) -> bool:
        """判断是否为可重试的链路抖动错误。"""
        s = (out or "").lower()
        if not s:
            return False
        # 这些是"真"错误，重试也没用
        for solid in ("no such file", "does not exist", "permission denied",
                      "no space left", "is a directory", "not a directory"):
            if solid in s:
                return False
        for transient in ("device offline", "device not found", "no devices",
                          "error: closed", "cannot stat", "connection reset",
                          "protocol fault", "failed to get feature set",
                          "adb server didn't ack", "adb server is out of date"):
            if transient in s:
                return True
        return False

    def adb_devices(self):
        rc, out = self.adb(["devices", "-l"], timeout=30)
        res = []
        for line in (out or "").splitlines():
            s = line.strip()
            if not s or s.startswith("List of devices") or s.startswith("*"):
                continue
            parts = s.split()
            if len(parts) >= 2:
                res.append((parts[0], parts[1], s))
        return res

    def adb_version(self):
        rc, out = self.exec('%s version 2>&1' % _shq(self.adb_remote), timeout=20)
        return (out or "").strip()

    # ---- 远端文件系统 ---------------------------------------------------
    def list_dir(self, path):
        """返回 (ok, [(name, is_dir, size, mtime_text)], 错误)。

        `path` 允许含 `~`：单独处理，避免被单引号包裹后无法展开。
        """
        p = (path or "").strip()
        if p in ("", "~", "~/"):
            target = '"$HOME"'
        elif p.startswith("~/"):
            target = '"$HOME"%s' % _shq(p[1:])
        else:
            target = _shq(p)
        cmd = ('cd %s 2>/dev/null && for f in .* *; do '
               '[ "$f" = "." ] && continue; [ "$f" = ".." ] && continue; '
               '[ -e "$f" ] || continue; '
               'if [ -d "$f" ]; then t=d; else t=f; fi; '
               's=$(stat -c %%s "$f" 2>/dev/null || echo 0); '
               'm=$(stat -c %%y "$f" 2>/dev/null | cut -c1-16); '
               'printf "%%s\\t%%s\\t%%s\\t%%s\\n" "$t" "$s" "$m" "$f"; done') % target
        rc, out = self.exec(cmd, timeout=40)
        if rc != 0 and not (out or "").strip():
            return False, [], (out or "").strip() or "无法列出目录"
        entries = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            t, size, mtime, name = parts
            try:
                size_i = int(size)
            except ValueError:
                size_i = None
            entries.append((name, t == "d", size_i, mtime))
        entries.sort(key=lambda e: (0 if e[1] else 1, e[0].lower()))
        return True, entries, ""

    def realpath(self, path):
        p = (path or "").strip()
        if p in ("", "~", "~/"):
            target = '"$HOME"'
        elif p.startswith("~/"):
            target = '"$HOME"%s' % _shq(p[1:])
        else:
            target = _shq(p)
        rc, out = self.exec('cd %s 2>/dev/null && pwd' % target, timeout=15)
        return (out or "").strip() or path

    def mkdir(self, path):
        return self.exec('mkdir -p %s 2>&1 && echo OK' % _shq(path), timeout=30)

    def delete(self, paths):
        q = " ".join(_shq(p) for p in paths)
        return self.exec('rm -rf %s && echo OK' % q, timeout=120)

    def rename(self, old, new):
        return self.exec('mv %s %s 2>&1 && echo OK' % (_shq(old), _shq(new)), timeout=30)

    def read_text(self, path, max_bytes=2 * 1024 * 1024):
        rc, out = self.exec('head -c %d %s 2>/dev/null' % (max_bytes, _shq(path)), timeout=60)
        return rc == 0, out

    # ---- 上传 / 下载 ----------------------------------------------------
    def upload_bytes(self, data, remote_path):
        """把本地字节写到远端文件（走 SFTP 或 base64，不依赖 scp）。"""
        if self.backend == "paramiko":
            try:
                sftp = self.client.open_sftp()
                with sftp.open(remote_path, "wb") as f:
                    f.write(data)
                sftp.close()
                return True, "OK"
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        return self._upload_base64(data, remote_path)

    def upload_file(self, local_path, remote_dir):
        """把本地文件/目录上传到远端目录。返回 (ok, 输出)。"""
        if self.backend == "paramiko":
            try:
                sftp = self.client.open_sftp()
                name = os.path.basename(local_path.rstrip("/\\"))
                target = remote_dir.rstrip("/") + "/" + name
                if os.path.isdir(local_path):
                    self._sftp_put_dir(sftp, local_path, target)
                else:
                    sftp.put(local_path, target)
                sftp.close()
                return True, "已上传 %s -> %s" % (name, remote_dir)
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        # plink 后备：用 pscp
        try:
            p = subprocess.run(
                [self._pscp, "-batch", "-P", str(self.port), "-pw", self.password,
                 "-r", local_path, "%s@%s:%s" % (self.user, self.host, remote_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
                creationflags=NO_WINDOW if IS_WINDOWS else 0)
            return p.returncode == 0, p.stdout.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def download_file(self, remote_path, local_dir):
        if self.backend == "paramiko":
            try:
                sftp = self.client.open_sftp()
                name = os.path.basename(remote_path.rstrip("/")) or "download"
                target = os.path.join(local_dir, name)
                self._sftp_get_path(sftp, remote_path, target)
                sftp.close()
                return True, "已下载 %s -> %s" % (remote_path, local_dir)
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        try:
            p = subprocess.run(
                [self._pscp, "-batch", "-P", str(self.port), "-pw", self.password, "-r",
                 "%s@%s:%s" % (self.user, self.host, remote_path), local_dir],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
                creationflags=NO_WINDOW if IS_WINDOWS else 0)
            return p.returncode == 0, p.stdout.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    @staticmethod
    def _sftp_put_dir(sftp, local_dir, remote_dir):
        try:
            sftp.mkdir(remote_dir)
        except Exception:  # noqa: BLE001
            pass
        for name in os.listdir(local_dir):
            lp = os.path.join(local_dir, name)
            rp = remote_dir + "/" + name
            if os.path.isdir(lp):
                RemoteSession._sftp_put_dir(sftp, lp, rp)
            else:
                sftp.put(lp, rp)

    @staticmethod
    def _sftp_get_path(sftp, remote_path, local_path):
        import stat as statmod
        try:
            st = sftp.stat(remote_path)
        except Exception:  # noqa: BLE001
            raise
        if statmod.S_ISDIR(st.st_mode):
            os.makedirs(local_path, exist_ok=True)
            for ent in sftp.listdir_attr(remote_path):
                RemoteSession._sftp_get_path(sftp, remote_path.rstrip("/") + "/" + ent.filename,
                                             os.path.join(local_path, ent.filename))
        else:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            sftp.get(remote_path, local_path)

    def _upload_base64(self, data, remote_path):
        import base64
        b64 = base64.b64encode(data).decode("ascii")
        cmd = "echo %s | base64 -d > %s && echo OK" % (_shq(b64), _shq(remote_path))
        rc, out = self.exec(cmd, timeout=120)
        return ("OK" in out), out

    # ---- 交互式终端 -----------------------------------------------------
    def open_shell(self, term="xterm-256color", width=100, height=30, command=None):
        """打开一个远端 PTY，返回 (写函数, 读函数, 关闭函数)。

        ``command`` 不为空时，在 PTY 里直接执行该命令（例如 `adb shell`），
        这样终端里的内容就是命令本身的会话，而不是 Ubuntu 的 login shell。
        """
        if self.backend == "paramiko":
            # paramiko 的 SSHClient.invoke_shell() 不支持指定命令，
            # 所以直接用底层通道：get_pty() + exec_command()，
            # 这样远端命令（如 adb shell）就运行在一个真正的 PTY 里。
            chan = self.client.get_transport().open_session()
            chan.get_pty(term=term, width=width, height=height)
            chan.exec_command(command if command else "exec bash -l")
            chan.settimeout(0.0)

            def write(b):
                try:
                    chan.send(b if isinstance(b, bytes) else b.encode())
                except Exception:  # noqa: BLE001
                    pass

            def read():
                if chan.recv_ready():
                    return chan.recv(65536)
                return b""

            def close():
                try:
                    chan.close()
                except Exception:  # noqa: BLE001
                    pass

            def resize_pty(width, height):
                try:
                    chan.resize_pty(width=max(20, int(width)),
                                    height=max(4, int(height)))
                except Exception:  # noqa: BLE001
                    pass

            # 保持原有三元返回值兼容；把当前 PTY 的 resize 能力挂到写函数上，
            # 多终端时只调整自己的 channel，不再遍历并误改其他终端。
            write.resize_pty = resize_pty
            return write, read, close
        raise SSHError("plink 后端暂不支持交互终端，请安装 paramiko：pip install paramiko")

    # ---- 关闭 ----------------------------------------------------------
    def close(self):
        try:
            if self.client is not None:
                self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.client = None

    @property
    def alive(self):
        if self.backend == "paramiko":
            t = self.client.get_transport() if self.client else None
            return bool(t and t.is_active())
        return bool(self._plink)

    def describe(self):
        return "%s@%s:%s (%s)" % (self.user, self.host, self.port, self.backend)


def _shq(s):
    """POSIX shell 单引号转义。"""
    return "'" + str(s).replace("'", "'\\''") + "'"

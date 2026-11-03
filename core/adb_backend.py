# -*- coding: utf-8 -*-
"""ADB 后端：设备发现、命令执行、文件传输、进度解析等纯业务逻辑。

本模块与界面框架完全无关（不导入任何 GUI 库），可被 PySide6 界面、
命令行工具或自动化测试直接复用。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import platform
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

IS_WINDOWS = platform.system().lower().startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

def popen_kwargs() -> dict:
    """仅进程隔离参数。

    Windows: 隐藏控制台窗口并新建进程组（便于整组 taskkill）。
    类 Unix: 新建进程组（便于整组终止）。
    """
    if IS_WINDOWS:
        return {"creationflags": NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def adb_spawn_kwargs() -> dict:
    """adb 子进程的 Popen 参数（一次给出，避免关键字重复）。

    类 Unix 上，若同时设置 stdout=PIPE 与 start_new_session=True，CPython 会改走
    posix_spawn 的 file_actions 路径，导致 stdin 管道不可用（实测 stdin 为 None）。
    因此在这里显式禁用 posix_spawn（3.10+），保证 fork/exec 与稳定的管道语义。
    """
    kw = popen_kwargs()
    if not IS_WINDOWS and sys.version_info >= (3, 10):
        kw["use_posix_spawn"] = False
    return kw


def kill_tree(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.call(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        else:
            import signal

            # 如果子进程是独立进程组（start_new_session），终止整组；
            # 否则只终止该进程本身，避免误杀与本程序同组的进程。
            try:
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def adb_version_of(exe: str) -> str:
    """读取 adb 的版本号（如 1.0.41）。失败返回空串。"""
    try:
        p = subprocess.run([exe, "version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10, **({"creationflags": NO_WINDOW} if IS_WINDOWS else {}))
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("Version "):
                return line.split(None, 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _ver_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def _adb_candidates() -> List[str]:
    """收集所有可能的 adb 路径（去重、去掉不存在的）。"""
    home = os.path.expanduser("~")
    exe = "adb.exe" if IS_WINDOWS else "adb"
    seen, out = set(), []

    def add(p):
        if not p:
            return
        try:
            p = os.path.abspath(p)
        except OSError:
            return
        if p in seen or not os.path.isfile(p) or not os.access(p, os.X_OK):
            return
        seen.add(p)
        out.append(p)

    # 1) 环境变量显式指定，最高优先
    add(os.environ.get("ADB"))
    add(os.environ.get("ADB_EXE"))

    # 2) PATH 中的所有 adb 副本（which -a 的效果）
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            add(os.path.join(d, exe))

    # 3) 常见 SDK / platform-tools 安装位置
    roots = []
    if IS_WINDOWS:
        roots += [
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.environ.get("ANDROID_HOME", ""),
            os.path.join(home, "AppData", "Local", "Android", "Sdk"),
            os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools"),
            r"C:\Android\Sdk", r"C:\platform-tools", r"C:\adb",
            os.path.join(home, "platform-tools"),
        ]
    else:
        roots += [
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.environ.get("ANDROID_HOME", ""),
            os.path.join(home, "Android", "Sdk"),
            os.path.join(home, "platform-tools"),
            os.path.join(home, "tools", "platform-tools-new", "platform-tools"),
            "/opt/platform-tools",
            "/usr/lib/android-sdk",
            "/usr/lib/android-sdk/platform-tools",
            "/opt/android-sdk",
        ]
    for root in roots:
        if not root:
            continue
        r = root.rstrip("/\\")
        add(os.path.join(r, exe))
        if os.path.basename(r).lower() != "platform-tools":
            add(os.path.join(r, "platform-tools", exe))
    return out


def find_adb() -> Optional[str]:
    """查找 adb，并**优先选用版本最高**的一个。

    这一点很关键：Ubuntu 软件源里的 adb 是 1.0.39，而 platform-tools 已到 1.0.41。
    两者混用会出现 "adb server version (41) doesn't match this client (39)"，
    adb 会反复杀掉并重建 server，期间设备可能短暂离线。
    """
    cands = _adb_candidates()
    if not cands:
        return None
    # 环境变量显式指定的，无条件优先
    for env_key in ("ADB", "ADB_EXE"):
        v = os.environ.get(env_key)
        if v:
            try:
                av = os.path.abspath(v)
            except OSError:
                av = v
            if av in cands:
                return av
    if len(cands) == 1:
        return cands[0]

    best, best_key = cands[0], _ver_tuple(adb_version_of(cands[0]))
    for c in cands[1:]:
        key = _ver_tuple(adb_version_of(c))
        if key > best_key:
            best, best_key = c, key
    return best


_RE_PCT_BRACKET = re.compile(r"\[\s*(\d+)%\]")
_RE_PCT_COLON = re.compile(r":\s*(\d+)%\s*$")
_RE_DONE = re.compile(r"\((\d+) bytes")
_RE_FAIL = re.compile(r"(error|failed|no such file|permission denied|closed)", re.I)


def parse_progress(line: str) -> Optional[Tuple[float, str]]:
    """解析 adb push/pull 输出行 -> (0~1, 文本)。"""
    s = line.strip()
    if not s:
        return None
    m = _RE_PCT_BRACKET.search(s) or _RE_PCT_COLON.search(s)
    if m:
        return int(m.group(1)) / 100.0, s
    if _RE_DONE.search(s):
        return 1.0, s
    if _RE_FAIL.search(s):
        return 0.0, s
    return None


def human_size(n) -> str:
    if n is None:
        return "-"
    if n < 0:
        return "<DIR>"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return ("%d %s" % (v, u)) if u == "B" else ("%.1f %s" % (v, u))
        v /= 1024.0
    return "%d B" % n


def quote(path: str) -> str:
    return shlex.quote(path)


# ===========================================================================
# adb 封装
# ===========================================================================


@dataclass
class DeviceInfo:
    serial: str
    state: str
    raw: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def model(self) -> str:
        m = re.search(r"model:(\S+)", self.raw) or re.search(r"product:(\S+)", self.raw)
        return m.group(1) if m else ""

    def label(self) -> str:
        s = self.serial
        if self.model:
            s += " (%s)" % self.model
        if self.state != "device":
            s += "  [%s]" % self.state
        return s


class Adb:
    """adb 命令行封装。所有方法可在线程中调用。"""

    def __init__(self, exe: Optional[str] = None):
        self.exe = exe or find_adb() or "adb"
        self.serial: Optional[str] = None
        # 路径含空格时 Windows 上 subprocess 用 list 形式即可，无需加引号；
        # 这里仅保证路径统一为绝对路径，避免工作目录变化导致找不到。
        if self.exe != "adb" and os.path.exists(self.exe):
            self.exe = os.path.abspath(self.exe)

    def _base(self, serial: Optional[str] = "auto") -> List[str]:
        cmd = [self.exe]
        s = self.serial if serial == "auto" else serial
        if s:
            cmd += ["-s", s]
        return cmd

    # ---- 通用执行 --------------------------------------------------------
    def run(self, args: List[str], timeout: float = 60, serial: Optional[str] = "auto") -> Tuple[int, str]:
        try:
            p = subprocess.run(
                self._base(serial) + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                **popen_kwargs(),
            )
            return p.returncode, p.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return 124, "命令超时(%.0fs): adb %s" % (timeout, " ".join(args))
        except FileNotFoundError:
            return 127, "未找到 adb 可执行文件: %s" % self.exe
        except Exception as e:  # noqa: BLE001
            return 1, "执行失败: %s" % e

    def popen(self, args: List[str], serial: Optional[str] = "auto", want_stdin: bool = False) -> subprocess.Popen:
        # 同时给 stdout=PIPE 与 start_new_session=True 时，CPython 会走 posix_spawn 的
        # 文件动作路径；类 Unix 上这里统一改回 fork/exec 以保持稳定的管道语义
        # （见 adb_spawn_kwargs），Windows 下行为不变。
        return subprocess.Popen(
            self._base(serial) + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if want_stdin else subprocess.DEVNULL,
            bufsize=0,
            **adb_spawn_kwargs(),
        )

    def popen_input(self, args: List[str], serial: Optional[str] = "auto") -> subprocess.Popen:
        """需要向 adb 子进程写数据的场景（例如 adb shell cat > file）。

        注意这里不输出到管道以外的方式读取，stdin 必须为管道；
        类 Unix 上同样禁用 posix_spawn 的文件动作模式，确保 stdin 管道可用。
        """
        return subprocess.Popen(
            self._base(serial) + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **adb_spawn_kwargs(),
        )

    # ---- 服务 / 设备 -----------------------------------------------------
    def version(self) -> Tuple[bool, str]:
        rc, out = self.run(["version"], timeout=20, serial=None)
        return rc == 0, out.strip()

    def kill_server(self) -> None:
        self.run(["kill-server"], timeout=15, serial=None)

    def start_server(self) -> Tuple[int, str]:
        return self.run(["start-server"], timeout=60, serial=None)

    def devices(self) -> List[DeviceInfo]:
        _, out = self.run(["devices", "-l"], timeout=30, serial=None)
        res = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                res.append(DeviceInfo(parts[0], parts[1], line))
        return res

    def connect_tcp(self, hostport: str) -> Tuple[int, str]:
        return self.run(["connect", hostport], timeout=20, serial=None)

    def disconnect_tcp(self, hostport: str) -> Tuple[int, str]:
        return self.run(["disconnect", hostport], timeout=15, serial=None)

    def forward(self, local: str, remote: str) -> Tuple[int, str]:
        return self.run(["forward", local, remote], timeout=20)

    def forward_remove(self, local: str) -> Tuple[int, str]:
        return self.run(["forward", "--remove", local], timeout=20)

    def reverse(self, remote: str, local: str) -> Tuple[int, str]:
        return self.run(["reverse", remote, local], timeout=20)

    def reverse_remove(self, remote: str) -> Tuple[int, str]:
        return self.run(["reverse", "--remove", remote], timeout=20)

    # ---- 设备端 ----------------------------------------------------------
    def shell(self, command: str, timeout: float = 60) -> Tuple[int, str]:
        return self.run(["shell", command], timeout=timeout)

    def push(self, local: str, remote: str, timeout: float = 3600) -> Tuple[int, str]:
        return self.run(["push", local, remote], timeout=timeout)

    def pull(self, remote: str, local: str, timeout: float = 3600) -> Tuple[int, str]:
        return self.run(["pull", remote, local], timeout=timeout)

    def ls(self, path: str) -> Tuple[bool, str]:
        q = quote(path)
        for args in (["ls", "-la", q], ["ls", "-l", q], ["busybox", "ls", "-la", q]):
            rc, out = self.shell(" ".join(args), timeout=30)
            if rc == 0 and "not found" not in out.split("\n")[0]:
                return True, out
        return False, out

    def probe_path(self, path: str) -> Tuple[str, Optional[int]]:
        """返回 ('dir'|'file'|'none', 文件字节数)。"""
        cmd = (
            "p=%s; if [ -d \"$p\" ]; then echo DIR; elif [ -e \"$p\" ]; then "
            "s=$(stat -c %%s \"$p\" 2>/dev/null); echo \"FILE ${s:-0}\"; else echo NONE; fi" % quote(path)
        )
        _, out = self.shell(cmd, timeout=30)
        for line in out.splitlines():
            line = line.strip()
            if line == "DIR":
                return "dir", None
            if line == "NONE":
                return "none", None
            if line.startswith("FILE"):
                parts = line.split()
                try:
                    return "file", int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    return "file", 0
        return "none", None

    def read_file_bytes(self, remote: str, max_bytes: int = 32 * 1024 * 1024) -> Tuple[bool, bytes, str]:
        """二进制安全读取设备文件（pull 到临时文件）。"""
        kind, size = self.probe_path(remote)
        if kind != "file":
            return False, b"", "不是普通文件: %s" % remote
        if size is not None and size > max_bytes:
            return False, b"", "文件过大(%s)，请到“文件管理”页下载" % human_size(size)
        tmpdir = tempfile.mkdtemp(prefix="adbhost_")
        local = os.path.join(tmpdir, "f.bin")
        try:
            rc, out = self.pull(remote, local, timeout=180)
            if rc != 0 or not os.path.exists(local):
                return False, b"", out.strip() or "读取失败"
            with open(local, "rb") as f:
                return True, f.read(), out.strip()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def tcp_ping(self, host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


# ===========================================================================
# 后台任务辅助
# ===========================================================================




_RE_LS = re.compile(
    r"^([dlcbps-][rwxstST-]{9})[\s\+\.@]*\s+\d+\s+(\S+)\s+(\S+)\s+(\d+(?:,\s*\d+)?)\s+"
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?(?:\s+[+-]\d{4})?"
    r"|\w{3}\s+\d{1,2}\s+\d{2}:\d{2}|\w{3}\s+\d{1,2}\s+\d{4})\s+(.*)$"
)


def parse_remote_listing(listing: str):
    """解析 `ls -la` 输出 -> [(name, is_dir, size, perms(如 644), user, group)]"""
    out = []
    for line in listing.splitlines():
        s = line.rstrip()
        if not s.strip() or s.startswith("total"):
            continue
        m = _RE_LS.match(s.strip())
        if not m:
            # 宽松回退：形如 `ls -l` 的 9 段格式
            parts = s.split()
            if len(parts) >= 9 and re.match(r"^[dlcbps-][rwxstST-]{9}", parts[0]):
                name = " ".join(parts[8:])
                out.append((name, parts[0].startswith("d"), _to_int(parts[4]),
                            _perm_str(parts[0]), parts[2], parts[3]))
            continue
        perm, user, group, size, _date, name = m.groups()
        # 符号链接: name -> target，只保留 name
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        out.append((name, perm.startswith("d"), _to_int(size), _perm_str(perm), user, group))
    return out


def _to_int(s):
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None




# ---------------------------------------------------------------------------
# 文件列表解析（设备侧 `ls -la` 输出）
# ---------------------------------------------------------------------------


def parse_remote_listing(listing: str):
    """解析 `ls -la` 输出 -> [(name, is_dir, size, perms, user, group)]"""
    out = []
    for line in listing.splitlines():
        s = line.rstrip()
        if not s.strip() or s.startswith("total"):
            continue
        m = _RE_LS.match(s.strip())
        if not m:
            parts = s.split()
            if len(parts) >= 9 and re.match(r"^[dlcbps-][rwxstST-]{9}", parts[0]):
                name = " ".join(parts[8:])
                out.append((name, parts[0].startswith("d"), _to_int(parts[4]),
                            _perm_str(parts[0]), parts[2], parts[3]))
            continue
        perm, user, group, size, _date, name = m.groups()
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        out.append((name, perm.startswith("d"), _to_int(size), _perm_str(perm), user, group))
    return out


def _to_int(s):
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _perm_str(perm: str) -> str:
    """drwxr-xr-x -> d755"""
    bits = perm[1:]
    out = ""
    for i in range(0, 9, 3):
        v = (4 if bits[i] in "rs" else 0) + (2 if bits[i + 1] in "ws" else 0) + (1 if bits[i + 2] in "xst" else 0)
        out += str(v)
    return ("d" if perm.startswith("d") else "-") + out

# -*- coding: utf-8 -*-
"""终端会话后端：持久 adb shell + pyte 屏幕缓冲（无 GUI 依赖）。

设计要点
--------
* 后台线程常驻一个 `adb shell` 进程，构成**持久交互会话**：
  `cd`、环境变量、`export` 都跨命令保留，提示符持续显示。
* 设备输出是原始 ANSI 字节流，用 `pyte` 解析成字符网格，
  上层界面（Qt / Tk / 终端）只需按行 + 样式段渲染即可，
  这样 vim / nano / top 等全屏程序才能正确显示。
* 窗口尺寸变化时把新的 rows/cols 告诉设备（`stty rows R cols C`），
  否则设备上 `stty size` 会返回 `0 0`，vim 排版会错乱。

没有 pyte 时自动降级为“纯文本模式”。
"""

import atexit
import os
import queue
import re
import select
import subprocess
import threading
import time
# 无 GUI 依赖

# 程序退出时兜底清理所有还活着的会话，避免留下孤儿 `adb shell` 进程
_LIVE_SESSIONS = set()
_LIVE_LOCK = threading.Lock()


def _register(sess):
    with _LIVE_LOCK:
        _LIVE_SESSIONS.add(sess)


def _unregister(sess):
    with _LIVE_LOCK:
        _LIVE_SESSIONS.discard(sess)


def _cleanup_all():
    with _LIVE_LOCK:
        items = list(_LIVE_SESSIONS)
    for s in items:
        try:
            s.stop()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_cleanup_all)

try:
    import pyte
    HAS_PYTE = True
except Exception:  # noqa: BLE001
    pyte = None
    HAS_PYTE = False

IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# 16 色 ANSI -> 十六进制（偏 MobaXterm / Solarized-dark 观感）
ANSI_COLORS = {
    "black": "#1c1c1c", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510",
    "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5",
    "brightblack": "#666666", "brightred": "#f14c4c", "brightgreen": "#23d18b",
    "brightbrown": "#f5f543", "brightblue": "#3b8eea", "brightmagenta": "#d670d6",
    "brightcyan": "#29b8db", "brightwhite": "#ffffff",
}
DEFAULT_FG = "#e6e6e6"
DEFAULT_BG = "#1e1e1e"
HISTORY_LINES = 5000


def _make_screen(pyte_mod, cols, rows):
    """创建带滚动历史的屏幕；旧 pyte 自动退回普通 Screen。"""
    history_cls = getattr(pyte_mod, "HistoryScreen", None)
    if history_cls is not None:
        return history_cls(cols, rows, history=HISTORY_LINES,
                           ratio=1.0 / max(1, rows))
    return pyte_mod.Screen(cols, rows)


def _make_stream(pyte_mod, screen):
    """兼容不同版本的 pyte：

    * 新版本（>=0.8）：``pyte.ByteStream(screen)``，构造时即绑定屏幕；
    * 旧版本（如 Ubuntu 20.04 的 0.4.8）：``ByteStream(encodings)`` + ``attach(screen)``。
    """
    try:
        return pyte_mod.ByteStream(screen)
    except TypeError:
        pass
    stream = pyte_mod.ByteStream([("utf-8", "replace")])
    if hasattr(stream, "attach"):
        stream.attach(screen)
    return stream


def _set_history_ratio(screen, rows):
    history = getattr(screen, "history", None)
    if history is not None and hasattr(history, "_replace"):
        screen.history = history._replace(ratio=1.0 / max(1, rows))


def _history_state(screen):
    """返回 (最旧位置, 最新位置, 当前位置)，单位为行。"""
    history = getattr(screen, "history", None)
    if history is None:
        return 0, 0, 0
    top = len(history.top)
    bottom = len(history.bottom)
    return 0, top + bottom, top


def _scroll_history_to(screen, value):
    """把 HistoryScreen 滚到指定行位置并返回实际位置。"""
    low, high, current = _history_state(screen)
    target = max(low, min(high, int(value)))
    while current > target:
        before = current
        screen.prev_page()
        current = _history_state(screen)[2]
        if current == before:
            break
    while current < target:
        before = current
        screen.next_page()
        current = _history_state(screen)[2]
        if current == before:
            break
    return current


# 尺寸同步命令的回显：不同设备可能带也可能不带 2>/dev/null，都要能清掉
_SILENT_CMD = re.compile(r"stty rows \d+ cols \d+\s*(?:2>\s*/dev/null)?")


def _hidden_resize_payload(command):
    """执行 stty 后擦掉命令回显，让 shell 提示符留在原行。"""
    return "\x15%s; printf '\\033[1A\\033[2K\\r'\r" % command

# 粗略判断东亚宽字符（占两列）
_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)


def _char_width(ch: str) -> int:
    o = ord(ch)
    for lo, hi in _WIDE_RANGES:
        if lo <= o <= hi:
            return 2
    return 1



class TerminalSession:
    """常驻的 `adb shell` 会话 + pyte 屏幕缓冲。"""

    def __init__(self, adb_exe, serial, rows=30, cols=100, log=None):
        self.adb_exe = adb_exe
        self.serial = serial
        self.rows = rows
        self.cols = cols
        self.proc = None
        self.alive = False
        self._log = log or (lambda s: None)
        self.out_q = queue.Queue()      # 原始字节块
        self.exited = threading.Event()
        self._last_resize = None
        self._resize_job = None

        if HAS_PYTE:
            self.screen = _make_screen(pyte, cols, rows)
            self.stream = _make_stream(pyte, self.screen)
        else:
            self.screen = None
            self.stream = None
            self.plain = bytearray()    # 降级模式下的原始输出

    # ---- 生命周期 ------------------------------------------------------
    def start(self):
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        kw = {}
        if IS_WINDOWS:
            kw["creationflags"] = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [self.adb_exe, "-s", self.serial, "shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env=env, **kw
        )
        self.alive = True
        _register(self)
        threading.Thread(target=self._reader, daemon=True).start()
        # 设备上 stty size 默认是 0 0，先同步一次窗口尺寸
        self.resize(self.rows, self.cols, initial=True)
        return True

    def _reader(self):
        fd = self.proc.stdout.fileno() if self.proc.stdout else None
        while self.alive:
            try:
                if fd is not None and hasattr(select, "select"):
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if not r:
                        continue
                chunk = os.read(fd, 8192)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self._feed(chunk)
            self.out_q.put(chunk)
        self.alive = False
        self.exited.set()

    def _feed(self, chunk):
        if self.stream is not None:
            try:
                self.stream.feed(chunk)
            except Exception:  # noqa: BLE001
                pass
        else:
            self.plain += chunk
            if len(self.plain) > 400000:
                del self.plain[:200000]

    def stop(self):
        """结束会话并确保进程真的消失（避免留下孤儿 adb shell 进程）。

        注意顺序：**先终止进程，再关闭管道**。如果先 close(stdin)，在 adb 已经
        不再读取该管道时可能阻塞在 C 层，导致界面卡死。
        """
        self.alive = False
        p = self.proc
        if p is None:
            _unregister(self)
            self.exited.set()
            return
        # 1) 直接终止进程（adb shell 对 SIGTERM 响应良好）
        if p.poll() is None:
            try:
                if IS_WINDOWS:
                    subprocess.call(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
                else:
                    import signal
                    try:
                        os.kill(p.pid, signal.SIGTERM)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        try:
            p.wait(timeout=2)
        except Exception:  # noqa: BLE001
            # 2) 还在就 SIGKILL
            try:
                if IS_WINDOWS:
                    subprocess.call(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
                else:
                    import signal
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        os.kill(p.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                p.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        # 3) 进程已死，此时关闭管道不会阻塞
        for f in (p.stdin, p.stdout):
            try:
                if f:
                    f.close()
            except Exception:  # noqa: BLE001
                pass
        _unregister(self)
        self.exited.set()

    # ---- 输入 ----------------------------------------------------------
    def write_bytes(self, data):
        p = self.proc
        if not p or p.poll() is not None:
            return False
        try:
            p.stdin.write(data)
            p.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def write_text(self, s):
        return self.write_bytes(s.encode("utf-8", "replace"))

    def send_line(self, cmd: str):
        """执行一条命令并回车。"""
        return self.write_text(cmd + "\n")

    def send_ctrl(self, ch: str):
        """发送 Ctrl+<ch>，例如 send_ctrl('c') -> 0x03。"""
        if not ch:
            return False
        return self.write_bytes(bytes([ord(ch.upper()) & 0x1F]))

    # ---- 窗口尺寸 ------------------------------------------------------
    def resize(self, rows, cols, initial=False):
        """告诉设备端新的窗口尺寸（rows/cols）。

        设备上 pty 默认是 `0 0`，不设置的话 vim 等全屏程序排版会错乱。
        实现方式：用 Ctrl-U 清掉正在输入的行，再发送 stty 命令并回车；
        该行的回显由终端控件在渲染时过滤掉（见 _SILENT_CMDS），用户看不到。
        """
        self.rows, self.cols = max(1, rows), max(1, cols)
        if self.screen is not None:
            try:
                self.screen.resize(self.rows, self.cols)
                _set_history_ratio(self.screen, self.rows)
            except Exception:  # noqa: BLE001
                pass
        if (rows, cols) == self._last_resize and not initial:
            return
        self._last_resize = (rows, cols)
        cmd = "stty rows %d cols %d 2>/dev/null" % (self.rows, self.cols)
        # 刚连接时提示符可能还没打印出来，直接发命令会和提示符黏在同一行。
        # 用一个短延时等 shell 就绪；resize 频繁触发时也只排一次。
        if self._resize_job is not None:
            try:
                self._resize_job.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._resize_job = threading.Timer(0.6 if initial else 0.15,
                                           self._send_resize_cmd, args=(cmd,))
        self._resize_job.daemon = True
        self._resize_job.start()

    def _send_resize_cmd(self, cmd):
        self._resize_job = None
        if self.alive:
            self.write_text(_hidden_resize_payload(cmd))

    # ---- 读取 ----------------------------------------------------------
    def snapshot_lines(self):
        """返回 [(文本, [(起列, 止列, 样式)...]), ...]，行数为屏幕高度。"""
        return _snapshot_impl(self, self.rows, self.cols)

    def history_state(self):
        return _history_state(self.screen) if self.screen is not None else (0, 0, 0)

    def scroll_history_to(self, value):
        if self.screen is None:
            return 0
        return _scroll_history_to(self.screen, value)


def _snapshot_impl(sess, rows, cols):
    """TerminalSession 与 SSHTerminalSession 共用的屏幕快照实现。"""
    if sess.screen is None:
        return None
    out = []
    for y in range(rows):
        row = sess.screen.buffer[y]
        segs = []
        cur_style = None
        cur_start = 0
        parts = []
        for x in range(cols):
            ch = row[x]
            style = (ch.fg, ch.bg, ch.bold, ch.reverse)
            parts.append(ch.data or " ")
            if style != cur_style:
                if cur_style is not None:
                    segs.append((cur_start, x, cur_style))
                cur_style = style
                cur_start = x
        if cur_style is not None:
            segs.append((cur_start, cols, cur_style))
        line = "".join(parts).rstrip()
        # 设备会回显我们注入的 `stty rows R cols C`。只把命令本身去掉，
        # **必须保留这一行**：一旦整行丢弃，后面所有行都会上移一位，
        # 而光标是按 pyte 的原始行号绘制的，就会画到文字下方一行去。
        if "stty rows" in line and "cols" in line:
            line = _SILENT_CMD.sub("", line).rstrip()
        n = len(line)
        clipped = []
        for (s0, s1, st) in segs:
            if s0 >= n:
                break
            clipped.append((s0, min(s1, n), st))
        out.append((line, clipped))
    return out


# ---------------------------------------------------------------------------
# 远程模式终端：在 Ubuntu 的 SSH 里执行 `adb shell`，得到的是**设备**的 shell
# ---------------------------------------------------------------------------

class RemoteAdbShellSession:
    """远程模式下的交互终端。

    效果等同于在 Ubuntu 终端里执行：

        user@ubuntu-host:~$ adb shell
        root@ATK-DLRK3568:/#

    实现：开一个 SSH PTY，在里面直接跑 `<远端adb> -s <serial> shell`。
    这条 adb shell 跑在 Ubuntu 上，所以它连的是插在 Ubuntu 上的开发板，
    但终端内容与本地模式完全一致（设备 bash、提示符、vim、python3 都可用）。
    """

    def __init__(self, remote, serial, rows=30, cols=100):
        self.remote = remote
        self.serial = serial
        self.rows = rows
        self.cols = cols
        self.out_q = queue.Queue()
        self.exited = threading.Event()
        self.alive = False
        self._write = None
        self._read = None
        self._close = None
        self._last_resize = None
        self._resize_job = None
        if HAS_PYTE:
            self.screen = _make_screen(pyte, cols, rows)
            self.stream = _make_stream(pyte, self.screen)
        else:
            self.screen = None
            self.stream = None
            self.plain = bytearray()

    def _cmdline(self):
        """构造在 Ubuntu 的 PTY 里执行的命令：`<adb> -s <serial> shell`。"""
        adb = self.remote.adb_remote or "adb"
        # 路径本身用双引号包住即可（adb 路径一般不含 $ ` \ 等字符）
        return '"%s" -s "%s" shell' % (adb.replace('"', '\\"'),
                                       str(self.serial).replace('"', '\\"'))

    def start(self):
        self._write, self._read, self._close = self.remote.open_shell(
            term="xterm-256color", width=self.cols, height=self.rows,
            command=self._cmdline())
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        # 设备端 pty 默认 0x0，需要显式设置，否则 vim 排版错乱
        self.resize(self.rows, self.cols, initial=True)
        return True

    def _reader(self):
        while self.alive:
            try:
                chunk = self._read()
            except Exception:  # noqa: BLE001
                break
            if chunk:
                self._feed(chunk)
                self.out_q.put(chunk)
            else:
                time.sleep(0.03)
        self.alive = False
        self.exited.set()

    def _feed(self, chunk):
        if self.stream is not None:
            try:
                self.stream.feed(chunk)
            except Exception:  # noqa: BLE001
                pass
        else:
            self.plain += chunk
            if len(self.plain) > 400000:
                del self.plain[:200000]

    def stop(self):
        self.alive = False
        job = self._resize_job
        self._resize_job = None
        if job is not None:
            try:
                job.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._close:
                self._close()
        except Exception:  # noqa: BLE001
            pass
        self.exited.set()

    def write_bytes(self, data):
        if not self.alive or self._write is None:
            return False
        try:
            self._write(data)
            return True
        except Exception:  # noqa: BLE001
            return False

    def write_text(self, s):
        return self.write_bytes(s.encode("utf-8", "replace"))

    def send_line(self, cmd):
        return self.write_text(cmd + "\n")

    def send_ctrl(self, ch):
        if not ch:
            return False
        return self.write_bytes(bytes([ord(ch.upper()) & 0x1F]))

    def resize(self, rows, cols, initial=False):
        rows, cols = max(1, rows), max(1, cols)
        unchanged = (rows, cols) == self._last_resize
        self.rows, self.cols = rows, cols
        if self.screen is not None:
            try:
                self.screen.resize(self.rows, self.cols)
                _set_history_ratio(self.screen, self.rows)
            except Exception:  # noqa: BLE001
                pass
        # 外层 SSH PTY 也跟着改，否则输出可能被硬换行。每个终端只调整
        # open_shell() 为它创建的独立 channel，不能遍历整个 transport。
        try:
            resize_pty = getattr(self._write, "resize_pty", None)
            if resize_pty is not None:
                resize_pty(self.cols, self.rows)
        except Exception:  # noqa: BLE001
            pass
        if unchanged:
            return
        self._last_resize = (rows, cols)
        # 再把尺寸告诉设备端的 pty
        cmd = "stty rows %d cols %d 2>/dev/null" % (self.rows, self.cols)
        delay = 0.6 if initial else 0.15

        old_job = self._resize_job
        if old_job is not None:
            try:
                old_job.cancel()
            except Exception:  # noqa: BLE001
                pass

        def send():
            self._resize_job = None
            if self.alive:
                self.write_text(_hidden_resize_payload(cmd))
        t = threading.Timer(delay, send)
        t.daemon = True
        self._resize_job = t
        t.start()

    def snapshot_lines(self):
        return _snapshot_impl(self, self.rows, self.cols)

    def history_state(self):
        return _history_state(self.screen) if self.screen is not None else (0, 0, 0)

    def scroll_history_to(self, value):
        if self.screen is None:
            return 0
        return _scroll_history_to(self.screen, value)


# ---------------------------------------------------------------------------

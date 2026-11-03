# -*- coding: utf-8 -*-
"""核心业务层测试（不启动界面，可无显示器环境运行）。

运行: python tests/test_core.py
"""

from __future__ import annotations

import os
import base64
import sys
import tempfile

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from core import adb_backend as ab
from core import ssh_backend
from core import terminal_backend as tb

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  -- %s" % (name, detail))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s  -- %s" % (name, why))


def main():
    print("=" * 62)
    print(" ADB 上位机 v2 核心测试")
    print("=" * 62)

    # ---- 1. adb 定位 ----
    print("\n== 1. adb 定位 ==")
    exe = ab.find_adb()
    check("找到 adb 可执行文件", bool(exe), str(exe))
    print("     路径: %s" % exe)
    if exe:
        ver = ab.adb_version_of(exe)
        check("能读取 adb 版本", bool(ver), ver)
        print("     版本: %s" % (ver or "").strip()[:80])

    # ---- 2. 进度解析 ----
    print("\n== 2. 进度解析 ==")
    cases = [
        ("data/local/tmp/a.bin: 45%", 0.45),
        ("[ 87%] /data/local/tmp/b.bin", 0.87),
        ("1 file pushed. 0 files skipped.", None),
    ]
    for text, expect in cases:
        r = ab.parse_progress(text)
        got = r[0] if r else None
        check("解析 %r" % text[:34], got == expect, "got=%s expect=%s" % (got, expect))

    # ---- 3. 大小格式化 ----
    print("\n== 3. 大小格式化 ==")
    check("human_size(0)", ab.human_size(0) in ("0 B", "0.0 B"), ab.human_size(0))
    check("human_size(1023)", "B" in ab.human_size(1023), ab.human_size(1023))
    check("human_size(1536)", "KB" in ab.human_size(1536), ab.human_size(1536))
    check("human_size(5MB)", "MB" in ab.human_size(5 * 1024 * 1024), ab.human_size(5 * 1024 * 1024))

    # ---- 4. 引号转义 ----
    print("\n== 4. 路径引号 ==")
    q = ab.quote("/data/local/tmp/a b.txt")
    check("含空格路径被引号包裹", q.startswith("'") and q.endswith("'"), q)

    # ---- 5. ls 解析 ----
    print("\n== 5. ls -la 解析 ==")
    listing = (
        "total 12\n"
        "drwxr-xr-x 2 root root  4096 2026-10-31 20:00 .\n"
        "drwxr-xr-x 8 root root  4096 2026-10-31 19:00 ..\n"
        "-rw-r--r-- 1 root root  1024 2026-10-31 20:01 hello.txt\n"
        "drwxrwxrwx 2 root root  4096 2026-10-31 20:02 mydir\n"
        "lrwxrwxrwx 1 root root     9 2026-10-31 20:03 link -> /tmp/x\n"
    )
    rows = ab.parse_remote_listing(listing)
    names = [r[0] for r in rows]
    check("忽略 . 与 ..", "." not in names and ".." not in names, str(names))
    check("解析出 3 项", len(rows) == 3, str(len(rows)))
    check("识别普通文件与大小", any(r[0] == "hello.txt" and r[2] == 1024 for r in rows), str(rows))
    check("识别目录", any(r[0] == "mydir" and r[1] for r in rows), str(rows))
    check("符号链接取名字", any(r[0] == "link" for r in rows), str(rows))
    check("权限串 d777（drwxrwxrwx）", any(r[3] == "d777" for r in rows), str([r[3] for r in rows]))
    check("普通文件权限 -644", any(r[3] == "-644" for r in rows), str([r[3] for r in rows]))

    rows2 = ab.parse_remote_listing("drwxr-xr-x 2 root root 4096 2026-10-31 20:00 sub\n")
    check("权限串 d755（drwxr-xr-x）", bool(rows2) and rows2[0][3] == "d755", str(rows2))

    # ---- 6. 远程 adb 命令构造 / 二进制通道 ----
    print("\n== 6. 远程 adb 作用域与二进制通道 ==")
    remote = ssh_backend.RemoteSession("example.invalid")
    remote.adb_remote = "/opt/platform tools/adb"
    remote.serial = "SERIAL 1"
    commands = []

    def fake_exec(command, timeout=60):
        commands.append(command)
        return 0, "OK"

    remote.exec = fake_exec
    remote.adb(["shell", "echo OK"])
    check("设备级远程 adb 自动绑定 -s 序列号",
          "'-s' 'SERIAL 1' 'shell'" in commands[-1], commands[-1])
    remote.adb(["devices", "-l"])
    check("adb devices 等主机级命令不附加 -s",
          "'SERIAL 1'" not in commands[-1], commands[-1])

    binary_payload = bytes(range(256)) + b"\x00\xffADBHOST"
    commands.clear()
    def fake_binary_exec(command, timeout=60):
        commands.append(command)
        if "wc -c" in command:
            return 0, str(len(binary_payload)) + "\n"
        return 0, base64.b64encode(binary_payload).decode("ascii")

    remote.exec = fake_binary_exec
    ok_binary, binary_back, binary_msg = remote.adb_read_bytes("/data/a b.bin")
    check("远程设备文件读取保持任意二进制字节",
          ok_binary and binary_back == binary_payload, binary_msg)
    check("远程二进制读取同样绑定目标设备",
          all("'-s' 'SERIAL 1' 'shell'" in command for command in commands),
          str(commands))

    remote.exec = lambda _command, timeout=60: (0, "40000000\n")
    too_ok, _too_data, too_msg = remote.adb_read_bytes("/data/large.bin")
    check("远程二进制读取拒绝超大文件",
          not too_ok and "文件过大" in too_msg, too_msg)

    uploaded = []
    commands.clear()
    remote.upload_bytes = lambda data, path: (uploaded.append((data, path)) or (True, "OK"))
    remote.exec = fake_exec
    stdin_ok, _stdin_msg = remote.adb_with_stdin(b"A\x00B", "cat > /data/stdin.bin")
    check("远程 stdin 数据经 adb shell 送往设备而非 Ubuntu",
          stdin_ok and uploaded and any("'shell' 'cat > /data/stdin.bin <" in c
                                       for c in commands), str(commands))

    # ---- 7. 终端滚动历史 ----
    print("\n== 7. 终端滚动历史 ==")
    if not tb.HAS_PYTE or not hasattr(tb.pyte, "HistoryScreen"):
        skip("终端滚动历史", "当前 pyte 不支持 HistoryScreen")
    else:
        screen = tb._make_screen(tb.pyte, 20, 4)
        stream = tb._make_stream(tb.pyte, screen)
        stream.feed(b"".join(("LINE_%02d\r\n" % i).encode() for i in range(12)))

        class HistoryFixture:
            pass

        fixture = HistoryFixture()
        fixture.screen = screen
        low, high, current = tb._history_state(screen)
        check("终端保留超出当前屏幕的历史行",
              (low, high, current) == (0, 9, 9), str((low, high, current)))
        actual = tb._scroll_history_to(screen, 2)
        old_lines = [line for line, _segs in tb._snapshot_impl(fixture, 4, 20)]
        check("终端历史可滚轮式逐行回看",
              actual == 2 and old_lines[0] == "LINE_02", repr(old_lines))
        actual = tb._scroll_history_to(screen, high)
        live_lines = [line for line, _segs in tb._snapshot_impl(fixture, 4, 20)]
        check("终端历史可回到最新输出",
              actual == high and live_lines[0] == "LINE_09", repr(live_lines))

        class WriteProbe:
            def __init__(self):
                self.payloads = []
                self.sizes = []

            def __call__(self, payload):
                self.payloads.append(payload)

            def resize_pty(self, width, height):
                self.sizes.append((width, height))

        class RemoteProbe:
            adb_remote = "/opt/adb"

            def __init__(self):
                self.writer = WriteProbe()

            def open_shell(self, **_kwargs):
                return self.writer, (lambda: b""), (lambda: None)

        remote_probe = RemoteProbe()
        remote_terminal = tb.RemoteAdbShellSession(
            remote_probe, "SERIAL", rows=4, cols=20)
        remote_terminal.start()
        remote_terminal.resize(4, 20)
        remote_terminal.resize(4, 20)
        import time
        time.sleep(0.75)
        check("远程终端相同尺寸只注入一次 stty",
              len(remote_probe.writer.payloads) == 1,
              repr(remote_probe.writer.payloads))
        payload = remote_probe.writer.payloads[0] if remote_probe.writer.payloads else b""
        check("终端尺寸命令会擦除自身回显而不新增提示符行",
              payload.startswith(b"\x15stty rows 4 cols 20") and
              b"printf '\\033[1A\\033[2K\\r'" in payload,
              repr(payload))
        remote_terminal.stop()

    # ---- 8. 真实设备（可选） ----
    print("\n== 8. 真实设备 ==")
    if not exe:
        skip("设备相关测试", "没有 adb")
    else:
        a = ab.Adb(exe)
        ok, _v = a.version()
        devs = a.devices() if ok else []
        usable = [d for d in devs if d.usable]
        if not usable:
            skip("设备 shell / push / pull", "当前没有在线设备")
        else:
            serial = usable[0].serial
            a.serial = serial
            print("     使用设备: %s" % serial)

            rc, out = a.shell("echo CORE_TEST_$((6*7))")
            check("设备 shell 可执行", "CORE_TEST_42" in out, out[:120])

            rc, out = a.shell("uname -s")
            check("设备 shell 返回系统信息", bool(out.strip()), out[:80])

            tmpdir = tempfile.mkdtemp(prefix="adbtest_")
            local = os.path.join(tmpdir, "core_push.bin")
            payload = os.urandom(4096)
            with open(local, "wb") as f:
                f.write(payload)
            rc, out = a.push(local, "/data/local/tmp/adbtool_core_push.bin")
            check("push 成功", rc == 0, out[:200])

            rc, out = a.shell("md5sum /data/local/tmp/adbtool_core_push.bin")
            import hashlib
            check("设备端 md5 一致", hashlib.md5(payload).hexdigest() in out, out[:200])

            back = os.path.join(tmpdir, "core_pull.bin")
            rc, out = a.pull("/data/local/tmp/adbtool_core_push.bin", back)
            ok2 = rc == 0 and os.path.isfile(back) and open(back, "rb").read() == payload
            check("pull 内容一致", ok2, out[:200])

            a.shell("rm -f /data/local/tmp/adbtool_core_push.bin")
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 汇总 ----
    print("\n" + "=" * 62)
    print(" 通过 %d 项, 失败 %d 项, 跳过 %d 项" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print(" 失败: " + ", ".join(FAIL))
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""终端光标位置测试：验证 pyte 光标坐标与渲染出来的文字严格对齐。

背景：设备会回显我们注入的 `stty rows R cols C`。若快照把该行整行丢弃，
后续行会整体上移，而光标仍按 pyte 原始行号绘制 —— 就会"光标和文字错位"。

判定方法（不依赖像素，直接比对数据）：
  1. 快照行数必须等于屏幕行数（不能少行）
  2. 光标所在行必须就是最后一行有内容的行
  3. 光标列必须正好落在该行文字末尾（shell 提示符后）
"""

from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from core import terminal_backend as tb

HOST = os.environ.get("ADBTOOL_SSH_HOST", "").strip()
PORT = int(os.environ.get("ADBTOOL_SSH_PORT", "22"))
USER = os.environ.get("ADBTOOL_SSH_USER", "").strip()
PASS = os.environ.get("ADBTOOL_SSH_PASS", "")

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILS.append(name)
        print("  [FAIL] %s  -- %s" % (name, detail))


def local_session_tests():
    """不依赖设备：直接用 pyte 造一个屏幕，验证快照行数与光标一致。"""
    print("\n== 1. 快照行数 / 光标对齐（构造用例）==")

    class Fake:
        pass

    import pyte
    rows, cols = 10, 60
    f = Fake()
    f.screen = pyte.Screen(cols, rows)
    # 这里直接吃 str，用基础 Stream（ByteStream 需要 bytes）
    f.stream = pyte.Stream(f.screen)
    f.rows, f.cols = rows, cols

    # 模拟：两次尺寸同步产生的 stty 回显 + 提示符
    f.stream.feed("root@board:/# stty rows 10 cols 60\r\n")
    f.stream.feed("root@board:/# ")
    lines = tb._snapshot_impl(f, rows, cols)
    check("快照行数与屏幕一致", len(lines) == rows, "%d vs %d" % (len(lines), rows))

    # 找出最后一行有内容的行
    last = -1
    for i, (t, _s) in enumerate(lines):
        if t.strip():
            last = i
    cy = f.screen.cursor.y
    check("光标行 = 最后一行有内容的行", cy == last, "cursor.y=%d, last=%d" % (cy, last))

    text = lines[cy][0]
    check("光标行内容为提示符", text.strip().startswith("root@board"), repr(text))
    cx = f.screen.cursor.x
    check("光标列落在提示符末尾", abs(cx - len(text)) <= 1,
          "cursor.x=%d, len(line)=%d, line=%r" % (cx, len(text), text))
    check("stty 回显已被清除", "stty rows" not in "".join(t for t, _ in lines),
          repr("".join(t for t, _ in lines)))

    # 再喂一条命令，确认连续输出后仍对齐
    f.stream.feed("ls\r\nfile1  file2\r\nroot@board:/# ")
    lines2 = tb._snapshot_impl(f, rows, cols)
    last2 = max(i for i, (t, _s) in enumerate(lines2) if t.strip())
    check("多条命令后光标仍对齐（行）", f.screen.cursor.y == last2,
          "cursor.y=%d, last=%d" % (f.screen.cursor.y, last2))
    t2 = lines2[f.screen.cursor.y][0]
    check("多条命令后光标仍对齐（列）", abs(f.screen.cursor.x - len(t2)) <= 1,
          "cursor.x=%d, len=%d, %r" % (f.screen.cursor.x, len(t2), t2))


def device_tests():
    """真实设备终端的光标对齐验证（走 SSH，与界面同一条代码路径）。"""
    print("\n== 2. 真实设备终端 ==")
    from core import ssh_backend
    sess = ssh_backend.RemoteSession(HOST, PORT, USER, PASS)
    sess.connect()
    devs = [d for d in sess.adb_devices() if d[1] in ("device",)]
    if not devs:
        print("  [SKIP] Ubuntu 上没有在线设备")
        sess.close()
        return
    serial = devs[0][0]
    print("     设备: %s" % serial)

    t = tb.RemoteAdbShellSession(sess, serial, rows=24, cols=80)
    t.start()
    time.sleep(2.0)

    t.send_line("echo CURSOR_CASE_OK")
    time.sleep(1.6)

    rows, cols = t.rows, t.cols
    lines = t.snapshot_lines()
    check("设备终端快照行数 = 屏幕行数", len(lines) == rows, "%d vs %d" % (len(lines), rows))

    last = max(i for i, (x, _s) in enumerate(lines) if x.strip())
    cy = t.screen.cursor.y
    check("设备终端光标行 = 最后有内容的行", cy == last,
          "cursor.y=%d last=%d\n%s" % (cy, last,
                                        "\n".join("%2d|%s" % (i, x) for i, (x, _s) in
                                                  enumerate(lines[-8:], start=rows - 8))))
    text = lines[cy][0] if 0 <= cy < len(lines) else ""
    check("光标行是提示符（等待输入）",
          text.rstrip().endswith("#") or text.rstrip().endswith("$") or not text,
          repr(text))
    cx = t.screen.cursor.x
    check("光标列落在提示符末尾", abs(cx - len(text)) <= 1,
          "cursor.x=%d len=%d %r" % (cx, len(text), text))

    # 换行/尺寸变化后再验证一次（这是最容易错位的场景）
    t.resize(30, 100)
    time.sleep(1.2)
    t.send_line("pwd")
    time.sleep(1.2)
    lines3 = t.snapshot_lines()
    check("resize 后快照行数 = 新屏幕行数", len(lines3) == t.rows,
          "%d vs %d" % (len(lines3), t.rows))
    last3 = max(i for i, (x, _s) in enumerate(lines3) if x.strip())
    check("resize 后光标仍对齐（行）", t.screen.cursor.y == last3,
          "cursor.y=%d last=%d" % (t.screen.cursor.y, last3))
    t3 = lines3[t.screen.cursor.y][0] if t.screen.cursor.y < len(lines3) else ""
    check("resize 后光标仍对齐（列）",
          abs(t.screen.cursor.x - len(t3)) <= 1,
          "cursor.x=%d len=%d %r" % (t.screen.cursor.x, len(t3), t3))

    t.stop()
    sess.close()


def main():
    print("=" * 62)
    print(" 终端光标对齐测试")
    print("=" * 62)
    local_session_tests()
    if not HOST or not USER:
        print("  [SKIP] 真实设备测试：请先设置 ADBTOOL_SSH_HOST 和 ADBTOOL_SSH_USER")
    else:
        try:
            device_tests()
        except Exception as e:  # noqa: BLE001
            print("  [SKIP] 设备测试不可用: %s" % e)

    print("\n" + "=" * 62)
    print(" 通过 %d 项, 失败 %d 项" % (len(PASSES), len(FAILS)))
    if FAILS:
        print(" 失败: " + ", ".join(FAILS))
    print("=" * 62)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

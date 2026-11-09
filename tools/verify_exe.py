# -*- coding: utf-8 -*-
"""验证打包后的 EXE 真能启动并正常工作。

用法: python tools/verify_exe.py <exe路径>

检查项：体积、能否启动、是否出现窗口、稳定运行、干净退出、无残留进程。
窗口检测用 Win32 EnumWindows，不依赖外部命令，避免管道/编码问题。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

APP_TITLE_PREFIXES = ("MUXO+  v", "ADB 上位机  v")
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_windows():
    """枚举所有顶层窗口 -> [(hwnd, pid, title)]"""
    out = []

    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        out.append((hwnd, pid.value, title))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return out


def app_pids(name: str = "ADB_Host_Tool") -> set:
    """取名字匹配的进程 PID 集合（onefile 会有父+子两个进程）。"""
    pids = set()
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % name, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower().startswith(name.lower()):
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return pids


def find_app_window(name: str = "ADB_Host_Tool"):
    """返回属于该程序、且**可见**的窗口标题；找不到返回空串。"""
    pids = app_pids(name)
    if not pids:
        return ""
    candidates = []
    for hwnd, pid, title in list_windows():
        if pid not in pids:
            continue
        if not title.strip():
            continue
        if not user32.IsWindowVisible(hwnd):
            continue
        candidates.append(title)
    # 优先返回真正的界面窗口
    for t in candidates:
        if t.startswith(APP_TITLE_PREFIXES):
            return t
    return candidates[0] if candidates else ""


def request_app_close(name: str = "ADB_Host_Tool") -> int:
    """向该程序的可见顶层窗口发送 WM_CLOSE，返回发送数。"""
    pids = app_pids(name)
    sent = 0
    for hwnd, pid, _title in list_windows():
        if pid in pids and user32.IsWindowVisible(hwnd):
            if user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                sent += 1
    return sent


def process_count(name: str = "ADB_Host_Tool") -> int:
    """用 tasklist 统计进程数（EXE 名去掉 .exe）。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % name],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or "").lower()
        return out.count(name.lower() + ".exe")
    except Exception:  # noqa: BLE001
        return -1


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/verify_exe.py <exe路径>")
        return 2
    exe = os.path.abspath(sys.argv[1])
    name = os.path.splitext(os.path.basename(exe))[0]
    if not os.path.isfile(exe):
        print("[FAIL] 文件不存在: %s" % exe)
        return 1

    size_mb = os.path.getsize(exe) / 1048576.0
    print("=" * 62)
    print(" 验证 EXE")
    print(" 路径: %s" % exe)
    print(" 大小: %.1f MB" % size_mb)
    print("=" * 62)

    fails = []
    if size_mb < 15:
        fails.append("体积过小（%.1f MB），可能缺少 Qt 运行时" % size_mb)
        print("  [FAIL] 体积检查")
    else:
        print("  [PASS] 体积正常（含 Qt 运行时）")

    print("\n启动中（onefile 首次解包较慢，最多等 60 秒）...")
    t0 = time.time()
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    title = ""
    for i in range(60):
        time.sleep(1.0)
        if proc.poll() is not None:
            print("  [FAIL] 进程启动后立即退出，返回码 %s" % proc.returncode)
            fails.append("启动即退出")
            break
        titles = [t for t in [find_app_window(name)] if t]
        if titles:
            title = titles[0]
            if not title.startswith(APP_TITLE_PREFIXES):
                print("  [FAIL] 出现异常窗口: %s  (%.1fs)" % (title, time.time() - t0))
                fails.append("未出现应用主窗口")
            else:
                print("  [PASS] 窗口已出现: %s  (%.1fs)" % (title, time.time() - t0))
            break
    else:
        pass

    if proc.poll() is None and not title:
        print("  [FAIL] 60 秒内未检测到应用主窗口（进程仍在运行）")
        fails.append("未检测到应用主窗口")

    if proc.poll() is not None and fails:
        print("\n" + "=" * 62)
        print(" 验证失败: %s" % "; ".join(fails))
        print("=" * 62)
        return 1

    print("\n保持运行 8 秒，确认稳定...")
    crashed = False
    for _ in range(8):
        time.sleep(1.0)
        if proc.poll() is not None:
            print("  [FAIL] 运行中崩溃，返回码 %s" % proc.returncode)
            fails.append("运行中崩溃")
            crashed = True
            break
    if not crashed:
        print("  [PASS] 持续运行稳定")

    print("\n正常关闭程序...")
    sent = request_app_close(name)
    deadline = time.time() + 10.0
    while time.time() < deadline and process_count(name) > 0:
        time.sleep(0.25)
    graceful = sent > 0 and process_count(name) == 0
    if graceful:
        print("  [PASS] 已响应 WM_CLOSE 并自行退出")
    else:
        print("  [FAIL] 未在 10 秒内正常退出，将强制清理")
        fails.append("无法正常退出")
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass

    left = process_count(name)
    if left > 0:
        print("  [FAIL] 仍有 %d 个残留进程" % left)
        fails.append("残留进程")
        subprocess.run(["taskkill", "/IM", name + ".exe", "/F"], capture_output=True)
    else:
        print("  [PASS] 无残留进程")

    print("\n" + "=" * 62)
    if fails:
        print(" 验证失败: %s" % "; ".join(fails))
    else:
        print(" 验证通过：EXE 可启动、出现窗口、稳定运行、干净退出")
    print("=" * 62)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

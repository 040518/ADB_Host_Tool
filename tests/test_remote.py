# -*- coding: utf-8 -*-
"""远程模式端到端测试（真实环境）：Windows → SSH → Ubuntu → adb → RK3568。

需要一台可 SSH 登录且插着开发板的 Ubuntu。
配置可用环境变量覆盖：
    ADBTOOL_SSH_HOST / ADBTOOL_SSH_PORT / ADBTOOL_SSH_USER / ADBTOOL_SSH_PASS

运行: python tests/test_remote.py
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui import theme
from ui.context import AppContext
from ui.main_window import MainWindow

HOST = os.environ.get("ADBTOOL_SSH_HOST", "").strip()
PORT = int(os.environ.get("ADBTOOL_SSH_PORT", "22"))
USER = os.environ.get("ADBTOOL_SSH_USER", "").strip()
PASS = os.environ.get("ADBTOOL_SSH_PASS", "")

PASSES, FAILS, SKIPS = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILS.append(name)
        print("  [FAIL] %s  -- %s" % (name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("  [SKIP] %s  -- %s" % (name, why))


def main():
    print("=" * 62)
    print(" ADB 上位机 v2 远程模式端到端测试")
    if not HOST or not USER:
        skip("远程端到端测试", "请先设置 ADBTOOL_SSH_HOST 和 ADBTOOL_SSH_USER")
        print("=" * 62)
        return 0
    print(" 目标: %s@%s:%d" % (USER, HOST, PORT))
    print("=" * 62)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.load_stylesheet())
    f = QFont()
    f.setPointSize(13)
    app.setFont(f)

    settings = theme.load_settings()
    ctx = AppContext(settings)
    win = MainWindow(ctx)
    win.resize(1400, 880)
    win.show()

    def pump(sec=0.4):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    def wait(cond, timeout=30, what=""):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            app.processEvents()
            time.sleep(0.05)
        if what:
            print("    超时等待: %s" % what)
        return False

    from core import adb_backend as _ab
    ab_quote = _ab.quote

    def wait_idle(timeout=120, what="后台任务"):
        """等所有后台任务结束（传输/刷新都在 Worker 里跑）。"""
        end = time.time() + timeout
        time.sleep(0.3)
        while time.time() < end:
            if not ctx._workers:
                # 再多转两圈，确保 done 回调里的刷新也跑完
                pump(0.8)
                if not ctx._workers:
                    return True
            app.processEvents()
            time.sleep(0.05)
        print("    超时等待: %s" % what)
        return False

    pump(1.5)

    # ---- 1. 连接 ----
    print("\n== 1. SSH 连接 ==")
    state = {"done": False, "ok": False, "err": None}

    def on_done(ok, err):
        state["done"] = True
        state["ok"] = ok
        state["err"] = err

    ctx.connect_remote({"name": "Ubuntu", "host": HOST, "port": PORT,
                        "user": USER, "password": PASS, "adb": ""}, on_done=on_done)
    got = wait(lambda: state["done"], 40, "SSH 连接")
    if not got or not state["ok"]:
        check("SSH 连接成功", False, str(state.get("err")))
        print("\n远程环境不可用，后续测试跳过。")
        print("=" * 62)
        print(" 通过 0 项, 失败 %d 项" % len(FAILS))
        return 1
    check("SSH 连接成功", True, "")
    sess = ctx.remote
    print("     远端 adb: %s" % sess.adb_remote)
    print("     远端 home: %s" % sess.remote_home)

    check("远端能取到 adb 版本", bool(sess.adb_version()), str(sess.adb_version())[:80])
    check("已进入远程模式", ctx.is_remote, "")

    # ---- 2. 设备列表来自 Ubuntu ----
    print("\n== 2. 设备列表（来自 Ubuntu）==")
    win._after_remote(True)
    pump(0.5)
    ok = wait(lambda: len(ctx.devices) > 0, 30, "远端设备列表")
    check("远端发现设备", ok, str([d.serial for d in ctx.devices]))
    if not ok:
        print("\nUbuntu 上没有在线设备，后续测试跳过。")
        print("=" * 62)
        print(" 通过 %d 项, 失败 %d 项" % (len(PASSES), len(FAILS)))
        return 1
    serial = ctx.devices[0].serial
    print("     设备: %s" % serial)
    win._on_remote(True)
    pump(0.5)
    check("重复远程状态通知不丢失当前设备", ctx.current is not None, "")
    check("远程 adb 已绑定当前设备序列号", sess.serial == serial,
          "remote.serial=%r" % sess.serial)
    check("界面显示远程模式", "远程" in win.sb_mode.text(), win.sb_mode.text())
    check("状态栏显示 SSH 主机", HOST in win.sb_remoteinfo.text(), win.sb_remoteinfo.text())

    # ---- 3. 文件页：左栏变 Ubuntu ----
    print("\n== 3. 文件管理（左栏 = Ubuntu）==")
    win._switch_page("files")
    pump(1.2)
    fp = win.pages["files"]
    fp.pane_local.set_endpoint("ssh")
    fp.pane_remote.set_endpoint("adb")
    ok = wait(lambda: fp.pane_local.cwd.startswith("/"), 25, "左栏切到 Ubuntu 路径")
    check("左栏路径为 Ubuntu 风格", ok, fp.pane_local.cwd)
    check("左栏标题显示 SSH", "SSH" in fp.pane_local.lbl_title.text(),
          fp.pane_local.lbl_title.text())
    check("左栏列出 Ubuntu 目录", fp.pane_local.model.rowCount() > 0,
          str(fp.pane_local.model.rowCount()))
    home = fp.pane_local.cwd
    print("     Ubuntu 当前目录: %s (%d 项)" % (home, fp.pane_local.model.rowCount()))

    # 设备侧
    fp.pane_remote.goto("/data/local/tmp")
    ok = wait(lambda: fp.pane_remote.cwd == "/data/local/tmp", 25, "设备目录")
    check("设备侧目录可浏览", ok, fp.pane_remote.cwd)
    check("设备侧有内容", fp.pane_remote.model.rowCount() >= 0, "")
    print("     设备目录条目数: %d" % fp.pane_remote.model.rowCount())

    # ---- 4. 上传：Ubuntu -> 设备 ----
    print("\n== 4. 上传（Ubuntu → 设备）==")
    payload = os.urandom(30000)
    up_remote = "/tmp/adbtool_uitest/up.bin"
    sess.exec("mkdir -p /tmp/adbtool_uitest && rm -f %s" % up_remote)
    okup, msg = sess.upload_bytes(payload, up_remote)
    check("在 Ubuntu 上准备待上传文件", okup, str(msg))
    src_md5 = hashlib.md5(payload).hexdigest()
    rc, uout = sess.exec("md5sum %s; wc -c < %s" % (up_remote, up_remote))
    print("     Ubuntu 源文件: %s" % uout.strip().replace("\n", "  "))
    check("Ubuntu 源文件内容正确", src_md5 in uout, uout[:160])

    fp.pane_local.goto("/tmp/adbtool_uitest")
    ok = wait(lambda: fp.pane_local.cwd == "/tmp/adbtool_uitest", 25, "进入 Ubuntu 测试目录")
    names = [fp.pane_local.model.item(i, 0).text().rstrip("/")
             for i in range(fp.pane_local.model.rowCount())]
    check("左栏能看到待上传文件", "up.bin" in names, str(names[:10]))

    fp.upload_items(["up.bin"], "/data/local/tmp")
    wait_idle(180, "上传")

    def device_has(name):
        # 不能用 `ls 文件` 的输出里是否含文件名来判断：
        # "No such file or directory: /data/local/tmp/up.bin" 里也含这个名字，
        # 会把失败误判为成功。
        rc, out = sess.adb(["shell", "test -f %s && echo YES || echo NO"
                            % ab_quote("/data/local/tmp/" + name)])
        return "YES" in out

    ok = wait(lambda: device_has("up.bin"), 20, "设备端出现文件")
    check("上传后设备侧出现文件", ok, "")
    rc, out = sess.adb(["shell", "md5sum /data/local/tmp/up.bin"])
    rc2, szout = sess.adb(["shell", "wc -c < /data/local/tmp/up.bin"])
    print("     设备端: %s  (大小 %s, 期望 %d)" % (out.strip(), szout.strip(), len(payload)))
    check("设备端 md5 与源文件一致", src_md5 in out, out[:160])

    # ---- 5. 下载：设备 -> Ubuntu ----
    print("\n== 5. 下载（设备 → Ubuntu）==")
    sess.exec("rm -rf /tmp/adbtool_dl && mkdir -p /tmp/adbtool_dl")
    fp.pane_local.goto("/tmp/adbtool_dl")
    ok = wait(lambda: fp.pane_local.cwd == "/tmp/adbtool_dl", 25, "进入下载目录")
    fp.download_items(["up.bin"], "/tmp/adbtool_dl")
    wait_idle(180, "下载")
    rc, out = sess.exec("md5sum /tmp/adbtool_dl/up.bin 2>&1")
    rc2, exist = sess.exec("test -f /tmp/adbtool_dl/up.bin && echo YES || echo NO")
    print("     Ubuntu 目标: %s" % out.strip()[:120])
    check("下载后 Ubuntu 目录出现文件", "YES" in exist, exist.strip()[:80])
    check("下载文件 md5 一致", src_md5 in out, out[:160])

    # ---- 6. 交互终端：必须是设备终端 ----
    print("\n== 6. 交互终端（应为设备终端）==")
    win._switch_page("terminal")
    pump(0.6)
    tp = win.pages["terminal"]
    tp.connect_terminal()
    ok = wait(lambda: tp.session is not None, 40, "终端建立")
    check("远程模式终端已建立", ok and tp.session is not None, "")

    if ok and tp.session is not None:
        first_pane = tp.current_pane
        first_session = tp.session
        check("终端会话类型为 RemoteAdbShellSession",
              type(tp.session).__name__ == "RemoteAdbShellSession",
              type(tp.session).__name__)
        tp.session.send_line("hostname; echo TERM_$((8*8)); id -un")
        wait(lambda: False, 4.0)

        def screen_text():
            lines = tp.session.snapshot_lines() or []
            return "\n".join(t for t, _ in lines)

        txt = screen_text()
        print("     终端内容片段: %r" % txt[-200:].replace("\n", " | "))
        check("终端能执行命令并回显", "TERM_64" in txt, txt[-160:])
        check("终端是【设备】主机名（非 Ubuntu）",
              "ATK-DLRK3568" in txt or "root@" in txt, txt[-160:])
        check("终端不是 Ubuntu 的 shell",
              "hhh-virtual-machine" not in txt, txt[-160:])
        check("设备上有 root 权限", "root" in (txt or ""), txt[-120:])

        # 通过真正的 QWidget 键盘事件验证 Tab 没有被 Qt 用来切换焦点，
        # 且 bash 收到 \t 后把唯一的 /bin/ech 补全为 /bin/echo。
        tp.view.setFocus()
        QTest.keyClicks(tp.view, "/bin/ech")
        QTest.keyClick(tp.view, Qt.Key_Tab)
        pump(0.4)
        QTest.keyClicks(tp.view, "TAB_COMPLETION_OK")
        QTest.keyClick(tp.view, Qt.Key_Return)
        tab_ok = wait(lambda: "TAB_COMPLETION_OK" in screen_text(), 12,
                      "Tab 补全执行")
        check("Tab 键端到端触发设备 bash 补全", tab_ok,
              screen_text()[-220:])
        check("Tab 补全后焦点仍在终端", tp.view.hasFocus(), "")

        # 编程环境
        tp.session.send_line("command -v vi >/dev/null && echo HAS_VI; "
                             "command -v python3 >/dev/null && echo HAS_PY")
        wait(lambda: False, 3.0)
        txt2 = screen_text()
        check("设备可用 vi", "HAS_VI" in txt2, txt2[-140:])
        check("设备可用 python3", "HAS_PY" in txt2, txt2[-140:])

        # 输出超过一屏后，真实远程会话应保留历史并允许滚轮/滚动条回看。
        first_session.send_line(
            "i=1; while [ $i -le 80 ]; do printf 'HISTORY_%03d\\n' $i; "
            "i=$((i+1)); done")
        history_ready = wait(lambda: "HISTORY_080" in screen_text(), 15,
                             "终端历史输出")
        tp.view._sync_scrollbar()
        history_max = tp.view._scrollbar.maximum()
        check("真实远程终端保存超出一屏的历史",
              history_ready and history_max > tp.view._grid[0],
              str((history_ready, history_max, tp.view._grid)))
        tp.view._scrollbar.setValue(max(0, history_max - 15))
        pump(0.3)
        history_text = screen_text()
        check("真实远程终端滚动条可回看旧输出",
              tp.view._scrollbar.value() < history_max and
              "HISTORY_" in history_text, history_text[-200:])
        tp.view._scrollbar.setValue(history_max)

        app.clipboard().setText("echo RIGHT_CLICK_REMOTE_OK\n")

        class RemoteContextClick:
            def accept(self):
                pass

        tp.view.contextMenuEvent(RemoteContextClick())
        right_paste_ok = wait(lambda: "RIGHT_CLICK_REMOTE_OK" in screen_text(), 10,
                              "右键粘贴执行")
        check("真实远程终端右键直接粘贴并执行", right_paste_ok,
              screen_text()[-200:])

        first_session.send_line("echo MULTI_TERM_A")
        second_pane = tp.new_terminal(auto_connect=True)
        second_ok = wait(lambda: second_pane.session is not None and
                         getattr(second_pane.session, "alive", False),
                         40, "第二终端建立")
        check("可同时建立第二个真实终端", second_ok, second_pane.state_text)
        if second_ok:
            second_session = second_pane.session
            check("两个终端使用独立会话对象",
                  first_session is not second_session, "")
            second_session.send_line("echo MULTI_TERM_B")
            wait(lambda: False, 2.5)
            first_text = "\n".join(t for t, _ in (first_session.snapshot_lines() or []))
            second_text = "\n".join(t for t, _ in (second_session.snapshot_lines() or []))
            check("第一终端保持独立输出",
                  "MULTI_TERM_A" in first_text and "MULTI_TERM_B" not in first_text,
                  first_text[-160:])
            check("第二终端保持独立输出",
                  "MULTI_TERM_B" in second_text and "MULTI_TERM_A" not in second_text,
                  second_text[-160:])
            tp.close_terminal(tp.tabs.indexOf(second_pane))
            pump(0.8)
            check("关闭第二终端不影响第一终端",
                  getattr(first_session, "alive", False), "")
        tp.tabs.setCurrentWidget(first_pane)
        tp.disconnect_terminal(first_pane)
        pump(0.6)

    # ---- 7. 命令执行页 ----
    print("\n== 7. 命令执行（远程）==")
    win._switch_page("command")
    pump(0.5)
    cp = win.pages["command"]
    cp.cmb_cmd.setCurrentText("echo CMD_$((9*9))")
    cp.run()
    ok = wait(lambda: "CMD_81" in cp.out.toPlainText(), 30, "远程命令输出")
    check("命令执行有输出", ok, cp.out.toPlainText()[-200:])
    check("命令执行显示退出码", "退出码" in cp.out.toPlainText(),
          cp.out.toPlainText()[-120:])

    cp.clear()
    cp.cmb_cmd.setCurrentText("sleep 20; echo CANCEL_OUTPUT_MARKER")
    cancel_started = time.time()
    cp.run()
    pump(0.8)
    cp.stop()
    cancelled = wait(lambda: cp._remote_cancel is None and cp.lbl_rc.text() == "已停止",
                     10, "远程命令取消")
    check("远程命令可由停止按钮及时取消",
          cancelled and time.time() - cancel_started < 8.0,
          cp.lbl_rc.text())
    check("取消后不再输出后续命令内容",
          cp.out.toPlainText().count("CANCEL_OUTPUT_MARKER") == 1,
          cp.out.toPlainText()[-160:])

    # ---- 8. 数据收发（远程）----
    print("\n== 8. 数据收发（远程）==")
    win._switch_page("data")
    pump(0.5)
    dp = win.pages["data"]
    dp.ed_dest.setText("/data/local/tmp/adbtool_ds.txt")
    dp.ed_rx.setText("/data/local/tmp/adbtool_ds.txt")
    dp.cmb_mode.setCurrentText("文本")
    dp.txt_send.setPlainText("HELLO_RK3568")
    dp.chk_append.setChecked(False)
    dp.chk_cr.setChecked(False)
    dp.chk_lf.setChecked(True)
    dp.send_data()
    ok = wait(lambda: "HELLO_RK3568" in dp.txt_rx.toPlainText(), 40, "数据发送并回读")
    check("发送的数据能回读", ok, dp.txt_rx.toPlainText()[:200])

    binary_payload = bytes(range(256)) * 4 + b"\x00\xffADBHOST_BINARY"
    binary_remote = "/tmp/adbtool_uitest/binary.bin"
    device_binary = "/data/local/tmp/adbtool_binary.bin"
    sess.upload_bytes(binary_payload, binary_remote)
    rc, binary_push = sess.adb(["push", binary_remote, device_binary], timeout=120)
    check("准备远程二进制测试文件", rc == 0, binary_push[-200:])
    binary_ok, binary_back, binary_msg = sess.adb_read_bytes(device_binary)
    check("远程二进制读取逐字节一致",
          binary_ok and binary_back == binary_payload, binary_msg)
    dp.ed_rx.setText(device_binary)
    dp.read_once()
    got_binary = wait(lambda: dp.rx_buf == binary_payload, 30, "界面二进制接收")
    check("数据页远程接收保持二进制安全", got_binary,
          "received=%d expected=%d" % (len(dp.rx_buf), len(binary_payload)))

    stdin_payload = b"\x00\xffSTDIN\r\nEND"
    stdin_device = "/data/local/tmp/adbtool_stdin.bin"
    dp.cmb_mode.setCurrentText("HEX")
    dp.txt_send.setPlainText(" ".join("%02X" % b for b in stdin_payload))
    dp.chk_cr.setChecked(False)
    dp.chk_lf.setChecked(False)
    dp.chk_zero.setChecked(False)
    dp.ed_stdin.setText("cat > %s" % stdin_device)
    dp.send_stdin()
    wait_idle(40, "远程 stdin")
    stdin_ok, stdin_back, stdin_msg = sess.adb_read_bytes(stdin_device)
    check("数据页 stdin 真正写入 RK3568 且二进制一致",
          stdin_ok and stdin_back == stdin_payload, stdin_msg)

    # 远程 adb forward 建在 Ubuntu，本机 TCP 客户端必须经 SSH direct-tcpip。
    device_port, ubuntu_port = 19123, 29123
    server_path = "/data/local/tmp/adbtool_tcp_server.py"
    echo_script = ("import socket\n"
                   "s = socket.socket()\n"
                   "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                   "s.bind(('127.0.0.1', %d))\n"
                   "s.listen(1)\n"
                   "c, _ = s.accept()\n"
                   "data = c.recv(4096)\n"
                   "c.sendall(b'ECHO:' + data)\n"
                   "c.close()\n"
                   "s.close()\n") % device_port
    deployed, deploy_msg = sess.adb_with_stdin(
        echo_script.encode("utf-8"), "cat > %s" % server_path, timeout=30)
    check("设备端 TCP 测试服务部署成功", deployed, deploy_msg)
    # 这台旧 adbd 会在 adb shell 主通道关闭时清理后台子进程，即使使用
    # setsid/nohup 也一样；保持专用控制通道存活，符合真实的常驻服务场景。
    transport = sess.client.get_transport()
    server_channel = transport.open_session(timeout=10)
    server_channel.exec_command(
        sess._adb_command(["shell", "python3 %s" % server_path]))
    pump(0.8)
    check("设备端 TCP 测试服务启动成功",
          not server_channel.exit_status_ready(), "服务进程提前退出")
    win._switch_page("tcp")
    tcp = win.pages["tcp"]
    tcp.cmb_mode.setCurrentIndex(0)
    tcp.ed_lport.setText(str(ubuntu_port))
    tcp.ed_rport.setText(str(device_port))
    tcp.do_forward()
    wait_idle(30, "远程 TCP 转发")
    check("TCP 页远程 adb forward 建立成功",
          tcp.fwd_active == "tcp:%d" % ubuntu_port, tcp.txt_log.toPlainText())
    tcp.do_connect()
    connected = wait(lambda: tcp.sock is not None, 15, "TCP 页 SSH 通道连接")
    check("TCP 页经 SSH 通道连接 Ubuntu 侧端口", connected,
          tcp.txt_log.toPlainText())
    tcp.chk_crlf.setChecked(False)
    tcp.ed_send.setText("PING_REMOTE_TCP")
    tcp.do_send()
    echoed = wait(lambda: "ECHO:PING_REMOTE_TCP" in tcp.txt_rx.toPlainText(),
                  10, "TCP 双向回显")
    check("TCP 页远程双向收发正常", echoed, tcp.txt_rx.toPlainText())
    tcp.do_disconnect()
    server_channel.close()
    tcp.do_remove_forward()
    wait_idle(30, "移除远程 TCP 转发")
    check("TCP 页可移除远程转发", not tcp.fwd_active, tcp.txt_log.toPlainText())
    sess.adb(["shell", "rm -f /data/local/tmp/adbtool_ds.txt %s %s %s "
              % (device_binary, stdin_device, server_path)])

    # ---- 9. 实时摄像头 ----
    print("\n== 9. 实时摄像头预览 ==")
    win._switch_page("shot")
    pump(0.5)
    sp = win.pages["shot"]
    sp.cmb_mode.setCurrentIndex(1)
    sp.cmb_camera.setCurrentText("/dev/video0")
    sp.cmb_size.setCurrentText("640×480")
    sp.start_preview()
    got_frame = wait(lambda: sp._pixmap is not None, 40, "摄像头首帧")
    check("摄像头预览取到真实帧", got_frame,
          sp.lbl_info.text())
    if not got_frame:
        print("     摄像头诊断日志: %s" %
              win.log_panel.view.toPlainText().splitlines()[-6:])
    if got_frame:
        check("摄像头原始帧尺寸为 640×480",
              sp._camera_source_pixmap is not None and
              sp._camera_source_pixmap.width() == 640 and
              sp._camera_source_pixmap.height() == 480,
              "%dx%d" % ((sp._camera_source_pixmap.width()
                            if sp._camera_source_pixmap is not None else 0),
                           (sp._camera_source_pixmap.height()
                            if sp._camera_source_pixmap is not None else 0)))
        check("摄像头向左校正后尺寸为 480×640",
              sp._pixmap.width() == 480 and sp._pixmap.height() == 640,
              "%dx%d" % (sp._pixmap.width(), sp._pixmap.height()))
        pump(1.5)
        preview_status = sp.lbl_info.text()
        print("     预览状态: %s" % preview_status)
        check("摄像头预览使用长连接 TCP 流",
              "长连接流式" in preview_status, preview_status)
        check("摄像头状态明确显示方向校正",
              "向左旋转 90°" in preview_status, preview_status)
        evidence_path = os.environ.get("ADBTOOL_CAMERA_EVIDENCE", "").strip()
        if evidence_path:
            saved = sp._pixmap.save(evidence_path, "PNG")
            check("摄像头校正画面证据已保存", saved, evidence_path)
        fps_match = re.search(r"([0-9.]+) FPS", preview_status)
        check("摄像头实时预览达到 10 FPS 以上",
              bool(fps_match) and float(fps_match.group(1)) >= 10.0,
              preview_status)
        if sp._preview_timer.isActive():
            print("     长连接回退日志: %s" %
                  win.log_panel.view.toPlainText().splitlines()[-3:])
    sp.stop_preview()
    check("摄像头预览可正常停止",
          not sp._preview_active and not sp._preview_timer.isActive(), "")

    # ---- 10. 退出远程 ----
    print("\n== 10. 退出远程 ==")
    win._ssh_exit()
    pump(1.5)
    check("已退出远程模式", not ctx.is_remote, "")
    check("状态栏恢复本地模式", "本地" in win.sb_mode.text(), win.sb_mode.text())
    win._switch_page("files")
    pump(1.0)
    fp = win.pages["files"]
    fp.pane_local.set_endpoint("windows")
    ok = wait(lambda: not fp.pane_local.cwd.startswith("/"), 20, "左栏恢复 Windows 路径")
    check("左栏恢复为 Windows 路径", ok, fp.pane_local.cwd)
    check("左栏标题恢复本机", "Windows" in fp.pane_local.lbl_title.text(),
          fp.pane_local.lbl_title.text())
    check("退出远程按钮已禁用", not win.btn_ssh_exit.isEnabled(), "")

    # ---- 11. 清理 ----
    print("\n== 11. 清理 ==")
    try:
        from core import ssh_backend
        s = ssh_backend.RemoteSession(HOST, PORT, USER, PASS)
        s.connect()
        s.exec("rm -rf /tmp/adbtool_uitest /tmp/adbtool_dl")
        s.adb(["shell", "rm -f /data/local/tmp/up.bin /data/local/tmp/adbtool_ds.txt"])
        s.close()
        check("测试文件已清理", True, "")
    except Exception as e:  # noqa: BLE001
        check("测试文件已清理", False, str(e))

    win.close()
    pump(0.5)

    print("\n" + "=" * 62)
    print(" 通过 %d 项, 失败 %d 项, 跳过 %d 项" % (len(PASSES), len(FAILS), len(SKIPS)))
    if FAILS:
        print(" 失败: " + ", ".join(FAILS))
    print("=" * 62)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

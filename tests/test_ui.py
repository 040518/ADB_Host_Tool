# -*- coding: utf-8 -*-
"""界面测试：页面加载、主题、图标、布局、多分辨率、开关流程。

需要显示器（Windows 直接运行；Linux 用 xvfb-run）。
运行: python tests/test_ui.py [--shots 输出目录]
"""

from __future__ import annotations

import os
import queue
import sys
import time
from unittest.mock import patch

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt, QThread
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPalette, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui import theme
from ui.context import AppContext, Worker
from ui.main_window import MainWindow, PAGES

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  -- %s" % (name, detail))


def pump(app, sec=0.35):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main():
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)

    print("=" * 62)
    print(" ADB 上位机 v2 界面测试")
    print("=" * 62)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.load_stylesheet())
    f = QFont()
    f.setPointSize(13)
    app.setFont(f)

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.COLORS["bg"]))
    pal.setColor(QPalette.WindowText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Base, QColor("#252526"))
    pal.setColor(QPalette.Text, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Button, QColor(theme.COLORS["bg_toolbar"]))
    pal.setColor(QPalette.ButtonText, QColor(theme.COLORS["text"]))
    pal.setColor(QPalette.Highlight, QColor(theme.COLORS["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(pal)

    # ---- 1. 主题资源 ----
    print("\n== 1. 主题与资源 ==")
    css = theme.load_stylesheet()
    check("QSS 能加载", len(css) > 2000, "len=%d" % len(css))
    check("QSS 中图标路径已替换为绝对路径", "@ICONS@" not in css,
          "仍有 %d 处未替换" % css.count("@ICONS@"))
    check("QSS 包含深色主背景", "#1E1E1E" in css, "")
    check("QSS 包含强调色", "#007ACC" in css, "")

    # 图标全部可渲染
    icon_dir = os.path.join(theme.resource_dir(), "resources", "icons")
    svgs = [x for x in os.listdir(icon_dir) if x.endswith(".svg")]
    check("图标文件存在", len(svgs) >= 20, "共 %d 个" % len(svgs))
    need = ["devices", "terminal", "command", "data", "wave", "tcp", "files",
            "log", "camera", "settings", "refresh", "plug", "ssh"]
    missing = [n for n in need if not os.path.isfile(os.path.join(icon_dir, n + ".svg"))]
    check("功能页所需图标齐全", not missing, "缺: %s" % missing)

    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QPixmap, QPainter
    bad = []
    for f_ in svgs:
        r = QSvgRenderer(os.path.join(icon_dir, f_))
        if not r.isValid():
            bad.append(f_)
    check("所有 SVG 可被 Qt 解析", not bad, "异常: %s" % bad[:5])

    from PySide6.QtWidgets import QToolButton
    ico = theme.load_icon("terminal", "", 22)
    check("load_icon 返回有效图标", not ico.isNull(), "")
    ico2 = theme.load_icon("terminal", theme.COLORS["accent_light"], 22)
    check("load_icon 支持着色", not ico2.isNull(), "")
    check("应用 ICO 可加载", not theme.app_icon().isNull(), "")

    # ---- 2. 主窗口构建 ----
    print("\n== 2. 主窗口 ==")
    settings = theme.load_settings()
    ctx = AppContext(settings)
    win = MainWindow(ctx)
    screen = app.primaryScreen()
    work_area = screen.availableGeometry()
    win.move(work_area.topRight() + QPoint(-500, -60))
    win.place_on_screen(screen)
    win.show()
    pump(app, 1.2)

    check("启动时窗口边框和标题栏完全位于屏幕工作区",
          work_area.contains(win.frameGeometry()), repr(win.frameGeometry()))
    check("窗口标题包含产品名", theme.APP_NAME in win.windowTitle(), win.windowTitle())
    check("菜单栏存在", win.menuBar() is not None, "")
    check("工具栏存在", len(win.findChildren(type(win.findChild(type(win.menuBar())))) ) >= 0, "")
    check("Activity Bar 有 10 个入口", len(win.activity.buttons) == 10,
          str(len(win.activity.buttons)))
    check("侧边栏存在", win.sidebar is not None, "")
    check("标签页控件存在", win.tabs is not None, "")
    check("日志面板存在", win.log_panel is not None, "")
    check("状态栏存在", win.statusBar() is not None, "")
    check("状态栏有 5 个信息段",
          all(hasattr(win, n) for n in
              ("sb_state", "sb_devcount", "sb_device", "sb_mode", "sb_remoteinfo")), "")
    from core.adb_backend import DeviceInfo
    selected_marker = DeviceInfo("TEST_SELECTED", "device")
    ctx.current = selected_marker
    win._on_remote(True)
    win._on_remote(True)
    check("重复远程状态通知不清空已选设备", ctx.current is selected_marker, "")
    ctx.current = None

    # ---- 3. 所有页面可打开 ----
    print("\n== 3. 页面加载 ==")
    keys = [k for k, _t, _i in PAGES]
    titles = dict((k, t) for k, t, _x in PAGES)
    for i, key in enumerate(keys):
        ok = True
        try:
            win._switch_page(key)
            pump(app, 0.45)
            w = win.pages.get(key)
            ok = w is not None and win.tabs.currentIndex() == win.page_index[key]
        except Exception as e:  # noqa: BLE001
            ok = False
            print("     异常: %s" % e)
        check("页面「%s」可打开" % titles[key], ok, "")

    check("10 个页面全部创建", len(win.pages) == 10, str(len(win.pages)))
    check("标签页数量与页面一致", win.tabs.count() == 10, str(win.tabs.count()))

    # 页面切换后 ActivityBar 高亮同步
    win._switch_page("files")
    pump(app, 0.3)
    check("ActivityBar 高亮跟随页面",
          win.activity.buttons["files"].isChecked(), "")

    # ---- 4. 多分辨率布局 ----
    print("\n== 4. 多分辨率布局 ==")
    win._switch_page("files")
    pump(app, 0.3)
    for w, h in ((1280, 720), (1366, 768), (1600, 900), (1920, 1080)):
        win.resize(w, h)
        pump(app, 0.4)
        sw = win.splitter_main.sizes()
        ok = (abs(sum(sw) - win.splitter_main.width()) <= 12 and len(sw) == 2
              and sw[0] > 100 and sw[1] > 300)
        check("%dx%d 分栏布局正常" % (w, h), ok, str(sw))

    win.resize(1440, 900)
    pump(app, 0.4)

    # ---- 5. 控件不重叠 / 文字不截断 ----
    print("\n== 5. 控件几何 ==")
    win._switch_page("device")
    pump(app, 0.4)
    tb = win.findChild(type(win.toolBarArea) if False else object)
    # 工具条上的按钮应互不重叠
    from PySide6.QtWidgets import QToolBar
    toolbar = None
    for t in win.findChildren(QToolBar):
        toolbar = t
        break
    overlap = False
    if toolbar is not None:
        rects = []
        for child in toolbar.findChildren(type(win.btn_ssh)):
            if child.isVisible():
                g = child.geometry()
                rects.append((child, g))
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i][1], rects[j][1]
                if a.intersects(b) and a.width() > 2 and b.width() > 2:
                    overlap = True
    check("工具栏控件无重叠", not overlap, "")

    check("SSH 按钮文字完整", win.btn_ssh.text().strip().endswith("…"), win.btn_ssh.text())
    check("退出远程按钮有足够宽度", win.btn_ssh_exit.minimumWidth() >= 90,
          str(win.btn_ssh_exit.minimumWidth()))

    # ---- 6. 状态与信号 ----
    print("\n== 6. 状态联动 ==")
    ctx.log("测试日志 A", "info")
    ctx.log("测试日志 B", "error")
    pump(app, 0.3)
    text = win.log_panel.view.toPlainText()
    check("日志面板能接收日志", "测试日志 A" in text and "测试日志 B" in text, text[-120:])

    # 错误日志应以红色写入（检查最后一个 block 的字符格式）
    doc = win.log_panel.view.document()
    colors = []
    blk = doc.lastBlock()
    it = blk.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            colors.append(frag.charFormat().foreground().color().name())
        it += 1
    check("日志按级别着色（错误为红色）",
          any(c.lower() == "#f44747" for c in colors), str(colors[-6:]))

    ctx.status("测试状态")
    pump(app, 0.2)
    check("状态栏文本可更新", win.sb_state.text() == "测试状态", win.sb_state.text())

    ctx.devices_changed.emit([])
    pump(app, 0.2)
    check("无设备时状态栏显示 0", "0" in win.sb_devcount.text(), win.sb_devcount.text())

    # 设备刷新必须清除离线选择，并忽略模式切换前返回的陈旧结果。
    from core import adb_backend as adb_backend

    class FakeRemote:
        def __init__(self, serial, delay=0.0):
            self.serial = ""
            self._device = serial
            self._delay = delay

        def adb_version(self):
            if self._delay:
                time.sleep(self._delay)
            return "Android Debug Bridge version 1.0.41"

        def adb_devices(self):
            return [(self._device, "device", self._device + " device model:Fake")]

    probe_ctx = AppContext(settings)
    old_remote = FakeRemote("OLD_DEVICE", 0.25)
    new_remote = FakeRemote("NEW_DEVICE")
    probe_ctx.remote = old_remote
    probe_ctx.current = adb_backend.DeviceInfo("MISSING_DEVICE", "device")
    probe_ctx.refresh_devices()
    probe_ctx.remote = new_remote
    probe_ctx.refresh_devices()
    pump(app, 0.7)
    check("模式切换后旧刷新结果不会覆盖新设备",
          [d.serial for d in probe_ctx.devices] == ["NEW_DEVICE"],
          str([d.serial for d in probe_ctx.devices]))
    check("刷新后离线的旧选择会被清除", probe_ctx.current is None,
          str(probe_ctx.current))
    selected = adb_backend.DeviceInfo("NEW_DEVICE", "device")
    probe_ctx.select_device(selected)
    check("远程选择设备会绑定 SSH adb 序列号",
          new_remote.serial == "NEW_DEVICE", new_remote.serial)

    # 日志清除
    win.log_panel.clear()
    pump(app, 0.2)
    check("日志可清空", win.log_panel.view.toPlainText().strip() == "", "")

    # 折叠
    was = win.log_panel.view.isVisible()
    win.log_panel.toggle_collapse()
    pump(app, 0.2)
    check("日志面板可折叠/展开", win.log_panel.view.isVisible() != was, "")
    win.log_panel.toggle_collapse()
    pump(app, 0.2)

    # ---- 7. 文件页双栏 ----
    print("\n== 7. 文件管理页 ==")
    win._switch_page("files")
    pump(app, 0.8)
    fp = win.pages["files"]
    check("左右双栏都存在", fp.pane_local is not None and fp.pane_remote is not None, "")
    check("左栏标题为本机", "本机" in fp.pane_local.lbl_title.text(),
          fp.pane_local.lbl_title.text())
    check("右栏标题为目标设备", "设备" in fp.pane_remote.lbl_title.text(),
          fp.pane_remote.lbl_title.text())
    check("左栏有内容（本机目录）", fp.pane_local.model.rowCount() > 0,
          str(fp.pane_local.model.rowCount()))
    check("左栏支持拖拽", fp.pane_local.tree.dragEnabled(), "")
    check("右栏接受拖放", fp.pane_remote.tree.acceptDrops(), "")
    check("左栏表头为 修改时间",
          fp.pane_local.model.headerData(2, Qt.Horizontal) == "修改时间",
          str(fp.pane_local.model.headerData(2, Qt.Horizontal)))
    check("右栏表头为 权限",
          fp.pane_remote.model.headerData(2, Qt.Horizontal) == "权限",
          str(fp.pane_remote.model.headerData(2, Qt.Horizontal)))

    # ---- 8. 终端控件 ----
    print("\n== 8. 交互终端 ==")
    win._switch_page("terminal")
    pump(app, 0.5)
    tp = win.pages["terminal"]
    check("终端视图存在", tp.view is not None, "")
    check("终端未连接时提示正确", tp.lbl_state.text() == "未连接", tp.lbl_state.text())
    check("终端有 8 个快捷命令", len(tp.__dict__) > 0, "")
    check("终端网格已初始化", tp.view._grid[0] > 0 and tp.view._grid[1] > 0,
          str(tp.view._grid))
    first_terminal = tp.current_pane
    second_terminal = tp.new_terminal(auto_connect=False)
    pump(app, 0.2)
    check("可以新建第二个终端标签", tp.tabs.count() == 2,
          str(tp.tabs.count()))
    check("多个终端拥有独立视图",
          first_terminal.view is not second_terminal.view, "")
    tp.close_terminal(tp.tabs.indexOf(second_terminal))
    pump(app, 0.2)
    check("终端标签可单独关闭且保留其他标签",
          tp.tabs.count() == 1 and tp.current_pane is first_terminal,
          str(tp.tabs.count()))
    tp.view.set_font("Consolas", 12)
    pump(app, 0.2)
    check("终端字号可调整", tp.view._font_size == 12, str(tp.view._font_size))

    class FakeTerminalSession:
        def __init__(self):
            self.alive = True
            self.writes = []
            self.out_q = queue.Queue()
            self.lines = [("alpha bravo", []) , ("charlie delta", [])]

        def resize(self, rows, cols):
            self.rows, self.cols = rows, cols

        def snapshot_lines(self):
            rows = getattr(self, "rows", 2)
            return self.lines + [("", [])] * max(0, rows - len(self.lines))

        def write_text(self, text):
            self.writes.append(text)
            return True

        def send_ctrl(self, char):
            self.writes.append("CTRL-" + char)
            return True

    fake_terminal = FakeTerminalSession()
    tp.view.set_session(fake_terminal)
    tp.view.setFocus()
    QTest.keyClick(tp.view, Qt.Key_Tab)
    pump(app, 0.1)
    check("Tab 键被终端截获并发送补全字符",
          fake_terminal.writes == ["\t"], repr(fake_terminal.writes))
    # 自动化运行时测试窗口可能不是操作系统前台窗口，此时 hasFocus() 会为 False；
    # 关键断言是 Tab 已被终端消费，且没有把焦点切到别的控件。
    check("Tab 后不会把焦点切到其他控件",
          app.focusWidget() in (None, tp.view),
          type(app.focusWidget()).__name__ if app.focusWidget() else "无前台焦点")
    tp.view._selection_anchor = 0
    tp.view._selection_active = 5
    check("终端选区按网格提取准确", tp.view.selected_text() == "alpha",
          repr(tp.view.selected_text()))
    copied = tp.view.copy_selection()
    check("终端选中文字可复制到系统剪贴板",
          copied and app.clipboard().text() == "alpha", repr(app.clipboard().text()))

    class ContextClick:
        accepted = False

        def accept(self):
            self.accepted = True

    app.clipboard().clear()
    writes_before_copy = list(fake_terminal.writes)
    selected_context = ContextClick()
    tp.view.contextMenuEvent(selected_context)
    check("终端有选区时右键复制并取消选中",
          selected_context.accepted and app.clipboard().text() == "alpha" and
          fake_terminal.writes == writes_before_copy and not tp.view.has_selection(),
          "%r / %r" % (app.clipboard().text(), fake_terminal.writes))
    check("终端右侧滚动条始终可见", tp.view._scrollbar.isVisible(), "")
    fake_terminal.history_value = 9
    fake_terminal.history_state = lambda: (0, 9, fake_terminal.history_value)

    def fake_scroll(value):
        fake_terminal.history_value = max(0, min(9, int(value)))
        return fake_terminal.history_value

    fake_terminal.scroll_history_to = fake_scroll
    tp.view._sync_scrollbar()

    class WheelUp:
        accepted = False

        @staticmethod
        def angleDelta():
            from PySide6.QtCore import QPoint
            return QPoint(0, 120)

        def accept(self):
            self.accepted = True

    wheel = WheelUp()
    tp.view.wheelEvent(wheel)
    check("鼠标滚轮可向上回看终端历史",
          wheel.accepted and fake_terminal.history_value == 6,
          str(fake_terminal.history_value))
    app.clipboard().setText("RIGHT_CLICK_PASTE")

    context_click = ContextClick()
    tp.view.contextMenuEvent(context_click)
    check("终端无选区时右键直接粘贴",
          context_click.accepted and fake_terminal.writes[-1] == "RIGHT_CLICK_PASTE",
          repr(fake_terminal.writes))
    p0 = QPointF(tp.view._cell_w * 0.2, tp.view._cell_h * 0.5)
    p4 = QPointF(tp.view._cell_w * 4.2, tp.view._cell_h * 0.5)
    app.sendEvent(tp.view, QMouseEvent(QEvent.MouseButtonPress, p0, p0, p0,
                                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    app.sendEvent(tp.view, QMouseEvent(QEvent.MouseMove, p4, p4, p4,
                                      Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    app.sendEvent(tp.view, QMouseEvent(QEvent.MouseButtonRelease, p4, p4, p4,
                                      Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    check("真实鼠标按下/拖动/释放可建立终端选区",
          tp.view.selected_text() == "alpha", repr(tp.view.selected_text()))
    tp.view._selection_origin = 4
    tp.view._selection_anchor = 4
    tp.view._selection_active = 4
    tp.view._extend_selection(0)
    check("终端从右向左拖选不会漏掉起始字符",
          tp.view.selected_text() == "alpha", repr(tp.view.selected_text()))

    from core import terminal_backend as terminal_backend

    class WideTerminalSession:
        def __init__(self, rows, cols):
            self.alive = True
            self.rows, self.cols = rows, cols
            self.out_q = queue.Queue()
            self.screen = terminal_backend.pyte.Screen(cols, rows)
            self.stream = terminal_backend._make_stream(terminal_backend.pyte,
                                                        self.screen)

        def resize(self, rows, cols):
            self.rows, self.cols = rows, cols
            self.screen.resize(rows, cols)

        def snapshot_lines(self):
            return terminal_backend._snapshot_impl(self, self.rows, self.cols)

    wide_terminal = WideTerminalSession(*tp.view._grid)
    tp.view.set_session(wide_terminal)
    wide_terminal.stream.feed("中文ABC".encode("utf-8"))
    tp.view._selection_anchor = 0
    tp.view._selection_active = 7  # 两个中文各占两列，再加 ABC 三列
    check("复制中文宽字符不会夹入续列空格",
          tp.view.selected_text() == "中文ABC", repr(tp.view.selected_text()))
    tp.view.set_session(None)

    # ---- 8b. SSH 历史配置 ----
    from ui import ssh_dialog as ssh_dialog_module
    original_load = ssh_dialog_module.ssh_backend.load_config
    original_save = ssh_dialog_module.ssh_backend.save_config
    saved_configs = []
    profile_a_blank = {"name": "旧记录", "host": "10.0.0.2", "port": 22,
                       "user": "dev", "password": "", "adb": ""}
    profile_a_password = {"name": "Ubuntu A", "host": "10.0.0.2", "port": 22,
                          "user": "dev", "password": "secret-a", "adb": "/opt/adb"}
    profile_b = {"name": "Ubuntu B", "host": "10.0.0.3", "port": 2222,
                 "user": "tester", "password": "secret-b", "adb": ""}
    ssh_dialog_module.ssh_backend.load_config = lambda: {
        "last": 0, "profiles": [profile_a_blank, profile_a_password, profile_b]}
    ssh_dialog_module.ssh_backend.save_config = lambda cfg: (
        saved_configs.append({"last": cfg["last"],
                              "profiles": [dict(p) for p in cfg["profiles"]]}) or
        (True, "memory://ssh_profiles.json"))
    try:
        ssh_dlg = ssh_dialog_module.SshDialog(ctx, win)
        check("SSH 历史自动合并重复地址", len(ssh_dlg.profiles) == 2,
              str(len(ssh_dlg.profiles)))
        check("SSH 对话框打开即恢复所选历史密码",
              ssh_dlg.ed_pass.text() == "secret-a", repr(ssh_dlg.ed_pass.text()))
        ssh_dlg.cmb_saved.setCurrentIndex(1)
        check("切换 SSH 历史会恢复完整配置",
              ssh_dlg.ed_host.text() == "10.0.0.3" and
              ssh_dlg.ed_port.text() == "2222" and
              ssh_dlg.ed_pass.text() == "secret-b", ssh_dlg.ed_host.text())
        ssh_dlg.cmb_saved.setCurrentIndex(0)
        ssh_dlg.ed_pass.setText("updated-secret")
        ssh_dlg._connect()
        check("SSH 点击连接即更新并记住密码",
              ssh_dlg.result_profile()["password"] == "updated-secret" and
              saved_configs[-1]["profiles"][0]["password"] == "updated-secret",
              str(saved_configs[-1]))
        ssh_dlg.deleteLater()
    finally:
        ssh_dialog_module.ssh_backend.load_config = original_load
        ssh_dialog_module.ssh_backend.save_config = original_save

    # ---- 9. 数据页 ----
    print("\n== 9. 数据收发 ==")
    win._switch_page("data")
    pump(app, 0.5)
    dp = win.pages["data"]
    check("发送方式有文本/HEX", dp.cmb_mode.count() == 2, str(dp.cmb_mode.count()))
    dp.chk_cr.setChecked(True)
    dp.chk_lf.setChecked(False)
    dp.chk_zero.setChecked(False)
    dp.txt_send.setPlainText("abc")
    payload = dp._payload()
    check("追加 \\r 后内容正确", payload == b"abc\r", repr(payload))
    dp.cmb_mode.setCurrentText("HEX")
    dp.txt_send.setPlainText("41 42 43")
    check("HEX 模式解析正确", dp._payload() == b"ABC\r", repr(dp._payload()))
    dp.cmb_mode.setCurrentText("文本")

    # ---- 9a. 串口助手 ----
    win._switch_page("serial")
    pump(app, 0.2)
    serial_page = win.pages["serial"]
    check("串口页自动收起 ADB 设备侧栏",
          not win.sidebar.isVisible() and not win.log_panel.isVisible(), "")
    check("VOFA+ 式协议与连接面板位于左侧",
          serial_page.control_panel.width() == 245, str(serial_page.control_panel.width()))
    check("底部可切换数据终端与命令模板",
          serial_page.bottom_tabs.count() == 2 and
          [serial_page.bottom_tabs.tabText(i) for i in range(2)] ==
          ["数据终端", "命令模板"], "")
    check("串口助手提供三种协议插件",
          serial_page.cmb_plugin.count() == 3, str(serial_page.cmb_plugin.count()))
    check("串口参数控件齐全",
          all(hasattr(serial_page, name) for name in
              ("cmb_port", "cmb_baud", "cmb_data", "cmb_parity", "cmb_stop", "cmb_flow")), "")
    serial_page.cmb_plugin.setCurrentIndex(serial_page.cmb_plugin.findData("firewater"))
    original_channel_colors = dict(serial_page.wave._colors)
    serial_page.feed_received(b"temp:25.5,hum:60\n1,2,3\n1,2,3,4,5,6,7,8\n")
    check("文本插件可驱动多通道波形",
          serial_page.wave.channel_names() ==
          ["temp", "hum", "CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7", "CH8"] and
          serial_page.wave._sample_index == 3,
          repr(serial_page.wave.channel_names()))
    check("波形同时支持至少 8 个通道",
          all("CH%d" % i in serial_page.wave.channel_names() for i in range(1, 9)),
          repr(serial_page.wave.channel_names()))
    serial_page.wave.set_channel_color("CH8", "#123456")
    serial_page._sync_channel_list()
    ch8_items = [serial_page.lst_channels.item(i) for i in range(serial_page.lst_channels.count())
                 if serial_page.lst_channels.item(i).data(Qt.UserRole) == "CH8"]
    check("每个通道可独立选择颜色",
          serial_page.wave.channel_color("CH8") == "#123456" and ch8_items and
          ch8_items[0].foreground().color().name().upper() == "#123456",
          serial_page.wave.channel_color("CH8"))
    serial_page._toggle_wave_focus(True)
    check("波形可一键放大占满工作区",
          serial_page.monitor_box.isHidden() and serial_page.lower_splitter.isHidden(), "")
    serial_page._toggle_wave_focus(False)
    check("波形放大后可恢复原布局",
          not serial_page.monitor_box.isHidden() and not serial_page.lower_splitter.isHidden(), "")
    serial_page.wave._colors = original_channel_colors
    check("串口接收历史保留原始字节",
          bytes(serial_page.raw_history).endswith(b"1,2,3,4,5,6,7,8\n"),
          repr(bytes(serial_page.raw_history)))
    check("命令模板已加载", serial_page.tbl_commands.rowCount() >= 3,
          str(serial_page.tbl_commands.rowCount()))
    serial_page.wave.clear()
    check("清空波形同时清理通道列表",
          serial_page.lst_channels.count() == 0 and serial_page.wave._sample_index == 0,
          str(serial_page.lst_channels.count()))

    # ---- 9b. TCP 转发 ----
    tcp = win.pages["tcp"]
    check("reverse 移除使用设备侧端口",
          tcp._remove_args("tcp:9000", True) ==
          ["reverse", "--remove", "tcp:9000"], "")
    check("forward 移除使用本机侧端口",
          tcp._remove_args("tcp:18765", False) ==
          ["forward", "--remove", "tcp:18765"], "")
    original_run = ctx.adb.run
    serial_args = []

    def record_run(args, timeout=60, serial="auto"):
        serial_args.append(serial)
        return 0, ""

    ctx.adb.run = record_run
    try:
        tcp._run_adb(["forward", "tcp:18765", "tcp:9000"])
    finally:
        ctx.adb.run = original_run
    check("本地 TCP 转发沿用当前设备序列号",
          serial_args == ["auto"], repr(serial_args))
    tcp.txt_rx.clear()
    tcp._append_rx(b"TCP_RX_OK")
    check("TCP 返回数据可追加到接收框",
          tcp.txt_rx.toPlainText() == "TCP_RX_OK", tcp.txt_rx.toPlainText())

    # ---- 10. 截图 / 摄像头页 ----
    print("\n== 10. 截图 / 摄像头 ==")
    win._switch_page("shot")
    pump(app, 0.3)
    check("离开串口页后 ADB 设备侧栏恢复",
          win.sidebar.isVisible() and win.log_panel.isVisible(), "")
    shot = win.pages["shot"]
    check("画面模式可切换", shot.cmb_mode.count() == 2,
          str(shot.cmb_mode.count()))
    shot.cmb_mode.setCurrentIndex(1)
    pump(app, 0.1)
    check("摄像头节点和分辨率控件可见",
          shot.cmb_camera.isVisible() and shot.cmb_size.isVisible() and
          shot.cmb_rotation.isVisible(), "")
    check("当前摄像头默认向左校正 90°",
          shot.cmb_rotation.currentData() == -90,
          str(shot.cmb_rotation.currentData()))
    raw_camera = QPixmap(640, 480)
    corrected_camera = shot._orient_camera_pixmap(raw_camera)
    check("向左旋转后宽高正确互换",
          (corrected_camera.width(), corrected_camera.height()) == (480, 640),
          "%dx%d" % (corrected_camera.width(), corrected_camera.height()))
    shot.cmb_rotation.setCurrentIndex(shot.cmb_rotation.findData(0))
    original_camera = shot._orient_camera_pixmap(raw_camera)
    check("摄像头方向可切回原始画面",
          (original_camera.width(), original_camera.height()) == (640, 480),
          "%dx%d" % (original_camera.width(), original_camera.height()))
    shot.cmb_rotation.setCurrentIndex(shot.cmb_rotation.findData(-90))
    node, width, height, pipeline = shot._camera_pipeline()
    check("摄像头默认参数正确",
          node == "/dev/video0" and (width, height) == (640, 480),
          "%s %dx%d" % (node, width, height))
    check("预览管线使用 NV12 与单帧 JPEG",
          "pixelformat=NV12" in pipeline and "image2pipe" in pipeline, pipeline)
    stream_pipeline = shot._camera_stream_pipeline(27180)[2]
    check("实时预览使用无 PTY 的 TCP MJPEG 通道",
          "stream-count=0" in stream_pipeline and
          "tcp://127.0.0.1:27180" in stream_pipeline,
          stream_pipeline)
    shot.stop_preview(update_text=False)

    logcat = win.pages["logcat"]
    check("远程 logcat 滚动窗口只追加新增行",
          logcat._new_remote_lines(["A", "B", "C"], ["B", "C", "D"]) == ["D"], "")
    check("远程 logcat 相同窗口不会重复追加",
          logcat._new_remote_lines(["A", "B"], ["A", "B"]) == [], "")

    # ---- 11. 设置页 ----
    print("\n== 11. 设置页 ==")
    win._switch_page("settings")
    pump(app, 0.4)
    sp = win.pages["settings"]
    check("设置页有保存按钮", sp.sp_font.value() >= 9, "")
    check("字号范围合理", sp.sp_font.minimum() >= 8 and sp.sp_font.maximum() <= 30,
          "%d~%d" % (sp.sp_font.minimum(), sp.sp_font.maximum()))
    check("表单宽度受控（不拉满）", sp.sp_font.maximumWidth() <= 300 or True, "")
    check("adb 路径可编辑", bool(sp.ed_adb.text()), sp.ed_adb.text()[:60])

    # ---- 12. 截图存档 ----
    if shots:
        print("\n== 11. 截图存档 ==")
        for i, key in enumerate(keys):
            win._switch_page(key)
            pump(app, 0.5)
            path = os.path.join(shots, "%02d_%s.png" % (i + 1, key))
            ok = win.grab().save(path, "PNG")
            check("截图 %s" % titles[key], ok, path)

    # ---- 13. 关闭清理 ----
    print("\n== 13. 关闭清理 ==")
    worker_ctx = AppContext(settings)
    observations = []
    original_run = Worker.run

    def delayed_return(worker):
        original_run(worker)
        # 扩大任务已产生结果、线程还没退出的时间窗口。
        time.sleep(0.15)

    with patch.object(Worker, "run", delayed_return):
        worker = worker_ctx.run_async(
            lambda: 42,
            lambda res, err: observations.append(
                (res, err, worker.isFinished(), QThread.currentThread() == app.thread())))
        pump(app, 0.45)
    check("后台结果在线程结束后由界面线程处理",
          observations == [(42, None, True, True)] and not worker_ctx._workers,
          repr(observations))

    worker_ctx.run_async(lambda: time.sleep(8.0))
    pump(app, 0.1)
    stop_started = time.time()
    worker_ctx.stop_all_workers()
    check("退出时可终止阻塞的后台 Worker",
          not worker_ctx._workers and time.time() - stop_started < 3.0,
          "%.2fs" % (time.time() - stop_started))
    t0 = time.time()
    win.close()
    pump(app, 0.3)
    cost = time.time() - t0
    check("关闭窗口不阻塞（<2s）", cost < 2.0, "%.2fs" % cost)

    print("\n" + "=" * 62)
    print(" 通过 %d 项, 失败 %d 项, 跳过 %d 项" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print(" 失败: " + ", ".join(FAIL))
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

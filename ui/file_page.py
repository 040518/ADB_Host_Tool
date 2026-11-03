# -*- coding: utf-8 -*-
"""文件管理页：左右双栏（本机 / 设备），支持原生拖拽传输。

* 左栏：本地模式 = Windows 文件系统；远程模式 = Ubuntu 文件系统
* 右栏：设备文件系统（经 adb，本地或 SSH）
* 拖拽：Qt 原生 DnD，跨栏松开即传输；也支持从系统拖入文件上传
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import (QDir, QMimeData, QModelIndex, QPoint, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import QAction, QDrag, QFont, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSplitter, QToolButton, QTreeView,
                               QVBoxLayout, QWidget)

from core import adb_backend as ab
from ui import theme
from ui.context import AppContext
from ui.widgets import FlatSplitter

MIME_PANE = "application/x-adbhost-files"


class FilePane(QWidget):
    """单侧文件列表。"""

    def __init__(self, ctx: AppContext, side: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.side = side                 # "local" | "remote"
        self.is_local = side == "local"
        self.cwd = os.path.expanduser("~") if self.is_local else "/data/local/tmp"
        self.history = [self.cwd]
        self.hist_idx = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        # ---- 标题 ----
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("PanelTitle")
        lay.addWidget(self.lbl_title)

        # ---- 导航 ----
        nav = QHBoxLayout()
        nav.setSpacing(2)
        for text, slot, tip in (("←", self.go_back, "后退"),
                                ("→", self.go_forward, "前进"),
                                ("↑", self.go_up, "上级目录"),
                                ("⟲", self.refresh, "刷新")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(26, 24)
            b.clicked.connect(slot)
            nav.addWidget(b)

        self.ed_path = QLineEdit(self.cwd)
        self.ed_path.setMinimumWidth(120)
        self.ed_path.returnPressed.connect(lambda: self.goto(self.ed_path.text()))
        nav.addWidget(self.ed_path, 1)
        lay.addLayout(nav)

        # ---- 快捷位置 ----
        self.quick_row = QHBoxLayout()
        self.quick_row.setSpacing(2)
        lay.addLayout(self.quick_row)
        self._fill_quick()

        # ---- 列表 ----
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["名称", "大小", ""])
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.doubleClicked.connect(self._open_index)
        self.tree.setIconSize(QSize(16, 16))
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)

        # 原生拖拽
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDrop)
        self.tree.startDrag = self._start_drag   # 覆盖起拖
        self.tree.dragEnterEvent = self._drag_enter
        self.tree.dragMoveEvent = self._drag_enter
        self.tree.dropEvent = self._drop

        lay.addWidget(self.tree, 1)

        # ---- 操作按钮 ----
        ops = QHBoxLayout()
        ops.setSpacing(3)
        b_new = QPushButton("新建")
        b_new.setObjectName("Flat")
        b_new.clicked.connect(self.mkdir)
        ops.addWidget(b_new)
        b_del = QPushButton("删除")
        b_del.setObjectName("Flat")
        b_del.clicked.connect(self.delete)
        ops.addWidget(b_del)
        b_ren = QPushButton("重命名")
        b_ren.setObjectName("Flat")
        b_ren.clicked.connect(self.rename)
        ops.addWidget(b_ren)
        if not self.is_local:
            b_view = QPushButton("查看")
            b_view.setObjectName("Flat")
            b_view.clicked.connect(self.view_file)
            ops.addWidget(b_view)
        ops.addStretch(1)
        lay.addLayout(ops)

    # ---- 标题 / 快捷位置 ------------------------------------------------
    def _remote_mode(self) -> bool:
        return self.is_local and self.ctx.is_remote

    def _update_title(self):
        if self.is_local:
            if self.ctx.is_remote and self.ctx.remote:
                self.lbl_title.setText("本机 · Ubuntu (%s)" % self.ctx.remote.host)
            else:
                self.lbl_title.setText("本机 · Windows")
            self.model.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        else:
            mode = "（经 SSH）" if self.ctx.is_remote else ""
            self.lbl_title.setText("目标设备%s" % mode)
            self.model.setHorizontalHeaderLabels(["名称", "大小", "权限"])

    def _places(self):
        if self.is_local:
            if self._remote_mode():
                home = self.ctx.remote.remote_home or "~"
                return [("主目录", home), ("/", "/"), ("/tmp", "/tmp"),
                        ("/home", "/home"), ("/opt", "/opt")]
            home = os.path.expanduser("~")
            out = [("主目录", home)]
            for label, sub in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents")):
                p = os.path.join(home, sub)
                if os.path.isdir(p):
                    out.append((label, p))
            if os.name == "nt":
                drive = os.environ.get("SystemDrive", "C:") + "\\"
                out.append((drive, drive))
            else:
                out.append(("/", "/"))
            return out
        return [("/", "/"), ("/data/local/tmp", "/data/local/tmp"), ("/sdcard", "/sdcard"),
                ("/tmp", "/tmp"), ("/root", "/root"), ("/oem", "/oem")]

    def _fill_quick(self):
        while self.quick_row.count():
            it = self.quick_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for name, path in self._places():
            b = QPushButton(name)
            b.setObjectName("Flat")
            b.setFixedHeight(22)
            b.clicked.connect(lambda _c=False, p=path: self.goto(p))
            self.quick_row.addWidget(b)
        self.quick_row.addStretch(1)

    # ---- 跳转 ----------------------------------------------------------
    def goto(self, path: str, push: bool = True):
        path = (path or "").strip()
        if not path:
            return
        if self.is_local:
            if self._remote_mode():
                self._load_remote_ubuntu(path, push)
            else:
                self._load_local(path, push)
        else:
            self._load_device(path, push)

    def refresh(self):
        self.goto(self.cwd, push=False)

    def go_up(self):
        if self.is_local and not self._remote_mode():
            parent = os.path.dirname(self.cwd.rstrip("/\\"))
            self.goto(parent or self.cwd)
        else:
            parent = os.path.dirname(self.cwd.rstrip("/")) or "/"
            self.goto(parent)

    def go_back(self):
        if self.hist_idx > 0:
            j = self.hist_idx - 1
            while j >= 0 and self.history[j] == self.cwd:
                j -= 1
            if j >= 0:
                self.hist_idx = j
                self._jump(self.history[j])

    def go_forward(self):
        j = self.hist_idx + 1
        while j < len(self.history) and self.history[j] == self.cwd:
            j += 1
        if j < len(self.history):
            self.hist_idx = j
            self._jump(self.history[j])

    def _jump(self, path):
        if self.is_local:
            if self._remote_mode():
                self._load_remote_ubuntu(path, push=False)
            else:
                self._load_local(path, push=False)
        else:
            self._load_device(path, push=False)

    def _push_history(self, real, push=True):
        self.cwd = real
        self.ed_path.setText(real)
        if push and (self.history[self.hist_idx:self.hist_idx + 1] != [real]):
            self.history = self.history[: self.hist_idx + 1] + [real]
            self.hist_idx = len(self.history) - 1

    # ---- 加载：本地 ----------------------------------------------------
    def _load_local(self, path, push=True):
        real = os.path.abspath(os.path.expanduser(path))

        def work():
            entries = []
            with os.scandir(real) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir()
                        size = None if is_dir else e.stat().st_size
                        mtime = time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(e.stat().st_mtime))
                        entries.append((e.name, is_dir, size, mtime))
                    except OSError:
                        continue
            entries.sort(key=lambda t: (0 if t[1] else 1, t[0].lower()))
            return real, entries

        def done(res, err):
            if err:
                self.ctx.log("打开本机目录失败 %s: %s" % (real, err), "error")
                return
            real2, entries = res
            self._push_history(real2, push)
            self._set_rows([(n, d, ab.human_size(s) if s is not None else "", extra)
                            for (n, d, s, extra) in entries])

        self.ctx.run_async(work, done)

    # ---- 加载：远程模式下的 Ubuntu 目录 --------------------------------
    def _load_remote_ubuntu(self, path, push=True):
        sess = self.ctx.remote
        if sess is None:
            return
        p = path
        if not (p.startswith("/") or p.startswith("~")):
            p = sess.remote_home or "~"

        def work():
            real = sess.realpath(p)
            ok, entries, err = sess.list_dir(real)
            return real, ok, entries, err

        def done(res, err):
            if err:
                self.ctx.log("打开 Ubuntu 目录失败: %s" % err, "error")
                return
            real, ok, entries, msg = res
            if self.ctx.remote is not sess:
                return
            if not ok:
                self.ctx.log("无法打开 Ubuntu 目录 %s: %s" % (real, msg), "error")
                return
            self._push_history(real, push)
            self._set_rows([(n, d, ab.human_size(s) if s is not None else "", mt)
                            for (n, d, s, mt) in entries])

        self.ctx.run_async(work, done)

    # ---- 加载：设备侧 --------------------------------------------------
    def _load_device(self, path, push=True):
        if self.ctx.current is None:
            self._set_rows([])
            return
        real = path or "/"

        def work():
            ok, out = self.ctx.adb_ls(real)
            if not ok:
                return real, False, [], (out or "").strip()[:300]
            entries = ab.parse_remote_listing(out)
            return real, True, entries, ""

        def done(res, err):
            if err:
                self.ctx.log("列设备目录失败: %s" % err, "error")
                return
            real, ok, entries, msg = res
            if not ok:
                self.ctx.log("无法打开设备目录 %s: %s" % (real, msg), "error")
                return
            self.ctx.cwd = real
            self._push_history(real, push)
            rows = []
            for (n, d, s, perm, _u, _g) in entries:
                rows.append((n, d, "<DIR>" if d else ab.human_size(s), perm))
            self._set_rows(rows)

        self.ctx.run_async(work, done)

    # ---- 填充表格 ------------------------------------------------------
    def _set_rows(self, rows):
        self.model.removeRows(0, self.model.rowCount())
        icon_dir = theme.load_icon("folder", theme.COLORS["accent_light"], 16)
        icon_file = theme.load_icon("file", theme.COLORS["text_dim"], 16)
        for name, is_dir, size, extra in rows:
            items = [
                QStandardItem(("%s/" % name) if is_dir else name),
                QStandardItem(str(size or "")),
                QStandardItem(str(extra or "")),
            ]
            items[0].setIcon(icon_dir if is_dir else icon_file)
            items[0].setData((name, is_dir), Qt.UserRole)
            items[1].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for it in items:
                if is_dir:
                    it.setForeground(theme.qcolor("accent_light"))
            self.model.appendRow(items)
        self.tree.sortByColumn(0, Qt.AscendingOrder)

    def selected_items(self):
        """[(name, is_dir)]"""
        out = []
        seen = set()
        for idx in self.tree.selectionModel().selectedRows() if self.tree.selectionModel() else []:
            data = self.model.item(idx.row(), 0).data(Qt.UserRole)
            if data and data[0] not in seen:
                seen.add(data[0])
                out.append(data)
        return out

    def abs_path(self, name: str) -> str:
        if self.is_local and not self._remote_mode():
            return os.path.join(self.cwd, name)
        n = name.rstrip("/")
        if n.startswith("/"):
            return n
        base = self.cwd.rstrip("/")
        return (base + "/" + n) if base else ("/" + n)

    # ---- 打开 ----------------------------------------------------------
    def _open_index(self, index):
        data = self.model.item(index.row(), 0).data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if is_dir:
            self.goto(self.abs_path(name))
        elif not self.is_local:
            self.view_file()

    # ---- 右键菜单 ------------------------------------------------------
    def _menu(self, pos: QPoint):
        m = QMenu(self.tree)
        sel = self.selected_items()
        if self.is_local:
            m.addAction("打开", self._open_index_action)
            m.addSeparator()
            m.addAction("上传到设备", self.upload_selected)
        else:
            m.addAction("打开 / 查看", self.view_file)
            m.addAction("下载到本机", self.download_selected)
        m.addSeparator()
        m.addAction("新建文件夹", self.mkdir)
        m.addAction("重命名", self.rename)
        m.addAction("删除", self.delete)
        m.addSeparator()
        m.addAction("刷新", self.refresh)
        if sel:
            m.addSeparator()
            m.addAction("复制名称", lambda: self._copy_names(sel))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_index_action(self):
        sel = self.selected_items()
        if sel:
            name, is_dir = sel[0]
            if is_dir:
                self.goto(self.abs_path(name))

    def _copy_names(self, sel):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText("\n".join(n for n, _ in sel))

    # ---- 原生拖拽 ------------------------------------------------------
    def _start_drag(self, supported_actions):
        sel = self.selected_items()
        if not sel:
            return
        drag = QDrag(self.tree)
        mime = QMimeData()
        mime.setData(MIME_PANE, ("%s|%s" % (self.side, "\n".join(n for n, _ in sel))).encode("utf-8"))
        # 若拖到系统资源管理器（仅本地模式有意义）
        if self.is_local and not self._remote_mode():
            mime.setUrls([QUrl.fromLocalFile(self.abs_path(n)) for n, _ in sel])
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def _drag_enter(self, e):
        if e.mimeData().hasFormat(MIME_PANE) or e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def _drop(self, e):
        page = self.window().findChild(FilePage)
        md = e.mimeData()
        if md.hasFormat(MIME_PANE) and page is not None:
            raw = bytes(md.data(MIME_PANE)).decode("utf-8")
            src_side, _, names = raw.partition("|")
            items = [n for n in names.split("\n") if n]
            if src_side == self.side:
                e.ignore()
                return
            e.acceptProposedAction()
            if src_side == "local":
                page.upload_items(items, self.cwd)
            else:
                page.download_items(items, self.cwd)
            return
        if md.hasUrls():
            e.acceptProposedAction()
            if self.is_local:
                return                       # 拖进本机侧无意义
            # 从系统拖入文件 -> 上传到设备当前目录
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if page is not None and paths:
                page.upload_local_paths(paths, self.cwd)
            return
        e.ignore()

    # ---- 操作 ----------------------------------------------------------
    def mkdir(self):
        name, ok = QInputDialog.getText(self, "新建文件夹", "名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        page = self.window().findChild(FilePage)
        if self.is_local:
            if self._remote_mode():
                self.ctx.remote.mkdir(self.abs_path(name))
                self.refresh()
            else:
                try:
                    os.makedirs(self.abs_path(name), exist_ok=True)
                except OSError as e:
                    QMessageBox.critical(self, "新建失败", str(e))
                self.refresh()
        else:
            def work():
                return self.ctx.adb_shell("mkdir -p %s && echo OK" % ab.quote(self.abs_path(name)))

            def done(res, err):
                self.ctx.log("新建目录 %s: %s" % (self.abs_path(name), res if not err else err))
                self.refresh()

            self.ctx.run_async(work, done)

    def rename(self):
        sel = self.selected_items()
        if len(sel) != 1:
            QMessageBox.information(self, "重命名", "请选择一个文件或文件夹")
            return
        old, _ = sel[0]
        new, ok = QInputDialog.getText(self, "重命名", "新名称：", text=old)
        if not ok or not new.strip() or new == old:
            return
        new = new.strip()
        if self.is_local:
            try:
                os.rename(self.abs_path(old), self.abs_path(new))
            except OSError as e:
                QMessageBox.critical(self, "重命名失败", str(e))
            self.refresh()
        else:
            def work():
                return self.ctx.adb_shell("mv %s %s && echo OK"
                                          % (ab.quote(self.abs_path(old)), ab.quote(self.abs_path(new))))

            def done(res, err):
                self.refresh()

            self.ctx.run_async(work, done)

    def delete(self):
        sel = self.selected_items()
        if not sel:
            return
        target = "Ubuntu" if self._remote_mode() else ("本机" if self.is_local else "设备")
        if QMessageBox.question(self, "删除",
                                "确认从%s删除以下 %d 项？\n\n%s"
                                % (target, len(sel), "\n".join(n for n, _ in sel[:20]))) \
                != QMessageBox.Yes:
            return
        if self.is_local:
            if self._remote_mode():
                self.ctx.remote.delete([self.abs_path(n) for n, _ in sel])
                self.refresh()
                return
            import shutil
            for n, is_dir in sel:
                p = self.abs_path(n)
                try:
                    if is_dir:
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                except OSError as e:
                    QMessageBox.critical(self, "删除失败", "%s: %s" % (n, e))
            self.refresh()
        else:
            names = " ".join(ab.quote(self.abs_path(n)) for n, _ in sel)

            def work():
                return self.ctx.adb_shell("rm -rf %s && echo OK" % names, timeout=120)

            def done(res, err):
                self.refresh()

            self.ctx.run_async(work, done)

    def view_file(self):
        sel = self.selected_items()
        if len(sel) != 1 or sel[0][1]:
            QMessageBox.information(self, "查看", "请选择一个文件")
            return
        path = self.abs_path(sel[0][0])

        def work():
            if self.ctx.remote is not None:
                rc, out = self.ctx.remote.adb(["shell", "cat %s" % ab.quote(path)], timeout=90)
                return rc == 0, out.encode("utf-8", "replace"), out
            return self.ctx.adb.read_file_bytes(path)

        def done(res, err):
            if err:
                QMessageBox.critical(self, "查看失败", str(err))
                return
            ok, data, msg = res
            if not ok:
                QMessageBox.warning(self, "查看失败", str(msg)[:600])
                return
            from ui.text_viewer import TextViewerDialog
            dlg = TextViewerDialog(path, data.decode("utf-8", "replace"), self)
            dlg.exec()

        self.ctx.run_async(work, done, busy_text="读取文件…")

    # ---- 与 FilePage 的转发 --------------------------------------------
    def upload_selected(self):
        page = self.window().findChild(FilePage)
        if page is not None:
            page.upload_items([n for n, _ in self.selected_items()], page.pane_remote.cwd)

    def download_selected(self):
        page = self.window().findChild(FilePage)
        if page is not None:
            page.download_items([n for n, _ in self.selected_items()], page.pane_local.cwd)


class FilePage(QWidget):
    """双栏文件管理页。"""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        self.split = FlatSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.pane_local = FilePane(ctx, "local")
        self.pane_remote = FilePane(ctx, "remote")
        self.split.addWidget(self.pane_local)
        self.split.addWidget(self.pane_remote)
        self.split.setSizes([560, 560])
        lay.addWidget(self.split, 1)

        # 中间工具栏
        bar = QHBoxLayout()
        bar.setSpacing(6)
        lbl = QLabel("拖拽文件/文件夹到另一侧即可传输，或使用按钮：")
        lbl.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        bar.addWidget(lbl)

        b_up = QPushButton("上传 →")
        b_up.setIcon(theme.load_icon("up"))
        b_up.clicked.connect(self._upload_clicked)
        bar.addWidget(b_up)

        b_down = QPushButton("← 下载")
        b_down.setIcon(theme.load_icon("down"))
        b_down.clicked.connect(self._download_clicked)
        bar.addWidget(b_down)

        b_updir = QPushButton("上传文件夹 →")
        b_updir.setObjectName("Secondary")
        b_updir.clicked.connect(self._upload_dir_clicked)
        bar.addWidget(b_updir)

        bar.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        bar.addWidget(self.progress)

        self.lbl_task = QLabel("")
        self.lbl_task.setStyleSheet("color:%s;" % theme.COLORS["text_dim"])
        self.lbl_task.setMinimumWidth(120)
        bar.addWidget(self.lbl_task)
        lay.addLayout(bar)

        ctx.device_selected.connect(lambda _d: self.refresh_remote())
        ctx.remote_changed.connect(lambda _x: self.on_remote_changed())

        self.on_remote_changed()

    # ---- 外部通知 ------------------------------------------------------
    def on_remote_changed(self):
        self.pane_local._fill_quick()
        self.pane_local._update_title()
        self.pane_remote._update_title()
        if self.ctx.is_remote and self.ctx.remote:
            home = self.ctx.remote.remote_home or "~"
            self.pane_local.history = [home]
            self.pane_local.hist_idx = 0
            self.pane_local.goto(home)
            self.refresh_remote()
        else:
            home = os.path.expanduser("~")
            self.pane_local.history = [home]
            self.pane_local.hist_idx = 0
            self.pane_local.goto(home)
            self.refresh_remote()

    def refresh_remote(self):
        self.pane_remote._update_title()
        if self.ctx.current is None:
            self.pane_remote._set_rows([])
            return
        self.pane_remote.goto(self.ctx.cwd or "/data/local/tmp", push=False)

    # ---- 按钮 ----------------------------------------------------------
    def _upload_clicked(self):
        sel = self.pane_local.selected_items()
        if not sel:
            QMessageBox.information(self, "上传", "请先在左侧选择要上传的文件或文件夹")
            return
        self.upload_items([n for n, _ in sel], self.pane_remote.cwd)

    def _download_clicked(self):
        sel = self.pane_remote.selected_items()
        if not sel:
            QMessageBox.information(self, "下载", "请先在右侧选择要下载的文件")
            return
        self.download_items([n for n, _ in sel], self.pane_local.cwd)

    def _upload_dir_clicked(self):
        d = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if d:
            self.upload_local_paths([d], self.pane_remote.cwd)

    # ---- 传输实现 ------------------------------------------------------
    def _set_progress(self, val: int, text: str = ""):
        self.progress.setValue(max(0, min(100, val)))
        if text:
            self.lbl_task.setText(text)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

    def upload_local_paths(self, paths, dest_dir):
        """从系统路径上传（仅本地模式，或远程模式经 SFTP 中转）。"""
        if self.ctx.current is None:
            QMessageBox.warning(self, "上传", "请先连接设备")
            return
        if self.ctx.is_remote:
            self._upload_via_sftp(paths, dest_dir)
            return

        names = ", ".join(os.path.basename(p) for p in paths)
        self.ctx.log("上传 %s -> %s" % (names, dest_dir))

        def work():
            proc = self.ctx.adb.popen(["push"] + list(paths) + [dest_dir])
            last = []
            buf = b""
            try:
                while True:
                    chunk = proc.stdout.read(512) if proc.stdout else b""
                    if not chunk:
                        break
                    buf += chunk
                    while b"\r" in buf or b"\n" in buf:
                        sep = b"\r" if b"\r" in buf else b"\n"
                        line, buf = buf.split(sep, 1)
                        text = line.decode("utf-8", "replace")
                        pr = ab.parse_progress(text)
                        if pr:
                            last.append(text)
            finally:
                proc.wait()
            return proc.returncode, "\n".join(last[-5:])

        def done(res, err):
            if err:
                QMessageBox.critical(self, "上传失败", str(err))
                self._set_progress(0)
                return
            rc, out = res
            self._set_progress(100 if rc == 0 else 0, "上传完成" if rc == 0 else "上传失败")
            self.ctx.log("上传完成 rc=%s" % rc, "ok" if rc == 0 else "error")
            self.refresh_remote()
            if rc != 0:
                QMessageBox.critical(self, "上传失败", out[-1200:])

        self._set_progress(0, "上传中…")
        self.ctx.run_async(work, done)

    def _upload_via_sftp(self, paths, dest_dir):
        sess = self.ctx.remote

        def work():
            stage = "/tmp/adbhost_stage_%d" % int(time.time() * 1000)
            sess.exec("mkdir -p %s" % stage, timeout=20)
            for p in paths:
                ok, msg = sess.upload_file(p, stage)
                if not ok:
                    sess.exec("rm -rf %s" % stage)
                    return False, msg
            rc, out = sess.adb(["push"] + [stage + "/" + os.path.basename(p.rstrip("/\\"))
                                           for p in paths] + [dest_dir], timeout=1800)
            sess.exec("rm -rf %s" % stage, timeout=20)
            return rc == 0, out

        def done(res, err):
            if err:
                QMessageBox.critical(self, "上传失败", str(err))
                return
            ok, out = res
            self._set_progress(100 if ok else 0, "上传完成" if ok else "上传失败")
            self.ctx.log("上传(经 SSH) %s" % ("成功" if ok else "失败: " + (out or "")[-300:]),
                         "ok" if ok else "error")
            self.refresh_remote()

        self._set_progress(0, "上传中…")
        self.ctx.run_async(work, done)

    def upload_items(self, names, dest_dir):
        """左侧栏里的项目 -> 设备。"""
        if not names:
            return
        if self.ctx.current is None:
            QMessageBox.warning(self, "上传", "请先连接设备")
            return
        if self.ctx.is_remote:
            # 远程模式：左侧就是 Ubuntu 路径，直接在远端 adb push
            paths = [self.pane_local.abs_path(n) for n in names]
            sess = self.ctx.remote

            def work():
                return sess.adb(["push"] + paths + [dest_dir], timeout=1800)

            def done(res, err):
                if err:
                    QMessageBox.critical(self, "上传失败", str(err))
                    return
                rc, out = res
                self._set_progress(100 if rc == 0 else 0, "上传完成" if rc == 0 else "上传失败")
                self.ctx.log("上传(经 SSH) %s -> %s %s"
                             % (", ".join(names), dest_dir, "成功" if rc == 0 else "失败"),
                             "ok" if rc == 0 else "error")
                self.refresh_remote()

            self._set_progress(0, "上传中…")
            self.ctx.run_async(work, done)
            return
        self.upload_local_paths([self.pane_local.abs_path(n) for n in names], dest_dir)

    def download_items(self, names, dest_dir):
        """设备 -> 左侧栏目录。"""
        if not names:
            return
        if self.ctx.current is None:
            return
        remotes = [self.pane_remote.abs_path(n) for n in names]
        sess = self.ctx.remote

        if sess is not None:
            def work():
                stage = "/tmp/adbhost_stage_%d" % int(time.time() * 1000)
                sess.exec("mkdir -p %s" % stage, timeout=20)
                rc, out = sess.adb(["pull"] + remotes + [stage], timeout=1800)
                if rc != 0:
                    sess.exec("rm -rf %s" % stage, timeout=20)
                    return False, out
                q = " ".join(ab.quote(stage + "/" + os.path.basename(r.rstrip("/"))) for r in remotes)
                cprc, cpout = sess.exec("cp -a %s %s/ && echo OK" % (q, ab.quote(dest_dir)),
                                        timeout=600)
                sess.exec("rm -rf %s" % stage, timeout=20)
                return (cprc == 0 and "OK" in (cpout or "")), (out + cpout)

            def done(res, err):
                if err:
                    QMessageBox.critical(self, "下载失败", str(err))
                    return
                ok, out = res
                self._set_progress(100 if ok else 0, "下载完成" if ok else "下载失败")
                self.ctx.log("下载(经 SSH) %s %s" % (", ".join(names), "成功" if ok else "失败"),
                             "ok" if ok else "error")
                self.refresh_remote()
                if self.pane_local.cwd:
                    self.pane_local.refresh()

            self._set_progress(0, "下载中…")
            self.ctx.run_async(work, done)
            return

        # 本地模式：adb pull 到 Windows 目录
        def work():
            proc = self.ctx.adb.popen(["pull"] + remotes + [dest_dir])
            buf = b""
            try:
                while True:
                    chunk = proc.stdout.read(512) if proc.stdout else b""
                    if not chunk:
                        break
                    buf += chunk
            finally:
                proc.wait()
            return proc.returncode, buf.decode("utf-8", "replace")

        def done(res, err):
            if err:
                QMessageBox.critical(self, "下载失败", str(err))
                return
            rc, out = res
            self._set_progress(100 if rc == 0 else 0, "下载完成" if rc == 0 else "下载失败")
            self.ctx.log("下载 %s -> %s %s"
                         % (", ".join(names), dest_dir, "成功" if rc == 0 else "失败"),
                         "ok" if rc == 0 else "error")
            self.pane_local.refresh()
            if rc != 0:
                QMessageBox.critical(self, "下载失败", out[-1200:])

        self._set_progress(0, "下载中…")
        self.ctx.run_async(work, done)
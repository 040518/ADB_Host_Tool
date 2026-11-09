# -*- coding: utf-8 -*-
"""左右两栏独立选择 Windows / SSH / ADB，支持文件与目录互传。"""

from __future__ import annotations

import os
import time
import json
import uuid

from PySide6.QtCore import (QDir, QMimeData, QModelIndex, QPoint, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import QAction, QDrag, QFont, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSplitter, QToolButton, QTreeView,
                               QVBoxLayout, QWidget)

from core import adb_backend as ab
from core.file_transfer import endpoint_for, transfer_paths
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
        self.endpoint_kind = "windows" if side == "local" else "adb"
        self._load_generation = 0
        self._locations = {}
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
        self.cmb_source = QComboBox()
        self.cmb_source.addItem("Windows 本机", "windows")
        self.cmb_source.addItem("Ubuntu / SSH 目标", "ssh")
        self.cmb_source.addItem("ADB 设备", "adb")
        self.cmb_source.setCurrentIndex(self.cmb_source.findData(self.endpoint_kind))
        self.cmb_source.currentIndexChanged.connect(
            lambda: self.set_endpoint(self.cmb_source.currentData()))
        lay.addWidget(self.cmb_source)

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
        return self.endpoint_kind == "ssh"

    def endpoint(self):
        return endpoint_for(self.ctx, self.endpoint_kind)

    def set_endpoint(self, kind):
        self._locations[self.endpoint_kind] = (self.cwd, list(self.history), self.hist_idx)
        self.endpoint_kind = kind
        self.is_local = kind != "adb"
        self.cmb_source.blockSignals(True)
        self.cmb_source.setCurrentIndex(self.cmb_source.findData(kind))
        self.cmb_source.blockSignals(False)
        home = (os.path.expanduser("~") if kind == "windows" else
                ((getattr(self.ctx.remote, "remote_home", "") or "~") if kind == "ssh"
                 else (self.ctx.cwd or "/data/local/tmp")))
        self.cwd, self.history, self.hist_idx = self._locations.get(kind, (home, [home], 0))
        self._load_generation += 1
        self.ed_path.setText(self.cwd)
        self._set_rows([])
        self._update_title()
        self._fill_quick()
        self.refresh()

    def _update_title(self):
        if self.is_local:
            if self._remote_mode():
                host = self.ctx.remote.host if self.ctx.remote else "未连接"
                self.lbl_title.setText("SSH 目标 · %s" % host)
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
                home = getattr(self.ctx.remote, "remote_home", "") or "~"
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
                w.hide()
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
        self._load_generation += 1
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
        self.goto(path, push=False)

    def _push_history(self, real, push=True):
        self.cwd = real
        self.ed_path.setText(real)
        if push and (self.history[self.hist_idx:self.hist_idx + 1] != [real]):
            self.history = self.history[: self.hist_idx + 1] + [real]
            self.hist_idx = len(self.history) - 1

    # ---- 加载：本地 ----------------------------------------------------
    def _load_local(self, path, push=True):
        real = os.path.abspath(os.path.expanduser(path))
        generation = self._load_generation

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
            if generation != self._load_generation:
                return
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
            self._set_rows([])
            self.lbl_title.setText("SSH 目标 · 请先连接 SSH")
            return
        generation = self._load_generation
        p = path
        if not (p.startswith("/") or p.startswith("~")):
            p = sess.remote_home or "~"

        def work():
            real = sess.realpath(p)
            ok, entries, err = sess.list_dir(real)
            return real, ok, entries, err

        def done(res, err):
            if generation != self._load_generation or self.ctx.remote is not sess:
                return
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
        generation = self._load_generation
        endpoint = self.endpoint()

        def work():
            out = endpoint.command("ls -la %s" % ab.quote(real), timeout=40)
            entries = ab.parse_remote_listing(out)
            return real, True, entries, ""

        def done(res, err):
            if generation != self._load_generation or endpoint.identity != self.endpoint().identity:
                return
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
        else:
            self.view_file()

    # ---- 右键菜单 ------------------------------------------------------
    def _menu(self, pos: QPoint):
        m = QMenu(self.tree)
        sel = self.selected_items()
        m.addAction("打开文件夹", self._open_index_action)
        m.addAction("查看文本", self.view_file)
        m.addAction("复制到另一侧", self.upload_selected)
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
        page = self._page()
        mime.setData(MIME_PANE, json.dumps({
            "page": page.drag_id if page else "", "side": self.side,
            "endpoint": list(self.endpoint().identity),
            "paths": [self.abs_path(n) for n, _ in sel]}, ensure_ascii=False).encode("utf-8"))
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
        page = self._page()
        md = e.mimeData()
        if md.hasFormat(MIME_PANE) and page is not None:
            try:
                payload = json.loads(bytes(md.data(MIME_PANE)).decode("utf-8"))
                src = page.pane_local if payload["side"] == "local" else page.pane_remote
                valid = (payload["page"] == page.drag_id and src is not self and
                         payload["endpoint"] == list(src.endpoint().identity))
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                e.ignore()
                return
            e.acceptProposedAction()
            page.copy_paths(src.endpoint(), self.endpoint(), payload["paths"], self.cwd)
            return
        if md.hasUrls():
            e.acceptProposedAction()
            # 从系统拖入文件 -> 上传到设备当前目录
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if page is not None and paths:
                page.copy_paths(endpoint_for(self.ctx, "windows"), self.endpoint(), paths, self.cwd)
            return
        e.ignore()

    # ---- 操作 ----------------------------------------------------------
    def mkdir(self):
        name, ok = QInputDialog.getText(self, "新建文件夹", "名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not self._valid_name(name):
            return
        endpoint, path = self.endpoint(), self.abs_path(name)
        self._operation(lambda: endpoint.mkdir(path))
        return

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
        if not self._valid_name(new):
            return
        endpoint, old_path, new_path = self.endpoint(), self.abs_path(old), self.abs_path(new)
        self._operation(lambda: endpoint.rename(old_path, new_path))
        return

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
        endpoint, paths = self.endpoint(), [self.abs_path(n) for n, _ in sel]
        self._operation(lambda: endpoint.delete(paths))
        return

    def view_file(self):
        sel = self.selected_items()
        if len(sel) != 1 or sel[0][1]:
            QMessageBox.information(self, "查看", "请选择一个文件")
            return
        path = self.abs_path(sel[0][0])
        endpoint = self.endpoint()

        def work():
            return True, endpoint.read_bytes(path), ""

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
        page = self._page()
        if page is not None:
            page.copy_selected(self, page.other_pane(self))

    def download_selected(self):
        self.upload_selected()

    def _page(self):
        parent = self.parent()
        while parent is not None and not isinstance(parent, FilePage):
            parent = parent.parent()
        return parent

    def _valid_name(self, name):
        valid = name not in (".", "..") and not any(c in name for c in ("/", "\\", "\x00"))
        if not valid:
            QMessageBox.warning(self, "名称无效", "请输入单个文件或文件夹名称，不包含路径分隔符")
        return valid

    def _operation(self, fn):
        def done(_res, err):
            if err:
                QMessageBox.warning(self, "文件操作失败", str(err))
            self.refresh()
        self.ctx.run_async(fn, done, busy_text="文件操作…")


class FilePage(QWidget):
    """左右两栏可独立选择 Windows、SSH 或 ADB。"""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.drag_id = uuid.uuid4().hex
        self._transferring = False
        self._remote_identity = id(ctx.remote)
        self._device_identity = ctx.target_identity("adb")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        hint = QLabel("左右两栏可分别选择 Windows、Ubuntu / SSH 或 ADB；选中文件后跨栏拖动即可复制。")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.split = FlatSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.pane_local = FilePane(ctx, "local")
        self.pane_remote = FilePane(ctx, "remote")
        self.split.addWidget(self.pane_local)
        self.split.addWidget(self.pane_remote)
        self.split.setSizes([560, 560])
        lay.addWidget(self.split, 1)
        bar = QHBoxLayout()
        self.btn_right = QPushButton("复制到右侧 →")
        self.btn_right.clicked.connect(self._upload_clicked)
        bar.addWidget(self.btn_right)
        self.btn_left = QPushButton("← 复制到左侧")
        self.btn_left.clicked.connect(self._download_clicked)
        bar.addWidget(self.btn_left)
        self.btn_import = QPushButton("从 Windows 选择文件…")
        self.btn_import.clicked.connect(self._import_files)
        bar.addWidget(self.btn_import)
        self.btn_import_dir = QPushButton("选择文件夹…")
        self.btn_import_dir.clicked.connect(self._upload_dir_clicked)
        bar.addWidget(self.btn_import_dir)
        bar.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        bar.addWidget(self.progress)
        self.lbl_task = QLabel("")
        bar.addWidget(self.lbl_task)
        lay.addLayout(bar)
        ctx.device_selected.connect(lambda _d: self.refresh_remote())
        ctx.remote_changed.connect(lambda _x: self.on_remote_changed())
        if ctx.remote is not None and ctx.current is None:
            self.pane_remote.set_endpoint("ssh")
        self.on_remote_changed()

    def other_pane(self, pane):
        return self.pane_remote if pane is self.pane_local else self.pane_local

    def on_remote_changed(self):
        changed = self._remote_identity != id(self.ctx.remote)
        self._remote_identity = id(self.ctx.remote)
        for pane in (self.pane_local, self.pane_remote):
            if changed:
                pane._locations.pop("ssh", None)
                if pane.endpoint_kind == "ssh":
                    pane.cwd = getattr(self.ctx.remote, "remote_home", "") or "~"
                    pane.history, pane.hist_idx = [pane.cwd], 0
            pane._update_title()
            pane._fill_quick()
            pane.refresh()
        if self.ctx.remote is not None and self.ctx.current is None and self.pane_remote.endpoint_kind == "adb":
            self.pane_remote.set_endpoint("ssh")

    def refresh_remote(self):
        # ADB 自动刷新不应打断 Windows/SSH 目录浏览或使异步结果失效。
        identity = self.ctx.target_identity("adb")
        if identity == self._device_identity:
            return
        self._device_identity = identity
        for pane in (self.pane_local, self.pane_remote):
            if pane.endpoint_kind == "adb":
                pane._update_title()
                pane.refresh()

    def copy_selected(self, source, destination):
        paths = [source.abs_path(n) for n, _ in source.selected_items()]
        if not paths:
            QMessageBox.information(self, "传输", "请先选择要复制的文件或文件夹")
            return
        self.copy_paths(source.endpoint(), destination.endpoint(), paths, destination.cwd)

    def _upload_clicked(self):
        self.copy_selected(self.pane_local, self.pane_remote)

    def _download_clicked(self):
        self.copy_selected(self.pane_remote, self.pane_local)

    def _import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 Windows 文件，复制到右侧")
        if paths:
            self.upload_local_paths(paths, self.pane_remote.cwd)

    def _upload_dir_clicked(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Windows 文件夹，复制到右侧")
        if path:
            self.upload_local_paths([path], self.pane_remote.cwd)

    def _set_progress(self, value, text=""):
        self.progress.setRange(0, 100)
        self.progress.setValue(value)
        self.lbl_task.setText(text)

    def copy_paths(self, source, destination, paths, destination_dir):
        if self._transferring:
            self.ctx.log("当前传输尚未完成，请稍后再试", "warn")
            return
        try:
            source.require()
            destination.require()
        except Exception as exc:
            QMessageBox.warning(self, "传输", str(exc))
            return
        self._transferring = True
        for button in (self.btn_left, self.btn_right, self.btn_import, self.btn_import_dir):
            button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.lbl_task.setText("正在复制…")
        self.ctx.log("文件传输 %s → %s：%s" % (source.kind, destination.kind, destination_dir))
        frozen_paths = list(paths)

        def work():
            return transfer_paths(source, destination, frozen_paths, destination_dir)

        def done(names, err):
            self._transferring = False
            for button in (self.btn_left, self.btn_right, self.btn_import, self.btn_import_dir):
                button.setEnabled(True)
            self._set_progress(0 if err else 100, "传输失败" if err else "完成 %d 项" % len(names))
            if err:
                self.ctx.log("文件传输失败：%s" % err, "error")
                QMessageBox.warning(self, "传输失败", str(err))
            else:
                self.ctx.log("文件传输完成：%s" % ", ".join(names), "ok")
            for pane in (self.pane_local, self.pane_remote):
                if pane.endpoint().identity == destination.identity:
                    pane.refresh()

        self.ctx.run_async(work, done)

    # 保留外部调用入口，目标始终取对应栏当前选择的文件系统。
    def upload_local_paths(self, paths, dest_dir):
        self.copy_paths(endpoint_for(self.ctx, "windows"), self.pane_remote.endpoint(), paths, dest_dir)

    def upload_items(self, names, dest_dir):
        self.copy_paths(self.pane_local.endpoint(), self.pane_remote.endpoint(),
                        [self.pane_local.abs_path(n) for n in names], dest_dir)

    def download_items(self, names, dest_dir):
        self.copy_paths(self.pane_remote.endpoint(), self.pane_local.endpoint(),
                        [self.pane_remote.abs_path(n) for n in names], dest_dir)

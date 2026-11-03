# -*- coding: utf-8 -*-
"""SSH 连接对话框：配置保存、下拉选择、连接/删除。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from core import ssh_backend
from ui import theme
from ui.context import AppContext


def _profile_key(profile):
    try:
        port = int(profile.get("port", 22) or 22)
    except (TypeError, ValueError):
        port = 22
    return (str(profile.get("user", "")).strip().lower(),
            str(profile.get("host", "")).strip().lower(), port)


def _merge_profiles(profiles):
    """按 user/host/port 合并重复历史，并优先保留最近的非空密码。"""
    merged = []
    positions = {}
    for raw in profiles or []:
        if not isinstance(raw, dict):
            continue
        profile = dict(raw)
        key = _profile_key(profile)
        if not key[0] or not key[1]:
            continue
        if key not in positions:
            positions[key] = len(merged)
            merged.append(profile)
            continue
        index = positions[key]
        current = dict(merged[index])
        for field in ("name", "host", "port", "user", "adb"):
            value = profile.get(field)
            if value not in (None, ""):
                current[field] = value
        if profile.get("password"):
            current["password"] = profile["password"]
        merged[index] = current
    return merged


class SshDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._profile = None
        self.setWindowTitle("SSH 连接到 Ubuntu")
        self.setMinimumWidth(560)
        self.setModal(True)

        cfg = ssh_backend.load_config()
        raw_profiles = list(cfg.get("profiles") or [])
        try:
            raw_last = max(0, min(int(cfg.get("last", 0)), len(raw_profiles) - 1))
        except (TypeError, ValueError):
            raw_last = 0
        last_key = _profile_key(raw_profiles[raw_last]) if raw_profiles else None
        self.profiles = _merge_profiles(raw_profiles)
        self._last_index = next(
            (i for i, p in enumerate(self.profiles) if _profile_key(p) == last_key),
            0)
        if self.profiles != raw_profiles or cfg.get("last") != self._last_index:
            ssh_backend.save_config({"last": self._last_index,
                                     "profiles": self.profiles})

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(10)

        head = QLabel("在 Windows 上操作插在 Ubuntu 上的开发板：\n"
                      "连接后，设备列表、文件管理、终端全部走 Ubuntu 上的 adb。")
        head.setObjectName("Hint")
        head.setWordWrap(True)
        lay.addWidget(head)

        # 已保存配置
        row = QHBoxLayout()
        row.addWidget(QLabel("已保存"))
        self.cmb_saved = QComboBox()
        self.cmb_saved.setMinimumWidth(340)
        row.addWidget(self.cmb_saved, 1)
        lay.addLayout(row)
        self.lbl_saved = QLabel("")
        self.lbl_saved.setObjectName("Hint")
        lay.addWidget(self.lbl_saved)

        form = QFormLayout()
        form.setSpacing(8)
        self.ed_name = QLineEdit("Ubuntu")
        self.ed_host = QLineEdit("")
        self.ed_host.setPlaceholderText("例如 192.168.1.10 或 ubuntu.local")
        self.ed_port = QLineEdit("22")
        self.ed_user = QLineEdit("")
        self.ed_user.setPlaceholderText("SSH 用户名")
        self.ed_pass = QLineEdit("")
        self.ed_pass.setEchoMode(QLineEdit.Password)
        self.ed_adb = QLineEdit("")

        form.addRow("名称", self.ed_name)
        form.addRow("IP / 主机", self.ed_host)
        form.addRow("端口", self.ed_port)
        form.addRow("用户名", self.ed_user)
        form.addRow("密码", self.ed_pass)
        form.addRow("远端 adb 路径（可空）", self.ed_adb)
        lay.addLayout(form)

        self.chk_show = QPushButton("显示密码")
        self.chk_show.setObjectName("Flat")
        self.chk_show.setCheckable(True)
        self.chk_show.setFixedWidth(90)
        self.chk_show.toggled.connect(
            lambda on: self.ed_pass.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        lay.addWidget(self.chk_show, 0, Qt.AlignLeft)
        password_hint = QLabel("密码按你的选择保存在本机配置文件中（明文），请仅在可信电脑使用。")
        password_hint.setObjectName("Hint")
        password_hint.setWordWrap(True)
        lay.addWidget(password_hint)

        self.lbl_msg = QLabel("")
        self.lbl_msg.setWordWrap(True)
        lay.addWidget(self.lbl_msg)

        btns = QHBoxLayout()
        b_save = QPushButton("保存配置")
        b_save.setObjectName("Secondary")
        b_save.clicked.connect(self._save)
        btns.addWidget(b_save)

        b_del = QPushButton("删除")
        b_del.setObjectName("Secondary")
        b_del.clicked.connect(self._delete)
        btns.addWidget(b_del)

        btns.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("Secondary")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)

        b_conn = QPushButton("连接")
        b_conn.setDefault(True)
        b_conn.clicked.connect(self._connect)
        btns.addWidget(b_conn)
        lay.addLayout(btns)

        self.cmb_saved.currentIndexChanged.connect(self._selection_changed)
        self._reload_combo(self._last_index)
        self._apply_saved(self.cmb_saved.currentIndex())

    # ---- 配置 ----------------------------------------------------------
    def _reload_combo(self, select_index=None):
        self.cmb_saved.blockSignals(True)
        self.cmb_saved.clear()
        for p in self.profiles:
            self.cmb_saved.addItem("%s  —  %s@%s:%s"
                                   % (p.get("name", ""), p.get("user", ""),
                                      p.get("host", ""), p.get("port", 22)))
        if self.profiles:
            index = self._last_index if select_index is None else int(select_index)
            self.cmb_saved.setCurrentIndex(max(0, min(index, len(self.profiles) - 1)))
        self.cmb_saved.blockSignals(False)
        self.lbl_saved.setText("共 %d 条历史配置；选中后会自动恢复密码，可直接连接。" %
                               len(self.profiles))

    def _persist(self, index):
        self._last_index = max(0, min(int(index), len(self.profiles) - 1)) if self.profiles else 0
        return ssh_backend.save_config({"last": self._last_index,
                                        "profiles": self.profiles})

    def _selection_changed(self, idx):
        self._apply_saved(idx)
        if 0 <= idx < len(self.profiles):
            self._persist(idx)

    def _apply_saved(self, idx: int):
        if 0 <= idx < len(self.profiles):
            p = self.profiles[idx]
            self.ed_name.setText(p.get("name", ""))
            self.ed_host.setText(p.get("host", ""))
            self.ed_port.setText(str(p.get("port", 22)))
            self.ed_user.setText(p.get("user", ""))
            self.ed_pass.setText(p.get("password", ""))
            self.ed_adb.setText(p.get("adb", ""))

    def _collect(self):
        try:
            port = int(self.ed_port.text().strip() or 22)
        except ValueError:
            raise ValueError("端口必须是数字")
        p = {
            "name": self.ed_name.text().strip() or "Ubuntu",
            "host": self.ed_host.text().strip(),
            "port": port,
            "user": self.ed_user.text().strip(),
            "password": self.ed_pass.text(),
            "adb": self.ed_adb.text().strip(),
        }
        if not p["host"]:
            raise ValueError("请填写 IP / 主机")
        if not p["user"]:
            raise ValueError("请填写用户名")
        return p

    def _msg(self, text: str, ok: bool = False):
        self.lbl_msg.setText(text)
        self.lbl_msg.setStyleSheet("color:%s;" % (theme.COLORS["ok"] if ok else theme.COLORS["error"]))

    def _save(self):
        try:
            p = self._collect()
        except ValueError as e:
            self._msg(str(e))
            return
        idx = self.cmb_saved.currentIndex()
        if 0 <= idx < len(self.profiles):
            self.profiles[idx] = p
        else:
            self.profiles.append(p)
            idx = len(self.profiles) - 1
        self.profiles = _merge_profiles(self.profiles)
        idx = next(i for i, item in enumerate(self.profiles)
                   if _profile_key(item) == _profile_key(p))
        ok, path = self._persist(idx)
        self._reload_combo(idx)
        self._apply_saved(idx)
        self._msg("已保存到 %s" % path if ok else "保存失败: %s" % path, ok)

    def _delete(self):
        idx = self.cmb_saved.currentIndex()
        if 0 <= idx < len(self.profiles):
            self.profiles.pop(idx)
            next_index = min(idx, len(self.profiles) - 1)
            self._persist(next_index)
            self._reload_combo(next_index)
            if self.profiles:
                self._apply_saved(next_index)
            self._msg("已删除", ok=True)

    def _connect(self):
        try:
            p = self._collect()
        except ValueError as e:
            self._msg(str(e))
            return
        # 连接即保存/更新，包括密码；无需先按“保存配置”。
        idx = next((i for i, item in enumerate(self.profiles)
                    if _profile_key(item) == _profile_key(p)), -1)
        if idx >= 0:
            if not p["password"] and self.profiles[idx].get("password"):
                p["password"] = self.profiles[idx]["password"]
                self.ed_pass.setText(p["password"])
            self.profiles[idx] = p
        else:
            self.profiles.append(p)
            idx = len(self.profiles) - 1
        self._persist(idx)
        self._profile = p
        self.accept()

    def result_profile(self):
        return self._profile

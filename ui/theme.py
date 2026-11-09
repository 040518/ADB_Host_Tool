# -*- coding: utf-8 -*-
"""界面基础设置：配色、资源路径、SVG 图标加载、用户设置持久化。"""

from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

APP_NAME = "MUXO+"
APP_ID = "ADB_Host_Tool"
APP_VERSION = "2.3.7"
WINDOWS_APP_ID = "ADBHostTool.Desktop.v2"

# ---------------------------------------------------------------------------
# 配色（与 themes/dark.qss 保持一致）
# ---------------------------------------------------------------------------
COLORS = {
    "bg":            "#1E1E1E",
    "bg_sidebar":    "#252526",
    "bg_toolbar":    "#2D2D30",
    "bg_input":      "#3C3C3C",
    "bg_activity":   "#333333",
    "bg_hover":      "#3E3E42",
    "text":          "#D4D4D4",
    "text_dim":      "#9D9D9D",
    "accent":        "#007ACC",
    "accent_light":  "#4FC1FF",
    "ok":            "#4EC9B0",
    "warn":          "#DCDCAA",
    "error":         "#F44747",
    "border":        "#3F3F46",
}


# ---------------------------------------------------------------------------
# 资源路径（兼容 PyInstaller onefile）
# ---------------------------------------------------------------------------
def resource_dir() -> str:
    """返回只读资源根目录（themes/ resources/）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller: sys._MEIPASS 指向解包临时目录
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return base
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir() -> str:
    """返回程序所在目录（EXE 或源码目录），用于放可写的用户文件。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def icon_path(name: str, variant: str = "") -> str:
    return os.path.join(resource_dir(), "resources", "icons", "%s%s.svg" % (name, variant))


def app_icon() -> QIcon:
    """返回窗口/任务栏统一使用的多尺寸应用图标。"""
    path = os.path.join(resource_dir(), "resources", "app.ico")
    if os.path.isfile(path):
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return load_icon("chip", COLORS["accent_light"], 128)


def load_icon(name: str, color: str = "", size: int = 24) -> QIcon:
    """加载 SVG 图标；color 不为空时现场着色渲染（用于选中态）。"""
    path = icon_path(name)
    if not os.path.isfile(path):
        return QIcon()
    if not color:
        return QIcon(path)
    # 读取 SVG 源码，替换 stroke 颜色后渲染
    try:
        with open(path, "r", encoding="utf-8") as f:
            svg = f.read()
        import re
        svg = re.sub(r'stroke="#[0-9A-Fa-f]{6}"', 'stroke="%s"' % color, svg)
        renderer = QSvgRenderer(svg.encode("utf-8"))
        pm = QPixmap(QSize(size, size) * 2)      # 2x 适配高 DPI
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        pm.setDevicePixelRatio(2.0)
        return QIcon(pm)
    except Exception:  # noqa: BLE001
        return QIcon(path)


def load_pixmap(name: str, color: str = "", size: int = 24) -> QPixmap:
    return load_icon(name, color, size).pixmap(QSize(size, size))


# ---------------------------------------------------------------------------
# 用户设置
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "font_family": "",          # 空 = 使用主题默认
    "font_size": 13,
    "mono_font_family": "",
    "mono_font_size": 12,
    "adb_path": "",             # 空 = 自动查找
    "auto_refresh_sec": 6,      # 设备列表自动刷新间隔，0 = 关闭
    "ssh_profiles_file": "",    # 空 = 默认位置（与旧版兼容）
    "log_max_lines": 5000,
    "log_autoscroll": True,
    "terminal_type": "xterm-256color",
    "camera_rotation": -90,      # RK3568 当前摄像头模组横装，预览默认向左校正
    "camera_rotation_ssh": 0,    # SSH 摄像头方向独立保存
    "last_page": 0,
    "restore_layout": True,
    "language": "zh",
}


def settings_path() -> str:
    base = os.environ.get("APPDATA") if os.name == "nt" else os.path.expanduser("~/.config")
    if not base:
        base = os.path.expanduser("~")
    d = os.path.join(base, APP_ID)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return os.path.join(d, "settings.json")


def load_settings() -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    p = settings_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_settings(cfg: dict) -> tuple[bool, str]:
    p = settings_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, p
    except OSError as e:
        return False, str(e)


def load_stylesheet() -> str:
    """读取 QSS，并把 @ICONS@ 替换成图标目录的绝对路径。

    QSS 里 url() 的相对路径按进程工作目录解析，打包成 EXE 后不可靠，
    因此这里统一替换成绝对路径（用 / 分隔，Windows 下 Qt 也能识别）。
    """
    path = os.path.join(resource_dir(), "themes", "dark.qss")
    try:
        with open(path, "r", encoding="utf-8") as f:
            css = f.read()
    except OSError:
        return ""
    icons = os.path.join(resource_dir(), "resources", "icons").replace(os.sep, "/")
    return css.replace("@ICONS@", icons)


def qcolor(key: str) -> QColor:
    return QColor(COLORS.get(key, "#FFFFFF"))

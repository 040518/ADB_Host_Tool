# -*- coding: utf-8 -*-
"""自定义控件：去掉 Fusion 风格分隔条上多余的原生圆点。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSplitter, QSplitterHandle

from ui import theme


class _FlatHandle(QSplitterHandle):
    """只画一条纯色分隔线，不画 Qt 默认的抓手圆点。"""

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        color = QColor(theme.COLORS["accent"]) if self._hover else QColor("#1B1B1C")
        p.fillRect(self.rect(), color)


class FlatSplitter(QSplitter):
    """无圆点分隔条。"""

    def createHandle(self):
        return _FlatHandle(self.orientation(), self)
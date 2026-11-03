# -*- coding: utf-8 -*-
"""文本查看对话框：设备文件内容预览。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from ui import theme


class TextViewerDialog(QDialog):
    def __init__(self, path: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看：%s" % path)
        self.resize(820, 600)
        self._path = path
        self._text = text

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        head = QLabel("%s   ·   %d 字符" % (path, len(text)))
        head.setObjectName("Hint")
        lay.addWidget(head)

        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setPlainText(text)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.view.setFont(f)
        lay.addWidget(self.view, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_copy = QPushButton("复制全部")
        b_copy.setObjectName("Secondary")
        b_copy.clicked.connect(self._copy)
        btns.addWidget(b_copy)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_close)
        lay.addLayout(btns)

    def _copy(self):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self._text)
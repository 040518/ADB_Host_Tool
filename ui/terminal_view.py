# -*- coding: utf-8 -*-
"""Qt 终端控件：把 terminal_backend 的 pyte 屏幕画到 QWidget 上。

特性：
* 真彩 / 16 色 ANSI、粗体、反色
* 等宽字体、光标绘制
* 完整按键映射（方向键、Home/End/PgUp/PgDn、F1~F12、Ctrl 组合键）
* Ctrl+Shift+V 粘贴、Ctrl+Shift+C 复制
* 窗口尺寸变化时同步 rows/cols 给设备（经 session.resize）
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QScrollBar, QWidget

# ANSI 16 色（VS Code / MobaXterm 观感）
PALETTE = {
    "black": QColor("#1C1C1C"), "red": QColor("#CD3131"),
    "green": QColor("#0DBC79"), "brown": QColor("#E5E510"),
    "blue": QColor("#2472C8"), "magenta": QColor("#BC3FBC"),
    "cyan": QColor("#11A8CD"), "white": QColor("#E5E5E5"),
    "brightblack": QColor("#666666"), "brightred": QColor("#F14C4C"),
    "brightgreen": QColor("#23D18B"), "brightbrown": QColor("#F5F543"),
    "brightblue": QColor("#3B8EEA"), "brightmagenta": QColor("#D670D6"),
    "brightcyan": QColor("#29B8DB"), "brightwhite": QColor("#FFFFFF"),
}
DEFAULT_FG = QColor("#E6E6E6")
DEFAULT_BG = QColor("#1A1A1A")
CURSOR_COLOR = QColor("#AEAFAD")
SELECTION_BG = QColor("#264F78")

KEYMAP = {
    Qt.Key_Up: "\x1b[A", Qt.Key_Down: "\x1b[B",
    Qt.Key_Right: "\x1b[C", Qt.Key_Left: "\x1b[D",
    Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F",
    Qt.Key_PageUp: "\x1b[5~", Qt.Key_PageDown: "\x1b[6~",
    Qt.Key_Insert: "\x1b[2~", Qt.Key_Delete: "\x1b[3~",
    Qt.Key_Backspace: "\x7f", Qt.Key_Tab: "\t",
    Qt.Key_Backtab: "\x1b[Z", Qt.Key_Escape: "\x1b",
    Qt.Key_F1: "\x1bOP", Qt.Key_F2: "\x1bOQ", Qt.Key_F3: "\x1bOR", Qt.Key_F4: "\x1bOS",
    Qt.Key_F5: "\x1b[15~", Qt.Key_F6: "\x1b[17~", Qt.Key_F7: "\x1b[18~",
    Qt.Key_F8: "\x1b[19~", Qt.Key_F9: "\x1b[20~", Qt.Key_F10: "\x1b[21~",
    Qt.Key_F11: "\x1b[23~", Qt.Key_F12: "\x1b[24~",
}


class TerminalView(QWidget):
    """终端显示 + 输入。"""

    resized_grid = Signal(int, int)

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)

        self._font_family = "Cascadia Mono"
        self._font_size = 11
        self._font = QFont(self._font_family, self._font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setFixedPitch(True)
        self._metrics = QFontMetricsF(self._font)
        self._cell_w = max(6.0, self._metrics.horizontalAdvance("M"))
        self._cell_h = max(12.0, self._metrics.height())
        self._ascent = self._metrics.ascent()
        self._grid = (0, 0)
        self._dirty = True
        # 选区使用终端网格里的线性半开区间 [anchor, active)，不依赖像素，
        # 因而窗口重绘、ANSI 颜色变化时仍能精确复制原文字。
        self._selection_anchor = None
        self._selection_active = None
        self._selection_origin = None
        self._selecting = False
        self._drag_position = None
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(60)
        self._drag_timer.timeout.connect(self._drag_autoscroll)
        self._cursor_on = True
        self._blink = QTimer(self)
        self._blink.setInterval(530)
        self._blink.timeout.connect(self._toggle_cursor)
        self._blink.start()

        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # 终端自身维护滚动历史；滚动条始终可见，便于明确当前位置并可拖动回看。
        self._scrollbar = QScrollBar(Qt.Vertical, self)
        self._scrollbar.setRange(0, 0)
        self._scrollbar.setPageStep(1)
        self._scrollbar.valueChanged.connect(self._scrollbar_changed)

    # ---- 配置 ----------------------------------------------------------
    def set_font(self, family: str, size: int):
        self._font_family = family or self._font_family
        self._font_size = max(6, int(size or self._font_size))
        self._font = QFont(self._font_family, self._font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setFixedPitch(True)
        self._metrics = QFontMetricsF(self._font)
        self._cell_w = max(6.0, self._metrics.horizontalAdvance("M"))
        self._cell_h = max(12.0, self._metrics.height())
        self._ascent = self._metrics.ascent()
        self._update_grid(force=True)
        self.update()

    def set_session(self, session):
        self.session = session
        self.clear_selection()
        self._update_grid(force=True)
        self._sync_scrollbar()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()

    # ---- 网格 ----------------------------------------------------------
    def _update_grid(self, force=False):
        usable_width = max(0, self.width() - self._scrollbar.sizeHint().width())
        cols = max(20, int(usable_width / self._cell_w))
        rows = max(4, int(self.height() / self._cell_h))
        if force or (rows, cols) != self._grid:
            self._grid = (rows, cols)
            if self.session is not None:
                try:
                    self.session.resize(rows, cols)
                except Exception:  # noqa: BLE001
                    pass
            self.resized_grid.emit(rows, cols)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        width = self._scrollbar.sizeHint().width()
        self._scrollbar.setGeometry(max(0, self.width() - width), 0,
                                    width, self.height())
        self._update_grid()

    # ---- 刷新 ----------------------------------------------------------
    def _tick(self):
        sess = self.session
        if sess is None:
            return
        drained = False
        try:
            while True:
                sess.out_q.get_nowait()
                drained = True
        except Exception:  # noqa: BLE001
            pass
        if drained or self._dirty:
            self._dirty = False
            self._sync_scrollbar()
            self.update()

    def _history_state(self):
        sess = self.session
        getter = getattr(sess, "history_state", None) if sess is not None else None
        if getter is None:
            return 0, 0, 0
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return 0, 0, 0

    def _sync_scrollbar(self):
        low, high, value = self._history_state()
        self._scrollbar.blockSignals(True)
        self._scrollbar.setRange(int(low), int(high))
        self._scrollbar.setPageStep(max(1, self._grid[0]))
        self._scrollbar.setValue(int(value))
        self._scrollbar.blockSignals(False)

    def _scrollbar_changed(self, value):
        sess = self.session
        scroll = getattr(sess, "scroll_history_to", None) if sess is not None else None
        if scroll is None:
            return
        try:
            actual = int(scroll(value))
        except Exception:  # noqa: BLE001
            return
        if actual != value:
            self._scrollbar.blockSignals(True)
            self._scrollbar.setValue(actual)
            self._scrollbar.blockSignals(False)
        if self._selecting and self._drag_position is not None:
            self._extend_selection(self._cell_index(self._drag_position))
        self.update()

    def _return_to_live(self):
        if self._scrollbar.value() != self._scrollbar.maximum():
            self._scrollbar.setValue(self._scrollbar.maximum())

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        self.update()

    def refresh(self):
        self._dirty = True
        self.update()

    def _color(self, name, default: QColor) -> QColor:
        if not name or name == "default":
            return default
        return PALETTE.get(name, default)

    # ---- 绘制 ----------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), DEFAULT_BG)
        p.setFont(self._font)
        sess = self.session
        rows, cols = self._grid
        if sess is None:
            p.setPen(QColor("#9D9D9D"))
            p.drawText(QRectF(12, 12, self.width() - 24, 40), Qt.AlignLeft | Qt.AlignVCenter,
                       "未连接终端。点击上方「连接终端」开始。")
            return
        lines = sess.snapshot_lines()
        if lines is None:
            self._paint_plain(p)
            return

        cw, ch = self._cell_w, self._cell_h
        for y, (text, segs) in enumerate(lines[:rows]):
            base_y = y * ch
            # 先画 ANSI 背景，再画选区，最后画文字，避免彩色背景盖住选区。
            for (x0, x1, (fg, bg, bold, reverse)) in segs:
                if x0 >= len(text):
                    break
                seg = text[x0:min(x1, len(text))]
                if not seg:
                    continue
                f = self._color(fg, DEFAULT_FG)
                b = self._color(bg, DEFAULT_BG)
                if reverse:
                    f, b = b, f
                if b != DEFAULT_BG:
                    p.fillRect(QRectF(x0 * cw, base_y, (len(seg)) * cw, ch), b)

            selected = self._selection_span(y)
            if selected is not None:
                sx0, sx1 = selected
                p.fillRect(QRectF(sx0 * cw, base_y, (sx1 - sx0) * cw, ch),
                           SELECTION_BG)

            for (x0, x1, (fg, bg, bold, reverse)) in segs:
                if x0 >= len(text):
                    break
                seg = text[x0:min(x1, len(text))]
                if not seg:
                    continue
                f = self._color(fg, DEFAULT_FG)
                b = self._color(bg, DEFAULT_BG)
                if reverse:
                    f, b = b, f
                if bold and f == DEFAULT_FG:
                    f = QColor("#FFFFFF")
                p.setPen(f)
                # pyte 的宽字符续列已在文本中占一个空单元格，因此这里严格按
                # 网格列绘制，不能再按字符宽度额外推进，否则中文后的字符会右移。
                for offset, chs in enumerate(seg):
                    if chs != " ":
                        p.drawText(QPointF((x0 + offset) * cw,
                                           base_y + self._ascent), chs)

        # 光标：行号必须落在实际渲染的行范围内，否则会画到文字下方一行
        cursor = self._visible_cursor()
        if self._cursor_on and self.hasFocus() and sess.alive and cursor is not None:
            cx, cy = cursor
            p.fillRect(QRectF(cx * cw, cy * ch, cw, ch), CURSOR_COLOR)

    def _visible_cursor(self):
        # HistoryScreen 移动 buffer 时并不移动 cursor.y；实际输入行位于
        # 当前视口下方 len(history.bottom) 行，超出视口时不绘制光标。
        screen = getattr(self.session, "screen", None)
        if screen is None:
            return None
        history = getattr(screen, "history", None)
        shift = len(history.bottom) if history is not None else 0
        cx, cy = int(screen.cursor.x), int(screen.cursor.y) + shift
        rows, cols = self._grid
        if not shift and getattr(screen.cursor, "hidden", False):
            return None
        if 0 <= cy < rows and 0 <= cx < cols:
            return cx, cy
        return None

    def _paint_plain(self, p: QPainter):
        """无 pyte 时的降级显示。"""
        sess = self.session
        import re
        data = bytes(getattr(sess, "plain", b"")[-60000:])
        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", data.decode("utf-8", "replace"))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        rows = max(1, int(self.height() / self._cell_h))
        lines = text.split("\n")[-rows:]
        p.setPen(DEFAULT_FG)
        for i, line in enumerate(lines):
            p.drawText(QPointF(0, i * self._cell_h + self._ascent), line)

    # ---- 输入 ----------------------------------------------------------
    def event(self, e):
        # QWidget::event 默认把 Tab/Shift+Tab 当作焦点切换，在 keyPressEvent
        # 之前就吃掉。终端已连接时必须在这里截获，原样交给远端 shell 补全。
        if (e.type() == QEvent.KeyPress and
                e.key() in (Qt.Key_Tab, Qt.Key_Backtab) and
                self.session is not None and getattr(self.session, "alive", False)):
            self.keyPressEvent(e)
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        sess = self.session
        if e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
            self.select_all()
            e.accept()
            return
        if sess is None or not getattr(sess, "alive", False):
            super().keyPressEvent(e)
            return
        mods = e.modifiers()
        key = e.key()

        # Ctrl+Shift+C / V
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:
                self.copy_selection()
                return
            if key == Qt.Key_V:
                self.paste()
                return

        # Windows/Linux 终端常见的备用快捷键。
        if key == Qt.Key_Insert and mods & Qt.ControlModifier:
            self.copy_selection()
            return
        if key == Qt.Key_Insert and mods & Qt.ShiftModifier:
            self.paste()
            return

        # Ctrl + 字母
        if mods & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
            self._return_to_live()
            self.clear_selection()
            sess.send_ctrl(chr(key))
            return

        if key in KEYMAP and not (mods & Qt.ControlModifier):
            self._return_to_live()
            self.clear_selection()
            sess.write_text(KEYMAP[key])
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._return_to_live()
            self.clear_selection()
            sess.write_text("\r")
            return

        text = e.text()
        if text:
            self._return_to_live()
            self.clear_selection()
            sess.write_text(text)
            return
        super().keyPressEvent(e)

    # ---- 复制粘贴 ------------------------------------------------------
    def _view_offset(self):
        screen = getattr(self.session, "screen", None)
        history = getattr(screen, "history", None)
        return len(history.top) if history is not None else 0

    def _document_lines(self):
        """完整历史和可见屏幕，使用原始字符单元以正确处理中文续列。"""
        sess = self.session
        screen = getattr(sess, "screen", None)
        if screen is None:
            return None
        history = getattr(screen, "history", None)
        top = list(history.top) if history is not None else []
        bottom = list(history.bottom) if history is not None else []
        return top + [screen.buffer[y] for y in range(screen.lines)] + bottom

    def _cell_index(self, pos) -> int:
        rows, cols = self._grid
        rows, cols = max(1, rows), max(1, cols)
        col = max(0, min(cols - 1, int(pos.x() / self._cell_w)))
        row = max(0, min(rows - 1, int(pos.y() / self._cell_h)))
        return (row + self._view_offset()) * cols + col

    def _selection_range(self):
        if self._selection_anchor is None or self._selection_active is None:
            return None
        start = min(self._selection_anchor, self._selection_active)
        end = max(self._selection_anchor, self._selection_active)
        return (start, end) if end > start else None

    def _selection_span(self, row):
        selected = self._selection_range()
        if selected is None:
            return None
        _rows, cols = self._grid
        if cols <= 0:
            return None
        start, end = selected
        row_start = (row + self._view_offset()) * cols
        left, right = max(start, row_start), min(end, row_start + cols)
        if right <= left:
            return None
        return left - row_start, right - row_start

    def _extend_selection(self, idx):
        """把拖选扩展到 idx；正向/反向均包含鼠标落点字符。"""
        origin = self._selection_origin
        if origin is None:
            return
        self._selection_anchor = min(origin, idx)
        self._selection_active = max(origin, idx) + 1

    def has_selection(self):
        return self._selection_range() is not None

    def clear_selection(self):
        changed = self._selection_anchor is not None or self._selection_active is not None
        self._selection_anchor = None
        self._selection_active = None
        self._selection_origin = None
        self._selecting = False
        self._drag_timer.stop()
        self._drag_position = None
        if changed:
            self.update()

    def selected_text(self):
        sess = self.session
        selected = self._selection_range()
        if sess is None or selected is None:
            return ""
        lines = sess.snapshot_lines() or []
        document = self._document_lines()
        _rows, cols = self._grid
        if not lines or cols <= 0:
            return ""
        start, end = selected
        first_row = max(0, start // cols)
        last_row = min((len(document) if document is not None else len(lines)) - 1,
                       (end - 1) // cols)
        parts = []
        screen = getattr(sess, "screen", None)
        for row in range(first_row, last_row + 1):
            left = start % cols if row == first_row else 0
            right = ((end - 1) % cols) + 1 if row == last_row else cols
            if document is not None:
                # 宽字符的第二列在 pyte 中 data==""；复制时跳过这个续列，
                # 但保留真正的空格单元格，避免得到“中 文”这样的伪空格。
                cells = document[row]
                part = "".join((cells[col].data or "")
                               for col in range(left, min(right, cols)))
            else:
                part = lines[row][0][left:right]
            parts.append(part.rstrip())
        return "\n".join(parts).rstrip("\n")

    def copy_selection(self):
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        return True

    def select_all(self):
        sess = self.session
        lines = sess.snapshot_lines() if sess is not None else []
        lines = lines or []
        _rows, cols = self._grid
        document = self._document_lines()
        if document is not None:
            lines = [("".join((row[x].data or " ") for x in range(cols)).rstrip(), [])
                     for row in document]
        nonempty = [i for i, (text, _segs) in enumerate(lines) if text]
        if not nonempty or cols <= 0:
            self.clear_selection()
            return
        first, last = nonempty[0], nonempty[-1]
        self._selection_anchor = first * cols
        self._selection_active = last * cols + len(lines[last][0])
        self._selection_origin = None
        self.update()

    def paste(self):
        sess = self.session
        if sess is None:
            return
        data = QGuiApplication.clipboard().text()
        if data:
            self._return_to_live()
            self.clear_selection()
            sess.write_text(data.replace("\r\n", "\r").replace("\n", "\r"))

    # ---- 鼠标选区 ------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.session is not None:
            self.setFocus(Qt.MouseFocusReason)
            idx = self._cell_index(e.position())
            self._selection_origin = idx
            self._selection_anchor = idx
            self._selection_active = idx
            self._selecting = True
            self._drag_position = e.position()
            self._drag_timer.start()
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._selecting and e.buttons() & Qt.LeftButton:
            self._drag_position = e.position()
            idx = self._cell_index(e.position())
            self._extend_selection(idx)
            self.update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._selecting:
            idx = self._cell_index(e.position())
            origin = self._selection_origin
            if idx == origin:
                self._selection_anchor = self._selection_active = origin
            else:
                self._extend_selection(idx)
            self._selection_origin = None
            self._selecting = False
            self._drag_timer.stop()
            self._drag_position = None
            self.update()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self.session is not None:
            lines = self.session.snapshot_lines() or []
            _rows, cols = self._grid
            idx = self._cell_index(e.position())
            absolute_row, col = divmod(idx, max(1, cols))
            row = absolute_row - self._view_offset()
            if row < len(lines):
                text = lines[row][0]
                if col < len(text) and not text[col].isspace():
                    left, right = col, col + 1
                    while left > 0 and not text[left - 1].isspace():
                        left -= 1
                    while right < len(text) and not text[right].isspace():
                        right += 1
                    self._selection_anchor = absolute_row * cols + left
                    self._selection_active = absolute_row * cols + right
                    self._selection_origin = None
                    self._selecting = False
                    self.update()
                    e.accept()
                    return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e):
        # 与 VS Code 终端默认 copyPaste 行为一致：有选区时复制并取消选中，
        # 没有选区时粘贴。
        if self.has_selection():
            if self.copy_selection():
                self.clear_selection()
        else:
            self.paste()
        e.accept()

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if delta:
            steps = max(1, abs(delta) // 120)
            direction = -1 if delta > 0 else 1
            self._scrollbar.setValue(
                self._scrollbar.value() + direction * steps * 3)
            e.accept()
            return
        super().wheelEvent(e)

    def _drag_autoscroll(self):
        pos = self._drag_position
        if not self._selecting or pos is None:
            self._drag_timer.stop()
            return
        direction = -1 if pos.y() < 0 else (1 if pos.y() >= self.height() else 0)
        if direction:
            self._scrollbar.setValue(self._scrollbar.value() + direction * 2)
            self._extend_selection(self._cell_index(pos))
            self.update()

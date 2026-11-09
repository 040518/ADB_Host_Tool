"""真实 pyte 历史缓冲 + Qt 输入事件，不需要连接设备。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from core.terminal_backend import TerminalSession
from ui.terminal_view import TerminalView


class HistoryInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = TerminalView()
        self.view.resize(640, 240)
        self.view.show()
        self.app.processEvents()
        rows, cols = self.view._grid
        self.session = TerminalSession('', '', rows=rows, cols=cols)
        self.view.set_session(self.session)
        self.session._feed((''.join('ROW_%03d 中文\r\n' % i for i in range(100)) +
                            'root@ATK-DLRK3568:/# ').encode())
        self.session.alive = True
        self.sent = []
        self.session.send_ctrl = self.sent.append
        self.view._sync_scrollbar()

    def tearDown(self):
        self.session.alive = False
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def wheel_up(self):
        pos = QPointF(20, 20)
        event = QWheelEvent(pos, pos, QPoint(), QPoint(0, 120),
                            Qt.LeftButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(self.view, event)

    def test_cursor_follows_prompt_and_disappears_outside_viewport(self):
        cursor = self.view._visible_cursor()
        self.assertEqual(cursor, (len('root@ATK-DLRK3568:/# '), self.session.screen.cursor.y))
        self.wheel_up()
        self.assertIsNone(self.view._visible_cursor())
        self.view._return_to_live()
        self.assertEqual(self.view._visible_cursor(), cursor)

    def test_ctrl_a_includes_all_history_from_any_viewport(self):
        QTest.keyClick(self.view, Qt.Key_A, Qt.ControlModifier)
        expected = self.view.selected_text()
        self.assertIn('ROW_000 中文', expected)
        self.assertIn('ROW_099 中文', expected)
        self.assertTrue(expected.endswith('root@ATK-DLRK3568:/#'))
        self.assertEqual(self.sent, [])
        self.wheel_up()
        self.assertEqual(self.view.selected_text(), expected)
        QTest.keyClick(self.view, Qt.Key_A, Qt.ControlModifier)
        self.assertEqual(self.view.selected_text(), expected)
        self.assertTrue(self.view.copy_selection())
        self.assertEqual(self.app.clipboard().text(), expected)

    def test_drag_wheel_retains_anchor_and_extends_selection(self):
        self.view._scrollbar.setValue(30)
        pos = QPoint(4, int(self.view._cell_h * 3.5))
        QTest.mousePress(self.view, Qt.LeftButton, pos=pos)
        origin = self.view._selection_origin
        self.wheel_up()
        self.assertEqual(self.view._selection_origin, origin)
        self.assertTrue(self.view._selecting)
        text = self.view.selected_text()
        self.assertGreaterEqual(len(text.splitlines()), 4)
        QTest.mouseRelease(self.view, Qt.LeftButton, pos=pos)
        self.assertEqual(self.view.selected_text(), text)
        self.view._scrollbar.setValue(0)
        self.assertEqual(self.view.selected_text(), text)

    def test_drag_outside_view_autoscrolls(self):
        self.view._scrollbar.setValue(30)
        QTest.mousePress(self.view, Qt.LeftButton, pos=QPoint(4, 20))
        self.view._drag_position = QPointF(4, -10)
        before = self.view._scrollbar.value()
        self.view._drag_autoscroll()
        self.assertLess(self.view._scrollbar.value(), before)
        self.assertTrue(self.view.has_selection())


if __name__ == '__main__':
    unittest.main(verbosity=2)

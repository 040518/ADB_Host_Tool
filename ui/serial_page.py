# -*- coding: utf-8 -*-
"""串口助手：连接、收发、命令封装、协议插件与多通道波形。"""

from __future__ import annotations

import codecs
import json
import math
import os
import time
from collections import deque
from typing import Dict, List

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QTextCursor
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QColorDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QToolButton, QVBoxLayout, QWidget, QInputDialog,
)

from core import serial_config
from core.serial_protocols import (LINE_ENDINGS, builtin_plugins,
                                   command_placeholders, encode_payload,
                                   render_command)
from ui import theme
from ui.context import AppContext


WAVE_COLORS = [
    "#4FC1FF", "#4EC9B0", "#DCDCAA", "#F44747", "#C586C0", "#CE9178",
    "#9CDCFE", "#B5CEA8", "#D16969", "#569CD6", "#FFD700", "#FF8C00",
]


def mono(size: int = 10) -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(size)
    return font


class WaveformWidget(QWidget):
    """不依赖 QtCharts/matplotlib 的轻量多通道曲线。"""

    channelsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 240)
        self.setMouseTracking(True)
        self.setToolTip("鼠标滚轮调整样本窗口，右侧列表可隐藏通道")
        self._series: Dict[str, deque] = {}
        self._visible: Dict[str, bool] = {}
        self._colors: Dict[str, str] = {}
        self._sample_index = 0
        self.max_points = 500
        self.capacity = 5000
        self.paused = False
        self.auto_scale = True
        self.fixed_min = -1.0
        self.fixed_max = 1.0
        self._mouse_pos = None

    def channel_names(self) -> List[str]:
        return list(self._series.keys())

    def is_channel_visible(self, name: str) -> bool:
        return self._visible.get(name, True)

    def set_channel_visible(self, name: str, visible: bool) -> None:
        self._visible[name] = bool(visible)
        self.update()

    def channel_color(self, name: str) -> str:
        if name not in self._colors:
            self._colors[name] = WAVE_COLORS[len(self._colors) % len(WAVE_COLORS)]
        return self._colors[name]

    def set_channel_color(self, name: str, color: str) -> None:
        value = QColor(color)
        if not value.isValid():
            return
        self._colors[name] = value.name().upper()
        self.update()

    def add_frame(self, values: Dict[str, float]) -> None:
        self._sample_index += 1
        changed = False
        for name, raw in values.items():
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            if name not in self._series:
                self._series[name] = deque(maxlen=self.capacity)
                self._visible[name] = True
                self.channel_color(name)
                changed = True
            self._series[name].append((self._sample_index, value))
        if changed:
            self.channelsChanged.emit()
        if not self.paused:
            self.update()

    def clear(self) -> None:
        self._series.clear()
        self._visible.clear()
        self._sample_index = 0
        self.channelsChanged.emit()
        self.update()

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        if not paused:
            self.update()

    def wheelEvent(self, event):
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self.max_points = max(50, min(self.capacity, int(self.max_points * factor)))
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        self.update()

    def leaveEvent(self, event):
        self._mouse_pos = None
        self.update()
        super().leaveEvent(event)

    def _range(self, start: int):
        values = []
        for name, points in self._series.items():
            if not self._visible.get(name, True):
                continue
            values.extend(v for x, v in points if x >= start)
        if not values:
            return -1.0, 1.0
        if not self.auto_scale:
            return self.fixed_min, self.fixed_max
        lo, hi = min(values), max(values)
        if abs(hi - lo) < 1e-12:
            pad = max(1.0, abs(hi) * 0.1)
        else:
            pad = (hi - lo) * 0.08
        return lo - pad, hi + pad

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#101418"))
        painter.setFont(mono(8))
        metrics = painter.fontMetrics()
        line_height = metrics.height()
        plot = QRectF(56, 18, max(20, self.width() - 72),
                      max(20, self.height() - 27 - 2 * line_height))

        start = max(1, self._sample_index - self.max_points + 1)
        end = max(start + 1, self._sample_index or self.max_points)
        sample_span = end - start
        lo, hi = self._range(start)
        span = max(1e-12, hi - lo)

        # 网格和坐标。
        for i in range(6):
            y = plot.top() + plot.height() * i / 5.0
            painter.setPen(QPen(QColor("#283038"), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            value = hi - span * i / 5.0
            painter.setPen(QColor("#84919A"))
            painter.drawText(QRectF(2, y - 8, 50, 16), Qt.AlignRight | Qt.AlignVCenter,
                             "%.4g" % value)
        label_width = metrics.horizontalAdvance(str(end)) + 16
        intervals = max(1, min(10, sample_span, int(plot.width() / label_width)))
        for i in range(intervals + 1):
            sample = start + round(sample_span * i / intervals)
            x = plot.left() + (sample - start) * plot.width() / sample_span
            painter.setPen(QPen(QColor("#222A31"), 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            label_left = min(x - label_width / 2, self.width() - label_width - 2)
            painter.setPen(QColor("#84919A"))
            painter.drawText(QRectF(label_left, plot.bottom() + 3, label_width, line_height),
                             Qt.AlignCenter, str(sample))
        painter.setPen(QPen(QColor("#59636C"), 1))
        painter.drawRect(plot)
        painter.setPen(QColor("#84919A"))
        painter.drawText(QRectF(plot.left(), plot.bottom() + line_height + 6,
                                plot.width(), line_height),
                         Qt.AlignCenter, "样本序号  |  最近 %d 点 · 滚轮缩放" % self.max_points)

        if not self._series:
            painter.setPen(QColor("#7E8A92"))
            painter.drawText(plot, Qt.AlignCenter,
                             "等待波形数据\nFireWater: 1,2,3 或 temp:25.1,hum:60\n"
                             "JustFloat: float32[] + 00 00 80 7F")
            return

        for idx, (name, points) in enumerate(self._series.items()):
            if not self._visible.get(name, True):
                continue
            visible = [(x, v) for x, v in points if x >= start]
            if not visible:
                continue
            path = QPainterPath()
            for j, (sample, value) in enumerate(visible):
                x = plot.left() + (sample - start) * plot.width() / sample_span
                y = plot.bottom() - (value - lo) * plot.height() / span
                if j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.setPen(QPen(QColor(self.channel_color(name)), 1.5))
            if len(visible) == 1:
                painter.drawPoint(path.currentPosition())
            else:
                painter.drawPath(path)

        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx = self._mouse_pos.x()
            sample = int(round(start + (mx - plot.left()) / plot.width() * sample_span))
            x = plot.left() + (sample - start) * plot.width() / sample_span
            painter.setPen(QPen(QColor("#A0A0A0"), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            tips = ["#%d" % sample]
            for idx, (name, points) in enumerate(self._series.items()):
                if not self._visible.get(name, True):
                    continue
                nearest = min(points, key=lambda p: abs(p[0] - sample), default=None)
                if nearest and abs(nearest[0] - sample) <= 2:
                    tips.append("%s=%.6g" % (name, nearest[1]))
            text = "  ".join(tips)
            painter.setPen(QColor("#D4D4D4"))
            painter.fillRect(QRectF(plot.left() + 6, plot.top() + 6,
                                    min(plot.width() - 12, 12 + len(text) * 7), 22),
                             QColor("#252A2E"))
            painter.drawText(QRectF(plot.left() + 10, plot.top() + 7,
                                    plot.width() - 20, 20), Qt.AlignLeft, text)


class CommandDialog(QDialog):
    def __init__(self, command=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("命令模板")
        self.setMinimumWidth(520)
        command = command or {}
        form = QFormLayout(self)
        self.ed_group = QLineEdit(str(command.get("group", "常用")))
        self.ed_name = QLineEdit(str(command.get("name", "")))
        self.ed_payload = QLineEdit(str(command.get("payload", "")))
        self.ed_payload.setPlaceholderText("例：AT+SET={value}，发送时会询问 value")
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["文本", "HEX"])
        self.cmb_mode.setCurrentText(str(command.get("mode", "文本")))
        self.cmb_ending = QComboBox()
        self.cmb_ending.addItems(list(LINE_ENDINGS))
        self.cmb_ending.setCurrentText(str(command.get("ending", "CRLF (\\r\\n)")))
        form.addRow("分组", self.ed_group)
        form.addRow("名称", self.ed_name)
        form.addRow("命令内容", self.ed_payload)
        form.addRow("格式", self.cmb_mode)
        form.addRow("行尾", self.cmb_ending)
        hint = QLabel("支持 {name} 参数占位符；点击发送时会逐个输入。")
        hint.setObjectName("Hint")
        form.addRow("", hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self):
        return {
            "group": self.ed_group.text().strip() or "常用",
            "name": self.ed_name.text().strip(),
            "payload": self.ed_payload.text(),
            "mode": self.cmb_mode.currentText(),
            "ending": self.cmb_ending.currentText(),
        }

    def accept(self):
        if not self.ed_name.text().strip():
            QMessageBox.warning(self, "命令模板", "请输入命令名称")
            return
        super().accept()


class SerialPage(QWidget):
    HISTORY_LIMIT = 4 * 1024 * 1024

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.config = serial_config.load_config()
        self.plugins = {item.plugin_id: item for item in builtin_plugins()}
        self.plugin = self.plugins["firewater"]
        self.raw_history = bytearray()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._intentional_close = False
        self._last_port = ""

        self.serial = QSerialPort(self)
        self.serial.readyRead.connect(self._read_ready)
        self.serial.errorOccurred.connect(self._serial_error)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.setInterval(1500)
        self.reconnect_timer.timeout.connect(self._try_reconnect)
        self.send_timer = QTimer(self)
        self.send_timer.timeout.connect(self.send_manual)

        self._build_ui()
        self._load_config_to_ui()
        self.refresh_ports()
        self._refresh_commands()
        self._update_status("未连接")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 7, 8, 7)
        root.setSpacing(6)

        # VOFA+ 式左侧“协议与连接”面板：把连接参数从波形上方
        # 移开，让中央区域尽可能留给曲线。
        self.control_panel = QGroupBox("协议与连接")
        self.control_panel.setFixedWidth(245)
        control_lay = QVBoxLayout(self.control_panel)
        control_lay.setContentsMargins(10, 14, 10, 10)
        control_lay.setSpacing(7)

        engine_title = QLabel("数据引擎")
        engine_title.setStyleSheet("font-weight:600;color:#4FC1FF;")
        control_lay.addWidget(engine_title)
        self.cmb_plugin = QComboBox()
        for item in self.plugins.values():
            self.cmb_plugin.addItem(item.name, item.plugin_id)
        self.cmb_plugin.currentIndexChanged.connect(self._plugin_changed)
        control_lay.addWidget(self.cmb_plugin)
        self.lbl_plugin = QLabel("")
        self.lbl_plugin.setObjectName("Hint")
        self.lbl_plugin.setWordWrap(True)
        self.lbl_plugin.setToolTip("当前插件说明")
        control_lay.addWidget(self.lbl_plugin)

        interface_row = QHBoxLayout()
        interface_row.addWidget(QLabel("数据接口"))
        interface_row.addStretch(1)
        interface_type = QLabel("串口")
        interface_type.setObjectName("Badge")
        interface_row.addWidget(interface_type)
        control_lay.addLayout(interface_row)

        serial_title = QLabel("串口参数配置")
        serial_title.setStyleSheet("font-weight:600;color:#4FC1FF;margin-top:6px;")
        control_lay.addWidget(serial_title)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(7)

        self.cmb_port = QComboBox()
        self.cmb_port.setMinimumWidth(120)
        refresh = QToolButton()
        refresh.setIcon(theme.load_icon("refresh"))
        refresh.setToolTip("刷新串口")
        refresh.clicked.connect(self.refresh_ports)
        port_row = QWidget()
        port_lay = QHBoxLayout(port_row)
        port_lay.setContentsMargins(0, 0, 0, 0)
        port_lay.setSpacing(3)
        port_lay.addWidget(self.cmb_port, 1)
        port_lay.addWidget(refresh)
        form.addRow("端口号", port_row)

        self.cmb_baud = QComboBox()
        self.cmb_baud.setEditable(True)
        self.cmb_baud.addItems(["9600", "19200", "38400", "57600", "115200", "230400",
                                "460800", "921600", "1000000", "2000000"])
        form.addRow("波特率", self.cmb_baud)
        self.cmb_data = self._combo(["5", "6", "7", "8"], 54)
        self.cmb_parity = self._combo(["无", "奇", "偶"], 58)
        self.cmb_stop = self._combo(["1", "1.5", "2"], 58)
        self.cmb_flow = self._combo(["无", "RTS/CTS", "XON/XOFF"], 84)
        form.addRow("数据流控", self.cmb_flow)
        form.addRow("校验位", self.cmb_parity)
        form.addRow("数据位数", self.cmb_data)
        form.addRow("停止位数", self.cmb_stop)
        control_lay.addLayout(form)

        signal_title = QLabel("流控信号")
        signal_title.setStyleSheet("font-weight:600;color:#4FC1FF;margin-top:6px;")
        control_lay.addWidget(signal_title)
        signal_row = QHBoxLayout()
        self.btn_connect = QPushButton("打开串口")
        self.btn_connect.setIcon(theme.load_icon("plug"))
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.chk_reconnect = QCheckBox("自动重连")
        self.chk_dtr = QCheckBox("DTR")
        self.chk_rts = QCheckBox("RTS")
        self.chk_dtr.toggled.connect(self._line_signal_changed)
        self.chk_rts.toggled.connect(self._line_signal_changed)
        signal_row.addWidget(self.chk_dtr)
        signal_row.addWidget(self.chk_rts)
        signal_row.addStretch(1)
        control_lay.addLayout(signal_row)
        control_lay.addWidget(self.chk_reconnect)
        control_lay.addWidget(self.btn_connect)
        self.lbl_state = QLabel("未连接")
        self.lbl_state.setObjectName("Badge")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        control_lay.addWidget(self.lbl_state)
        control_lay.addStretch(1)
        root.addWidget(self.control_panel)

        # 数据终端的显示选项放在底部工具栏，对应 VOFA+ 的 RX/TX 区。
        self.cmb_encoding = self._combo(["UTF-8", "GBK", "ASCII"], 82)
        self.cmb_encoding.currentTextChanged.connect(self._encoding_changed)
        self.chk_timestamp = QCheckBox("时间戳")
        self.chk_rx_hex = QCheckBox("HEX 显示")
        self.chk_rx_hex.toggled.connect(self._rerender_history)

        vertical = QSplitter(Qt.Vertical)
        vertical.setChildrenCollapsible(False)

        wave_box = QGroupBox("多通道实时波形")
        wave_lay = QVBoxLayout(wave_box)
        wave_lay.setContentsMargins(6, 10, 6, 6)
        wave_tools = QHBoxLayout()
        self.btn_pause = QPushButton("暂停绘制")
        self.btn_pause.setCheckable(True)
        self.btn_pause.clicked.connect(self._toggle_pause)
        wave_tools.addWidget(self.btn_pause)
        clear_wave = QPushButton("清空波形")
        clear_wave.setObjectName("Secondary")
        clear_wave.clicked.connect(lambda: self.wave.clear())
        wave_tools.addWidget(clear_wave)
        self.chk_auto_scale = QCheckBox("自动量程")
        self.chk_auto_scale.setChecked(True)
        self.chk_auto_scale.toggled.connect(self._auto_scale_changed)
        wave_tools.addWidget(self.chk_auto_scale)
        wave_tools.addStretch(1)
        self.lbl_samples = QLabel("0 样本 / 0 通道")
        self.lbl_samples.setObjectName("Hint")
        wave_tools.addWidget(self.lbl_samples)
        self.btn_wave_focus = QPushButton("放大波形")
        self.btn_wave_focus.setCheckable(True)
        self.btn_wave_focus.clicked.connect(self._toggle_wave_focus)
        wave_tools.addWidget(self.btn_wave_focus)
        wave_lay.addLayout(wave_tools)
        wave_row = QHBoxLayout()
        self.wave = WaveformWidget()
        self.wave.channelsChanged.connect(self._sync_channel_list)
        wave_row.addWidget(self.wave, 1)
        self.lst_channels = QListWidget()
        self.lst_channels.setFixedWidth(175)
        self.lst_channels.itemChanged.connect(self._channel_changed)
        self.lst_channels.itemDoubleClicked.connect(lambda _item: self.choose_channel_color())
        channel_tools = QVBoxLayout()
        channel_tools.setSpacing(4)
        channel_tools.addWidget(self.lst_channels, 1)
        color_buttons = QHBoxLayout()
        color_buttons.setSpacing(4)
        color_button = QPushButton("改色")
        color_button.setObjectName("Secondary")
        color_button.setToolTip("也可双击通道名称")
        color_button.clicked.connect(self.choose_channel_color)
        color_buttons.addWidget(color_button)
        reset_colors = QPushButton("重置")
        reset_colors.setObjectName("Secondary")
        reset_colors.setToolTip("恢复全部通道的默认配色")
        reset_colors.clicked.connect(self.reset_channel_colors)
        color_buttons.addWidget(reset_colors)
        channel_tools.addLayout(color_buttons)
        color_hint = QLabel("双击通道可单独改色")
        color_hint.setObjectName("Hint")
        color_hint.setAlignment(Qt.AlignCenter)
        channel_tools.addWidget(color_hint)
        wave_row.addLayout(channel_tools)
        wave_lay.addLayout(wave_row, 1)
        self.wave_box = wave_box
        vertical.addWidget(wave_box)

        self.monitor_box = QGroupBox("数据监视")
        monitor_lay = QVBoxLayout(self.monitor_box)
        monitor_lay.setContentsMargins(6, 10, 6, 6)
        mh = QHBoxLayout()
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        mh.addWidget(self.chk_autoscroll)
        mh.addWidget(QLabel("编码"))
        mh.addWidget(self.cmb_encoding)
        mh.addWidget(self.chk_timestamp)
        mh.addWidget(self.chk_rx_hex)
        mh.addStretch(1)
        clear_rx = QPushButton("清空")
        clear_rx.setObjectName("Secondary")
        clear_rx.clicked.connect(self.clear_receive)
        mh.addWidget(clear_rx)
        save_rx = QPushButton("保存")
        save_rx.setObjectName("Secondary")
        save_rx.clicked.connect(self.save_receive)
        mh.addWidget(save_rx)
        monitor_lay.addLayout(mh)
        self.txt_rx = QPlainTextEdit()
        self.txt_rx.setObjectName("MonitorView")
        self.txt_rx.setFont(mono(9))
        self.txt_rx.setReadOnly(True)
        self.txt_rx.setMaximumBlockCount(10000)
        monitor_lay.addWidget(self.txt_rx, 1)

        data_splitter = QSplitter(Qt.Horizontal)
        data_splitter.setChildrenCollapsible(False)
        send_box = QGroupBox("数据发送")
        send_lay = QVBoxLayout(send_box)
        send_lay.setContentsMargins(8, 10, 8, 7)
        opts = QHBoxLayout()
        self.cmb_tx_mode = self._combo(["文本", "HEX"], 72)
        self.cmb_ending = self._combo(list(LINE_ENDINGS), 118)
        opts.addWidget(QLabel("格式"))
        opts.addWidget(self.cmb_tx_mode)
        opts.addWidget(QLabel("行尾"))
        opts.addWidget(self.cmb_ending)
        self.chk_loop = QCheckBox("定时发送")
        self.chk_loop.toggled.connect(self._loop_toggled)
        opts.addWidget(self.chk_loop)
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(10, 600000)
        self.sp_interval.setValue(1000)
        self.sp_interval.setSuffix(" ms")
        self.sp_interval.valueChanged.connect(
            lambda value: self.send_timer.setInterval(value) if self.send_timer.isActive() else None)
        opts.addWidget(self.sp_interval)
        opts.addStretch(1)
        send_lay.addLayout(opts)
        self.txt_tx = QPlainTextEdit()
        self.txt_tx.setObjectName("MonitorView")
        self.txt_tx.setFont(mono(10))
        self.txt_tx.setPlaceholderText("输入文本，或 HEX：01 03 00 00 00 02")
        send_lay.addWidget(self.txt_tx, 1)
        send_buttons = QHBoxLayout()
        send = QPushButton("发送")
        send.setIcon(theme.load_icon("send"))
        send.clicked.connect(self.send_manual)
        send_buttons.addWidget(send)
        clear_tx = QPushButton("清空")
        clear_tx.setObjectName("Secondary")
        clear_tx.clicked.connect(self.txt_tx.clear)
        send_buttons.addWidget(clear_tx)
        send_buttons.addStretch(1)
        self.lbl_counter = QLabel("RX 0 B  |  TX 0 B")
        self.lbl_counter.setObjectName("Hint")
        send_buttons.addWidget(self.lbl_counter)
        send_lay.addLayout(send_buttons)
        data_splitter.addWidget(self.monitor_box)
        data_splitter.addWidget(send_box)
        data_splitter.setSizes([720, 400])

        command_box = QGroupBox("命令模板（双击发送）")
        command_lay = QVBoxLayout(command_box)
        command_lay.setContentsMargins(6, 10, 6, 6)
        cmd_buttons = QHBoxLayout()
        for text, slot in (("新建", self.add_command), ("编辑", self.edit_command),
                           ("删除", self.delete_command), ("导入", self.import_commands),
                           ("导出", self.export_commands)):
            button = QPushButton(text)
            button.setObjectName("Secondary")
            button.clicked.connect(slot)
            cmd_buttons.addWidget(button)
        cmd_buttons.addStretch(1)
        send_cmd = QPushButton("发送选中")
        send_cmd.clicked.connect(self.send_selected_command)
        cmd_buttons.addWidget(send_cmd)
        command_lay.addLayout(cmd_buttons)
        self.tbl_commands = QTableWidget(0, 4)
        self.tbl_commands.setHorizontalHeaderLabels(["分组", "名称", "命令", "格式"])
        self.tbl_commands.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_commands.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_commands.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_commands.verticalHeader().setVisible(False)
        self.tbl_commands.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_commands.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_commands.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_commands.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_commands.doubleClicked.connect(self.send_selected_command)
        command_lay.addWidget(self.tbl_commands, 1)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.addTab(data_splitter, "数据终端")
        self.bottom_tabs.addTab(command_box, "命令模板")
        self.lower_splitter = self.bottom_tabs
        vertical.addWidget(self.bottom_tabs)
        vertical.setSizes([680, 250])
        self.serial_splitter = vertical
        root.addWidget(vertical, 1)

    @staticmethod
    def _combo(items, width):
        box = QComboBox()
        box.addItems(items)
        box.setFixedWidth(width)
        return box

    # ------------------------------------------------------------ 配置/串口
    def _load_config_to_ui(self):
        cfg = self.config.get("serial", {})
        self._last_port = str(cfg.get("port", ""))
        self.cmb_baud.setCurrentText(str(cfg.get("baud", 115200)))
        self.cmb_data.setCurrentText(str(cfg.get("data_bits", 8)))
        self.cmb_parity.setCurrentText(str(cfg.get("parity", "无")))
        self.cmb_stop.setCurrentText(str(cfg.get("stop_bits", "1")))
        self.cmb_flow.setCurrentText(str(cfg.get("flow_control", "无")))
        self.chk_reconnect.setChecked(bool(cfg.get("auto_reconnect", True)))
        self.chk_dtr.setChecked(bool(cfg.get("dtr", False)))
        self.chk_rts.setChecked(bool(cfg.get("rts", False)))
        colors = self.config.get("channel_colors", {})
        if isinstance(colors, dict):
            for name, color in colors.items():
                self.wave.set_channel_color(str(name), str(color))
        self.cmb_encoding.setCurrentText(str(cfg.get("encoding", "UTF-8")))
        plugin_id = str(cfg.get("plugin", "firewater"))
        idx = self.cmb_plugin.findData(plugin_id)
        self.cmb_plugin.setCurrentIndex(max(0, idx))
        self._plugin_changed()

    def _save_config(self):
        try:
            baud = int(self.cmb_baud.currentText() or 115200)
        except ValueError:
            baud = 115200
        port = self.cmb_port.currentData() or self.cmb_port.currentText().split(" ", 1)[0]
        port = port or self._last_port
        self.config["serial"] = {
            "port": port,
            "baud": baud,
            "data_bits": int(self.cmb_data.currentText()),
            "parity": self.cmb_parity.currentText(),
            "stop_bits": self.cmb_stop.currentText(),
            "flow_control": self.cmb_flow.currentText(),
            "auto_reconnect": self.chk_reconnect.isChecked(),
            "dtr": self.chk_dtr.isChecked(), "rts": self.chk_rts.isChecked(),
            "encoding": self.cmb_encoding.currentText(),
            "plugin": self.cmb_plugin.currentData(),
        }
        self.config["channel_colors"] = dict(self.wave._colors)
        serial_config.save_config(self.config)

    def refresh_ports(self):
        current = self.cmb_port.currentData() or self._last_port
        self.cmb_port.blockSignals(True)
        self.cmb_port.clear()
        for info in QSerialPortInfo.availablePorts():
            description = info.description().strip()
            label = info.portName() + (("  —  " + description) if description else "")
            self.cmb_port.addItem(label, info.portName())
        if current:
            idx = self.cmb_port.findData(current)
            if idx >= 0:
                self.cmb_port.setCurrentIndex(idx)
        self.cmb_port.blockSignals(False)

    def toggle_connection(self):
        if self.serial.isOpen():
            self.close_serial(True)
        else:
            self.open_serial()

    def open_serial(self, silent=False):
        port = self.cmb_port.currentData()
        if not port:
            if not silent:
                QMessageBox.warning(self, "串口", "未发现可用串口，请插入设备后点击刷新。")
            return False
        try:
            baud = int(self.cmb_baud.currentText())
        except ValueError:
            if not silent:
                QMessageBox.warning(self, "串口", "波特率必须是正整数。")
            return False
        self._intentional_close = False
        self.serial.setPortName(str(port))
        self.serial.setBaudRate(baud)
        data_map = {5: QSerialPort.Data5, 6: QSerialPort.Data6,
                    7: QSerialPort.Data7, 8: QSerialPort.Data8}
        parity_map = {"无": QSerialPort.NoParity, "奇": QSerialPort.OddParity,
                      "偶": QSerialPort.EvenParity}
        stop_map = {"1": QSerialPort.OneStop, "1.5": QSerialPort.OneAndHalfStop,
                    "2": QSerialPort.TwoStop}
        flow_map = {"无": QSerialPort.NoFlowControl, "RTS/CTS": QSerialPort.HardwareControl,
                    "XON/XOFF": QSerialPort.SoftwareControl}
        self.serial.setDataBits(data_map[int(self.cmb_data.currentText())])
        self.serial.setParity(parity_map[self.cmb_parity.currentText()])
        self.serial.setStopBits(stop_map[self.cmb_stop.currentText()])
        self.serial.setFlowControl(flow_map[self.cmb_flow.currentText()])
        if not self.serial.open(QSerialPort.ReadWrite):
            self._update_status("打开失败", False)
            if not silent:
                QMessageBox.warning(self, "打开串口", self.serial.errorString())
            self._schedule_reconnect()
            return False
        self._last_port = str(port)
        self.serial.setDataTerminalReady(self.chk_dtr.isChecked())
        self.serial.setRequestToSend(self.chk_rts.isChecked())
        self.reconnect_timer.stop()
        self.btn_connect.setText("关闭串口")
        self.btn_connect.setIcon(theme.load_icon("unplug"))
        self._set_options_enabled(False)
        self._update_status("%s @ %d" % (port, baud), True)
        self.ctx.log("串口已打开：%s @ %d" % (port, baud), "ok")
        self._save_config()
        return True

    def close_serial(self, intentional=True):
        self._intentional_close = intentional
        self.reconnect_timer.stop()
        self.send_timer.stop()
        self.chk_loop.setChecked(False)
        if self.serial.isOpen():
            self.serial.close()
        self.btn_connect.setText("打开串口")
        self.btn_connect.setIcon(theme.load_icon("plug"))
        self._set_options_enabled(True)
        self._update_status("未连接")
        if intentional:
            self.ctx.log("串口已关闭")
        self._save_config()

    def _set_options_enabled(self, enabled):
        for widget in (self.cmb_port, self.cmb_baud, self.cmb_data, self.cmb_parity,
                       self.cmb_stop, self.cmb_flow):
            widget.setEnabled(enabled)

    def _update_status(self, text, ok=None):
        self.lbl_state.setText(text)
        if ok is True:
            self.lbl_state.setObjectName("BadgeOk")
        elif ok is False:
            self.lbl_state.setObjectName("BadgeErr")
        else:
            self.lbl_state.setObjectName("Badge")
        self.lbl_state.style().unpolish(self.lbl_state)
        self.lbl_state.style().polish(self.lbl_state)

    def _serial_error(self, error):
        if error == QSerialPort.NoError:
            return
        if error in (QSerialPort.ResourceError, QSerialPort.DeviceNotFoundError):
            message = self.serial.errorString()
            if self.serial.isOpen():
                self.serial.close()
            self.btn_connect.setText("打开串口")
            self._set_options_enabled(True)
            self._update_status("连接中断", False)
            self.ctx.log("串口中断：%s" % message, "warn")
            self._schedule_reconnect()

    def _line_signal_changed(self):
        if self.serial.isOpen():
            self.serial.setDataTerminalReady(self.chk_dtr.isChecked())
            self.serial.setRequestToSend(self.chk_rts.isChecked())

    def _schedule_reconnect(self):
        if self.chk_reconnect.isChecked() and not self._intentional_close:
            self._update_status("等待重连…", False)
            self.reconnect_timer.start()

    def _try_reconnect(self):
        self.refresh_ports()
        if not self.open_serial(silent=True):
            self._schedule_reconnect()

    # -------------------------------------------------------------- 接收/波形
    def _plugin_changed(self):
        plugin_id = self.cmb_plugin.currentData()
        if plugin_id not in self.plugins:
            return
        self.plugin = self.plugins[plugin_id]
        self.plugin.reset()
        self.lbl_plugin.setText(self.plugin.description)
        self._save_config()

    def _encoding_changed(self):
        codec = {"UTF-8": "utf-8", "GBK": "gbk", "ASCII": "ascii"}.get(
            self.cmb_encoding.currentText(), "utf-8")
        self._decoder = codecs.getincrementaldecoder(codec)("replace")

    def _read_ready(self):
        self.feed_received(bytes(self.serial.readAll()))

    def feed_received(self, data: bytes):
        """公开的数据入口，既用于真实串口，也用于测试和回放。"""
        if not data:
            return
        self.rx_bytes += len(data)
        self.raw_history.extend(data)
        if len(self.raw_history) > self.HISTORY_LIMIT:
            del self.raw_history[:-self.HISTORY_LIMIT]
        self._append_monitor(data)
        frames = self.plugin.feed(data)
        for frame in frames:
            self.wave.add_frame(dict(frame.values))
        self.lbl_samples.setText("%d 样本 / %d 通道" %
                                 (self.wave._sample_index, len(self.wave.channel_names())))
        self._update_counter()

    def _append_monitor(self, data: bytes):
        now = time.time()
        timestamp = (time.strftime("[%H:%M:%S", time.localtime(now)) +
                     ".%03d] " % int((now % 1) * 1000)) if self.chk_timestamp.isChecked() else ""
        if self.chk_rx_hex.isChecked():
            text = timestamp + " ".join("%02X" % b for b in data)
        else:
            text = self._decoder.decode(data)
            if timestamp:
                text = timestamp + text
        self.txt_rx.moveCursor(QTextCursor.End)
        self.txt_rx.insertPlainText(text)
        if self.chk_autoscroll.isChecked():
            bar = self.txt_rx.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _rerender_history(self):
        data = bytes(self.raw_history)
        self.txt_rx.clear()
        self._encoding_changed()
        if not data:
            return
        if self.chk_rx_hex.isChecked():
            lines = []
            for i in range(0, len(data), 16):
                block = data[i:i + 16]
                lines.append("%08X  %s" % (i, " ".join("%02X" % b for b in block)))
            self.txt_rx.setPlainText("\n".join(lines))
        else:
            self.txt_rx.setPlainText(self._decoder.decode(data, final=False))

    def clear_receive(self):
        self.raw_history.clear()
        self.rx_bytes = 0
        self.txt_rx.clear()
        self.plugin.reset()
        self._encoding_changed()
        self._update_counter()

    def save_receive(self):
        if not self.raw_history:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存串口数据", "serial_rx.bin",
                                              "二进制 (*.bin);;所有文件 (*)")
        if path:
            try:
                with open(path, "wb") as f:
                    f.write(self.raw_history)
                self.ctx.log("已保存串口数据 %d 字节 -> %s" %
                             (len(self.raw_history), path), "ok")
            except OSError as e:
                QMessageBox.warning(self, "保存失败", str(e))

    def _sync_channel_list(self):
        states = {self.lst_channels.item(i).data(Qt.UserRole):
                  self.lst_channels.item(i).checkState() == Qt.Checked
                  for i in range(self.lst_channels.count())}
        self.lst_channels.blockSignals(True)
        self.lst_channels.clear()
        for idx, name in enumerate(self.wave.channel_names()):
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setForeground(QColor(self.wave.channel_color(name)))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if states.get(name, True) else Qt.Unchecked)
            self.lst_channels.addItem(item)
        self.lst_channels.blockSignals(False)
        self.lbl_samples.setText("%d 样本 / %d 通道" %
                                 (self.wave._sample_index, len(self.wave.channel_names())))

    def _channel_changed(self, item):
        self.wave.set_channel_visible(str(item.data(Qt.UserRole)), item.checkState() == Qt.Checked)

    def choose_channel_color(self):
        item = self.lst_channels.currentItem()
        if item is None:
            return
        name = str(item.data(Qt.UserRole))
        selected = QColorDialog.getColor(QColor(self.wave.channel_color(name)), self,
                                         "选择 %s 的波形颜色" % name)
        if not selected.isValid():
            return
        self.wave.set_channel_color(name, selected.name())
        item.setForeground(selected)
        self._save_config()

    def reset_channel_colors(self):
        self.wave._colors.clear()
        for name in self.wave.channel_names():
            self.wave.channel_color(name)
        self._sync_channel_list()
        self.wave.update()
        self._save_config()

    def _toggle_wave_focus(self, checked):
        self.control_panel.setVisible(not checked)
        self.bottom_tabs.setVisible(not checked)
        self.monitor_box.setVisible(not checked)
        self.btn_wave_focus.setText("恢复布局" if checked else "放大波形")
        if not checked:
            self.serial_splitter.setSizes([680, 250])

    def _toggle_pause(self, checked):
        self.wave.set_paused(checked)
        self.btn_pause.setText("继续绘制" if checked else "暂停绘制")

    def _auto_scale_changed(self, checked):
        self.wave.auto_scale = checked
        self.wave.update()

    # ------------------------------------------------------------------ 发送
    def _write(self, payload: bytes):
        if not self.serial.isOpen():
            QMessageBox.information(self, "串口", "请先打开串口。")
            return False
        written = self.serial.write(payload)
        if written < 0:
            QMessageBox.warning(self, "发送失败", self.serial.errorString())
            return False
        self.tx_bytes += len(payload)
        self._update_counter()
        return True

    def send_manual(self):
        try:
            payload = encode_payload(self.txt_tx.toPlainText(), self.cmb_tx_mode.currentText(),
                                     self.cmb_encoding.currentText(), self.cmb_ending.currentText())
        except (ValueError, UnicodeError) as e:
            QMessageBox.warning(self, "发送数据", str(e))
            return
        if payload:
            self._write(payload)

    def _loop_toggled(self, checked):
        if checked:
            if not self.serial.isOpen():
                self.chk_loop.blockSignals(True)
                self.chk_loop.setChecked(False)
                self.chk_loop.blockSignals(False)
                QMessageBox.information(self, "定时发送", "请先打开串口。")
                return
            self.send_timer.start(self.sp_interval.value())
        else:
            self.send_timer.stop()

    def _update_counter(self):
        self.lbl_counter.setText("RX %d B  |  TX %d B" % (self.rx_bytes, self.tx_bytes))

    # -------------------------------------------------------------- 命令模板
    def _refresh_commands(self):
        commands = self.config.get("commands", [])
        self.tbl_commands.setRowCount(len(commands))
        for row, command in enumerate(commands):
            values = [command.get("group", ""), command.get("name", ""),
                      command.get("payload", ""), command.get("mode", "文本")]
            for col, value in enumerate(values):
                self.tbl_commands.setItem(row, col, QTableWidgetItem(str(value)))
        if commands and self.tbl_commands.currentRow() < 0:
            self.tbl_commands.selectRow(0)

    def add_command(self):
        dialog = CommandDialog(parent=self)
        if dialog.exec() == QDialog.Accepted:
            self.config.setdefault("commands", []).append(dialog.value())
            self._refresh_commands()
            self._save_config()

    def edit_command(self):
        row = self.tbl_commands.currentRow()
        commands = self.config.get("commands", [])
        if row < 0 or row >= len(commands):
            return
        dialog = CommandDialog(commands[row], self)
        if dialog.exec() == QDialog.Accepted:
            commands[row] = dialog.value()
            self._refresh_commands()
            self.tbl_commands.selectRow(row)
            self._save_config()

    def delete_command(self):
        row = self.tbl_commands.currentRow()
        commands = self.config.get("commands", [])
        if row < 0 or row >= len(commands):
            return
        if QMessageBox.question(self, "删除命令", "确定删除“%s”？" % commands[row]["name"]) == QMessageBox.Yes:
            del commands[row]
            self._refresh_commands()
            self._save_config()

    def send_selected_command(self, *_args):
        row = self.tbl_commands.currentRow()
        commands = self.config.get("commands", [])
        if row < 0 or row >= len(commands):
            return
        command = commands[row]
        values = {}
        for name in command_placeholders(str(command.get("payload", ""))):
            value, ok = QInputDialog.getText(self, "命令参数", "请输入 %s：" % name)
            if not ok:
                return
            values[name] = value
        try:
            text = render_command(str(command.get("payload", "")), values)
            payload = encode_payload(text, str(command.get("mode", "文本")),
                                     self.cmb_encoding.currentText(),
                                     str(command.get("ending", "无")))
        except (ValueError, UnicodeError) as e:
            QMessageBox.warning(self, "发送命令", str(e))
            return
        self._write(payload)

    def import_commands(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入命令模板", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            commands = data.get("commands", data) if isinstance(data, dict) else data
            if not isinstance(commands, list):
                raise ValueError("文件中没有命令列表")
            valid = [x for x in commands if isinstance(x, dict) and x.get("name")]
            self.config["commands"] = valid
            self._refresh_commands()
            self._save_config()
        except (OSError, ValueError, TypeError) as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def export_commands(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出命令模板", "serial_commands.json",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"commands": self.config.get("commands", [])}, f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def shutdown(self):
        self._save_config()
        self.close_serial(True)

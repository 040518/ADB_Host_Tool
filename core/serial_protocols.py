# -*- coding: utf-8 -*-
"""串口助手协议插件与命令编码。

这一层不依赖 Qt，方便单元测试，也方便后续增加新的帧格式。
内置插件兼容 VOFA+ 常用的 FireWater 文本协议与 JustFloat 协议。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping


@dataclass(frozen=True)
class SampleFrame:
    """一个同时刻的多通道样本。"""

    values: Mapping[str, float]


class ProtocolPlugin:
    """流式协议插件基类。"""

    plugin_id = "base"
    name = "基础插件"
    description = ""
    supports_waveform = False

    def reset(self) -> None:
        pass

    def feed(self, data: bytes) -> List[SampleFrame]:
        raise NotImplementedError


class FireWaterPlugin(ProtocolPlugin):
    """文本多通道插件。

    支持：
      1,2,3\n
      temp:25.1,hum:63.2\n
      voltage=3.3 current=0.12\n
    没有通道名时自动使用 CH1、CH2 ...。
    """

    plugin_id = "firewater"
    name = "FireWater（文本多通道）"
    description = "按行解析逗号/空格数值，支持 name:value 和 name=value"
    supports_waveform = True
    _named = re.compile(
        r"([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff.\-/]*)\s*[:=]\s*"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    )
    _number = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    @classmethod
    def parse_line(cls, text: str) -> Dict[str, float]:
        text = text.strip()
        if not text:
            return {}
        named = cls._named.findall(text)
        if named:
            return {name: float(value) for name, value in named}

        # 避免把日期、时间等普通日志误当成波形数据：未命名模式
        # 只接受“纯数字 + 分隔符”的整行。
        remainder = cls._number.sub("", text)
        if remainder.strip(" ,;\t|"):
            return {}
        values = cls._number.findall(text)
        return {"CH%d" % (i + 1): float(v) for i, v in enumerate(values)}

    def feed(self, data: bytes) -> List[SampleFrame]:
        self._buffer.extend(data)
        # 异常数据不能让未换行缓冲无限增长。
        if len(self._buffer) > 1024 * 1024:
            del self._buffer[:-65536]
        frames: List[SampleFrame] = []
        while True:
            pos_n = self._buffer.find(b"\n")
            pos_r = self._buffer.find(b"\r")
            positions = [p for p in (pos_n, pos_r) if p >= 0]
            if not positions:
                break
            pos = min(positions)
            line = bytes(self._buffer[:pos]).decode("utf-8", "replace")
            consume = pos + 1
            while consume < len(self._buffer) and self._buffer[consume] in (10, 13):
                consume += 1
            del self._buffer[:consume]
            parsed = self.parse_line(line)
            if parsed:
                frames.append(SampleFrame(parsed))
        return frames


class JustFloatPlugin(ProtocolPlugin):
    """VOFA+ JustFloat：若干 little-endian float32 + 00 00 80 7F。"""

    plugin_id = "justfloat"
    name = "JustFloat（VOFA+ 浮点帧）"
    description = "float32 小端数组，帧尾 00 00 80 7F"
    supports_waveform = True
    TAIL = b"\x00\x00\x80\x7f"

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> List[SampleFrame]:
        self._buffer.extend(data)
        frames: List[SampleFrame] = []
        while True:
            end = self._buffer.find(self.TAIL)
            if end < 0:
                break
            payload = bytes(self._buffer[:end])
            del self._buffer[:end + len(self.TAIL)]
            if not payload or len(payload) % 4:
                continue
            count = len(payload) // 4
            try:
                values = struct.unpack("<%df" % count, payload)
            except struct.error:
                continue
            frames.append(SampleFrame({"CH%d" % (i + 1): float(v)
                                       for i, v in enumerate(values)}))
        if len(self._buffer) > 4 * 1024 * 1024:
            # 长时间找不到帧尾时丢弃无效前缀，保留可能的半个帧尾。
            del self._buffer[:-3]
        return frames


class RawPlugin(ProtocolPlugin):
    plugin_id = "raw"
    name = "Raw（仅显示原始数据）"
    description = "不解析波形，适合文本/HEX 监视与自定义协议调试"
    supports_waveform = False

    def feed(self, data: bytes) -> List[SampleFrame]:
        return []


def builtin_plugins() -> List[ProtocolPlugin]:
    """返回每个页面独立使用的插件实例。"""

    return [FireWaterPlugin(), JustFloatPlugin(), RawPlugin()]


def decode_hex(text: str) -> bytes:
    """把 01 02、0x01,0x02 等用户输入解码为字节。"""

    cleaned = re.sub(r"0[xX]", "", text)
    cleaned = re.sub(r"[^0-9a-fA-F]", "", cleaned)
    if len(cleaned) % 2:
        raise ValueError("HEX 字符数为奇数，无法组成完整字节")
    return bytes.fromhex(cleaned) if cleaned else b""


LINE_ENDINGS = {
    "无": b"",
    "LF (\\n)": b"\n",
    "CR (\\r)": b"\r",
    "CRLF (\\r\\n)": b"\r\n",
}


def encode_payload(text: str, mode: str = "文本", encoding: str = "UTF-8",
                   ending: str = "无") -> bytes:
    if mode.upper() == "HEX":
        data = decode_hex(text)
    else:
        codec = {"UTF-8": "utf-8", "GBK": "gbk", "ASCII": "ascii"}.get(
            encoding.upper(), "utf-8")
        data = text.encode(codec)
    return data + LINE_ENDINGS.get(ending, b"")


_placeholder = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def command_placeholders(template: str) -> List[str]:
    """按出现顺序返回不重复的 {name} 占位符。"""

    result: List[str] = []
    for name in _placeholder.findall(template):
        if name not in result:
            result.append(name)
    return result


def render_command(template: str, values: Mapping[str, object]) -> str:
    missing = [name for name in command_placeholders(template) if name not in values]
    if missing:
        raise ValueError("缺少命令参数: %s" % ", ".join(missing))
    return _placeholder.sub(lambda m: str(values[m.group(1)]), template)


def frames_to_rows(frames: Iterable[SampleFrame]) -> List[Dict[str, float]]:
    """测试和非 GUI 使用时的便捷转换。"""

    return [dict(frame.values) for frame in frames]

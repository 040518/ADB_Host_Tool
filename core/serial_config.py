# -*- coding: utf-8 -*-
"""串口助手本地配置。"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict


DEFAULT_COMMANDS = [
    {"group": "常用", "name": "查询版本", "payload": "AT+VER?", "mode": "文本",
     "ending": "CRLF (\\r\\n)"},
    {"group": "常用", "name": "查询状态", "payload": "AT+STATUS?", "mode": "文本",
     "ending": "CRLF (\\r\\n)"},
    {"group": "参数", "name": "设置参数", "payload": "AT+SET={value}", "mode": "文本",
     "ending": "CRLF (\\r\\n)"},
]


def default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "serial": {
            "port": "", "baud": 115200, "data_bits": 8, "parity": "无",
            "stop_bits": "1", "flow_control": "无", "auto_reconnect": True,
            "dtr": False, "rts": False, "encoding": "UTF-8", "plugin": "firewater",
        },
        "channel_colors": {},
        "commands": [dict(item) for item in DEFAULT_COMMANDS],
    }


def config_path() -> str:
    base = os.environ.get("APPDATA") if os.name == "nt" else os.path.expanduser("~/.config")
    if not base:
        base = os.path.expanduser("~")
    directory = os.path.join(base, "ADB_Host_Tool")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "serial_assistant.json")


def load_config(path: str = "") -> Dict[str, Any]:
    cfg = default_config()
    target = path or config_path()
    try:
        with open(target, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            serial = saved.get("serial")
            if isinstance(serial, dict):
                cfg["serial"].update(serial)
            commands = saved.get("commands")
            if isinstance(commands, list):
                valid = [x for x in commands if isinstance(x, dict) and x.get("name")]
                cfg["commands"] = valid
            colors = saved.get("channel_colors")
            if isinstance(colors, dict):
                cfg["channel_colors"] = {str(k): str(v) for k, v in colors.items()}
    except (OSError, ValueError, TypeError):
        pass
    return cfg


def save_config(config: Dict[str, Any], path: str = "") -> tuple[bool, str]:
    target = path or config_path()
    directory = os.path.dirname(os.path.abspath(target))
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".serial_", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True, target
    except OSError as e:
        return False, str(e)

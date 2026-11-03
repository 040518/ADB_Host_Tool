# -*- coding: utf-8 -*-
"""生成界面所需的 SVG 图标（统一线性风格，可指定颜色）。

用法:  python tools/make_icons.py
输出:  resources/icons/<name>.svg（默认灰色）与 <name>_active.svg（强调蓝）
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "icons")

# 24x24 线性图标，stroke 使用占位符 {c}
ICONS = {
    # 设备 / 手机
    "device": '<rect x="7" y="2.5" width="10" height="19" rx="2.2"/>'
              '<line x1="10.5" y1="18.5" x2="13.5" y2="18.5"/>',
    "devices": '<rect x="2.5" y="4" width="13" height="10" rx="1.5"/>'
               '<path d="M6 17h6"/><rect x="16.5" y="9" width="5" height="11" rx="1.4"/>',
    # 终端
    "terminal": '<rect x="2.5" y="3.5" width="19" height="17" rx="2"/>'
                '<path d="M6.5 9l3 3-3 3"/><line x1="12" y1="15" x2="17" y2="15"/>',
    # 命令 / 代码
    "command": '<path d="M8 7l-4.5 5L8 17"/><path d="M16 7l4.5 5L16 17"/>'
               '<line x1="13.5" y1="5" x2="10.5" y2="19"/>',
    # 数据收发
    "data": '<path d="M4 8h11"/><path d="M12 5l3 3-3 3"/>'
            '<path d="M20 16H9"/><path d="M12 13l-3 3 3 3"/>',
    # TCP / 链路
    "tcp": '<path d="M9.5 14.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.2 1.2"/>'
           '<path d="M14.5 9.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.2-1.2"/>',
    # 文件管理
    "files": '<path d="M3 7.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2.5h8.5A1.5 1.5 0 0 1 20.5 10v8a1.5 1.5 0 0 1-1.5 1.5H4.5A1.5 1.5 0 0 1 3 18z"/>',
    # 日志
    "log": '<rect x="2.5" y="3.5" width="19" height="17" rx="2"/>'
           '<line x1="6" y1="8" x2="10" y2="8"/><line x1="6" y1="12" x2="14" y2="12"/>'
           '<line x1="6" y1="16" x2="17" y2="16"/>',
    # 截图 / 相机
    "camera": '<path d="M3 8.5A1.5 1.5 0 0 1 4.5 7h2.2l1.3-2h8l1.3 2h2.2A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>'
              '<circle cx="12" cy="13" r="3.2"/>',
    # 设置（滑块风格）
    "settings": '<line x1="4" y1="7" x2="20" y2="7"/><circle cx="9" cy="7" r="2.2"/>'
                '<line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2.2"/>'
                '<line x1="4" y1="17" x2="20" y2="17"/><circle cx="11" cy="17" r="2.2"/>',
    # 工具栏动作
    "refresh": '<path d="M20 12a8 8 0 1 1-2.34-5.66"/>'
               '<path d="M20 3.5V8h-4.5"/>',
    "plug": '<path d="M9 2.5v5M15 2.5v5"/>'
            '<path d="M6 7.5h12v3a6 6 0 0 1-12 0z"/><path d="M12 16.5v5"/>',
    "unplug": '<path d="M9 3v4M15 3v4"/><path d="M6.5 7h11v3.2a5.5 5.5 0 0 1-11 0z"/>'
              '<path d="M3.5 20.5L20.5 3.5"/>',
    "network": '<circle cx="12" cy="12" r="9"/>'
               '<path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>',
    "server": '<rect x="3" y="3.5" width="18" height="7" rx="1.6"/>'
              '<rect x="3" y="13.5" width="18" height="7" rx="1.6"/>'
              '<line x1="6.5" y1="7" x2="6.6" y2="7"/><line x1="6.5" y1="17" x2="6.6" y2="17"/>',
    "up": '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
    "down": '<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
    "folder": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h8A1.5 1.5 0 0 1 20 10v8a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3 18z"/>',
    "file": '<path d="M6 3.5h7l5 5v12H6z"/><path d="M13 3.5V9h5"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V4.5h6V7"/>'
             '<path d="M6 7l1 13h10l1-13"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "save": '<path d="M4 4.5h11L20 9.5v10H4z"/><path d="M8 4.5V10h7V4.5"/>',
    "send": '<path d="M3 11.5l18-8-8 18-2.5-7.5z"/>',
    "clear": '<path d="M4 6h16"/><path d="M8 6l1 14h6l1-14"/>'
             '<path d="M9 3.5h6"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="1.6"/>'
            '<path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
    "play": '<path d="M7 4.5l12 7.5-12 7.5z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "ssh": '<rect x="2.5" y="4" width="19" height="16" rx="2"/>'
           '<path d="M7 9.5l2.5 2.5L7 14.5"/><line x1="11.5" y1="14.5" x2="16" y2="14.5"/>'
           '<circle cx="18" cy="7" r="0.6"/>',
    "back": '<path d="M19 12H5"/><path d="M11 6l-6 6 6 6"/>',
    "forward": '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
    "updir": '<path d="M12 20V6"/><path d="M5.5 12.5L12 6l6.5 6.5"/>',
    # 控件装饰
    "chevron_down": '<path d="M6 9.5l6 6 6-6"/>',
    "chevron_up": '<path d="M6 14.5l6-6 6 6"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="2"/>'
            '<path d="M9 2.8v3M15 2.8v3M9 18.2v3M15 18.2v3'
            'M2.8 9h3M2.8 15h3M18.2 9h3M18.2 15h3"/>',
}

COLORS = {
    "": "#CCCCCC",
    "_active": "#4FC1FF",
    "_muted": "#8A8A8A",
}


def build_svg(body, color, size=24, stroke=1.6):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" fill="none" stroke="%s" stroke-width="%s" '
        'stroke-linecap="round" stroke-linejoin="round">%s</svg>'
        % (size, size, size, size, color, stroke, body)
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    count = 0
    # 这些图标会在 8~12px 下显示，笔画加粗才不会糊成一团
    SMALL = {"chevron_down", "chevron_up", "check"}
    for name, body in ICONS.items():
        stroke = 2.8 if name in SMALL else 1.6
        for suffix, color in COLORS.items():
            path = os.path.join(OUT, "%s%s.svg" % (name, suffix))
            with open(path, "w", encoding="utf-8") as f:
                f.write(build_svg(body, color, stroke=stroke))
            count += 1
    print("生成 %d 个 SVG 图标到 %s" % (count, OUT))


if __name__ == "__main__":
    main()
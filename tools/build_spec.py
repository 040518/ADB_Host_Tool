# -*- coding: utf-8 -*-
"""PyInstaller 打包配置（onefile，无控制台）。

用法：
    python build_spec.py            # 生成 ADB_Host_Tool.spec
或由 build_windows.bat 调用。
"""

import os
import sys

# 本脚本在 tools/ 下，工程根目录是它的上一级
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPEC = r'''# -*- mode: python ; coding: utf-8 -*-
"""由 tools/build_spec.py 生成，请勿手工修改。"""

import os
import sys

block_cipher = None
ROOT = r"{root}"

# 把主题、图标等静态资源打进包里，运行时通过 sys._MEIPASS 访问
datas = [
    (os.path.join(ROOT, "themes"), "themes"),
    (os.path.join(ROOT, "resources"), "resources"),
]

hiddenimports = [
    "pyte", "paramiko", "PySide6.QtSvg",
    "PySide6.QtNetwork", "PySide6.QtPrintSupport", "PySide6.QtSerialPort",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    # 去掉体积巨大且用不到的模块
    excludes=[
        "tkinter", "PyQt5", "PyQt6", "matplotlib", "numpy", "pandas",
        "PIL", "sqlite3", "pydoc_data", "test", "unittest",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtPdf",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Qt 6.12 在 Windows 上使用系统 ICU。打包环境的 PATH 可能还包含
# Poppler 的不兼容 ICU 78，PyInstaller 会把它误判为 Qt6Core 的依赖并收集，
# 导致启动时 `DLL load failed while importing QtCore`。过滤这两个外来库，
# 让 Windows 加载 System32 中与 Qt 兼容的 ICU。
_bad_icu = {{"icuuc.dll", "icudt78.dll"}}
a.binaries = [entry for entry in a.binaries
              if os.path.basename(entry[0]).lower() not in _bad_icu]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ADB_Host_Tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # 无黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "resources", "app.ico") if os.path.isfile(
        os.path.join(ROOT, "resources", "app.ico")) else None,
)
'''


def main():
    root = HERE
    spec_path = os.path.join(root, "ADB_Host_Tool.spec")
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(SPEC.format(root=root.replace("\\", "\\\\")))
    print("已生成:", spec_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

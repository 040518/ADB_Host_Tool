@echo off
REM ===========================================================================
REM  ADB 上位机 - Windows 打包脚本
REM  双击即可：检查环境 -> 装依赖 -> 清理 -> PyInstaller 打包 -> 复制到桌面
REM ===========================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ===========================================================================
echo   ADB 上位机 - 打包为单文件 EXE
echo ===========================================================================
echo.

REM ---------------------------------------------------------------- 1. Python
echo [1/7] 检查 Python 环境...
where python >nul 2>nul
if errorlevel 1 (
    echo    [错误] 未找到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo     !PYVER!

REM ---------------------------------------------------------------- 2. 依赖
echo [2/7] 检查依赖包...
python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo     安装 PySide6 ...
    python -m pip install "PySide6>=6.5" -q
    if errorlevel 1 (
        echo     [错误] PySide6 安装失败
        pause
        exit /b 1
    )
)
python -c "import pyte" >nul 2>nul
if errorlevel 1 (
    echo     安装 pyte ...
    python -m pip install pyte -q
)
python -c "import paramiko" >nul 2>nul
if errorlevel 1 (
    echo     安装 paramiko ...
    python -m pip install paramiko -q
)
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo     安装 PyInstaller ...
    python -m pip install pyinstaller -q
    if errorlevel 1 (
        echo     [错误] PyInstaller 安装失败
        pause
        exit /b 1
    )
)
echo     依赖齐全

REM ---------------------------------------------------------------- 3. 资源
echo [3/7] 生成图标与资源...
python tools\make_icons.py
if errorlevel 1 (
    echo    [警告] 图标生成失败，继续使用现有图标
)
if not exist "resources\app.ico" (
    python tools\make_icon_ico.py
)

REM ---------------------------------------------------------------- 4. 清理
echo [4/7] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist ADB_Host_Tool.spec del /q ADB_Host_Tool.spec
echo     已清理 build\ dist\

REM ---------------------------------------------------------------- 5. 打包
echo [5/7] 生成 spec 并打包（首次约 1-3 分钟）...
python tools\build_spec.py
if errorlevel 1 (
    echo    [错误] 生成 spec 失败
    pause
    exit /b 1
)
python -m PyInstaller --noconfirm --clean ADB_Host_Tool.spec
if errorlevel 1 (
    echo.
    echo    [错误] PyInstaller 打包失败，请查看上面的日志
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- 6. 校验
echo.
echo [6/7] 检查产物...
if not exist "dist\ADB_Host_Tool.exe" (
    echo    [错误] 未生成 dist\ADB_Host_Tool.exe
    pause
    exit /b 1
)
for %%f in ("dist\ADB_Host_Tool.exe") do (
    set /a SIZEMB=%%~zf/1048576
    echo     生成成功: dist\ADB_Host_Tool.exe  ^(!SIZEMB! MB^)
)

REM ---------------------------------------------------------------- 7. 桌面
echo [7/7] 复制到桌面...
REM 优先用系统 API 取真实桌面（支持 OneDrive 重定向），退回 %USERPROFILE%\Desktop
set "DESKTOP="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%d"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "!DESKTOP!" (
    echo     [警告] 桌面目录不存在: !DESKTOP!
    echo     产物保留在: %CD%\dist\ADB_Host_Tool.exe
    pause
    exit /b 0
)
copy /y "dist\ADB_Host_Tool.exe" "!DESKTOP!\ADB_Host_Tool.exe" >nul
if errorlevel 1 (
    echo     [错误] 复制到桌面失败
    echo     产物保留在: %CD%\dist\ADB_Host_Tool.exe
    pause
    exit /b 1
)
echo     已复制到: !DESKTOP!\ADB_Host_Tool.exe

echo.
echo ===========================================================================
echo   打包完成
echo ===========================================================================
echo   桌面文件: !DESKTOP!\ADB_Host_Tool.exe
echo   构建目录: %CD%\dist\
echo.
echo   直接双击桌面上的 ADB_Host_Tool.exe 即可启动。
echo ===========================================================================
echo.
pause
endlocal
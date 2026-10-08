@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title FinAgent 一键安装依赖

rem ==== 依赖源：默认走清华镜像（国内直连 PyPI 常常停滞）====
rem 如需改用官方源或其它镜像，修改下面这行即可。镜像只影响下载渠道，
rem 安装的版本仍由 requirements.txt 固定。
set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIPOPT=--timeout 30 --retries 3 --disable-pip-version-check"

echo ============================================================
echo   FinAgent 环境准备
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

echo [1/4] 检查 Python 版本……
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo.
    echo [错误] 需要 Python 3.10 或更高版本，当前版本为：
    python --version
    echo.
    pause
    exit /b 1
)
python --version

echo.
echo [2/4] 创建隔离环境 .venv……
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    echo        创建完成。
) else (
    echo        已存在，跳过创建。
)

echo.
echo [3/4] 安装依赖（版本已在 requirements.txt 中固定）……
".venv\Scripts\python.exe" -m pip install --upgrade pip %MIRROR% %PIPOPT%
".venv\Scripts\python.exe" -m pip install -r requirements.txt %MIRROR% %PIPOPT%
if errorlevel 1 (
    echo.
    echo [提示] 镜像安装未成功，改用官方 PyPI 重试……
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --timeout 60 --retries 5
    if errorlevel 1 goto PIPFAIL
)

echo.
echo [4/4] 环境自检……
".venv\Scripts\python.exe" tests\check_env.py
if errorlevel 1 (
    echo.
    echo [警告] 自检未通过，请把上面的输出发给队友排查。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   安装完成。以后双击「启动FinAgent应用.bat」即可使用。
echo ============================================================
echo.
pause
exit /b 0

:PIPFAIL
echo.
echo [错误] 依赖安装失败。
echo        请检查网络后重新运行本脚本；若使用校园网，可尝试换用其它镜像，例如：
echo        .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
echo.
pause
exit /b 1

:NOPYTHON
echo.
echo [错误] 未找到 python 命令。
echo        请安装 Python 3.10 或更高版本，安装时务必勾选
echo        "Add python.exe to PATH"，然后重新运行本脚本。
echo.
pause
exit /b 1

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title FinAgent 应用服务（局域网共享）

rem ==== 让 Python 以 UTF-8 输出，避免中文乱码 ====
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"

rem ==== 大模型配置：未显式设置时给出默认值 ====
if not defined FINAGENT_BASE_URL set "FINAGENT_BASE_URL=https://api.deepseek.com/v1"
if not defined FINAGENT_MODEL set "FINAGENT_MODEL=deepseek-chat"
if not defined FINAGENT_API_KEY if defined DEEPSEEK_API_KEY set "FINAGENT_API_KEY=%DEEPSEEK_API_KEY%"

rem ==== 优先使用项目自带的隔离环境 .venv ====
set "PYBIN="
if exist ".venv\Scripts\python.exe" set "PYBIN=.venv\Scripts\python.exe"
if not defined PYBIN (
    where python >nul 2>nul
    if errorlevel 1 goto NOPYTHON
    set "PYBIN=python"
)

echo 正在以局域网共享模式启动，本机浏览器将自动打开……
echo.
echo 队友无需安装任何东西：把下面列出的「队友打开」地址发给他们即可。
echo 若首次启动弹出 Windows 防火墙提示，请勾选「专用网络」并允许访问。
echo.
echo 注意：同一时刻只允许一次分析运行。队友正在跑时请稍候，界面会提示。
echo.

"%PYBIN%" "app\server.py" --port 8765 --host 0.0.0.0

echo.
echo 服务已退出。
pause
exit /b 0

:NOPYTHON
echo.
echo [错误] 未找到 python 命令，项目里也没有 .venv 环境。
echo        请先安装 Python 3.10 或更高版本，安装时务必勾选
echo        "Add python.exe to PATH"，然后双击「一键安装依赖.bat」。
echo.
pause
exit /b 1

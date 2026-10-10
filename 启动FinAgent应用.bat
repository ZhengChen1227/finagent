@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title FinAgent 应用服务

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

if not defined FINAGENT_API_KEY (
    echo.
    echo [提示] 尚未检测到 DeepSeek API Key。
    echo        这不影响启动：应用会正常打开，请在界面右上角「设置」里
    echo        填入你自己的 DeepSeek API Key，然后上传财报并开始分析。
)

echo 正在启动 FinAgent 应用，浏览器将自动打开……
echo 关闭本窗口或按 Ctrl+C 即可停止服务。
echo.

"%PYBIN%" "app\server.py" --port 8765

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

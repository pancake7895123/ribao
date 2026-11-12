@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 工程计划识别 - 后端日志
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo ============================================
echo   工程计划识别 - 本地启动器
echo ============================================
echo.

if not exist "app.py" (
  echo [错误] 没有找到 app.py。
  echo 请把本脚本放在项目文件夹中。
  pause
  exit /b 1
)

if not exist ".env" (
  echo [错误] 没有找到 .env。
  echo 请先复制 .env.example 为 .env，并填写 DEEPSEEK_API_KEY。
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
  where python >nul 2>&1
  if errorlevel 1 (
    echo [错误] 找不到 python。
    echo 请先安装 Python 3.10+，或在项目目录创建 .venv。
    pause
    exit /b 1
  )
)

echo 检查后端是否已经在运行...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/health' -TimeoutSec 2; if ($r.service -eq 'engineering-plan-ocr') { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  echo 后端已经在运行，直接打开浏览器。
  start "" "http://127.0.0.1:5000"
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

echo 正在启动后端...
echo 服务就绪后会自动打开浏览器，请不要关闭本窗口。
echo.

rem 后台等待健康检查通过后打开浏览器，同时让 app.py 留在当前窗口输出日志。
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$url='http://127.0.0.1:5000/api/health'; for ($i=0; $i -lt 120; $i++) { try { $r = Invoke-RestMethod -Uri $url -TimeoutSec 2; if ($r.service -eq 'engineering-plan-ocr') { Start-Process 'http://127.0.0.1:5000'; exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }" >nul 2>&1

"%PYTHON%" app.py
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [错误] 后端已退出，退出码 %EXITCODE%。
  echo 常见原因：端口 5000 被占用、依赖未安装、API Key 未配置。
  echo 请根据上方的 Python 报错信息排查。
  pause
)

exit /b %EXITCODE%

@echo off
rem Signal Desk 盯盘面板 —— 双击即可运行。
rem 放在项目根目录。双击后启动「盯盘 + 落盘 + 面板」，并自动打开浏览器。
chcp 65001 >nul
cd /d "%~dp0"
title Signal Desk 盯盘中（关掉这个窗口就是停止）

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   [X] 找不到 .venv\Scripts\python.exe
  echo.
  echo   这台机器还没建虚拟环境。先在本目录执行一次：
  echo       py -3.12 -m venv .venv
  echo       .venv\Scripts\python.exe -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

rem 8000 已被占用多半是已经有一个在跑了。再起一个会抢同一个 runtime.sqlite3。
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo   [!] 8000 端口已被占用 —— 面板可能已经在运行了。
  echo       先看看浏览器： http://127.0.0.1:8000
  echo       确实要重开，先关掉那个窗口。
  echo.
  pause
  exit /b 1
)

rem 等服务端起来再开浏览器 —— 直接开会看到"无法连接"。
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 8; Start-Process 'http://127.0.0.1:8000'"

echo.
echo   面板地址： http://127.0.0.1:8000   （约 8 秒后自动打开）
echo   停止：关掉这个窗口，或按 Ctrl+C
echo.

.venv\Scripts\python.exe scripts\watch.py --web 127.0.0.1:8000

rem 走到这里说明进程退出了（正常停止或崩了）。留住窗口好看清原因。
echo.
echo   ---- 盯盘已停止 ----
pause

@echo off
rem Signal Desk 更新 —— 双击即可拉取最新代码并自检。
rem **更新前必须先停掉盯盘**（关掉「启动面板」那个窗口）。
chcp 65001 >nul
cd /d "%~dp0"
title Signal Desk 更新中

if not exist ".git" (
  echo.
  echo   [X] 这个目录不是 git 仓库，没法用 git 更新。
  echo       多半是从 GitHub 下载 ZIP 解压来的。转换方法见
  echo       docs\RUN-WINDOWS.md 的「五、更新到最新版」。
  echo.
  pause
  exit /b 1
)

rem 正在写盘时更新代码会出事
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo   [X] 面板还在运行（8000 端口被占用）。
  echo       请先关掉「启动面板」那个窗口，再回来双击本文件。
  echo.
  pause
  exit /b 1
)

echo.
echo   [1/4] 检查本地改动 ...
rem 注意用 `for %%i in (文件)` 取大小，不能用 `for /f` —— 那是读文件内容。
git status --porcelain > "%TEMP%\sigdesk_status.txt"
set SZ=0
for %%i in ("%TEMP%\sigdesk_status.txt") do set SZ=%%~zi
if not "%SZ%"=="0" (
  echo.
  echo   [!] 本地有未提交的改动：
  type "%TEMP%\sigdesk_status.txt"
  echo.
  echo       直接 git pull 可能冲突或覆盖。请先确认这些改动要不要保留。
  echo.
  pause
  exit /b 1
)

echo   [2/4] 拉取最新代码 ...
git pull
if errorlevel 1 goto failed

echo.
echo   [3/4] 更新依赖 ...
.venv\Scripts\python.exe -m pip install -e ".[dev]" --quiet
if errorlevel 1 goto failed

echo.
echo   [4/4] 自检（全部测试）...
.venv\Scripts\python.exe -m pytest -q
if errorlevel 1 goto failed

echo.
echo   ==== 更新完成 ====
echo   CHANGELOG.md 里写着这次改了什么、有没有需要额外操作的。
echo   现在可以双击「启动面板.bat」了。
echo.
pause
exit /b 0

:failed
echo.
echo   ==== 上面这一步失败了 ====
echo   把窗口里的完整输出发给开发确认，先别启动。
echo.
pause
exit /b 1

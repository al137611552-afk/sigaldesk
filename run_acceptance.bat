@echo off
REM signal-desk 离线验收。全程不联网、不需要凭据、不下单。
REM   run_acceptance.bat              跑全部，输出同时写进 acceptance-log.txt
REM   run_acceptance.bat --pause      跑完停住不关窗（双击运行时加这个）
REM   run_acceptance.bat --no-visual  跳过截图组
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 控制台改 UTF-8：脚本输出含 ✅ 这类字符，cp936 下会 UnicodeEncodeError
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM --pause 只影响收尾，不传给 Python
set DOPAUSE=0
set ARGS=
for %%A in (%*) do (
  if /i "%%~A"=="--pause" (set DOPAUSE=1) else (set ARGS=!ARGS! %%~A)
)

REM ---- 找一个 >=3.12 的 Python。系统默认的 python 常常是 3.11 甚至 3.8 ----
set PYEXE=
for %%C in ("py -3.14" "py -3.13" "py -3.12" "py -3" "python3" "python") do (
  if not defined PYEXE (
    %%~C -c "import sys;raise SystemExit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYEXE=%%~C"
  )
)
if not defined PYEXE (
  echo [错误] 找不到 Python 3.12+。
  echo        装一个 3.12 及以上版本，或先激活合适的环境再运行本脚本。
  echo        已尝试: py -3.14 / py -3.13 / py -3.12 / py -3 / python3 / python
  if "%DOPAUSE%"=="1" pause
  exit /b 1
)
for /f "delims=" %%V in ('%PYEXE% -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%V
echo 使用 Python %PYVER%（%PYEXE%）

REM ---- 虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
  echo [1/2] 建虚拟环境并装依赖（首次约 1-2 分钟）...
  %PYEXE% -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -e ".[dev]" || goto :fail
) else (
  echo [1/2] 虚拟环境已存在，跳过安装
)

echo [2/2] 跑验收...
echo.
REM 落盘由 acceptance.py 自己做（--log）。**不要用管道 tee** ——
REM cmd 的管道会让 %ERRORLEVEL% 变成管道最后一个命令的返回值，退出码就丢了。
.venv\Scripts\python scripts\acceptance.py %ARGS%
set CODE=%ERRORLEVEL%

echo.
if "%CODE%"=="0" (echo 结果：没有失败项) else (echo 结果：有失败项，见上面 ❌ 行)
echo 完整输出已保存到 acceptance-log.txt
REM **默认不 pause** —— 在 agent / CI 的非交互 shell 里 pause 会永远挂住。
REM 双击运行想让窗口留住，请用 run_acceptance.bat --pause
if "%DOPAUSE%"=="1" pause
exit /b %CODE%

:fail
echo [错误] 环境准备失败，见上面输出。
if "%DOPAUSE%"=="1" pause
exit /b 1

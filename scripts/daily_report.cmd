@echo off
rem 每日选股报告：先打开指南针更新活跃市值，再生成 HTML 报告并交给 DSH 工作台。
rem 给 Windows 任务计划在工作日收盘后调用（例如 20:00），也可以双击手动跑。
rem 日志目录：.env 里 REPORT_OUT_DIR 下的 logs；没配置时用技能目录下的 data\logs。
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set REPORT_OUT_DIR=
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /c:"REPORT_OUT_DIR=" .env 2^>nul`) do set REPORT_OUT_DIR=%%b
if defined REPORT_OUT_DIR (set "LOGDIR=%REPORT_OUT_DIR:/=\%\logs") else (set "LOGDIR=%~dp0..\data\logs")
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
echo ===== %date% %time% start >> "%LOGDIR%\%TODAY%.log"
".venv\Scripts\python.exe" -m modules.cli report --refresh-amv >> "%LOGDIR%\%TODAY%.log" 2>&1
echo ===== %date% %time% exit %errorlevel% >> "%LOGDIR%\%TODAY%.log"
endlocal

@echo off
REM QMT 模拟盘 — 15:30 收盘成交回报脚本（与模块一同批触发）

set PROJECT_ROOT=C:\parttime\qmt模拟盘pipeline\server
set VENV=C:\parttime\qmt数据推送\venv
set LOGDIR=%PROJECT_ROOT%\logs
set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call "%VENV%\Scripts\activate.bat"
cd /d "%PROJECT_ROOT%"
python -m src.trade_result --stage close --today %TODAY% --config config\settings.yaml >> "%LOGDIR%\trade_close_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%

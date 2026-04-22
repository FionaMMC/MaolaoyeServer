@echo off
REM QMT 模拟盘 — 09:10 竞价下单触发脚本
REM Windows 任务计划程序配置：每交易日 09:10 执行一次

set PROJECT_ROOT=C:\parttime\qmt模拟盘pipeline\server
set VENV=C:\parttime\qmt数据推送\venv
set LOGDIR=%PROJECT_ROOT%\logs
set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call "%VENV%\Scripts\activate.bat"
cd /d "%PROJECT_ROOT%"
python -m src.auction_order --today %TODAY% --config config\settings.yaml >> "%LOGDIR%\auction_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%

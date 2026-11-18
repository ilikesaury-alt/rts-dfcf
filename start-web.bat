@echo off
REM 网页版一键启动（启动台 8901；面板 8899 由扫描器自己托管）。
REM 关掉本窗口 = 停掉启动台；扫描器要停请到它自己的日志/页面里点「停止」。
cd /d "%~dp0"
python -m scanner.web_launcher
pause

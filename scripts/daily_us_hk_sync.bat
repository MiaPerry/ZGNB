@echo off
rem ZGNB 每日美股/港股增量同步（由 Windows 任务计划程序每日 08:00 调用）
rem 等价于前端"同步最新数据"按钮：Yahoo 日线增量 + 指标补算
cd /d D:\myProject\ZGNB-main\ZGNB-main
if not exist data\logs mkdir data\logs
C:\Python311\python.exe scripts\daily_us_hk_sync.py 1>>data\logs\daily_us_hk_sync.cron.log 2>&1
exit /b %ERRORLEVEL%

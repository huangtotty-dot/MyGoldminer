@echo off
cd /d C:\Users\Lenovo\.goldminer3\projects\e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7
echo ========================================
echo  掘金模拟盘监控启动
echo ========================================
start "GM Watcher" python gm_bridge/watcher.py
echo Watcher 已启动 (独立窗口)
echo 按任意键关闭本窗口 (watcher 继续运行)
pause > nul

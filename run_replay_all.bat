@echo off
set PY=C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe
cd /d C:\Users\Lenovo\.goldminer3\projects\e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7
%PY% -u replay_wp9.py fix3 > "docs\回测复盘\回放包_WP-B\fix3\run.log" 2>&1
echo FIX3_DONE >> "docs\回测复盘\回放包_WP-B\progress.txt"
%PY% -u replay_wp9.py fix2 > "docs\回测复盘\回放包_WP-B\fix2\run.log" 2>&1
echo FIX2_DONE >> "docs\回测复盘\回放包_WP-B\progress.txt"
%PY% -u replay_wp9.py fix4 > "docs\回测复盘\回放包_WP-B\fix4\run.log" 2>&1
echo FIX4_DONE >> "docs\回测复盘\回放包_WP-B\progress.txt"
echo ALL_DONE >> "docs\回测复盘\回放包_WP-B\progress.txt"

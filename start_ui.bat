@echo off
REM 启动可视化界面（双击运行）。停止：关闭本窗口。
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ===============================================
echo  个人投资决策工作流 - 可视化界面
echo  浏览器地址: http://127.0.0.1:8765/
echo  关闭本窗口即可停止服务
echo ===============================================
python -m advisor.cli ui --port 8765
pause

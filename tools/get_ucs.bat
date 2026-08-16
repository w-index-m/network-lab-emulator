@echo off
chcp 65001 > nul
echo ============================================
echo  BIG-IP / F5OS  UCS / バックアップ 一括取得
echo ============================================

set SCRIPT=%~dp0bigip_qkview_collector.py
set HOSTS=%~dp0hosts.txt

if not exist "%HOSTS%" (
    echo [ERROR] hosts.txt が見つかりません: %HOSTS%
    pause
    exit /b 1
)

python "%SCRIPT%" "%HOSTS%" --mode ucs %*

echo.
echo 完了しました。Enter で閉じます。
pause

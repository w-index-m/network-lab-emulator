@echo off
chcp 65001 > nul
echo ============================================
echo  Network Lab Emulator - Prometheus Exporter
echo ============================================

set SCRIPT=%~dp0prometheus_exporter.py

REM 必要に応じて書き換えてください
set EMULATOR_URL=http://localhost:8000
set EXPORT_PORT=9877

echo エミュレーター  : %EMULATOR_URL%
echo 公開ポート      : %EXPORT_PORT%
echo.
echo Prometheus の scrape_configs には以下を追加してください:
echo   - job_name: 'netlab-emulator'
echo     static_configs:
echo       - targets: ['localhost:%EXPORT_PORT%']
echo.

python "%SCRIPT%" --emulator-url %EMULATOR_URL% --port %EXPORT_PORT% %*

echo.
echo 終了しました。Enter で閉じます。
pause

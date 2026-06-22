@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Network Lab - プロトコル動作デモ

:menu
cls
echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║   Network Lab  プロトコル動作デモ               ║
echo  ║   ネットワーク教育資料サンプル                  ║
echo  ╚══════════════════════════════════════════════════╝
echo.
echo  実行するデモを選んでください:
echo.
echo    [1] RIP  動作デモ  （Si-R ↔ Catalyst）
echo    [2] OSPF 動作デモ  （DR/BDR選出・LSA交換）
echo    [3] BGP  動作デモ  （AS間セッション確立）
echo    [4] 全プロトコル動作確認（sample_configs.py）
echo.
echo    [0] エミュレーター本体を起動（http://localhost:8000）
echo    [Q] 終了
echo.
set /p CHOICE="番号を入力してください > "

if "%CHOICE%"=="1" goto demo_rip
if "%CHOICE%"=="2" goto demo_ospf
if "%CHOICE%"=="3" goto demo_bgp
if "%CHOICE%"=="4" goto demo_all
if "%CHOICE%"=="0" goto start_server
if /i "%CHOICE%"=="Q" goto end
goto menu

:demo_rip
cls
echo.
echo  ▶ RIP 動作デモを開始します...
echo  ─────────────────────────────────────────────────
echo.
python demo_rip.py
echo.
echo  ─────────────────────────────────────────────────
echo  デモが終了しました。何かキーを押してメニューに戻ります。
pause > nul
goto menu

:demo_ospf
cls
echo.
echo  ▶ OSPF 動作デモを開始します...
echo  ─────────────────────────────────────────────────
echo.
python demo_ospf.py
echo.
echo  ─────────────────────────────────────────────────
echo  デモが終了しました。何かキーを押してメニューに戻ります。
pause > nul
goto menu

:demo_bgp
cls
echo.
echo  ▶ BGP 動作デモを開始します...
echo  ─────────────────────────────────────────────────
echo.
python demo_bgp.py
echo.
echo  ─────────────────────────────────────────────────
echo  デモが終了しました。何かキーを押してメニューに戻ります。
pause > nul
goto menu

:demo_all
cls
echo.
echo  ▶ 全プロトコル動作確認を開始します...
echo  ─────────────────────────────────────────────────
echo.
python sample_configs.py
echo.
echo  ─────────────────────────────────────────────────
echo  完了しました。何かキーを押してメニューに戻ります。
pause > nul
goto menu

:start_server
cls
echo.
echo  ▶ エミュレーター本体を起動します...
echo    ブラウザで http://localhost:8000 を開いてください
echo    停止するには Ctrl+C を押してください
echo.
python app.py
pause
goto menu

:end
echo.
echo  終了します。
exit /b 0

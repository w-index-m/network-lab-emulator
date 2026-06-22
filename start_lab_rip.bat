@echo off
chcp 65001 > nul
cd /d "%~dp0"
title RIP ラボ実習

cls
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   RIP ラボ実習  起動中...                           ║
echo  ║   Routing Information Protocol                      ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

:: ── Python確認 ──
set PYTHON=
python --version > nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=python
    goto python_ok
)
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do (
  if exist %%P ( set PYTHON=%%P && goto python_ok )
)
echo  [エラー] Python が見つかりません。
echo  https://www.python.org/ からインストールしてください。
pause & exit /b 1

:python_ok
echo  [OK] Python 確認済み

:: ── パッケージ確認 ──
%PYTHON% -c "import fastapi" > nul 2>&1
if %errorlevel% neq 0 (
    echo  [--] パッケージをインストール中...
    %PYTHON% -m pip install -r requirements.txt -q
)
echo  [OK] パッケージ確認済み

:: ── ラボ初期コンフィグ投入 ──
echo.
echo  [--] RIP ラボ環境を初期化中...
%PYTHON% -c "
import os,sys,asyncio
os.environ['NETLAB_FAST_TIMERS']='1'
sys.path.insert(0,'.')
import app
from engine.rules import DeviceState
from engine.protocols import vnet

async def init():
    # Router-A (Si-R) 作成
    app.device_sessions['router-a'] = DeviceState('sir','Router-A')
    app._register_stub('router-a')
    app._register_icmp('router-a')
    # Dist-SW (Catalyst) 作成
    app.device_sessions['dist-sw'] = DeviceState('catalyst','Dist-SW')
    app._register_stub('dist-sw')
    app._register_icmp('dist-sw')
    # リンク接続
    vnet.add_link('router-a','dist-sw')
    print('初期化完了')

asyncio.run(init())
" > nul 2>&1
echo  [OK] ラボ環境 初期化完了

:: ── サーバーをバックグラウンドで起動 ──
echo.
echo  [--] エミュレーターサーバーを起動中...
start "" /min %PYTHON% app.py

:: サーバー起動待ち
timeout /t 3 /nobreak > nul

:: ── ブラウザを開く ──
echo  [OK] サーバー起動完了
echo.
echo  ┌─────────────────────────────────────────────────────┐
echo  │  ブラウザが2つ開きます:                             │
echo  │                                                     │
echo  │  1. エミュレーター  http://localhost:8000           │
echo  │     ← ここでコマンドを入力します                   │
echo  │                                                     │
echo  │  2. ラボ課題シート  http://localhost:8000/lab_rip  │
echo  │     ← 実習手順と確認コマンドを参照してください     │
echo  └─────────────────────────────────────────────────────┘
echo.

:: エミュレーター
start "" "http://localhost:8000"
timeout /t 1 /nobreak > nul
:: ラボ課題シート
start "" "http://localhost:8000/static/lab_rip.html"

echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  RIP ラボ実習を開始してください。
echo.
echo  【装置構成】
echo    Router-A  (Si-R G120)     - 左側のパネルで操作
echo    Dist-SW   (Catalyst 9300) - 右側のパネルで操作
echo.
echo  【実習の進め方】
echo    ラボ課題シートの Step 1 から順に設定してください。
echo    コマンドをコピーしてエミュレーターに貼り付けられます。
echo.
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  終了するには このウィンドウを閉じてください。
echo  （サーバーはタスクトレイに最小化されています）
echo.
pause

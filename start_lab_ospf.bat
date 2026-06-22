@echo off
chcp 65001 > nul
cd /d "%~dp0"
title OSPF ラボ実習

cls
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   OSPF ラボ実習  起動中...                          ║
echo  ║   Catalyst 9300  ↔  Catalyst 9300                  ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

set PYTHON=
python --version > nul 2>&1
if %errorlevel%==0 ( set PYTHON=python && goto python_ok )
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do ( if exist %%P ( set PYTHON=%%P && goto python_ok ) )
echo  [エラー] Python が見つかりません。
pause & exit /b 1

:python_ok
echo  [OK] Python 確認済み
%PYTHON% -c "import fastapi" > nul 2>&1
if %errorlevel% neq 0 ( %PYTHON% -m pip install -r requirements.txt -q )
echo  [OK] パッケージ確認済み

echo  [--] OSPF ラボ環境を初期化中...
%PYTHON% -c "
import os,sys,asyncio
os.environ['NETLAB_FAST_TIMERS']='1'
sys.path.insert(0,'.')
import app
from engine.rules import DeviceState
from engine.protocols import vnet

async def init():
    app.device_sessions['sw1'] = DeviceState('catalyst','SW1')
    app._register_stub('sw1'); app._register_icmp('sw1')
    app.device_sessions['sw2'] = DeviceState('catalyst','SW2')
    app._register_stub('sw2'); app._register_icmp('sw2')
    vnet.add_link('sw1','sw2')
    print('初期化完了')

asyncio.run(init())
" > nul 2>&1
echo  [OK] ラボ環境 初期化完了（SW1 ↔ SW2 接続済み）

echo.
start "" /min %PYTHON% app.py
timeout /t 3 /nobreak > nul

echo  ┌─────────────────────────────────────────────────────┐
echo  │  ブラウザが2つ開きます:                             │
echo  │                                                     │
echo  │  1. エミュレーター   http://localhost:8000          │
echo  │  2. ラボ課題シート   (OSPF課題・確認コマンド)      │
echo  └─────────────────────────────────────────────────────┘

start "" "http://localhost:8000"
timeout /t 1 /nobreak > nul
start "" "http://localhost:8000/static/lab_ospf.html"

echo.
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  OSPF ラボ実習を開始してください。
echo.
echo  【装置構成】
echo    SW1 (Catalyst 9300)  priority=200 → DR候補
echo    SW2 (Catalyst 9300)  priority=100 → BDR候補
echo.
echo  【実習のポイント】
echo    ・OSPFのnetworkコマンドはワイルドカードマスクで指定
echo    ・priority設定でDR/BDRを制御できる
echo    ・show ip ospf neighbor でFull/DR, Full/BDRを確認
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  終了するには このウィンドウを閉じてください。
echo.
pause

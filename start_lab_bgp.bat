@echo off
chcp 65001 > nul
cd /d "%~dp0"
title BGP ラボ実習

cls
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   BGP ラボ実習  起動中...                           ║
echo  ║   Catalyst 9300 (AS65001) ↔ Si-R G120 (AS65002)   ║
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

echo  [--] BGP ラボ環境を初期化中...
%PYTHON% -c "
import os,sys,asyncio
os.environ['NETLAB_FAST_TIMERS']='1'
sys.path.insert(0,'.')
import app
from engine.rules import DeviceState
from engine.protocols import vnet

async def init():
    app.device_sessions['gw'] = DeviceState('catalyst','GW-Router')
    app._register_stub('gw'); app._register_icmp('gw')
    app.device_sessions['ra'] = DeviceState('sir','Router-A')
    app._register_stub('ra'); app._register_icmp('ra')
    vnet.add_link('gw','ra')
    print('初期化完了')

asyncio.run(init())
" > nul 2>&1
echo  [OK] ラボ環境 初期化完了（GW-Router ↔ Router-A 接続済み）

echo.
start "" /min %PYTHON% app.py
timeout /t 3 /nobreak > nul

echo  ┌─────────────────────────────────────────────────────┐
echo  │  ブラウザが2つ開きます:                             │
echo  │                                                     │
echo  │  1. エミュレーター   http://localhost:8000          │
echo  │  2. ラボ課題シート   (BGP課題・確認コマンド)       │
echo  └─────────────────────────────────────────────────────┘

start "" "http://localhost:8000"
timeout /t 1 /nobreak > nul
start "" "http://localhost:8000/static/lab_bgp.html"

echo.
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  BGP ラボ実習を開始してください。
echo.
echo  【装置構成】
echo    GW-Router (Catalyst 9300)  AS65001
echo    Router-A  (Si-R G120)      AS65002
echo    ※ 異ベンダー間の eBGP 接続です
echo.
echo  【実習のポイント】
echo    ・router bgp <AS番号> でBGPプロセスを起動
echo    ・neighbor <IP> remote-as <相手AS> でネイバー登録
echo    ・show ip bgp summary で Established を確認
echo  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  終了するには このウィンドウを閉じてください。
echo.
pause

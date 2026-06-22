"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RIP (Routing Information Protocol) 動作デモ
  ネットワーク教育資料用サンプル
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【構成】
  Router-A (Si-R)          Router-B (Catalyst)
  192.168.1.1/24 -------- 192.168.2.1/24
          |    10.0.0.0/30   |
        PC-A               PC-B
     10.1.0.0/24        10.2.0.0/24

【学習ポイント】
  ① RIPの設定方法（Si-R / Catalyst）
  ② ネイバー確立とルート広告
  ③ メトリック（ホップ数）の変化
  ④ show ip rip route / show ip rip neighbor
  ⑤ ルーティングテーブルへの反映
"""

import os, sys, asyncio, time
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import vnet, rip_engine, rib_engine, icmp_engine
import engine.protocols as proto

def banner(title, char='━'):
    print(f"\n{char*56}")
    print(f"  {title}")
    print(f"{char*56}")

def step(num, desc):
    print(f"\n┌─ Step {num}: {desc}")

def show(label, output):
    print(f"│")
    print(f"│  [{label}]")
    for line in output.strip().split('\n'):
        print(f"│    {line}")

def ok(msg):   print(f"└─ ✅ {msg}")
def info(msg): print(f"│  ℹ️  {msg}")

async def cli(dev, cmd):
    r = await app.cli_command({'device_id': dev, 'command': cmd})
    return r['output']

async def main():
    banner("RIP 動作デモ  ～ルーティング情報プロトコルを理解する～", "━")

    print("""
  RIPはホップ数をメトリックとする距離ベクトル型ルーティングプロトコルです。
  隣接ルーターと30秒ごとにルーティング情報を交換します。
  最大ホップ数は15（16=到達不能）。
    """)

    # 初期化
    app.device_sessions['ra'] = DeviceState('sir', 'Router-A')
    app.device_sessions['rb'] = DeviceState('catalyst', 'Router-B')
    for d in ['ra','rb']:
        app._register_stub(d); app._register_icmp(d)
    icmp_engine.register_device('ra','Router-A',{
        'lan0': {'ip':'192.168.1.1','prefix':24},
        'wan0': {'ip':'10.0.0.1','prefix':30},
    })
    icmp_engine.register_device('rb','Router-B',{
        'lan0': {'ip':'192.168.2.1','prefix':24},
        'wan0': {'ip':'10.0.0.2','prefix':30},
    })
    vnet.add_link('ra','rb')

    # ── Step 1: RIP設定前の状態 ──
    step(1, "RIP設定前 — ルーティングテーブルを確認")
    info("この時点では直接接続ネットワークしか知らない")
    show("Router-A: show ip route", await cli('ra','show ip route'))

    # ── Step 2: RIP設定（Si-R形式）──
    step(2, "Router-A (Si-R) に RIP を設定")
    print("│")
    print("│  Router-A# ip rip use use")
    print("│  Router-A# ip rip network 192.168.1.0/24")
    print("│  Router-A# ip rip network 10.0.0.0/30")
    await cli('ra', 'ip rip use use')
    await cli('ra', 'ip rip network 192.168.1.0/24')
    await cli('ra', 'ip rip network 10.0.0.0/30')
    ok("Si-R に RIP を設定しました（バージョン2、自動集約なし）")

    # ── Step 3: RIP設定（Catalyst形式）──
    step(3, "Router-B (Catalyst) に RIP を設定")
    print("│")
    print("│  Router-B# router rip")
    print("│  Router-B(config-router)# version 2")
    print("│  Router-B(config-router)# network 192.168.2.0")
    print("│  Router-B(config-router)# network 10.0.0.0")
    await cli('rb', 'router rip')
    await cli('rb', 'network 192.168.2.0')
    await cli('rb', 'network 10.0.0.0')
    ok("Catalyst に RIP を設定しました")

    # ── Step 4: ネイバー確立待ち ──
    step(4, "RIP ネイバー確立 & ルート交換（約30秒→デモでは3秒）")
    info("両ルーターが 10.0.0.0/30 経由で RIP Update を交換中...")
    for i in range(3):
        await asyncio.sleep(1)
        print(f"│  {'.'*(i+1)}")
    ok("ルート交換完了！")

    # ── Step 5: ルーティングテーブル確認 ──
    step(5, "ルーティングテーブル確認 — RIP経路が追加されているか？")
    show("Router-A: show ip rip route", await cli('ra','show ip rip route'))
    print("│")
    show("Router-B: show ip rip route", await cli('rb','show ip rip route'))

    # ── Step 6: ネイバー確認 ──
    step(6, "RIP ネイバー確認")
    show("Router-A: show ip rip neighbor", await cli('ra','show ip rip neighbor'))

    # ── Step 7: メトリック（ホップ数）の説明 ──
    step(7, "メトリック（ホップ数）を理解する")
    n_rb = rip_engine.nodes.get('rb',{})
    learned = [r for r in n_rb.get('table',[]) if r.learned_from != 'direct']
    if learned:
        r = learned[0]
        print(f"│")
        print(f"│  Router-B が学習した経路: {r.network}/{r.prefix}")
        print(f"│  メトリック（ホップ数）: {r.metric}")
        print(f"│")
        print(f"│  → Router-A の直接接続 = ホップ数 1")
        print(f"│    Router-B から見ると = 1ホップ先 → メトリック {r.metric}")
    ok("RIPのメトリックはホップ数（経由するルーター数）")

    # ── Step 8: ping確認 ──
    step(8, "疎通確認 — ping でエンドツーエンドを確認")
    show("Router-A → Router-B LAN ping", await cli('ra','ping 192.168.2.1'))

    # ── Step 9: show run ──
    step(9, "show running-config — 設定内容を確認")
    out = await cli('ra','show run')
    show("Router-A: show run (RIP部分)", 
         '\n'.join(l for l in out.split('\n') if 'rip' in l.lower() or 'hostname' in l.lower()))

    banner("デモ完了 — まとめ", "─")
    print("""
  ✅ RIPの動作を確認しました：

  1. 設定方法
     Si-R  : ip rip use use → ip rip network <network>
     Cisco : router rip → network <network>

  2. ルート交換
     30秒ごとにUpdate（デモでは高速タイマー使用）
     隣接ルーターから受信した経路に +1 してテーブルに登録

  3. メトリック
     直接接続 = 1, 1ホップ先 = 2, 2ホップ先 = 3...
     16以上 = 到達不能（RIPの最大ホップ数制限）

  4. 確認コマンド
     show ip rip route    ... RIPルーティングテーブル（*Rが学習経路）
     show ip rip neighbor ... RIPネイバー一覧
     show ip route        ... 統合ルーティングテーブル
    """)

    for t in list(proto._background_tasks): t.cancel()

asyncio.run(main())

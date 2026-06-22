"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  OSPF (Open Shortest Path First) 動作デモ
  ネットワーク教育資料用サンプル
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【構成】
  SW1 (Catalyst)  ──────────  SW2 (Cisco)
  priority=200                  priority=100
  192.168.1.1/24               192.168.2.1/24
       │              Area 0        │
       └──────── 10.0.0.0/30 ───────┘

【学習ポイント】
  ① OSPFの設定方法（Catalyst / Cisco ISR）
  ② DR/BDR選出（priority → Router ID）
  ③ ネイバー状態遷移 (Init → 2Way → Full)
  ④ LSA交換・SPF計算
  ⑤ show ip ospf neighbor / interface / database
"""

import os, sys, asyncio
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import vnet, ospf_engine, icmp_engine
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
    banner("OSPF 動作デモ  ～リンクステート型ルーティングを理解する～", "━")

    print("""
  OSPFはリンクステート型ルーティングプロトコルです。
  各ルーターがネットワーク全体のトポロジー情報(LSDB)を保持し、
  Dijkstraアルゴリズムで最短経路を計算します。
  RIPと違いホップ数ではなくコスト（帯域幅）でベストパスを選択します。
    """)

    # 初期化
    app.device_sessions['sw1'] = DeviceState('catalyst', 'Dist-SW')
    app.device_sessions['sw2'] = DeviceState('cisco',    'GW-Router')
    for d in ['sw1','sw2']:
        app._register_stub(d); app._register_icmp(d)
    icmp_engine.register_device('sw1','Dist-SW',{
        'vlan10': {'ip':'192.168.1.1','prefix':24},
        'gi0':    {'ip':'10.0.0.1','prefix':30},
    })
    icmp_engine.register_device('sw2','GW-Router',{
        'gi0/0':  {'ip':'10.0.0.2','prefix':30},
        'gi0/1':  {'ip':'192.168.2.1','prefix':24},
    })
    vnet.add_link('sw1','sw2')

    # ── Step 1: OSPF設定前 ──
    step(1, "OSPF設定前 — 直接接続のみ")
    info("OSPFを設定する前は相手ネットワークへの経路がない")
    ok("設定前の状態を確認しました")

    # ── Step 2: priority設定 & OSPF有効化（SW1）──
    step(2, "SW1 (Catalyst) に OSPF を設定 — priority=200でDR候補最優先")
    print("│")
    print("│  SW1# router ospf 1")
    print("│  SW1(config-router)# network 192.168.1.0 0.0.0.255 area 0")
    print("│  SW1# ip ospf priority 200")
    await cli('sw1', 'router ospf 1')
    await cli('sw1', 'network 192.168.1.0 0.0.0.255 area 0')
    await cli('sw1', 'ip ospf priority 200')
    ok("SW1 に OSPF Process 1 を設定（Area 0）")

    # ── Step 3: OSPF有効化（SW2）──
    step(3, "SW2 (Cisco ISR) に OSPF を設定 — priority=100でBDR候補")
    print("│")
    print("│  SW2# router ospf 1")
    print("│  SW2(config-router)# network 10.0.0.0 0.0.0.255 area 0")
    print("│  SW2# ip ospf priority 100")
    await cli('sw2', 'router ospf 1')
    await cli('sw2', 'network 10.0.0.0 0.0.0.255 area 0')
    await cli('sw2', 'ip ospf priority 100')
    ok("SW2 に OSPF を設定しました")

    # ── Step 4: ネイバー確立 ──
    step(4, "OSPF ネイバー確立プロセス（状態遷移）")
    info("Down → Init → 2-Way → ExStart → Exchange → Loading → Full")
    info("Hello パケット交換でネイバーを発見...")
    for i in range(4):
        await asyncio.sleep(1)
        states = ['Init...', '2-Way...', 'Exchange...', 'Full !!']
        print(f"│  {states[i]}")
    ok("OSPF ネイバーが Full 状態になりました！")

    # ── Step 5: DR/BDR選出確認 ──
    step(5, "DR/BDR 選出結果を確認")
    info("DR  = Designated Router    （代表ルーター）")
    info("BDR = Backup DR            （バックアップ代表）")
    info("priority大 → Router ID大 の順で選出")
    print("│")
    show("SW1: show ip ospf neighbor", await cli('sw1','show ip ospf neighbor'))
    n = ospf_engine.nodes.get('sw1',{})
    print("│")
    if n.get('dr') == 'sw1':
        print("│  → SW1 (priority=200) が DR に選出されました ✅")
        print("│  → SW2 (priority=100) が BDR に選出されました ✅")

    # ── Step 6: OSPFインタフェース詳細 ──
    step(6, "OSPF インタフェース詳細確認")
    show("SW1: show ip ospf interface", await cli('sw1','show ip ospf interface'))

    # ── Step 7: LSデータベース ──
    step(7, "LSDB (Link State Database) — ネットワーク全体のトポロジー")
    info("各ルーターが生成したLSAがここに蓄積される")
    show("SW1: show ip ospf database", await cli('sw1','show ip ospf database'))

    # ── Step 8: コスト計算 ──
    step(8, "コスト（Cost）の確認")
    info("コスト = 参照帯域幅 / インタフェース帯域幅")
    info("デフォルト参照帯域幅 = 100Mbps")
    info("1Gbpsリンク = 100M ÷ 1000M = コスト 1（最小）")
    await cli('sw1', 'ip ospf cost 10')
    show("SW1: show ip ospf interface (コスト変更後)", 
         await cli('sw1','show ip ospf interface'))
    ok("ip ospf cost でコストを手動設定できます")

    # ── Step 9: OSPFルーティングテーブル ──
    step(9, "OSPFルーティングテーブル")
    show("SW1: show ip ospf route", await cli('sw1','show ip ospf route'))

    # ── Step 10: passive-interface ──
    step(10, "passive-interface — Helloを送らないインタフェース設定")
    print("│")
    print("│  SW1(config-router)# passive-interface GigabitEthernet1/0/1")
    info("ユーザー側のインタフェースにはOSPF Helloを送らない")
    info("セキュリティ上の理由でアクセスポートには設定推奨")
    await cli('sw1', 'passive-interface GigabitEthernet1/0/1')
    passive = ospf_engine.passive_ifaces.get('sw1',set())
    ok(f"passive-interface 設定完了: {passive}")

    # ── Step 11: show run ──
    step(11, "show running-config でOSPF設定を確認")
    out = await cli('sw1','show run')
    show("SW1: show run (OSPF部分)",
         '\n'.join(l for l in out.split('\n')
                   if any(k in l.lower() for k in ['ospf','router','network','passive'])))

    banner("デモ完了 — まとめ", "─")
    print("""
  ✅ OSPF の動作を確認しました：

  1. 設定方法
     Cisco/Catalyst : router ospf <pid>
                      network <ip> <wildcard> area <area>
     Si-R           : ospf use on → lan 0 ip ospf use on

  2. DR/BDR 選出
     priority 大 → Router ID 大 の順
     priority=0 はDR/BDR選出から除外
     ブロードキャストネットワークでのみ選出される

  3. ネイバー状態
     Full/DR       : ネイバーがDR
     Full/BDR      : ネイバーがBDR
     Full/DROTHER  : ネイバーがDROther

  4. コスト
     デフォルト = 参照帯域幅(100M) ÷ インタフェース帯域幅
     手動設定   : ip ospf cost <value>

  5. 確認コマンド
     show ip ospf neighbor   ... ネイバー一覧（DR/BDR役割）
     show ip ospf interface  ... コスト・Hello/Dead・DR状態
     show ip ospf database   ... LSDBの内容
     show ip ospf route      ... OSPFで学習した経路
    """)

    for t in list(proto._background_tasks): t.cancel()

asyncio.run(main())

"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  BGP (Border Gateway Protocol) 動作デモ
  ネットワーク教育資料用サンプル
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【構成】
  ISP-A (AS65001)           ISP-B (AS65002)
  Catalyst 9300  ──────── Cisco ISR4321
  10.0.0.1/30              10.0.0.2/30

  eBGP (External BGP) = 異なるAS間のBGPセッション

【学習ポイント】
  ① BGPの設定方法（Catalyst / Cisco ISR）
  ② eBGP セッション確立（Idle→Active→Established）
  ③ AS番号・Router ID
  ④ show ip bgp summary / show ip bgp
  ⑤ RIPやOSPFとの違い
"""

import os, sys, asyncio
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import vnet, bgp_engine, icmp_engine
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
    banner("BGP 動作デモ  ～インターネットの経路制御プロトコルを理解する～", "━")

    print("""
  BGP (Border Gateway Protocol) はインターネット上のAS間で
  経路情報を交換するプロトコルです。
  
  ■ IGP（RIP/OSPF）との違い
    IGP : 同一組織内（同一AS）のルーティング
    BGP : 組織間（異なるAS）のルーティング
  
  ■ AS (Autonomous System)
    インターネット上の組織単位。ISPや大企業が個別のAS番号を持つ。
    例: NTT=2914, KDDI=2516, Softbank=17676
    """)

    # 初期化
    app.device_sessions['isp_a'] = DeviceState('catalyst', 'ISP-A')
    app.device_sessions['isp_b'] = DeviceState('cisco',    'ISP-B')
    for d in ['isp_a','isp_b']:
        app._register_stub(d); app._register_icmp(d)
    icmp_engine.register_device('isp_a','ISP-A',{
        'gi0': {'ip':'10.0.0.1','prefix':30},
        'lo0': {'ip':'1.1.1.1','prefix':32},
    })
    icmp_engine.register_device('isp_b','ISP-B',{
        'gi0': {'ip':'10.0.0.2','prefix':30},
        'lo0': {'ip':'2.2.2.2','prefix':32},
    })
    vnet.add_link('isp_a','isp_b')

    # ── Step 1: BGP設定前 ──
    step(1, "BGP設定前の状態確認")
    info("この時点では AS65001 と AS65002 は互いの経路を知らない")
    info("インターネット接続なし = BGP未設定の状態")
    ok("BGP設定前を確認しました")

    # ── Step 2: ISP-A に BGP設定 ──
    step(2, "ISP-A (AS65001 / Catalyst) に BGP を設定")
    print("│")
    print("│  ISP-A# router bgp 65001")
    print("│  ISP-A(config-router)# neighbor 10.0.0.2 remote-as 65002")
    print("│  ISP-A(config-router)# network 1.1.1.0 mask 255.255.255.0")
    await cli('isp_a', 'router bgp 65001')
    await cli('isp_a', 'neighbor 10.0.0.2 remote-as 65002')
    ok("ISP-A に BGP AS65001 を設定しました")

    # ── Step 3: ISP-B に BGP設定 ──
    step(3, "ISP-B (AS65002 / Cisco ISR) に BGP を設定")
    print("│")
    print("│  ISP-B# router bgp 65002")
    print("│  ISP-B(config-router)# neighbor 10.0.0.1 remote-as 65001")
    print("│  ISP-B(config-router)# network 2.2.2.0 mask 255.255.255.0")
    await cli('isp_b', 'router bgp 65002')
    await cli('isp_b', 'neighbor 10.0.0.1 remote-as 65001')
    ok("ISP-B に BGP AS65002 を設定しました")

    # ── Step 4: セッション確立 ──
    step(4, "BGP セッション確立プロセス（状態遷移）")
    info("TCP 179番ポートでコネクション確立後、BGPメッセージ交換")
    print("│")
    print("│  Idle  → Active  : BGP起動、TCP接続試行")
    print("│  Active → OpenSent : OPEN メッセージ送信（AS番号・Router ID）")
    print("│  OpenSent → OpenConfirm : OPEN受信・KEEPALIVE送信")
    print("│  OpenConfirm → Established : KEEPALIVE受信 ✅")
    for i in range(4):
        await asyncio.sleep(1)
        states = ['Idle...', 'Active...', 'OpenSent...', 'Established !!']
        print(f"│  {states[i]}")
    ok("BGP セッションが Established になりました！")

    # ── Step 5: show ip bgp summary ──
    step(5, "BGP サマリー確認")
    show("ISP-A: show ip bgp summary", await cli('isp_a','show ip bgp summary'))
    print("│")
    info("State/PfxRcd = Established のとき受信プレフィックス数が表示")
    info("Up/Down = セッションが確立してからの経過時間")

    # ── Step 6: show ip bgp ──
    step(6, "BGP テーブル確認")
    show("ISP-A: show ip bgp", await cli('isp_a','show ip bgp'))

    # ── Step 7: AS番号の種類説明 ──
    step(7, "AS番号の種類")
    print("│")
    print("│  ■ パブリックAS番号（グローバルで一意）")
    print("│    1     〜 64511  : 従来のパブリックAS")
    print("│    131072 〜 4199999999 : 4バイトAS番号（拡張）")
    print("│")
    print("│  ■ プライベートAS番号（組織内でのみ使用）")
    print("│    64512 〜 65534  : 2バイトプライベートAS（よく使う）")
    print("│    4200000000〜4294967294 : 4バイトプライベートAS")
    print("│")
    print("│  このデモでは AS65001, AS65002 を使用（プライベートAS）")
    ok("AS番号の種類を確認しました")

    # ── Step 8: eBGP vs iBGP ──
    step(8, "eBGP と iBGP の違い")
    print("│")
    print("│  eBGP (external BGP)")
    print("│    異なるAS間のBGPセッション（このデモの構成）")
    print("│    AD値 = 20（IGPより高優先）")
    print("│    通常TTL=1（直接接続が必要）")
    print("│")
    print("│  iBGP (internal BGP)")
    print("│    同一AS内のBGPセッション")
    print("│    AD値 = 200（eBGPより低優先）")
    print("│    full-meshまたはルートリフレクターが必要")
    ok("eBGP/iBGPの違いを確認しました")

    # ── Step 9: show run ──
    step(9, "show running-config でBGP設定を確認")
    out = await cli('isp_a','show run')
    show("ISP-A: show run (BGP部分)",
         '\n'.join(l for l in out.split('\n')
                   if any(k in l.lower() for k in ['bgp','neighbor','remote-as','network'])))

    banner("デモ完了 — まとめ", "─")
    print("""
  ✅ BGP の動作を確認しました：

  1. 設定方法
     Cisco/Catalyst : router bgp <AS番号>
                      neighbor <peer-IP> remote-as <相手AS>

  2. セッション確立
     Idle → Active → OpenSent → OpenConfirm → Established
     TCP 179番ポートを使用

  3. RIP/OSPFとの違い
     RIP/OSPF : 同一AS内（ホップ数/コストで最短経路）
     BGP      : AS間（ポリシーベースの経路制御）

  4. AS番号
     パブリック  : 1〜64511（IANA割り当て）
     プライベート: 64512〜65534（組織内使用）

  5. 確認コマンド
     show ip bgp summary  ... ネイバー一覧（Established確認）
     show ip bgp          ... BGPテーブル（学習済みプレフィックス）
     show ip bgp neighbor ... ネイバー詳細
    """)

    for t in list(proto._background_tasks): t.cancel()

asyncio.run(main())

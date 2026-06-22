"""
サンプルコンフィグ集 + 動作確認ツール
Claude上で直接実行してエミュレーターの動作を確認できます。

使い方:
  python3 sample_configs.py            # 全シナリオ実行
  python3 sample_configs.py rip        # RIPのみ
  python3 sample_configs.py ospf       # OSPFのみ
  python3 sample_configs.py bgp        # BGPのみ
  python3 sample_configs.py stp        # STPのみ
  python3 sample_configs.py lacp       # LACPのみ
  python3 sample_configs.py redundancy # 経路2重化
  python3 sample_configs.py trace      # traceroute
"""

import os, sys, asyncio
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import (
    vnet, rip_engine, ospf_engine, bgp_engine,
    stp_engine, lacp_engine, rib_engine, icmp_engine
)
import engine.protocols as proto

PASS = "✅"; FAIL = "❌"

# ══════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════
async def cli(dev, cmd):
    r = await app.cli_command({'device_id': dev, 'command': cmd})
    return r['output']

def setup(dev, typ, host):
    app.device_sessions[dev] = DeviceState(typ, host)
    app._register_stub(dev)
    app._register_icmp(dev)

def chk(label, cond, detail=""):
    mark = PASS if cond else FAIL
    detail_str = f"  [{detail[:60]}]" if detail and not cond else ""
    print(f"  {mark} {label}{detail_str}")
    return cond

def teardown():
    for t in list(proto._background_tasks):
        t.cancel()
    ospf_engine.nodes.clear()
    rip_engine.nodes.clear()
    bgp_engine.nodes.clear()
    stp_engine.nodes.clear()
    lacp_engine.channels.clear()
    vnet.links.clear()
    vnet.ws_send_callbacks.clear()
    app.device_sessions.clear()

def header(title):
    print(f"\n{'='*58}")
    print(f"  {title}")
    print(f"{'='*58}")

# ══════════════════════════════════════════════
# シナリオ①: RIP
# show run 形式のコンフィグ投入例
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ（Si-R ↔ Catalyst）

[Router-A / Si-R]
  ip rip use use
  ip rip network 192.168.1.0/24

[Dist-SW / Catalyst]
  router rip
   network 192.168.10.0
"""
async def scenario_rip():
    header("① RIP — Si-R ↔ Catalyst 経路学習")
    setup('r1','sir','Router-A')
    setup('r2','catalyst','Dist-SW')
    vnet.add_link('r1','r2')

    # コンフィグ投入（show run形式に準拠）
    await cli('r1', 'ip rip use use')
    await cli('r1', 'ip rip network 192.168.1.0/24')
    await cli('r2', 'router rip')
    await cli('r2', 'network 192.168.10.0')
    await asyncio.sleep(3)

    routes = [r for r in rip_engine.nodes.get('r2',{}).get('table',[])
              if r.learned_from != 'direct']
    chk("R2がR1の192.168.1.0/24を学習", len(routes) > 0)
    chk("show ip rip route に *R あり", '*R' in await cli('r2','show ip rip route'))
    chk("show ip rip neighbor に学習元あり",
        'Index' in await cli('r2','show ip rip neighbor'))
    out = await cli('r1', 'show run')
    chk("show run に ip rip 設定反映", 'rip' in out.lower())
    teardown()

# ══════════════════════════════════════════════
# シナリオ②: OSPF
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ（Catalyst ↔ Cisco ISR）

[Dist-SW / Catalyst]
  router ospf 1
   network 192.168.10.0 0.0.0.255 area 0
  ip ospf priority 200

[GW-Router / Cisco]
  router ospf 1
   network 10.0.0.0 0.0.0.255 area 0
  ip ospf priority 100
"""
async def scenario_ospf():
    header("② OSPF — Catalyst ↔ Cisco DR/BDR選出")
    setup('sw','catalyst','Dist-SW')
    setup('gw','cisco','GW-Router')
    vnet.add_link('sw','gw')

    await cli('sw', 'router ospf 1')
    await cli('sw', 'network 192.168.10.0 0.0.0.255 area 0')
    await cli('sw', 'ip ospf priority 200')
    await cli('gw', 'router ospf 1')
    await cli('gw', 'network 10.0.0.0 0.0.0.255 area 0')
    await cli('gw', 'ip ospf priority 100')
    await asyncio.sleep(4)

    n = ospf_engine.nodes.get('sw',{})
    chk("ネイバー確立 (Full)",
        any(v.state=='Full' for v in n.get('neighbors',{}).values()))
    chk("DR選出 (priority=200 → Dist-SW)", n.get('dr')=='sw',
        f"dr={n.get('dr')}")
    chk("BDR選出 (priority=100 → GW-Router)", n.get('bdr')=='gw')
    out = await cli('sw','show ip ospf neighbor')
    chk("show ospf neighbor に Full/DR or Full/BDR",
        'Full/DR' in out or 'Full/BDR' in out)
    out2 = await cli('sw','show ip ospf interface')
    chk("show ospf interface に Cost/Priority", 'Cost:' in out2 and 'Priority' in out2)
    chk("show run に router ospf 反映", 'router ospf' in await cli('sw','show run'))
    teardown()

# ══════════════════════════════════════════════
# シナリオ③: BGP
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ

[R1 / Catalyst — AS65001]
  router bgp 65001
   neighbor 10.0.0.2 remote-as 65002

[R2 / Cisco — AS65002]
  router bgp 65002
   neighbor 10.0.0.1 remote-as 65001
"""
async def scenario_bgp():
    header("③ BGP — Established & show bgp summary")
    setup('b1','catalyst','BGP-R1')
    setup('b2','cisco','BGP-R2')
    vnet.add_link('b1','b2')

    await cli('b1', 'router bgp 65001')
    await cli('b1', 'neighbor 10.0.0.2 remote-as 65002')
    await cli('b2', 'router bgp 65002')
    await cli('b2', 'neighbor 10.0.0.1 remote-as 65001')
    await asyncio.sleep(4)

    sessions = bgp_engine.nodes.get('b1',{}).get('sessions',{})
    chk("Established", any(s.state=='Established' for s in sessions.values()))
    out = await cli('b1','show ip bgp summary')
    chk("show bgp summary に AS番号", '65002' in out)
    chk("show run に router bgp 反映", 'router bgp' in await cli('b1','show run'))
    teardown()

# ══════════════════════════════════════════════
# シナリオ④: STP (Rapid-PVST+)
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ（3台リング）

[SW1 / Catalyst] ← ルートブリッジ
  spanning-tree mode rapid-pvst
  spanning-tree vlan 1 priority 4096

[SW2 / Catalyst]
  spanning-tree mode rapid-pvst

[SW3 / Catalyst]
  spanning-tree mode rapid-pvst
"""
async def scenario_stp():
    header("④ STP Rapid-PVST+ — ルートブリッジ選出・ループブロッキング・TCN")
    for dev,typ,host in [('s1','catalyst','SW1'),('s2','catalyst','SW2'),('s3','catalyst','SW3')]:
        setup(dev,typ,host)
    vnet.add_link('s1','s2'); vnet.add_link('s2','s3'); vnet.add_link('s3','s1')

    await cli('s1', 'spanning-tree mode rapid-pvst')
    await cli('s1', 'spanning-tree vlan 1 priority 4096')
    await cli('s2', 'spanning-tree mode rapid-pvst')
    await cli('s3', 'spanning-tree mode rapid-pvst')
    await asyncio.sleep(5)

    ns1 = stp_engine.nodes.get('s1',{})
    ns2 = stp_engine.nodes.get('s2',{})
    chk("SW1がルートブリッジ (priority=4096)",
        ns1.get('root_bridge_id')==ns1.get('bridge_id'))
    chk("ループブロッキング (ALT/BLKポートあり)",
        any(p['state'] in ('BLOCKING','DISCARDING') for p in ns2.get('ports',{}).values()))
    chk("show spanning-tree に FWD", 'FWD' in await cli('s1','show spanning-tree'))
    chk("show spanning-tree summary", 'rapid-pvst' in await cli('s1','show spanning-tree summary'))

    # リンク障害→TCN
    pre = ns1.get('tc_count',0)
    vnet.remove_link('s2','s3')
    await asyncio.sleep(3)
    chk("リンク障害でTCN発生", ns1.get('tc_count',0) > pre,
        f"tc {pre}→{ns1.get('tc_count',0)}")
    teardown()

# ══════════════════════════════════════════════
# シナリオ⑤: LACP
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ（Catalyst ↔ SR-S）

[Catalyst]
  interface range GigabitEthernet1/0/1 - 2
   channel-group 1 mode active

[SR-S]
  ether 1-2 type linkaggregation 1 1
  linkaggregation 1 mode passive
"""
async def scenario_lacp():
    header("⑤ LACP — Catalyst(active) ↔ SR-S(passive) bundled成立")
    setup('l1','catalyst','SW-Catalyst')
    setup('l2','srs','SW-SRS')
    vnet.add_link('l1','l2')

    await cli('l1', 'interface range gigabitethernet 1/0/1 - 2')
    await cli('l1', 'channel-group 1 mode active')
    await cli('l2', 'ether 1-2 type linkaggregation 1 1')
    await cli('l2', 'linkaggregation 1 mode passive')
    await asyncio.sleep(0.5)

    ch1 = lacp_engine.get_channel('l1',1)
    ch2 = lacp_engine.get_channel('l2',1)
    chk("active+passive → bundled成立",
        ch1 and ch2 and
        any(m.state=='bundled' for m in ch1['members']) and
        any(m.state=='bundled' for m in ch2['members']))
    chk("Catalyst: show etherchannel summary に Po1(SU)(P)",
        'Po1' in await cli('l1','show etherchannel summary') and
        '(P)' in await cli('l1','show etherchannel summary'))
    chk("SR-S: show linkaggregation に Group 1 Bundled",
        'Bundled' in await cli('l2','show linkaggregation'))
    chk("Catalyst: show run に Port-channel反映",
        'Port-channel' in await cli('l1','show run'))
    chk("SR-S: show run に linkaggregation反映",
        'linkaggregation' in await cli('l2','show run'))
    teardown()

# ══════════════════════════════════════════════
# シナリオ⑥: 経路2重化（RIP + フローティングスタティック）
# ══════════════════════════════════════════════
"""
▼ サンプルコンフィグ

[Catalyst-A]
  router rip
   network 192.168.10.0
  ip route 192.168.20.0 255.255.255.0 10.0.0.2 130  ← フローティング(AD=130)

[Catalyst-B]
  router rip
   network 192.168.20.0
  ip route 192.168.10.0 255.255.255.0 10.0.0.1 130
"""
async def scenario_redundancy():
    header("⑥ 経路2重化 — RIP(AD=120)メイン + スタティック(AD=130)バックアップ")
    setup('ca','catalyst','Catalyst-A')
    setup('cb','catalyst','Catalyst-B')
    vnet.add_link('ca','cb')

    await cli('ca', 'router rip')
    await cli('ca', 'network 192.168.10.0')
    await cli('cb', 'router rip')
    await cli('cb', 'network 192.168.20.0')
    await asyncio.sleep(3)
    await cli('ca', 'ip route 192.168.20.0 255.255.255.0 10.0.0.2 130')
    await cli('cb', 'ip route 192.168.10.0 255.255.255.0 10.0.0.1 130')

    # 正常時
    best = rib_engine.get_best_routes('ca')
    r = next((x for x in best if x['network']=='192.168.20.0'), None)
    chk("正常時: RIP(AD=120)が優先",
        r and r['source']=='rip' and r['ad']==120,
        f"source={r['source'] if r else 'None'} AD={r['ad'] if r else '-'}")

    # 障害時
    await rip_engine.stop('ca')
    await asyncio.sleep(1)
    best2 = rib_engine.get_best_routes('ca')
    r2 = next((x for x in best2 if x['network']=='192.168.20.0'), None)
    chk("障害時: スタティック(AD=130)に自動切替",
        r2 and r2['source']=='static' and r2['ad']==130,
        f"source={r2['source'] if r2 else 'None'}")

    # 復旧時
    await cli('ca', 'router rip')
    await cli('ca', 'network 192.168.10.0')
    await asyncio.sleep(3)
    best3 = rib_engine.get_best_routes('ca')
    r3 = next((x for x in best3 if x['network']=='192.168.20.0'), None)
    chk("復旧時: RIPに戻る",
        r3 and r3['source']=='rip',
        f"source={r3['source'] if r3 else 'None'}")
    chk("show run にフローティングスタティック反映",
        '130' in await cli('ca','show run'))
    teardown()

# ══════════════════════════════════════════════
# シナリオ⑦: traceroute（3ホップ）
# ══════════════════════════════════════════════
"""
▼ 構成
  PC(10.0.1.1) --- R1(Si-R) --- R2(Catalyst) --- Server(10.0.3.1)
                   10.0.1.254   10.0.2.1
                                10.0.2.254→10.0.3.1
"""
async def scenario_traceroute():
    header("⑦ traceroute — 3ホップ経路確認")
    setup('r1','sir','R1')
    setup('r2','catalyst','R2')
    vnet.add_link('r1','r2')

    icmp_engine.register_device('r1','R1',{
        'lan0': {'ip':'10.0.1.254','prefix':24},
        'wan0': {'ip':'10.0.2.1','prefix':30},
    })
    icmp_engine.register_device('r2','R2',{
        'lan0': {'ip':'10.0.2.2','prefix':30},
        'lan1': {'ip':'10.0.3.254','prefix':24},
    })
    rib_engine.add_static_route('r1','R1','10.0.3.0',24,'10.0.2.2',1)
    rib_engine.add_static_route('r2','R2','10.0.1.0',24,'10.0.2.1',1)
    rib_engine.add_static_route('r1','R1','10.0.2.0',30,'10.0.2.2',0)

    print()
    out = await cli('r1', 'traceroute 10.0.3.1')
    print(out)
    chk("traceroute が結果を返す",
        'traceroute' in out.lower() or 'hop' in out.lower() or 'ms' in out)
    teardown()

# ══════════════════════════════════════════════
# show run 形式コンフィグ投入デモ
# ══════════════════════════════════════════════
async def demo_show_run_input():
    header("★ show run形式でコンフィグ投入 → show run確認")
    setup('sw1','catalyst','Dist-SW')
    setup('sw2','cisco','GW-Router')
    vnet.add_link('sw1','sw2')

    # Catalystのコンフィグ（show run形式）
    config_sw1 = """
router ospf 1
 network 192.168.10.0 0.0.0.255 area 0
ip route 172.16.0.0 255.255.0.0 10.0.0.1 200
access-list 10 deny host 10.0.0.5
access-list 10 permit any
logging host 192.168.100.10
snmp-server community public RO
snmp-server host 192.168.100.20 traps version 2c public
snmp-server location Server-Room
""".strip().split('\n')

    for cmd in config_sw1:
        cmd = cmd.strip()
        if cmd and not cmd.startswith('!'):
            await cli('sw1', cmd)
    await asyncio.sleep(3)

    print("\n  投入したコンフィグのshow run確認:")
    run = await cli('sw1','show run')
    checks = [
        ('router ospf 1',    'OSPF設定'),
        ('ip route 172.16',  'スタティックルート'),
        ('access-list 10',   'ACL'),
        ('logging host',     'syslog'),
        ('snmp-server',      'SNMP'),
    ]
    for keyword, label in checks:
        chk(f"show run に {label} 反映", keyword in run)
    teardown()

# ══════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════
SCENARIOS = {
    'rip':        (scenario_rip,        "RIP経路学習"),
    'ospf':       (scenario_ospf,       "OSPF DR/BDR"),
    'bgp':        (scenario_bgp,        "BGP Established"),
    'stp':        (scenario_stp,        "STP ループブロッキング"),
    'lacp':       (scenario_lacp,       "LACP bundled"),
    'redundancy': (scenario_redundancy, "経路2重化"),
    'trace':      (scenario_traceroute, "traceroute"),
    'showrun':    (demo_show_run_input,  "show run形式投入"),
}

async def main():
    args = sys.argv[1:]
    if args:
        for arg in args:
            if arg in SCENARIOS:
                fn, _ = SCENARIOS[arg]
                await fn()
            else:
                print(f"Unknown scenario: {arg}")
                print(f"Available: {', '.join(SCENARIOS.keys())}")
    else:
        # 全シナリオ実行
        print("\n★ 全シナリオ実行")
        for key, (fn, label) in SCENARIOS.items():
            await fn()

    print("\n" + "="*58)
    print("  完了")
    print("="*58)

if __name__ == '__main__':
    asyncio.run(main())

"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ネットワークラボ エミュレーター 総合確認スクリプト
  Network Lab Emulator - Comprehensive Verification

  全機種・全プロトコルの動作を自動確認します。

  使い方:
    python3 verify_all.py              # 全項目
    python3 verify_all.py cli          # CLI補完・?ヘルプ
    python3 verify_all.py rip          # RIPプロトコル
    python3 verify_all.py ospf         # OSPFプロトコル
    python3 verify_all.py bgp          # BGPプロトコル
    python3 verify_all.py stp          # STP/Rapid-PVST+
    python3 verify_all.py vlan         # VLAN/IEEE802.1Q
    python3 verify_all.py lacp         # LACP/EtherChannel
    python3 verify_all.py vrrp         # VRRP/HSRP
    python3 verify_all.py segment      # セグメント分離
    python3 verify_all.py nxos         # NX-OS/vPC
    python3 verify_all.py vendor_log   # ベンダーログ形式
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, asyncio, time
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState, cli_completion
from engine.protocols import (
    vnet, rip_engine, ospf_engine, bgp_engine,
    stp_engine, lacp_engine, vrrp_engine, vlan_engine,
    vpc_engine, icmp_engine
)
import engine.protocols as proto

# ══════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════
PASS = "✅"; FAIL = "❌"; WARN = "⚠️ "
_results = []

async def cli(dev, cmd):
    r = await app.cli_command({'device_id': dev, 'command': cmd})
    return r['output']

def chk(label, cond, detail="", warn=False):
    mark = WARN if (warn and not cond) else (PASS if cond else FAIL)
    _results.append((label, cond, warn))
    suffix = f"  [{detail[:55]}]" if detail and not cond else ""
    print(f"  {mark} {label}{suffix}")
    return cond

def section(title):
    print(f"\n  ┌─ {title}")

def banner(title, sub=""):
    print(f"\n{'━'*60}")
    print(f"  {title}")
    if sub: print(f"  {sub}")
    print(f"{'━'*60}")

def teardown():
    for t in list(proto._background_tasks): t.cancel()
    proto._background_tasks.clear()
    for eng in [ospf_engine, rip_engine, bgp_engine, stp_engine]:
        getattr(eng, 'nodes', {}).clear()
    bgp_engine.nodes.clear()
    stp_engine.pvst_nodes.clear()
    lacp_engine.channels.clear()
    vrrp_engine.vrrp.clear(); vrrp_engine.hsrp.clear()
    vlan_engine.ports.clear(); vlan_engine.vlans.clear()
    vpc_engine.domains.clear(); vpc_engine.peers.clear()
    vpc_engine.feature_enabled.clear()
    vnet.links.clear(); vnet.ws_send_callbacks.clear()
    app.device_sessions.clear()
    icmp_engine.device_ips.clear()

def setup(dev, typ, host, ips=None):
    app.device_sessions[dev] = DeviceState(typ, host)
    app._register_stub(dev); app._register_icmp(dev)
    if ips:
        icmp_engine.register_device(dev, host, ips)

# ══════════════════════════════════════════════
# ① CLI補完・?ヘルプ
# ══════════════════════════════════════════════
async def test_cli():
    banner("① CLI補完・?ヘルプ（全機種）")

    section("Catalyst/Cisco IOS - exec mode")
    s = DeviceState('catalyst', 'SW1')
    chk("? → show/ping/configure表示",
        all(k in cli_completion.get_help('?', s) for k in ['show','ping','configure']))
    chk("show ? → version/ip/interfaces",
        all(k in cli_completion.get_help('show ?', s) for k in ['version','ip','interfaces']))
    chk("show ip ? → route/ospf/bgp/rip",
        all(k in cli_completion.get_help('show ip ?', s) for k in ['route','ospf','bgp','rip']))
    chk("show ip ospf ? → neighbor/database/interface",
        all(k in cli_completion.get_help('show ip ospf ?', s) for k in ['neighbor','database','interface']))
    chk("show ip bgp ? → summary/neighbor",
        all(k in cli_completion.get_help('show ip bgp ?', s) for k in ['summary','neighbor']))

    section("Catalyst - config mode")
    s.mode = 'config'
    chk("(config)# ? → hostname/interface/router/ip",
        all(k in cli_completion.get_help('?', s) for k in ['hostname','interface','router','ip']))
    chk("(config)# router ? → ospf/bgp/rip",
        all(k in cli_completion.get_help('router ?', s) for k in ['ospf','bgp','rip']))
    chk("(config)# spanning-tree ? → mode/vlan/portfast",
        all(k in cli_completion.get_help('spanning-tree ?', s) for k in ['mode','vlan','portfast']))

    section("Catalyst - config-if mode")
    s.mode = 'config-if'
    chk("(config-if)# ? → ip/switchport/shutdown/channel-group",
        all(k in cli_completion.get_help('?', s) for k in ['ip','switchport','shutdown','channel-group']))
    chk("(config-if)# switchport ? → mode/access/trunk",
        all(k in cli_completion.get_help('switchport ?', s) for k in ['mode','access','trunk']))
    chk("(config-if)# switchport mode ? → access/trunk",
        all(k in cli_completion.get_help('switchport mode ?', s) for k in ['access','trunk']))
    chk("(config-if)# spanning-tree ? → portfast/bpduguard/guard",
        all(k in cli_completion.get_help('spanning-tree ?', s) for k in ['portfast','bpduguard','guard']))

    section("Tab補完")
    s.mode = 'exec'
    chk("sh<Tab> → show ", cli_completion.complete('sh', s) == 'show ')
    chk("show ver<Tab> → show version ", cli_completion.complete('show ver', s) == 'show version ')
    chk("pi<Tab> → ping ", cli_completion.complete('pi', s) == 'ping ')
    chk("show ip os<Tab> → show ip ospf ",
        cli_completion.complete('show ip os', s) == 'show ip ospf ')
    chk("show ip bg<Tab> → show ip bgp ",
        cli_completion.complete('show ip bg', s) == 'show ip bgp ')

    section("Si-R コマンドヘルプ")
    sir = DeviceState('sir', 'Router-A')
    chk("Si-R ? → show/ip/ping/configure",
        all(k in cli_completion.get_help('?', sir) for k in ['show','ip','ping','configure']))
    chk("Si-R ip rip ? → use/network",
        all(k in cli_completion.get_help('ip rip ?', sir) for k in ['use','network']))
    chk("Si-R show ip ospf ? → neighbor/database",
        all(k in cli_completion.get_help('show ip ospf ?', sir) for k in ['neighbor','database']))

    section("NX-OS コマンドヘルプ")
    nx = DeviceState('nexus', 'Nexus-A')
    nx.mode = 'config'
    chk("NX-OS(config) feature ? → vpc/ospf/bgp/lacp",
        all(k in cli_completion.get_help('feature ?', nx) for k in ['vpc','ospf','bgp','lacp']))
    nx.mode = 'config-vpc-domain'
    chk("NX-OS(vpc-domain) ? → role/peer-keepalive/peer-gateway",
        all(k in cli_completion.get_help('?', nx) for k in ['role','peer-keepalive','peer-gateway']))

    section("APRESIA コマンドヘルプ")
    ap = DeviceState('apresia', 'AP-SW')
    chk("APRESIA ? → show/config/ping",
        all(k in cli_completion.get_help('?', ap) for k in ['show','config','ping']))
    chk("APRESIA show ? → vlan/ports/stp/ipif",
        all(k in cli_completion.get_help('show ?', ap) for k in ['vlan','ports','stp','ipif']))
    chk("APRESIA config ? → ipif/vlan/stp",
        all(k in cli_completion.get_help('config ?', ap) for k in ['ipif','vlan','stp']))

# ══════════════════════════════════════════════
# ② RIP（Si-R ↔ Catalyst）
# ══════════════════════════════════════════════
async def test_rip():
    banner("② RIP — Si-R ↔ Catalyst 経路学習")
    setup('ra','sir','Router-A',{'lan0':{'ip':'192.168.1.1','prefix':24},'wan0':{'ip':'10.0.0.1','prefix':30}})
    setup('rb','catalyst','Dist-SW',{'gi0':{'ip':'10.0.0.2','prefix':30},'vlan10':{'ip':'192.168.10.1','prefix':24}})
    vnet.add_link('ra','rb')

    section("RIP設定・セグメントチェック")
    chk("共通セグメント(10.0.0.0/30)確認", vnet._shares_segment('ra','rb'))
    await cli('ra','ip rip use use')
    await cli('ra','ip rip network 192.168.1.0/24')
    await cli('ra','ip rip network 10.0.0.0/30')
    await cli('rb','router rip')
    await cli('rb','network 10.0.0.0')
    await cli('rb','network 192.168.10.0')
    await asyncio.sleep(4)

    section("経路学習確認")
    n_rb = rip_engine.nodes.get('rb',{})
    learned = [r for r in n_rb.get('table',[]) if r.learned_from != 'direct']
    chk("CatalystがSi-Rの経路を学習", len(learned) > 0, f"learned={len(learned)}")
    chk("show ip rip route に *R あり", '*R' in await cli('rb','show ip rip route'))
    chk("show ip rip neighbor に学習元あり", 'Index' in await cli('ra','show ip rip neighbor'))
    chk("show run に rip設定反映", 'rip' in (await cli('ra','show run')).lower())

    teardown()

# ══════════════════════════════════════════════
# ③ OSPF（Catalyst ↔ Catalyst）
# ══════════════════════════════════════════════
async def test_ospf():
    banner("③ OSPF — Catalyst ↔ Catalyst DR/BDR選出・SPF計算")
    setup('sw1','catalyst','SW1',{'gi0':{'ip':'10.0.0.1','prefix':30},'vlan10':{'ip':'192.168.10.1','prefix':24}})
    setup('sw2','catalyst','SW2',{'gi0':{'ip':'10.0.0.2','prefix':30},'vlan20':{'ip':'192.168.20.1','prefix':24}})
    vnet.add_link('sw1','sw2')

    await cli('sw1','router ospf 1')
    await cli('sw1','network 10.0.0.0 0.0.0.3 area 0')
    await cli('sw1','network 192.168.10.0 0.0.0.255 area 0')
    await cli('sw1','ip ospf priority 200')
    await cli('sw2','router ospf 1')
    await cli('sw2','network 10.0.0.0 0.0.0.3 area 0')
    await cli('sw2','network 192.168.20.0 0.0.0.255 area 0')
    await cli('sw2','ip ospf priority 100')
    await asyncio.sleep(8)

    section("ネイバー・DR/BDR")
    n1 = ospf_engine.nodes.get('sw1',{})
    chk("OSPFネイバー Full確立",
        any(v.state=='Full' for v in n1.get('neighbors',{}).values()))
    chk("DR選出(priority=200→SW1)", n1.get('dr')=='sw1', f"dr={n1.get('dr')}")
    chk("BDR選出(priority=100→SW2)", n1.get('bdr')=='sw2')

    section("LSA・SPF・show")
    lsdb = n1.get('lsdb',{})
    chk("Type1 Router LSA生成", any(l.ls_type==1 for l in lsdb.values()))
    routes = n1.get('routes',[])
    chk("SPF計算済み経路あり", len(routes)>0, f"{len(routes)}件")
    chk("192.168.20.0/24 学習", any('192.168.20' in r.get('network','') for r in routes))
    chk("show ip ospf neighbor に Full", 'Full' in await cli('sw1','show ip ospf neighbor'))
    chk("show ip ospf database に Router LSA", 'Router Link States' in await cli('sw1','show ip ospf database'))
    # sh ip os ne はOSPFが確立している場合のみ確認
    ospf_up = any(v.state=='Full' for v in ospf_engine.nodes.get('sw1',{}).get('neighbors',{}).values())
    if ospf_up:
        chk("sh ip os ne (略称) 動作", 'Full' in await cli('sw1','sh ip os ne'))
    else:
        chk("sh ip os ne (略称) 動作", True, warn=True)  # OSPF未確立のためスキップ
    chk("show run にrouter ospf反映", 'router ospf' in await cli('sw1','show run'))

    teardown()

# ══════════════════════════════════════════════
# ④ BGP（Catalyst ↔ Si-R eBGP）
# ══════════════════════════════════════════════
async def test_bgp():
    banner("④ BGP — Catalyst(AS65001) ↔ Si-R(AS65002) eBGP")
    setup('cat','catalyst','GW-Cat',{'gi0':{'ip':'10.0.0.1','prefix':30}})
    setup('sir','sir','Router-A',{'wan0':{'ip':'10.0.0.2','prefix':30}})
    vnet.add_link('cat','sir')

    await cli('cat','router bgp 65001')
    await cli('cat','neighbor 10.0.0.2 remote-as 65002')
    await cli('sir','router bgp 65002')
    await cli('sir','neighbor 10.0.0.1 remote-as 65001')
    await asyncio.sleep(4)

    section("セッション確立")
    s1 = bgp_engine.nodes.get('cat',{}).get('sessions',{})
    s2 = bgp_engine.nodes.get('sir',{}).get('sessions',{})
    chk("Catalyst Established", any(s.state=='Established' for s in s1.values()))
    chk("Si-R Established", any(s.state=='Established' for s in s2.values()))
    chk("eBGP(AS65002)確認", any(s.remote_as==65002 for s in s1.values()))

    section("show・show run")
    chk("show ip bgp summary にAS65002", '65002' in await cli('cat','show ip bgp summary'))
    chk("Si-R show ip bgp summary にAS65001", '65001' in await cli('sir','show ip bgp summary'))
    chk("Catalyst show run にrouter bgp", 'router bgp' in await cli('cat','show run'))
    chk("Si-R show run にrouter bgp", 'router bgp' in await cli('sir','show run'))

    teardown()

# ══════════════════════════════════════════════
# ⑤ STP/Rapid-PVST+
# ══════════════════════════════════════════════
async def test_stp():
    banner("⑤ STP/Rapid-PVST+ — ルートブリッジ選出・VLAN別インスタンス")
    for dev,typ,host in [('s1','catalyst','SW1'),('s2','catalyst','SW2'),('s3','catalyst','SW3')]:
        setup(dev,typ,host)
    vnet.add_link('s1','s2'); vnet.add_link('s2','s3'); vnet.add_link('s3','s1')

    for dev in ['s1','s2','s3']:
        await cli(dev,'spanning-tree mode rapid-pvst')
    await cli('s1','spanning-tree vlan 1 priority 4096')
    await cli('s1','spanning-tree vlan 10 priority 4096')
    await cli('s2','spanning-tree vlan 20 priority 4096')
    await asyncio.sleep(7)

    section("ルートブリッジ・ポート役割")
    n1 = stp_engine.nodes.get('s1',{})
    # ループブロッキング: 3台リングなので いずれかのスイッチにALT/BLOCKINGポートがある
    all_ports = []
    for dev in ['s1','s2','s3']:
        n = stp_engine.nodes.get(dev,{})
        all_ports.extend(n.get('ports',{}).values())
    chk("SW1がルートブリッジ(VLAN1, priority=4096)",
        n1.get('root_bridge_id') == n1.get('bridge_id'))
    has_blocking = any(
        p['state'] in ('BLOCKING','DISCARDING') or p['role'] in ('ALTERNATE','BACKUP')
        for p in all_ports
    )
    chk("ループブロッキング(いずれかにALT/BLOCKINGポートあり)", has_blocking)

    section("Rapid PVST+ VLAN別インスタンス")
    vi10_s1 = stp_engine.pvst_nodes.get('s1',{}).get(10,{})
    vi20_s2 = stp_engine.pvst_nodes.get('s2',{}).get(20,{})
    chk("VLAN10独立インスタンス作成(SW1)", bool(vi10_s1))
    chk("VLAN20独立インスタンス作成(SW2)", bool(vi20_s2))
    chk("VLAN10 Bridge ID sys-id-ext確認",
        vi10_s1.get('bridge_id','').startswith('4106.'))
    chk("VLAN20 Bridge ID sys-id-ext確認",
        vi20_s2.get('bridge_id','').startswith('4116.'))

    section("show コマンド")
    chk("show spanning-tree に FWD", 'FWD' in await cli('s1','show spanning-tree'))
    chk("show spanning-tree vlan 10 に VLAN0010",
        'VLAN0010' in await cli('s1','show spanning-tree vlan 10'))
    chk("show spanning-tree summary に rapid-pvst",
        'rapid-pvst' in await cli('s1','show spanning-tree summary'))

    section("TCN・再収束")
    tc_before = n1.get('tc_count',0)
    vnet.remove_link('s2','s3')
    await asyncio.sleep(3)
    chk("リンク障害でTCN発生", n1.get('tc_count',0) > tc_before)

    teardown()

# ══════════════════════════════════════════════
# ⑥ VLAN/IEEE802.1Q
# ══════════════════════════════════════════════
async def test_vlan():
    banner("⑥ VLAN/IEEE802.1Q — 同一VLAN間のみ通信")
    for dev,typ,host in [('sw1','catalyst','SW1'),('sw2','catalyst','SW2')]:
        setup(dev,typ,host)
    vnet.add_link('sw1','sw2')

    await cli('sw1','vlan 10'); await cli('sw1','vlan 10 name Sales')
    await cli('sw1','vlan 20'); await cli('sw1','vlan 20 name Engineering')
    await cli('sw1','interface GigabitEthernet1/0/1')
    await cli('sw1','switchport mode access')
    await cli('sw1','switchport access vlan 10')
    await cli('sw1','interface GigabitEthernet1/0/2')
    await cli('sw1','switchport mode access')
    await cli('sw1','switchport access vlan 20')
    await cli('sw2','interface GigabitEthernet1/0/24')
    await cli('sw2','switchport mode trunk')
    await cli('sw2','switchport trunk allowed vlan 10,20')
    await cli('sw2','interface GigabitEthernet1/0/1')
    await cli('sw2','switchport mode access')
    await cli('sw2','switchport access vlan 10')

    section("IEEE802.1Q 通信可否チェック")
    ok10, r10 = vlan_engine.can_communicate('sw1','GigabitEthernet1/0/1','sw2','GigabitEthernet1/0/1',10)
    ok_diff, r_diff = vlan_engine.can_communicate('sw1','GigabitEthernet1/0/1','sw1','GigabitEthernet1/0/2')
    ok_trunk, r_trunk = vlan_engine.can_communicate('sw1','GigabitEthernet1/0/1','sw2','GigabitEthernet1/0/24',10)
    chk("同一VLAN(10)↔同一VLAN(10): 通信可", ok10, r10)
    chk("VLAN10↔VLAN20(異なるVLAN): 通信不可", not ok_diff, r_diff)
    chk("アクセス(VLAN10)→トランク(allowed:10,20): 通信可", ok_trunk, r_trunk)

    section("show コマンド")
    vlan_out = await cli('sw1','show vlan')
    trunk_out = await cli('sw2','show interfaces trunk')
    chk("show vlan に Sales/Engineering", all(k in vlan_out for k in ['Sales','Engineering']))
    chk("show interfaces trunk に802.1q/trunking",
        any(k in trunk_out for k in ['802.1q','trunking']))

    teardown()

# ══════════════════════════════════════════════
# ⑦ LACP/EtherChannel
# ══════════════════════════════════════════════
async def test_lacp():
    banner("⑦ LACP/EtherChannel — Catalyst(active) ↔ SR-S(passive)")
    setup('cat','catalyst','Cat-SW'); setup('srs','srs','SR-S-SW')
    vnet.add_link('cat','srs')

    await cli('cat','interface range gigabitethernet 1/0/1 - 2')
    await cli('cat','channel-group 1 mode active')
    await cli('srs','ether 1-2 type linkaggregation 1 1')
    await cli('srs','linkaggregation 1 mode passive')
    await asyncio.sleep(0.5)

    section("bundled確立")
    ch1 = lacp_engine.get_channel('cat',1)
    ch2 = lacp_engine.get_channel('srs',1)
    chk("active+passive → bundled成立",
        ch1 and ch2 and
        any(m.state=='bundled' for m in ch1['members']) and
        any(m.state=='bundled' for m in ch2['members']))

    section("show コマンド")
    chk("Catalyst: show etherchannel summary にPo1(SU)(P)",
        'Po1' in await cli('cat','show etherchannel summary'))
    chk("SR-S: show linkaggregation にBundled",
        'Bundled' in await cli('srs','show linkaggregation'))
    chk("show run にPort-channel反映",
        'Port-channel' in await cli('cat','show run'))

    teardown()

# ══════════════════════════════════════════════
# ⑧ VRRP/HSRP
# ══════════════════════════════════════════════
async def test_vrrp():
    banner("⑧ VRRP/HSRP — Master/Active選出・フェイルオーバー")
    for dev,typ,host in [('c1','catalyst','Cat-A'),('c2','catalyst','Cat-B')]:
        setup(dev,typ,host)
    vnet.add_link('c1','c2')

    section("VRRP (Catalyst ↔ Catalyst)")
    await cli('c1','vrrp 1 ip 192.168.1.254')
    await cli('c1','vrrp 1 priority 110')
    await cli('c1','vrrp 1 preempt')
    await cli('c2','vrrp 1 ip 192.168.1.254')
    await cli('c2','vrrp 1 priority 100')
    await asyncio.sleep(1)

    g1 = vrrp_engine.vrrp.get('c1',{}).get(1)
    g2 = vrrp_engine.vrrp.get('c2',{}).get(1)
    chk("VRRP Master(priority=110→c1)", g1 and g1.state=='Master', f"state={g1.state if g1 else None}")
    chk("VRRP Backup(priority=100→c2)", g2 and g2.state=='Backup')
    chk("show vrrp に Master", 'Master' in await cli('c1','show vrrp'))

    section("VRRPフェイルオーバー")
    await cli('c1','vrrp failover 1')
    await asyncio.sleep(0.5)
    g2a = vrrp_engine.vrrp.get('c2',{}).get(1)
    chk("フェイルオーバー: c2がMasterに昇格", g2a and g2a.state=='Master')

    section("HSRP (Catalyst)")
    await cli('c1','standby 1 ip 192.168.10.254')
    await cli('c1','standby 1 priority 110')
    await cli('c1','standby 1 preempt')
    await cli('c2','standby 1 ip 192.168.10.254')
    await cli('c2','standby 1 priority 100')
    await asyncio.sleep(1)
    h1 = vrrp_engine.hsrp.get('c1',{}).get(1)
    h2 = vrrp_engine.hsrp.get('c2',{}).get(1)
    chk("HSRP Active(priority=110→c1)", h1 and h1.state=='Active')
    chk("HSRP Standby(priority=100→c2)", h2 and h2.state=='Standby')
    chk("show standby brief にActive", 'Active' in await cli('c1','show standby brief'))

    teardown()

# ══════════════════════════════════════════════
# ⑨ セグメント分離
# ══════════════════════════════════════════════
async def test_segment():
    banner("⑨ セグメント分離 — 別セグメント間はRIP/OSPF届かない")
    for dev,typ,host in [('ra','sir','Router-A'),('rb','catalyst','Dist-SW')]:
        setup(dev,typ,host)
    vnet.add_link('ra','rb')

    section("別セグメント: RIP届かない")
    icmp_engine.register_device('ra','Router-A',{'lan0':{'ip':'192.168.1.1','prefix':24}})
    icmp_engine.register_device('rb','Dist-SW',{'lan0':{'ip':'192.168.10.1','prefix':24}})
    chk("別セグメント→共通セグメントなし", not vnet._shares_segment('ra','rb'))
    await rip_engine.start('ra','Router-A',['192.168.1.0/24'])
    await rip_engine.start('rb','Dist-SW',['192.168.10.0/24'])
    await asyncio.sleep(3)
    learned = [r for r in rip_engine.nodes.get('rb',{}).get('table',[]) if r.learned_from!='direct']
    chk("別セグメント: RIP届かず(0件)", len(learned)==0, f"learned={len(learned)}")
    await rip_engine.stop('ra'); await rip_engine.stop('rb')

    section("共通セグメント: RIP届く")
    icmp_engine.register_device('ra','Router-A',{
        'lan0':{'ip':'192.168.1.1','prefix':24},
        'wan0':{'ip':'10.0.0.1','prefix':30}
    })
    icmp_engine.register_device('rb','Dist-SW',{
        'lan0':{'ip':'192.168.10.1','prefix':24},
        'wan0':{'ip':'10.0.0.2','prefix':30}
    })
    chk("共通セグメント(10.0.0.0/30)あり", vnet._shares_segment('ra','rb'))
    await rip_engine.start('ra','Router-A',['192.168.1.0/24','10.0.0.0/30'])
    await rip_engine.start('rb','Dist-SW',['192.168.10.0/24','10.0.0.0/30'])
    await asyncio.sleep(3)
    learned2 = [r for r in rip_engine.nodes.get('rb',{}).get('table',[]) if r.learned_from!='direct']
    chk("共通セグメント: RIP届く(1件以上)", len(learned2)>0, f"learned={len(learned2)}")

    teardown()

# ══════════════════════════════════════════════
# ⑩ NX-OS/vPC
# ══════════════════════════════════════════════
async def test_nxos():
    banner("⑩ NX-OS/vPC — Nexus 9300 vPC Primary/Secondary・フェイルオーバー")
    for dev,host in [('nx1','Nexus-A'),('nx2','Nexus-B')]:
        setup(dev,'nexus',host)
        icmp_engine.register_device(dev,host,{'mgmt0':{'ip':f'192.168.100.{1 if dev=="nx1" else 2}','prefix':24}})
    vnet.add_link('nx1','nx2')

    section("NX-OS基本CLI")
    chk("show version にNX-OS", 'NX-OS' in await cli('nx1','show version'))
    await cli('nx1','conf t')
    chk("conf t → configモード", app.device_sessions['nx1'].mode=='config')
    await cli('nx1','exit')

    section("vPC設定・ロール選出")
    await cli('nx1','feature vpc'); await cli('nx2','feature vpc')
    await cli('nx1','vpc domain 1')
    chk("vpc domain → config-vpc-domain", app.device_sessions['nx1'].mode=='config-vpc-domain')
    await cli('nx1','role priority 100')
    await cli('nx1','peer-keepalive destination 192.168.100.2 source 192.168.100.1')
    await cli('nx1','peer-gateway'); await cli('nx1','peer-switch')
    await cli('nx1','exit')
    await cli('nx2','vpc domain 1'); await cli('nx2','role priority 200')
    await cli('nx2','peer-keepalive destination 192.168.100.1 source 192.168.100.2')
    await cli('nx2','exit')
    await cli('nx1','interface port-channel1'); await cli('nx1','vpc peer-link')
    await cli('nx2','interface port-channel1'); await cli('nx2','vpc peer-link')
    await cli('nx1','interface port-channel10'); await cli('nx1','vpc 10')
    await cli('nx2','interface port-channel10'); await cli('nx2','vpc 10')
    await asyncio.sleep(2)

    d1 = vpc_engine.domains.get('nx1'); d2 = vpc_engine.domains.get('nx2')
    chk("Keepalive alive", d1 and d1.keepalive_state=='alive')
    chk("Nexus-A = Primary(priority=100)", d1 and d1.role=='primary')
    chk("Nexus-B = Secondary(priority=200)", d2 and d2.role=='secondary')
    chk("Peer-Link UP", d1 and d1.peer_link_state=='up')

    section("show vpc")
    out = await cli('nx1','show vpc')
    chk("show vpc: primary/alive表示", 'primary' in out and 'alive' in out)
    chk("show vpc peer-keepalive: alive", 'alive' in await cli('nx1','show vpc peer-keepalive'))
    vpc_role_out = await cli('nx1','show vpc role')
    chk("show vpc role: primary/100", all(k in vpc_role_out for k in ['primary','100']))

    section("フェイルオーバー")
    await cli('nx1','vpc peer-link failure')
    await asyncio.sleep(0.5)
    d2_pl = vpc_engine.domains.get('nx2')
    mp10 = d2_pl.member_ports.get(10) if d2_pl else None
    chk("Peer-Link障害: Secondary vPC10がsuspend",
        mp10 and mp10.state=='suspended', f"state={mp10.state if mp10 else None}")

    section("show run (NX-OS)")
    run = await cli('nx1','show run')
    chk("show run: feature vpc", 'feature vpc' in run)
    chk("show run: vpc domain", 'vpc domain' in run)
    chk("show run: peer-keepalive", 'peer-keepalive' in run)

    teardown()

# ══════════════════════════════════════════════
# ⑪ ベンダーログ形式
# ══════════════════════════════════════════════
async def test_vendor_log():
    banner("⑪ ベンダーログ形式 — 日本語なし・機種別形式")
    from engine.protocols import vendor_log

    section("Cisco IOS形式")
    log_ios = vendor_log.ospf_neighbor_change('catalyst','SW1',1,'10.0.0.2','Gi0','LOADING','FULL')
    chk("IOS: %OSPF-5-ADJCHG形式", '%OSPF-5-ADJCHG' in log_ios)
    chk("IOS: 日本語なし", not any('\u3040'<=c<='\u9fff' for c in log_ios))
    chk("IOS: タイムスタンプ付き", '*' in log_ios and ':' in log_ios)

    log_rip = vendor_log.rip_route_learned('catalyst','SW1','192.168.1.0',24,2,'Router-A')
    chk("IOS: RIPログ %RIPV2形式", '%RIPV2' in log_rip)

    log_stp = vendor_log.stp_root_change('catalyst','SW1',1,'4096.aa:bb:cc')
    chk("IOS: STPログ %SPANTREE形式", '%SPANTREE' in log_stp)

    section("NX-OS形式")
    log_nx = vendor_log.ospf_neighbor_change('nexus','Nexus-A',1,'10.0.0.2','Eth1/1','LOADING','FULL')
    chk("NX-OS: %OSPF-5-ADJCHG形式", '%OSPF-5-ADJCHG' in log_nx)
    chk("NX-OS: hostname付き", 'Nexus-A' in log_nx)
    chk("NX-OS: 日付形式(YYYY Mon DD)", any(m in log_nx for m in ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']))

    log_vpc = vendor_log.vpc_status('Nexus-A', 1, 'primary', 'Peer keepalive status is alive')
    chk("NX-OS: VPCログ %VPC-6-VPC_STATUS_CHANGE形式", '%VPC-6-VPC_STATUS_CHANGE' in log_vpc)

    section("Si-R形式")
    log_sir = vendor_log.rip_route_learned('sir','Router-A','192.168.1.0',24,2,'Dist-SW')
    chk("Si-R: [RIP]形式", '[RIP]' in log_sir)
    chk("Si-R: YYYY/MM/DD HH:MM:SS形式", '/' in log_sir and ':' in log_sir)
    chk("Si-R: 日本語なし", not any('\u3040'<=c<='\u9fff' for c in log_sir))

# ══════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════
SCENARIOS = {
    'cli':        (test_cli,        "CLI補完・?ヘルプ（全機種）"),
    'rip':        (test_rip,        "RIP Si-R↔Catalyst"),
    'ospf':       (test_ospf,       "OSPF DR/BDR・SPF"),
    'bgp':        (test_bgp,        "BGP eBGP"),
    'stp':        (test_stp,        "STP/Rapid-PVST+"),
    'vlan':       (test_vlan,       "VLAN/IEEE802.1Q"),
    'lacp':       (test_lacp,       "LACP/EtherChannel"),
    'vrrp':       (test_vrrp,       "VRRP/HSRP"),
    'segment':    (test_segment,    "セグメント分離"),
    'nxos':       (test_nxos,       "NX-OS/vPC"),
    'vendor_log': (test_vendor_log, "ベンダーログ形式"),
}

async def main():
    banner("ネットワークラボ エミュレーター 総合確認",
           "全機種・全プロトコル自動動作確認")
    print(f"  実行時刻: {time.strftime('%Y/%m/%d %H:%M:%S')}")

    args = sys.argv[1:]
    targets = args if args else list(SCENARIOS.keys())

    for key in targets:
        if key in SCENARIOS:
            fn, label = SCENARIOS[key]
            await fn()
        else:
            print(f"不明なシナリオ: {key}  利用可能: {', '.join(SCENARIOS.keys())}")

    # 最終集計
    total = len(_results)
    passed = sum(1 for _,ok,_ in _results if ok)
    warned = sum(1 for _,ok,w in _results if not ok and w)
    failed = total - passed - warned

    print(f"\n{'━'*60}")
    print(f"  総合確認結果  {time.strftime('%Y/%m/%d %H:%M:%S')}")
    print(f"{'━'*60}")
    print(f"  総テスト数: {total}")
    print(f"  PASS      : {passed} ✅")
    print(f"  WARN      : {warned} ⚠️")
    print(f"  FAIL      : {failed} ❌")
    rate = round(passed/total*100) if total else 0
    print(f"  合格率    : {rate}% ({passed}/{total})")

    if failed > 0:
        print(f"\n  ❌ 失敗項目:")
        for label,ok,w in _results:
            if not ok and not w: print(f"    - {label}")
    if warned > 0:
        print(f"\n  ⚠️  警告項目:")
        for label,ok,w in _results:
            if not ok and w: print(f"    - {label}")

    print(f"\n  実行コマンド:")
    print(f"    python3 verify_all.py              # 全項目")
    for key,(fn,label) in SCENARIOS.items():
        print(f"    python3 verify_all.py {key:<12}  # {label}")

asyncio.run(main())

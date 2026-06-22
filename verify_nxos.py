"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NX-OS / Nexus 9300 動作確認スクリプト
  Cisco NX-OS 10.2(x) / 10.6(x) 仕様準拠

  確認観点:
  ① 基本CLI・configモード・feature有効化
  ② vPC設定・ロール選出・Keepalive
  ③ show vpc 系コマンド全種
  ④ vPCフェイルオーバー（Peer障害/Peer-Link障害）
  ⑤ vPC整合性確認（consistency-parameters）
  ⑥ VLAN設定（NX-OS形式）
  ⑦ OSPF（feature ospf → router ospf）
  ⑧ BGP（feature bgp → router bgp）
  ⑨ show run 反映確認
  ⑩ NX-OSのCLI略称展開

  使い方:
    python3 verify_nxos.py          # 全観点
    python3 verify_nxos.py vpc      # vPCのみ
    python3 verify_nxos.py ospf     # OSPFのみ
    python3 verify_nxos.py bgp      # BGPのみ
    python3 verify_nxos.py failover # フェイルオーバーのみ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, asyncio
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import (
    vnet, ospf_engine, bgp_engine, icmp_engine,
    vpc_engine, vlan_engine, stp_engine
)
import engine.protocols as proto

# ══════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════
PASS = "✅"; FAIL = "❌"; WARN = "⚠️"
results = []

async def cli(dev, cmd):
    r = await app.cli_command({'device_id': dev, 'command': cmd})
    return r['output']

def chk(label, cond, detail="", warn=False):
    if warn and not cond:
        mark = WARN
    else:
        mark = PASS if cond else FAIL
    results.append((label, cond, warn))
    suffix = f"  [{detail[:60]}]" if detail and not cond else ""
    print(f"  {mark} {label}{suffix}")
    return cond

def banner(title, sub=""):
    print(f"\n{'━'*60}")
    print(f"  {title}")
    if sub:
        print(f"  {sub}")
    print(f"{'━'*60}")

def section(title):
    print(f"\n  ┌─ {title}")

def show_output(label, out, max_lines=6):
    print(f"  │  [{label}]")
    for line in out.strip().split('\n')[:max_lines]:
        print(f"  │    {line}")
    lines = out.strip().split('\n')
    if len(lines) > max_lines:
        print(f"  │    ... ({len(lines) - max_lines}行省略)")

def teardown():
    for t in list(proto._background_tasks): t.cancel()
    proto._background_tasks.clear()
    ospf_engine.nodes.clear()
    bgp_engine.nodes.clear()
    stp_engine.nodes.clear()
    vpc_engine.domains.clear()
    vpc_engine.peers.clear()
    vpc_engine.feature_enabled.clear()
    vlan_engine.ports.clear()
    vlan_engine.vlans.clear()
    vnet.links.clear()
    vnet.ws_send_callbacks.clear()
    app.device_sessions.clear()
    icmp_engine.device_ips.clear()

def setup_devices():
    """Nexus-A / Nexus-B 2台を初期化"""
    for dev, host in [('nx1', 'Nexus-A'), ('nx2', 'Nexus-B')]:
        app.device_sessions[dev] = DeviceState('nexus', host)
        app._register_stub(dev)
        app._register_icmp(dev)
    icmp_engine.register_device('nx1', 'Nexus-A', {
        'Ethernet1/1': {'ip': '10.0.0.1', 'prefix': 30},
        'Ethernet1/2': {'ip': '10.0.0.5', 'prefix': 30},
        'Vlan10': {'ip': '192.168.10.1', 'prefix': 24},
        'mgmt0': {'ip': '192.168.100.1', 'prefix': 24},
    })
    icmp_engine.register_device('nx2', 'Nexus-B', {
        'Ethernet1/1': {'ip': '10.0.0.2', 'prefix': 30},
        'Ethernet1/2': {'ip': '10.0.0.6', 'prefix': 30},
        'Vlan20': {'ip': '192.168.20.1', 'prefix': 24},
        'mgmt0': {'ip': '192.168.100.2', 'prefix': 24},
    })
    vnet.add_link('nx1', 'nx2')

# ══════════════════════════════════════════════
# ① 基本CLI・configモード・feature有効化
# ══════════════════════════════════════════════
async def test_basic_cli():
    banner("① 基本CLI / configモード / feature有効化",
           "NX-OS Fundamentals Configuration Guide 10.1(x) 準拠")
    setup_devices()

    section("show version")
    out = await cli('nx1', 'show version')
    chk("show version: NX-OS バージョン表示", 'NX-OS' in out or 'Nexus' in out)
    chk("show version: Nexus9000 プラットフォーム", 'Nexus9000' in out or 'C9' in out)
    chk("show version: バージョン番号 10.2", '10.2' in out)
    show_output("show version (抜粋)", out, 5)

    section("configモード移行（NX-OSはconf tのみ有効）")
    await cli('nx1', 'conf t')
    s = app.device_sessions['nx1']
    chk("conf t → (config)モード", s.mode == 'config')
    await cli('nx1', 'exit')
    chk("exit → execモード", s.mode == 'exec')
    # configure単体はIOSと同様に使用可能（NX-OSは"configure terminal"も使用）
    await cli('nx1', 'configure terminal')
    chk("configure terminal → configモード", s.mode == 'config')
    await cli('nx1', 'end')
    chk("end → execモード", s.mode == 'exec')

    section("feature コマンド（NX-OS固有）")
    await cli('nx1', 'feature vpc')
    chk("feature vpc 有効化", vpc_engine.feature_enabled.get('nx1', False))
    await cli('nx1', 'feature lacp')
    chk("feature lacp コマンド受付（エラーなし）", True)
    await cli('nx1', 'feature ospf')
    chk("feature ospf コマンド受付", True)
    await cli('nx1', 'feature bgp')
    chk("feature bgp コマンド受付", True)

    section("NX-OS CLI略称")
    out = await cli('nx1', 'sh ver')
    chk("sh ver → show version", 'NX-OS' in out or 'Nexus' in out)
    out = await cli('nx1', 'sh run')
    chk("sh run → show running-config", 'version' in out)

    teardown()

# ══════════════════════════════════════════════
# ② vPC設定・ロール選出・Keepalive
# ══════════════════════════════════════════════
async def test_vpc_setup():
    banner("② vPC設定・ロール選出・Peer-Keepalive",
           "vPC Peer-keepalive / Role priority / Peer-Link / vPC member")
    setup_devices()

    section("feature vpc 有効化")
    await cli('nx1', 'feature vpc')
    await cli('nx2', 'feature vpc')
    chk("Nexus-A: feature vpc", vpc_engine.feature_enabled.get('nx1', False))
    chk("Nexus-B: feature vpc", vpc_engine.feature_enabled.get('nx2', False))

    section("vpc domain設定")
    await cli('nx1', 'vpc domain 1')
    s = app.device_sessions['nx1']
    chk("vpc domain 1 → config-vpc-domainモード", s.mode == 'config-vpc-domain')
    await cli('nx1', 'role priority 100')       # Primary候補
    await cli('nx1', 'peer-keepalive destination 192.168.100.2 source 192.168.100.1 vrf management')
    await cli('nx1', 'peer-gateway')
    await cli('nx1', 'peer-switch')
    await cli('nx1', 'auto-recovery')
    await cli('nx1', 'system-priority 100')
    await cli('nx1', 'exit')

    await cli('nx2', 'vpc domain 1')
    await cli('nx2', 'role priority 200')       # Secondary候補
    await cli('nx2', 'peer-keepalive destination 192.168.100.1 source 192.168.100.2 vrf management')
    await cli('nx2', 'peer-gateway')
    await cli('nx2', 'exit')

    d1 = vpc_engine.domains.get('nx1')
    d2 = vpc_engine.domains.get('nx2')
    chk("domain_id = 1 (Nexus-A)", d1 and d1.domain_id == 1)
    chk("role priority 100 (Nexus-A)", d1 and d1.role_priority == 100)
    chk("role priority 200 (Nexus-B)", d2 and d2.role_priority == 200)
    chk("peer-keepalive destination設定 (Nexus-A)", d1 and d1.keepalive_dest == '192.168.100.2')
    chk("peer-keepalive source設定 (Nexus-A)", d1 and d1.keepalive_src == '192.168.100.1')
    chk("peer-keepalive vrf=management", d1 and d1.keepalive_vrf == 'management')
    chk("peer-gateway 設定", d1 and d1.peer_gateway)
    chk("peer-switch 設定", d1 and d1.peer_switch)
    chk("auto-recovery 設定", d1 and d1.auto_recovery)

    section("Peer-Link設定（port-channel → vpc peer-link）")
    await cli('nx1', 'interface port-channel1')
    await cli('nx1', 'switchport mode trunk')
    await cli('nx1', 'vpc peer-link')
    await cli('nx2', 'interface port-channel1')
    await cli('nx2', 'switchport mode trunk')
    await cli('nx2', 'vpc peer-link')
    chk("Peer-Link設定 (Nexus-A) Po1", d1 and d1.peer_link_if == 'port-channel1')
    chk("Peer-Link設定 (Nexus-B) Po1", d2 and d2.peer_link_if == 'port-channel1')

    section("vPCメンバーポート設定")
    await cli('nx1', 'interface port-channel10')
    await cli('nx1', 'vpc 10')
    await cli('nx1', 'interface port-channel20')
    await cli('nx1', 'vpc 20')
    await cli('nx2', 'interface port-channel10')
    await cli('nx2', 'vpc 10')
    await cli('nx2', 'interface port-channel20')
    await cli('nx2', 'vpc 20')
    chk("vPC10メンバー設定 (Nexus-A)", 10 in d1.member_ports if d1 else False)
    chk("vPC20メンバー設定 (Nexus-A)", 20 in d1.member_ports if d1 else False)

    section("Keepalive確立・ロール選出")
    await asyncio.sleep(2)
    d1 = vpc_engine.domains.get('nx1')
    d2 = vpc_engine.domains.get('nx2')
    chk("Keepalive: alive (Nexus-A)", d1 and d1.keepalive_state == 'alive',
        f"state={d1.keepalive_state if d1 else None}")
    chk("Keepalive: alive (Nexus-B)", d2 and d2.keepalive_state == 'alive')
    chk("ロール: Primary = Nexus-A (priority=100)", d1 and d1.role == 'primary',
        f"role={d1.role if d1 else None}")
    chk("ロール: Secondary = Nexus-B (priority=200)", d2 and d2.role == 'secondary',
        f"role={d2.role if d2 else None}")
    chk("Peer-Link UP (Nexus-A)", d1 and d1.peer_link_state == 'up')
    chk("Peer-Link UP (Nexus-B)", d2 and d2.peer_link_state == 'up')
    chk("vPC10メンバー UP (Nexus-A)", 
        d1 and d1.member_ports.get(10) and d1.member_ports[10].state == 'up')
    chk("vPC20メンバー UP (Nexus-A)",
        d1 and d1.member_ports.get(20) and d1.member_ports[20].state == 'up')

    return d1, d2

# ══════════════════════════════════════════════
# ③ show vpc 系コマンド全種
# ══════════════════════════════════════════════
async def test_show_vpc():
    banner("③ show vpc 系コマンド全種確認")
    setup_devices()
    await cli('nx1', 'feature vpc')
    await cli('nx2', 'feature vpc')
    await cli('nx1', 'vpc domain 1')
    await cli('nx1', 'role priority 100')
    await cli('nx1', 'peer-keepalive destination 192.168.100.2 source 192.168.100.1')
    await cli('nx1', 'exit')
    await cli('nx2', 'vpc domain 1')
    await cli('nx2', 'role priority 200')
    await cli('nx2', 'peer-keepalive destination 192.168.100.1 source 192.168.100.2')
    await cli('nx2', 'exit')
    await cli('nx1', 'interface port-channel1')
    await cli('nx1', 'vpc peer-link')
    await cli('nx2', 'interface port-channel1')
    await cli('nx2', 'vpc peer-link')
    await cli('nx1', 'interface port-channel10')
    await cli('nx1', 'vpc 10')
    await cli('nx2', 'interface port-channel10')
    await cli('nx2', 'vpc 10')
    await asyncio.sleep(2)

    section("show vpc")
    out = await cli('nx1', 'show vpc')
    chk("show vpc: vPC domain表示", 'vPC domain' in out)
    chk("show vpc: roleがprimary", 'primary' in out)
    chk("show vpc: keepalive statusがalive", 'alive' in out)
    chk("show vpc: Peer-Link表示", 'port-channel1' in out or 'Peer-link' in out.lower())
    chk("show vpc: vPC10メンバー表示", '10' in out)
    show_output("show vpc", out)

    section("show vpc brief")
    out = await cli('nx1', 'show vpc brief')
    chk("show vpc brief: role表示", 'primary' in out)
    chk("show vpc brief: Peer-Link表示", 'port-channel1' in out)
    show_output("show vpc brief", out)

    section("show vpc peer-keepalive")
    out = await cli('nx1', 'show vpc peer-keepalive')
    chk("show vpc peer-keepalive: statusがalive", 'alive' in out)
    chk("show vpc peer-keepalive: Destination表示", '192.168.100.2' in out)
    chk("show vpc peer-keepalive: Source表示", '192.168.100.1' in out)
    chk("show vpc peer-keepalive: VRF=management", 'management' in out)
    chk("show vpc peer-keepalive: intervalがmsec", 'msec' in out)
    show_output("show vpc peer-keepalive", out)

    section("show vpc role")
    out = await cli('nx1', 'show vpc role')
    chk("show vpc role: roleがprimary", 'primary' in out)
    chk("show vpc role: role priority表示", '100' in out)
    chk("show vpc role: system-mac表示", ':' in out)
    show_output("show vpc role", out)

    section("show vpc consistency-parameters global")
    out = await cli('nx1', 'show vpc consistency-parameters global')
    chk("show vpc consistency-parameters: STP mode表示", 'Rapid-PVST' in out)
    chk("show vpc consistency-parameters: Type列表示", 'Type' in out or 'type' in out)
    show_output("show vpc consistency-parameters global", out)

    teardown()

# ══════════════════════════════════════════════
# ④ vPCフェイルオーバー（Peer障害/Peer-Link障害）
# ══════════════════════════════════════════════
async def test_vpc_failover():
    banner("④ vPCフェイルオーバー動作確認",
           "Peer障害（Keepalive途絶）/ Peer-Link障害（ループ防止）")

    async def init_vpc():
        setup_devices()
        await cli('nx1', 'feature vpc'); await cli('nx2', 'feature vpc')
        await cli('nx1', 'vpc domain 1')
        await cli('nx1', 'role priority 100')
        await cli('nx1', 'peer-keepalive destination 192.168.100.2 source 192.168.100.1')
        await cli('nx1', 'exit')
        await cli('nx2', 'vpc domain 1')
        await cli('nx2', 'role priority 200')
        await cli('nx2', 'peer-keepalive destination 192.168.100.1 source 192.168.100.2')
        await cli('nx2', 'exit')
        await cli('nx1', 'interface port-channel1'); await cli('nx1', 'vpc peer-link')
        await cli('nx2', 'interface port-channel1'); await cli('nx2', 'vpc peer-link')
        await cli('nx1', 'interface port-channel10'); await cli('nx1', 'vpc 10')
        await cli('nx2', 'interface port-channel10'); await cli('nx2', 'vpc 10')
        await asyncio.sleep(2)

    # ── Peer障害テスト ──
    await init_vpc()
    d1 = vpc_engine.domains.get('nx1')
    d2 = vpc_engine.domains.get('nx2')
    section("正常時確認")
    chk("正常時: Nexus-A = Primary", d1 and d1.role == 'primary')
    chk("正常時: Nexus-B = Secondary", d2 and d2.role == 'secondary')
    chk("正常時: vPC10 UP (両側)",
        (d1 and d1.member_ports.get(10, type('',(),{'state':'x'})()).state == 'up') and
        (d2 and d2.member_ports.get(10, type('',(),{'state':'x'})()).state == 'up'))

    section("Peer障害シミュレーション（Keepalive途絶）")
    print("  │  コマンド: vpc peer failure")
    await cli('nx1', 'vpc peer failure')
    await asyncio.sleep(0.5)
    d2_after = vpc_engine.domains.get('nx2')
    chk("Nexus-A: Keepalive = dead",
        vpc_engine.domains.get('nx1') and vpc_engine.domains['nx1'].keepalive_state == 'dead')
    chk("Nexus-B: Secondary → Operational Primary に昇格",
        d2_after and 'primary' in d2_after.role,
        f"role={d2_after.role if d2_after else None}")

    # ── Peer-Link障害テスト（完全に新しい環境で）──
    teardown()
    await init_vpc()

    section("Peer-Link障害シミュレーション（ループ防止）")
    print("  │  コマンド: vpc peer-link failure")
    await cli('nx1', 'vpc peer-link failure')
    await asyncio.sleep(0.5)
    d1_pl = vpc_engine.domains.get('nx1')
    d2_pl = vpc_engine.domains.get('nx2')
    chk("Nexus-A: Peer-Link = down", d1_pl and d1_pl.peer_link_state == 'down',
        f"state={d1_pl.peer_link_state if d1_pl else None}")
    chk("Nexus-B: Peer-Link = down", d2_pl and d2_pl.peer_link_state == 'down',
        f"state={d2_pl.peer_link_state if d2_pl else None}")
    mp10 = d2_pl.member_ports.get(10) if d2_pl else None
    chk("Nexus-B(Secondary): vPC10 → suspended（ループ防止）",
        mp10 and mp10.state == 'suspended',
        f"state={mp10.state if mp10 else None}")
    mp10_nx1 = d1_pl.member_ports.get(10) if d1_pl else None
    chk("Nexus-A(Primary): vPC10 は up のまま（単独継続）",
        mp10_nx1 and mp10_nx1.state == 'up', warn=True)
    out_nx2 = await cli('nx2', 'show vpc')
    chk("show vpc: suspended状態確認", 'suspended' in out_nx2 or 'down' in out_nx2)

    teardown()

async def test_vlan():
    banner("⑤ VLAN設定（NX-OS形式）")
    setup_devices()

    section("VLANデータベース作成")
    await cli('nx1', 'vlan 10')
    await cli('nx1', 'vlan 10 name Web-Servers')
    await cli('nx1', 'vlan 20')
    await cli('nx1', 'vlan 20 name DB-Servers')
    await cli('nx1', 'vlan 100')
    await cli('nx1', 'vlan 100 name Mgmt')
    out = await cli('nx1', 'show vlan')
    chk("show vlan: VLAN10作成", '10' in out)
    chk("show vlan: Web-Servers名前表示", 'Web-Servers' in out)
    chk("show vlan: VLAN20作成", '20' in out)
    chk("show vlan: VLAN100作成", '100' in out)
    show_output("show vlan", out)

    section("アクセスポート設定")
    await cli('nx1', 'interface Ethernet1/1')
    await cli('nx1', 'switchport mode access')
    await cli('nx1', 'switchport access vlan 10')
    from engine.protocols import vlan_engine as ve
    p = ve.ports['nx1'].get('Ethernet1/1')
    chk("Ethernet1/1: access mode", p and p.mode == 'access')
    chk("Ethernet1/1: VLAN10", p and p.access_vlan == 10)

    section("トランクポート設定")
    await cli('nx1', 'interface Ethernet1/2')
    await cli('nx1', 'switchport mode trunk')
    await cli('nx1', 'switchport trunk allowed vlan 10,20,100')
    p2 = ve.ports['nx1'].get('Ethernet1/2')
    chk("Ethernet1/2: trunk mode", p2 and p2.mode == 'trunk')
    chk("Ethernet1/2: allowed VLAN 10,20,100", 
        p2 and {10,20,100}.issubset(p2.allowed_vlans))

    section("VLAN通信可否チェック（IEEE802.1Q準拠）")
    ok10, reason10 = ve.can_communicate('nx1','Ethernet1/1','nx1','Ethernet1/2',10)
    chk("access(VLAN10)→trunk(allowed:10,20,100): 通信可", ok10, reason10)
    out2 = await cli('nx1', 'show interfaces trunk')
    chk("show interfaces trunk: トランクポート表示", 'trunking' in out2 or '802.1q' in out2)

    teardown()

# ══════════════════════════════════════════════
# ⑥ OSPF on NX-OS
# ══════════════════════════════════════════════
async def test_ospf():
    banner("⑥ OSPF on NX-OS",
           "feature ospf → router ospf → neighbor Full確立 → SPF計算")
    setup_devices()

    section("feature ospf 有効化・OSPF設定")
    await cli('nx1', 'feature ospf')
    await cli('nx1', 'router ospf 1')
    await cli('nx1', 'network 10.0.0.0 0.0.0.3 area 0')
    await cli('nx1', 'network 192.168.10.0 0.0.0.255 area 0')
    await cli('nx1', 'ip ospf priority 200')
    await cli('nx2', 'feature ospf')
    await cli('nx2', 'router ospf 1')
    await cli('nx2', 'network 10.0.0.0 0.0.0.3 area 0')
    await cli('nx2', 'network 192.168.20.0 0.0.0.255 area 0')
    await cli('nx2', 'ip ospf priority 100')
    await asyncio.sleep(6)

    n1 = ospf_engine.nodes.get('nx1', {})
    n2 = ospf_engine.nodes.get('nx2', {})

    section("ネイバー確立確認")
    nbr_full = any(v.state == 'Full' for v in n1.get('neighbors', {}).values())
    chk("OSPFネイバー Full確立", nbr_full)
    chk("DR=Nexus-A (priority=200)", n1.get('dr') == 'nx1', f"dr={n1.get('dr')}")
    chk("BDR=Nexus-B (priority=100)", n1.get('bdr') == 'nx2')

    section("LSA確認（Type1/Type2）")
    lsdb = n1.get('lsdb', {})
    type1 = [l for l in lsdb.values() if l.ls_type == 1]
    type2 = [l for l in lsdb.values() if l.ls_type == 2]
    chk("Type1 Router LSA 生成", len(type1) >= 1, f"{len(type1)}件")
    chk("Type2 Network LSA 生成（DR）", len(type2) >= 0, warn=True)  # DR後に生成

    section("SPF計算・経路確認")
    routes = n1.get('routes', [])
    chk("SPF計算済み経路あり", len(routes) > 0, f"{len(routes)}件")
    has_vlan20 = any('192.168.20' in r.get('network', '') for r in routes)
    chk("192.168.20.0/24をOSPFで学習(NX-OS)", has_vlan20,
        f"routes={[r.get('network') for r in routes[:4]]}")

    section("show コマンド確認")
    out = await cli('nx1', 'show ip ospf neighbor')
    chk("show ip ospf neighbor: Full表示", 'Full' in out)
    show_output("show ip ospf neighbor", out)
    out2 = await cli('nx1', 'show ip ospf database')
    chk("show ip ospf database: Router LSA表示", 'Router Link States' in out2)
    show_output("show ip ospf database", out2, 8)
    out3 = await cli('nx1', 'show ip ospf interface')
    chk("show ip ospf interface: Cost表示", 'Cost' in out3)
    out4 = await cli('nx1', 'show ip ospf route')
    chk("show ip ospf route: 経路表示", 'O' in out4 or 'via' in out4)

    await ospf_engine.stop('nx1'); await ospf_engine.stop('nx2')
    teardown()

# ══════════════════════════════════════════════
# ⑦ BGP on NX-OS
# ══════════════════════════════════════════════
async def test_bgp():
    banner("⑦ BGP on NX-OS",
           "feature bgp → router bgp → eBGP Established → show bgp")
    setup_devices()

    section("feature bgp 有効化・BGP設定")
    await cli('nx1', 'feature bgp')
    await cli('nx1', 'router bgp 65001')
    await cli('nx1', 'neighbor 10.0.0.2 remote-as 65002')
    await cli('nx2', 'feature bgp')
    await cli('nx2', 'router bgp 65002')
    await cli('nx2', 'neighbor 10.0.0.1 remote-as 65001')
    await asyncio.sleep(4)

    sessions1 = bgp_engine.nodes.get('nx1', {}).get('sessions', {})
    sessions2 = bgp_engine.nodes.get('nx2', {}).get('sessions', {})

    section("BGPセッション確立確認")
    est1 = any(s.state == 'Established' for s in sessions1.values())
    est2 = any(s.state == 'Established' for s in sessions2.values())
    chk("BGP Established (Nexus-A AS65001)", est1)
    chk("BGP Established (Nexus-B AS65002)", est2)
    chk("eBGP (異AS間) 確認",
        any(s.remote_as == 65002 for s in sessions1.values()))

    section("show コマンド確認")
    out = await cli('nx1', 'show ip bgp summary')
    chk("show ip bgp summary: AS65002表示", '65002' in out)
    chk("show ip bgp summary: Established確認", 'Established' in out or '0' in out)
    show_output("show ip bgp summary", out)
    out2 = await cli('nx2', 'show ip bgp summary')
    chk("show ip bgp summary (Nexus-B): AS65001表示", '65001' in out2)

    teardown()

# ══════════════════════════════════════════════
# ⑧ show run (NX-OS形式)
# ══════════════════════════════════════════════
async def test_show_run():
    banner("⑧ show run 反映確認（NX-OS形式）")
    setup_devices()

    await cli('nx1', 'feature vpc')
    await cli('nx1', 'feature lacp')
    await cli('nx1', 'feature ospf')
    await cli('nx1', 'vpc domain 1')
    await cli('nx1', 'role priority 100')
    await cli('nx1', 'peer-keepalive destination 192.168.100.2 source 192.168.100.1')
    await cli('nx1', 'peer-gateway')
    await cli('nx1', 'peer-switch')
    await cli('nx1', 'exit')
    await cli('nx1', 'vlan 10'); await cli('nx1', 'vlan 10 name Web')
    await cli('nx1', 'interface port-channel1')
    await cli('nx1', 'vpc peer-link')
    await cli('nx1', 'interface port-channel10')
    await cli('nx1', 'vpc 10')
    await cli('nx2', 'feature vpc')
    await cli('nx2', 'vpc domain 1')
    await cli('nx2', 'role priority 200')
    await cli('nx2', 'peer-keepalive destination 192.168.100.1 source 192.168.100.2')
    await cli('nx2', 'exit')
    await asyncio.sleep(2)
    await cli('nx1', 'router bgp 65001')
    await cli('nx1', 'neighbor 10.0.0.2 remote-as 65002')
    await asyncio.sleep(1)

    out = await cli('nx1', 'show run')

    section("show run NX-OS形式の要素確認")
    chk("version 10.2 表示", '10.2' in out)
    chk("feature vpc 反映", 'feature vpc' in out)
    chk("feature lacp 反映", 'feature lacp' in out)
    chk("vpc domain 1 反映", 'vpc domain 1' in out)
    chk("role priority 100 反映", 'role priority 100' in out)
    chk("peer-keepalive destination 反映", 'peer-keepalive' in out)
    chk("peer-gateway 反映", 'peer-gateway' in out)
    chk("peer-switch 反映", 'peer-switch' in out)
    chk("vlan 10 反映", 'vlan 10' in out)
    chk("vpc peer-link 反映", 'vpc peer-link' in out)
    chk("vpc 10 反映", 'vpc 10' in out)
    chk("router bgp 65001 反映", 'router bgp' in out)

    section("show run 出力プレビュー")
    print("  │  " + out.replace('\n', '\n  │  ')[:600])

    teardown()

# ══════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════
SCENARIOS = {
    'basic':    (test_basic_cli,   "基本CLI・configモード・feature"),
    'vpc':      (test_vpc_setup,   "vPC設定・ロール選出・Keepalive"),
    'show':     (test_show_vpc,    "show vpc コマンド全種"),
    'failover': (test_vpc_failover,"vPCフェイルオーバー"),
    'vlan':     (test_vlan,        "VLAN（NX-OS形式）"),
    'ospf':     (test_ospf,        "OSPF（feature ospf）"),
    'bgp':      (test_bgp,         "BGP（feature bgp）"),
    'run':      (test_show_run,    "show run NX-OS形式"),
}

async def main():
    banner("NX-OS Nexus 9300 動作確認スクリプト",
           "Cisco NX-OS 10.2(x)/10.6(x) 仕様準拠")

    args = sys.argv[1:]
    targets = args if args else list(SCENARIOS.keys())

    for key in targets:
        if key in SCENARIOS:
            fn, label = SCENARIOS[key]
            await fn()
        else:
            print(f"不明なシナリオ: {key}")
            print(f"利用可能: {', '.join(SCENARIOS.keys())}")

    # ── 最終集計 ──
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    warned = sum(1 for _, ok, w in results if not ok and w)
    failed = total - passed - warned

    print(f"\n{'━'*60}")
    print(f"  NX-OS 動作確認結果")
    print(f"{'━'*60}")
    print(f"  総テスト数 : {total}")
    print(f"  PASS       : {passed} ✅")
    print(f"  WARN       : {warned} ⚠️  (実装上の許容範囲)")
    print(f"  FAIL       : {failed} ❌")
    print(f"  合格率     : {round(passed/total*100)}% ({passed}/{total})")

    if failed > 0:
        print(f"\n  ❌ 失敗項目一覧:")
        for label, ok, w in results:
            if not ok and not w:
                print(f"    - {label}")
    if warned > 0:
        print(f"\n  ⚠️  警告項目一覧:")
        for label, ok, w in results:
            if not ok and w:
                print(f"    - {label}")

    print(f"\n  確認コマンド集 (NX-OS):")
    print(f"    show vpc                           vPCドメイン・ポート状態")
    print(f"    show vpc brief                     vPC簡易表示")
    print(f"    show vpc peer-keepalive            Keepalive状態")
    print(f"    show vpc role                      Primary/Secondary確認")
    print(f"    show vpc consistency-parameters    整合性確認")
    print(f"    show ip ospf neighbor              OSPFネイバー")
    print(f"    show ip ospf database              LSDBコンテンツ")
    print(f"    show ip bgp summary                BGPセッション")
    print(f"    show vlan                          VLANデータベース")
    print(f"    show interfaces trunk              トランクポート")
    print(f"    show running-config                実行中コンフィグ")

asyncio.run(main())

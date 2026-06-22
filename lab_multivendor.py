"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  マルチベンダー ラボ検証
  APRESIA ─── Catalyst ─── NX-OS ─── Si-R
  
  ネットワーク構成:
    APRESIA  AP-SW1  : 10.0.12.1/30  LAN: 192.168.1.0/24
    Catalyst Cat-SW1 : 10.0.12.2/30  10.0.23.1/30  LAN: 192.168.2.0/24
    NX-OS    Nexus-A : 10.0.23.2/30  10.0.34.1/30  LAN: 192.168.3.0/24
    Si-R     Router-A: 10.0.34.2/30  LAN: 192.168.4.0/24
  
  プロトコル:
    RIP  : APRESIA ─── Catalyst
    OSPF : Catalyst ─── NX-OS
    BGP  : NX-OS ─── Si-R (AS65001 ↔ AS65002)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, asyncio, sys, time
os.environ['NETLAB_FAST_TIMERS'] = '1'
sys.path.insert(0, os.path.dirname(__file__))

import app
from engine.rules import DeviceState
from engine.protocols import (
    vnet, rip_engine, ospf_engine, bgp_engine,
    icmp_engine, rib_engine
)
from engine.syslog_sender import syslog_dispatcher
import engine.syslog_sender as ss
import engine.protocols as proto

# ══════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════
PASS="✅"; FAIL="❌"; INFO="ℹ️ "
results = []

async def cli(dev, cmd):
    r = await app.cli_command({'device_id': dev, 'command': cmd})
    return r['output']

def chk(label, cond, detail=""):
    results.append((label, cond))
    sfx = f"  [{detail[:50]}]" if detail and not cond else ""
    print(f"    {PASS if cond else FAIL} {label}{sfx}")
    return cond

def banner(title):
    print(f"\n{'━'*62}")
    print(f"  {title}")
    print(f"{'━'*62}")

def section(title):
    print(f"\n  ┌─ {title}")

# ══════════════════════════════════════════════
# syslogサーバ受信モック
# ══════════════════════════════════════════════
syslog_received = []
orig_send = ss.send_syslog_async
async def mock_syslog(host, port, fac, sev, hn, msg):
    syslog_received.append({'host':host,'sev':sev,'hn':hn,'msg':msg})
ss.send_syslog_async = mock_syslog

# ══════════════════════════════════════════════
# フェーズ1: 環境構築・IP設定
# ══════════════════════════════════════════════
async def phase1_setup():
    banner("フェーズ1: 装置初期化 / IP設定 / リンク構成")

    section("装置初期化")
    configs = [
        ('ap',  'apresia', 'AP-SW1'),
        ('cat', 'catalyst','Cat-SW1'),
        ('nx',  'nexus',   'Nexus-A'),
        ('sir', 'sir',     'Router-A'),
    ]
    for dev, typ, host in configs:
        app.device_sessions[dev] = DeviceState(typ, host)
        app._register_stub(dev)
        app._register_icmp(dev)
        print(f"    {INFO} {host} ({typ}) 初期化完了")

    section("IPアドレス設定 / リンク構成")
    #
    #  AP-SW1 ──10.0.12.0/30── Cat-SW1 ──10.0.23.0/30── Nexus-A ──10.0.34.0/30── Router-A
    #  192.168.1.0/24          192.168.2.0/24             192.168.3.0/24            192.168.4.0/24
    #
    icmp_engine.register_device('ap', 'AP-SW1', {
        '1/0/1':  {'ip': '10.0.12.1',   'prefix': 30},
        'vlan1':  {'ip': '192.168.1.1',  'prefix': 24},
    })
    icmp_engine.register_device('cat', 'Cat-SW1', {
        'GigabitEthernet1/0/1': {'ip': '10.0.12.2', 'prefix': 30},
        'GigabitEthernet1/0/2': {'ip': '10.0.23.1', 'prefix': 30},
        'Vlan10':               {'ip': '192.168.2.1','prefix': 24},
    })
    icmp_engine.register_device('nx', 'Nexus-A', {
        'Ethernet1/1': {'ip': '10.0.23.2', 'prefix': 30},
        'Ethernet1/2': {'ip': '10.0.34.1', 'prefix': 30},
        'Vlan30':      {'ip': '192.168.3.1','prefix': 24},
    })
    icmp_engine.register_device('sir', 'Router-A', {
        'wan0': {'ip': '10.0.34.2',  'prefix': 30},
        'lan0': {'ip': '192.168.4.1','prefix': 24},
    })

    # リンク接続
    vnet.add_link('ap',  'cat')
    vnet.add_link('cat', 'nx')
    vnet.add_link('nx',  'sir')

    print("    AP-SW1(10.0.12.1) ─── Cat-SW1(10.0.12.2/10.0.23.1) ─── Nexus-A(10.0.23.2/10.0.34.1) ─── Router-A(10.0.34.2)")

    section("syslogサーバ設定 (192.168.1.100)")
    await cli('ap',  'config syslog 192.168.1.100')
    await cli('cat', 'conf t')
    await cli('cat', 'logging host 192.168.1.100')
    await cli('cat', 'logging trap informational')
    await cli('cat', 'exit')
    await cli('nx',  'conf t')
    await cli('nx',  'logging server 192.168.1.100')
    await cli('nx',  'exit')
    await cli('sir', 'configure')
    await cli('sir', 'syslog host 192.168.1.100')
    await cli('sir', 'exit')
    for dev in ['ap','cat','nx','sir']:
        syslog_dispatcher.register(dev, app.device_sessions[dev].syslog_servers)
    print("    全装置 syslog → 192.168.1.100:514 設定完了")

    section("直結セグメント 疎通確認")
    # AP ↔ Cat (10.0.12.x)
    r1 = await cli('ap', 'ping 10.0.12.2')
    chk("AP-SW1 → Cat-SW1 (10.0.12.2) ping", 'Success' in r1 or '64 bytes' in r1)
    # Cat ↔ NX (10.0.23.x)
    r2 = await cli('cat', 'ping 10.0.23.2')
    chk("Cat-SW1 → Nexus-A (10.0.23.2) ping", 'Success' in r2 or '64 bytes' in r2)
    # NX ↔ SiR (10.0.34.x)
    r3 = await cli('nx', 'ping 10.0.34.2')
    chk("Nexus-A → Router-A (10.0.34.2) ping", 'Success' in r3 or '64 bytes' in r3)

# ══════════════════════════════════════════════
# フェーズ2: RIP (APRESIA ─── Catalyst)
# ══════════════════════════════════════════════
async def phase2_rip():
    banner("フェーズ2: RIP設定 (AP-SW1 ─── Cat-SW1)")
    print("  区間: 10.0.12.0/30  広告: 192.168.1.0/24 ↔ 192.168.2.0/24")

    section("RIP設定投入")

    # Catalyst: router rip
    print("    [Cat-SW1] router rip 設定")
    await cli('cat', 'conf t')
    await cli('cat', 'router rip')
    await cli('cat', 'version 2')
    await cli('cat', 'network 10.0.12.0')
    await cli('cat', 'network 192.168.2.0')
    await cli('cat', 'no auto-summary')
    await cli('cat', 'exit')
    await cli('cat', 'exit')

    # APRESIA: ip rip 設定（APRESIAはCisco互換のrip設定を使用）
    print("    [AP-SW1] ip rip 設定")
    await cli('ap', 'router rip')
    await cli('ap', 'network 10.0.12.0')
    await cli('ap', 'network 192.168.1.0')

    section("RIP経路学習待機 (4秒)")
    await asyncio.sleep(4)

    section("RIP動作確認")
    # Catalystの学習経路確認
    rip_cat = rip_engine.nodes.get('cat', {})
    learned_cat = [r for r in rip_cat.get('table', []) if r.learned_from != 'direct']

    rip_ap = rip_engine.nodes.get('ap', {})
    learned_ap = [r for r in rip_ap.get('table', []) if r.learned_from != 'direct']

    chk("Cat-SW1: RIPで192.168.1.0/24を学習",
        any('192.168.1' in r.network for r in learned_cat),
        f"learned={[r.network for r in learned_cat]}")
    chk("AP-SW1:  RIPで192.168.2.0/24を学習",
        any('192.168.2' in r.network for r in learned_ap),
        f"learned={[r.network for r in learned_ap]}")

    # show ip rip route
    out_cat = await cli('cat', 'show ip rip route')
    chk("Cat-SW1: show ip rip route に *R(学習経路)", '*R' in out_cat)

    out_ap = await cli('ap', 'show ip rip route')
    chk("AP-SW1:  show ip rip route に *R(学習経路)", '*R' in out_ap)

    section("RIP経路でping確認")
    # Cat → AP LAN (192.168.1.1) via RIP
    r = await cli('cat', 'ping 192.168.1.1')
    chk("Cat-SW1 → 192.168.1.1 (AP LAN) via RIP ping",
        'Success' in r or '64 bytes' in r, r[:50])

    section("RIPログ・syslog確認")
    rip_syslog = [e for e in syslog_received if 'RIP' in e.get('msg','') or 'RIPV2' in e.get('msg','')]
    chk("RIP syslogサーバ転送あり", len(rip_syslog) > 0,
        f"転送数={len(rip_syslog)}")
    for e in rip_syslog[-2:]:
        print(f"    📤 <{e['sev']}> {e['hn']}: {e['msg'][:55]}")

    # show rip route 表示
    print()
    print("  【Cat-SW1 show ip rip route】")
    for l in out_cat.split('\n')[:8]:
        if l.strip(): print(f"    {l}")

# ══════════════════════════════════════════════
# フェーズ3: OSPF (Catalyst ─── NX-OS)
# ══════════════════════════════════════════════
async def phase3_ospf():
    banner("フェーズ3: OSPF設定 (Cat-SW1 ─── Nexus-A)")
    print("  Area 0  区間: 10.0.23.0/30  広告: 192.168.2.0/24 ↔ 192.168.3.0/24")

    section("OSPF設定投入")
    print("    [Cat-SW1] router ospf 1")
    await cli('cat', 'conf t')
    await cli('cat', 'router ospf 1')
    await cli('cat', 'network 10.0.23.0 0.0.0.3 area 0')
    await cli('cat', 'network 192.168.2.0 0.0.0.255 area 0')
    await cli('cat', 'ip ospf priority 200')
    await cli('cat', 'exit')
    await cli('cat', 'exit')

    print("    [Nexus-A] feature ospf → router ospf 1")
    await cli('nx', 'feature ospf')
    await cli('nx', 'conf t')
    await cli('nx', 'router ospf 1')
    await cli('nx', 'network 10.0.23.0 0.0.0.3 area 0')
    await cli('nx', 'network 192.168.3.0 0.0.0.255 area 0')
    await cli('nx', 'exit')
    await cli('nx', 'exit')

    # デバッグ: OSPF設定直後の状態確認
    from engine.protocols import ospf_engine as _ospf2
    _n1 = _ospf2.nodes.get('cat',{})
    _n2 = _ospf2.nodes.get('nx',{})
    print(f"  [DEBUG] ospf nodes: {list(_ospf2.nodes.keys())}")
    print(f"  [DEBUG] cat: enabled={_n1.get('enabled')} networks={_n1.get('networks')} hello_task={_n1.get('hello_task')}")
    print(f"  [DEBUG] nx:  enabled={_n2.get('enabled')} networks={_n2.get('networks')}")
    section("OSPFネイバー確立待機 (12秒)")
    # RIPエンジンを停止してOSPFに専念
    await rip_engine.stop('cat')
    await rip_engine.stop('ap')
    await asyncio.sleep(12)

    section("OSPF動作確認")
    # ospf_engine.nodesを毎回直接参照
    from engine.protocols import ospf_engine as _ospf
    n_cat = _ospf.nodes.get('cat', {})
    n_nx  = _ospf.nodes.get('nx',  {})

    nbr_cat = n_cat.get('neighbors', {})
    nbr_nx  = n_nx.get('neighbors', {})

    full_cat = any(v.state == 'Full' for v in nbr_cat.values())
    full_nx  = any(v.state == 'Full' for v in nbr_nx.values())

    chk("Cat-SW1: OSPFネイバー Full確立", full_cat,
        f"neighbors={[(k,v.state) for k,v in nbr_cat.items()]}")
    chk("Nexus-A: OSPFネイバー Full確立", full_nx,
        f"neighbors={[(k,v.state) for k,v in nbr_nx.items()]}")
    chk("DR選出: Cat-SW1 (priority=200)",
        n_cat.get('dr') == 'cat', f"dr={n_cat.get('dr')}")
    chk("BDR選出: Nexus-A",
        n_cat.get('bdr') == 'nx',  f"bdr={n_cat.get('bdr')}")

    routes_cat = n_cat.get('routes', [])
    routes_nx  = n_nx.get('routes', [])
    chk("Cat-SW1: 192.168.3.0/24 をOSPFで学習",
        any('192.168.3' in r.get('network','') for r in routes_cat),
        f"routes={[r.get('network') for r in routes_cat[:3]]}")
    chk("Nexus-A: 192.168.2.0/24 をOSPFで学習",
        any('192.168.2' in r.get('network','') for r in routes_nx),
        f"routes={[r.get('network') for r in routes_nx[:3]]}")

    out_ne = await cli('cat', 'show ip ospf neighbor')
    chk("Cat-SW1: show ip ospf neighbor にFull", 'Full' in out_ne)

    out_db = await cli('cat', 'show ip ospf database')
    chk("Cat-SW1: show ip ospf database にRouter LSA", 'Router Link States' in out_db)

    section("OSPF経路でping確認")
    r = await cli('nx', 'ping 192.168.2.1')
    chk("Nexus-A → 192.168.2.1 (Cat LAN) via OSPF ping",
        'Success' in r or '64 bytes' in r, r[:50])

    section("OSPFログ・syslog確認")
    ospf_syslog = [e for e in syslog_received if 'OSPF' in e.get('msg','')]
    chk("OSPF syslogサーバ転送あり", len(ospf_syslog) > 0,
        f"転送数={len(ospf_syslog)}")
    for e in ospf_syslog[-2:]:
        print(f"    📤 <{e['sev']}> {e['hn']}: {e['msg'][:55]}")

    print()
    print("  【Cat-SW1 show ip ospf neighbor】")
    for l in out_ne.split('\n')[:6]:
        if l.strip(): print(f"    {l}")

# ══════════════════════════════════════════════
# フェーズ4: BGP (NX-OS ─── Si-R)
# ══════════════════════════════════════════════
async def phase4_bgp():
    banner("フェーズ4: BGP設定 (Nexus-A AS65001 ─── Router-A AS65002)")
    print("  eBGP  区間: 10.0.34.0/30  広告: 192.168.3.0/24 ↔ 192.168.4.0/24")

    section("BGP設定投入")
    print("    [Nexus-A] feature bgp → router bgp 65001")
    await cli('nx', 'feature bgp')
    await cli('nx', 'conf t')
    await cli('nx', 'router bgp 65001')
    await cli('nx', 'neighbor 10.0.34.2 remote-as 65002')
    await cli('nx', 'exit')
    await cli('nx', 'exit')

    print("    [Router-A] router bgp 65002")
    await cli('sir', 'configure')
    await cli('sir', 'router bgp 65002')
    await cli('sir', 'neighbor 10.0.34.1 remote-as 65001')
    await cli('sir', 'exit')

    section("BGPセッション確立待機 (5秒)")
    await asyncio.sleep(5)

    section("BGP動作確認")
    sess_nx  = bgp_engine.nodes.get('nx',  {}).get('sessions', {})
    sess_sir = bgp_engine.nodes.get('sir', {}).get('sessions', {})

    est_nx  = any(s.state == 'Established' for s in sess_nx.values())
    est_sir = any(s.state == 'Established' for s in sess_sir.values())

    chk("Nexus-A:  BGP Established (AS65001→AS65002)", est_nx)
    chk("Router-A: BGP Established (AS65002→AS65001)", est_sir)
    chk("eBGP確認: remote-as=65002",
        any(s.remote_as == 65002 for s in sess_nx.values()))

    out_nx  = await cli('nx',  'show ip bgp summary')
    out_sir = await cli('sir', 'show ip bgp summary')
    chk("Nexus-A:  show ip bgp summary にAS65002", '65002' in out_nx)
    chk("Router-A: show ip bgp summary にAS65001", '65001' in out_sir)

    section("BGPログ・syslog確認")
    bgp_syslog = [e for e in syslog_received if 'BGP' in e.get('msg','')]
    chk("BGP syslogサーバ転送あり", len(bgp_syslog) > 0,
        f"転送数={len(bgp_syslog)}")
    for e in bgp_syslog[-2:]:
        print(f"    📤 <{e['sev']}> {e['hn']}: {e['msg'][:55]}")

    print()
    print("  【Nexus-A show ip bgp summary】")
    for l in out_nx.split('\n')[:8]:
        if l.strip(): print(f"    {l}")

# ══════════════════════════════════════════════
# フェーズ5: show run / show logging 確認
# ══════════════════════════════════════════════
async def phase5_show():
    banner("フェーズ5: show run / show logging 全機種確認")

    for dev, host, log_cmd in [
        ('ap',  'AP-SW1',   'show logging'),
        ('cat', 'Cat-SW1',  'show logging'),
        ('nx',  'Nexus-A',  'show logging'),
        ('sir', 'Router-A', 'show logging syslog'),
    ]:
        section(f"{host} ({app.device_sessions[dev].device_type})")
        run = await cli(dev, 'show run')
        log = await cli(dev, log_cmd)

        log_lines = [l for l in log.split('\n')
                     if l.strip() and not l.startswith('Syslog') and
                     not l.startswith('No ') and not l.startswith('    ') and
                     ('%' in l or 'Si-R' in l or 'informational' in l or 'warning' in l)]

        print(f"    show run: {len(run.split(chr(10)))}行")
        print(f"    show logging: {len(log_lines)}件のログ")
        for l in log_lines[-4:]:
            print(f"      {l[:70]}")

# ══════════════════════════════════════════════
# フェーズ6: エンドツーエンドpingとtraceroute
# ══════════════════════════════════════════════
async def phase6_e2e():
    banner("フェーズ6: エンドツーエンド疎通確認")
    print("  スタティックルートで中継経路を補完してE2E確認")

    section("スタティックルート補完（RIP/OSPF/BGP区間をまたぐルート）")
    # Cat → Router-A LANへのルート (OSPFでNX経由 + BGPでSiR)
    await cli('cat', 'conf t')
    await cli('cat', 'ip route 192.168.4.0/24 10.0.23.2')  # Cat → NX → SiR LAN
    await cli('cat', 'ip route 10.0.34.0/24 10.0.23.2')
    await cli('cat', 'exit')
    # NX → AP LAN
    await cli('nx', 'conf t')
    await cli('nx', 'ip route 192.168.1.0/24 10.0.23.1')   # NX → Cat → AP LAN
    await cli('nx', 'ip route 10.0.12.0/24 10.0.23.1')
    await cli('nx', 'exit')
    # SiR → Cat/AP LAN
    await cli('sir', 'configure')
    await cli('sir', 'ip route 192.168.2.0/24 10.0.34.1')
    await cli('sir', 'ip route 192.168.1.0/24 10.0.34.1')
    await cli('sir', 'ip route 10.0.12.0/24 10.0.34.1')
    await cli('sir', 'ip route 10.0.23.0/24 10.0.34.1')
    await cli('sir', 'exit')
    # AP → 全経路
    await cli('ap', 'ip route 192.168.2.0/24 10.0.12.2')
    await cli('ap', 'ip route 192.168.3.0/24 10.0.12.2')
    await cli('ap', 'ip route 192.168.4.0/24 10.0.12.2')
    await cli('ap', 'ip route 10.0.23.0/24 10.0.12.2')
    await cli('ap', 'ip route 10.0.34.0/24 10.0.12.2')

    section("全区間 ping 確認")
    ping_tests = [
        ('ap',  '192.168.2.1', 'AP-SW1 → Cat LAN   (192.168.2.1)'),
        ('ap',  '192.168.3.1', 'AP-SW1 → Nexus LAN (192.168.3.1)'),
        ('ap',  '192.168.4.1', 'AP-SW1 → SiR LAN   (192.168.4.1)'),
        ('cat', '192.168.1.1', 'Cat    → AP LAN    (192.168.1.1)'),
        ('cat', '192.168.3.1', 'Cat    → Nexus LAN (192.168.3.1)'),
        ('cat', '192.168.4.1', 'Cat    → SiR LAN   (192.168.4.1)'),
        ('nx',  '192.168.1.1', 'Nexus  → AP LAN    (192.168.1.1)'),
        ('nx',  '192.168.2.1', 'Nexus  → Cat LAN   (192.168.2.1)'),
        ('nx',  '192.168.4.1', 'Nexus  → SiR LAN   (192.168.4.1)'),
        ('sir', '192.168.1.1', 'SiR    → AP LAN    (192.168.1.1)'),
        ('sir', '192.168.2.1', 'SiR    → Cat LAN   (192.168.2.1)'),
        ('sir', '192.168.3.1', 'SiR    → Nexus LAN (192.168.3.1)'),
    ]
    for dev, dest, label in ping_tests:
        r = await cli(dev, f'ping {dest}')
        ok = 'Success' in r or '64 bytes' in r
        chk(label, ok)

    section("traceroute (AP-SW1 → Router-A LAN: 192.168.4.1)")
    tr = await cli('ap', 'traceroute 192.168.4.1')
    print()
    for l in tr.split('\n'):
        if l.strip(): print(f"    {l}")
    chk("traceroute: 全ホップ到達", '192.168.4' in tr)

# ══════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════
async def main():
    banner("マルチベンダー ラボ検証")
    print(f"  APRESIA ─ RIP ─ Catalyst ─ OSPF ─ NX-OS ─ BGP ─ Si-R")
    print(f"  開始時刻: {time.strftime('%Y/%m/%d %H:%M:%S')}")

    await phase1_setup()
    await phase2_rip()
    await phase3_ospf()
    await phase4_bgp()
    await phase5_show()
    await phase6_e2e()

    # ── 総合集計 ──
    total  = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed

    print()
    print("="*62)
    print("  総合検証結果")
    print("="*62)
    print(f"  総テスト数 : {total}")
    print(f"  PASS       : {passed} ✅")
    print(f"  FAIL       : {failed} ❌")
    print(f"  合格率     : {round(passed/total*100)}% ({passed}/{total})")
    print(f"  syslog転送 : {len(syslog_received)}件 → 192.168.1.100")

    if failed:
        print(f"\n  ❌ 失敗項目:")
        for label, ok in results:
            if not ok: print(f"    - {label}")

    print()
    print("  構成サマリ:")
    print("  ┌──────────┬──────────────────┬──────────────────┐")
    print("  │ 機種     │ インタフェース    │ プロトコル       │")
    print("  ├──────────┼──────────────────┼──────────────────┤")
    print("  │ APRESIA  │ 10.0.12.1/30     │ RIP(広告側)      │")
    print("  │          │ 192.168.1.1/24   │                  │")
    print("  ├──────────┼──────────────────┼──────────────────┤")
    print("  │ Catalyst │ 10.0.12.2/30     │ RIP + OSPF       │")
    print("  │          │ 10.0.23.1/30     │ (DR priority=200)│")
    print("  │          │ 192.168.2.1/24   │                  │")
    print("  ├──────────┼──────────────────┼──────────────────┤")
    print("  │ NX-OS    │ 10.0.23.2/30     │ OSPF + BGP       │")
    print("  │          │ 10.0.34.1/30     │ AS65001          │")
    print("  │          │ 192.168.3.1/24   │                  │")
    print("  ├──────────┼──────────────────┼──────────────────┤")
    print("  │ Si-R     │ 10.0.34.2/30     │ BGP AS65002      │")
    print("  │          │ 192.168.4.1/24   │                  │")
    print("  └──────────┴──────────────────┴──────────────────┘")

    for t in list(proto._background_tasks): t.cancel()

asyncio.run(main())

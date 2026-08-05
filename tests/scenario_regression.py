#!/usr/bin/env python3
"""
シナリオ・リグレッションテスト（HTTP経由・実サーバ駆動）

対象トポロジー/機能:
  1) 3台Ciscoルータ チェーン OSPF: 多段でのルート交換 + エンドツーエンドping
  2) 3台Catalystスイッチ トライアングル STP: ルート選出 + ブロッキングポート
  3) 2台Catalyst EtherChannel: メンバーポート障害/復旧の反映

使い方:
  NETLAB_AUTH_DISABLE=1 NETLAB_FAST_TIMERS=1 uvicorn app:app --port 8099 &
  python tests/scenario_regression.py [http://127.0.0.1:8099]

終了コード: 全PASSで0、1件でも失敗で1。
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
_fails = []


def _post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def cli(dev, cmd):
    return _post("/api/cli", {"device_id": dev, "command": cmd}).get("output", "")


def conf(dev, *cmds):
    cli(dev, "enable")
    cli(dev, "configure terminal")
    for c in cmds:
        cli(dev, c)


def device(dev_id, dtype):
    _post("/api/device", {"id": dev_id, "type": dtype, "hostname": dev_id})


def link(a, b, ia, ib):
    _post("/api/link", {"a": a, "b": b, "iface_a": ia, "iface_b": ib})


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── シナリオ1: 3台Cisco OSPF チェーン ──────────────────────
def scenario_ospf_chain():
    print("== シナリオ1: 3台Cisco OSPF チェーン (r1-r2-r3) ==")
    for d in ("r1", "r2", "r3"):
        device(d, "cisco")
    link("r1", "r2", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("r2", "r3", "GigabitEthernet0/0/0", "GigabitEthernet0/0/0")
    conf("r1",
         "interface GigabitEthernet0/0/1", "ip address 10.1.12.1 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.1.1 255.255.255.0", "no shutdown", "exit",
         "router ospf 1", "network 10.1.12.0 0.0.0.255 area 0",
         "network 192.168.1.0 0.0.0.255 area 0", "exit")
    conf("r2",
         "interface GigabitEthernet0/0/1", "ip address 10.1.12.2 255.255.255.0", "no shutdown", "exit",
         "interface GigabitEthernet0/0/0", "ip address 10.1.23.2 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.2.1 255.255.255.0", "no shutdown", "exit",
         "router ospf 1", "network 10.1.12.0 0.0.0.255 area 0",
         "network 10.1.23.0 0.0.0.255 area 0",
         "network 192.168.2.0 0.0.0.255 area 0", "exit")
    conf("r3",
         "interface GigabitEthernet0/0/0", "ip address 10.1.23.3 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.3.1 255.255.255.0", "no shutdown", "exit",
         "router ospf 1", "network 10.1.23.0 0.0.0.255 area 0",
         "network 192.168.3.0 0.0.0.255 area 0", "exit")
    time.sleep(6)
    r1 = cli("r1", "show ip route ospf")
    r3 = cli("r3", "show ip route ospf")
    # R1 は R3 の LAN を、R3 は R1 の LAN を多段学習しているべき
    check("R1がR3のLAN 192.168.3.0 をOSPF学習", "O" in r1 and "192.168.3.0" in r1, r1)
    check("R1がR2のLAN 192.168.2.0 をOSPF学習", "192.168.2.0" in r1, r1)
    check("R3がR1のLAN 192.168.1.0 をOSPF学習(逆方向)", "192.168.1.0" in r3, r3)
    # エンドツーエンド疎通
    p13 = cli("r1", "ping 192.168.3.1")
    p31 = cli("r3", "ping 192.168.1.1")
    check("R1->R3 LAN ping 成功", "Success rate is 100" in p13, p13)
    check("R3->R1 LAN ping 成功", "Success rate is 100" in p31, p31)


# ── シナリオ2: 3台Catalyst STP トライアングル ───────────────
def scenario_stp_triangle():
    print("== シナリオ2: 3台Catalyst STP トライアングル ==")
    for d in ("sw1", "sw2", "sw3"):
        device(d, "catalyst")
    link("sw1", "sw2", "Gi1/0/1", "Gi1/0/1")
    link("sw2", "sw3", "Gi1/0/2", "Gi1/0/2")
    link("sw1", "sw3", "Gi1/0/3", "Gi1/0/3")
    for d, pri in (("sw1", 4096), ("sw2", 8192), ("sw3", 32768)):
        conf(d, f"spanning-tree vlan 1 priority {pri}", "spanning-tree mode rapid-pvst")
    time.sleep(5)
    s1 = cli("sw1", "show spanning-tree")
    s3 = cli("sw3", "show spanning-tree")
    check("SW1(最小優先度)がルートブリッジ", "This bridge is the root" in s1, s1)
    check("SW3はルートではない", "This bridge is the root" not in s3, s3)
    check("SW3にブロッキング/Alternateポートがある(ループ遮断)",
          "Altn" in s3 or "BLK" in s3, s3)
    # 動的再収束: ルートSW1をダウン → SW2が新ルート、SW3が迂回
    conf("sw1", "interface Gi1/0/1", "shutdown", "exit",
         "interface Gi1/0/3", "shutdown", "exit")
    time.sleep(5)
    s2_fo = cli("sw2", "show spanning-tree")
    s3_fo = cli("sw3", "show spanning-tree")
    check("ルート障害でSW2が新ルートに再選出",
          "This bridge is the root" in s2_fo, s2_fo)
    check("SW3が新ルートSW2配下へ再収束(ルートID変化)",
          "8193" in s3_fo.split("Bridge ID")[0], s3_fo)
    # 復旧: SW1が戻ると元のルートへ
    conf("sw1", "interface Gi1/0/1", "no shutdown", "exit",
         "interface Gi1/0/3", "no shutdown", "exit")
    time.sleep(5)
    s3_rec = cli("sw3", "show spanning-tree")
    check("SW1復旧で元のルート(4097)へ復帰",
          "4097" in s3_rec.split("Bridge ID")[0], s3_rec)


# ── シナリオ3: EtherChannel メンバー障害/復旧 ─────────────────
def scenario_etherchannel():
    print("== シナリオ3: Catalyst EtherChannel メンバー障害/復旧 ==")
    for d in ("ec1", "ec2"):
        device(d, "catalyst")
    link("ec1", "ec2", "Gi1/0/1", "Gi1/0/1")
    link("ec1", "ec2", "Gi1/0/2", "Gi1/0/2")
    for d in ("ec1", "ec2"):
        conf(d, "interface range gi1/0/1 - 2", "channel-group 1 mode active", "exit")
    conf("ec1", "interface vlan 1", "ip address 10.9.9.1 255.255.255.0", "no shutdown", "exit")
    conf("ec2", "interface vlan 1", "ip address 10.9.9.2 255.255.255.0", "no shutdown", "exit")
    time.sleep(1)
    base = cli("ec1", "show etherchannel summary")
    check("EtherChannel成立(両メンバーP)", "Gi1/0/1(P)" in base and "Gi1/0/2(P)" in base, base)
    # メンバー1停止
    conf("ec1", "interface gi1/0/1", "shutdown", "exit")
    time.sleep(1)
    d1 = cli("ec1", "show etherchannel summary")
    p1 = cli("ec1", "ping 10.9.9.2")
    check("Gi1/0/1停止で(D)表示・Po維持(SU)", "Gi1/0/1(D)" in d1 and "SU" in d1, d1)
    check("片系停止でもping継続", "Success rate is 100" in p1, p1)
    # メンバー1復旧
    conf("ec1", "interface gi1/0/1", "no shutdown", "exit")
    time.sleep(1)
    d2 = cli("ec1", "show etherchannel summary")
    check("Gi1/0/1復旧で(P)表示に戻る", "Gi1/0/1(P)" in d2, d2)
    # メンバー2停止→復旧
    conf("ec1", "interface gi1/0/2", "shutdown", "exit")
    time.sleep(1)
    d3 = cli("ec1", "show etherchannel summary")
    check("Gi1/0/2停止で(D)表示", "Gi1/0/2(D)" in d3, d3)
    conf("ec1", "interface gi1/0/2", "no shutdown", "exit")
    time.sleep(1)
    d4 = cli("ec1", "show etherchannel summary")
    check("Gi1/0/2復旧で両メンバーP", "Gi1/0/1(P)" in d4 and "Gi1/0/2(P)" in d4, d4)


def scenario_rip_chain():
    print("== シナリオ1b: 3台Cisco RIP チェーン (rp1-rp2-rp3, 広域イーサ中継) ==")
    for d in ("rp1", "rp2", "rp3"):
        device(d, "cisco")
    link("rp1", "rp2", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("rp2", "rp3", "GigabitEthernet0/0/0", "GigabitEthernet0/0/0")
    conf("rp1",
         "interface GigabitEthernet0/0/1", "ip address 10.2.12.1 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.11.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.11.0", "network 192.168.12.0", "network 192.168.13.0", "exit")
    conf("rp2",
         "interface GigabitEthernet0/0/1", "ip address 10.2.12.2 255.255.255.0", "no shutdown", "exit",
         "interface GigabitEthernet0/0/0", "ip address 10.2.23.2 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.12.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.11.0", "network 192.168.12.0", "network 192.168.13.0", "exit")
    conf("rp3",
         "interface GigabitEthernet0/0/0", "ip address 10.2.23.3 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.13.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.11.0", "network 192.168.12.0", "network 192.168.13.0", "exit")
    time.sleep(7)
    r1 = cli("rp1", "show ip route rip")
    r3 = cli("rp3", "show ip route rip")
    # RIPは距離ベクトル: 2ホップ先のLANはメトリック3で学習
    check("RP1がRP3のLAN 192.168.13.0 をRIP学習(metric3)",
          "192.168.13.0" in r1 and "/3" in r1, r1)
    check("RP3がRP1のLAN 192.168.11.0 をRIP学習(逆方向)", "192.168.11.0" in r3, r3)
    p13 = cli("rp1", "ping 192.168.13.1")
    p31 = cli("rp3", "ping 192.168.11.1")
    check("RP1->RP3 LAN ping 成功", "Success rate is 100" in p13, p13)
    check("RP3->RP1 LAN ping 成功", "Success rate is 100" in p31, p31)


def scenario_bgp_aws():
    print("== シナリオ4: 擬似AWS eBGP (cgw AS65000 <-> aws VGW AS64512) ==")
    for d in ("cgw", "aws"):
        device(d, "cisco")
    link("cgw", "aws", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    conf("cgw",
         "interface GigabitEthernet0/0/1", "ip address 169.254.1.1 255.255.255.252", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.100.1 255.255.255.0", "no shutdown", "exit",
         "router bgp 65000", "neighbor 169.254.1.2 remote-as 64512",
         "network 192.168.100.0 mask 255.255.255.0", "exit")
    conf("aws",
         "interface GigabitEthernet0/0/1", "ip address 169.254.1.2 255.255.255.252", "no shutdown", "exit",
         "interface Loopback0", "ip address 10.100.0.1 255.255.0.0", "no shutdown", "exit",
         "router bgp 64512", "neighbor 169.254.1.1 remote-as 65000",
         "network 10.100.0.0 mask 255.255.0.0", "exit")
    time.sleep(5)
    cgw = cli("cgw", "show ip bgp")
    aws = cli("aws", "show ip bgp")
    summ = cli("cgw", "show ip bgp summary")
    # CGWはAWSのVPC 10.100.0.0/16 を AS64512 経由で学習
    check("CGWがVPC 10.100.0.0/16 を学習(マスク維持)", "10.100.0.0/16" in cgw, cgw)
    check("学習経路にAS-path 64512 が付く", "64512" in cgw, cgw)
    check("AWSが顧客 192.168.100.0/24 を学習", "192.168.100.0/24" in aws, aws)
    check("BGPネイバーが確立(summary)", "64512" in summ, summ)


def scenario_aws_dx_vpn():
    print("== シナリオ6: 擬似AWS Direct Connect + VPNバックアップ "
          "(MD5/prepend/BFD/フェイルオーバー) ==")
    for d in ("cg", "adx", "avpn"):
        device(d, "cisco")
    link("cg", "adx", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("cg", "avpn", "GigabitEthernet0/0/2", "GigabitEthernet0/0/1")
    conf("cg",
         "interface GigabitEthernet0/0/1", "ip address 169.254.10.1 255.255.255.252", "no shutdown", "exit",
         "interface GigabitEthernet0/0/2", "ip address 169.254.20.1 255.255.255.252", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.200.1 255.255.255.0", "no shutdown", "exit",
         "route-map DX-IN permit 10", "set local-preference 200", "exit",
         "router bgp 65000",
         "neighbor 169.254.10.2 remote-as 64512", "neighbor 169.254.10.2 password KEY1",
         "neighbor 169.254.10.2 fall-over bfd", "neighbor 169.254.10.2 route-map DX-IN in",
         "neighbor 169.254.20.2 remote-as 64512", "neighbor 169.254.20.2 password KEY2",
         "network 192.168.200.0 mask 255.255.255.0", "exit")
    conf("adx",
         "interface GigabitEthernet0/0/1", "ip address 169.254.10.2 255.255.255.252", "no shutdown", "exit",
         "interface Loopback0", "ip address 10.200.0.1 255.255.0.0", "no shutdown", "exit",
         "router bgp 64512", "neighbor 169.254.10.1 remote-as 65000",
         "neighbor 169.254.10.1 password KEY1", "neighbor 169.254.10.1 fall-over bfd",
         "network 10.200.0.0 mask 255.255.0.0", "exit")
    conf("avpn",
         "interface GigabitEthernet0/0/1", "ip address 169.254.20.2 255.255.255.252", "no shutdown", "exit",
         "interface Loopback0", "ip address 10.200.0.2 255.255.0.0", "no shutdown", "exit",
         "route-map VPN-OUT permit 10", "set as-path prepend 64512 64512", "exit",
         "router bgp 64512", "neighbor 169.254.20.1 remote-as 65000",
         "neighbor 169.254.20.1 password KEY2", "neighbor 169.254.20.1 route-map VPN-OUT out",
         "network 10.200.0.0 mask 255.255.0.0", "exit")
    time.sleep(6)
    bgp = cli("cg", "show ip bgp")
    bfd = cli("cg", "show bfd neighbors")
    # DXがベスト（local-pref200, AS-path短）、VPNは冗長候補（prepend）
    dx_best = ("*> 10.200.0.0/16" in bgp and "169.254.10.2" in bgp and "200" in bgp)
    check("DXがベストパス(local-pref200)", dx_best, bgp)
    check("VPN冗長経路がAS-path prependされている", "64512 64512 64512" in bgp, bgp)
    check("BFDセッションがUp(DX)", "Up" in bfd and "169.254.10.2" in bfd, bfd)
    # フェイルオーバー: DX障害 → VPNへ
    conf("cg", "interface GigabitEthernet0/0/1", "shutdown", "exit")
    time.sleep(2)
    bgp_fo = cli("cg", "show ip bgp")
    check("DX障害でVPN(169.254.20.2)へフェイルオーバー",
          "*> 10.200.0.0/16" in bgp_fo and "169.254.20.2" in bgp_fo, bgp_fo)
    # 復旧: DXへ戻る
    conf("cg", "interface GigabitEthernet0/0/1", "no shutdown", "exit")
    time.sleep(4)
    bgp_rec = cli("cg", "show ip bgp")
    check("DX復旧でベストパスがDXへ戻る",
          "*> 10.200.0.0/16" in bgp_rec and "169.254.10.2" in bgp_rec, bgp_rec)
    # MD5不一致: VPNパスワードを誤りに → セッション確立不可
    conf("avpn", "router bgp 64512", "neighbor 169.254.20.1 password BADKEY", "exit")
    conf("cg", "interface GigabitEthernet0/0/2", "shutdown", "exit")
    time.sleep(1)
    conf("cg", "interface GigabitEthernet0/0/2", "no shutdown", "exit")
    time.sleep(3)
    summ = cli("cg", "show ip bgp summary")
    vpn_line = [l for l in summ.splitlines() if "169.254.20.2" in l]
    check("MD5不一致でVPNセッションが確立しない(Active)",
          bool(vpn_line) and "Active" in vpn_line[0], summ)


def scenario_bgp_transit():
    print("== シナリオ7: BGP 3-ASトランジット (b1 AS65001 - b2 AS65002 - b3 AS65003) ==")
    for d in ("bt1", "bt2", "bt3"):
        device(d, "cisco")
    link("bt1", "bt2", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("bt2", "bt3", "GigabitEthernet0/0/0", "GigabitEthernet0/0/0")
    conf("bt1",
         "interface GigabitEthernet0/0/1", "ip address 10.12.0.1 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 172.16.1.1 255.255.255.0", "no shutdown", "exit",
         "router bgp 65001", "neighbor 10.12.0.2 remote-as 65002",
         "network 172.16.1.0 mask 255.255.255.0", "exit")
    conf("bt2",
         "interface GigabitEthernet0/0/1", "ip address 10.12.0.2 255.255.255.0", "no shutdown", "exit",
         "interface GigabitEthernet0/0/0", "ip address 10.23.0.2 255.255.255.0", "no shutdown", "exit",
         "router bgp 65002", "neighbor 10.12.0.1 remote-as 65001",
         "neighbor 10.23.0.3 remote-as 65003",
         "network 172.16.2.0 mask 255.255.255.0", "exit")
    conf("bt3",
         "interface GigabitEthernet0/0/0", "ip address 10.23.0.3 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 172.16.3.1 255.255.255.0", "no shutdown", "exit",
         "router bgp 65003", "neighbor 10.23.0.2 remote-as 65002",
         "network 172.16.3.0 mask 255.255.255.0", "exit")
    time.sleep(7)
    b1 = cli("bt1", "show ip bgp")
    b3 = cli("bt3", "show ip bgp")
    # トランジット: b1 は b3 の経路を AS-path 65002 65003 で学習
    check("B1がB3経路172.16.3.0をトランジット学習(AS-path 65002 65003)",
          "172.16.3.0/24" in b1 and "65002 65003" in b1, b1)
    check("B3がB1経路172.16.1.0をトランジット学習(AS-path 65002 65001)",
          "172.16.1.0/24" in b3 and "65002 65001" in b3, b3)


def scenario_rip_distribute_list():
    print("== シナリオ5: RIP + distribute-list (prefix-list / 標準ACL) ==")
    # dl1-dl2-dl3。dl2がdl3のLANを2種のdistribute-listで抑制
    for d in ("dla", "dlb", "dlc"):
        device(d, "cisco")
    link("dla", "dlb", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("dlb", "dlc", "GigabitEthernet0/0/0", "GigabitEthernet0/0/0")
    conf("dla",
         "interface GigabitEthernet0/0/1", "ip address 10.5.12.1 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.81.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.81.0", "exit")
    conf("dlc",
         "interface GigabitEthernet0/0/0", "ip address 10.5.23.3 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.83.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.83.0", "exit")
    # (a) prefix-list ベース
    conf("dlb",
         "interface GigabitEthernet0/0/1", "ip address 10.5.12.2 255.255.255.0", "no shutdown", "exit",
         "interface GigabitEthernet0/0/0", "ip address 10.5.23.2 255.255.255.0", "no shutdown", "exit",
         "ip prefix-list BLK83 seq 5 deny 192.168.83.0/24",
         "ip prefix-list BLK83 seq 10 permit 0.0.0.0/0 le 32",
         "router rip", "version 2", "network 10.0.0.0", "distribute-list BLK83 out", "exit")
    time.sleep(7)
    r_pl = cli("dla", "show ip route rip")
    dlb_has = cli("dlb", "show ip route rip")
    check("DLB自身は192.168.83.0を保持", "192.168.83.0" in dlb_has, dlb_has)
    check("prefix-list distribute-listでDLAに192.168.83.0が来ない",
          "192.168.83.0" not in r_pl, r_pl)
    check("抑制対象外の10.5.23.0はDLAに届く", "10.5.23.0" in r_pl, r_pl)

    # (b) 標準ACL番号ベース: 別トポロジーで検証
    for d in ("ala", "alb", "alc"):
        device(d, "cisco")
    link("ala", "alb", "GigabitEthernet0/0/1", "GigabitEthernet0/0/1")
    link("alb", "alc", "GigabitEthernet0/0/0", "GigabitEthernet0/0/0")
    conf("ala",
         "interface GigabitEthernet0/0/1", "ip address 10.6.12.1 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.91.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.91.0", "exit")
    conf("alc",
         "interface GigabitEthernet0/0/0", "ip address 10.6.23.3 255.255.255.0", "no shutdown", "exit",
         "interface Loopback0", "ip address 192.168.93.1 255.255.255.0", "no shutdown", "exit",
         "router rip", "version 2", "network 10.0.0.0", "network 192.168.93.0", "exit")
    conf("alb",
         "interface GigabitEthernet0/0/1", "ip address 10.6.12.2 255.255.255.0", "no shutdown", "exit",
         "interface GigabitEthernet0/0/0", "ip address 10.6.23.2 255.255.255.0", "no shutdown", "exit",
         "access-list 20 deny 192.168.93.0 0.0.0.255", "access-list 20 permit any",
         "router rip", "version 2", "network 10.0.0.0", "distribute-list 20 out", "exit")
    time.sleep(7)
    r_acl = cli("ala", "show ip route rip")
    check("標準ACL distribute-listでALAに192.168.93.0が来ない",
          "192.168.93.0" not in r_acl, r_acl)
    check("抑制対象外の10.6.23.0はALAに届く", "10.6.23.0" in r_acl, r_acl)


def main():
    print(f"リグレッション実行 → {BASE}\n")
    scenario_ospf_chain()
    scenario_rip_chain()
    scenario_stp_triangle()
    scenario_etherchannel()
    scenario_bgp_aws()
    scenario_aws_dx_vpn()
    scenario_bgp_transit()
    scenario_rip_distribute_list()
    print()
    if _fails:
        print(f"結果: FAIL ({len(_fails)}件) -> {_fails}")
        sys.exit(1)
    print("結果: 全PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()

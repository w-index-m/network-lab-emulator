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


def main():
    print(f"リグレッション実行 → {BASE}\n")
    scenario_ospf_chain()
    scenario_stp_triangle()
    scenario_etherchannel()
    print()
    if _fails:
        print(f"結果: FAIL ({len(_fails)}件) -> {_fails}")
        sys.exit(1)
    print("結果: 全PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()

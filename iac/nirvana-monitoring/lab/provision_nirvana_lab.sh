#!/usr/bin/env bash
# エミュレータ上に監視対象を作る: Grafana 版と同じ CR1 / CR2 に、sFlow を出す Nexus(NX1)を足す
#
#   [LAN 10.240.1.0/24] CR1 ── 10.240.12.0/30 ── CR2 ── 10.240.23.0/30 ── NX1(Nexus)
#
#   - CR1 / CR2: SNMP(netlab-ro)、syslog → 127.0.0.1:5515、NetFlow v5 → 127.0.0.1:2055(ip flow ingress)、
#                SNMP trap(linkDown/linkUp)→ 127.0.0.1:1162(ニルヴァーナ)、送信元は trap-source の Gi0/0/0
#   - SNMP の許可: ACL 20(標準 ACL)を community と v3 グループに付ける。実機なら「収集サーバーの IP だけ」を
#     permit する。このラボは1台のホストの lo に全装置の IP が載るので、snmp_exporter / ニルヴァーナの問い合わせの
#     送信元は宛先の装置 IP そのものになる。そのためラボでは 10.240.0.0/16 を permit している
#     (それ以外の送信元からは応答しない。deploy.sh check で確かめる)
#   - NX1      : 管理 IP は mgmt0 の 10.240.99.3(SNMP はここで受ける。実機の NX-OS と同じ)、syslog、
#                sFlow → 127.0.0.1:6343(sampling-rate 64、agent-ip はデータ側の 10.240.23.2)
set -euo pipefail
cd "$(dirname "$0")"
EMU="${EMU_URL:-http://localhost:8000}"
TOKEN_HDR=()
[ -n "${NETLAB_TOKEN:-}" ] && TOKEN_HDR=(-H "X-Session-Token: ${NETLAB_TOKEN}")
post() { curl -sf "${TOKEN_HDR[@]}" -H 'Content-Type: application/json' -X POST "$EMU$1" -d "$2" >/dev/null; }
cli()  { local dev=$1; shift; for c in "$@"; do
           post /api/cli "$(python3 -c 'import json,sys;print(json.dumps({"device_id":sys.argv[1],"command":sys.argv[2]}))' "$dev" "$c")"
         done; }

EMU_URL="$EMU" ../../cisco-traffic-monitoring/lab/provision_cisco_lab.sh

post /api/device '{"id":"nx1","type":"nexus","hostname":"NX1"}'
post /api/link '{"a":"cr2","b":"nx1","iface_a":"GigabitEthernet0/0/2","iface_b":"Ethernet1/1"}'
for r in cr1 cr2; do
  cli "$r" "configure terminal" \
    "access-list 20 permit 10.240.0.0 0.0.255.255" \
    "snmp-server community netlab-ro RO 20" \
    "snmp-server group NETLAB-V3 v3 priv access 20" \
    "snmp-server trap-source GigabitEthernet0/0/0" \
    "snmp-server enable traps snmp linkdown linkup" \
    "snmp-server host 127.0.0.1 traps version 2c netlab-ro udp-port 1162" \
    "logging host 127.0.0.1 transport udp port 5515" "logging trap informational" \
    "ip flow-export destination 127.0.0.1 2055" "ip flow-export version 5" \
    "interface GigabitEthernet0/0/0" "ip flow ingress" "exit" "interface GigabitEthernet0/0/1" "ip flow ingress" "exit" "end"
done
cli cr2 "configure terminal" "interface GigabitEthernet0/0/2" "description to NX1" "ip address 10.240.23.1 255.255.255.252" \
  "no shutdown" "exit" "ip route 10.240.30.0 255.255.255.0 10.240.23.2" "end"
cli nx1 "configure terminal" "feature sflow" \
  "interface mgmt0" "ip address 10.240.99.3/24" "exit" \
  "interface Ethernet1/1" "no switchport" "ip address 10.240.23.2/30" "no shutdown" "exit" \
  "ip route 0.0.0.0/0 10.240.23.1" \
  "ip access-list SNMP-ALLOW" "10 permit ip 10.240.0.0/16 any" "exit" \
  "snmp-server community netlab-ro group network-operator" "snmp-server community netlab-ro use-ipv4acl SNMP-ALLOW" \
  "snmp-server host 127.0.0.1 traps version 2c netlab-ro udp-port 1162" \
  "logging server 127.0.0.1 5515" \
  "sflow sampling-rate 64" "sflow agent-ip 10.240.23.2" "sflow collector-ip 127.0.0.1 vrf default" \
  "sflow data-source interface Ethernet1/1" "end"
cli cr1 "configure terminal" "ip route 10.240.23.0 255.255.255.252 10.240.12.2" "end"
echo "[lab] CR1(10.240.12.1) / CR2(10.240.12.2) / NX1(管理 10.240.99.3) を構築しました(NetFlow → :2055、sFlow → :6343、syslog → :5515、trap → :1162)"

#!/usr/bin/env bash
# エミュレータ上に監視対象の Cisco ルータ 2 台を構築する(装置側の構成もコード化)
#
#   [LAN 10.240.1.0/24]─Gi0/0/1 CR1 Gi0/0/0 ─── 10.240.12.0/30 ─── Gi0/0/0 CR2 Gi0/0/1─[LAN 10.240.2.0/24]
#                                     .1                                  .2
# SNMP: snmp-server community netlab-ro RO  (snmp_exporter/snmp.yml の auth と一致)
set -euo pipefail
EMU="${EMU_URL:-http://localhost:8000}"
TOKEN_HDR=()
[ -n "${NETLAB_TOKEN:-}" ] && TOKEN_HDR=(-H "X-Session-Token: ${NETLAB_TOKEN}")

post() { curl -sf "${TOKEN_HDR[@]}" -H 'Content-Type: application/json' -X POST "$EMU$1" -d "$2" >/dev/null; }
cli()  { local dev=$1; shift; for c in "$@"; do
           post /api/cli "$(python3 -c 'import json,sys;print(json.dumps({"device_id":sys.argv[1],"command":sys.argv[2]}))' "$dev" "$c")"
         done; }

# /api/device のキーは id/type/hostname (/api/cli の device_id とは違う)
post /api/device '{"id":"cr1","type":"cisco","hostname":"CR1"}'
post /api/device '{"id":"cr2","type":"cisco","hostname":"CR2"}'
post /api/link '{"a":"cr1","b":"cr2","iface_a":"GigabitEthernet0/0/0","iface_b":"GigabitEthernet0/0/0"}'

cli cr1 "configure terminal" \
  "interface GigabitEthernet0/0/0" "description WAN to CR2 (osaka)" "ip address 10.240.12.1 255.255.255.252" "no shutdown" "exit" \
  "interface GigabitEthernet0/0/1" "description LAN tokyo" "ip address 10.240.1.1 255.255.255.0" "no shutdown" "exit" \
  "ip route 10.240.2.0 255.255.255.0 10.240.12.2" \
  "snmp-server community netlab-ro RO" "snmp-server location tokyo" "end"
cli cr2 "configure terminal" \
  "interface GigabitEthernet0/0/0" "description WAN to CR1 (tokyo)" "ip address 10.240.12.2 255.255.255.252" "no shutdown" "exit" \
  "interface GigabitEthernet0/0/1" "description LAN osaka" "ip address 10.240.2.1 255.255.255.0" "no shutdown" "exit" \
  "ip route 10.240.1.0 255.255.255.0 10.240.12.1" \
  "snmp-server community netlab-ro RO" "snmp-server location osaka" "end"
echo "[lab] CR1(10.240.12.1) / CR2(10.240.12.2) を構築しました"

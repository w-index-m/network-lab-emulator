#!/usr/bin/env bash
# 実機のスイッチ・ルータと OSPF のネイバーを張る(Ubuntu の FRRouting)
#
#   sudo bash ospf-peer.sh up [--network 192.168.1.0/24] [--area 0] [--router-id <IP>] [--iface <名前>]
#   sudo bash ospf-peer.sh status      # ネイバー・経路・受信の確認
#   sudo bash ospf-peer.sh down        # OSPF を止める
#
# 何もつけなければ、既定の経路が出ているインタフェースの IP と /長さから network・router-id を決める。
# 相手(スイッチ)側は普通の OSPF(broadcast)でよい。例:
#   router ospf 1
#    network 192.168.1.0 0.0.0.255 area 0
#
# WSL(ミラーモード含む)では、外から来る OSPF(IP プロトコル 89)が届かないので張れない。
# Hyper-V の「外部」仮想スイッチ・VirtualBox の「ブリッジ」につないだ仮想マシン、または実機の Linux で使う。
set -euo pipefail

ACTION=${1:-up}; shift || true
NETWORK= AREA=0 RID= IFACE=
while [ $# -gt 0 ]; do
  case "$1" in
    --network) NETWORK=$2; shift 2 ;;
    --area) AREA=$2; shift 2 ;;
    --router-id) RID=$2; shift 2 ;;
    --iface) IFACE=$2; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "知らないオプション: $1" >&2; exit 2 ;;
  esac
done

ok() { echo " OK $*"; }
ng() { echo " NG $*"; }
say() { echo "==> $*"; }

[ "$(id -u)" = 0 ] || { echo 'sudo をつけて実行してください'; exit 1; }

if grep -qi microsoft /proc/version 2>/dev/null; then
  ng 'WSL の中で動いています。WSL には外から OSPF が届かないので、ネイバーは INIT のまま張れません'
  echo '    Hyper-V の外部仮想スイッチ / VirtualBox のブリッジにつないだ Ubuntu で実行してください'
  [ "$ACTION" = up ] && exit 1
fi

detect() {
  [ -n "$IFACE" ] || IFACE=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
  [ -n "$IFACE" ] || IFACE=$(ip -4 -o addr show scope global | awk '{print $2; exit}')
  local cidr; cidr=$(ip -4 -o addr show dev "$IFACE" scope global | awk '{print $4; exit}')
  [ -n "$cidr" ] || { ng "$IFACE に IPv4 アドレスがありません"; exit 1; }
  [ -n "$RID" ] || RID=${cidr%/*}
  if [ -z "$NETWORK" ]; then
    NETWORK=$(python3 -c "import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)" "$cidr" 2>/dev/null \
              || ip -4 route show dev "$IFACE" scope link | awk '{print $1; exit}')
  fi
}

case "$ACTION" in
  up)
    detect
    say "インタフェース $IFACE / router-id $RID / network $NETWORK area $AREA"
    if ! command -v vtysh >/dev/null; then
      say 'FRRouting を入れます'
      DEBIAN_FRONTEND=noninteractive apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq frr tcpdump >/dev/null
    fi
    ok "$(vtysh -c 'show version' 2>/dev/null | grep -m1 -o 'FRRouting [0-9.]*')"
    sed -i 's/^ospfd=no/ospfd=yes/' /etc/frr/daemons
    service frr restart >/dev/null 2>&1 || systemctl restart frr
    sleep 2
    # 前に入れた non-broadcast の設定(WSL で試したもの)があれば外す
    vtysh -c 'conf t' -c "interface $IFACE" -c 'no ip ospf network' -c 'no ip ospf hello-interval' \
          -c 'no ip ospf dead-interval' -c 'end' >/dev/null 2>&1 || true
    vtysh -c 'conf t' -c 'no router ospf' -c 'end' >/dev/null 2>&1 || true
    vtysh -c 'conf t' -c 'router ospf' -c "ospf router-id $RID" -c "network $NETWORK area $AREA" \
          -c 'end' -c 'write' >/dev/null
    ok 'OSPF を設定しました(/etc/frr/frr.conf に保存)'
    say '相手の Hello を待ちます(最大 50 秒)'
    for _ in $(seq 1 25); do
      st=$(vtysh -c 'show ip ospf neighbor' | awk 'NR>2 && NF{print $1" "$3}')
      if echo "$st" | grep -q 'Full'; then ok "ネイバー: $(echo "$st" | tr '\n' ' ')"; break; fi
      sleep 2
    done
    if ! echo "${st:-}" | grep -q 'Full'; then
      if [ -n "${st:-}" ]; then ng "ネイバーが Full になりません: $(echo "$st" | tr '\n' ' ')"
      else ng 'ネイバーが見えません(相手の Hello が届いていない)'; fi
      echo '    相手側: router ospf の network が同じ範囲か・area が同じか・インタフェースが passive でないか'
      echo "    受信の確認: sudo timeout 30 tcpdump -ni $IFACE ip proto 89(相手からの Hello が見えるか)"
      exit 1
    fi
    echo
    vtysh -c 'show ip route ospf' | sed -n '/^O/,$p' ;;
  status)
    detect
    vtysh -c 'show ip ospf neighbor'
    vtysh -c 'show ip route ospf' | sed -n '/^O/,$p'
    say "受信の確認(10 秒): $IFACE の OSPF"
    timeout 10 tcpdump -ni "$IFACE" ip proto 89 2>/dev/null || true ;;
  down)
    vtysh -c 'conf t' -c 'no router ospf' -c 'end' -c 'write' >/dev/null && ok 'OSPF を止めました' ;;
  *) echo "使い方: sudo bash $0 up|status|down"; exit 2 ;;
esac

#!/usr/bin/env bash
#
# PowerDNS（権威DNS） + Kea DHCP（DHCPv4）をセットアップするスクリプト
#
# Infobloxのような商用DDI(DNS+DHCP+IPAM)アプライアンスの代わりに、
# 主要コンポーネントをOSSで揃える構成。IPAM部分はtools/setup_netbox.sh
# で導入したNetBoxが担当し、こちらはDNS/DHCPの実サーバーを提供する。
#
# 両方ともUbuntu標準aptリポジトリから直接入手可能（Docker Hub等の
# 迂回策は不要）。
#
# 使い方:
#   sudo bash tools/setup_dns_dhcp.sh setup    # 初回セットアップ+起動
#   sudo bash tools/setup_dns_dhcp.sh start    # 2回目以降の起動のみ
#   sudo bash tools/setup_dns_dhcp.sh status   # ヘルスチェック
#   sudo bash tools/setup_dns_dhcp.sh stop     # 停止
#
# 詳細・つまずいた点は docs/dns-dhcp-setup.md を参照。

set -uo pipefail

PDNS_DB_NAME="pdns"
PDNS_DB_USER="pdns"
PDNS_DB_PASS="pdns"
ZONE_NAME="${ZONE_NAME:-netlab.test}"
DHCP_INTERFACE="${DHCP_INTERFACE:-eth0}"
DHCP_SUBNET="${DHCP_SUBNET:-192.0.2.0/24}"
DHCP_POOL="${DHCP_POOL:-192.0.2.100 - 192.0.2.150}"

log() { echo "[setup_dns_dhcp] $*"; }

cmd_setup() {
    log "パッケージをインストール（PowerDNS + Kea DHCPv4、いずれもaptから直接）"
    apt-get install -y pdns-server pdns-backend-pgsql postgresql \
        kea-dhcp4-server kea-common dnsutils > /dev/null

    service postgresql start 2>&1 | grep -v "^$" || true

    _setup_powerdns
    _setup_kea

    cmd_start
}

_setup_powerdns() {
    if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PDNS_DB_USER'" | grep -q 1; then
        log "PowerDNS用DBユーザー/データベースを作成"
        sudo -u postgres psql -c "CREATE USER $PDNS_DB_USER WITH PASSWORD '$PDNS_DB_PASS';"
        sudo -u postgres psql -c "CREATE DATABASE $PDNS_DB_NAME OWNER $PDNS_DB_USER;"
        sudo -u postgres psql -d "$PDNS_DB_NAME" \
            -f /usr/share/pdns-backend-pgsql/schema/schema.pgsql.sql > /dev/null
        sudo -u postgres psql -d "$PDNS_DB_NAME" \
            -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $PDNS_DB_USER;" > /dev/null
        sudo -u postgres psql -d "$PDNS_DB_NAME" \
            -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $PDNS_DB_USER;" > /dev/null
    fi

    # デフォルトのBINDバックエンドを無効化し、gpgsqlバックエンドに切り替える
    if [ -f /etc/powerdns/pdns.d/bind.conf ]; then
        mv /etc/powerdns/pdns.d/bind.conf /etc/powerdns/pdns.d/bind.conf.disabled
    fi
    if [ ! -f /etc/powerdns/pdns.d/gpgsql.conf ]; then
        log "PowerDNS gpgsqlバックエンド設定を作成"
        cat > /etc/powerdns/pdns.d/gpgsql.conf <<EOF
launch+=gpgsql
gpgsql-host=localhost
gpgsql-port=5432
gpgsql-dbname=$PDNS_DB_NAME
gpgsql-user=$PDNS_DB_USER
gpgsql-password=$PDNS_DB_PASS
EOF
    fi
    # このサンドボックスはIPv6未対応のため、IPv4限定にしないと起動時に
    # "Address family not supported" で落ちる
    if ! grep -q "^local-address=0.0.0.0$" /etc/powerdns/pdns.conf 2>/dev/null; then
        cat >> /etc/powerdns/pdns.conf <<EOF
local-address=0.0.0.0
query-local-address=0.0.0.0
EOF
    fi
}

_setup_kea() {
    mkdir -p /run/kea /var/lib/kea
    chown -R _kea:_kea /run/kea /var/lib/kea 2>/dev/null || true

    log "Kea DHCPv4設定を生成 (interface=$DHCP_INTERFACE, subnet=$DHCP_SUBNET)"
    cat > /etc/kea/kea-dhcp4.conf <<EOF
{
"Dhcp4": {
    "interfaces-config": {
        "interfaces": [ "$DHCP_INTERFACE" ],
        "dhcp-socket-type": "udp"
    },
    "control-socket": {
        "socket-type": "unix",
        "socket-name": "/run/kea/kea4-ctrl-socket"
    },
    "lease-database": {
        "type": "memfile",
        "lfc-interval": 3600
    },
    "valid-lifetime": 3600,
    "renew-timer": 900,
    "rebind-timer": 1800,
    "subnet4": [
        {
            "id": 1,
            "subnet": "$DHCP_SUBNET",
            "pools": [ { "pool": "$DHCP_POOL" } ],
            "option-data": [
                { "name": "domain-name", "data": "$ZONE_NAME" }
            ]
        }
    ]
}
}
EOF
}

cmd_start() {
    service postgresql start 2>&1 | grep -v "^$" || true

    if ! pgrep -f "pdns_server --daemon=no" > /dev/null 2>&1; then
        log "PowerDNS 起動"
        rm -f /tmp/pdns.log
        nohup pdns_server --daemon=no --guardian=no > /tmp/pdns.log 2>&1 &
        disown
        sleep 2
        # netlab.test ゾーンが無ければ作成（既にあればスキップ）
        if ! pdnsutil list-all-zones 2>/dev/null | grep -q "^${ZONE_NAME}$"; then
            pdnsutil create-zone "$ZONE_NAME" > /dev/null 2>&1
        fi
    else
        log "PowerDNS は既に起動しています"
    fi

    if ! pgrep -f "kea-dhcp4 -c" > /dev/null 2>&1; then
        log "Kea DHCPv4 起動"
        rm -f /tmp/kea.log
        nohup kea-dhcp4 -c /etc/kea/kea-dhcp4.conf > /tmp/kea.log 2>&1 &
        disown
        sleep 2
    else
        log "Kea DHCPv4 は既に起動しています"
    fi

    cmd_status
}

cmd_status() {
    if pgrep -f "pdns_server --daemon=no" > /dev/null 2>&1; then
        local test_result
        test_result=$(dig @127.0.0.1 -p 53 +norecurse +short "$ZONE_NAME" SOA 2>/dev/null)
        if [ -n "$test_result" ]; then
            log "PowerDNS: 稼働中（zone ${ZONE_NAME} 応答確認OK）"
        else
            log "PowerDNS: プロセスは起動しているが応答なし"
        fi
    else
        log "PowerDNS: 停止中"
    fi

    if pgrep -f "kea-dhcp4 -c" > /dev/null 2>&1; then
        log "Kea DHCPv4: 稼働中（interface ${DHCP_INTERFACE}, pool ${DHCP_POOL}）"
    else
        log "Kea DHCPv4: 停止中"
    fi
}

cmd_stop() {
    log "PowerDNS / Kea DHCPv4 停止"
    pkill -f "pdns_server --daemon=no" 2>/dev/null || true
    pkill -f "kea-dhcp4 -c" 2>/dev/null || true
}

case "${1:-}" in
    setup)  cmd_setup ;;
    start)  cmd_start ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    *)
        echo "usage: $0 {setup|start|status|stop}"
        exit 1
        ;;
esac

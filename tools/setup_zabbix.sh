#!/usr/bin/env bash
#
# Zabbix Server をソースからビルド・起動するスクリプト（検証用）
#
# packages.zabbix.com / Docker Hub ともにこの実行環境ではブロックされて
# いたため、GitHub本体リポジトリからのソースビルドで構築する。
# Zabbix 7.x系は静的スキーマSQLを同梱しないため、Perlジェネレーターで
# schema.sql/data.sqlを生成してから流し込む点が他ツールと異なる。
#
# 前提: NetBox同様、Docker不要。PostgreSQL/Redisはapt標準リポジトリから。
#
# 使い方:
#   sudo bash tools/setup_zabbix.sh setup    # 初回ビルド+DB初期化+起動
#   sudo bash tools/setup_zabbix.sh start    # 2回目以降の起動のみ
#   sudo bash tools/setup_zabbix.sh status   # ヘルスチェック
#   sudo bash tools/setup_zabbix.sh stop     # 停止
#
# 詳細・つまずいた点は docs/zabbix-setup.md を参照。
# 結論: 既存スタック(Prometheus/Grafana/Alertmanager/Loki)と機能重複が
# 大きいため、このラボへの常設は非推奨。IaCでの構築可否を試す実験用。

set -uo pipefail

ZABBIX_DIR="${ZABBIX_DIR:-/opt/zabbix}"
ZABBIX_TAG="${ZABBIX_TAG:-7.4.14}"
DB_NAME="zabbix"
DB_USER="zabbix"
DB_PASS="zabbix"
LOG_FILE="/tmp/zabbix_server.log"

log() { echo "[setup_zabbix] $*"; }

cmd_setup() {
    log "ビルド依存関係をインストール"
    apt-get install -y build-essential libpcre2-dev libevent-dev pkg-config \
        zlib1g-dev libssl-dev libpq-dev postgresql redis-server autoconf \
        automake git > /dev/null

    service postgresql start 2>&1 | grep -v "^$" || true

    if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        log "DBユーザー/データベースを作成"
        sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
        sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
    fi

    if [ ! -d "$ZABBIX_DIR" ]; then
        log "Zabbixソースを取得 ($ZABBIX_TAG)"
        GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 --branch "$ZABBIX_TAG" \
            https://github.com/zabbix/zabbix "$ZABBIX_DIR"
    fi

    if ! command -v zabbix_server > /dev/null 2>&1; then
        log "configure + ビルド（数分かかります）"
        (cd "$ZABBIX_DIR" && autoreconf -ivf > /dev/null && \
            ./configure --enable-server --enable-agent2 --with-postgresql \
                --with-libpcre2 --with-openssl > /dev/null && \
            make -j"$(nproc)" > /dev/null && \
            make install > /dev/null)
    else
        log "zabbix_server は既にインストール済み"
    fi

    _init_schema
    _configure_server

    if ! id zabbix > /dev/null 2>&1; then
        log "専用ユーザー zabbix を作成"
        useradd -r -s /usr/sbin/nologin zabbix
    fi
    mkdir -p /usr/local/share/zabbix/{externalscripts,alertscripts} /usr/local/lib/modules
    chown -R zabbix:zabbix /usr/local/share/zabbix /usr/local/lib/modules

    cmd_start
}

_init_schema() {
    local user_exists
    user_exists=$(sudo -u postgres psql -d "$DB_NAME" -tAc \
        "SELECT 1 FROM information_schema.tables WHERE table_name='users'" 2>/dev/null || echo "")
    if [ "$user_exists" = "1" ]; then
        log "DBスキーマは初期化済みのためスキップ"
        return
    fi

    log "DBスキーマを生成・投入（Zabbix 7.x はSQLファイルを同梱しないため"
    log "  create/bin/ 配下のPerlジェネレーターでその場生成する）"
    (cd "$ZABBIX_DIR/create/bin" && \
        perl gen_schema.pl postgresql > /tmp/zabbix_schema.sql && \
        perl gen_data.pl postgresql < ../src/data.tmpl > /tmp/zabbix_data.sql)

    sudo -u postgres psql -d "$DB_NAME" -f /tmp/zabbix_schema.sql > /dev/null 2>&1
    sudo -u postgres psql -d "$DB_NAME" -f "$ZABBIX_DIR/database/postgresql/images.sql" > /dev/null 2>&1
    sudo -u postgres psql -d "$DB_NAME" -f /tmp/zabbix_data.sql > /dev/null 2>&1

    # superuser経由で流し込んだテーブルの所有権はpostgresのままなので、
    # zabbixロールに明示的に権限を付与しないと起動時にpermission deniedになる
    sudo -u postgres psql -d "$DB_NAME" \
        -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;" > /dev/null
    sudo -u postgres psql -d "$DB_NAME" \
        -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;" > /dev/null
}

_configure_server() {
    local cfg="/usr/local/etc/zabbix_server.conf"
    if grep -q "^DBPassword=$DB_PASS" "$cfg" 2>/dev/null; then
        return
    fi
    log "zabbix_server.conf にDB接続情報を設定"
    sed -i "s/^DBName=.*/DBName=$DB_NAME/" "$cfg" 2>/dev/null || true
    sed -i "s/^DBUser=.*/DBUser=$DB_USER/" "$cfg" 2>/dev/null || true
    if grep -q "^# DBPassword=" "$cfg"; then
        sed -i "s/^# DBPassword=$/DBPassword=$DB_PASS/" "$cfg"
    elif ! grep -q "^DBPassword=" "$cfg"; then
        echo "DBPassword=$DB_PASS" >> "$cfg"
    fi
}

cmd_start() {
    if pgrep -f "zabbix_server -f" > /dev/null 2>&1; then
        log "既に起動しています"
        return
    fi
    service postgresql start 2>&1 | grep -v "^$" || true
    log "zabbix_server 起動"
    rm -f "$LOG_FILE"
    touch "$LOG_FILE"
    chown zabbix:zabbix "$LOG_FILE"
    nohup su -s /bin/bash zabbix -c "zabbix_server -f" > /tmp/zabbix_su.log 2>&1 &
    disown
    sleep 4
    cmd_status
}

cmd_status() {
    if pgrep -f "zabbix_server -f" > /dev/null 2>&1; then
        if grep -q "cannot use database\|permission denied" "$LOG_FILE" 2>/dev/null; then
            log "Zabbix: 起動失敗（DBエラー、$LOG_FILE を確認してください）"
        else
            log "Zabbix: 稼働中（trapper port 10051）"
        fi
    else
        log "Zabbix: 停止中"
    fi
    pg_lsclusters 2>/dev/null | tail -1 | awk '{print "[setup_zabbix] PostgreSQL: " $4}'
}

cmd_stop() {
    log "zabbix_server 停止"
    pkill -f "zabbix_server -f" 2>/dev/null || true
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

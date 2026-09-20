#!/usr/bin/env bash
# setup_monitoring_stack.sh
#
# network-lab-emulator を実際の Prometheus / Alertmanager / Grafana / FRRouting
# と接続するための IaC 相当のプロビジョニングスクリプト。
# docs/monitoring-stack-guide.md に記載した手順をそのまま自動化したもの。
#
# べき等: 既にダウンロード/展開済みのバイナリはスキップし、既に起動している
# プロセスは再起動しない。何度実行しても安全。
#
# 使い方:
#   ./tools/setup_monitoring_stack.sh setup     # 全サービスをダウンロード・起動
#   ./tools/setup_monitoring_stack.sh status    # 各サービスのヘルスチェック
#   ./tools/setup_monitoring_stack.sh stop      # 起動したプロセスを停止
#
# 環境変数で上書き可能:
#   STACK_DIR   作業ディレクトリ (default: /tmp/netlab-stack)
#   GRAFANA_RELEASE_URL  Grafanaのtar.gz取得元
#     (default: このリポジトリのGitHub Release "grafana" アセット。
#      dl.grafana.com は一部環境のプロキシでブロックされるため、
#      GitHub Releases 経由での取得を前提にしている)
#   APP_PORT / EXPORTER_PORT / PROM_PORT / ALERTMANAGER_PORT / GRAFANA_PORT /
#   LOKI_PORT / SYSLOG_BRIDGE_PORT

set -euo pipefail

STACK_DIR="${STACK_DIR:-/tmp/netlab-stack}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APP_PORT="${APP_PORT:-8000}"
EXPORTER_PORT="${EXPORTER_PORT:-9877}"
PROM_PORT="${PROM_PORT:-9090}"
ALERTMANAGER_PORT="${ALERTMANAGER_PORT:-9093}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
LOKI_PORT="${LOKI_PORT:-3100}"
SYSLOG_BRIDGE_PORT="${SYSLOG_BRIDGE_PORT:-5514}"

PROM_VERSION="2.54.1"
ALERTMANAGER_VERSION="0.27.0"
LOKI_VERSION="3.2.0"
GRAFANA_RELEASE_URL="${GRAFANA_RELEASE_URL:-https://github.com/w-index-m/network-lab-emulator/releases/download/grafana/grafana.tar.gz}"
LOKI_RELEASE_URL="${LOKI_RELEASE_URL:-https://github.com/grafana/loki/releases/download/v${LOKI_VERSION}/loki-linux-amd64.zip}"

mkdir -p "$STACK_DIR"

log()  { echo "[setup_monitoring_stack] $*"; }
port_open() { curl -s -o /dev/null -m 2 "http://localhost:$1/" 2>/dev/null; }

# ── 1. アプリ本体 ──────────────────────────────────
start_app() {
    if curl -s -o /dev/null -m 2 "http://localhost:${APP_PORT}/api/snmp/dashboard"; then
        log "app.py: already running on :${APP_PORT}"
        return
    fi
    log "app.py: starting on :${APP_PORT}"
    (cd "$REPO_DIR" && NETLAB_AUTH_DISABLE=1 nohup uvicorn app:app --host 0.0.0.0 --port "$APP_PORT" \
        > "$STACK_DIR/app.log" 2>&1 &)
    sleep 2
}

# ── 2. Prometheus Exporter ─────────────────────────
start_exporter() {
    if curl -s -o /dev/null -m 2 "http://localhost:${EXPORTER_PORT}/metrics"; then
        log "prometheus_exporter: already running on :${EXPORTER_PORT}"
        return
    fi
    log "prometheus_exporter: starting on :${EXPORTER_PORT}"
    (cd "$REPO_DIR" && nohup python3 tools/prometheus_exporter.py \
        --emulator-url "http://localhost:${APP_PORT}" --port "$EXPORTER_PORT" --interval 5 \
        > "$STACK_DIR/exporter.log" 2>&1 &)
    sleep 2
}

# ── 3. Prometheus ──────────────────────────────────
fetch_prometheus() {
    local dir="$STACK_DIR/prometheus-${PROM_VERSION}.linux-amd64"
    if [ -x "$dir/prometheus" ]; then
        log "prometheus: binary already present"
        return
    fi
    log "prometheus: downloading v${PROM_VERSION}"
    curl -sL "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz" \
        -o "$STACK_DIR/prometheus.tar.gz"
    tar xzf "$STACK_DIR/prometheus.tar.gz" -C "$STACK_DIR"
}

write_prometheus_config() {
    cat > "$STACK_DIR/alert_rules.yml" <<EOF
groups:
  - name: netlab
    rules:
      - alert: InterfaceDown
        expr: netlab_interface_oper_status == 2
        for: 0s
        labels:
          severity: critical
        annotations:
          summary: "{{ \$labels.hostname }} interface {{ \$labels.interface }} is down"
      - alert: HighCpu
        expr: netlab_cpu_percent >= 80
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "{{ \$labels.hostname }} CPU {{ \$value }}% is high"
EOF

    cat > "$STACK_DIR/prometheus.yml" <<EOF
global:
  scrape_interval: 5s
  evaluation_interval: 5s
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:${ALERTMANAGER_PORT}']
rule_files:
  - ${STACK_DIR}/alert_rules.yml
scrape_configs:
  - job_name: 'netlab-emulator'
    static_configs:
      - targets: ['localhost:${EXPORTER_PORT}']
EOF
}

start_prometheus() {
    if port_open "$PROM_PORT"; then
        log "prometheus: already running on :${PROM_PORT}"
        return
    fi
    write_prometheus_config
    log "prometheus: starting on :${PROM_PORT}"
    nohup "$STACK_DIR/prometheus-${PROM_VERSION}.linux-amd64/prometheus" \
        --config.file="$STACK_DIR/prometheus.yml" \
        --storage.tsdb.path="$STACK_DIR/prom-data" \
        --web.listen-address="0.0.0.0:${PROM_PORT}" \
        > "$STACK_DIR/prometheus.log" 2>&1 &
    sleep 2
}

# ── 4. Alertmanager ────────────────────────────────
fetch_alertmanager() {
    local dir="$STACK_DIR/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64"
    if [ -x "$dir/alertmanager" ]; then
        log "alertmanager: binary already present"
        return
    fi
    log "alertmanager: downloading v${ALERTMANAGER_VERSION}"
    curl -sL "https://github.com/prometheus/alertmanager/releases/download/v${ALERTMANAGER_VERSION}/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64.tar.gz" \
        -o "$STACK_DIR/alertmanager.tar.gz"
    tar xzf "$STACK_DIR/alertmanager.tar.gz" -C "$STACK_DIR"
}

start_alertmanager() {
    if port_open "$ALERTMANAGER_PORT"; then
        log "alertmanager: already running on :${ALERTMANAGER_PORT}"
        return
    fi
    cat > "$STACK_DIR/alertmanager.yml" <<EOF
route:
  receiver: 'default'
receivers:
  - name: 'default'
EOF
    log "alertmanager: starting on :${ALERTMANAGER_PORT}"
    # --cluster.listen-address="" : サンドボックス環境等プライベートIPが
    # 取得できない環境ではgossipメッシュ初期化に失敗するため無効化する
    nohup "$STACK_DIR/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64/alertmanager" \
        --config.file="$STACK_DIR/alertmanager.yml" \
        --storage.path="$STACK_DIR/alertmanager-data" \
        --web.listen-address="0.0.0.0:${ALERTMANAGER_PORT}" \
        --cluster.listen-address="" \
        > "$STACK_DIR/alertmanager.log" 2>&1 &
    sleep 2
}

# ── 5. Grafana ──────────────────────────────────────
fetch_grafana() {
    if [ -n "$(find "$STACK_DIR" -maxdepth 1 -iregex '.*/grafana-v?[0-9].*' -type d 2>/dev/null)" ]; then
        log "grafana: already extracted"
        return
    fi
    log "grafana: downloading from ${GRAFANA_RELEASE_URL}"
    curl -sL "$GRAFANA_RELEASE_URL" -o "$STACK_DIR/grafana.tar.gz"
    tar xzf "$STACK_DIR/grafana.tar.gz" -C "$STACK_DIR"
}

start_grafana() {
    if port_open "$GRAFANA_PORT"; then
        log "grafana: already running on :${GRAFANA_PORT}"
        return
    fi
    local gdir
    gdir=$(find "$STACK_DIR" -maxdepth 1 -iregex '.*/grafana-v?[0-9].*' -type d | head -1)
    if [ -z "$gdir" ]; then
        log "grafana: extracted directory not found, skipping"
        return
    fi
    log "grafana: starting on :${GRAFANA_PORT} (homepath=${gdir})"
    nohup "$gdir/bin/grafana" server --homepath="$gdir" \
        --config="$gdir/conf/defaults.ini" \
        cfg:default.server.http_port="${GRAFANA_PORT}" \
        > "$STACK_DIR/grafana.log" 2>&1 &
    sleep 5

    log "grafana: registering Prometheus datasource"
    curl -s -X POST "http://admin:admin@localhost:${GRAFANA_PORT}/api/datasources" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"Prometheus\",\"type\":\"prometheus\",\"url\":\"http://localhost:${PROM_PORT}\",\"access\":\"proxy\",\"isDefault\":true}" \
        > "$STACK_DIR/grafana_datasource.json" || true
}

# ── 6. Grafana Loki（ログ集約） ─────────────────────
fetch_loki() {
    if [ -x "$STACK_DIR/loki-linux-amd64" ]; then
        log "loki: binary already present"
        return
    fi
    log "loki: downloading v${LOKI_VERSION} from ${LOKI_RELEASE_URL}"
    curl -sL "$LOKI_RELEASE_URL" -o "$STACK_DIR/loki.zip"
    unzip -oq "$STACK_DIR/loki.zip" -d "$STACK_DIR"
    chmod +x "$STACK_DIR/loki-linux-amd64"
}

write_loki_config() {
    mkdir -p "$STACK_DIR/loki-data"
    cat > "$STACK_DIR/loki-config.yml" <<EOF
auth_enabled: false

server:
  http_listen_address: 127.0.0.1
  http_listen_port: ${LOKI_PORT}
  grpc_listen_address: 127.0.0.1
  grpc_listen_port: 9096

common:
  instance_addr: 127.0.0.1
  instance_interface_names:
    - lo
  path_prefix: ${STACK_DIR}/loki-data
  storage:
    filesystem:
      chunks_directory: ${STACK_DIR}/loki-data/chunks
      rules_directory: ${STACK_DIR}/loki-data/rules
  replication_factor: 1
  ring:
    instance_addr: 127.0.0.1
    instance_interface_names:
      - lo
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

frontend_worker:
  frontend_address: 127.0.0.1:9096

ruler:
  alertmanager_url: http://localhost:${ALERTMANAGER_PORT}
EOF
}

start_loki() {
    if port_open "$LOKI_PORT"; then
        log "loki: already running on :${LOKI_PORT}"
        return
    fi
    write_loki_config
    log "loki: starting on :${LOKI_PORT}"
    nohup "$STACK_DIR/loki-linux-amd64" -config.file="$STACK_DIR/loki-config.yml" \
        > "$STACK_DIR/loki.log" 2>&1 &
    sleep 3

    if port_open "$GRAFANA_PORT"; then
        log "loki: registering Grafana datasource"
        curl -s -X POST "http://admin:admin@localhost:${GRAFANA_PORT}/api/datasources" \
            -H "Content-Type: application/json" \
            -d "{\"name\":\"Loki\",\"type\":\"loki\",\"url\":\"http://localhost:${LOKI_PORT}\",\"access\":\"proxy\"}" \
            > "$STACK_DIR/grafana_loki_datasource.json" || true
    fi
}

# ── 7. syslog -> Loki ブリッジ ──────────────────────
start_syslog_bridge() {
    if pgrep -f "tools/syslog_to_loki.py" >/dev/null 2>&1; then
        log "syslog_to_loki: already running"
        return
    fi
    log "syslog_to_loki: starting on udp:${SYSLOG_BRIDGE_PORT} -> loki:${LOKI_PORT}"
    (cd "$REPO_DIR" && nohup python3 tools/syslog_to_loki.py \
        --syslog-port "$SYSLOG_BRIDGE_PORT" --loki-url "http://localhost:${LOKI_PORT}" \
        > "$STACK_DIR/syslog_to_loki.log" 2>&1 &)
    sleep 1
}

# ── 8. FRRouting ────────────────────────────────────
install_frr() {
    if command -v vtysh >/dev/null 2>&1; then
        log "frr: already installed"
        return
    fi
    log "frr: installing via apt-get (archive.ubuntu.com)"
    apt-get update -qq && apt-get install -y -qq frr frr-pythontools
}

# ── コマンド ────────────────────────────────────────
cmd_setup() {
    start_app
    start_exporter
    fetch_prometheus
    start_prometheus
    fetch_alertmanager
    start_alertmanager
    fetch_grafana
    start_grafana
    fetch_loki
    start_loki
    start_syslog_bridge
    install_frr
    echo
    cmd_status
}

cmd_status() {
    log "── ヘルスチェック ──"
    printf "%-14s :%-6s " "app"          "$APP_PORT";          curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${APP_PORT}/api/snmp/dashboard" || echo "down"
    printf "%-14s :%-6s " "exporter"     "$EXPORTER_PORT";     curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${EXPORTER_PORT}/metrics" || echo "down"
    printf "%-14s :%-6s " "prometheus"   "$PROM_PORT";         curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${PROM_PORT}/-/healthy" || echo "down"
    printf "%-14s :%-6s " "alertmanager" "$ALERTMANAGER_PORT"; curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${ALERTMANAGER_PORT}/" || echo "down"
    printf "%-14s :%-6s " "grafana"      "$GRAFANA_PORT";      curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${GRAFANA_PORT}/api/health" || echo "down"
    printf "%-14s :%-6s " "loki"         "$LOKI_PORT";         curl -s -o /dev/null -w "%{http_code}\n" -m 2 "http://localhost:${LOKI_PORT}/ready" || echo "down"
    printf "%-14s %s\n" "syslog_bridge" "$(pgrep -f 'tools/syslog_to_loki.py' >/dev/null 2>&1 && echo "running(udp:${SYSLOG_BRIDGE_PORT})" || echo 'not running')"
    printf "%-14s %s\n" "frr" "$(command -v vtysh >/dev/null 2>&1 && echo installed || echo 'not installed')"
}

cmd_stop() {
    log "stopping app.py / exporter / prometheus / alertmanager / grafana / loki / syslog_bridge"
    pkill -f "uvicorn app:app" 2>/dev/null || true
    pkill -f "tools/prometheus_exporter.py" 2>/dev/null || true
    pkill -f "$STACK_DIR/prometheus-" 2>/dev/null || true
    pkill -f "$STACK_DIR/alertmanager-" 2>/dev/null || true
    pkill -f "bin/grafana server" 2>/dev/null || true
    pkill -f "$STACK_DIR/loki-linux-amd64" 2>/dev/null || true
    pkill -f "tools/syslog_to_loki.py" 2>/dev/null || true
}

case "${1:-setup}" in
    setup)  cmd_setup ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    *) echo "usage: $0 {setup|status|stop}"; exit 1 ;;
esac

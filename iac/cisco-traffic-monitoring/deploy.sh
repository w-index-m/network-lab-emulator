#!/usr/bin/env bash
# Cisco トラフィック監視スタックのデプロイ (IaC エントリポイント)
#
#   ./deploy.sh up       エミュレータ上にCiscoラボを作り、Prometheus/snmp_exporter/Grafanaを起動
#   ./deploy.sh status   各コンポーネントのヘルスと監視対象の状態
#   ./deploy.sh down     コンテナ停止(データボリュームは残す)
#   ./deploy.sh destroy  コンテナとデータボリュームを削除
#
# 前提: エミュレータが起動済み (例: NETLAB_AUTH_DISABLE=1 python app.py)、docker compose v2
set -euo pipefail
cd "$(dirname "$0")"
EMU="${EMU_URL:-http://localhost:8000}"
PROM="http://127.0.0.1:${PROM_PORT:-9090}"
GRAFANA="http://127.0.0.1:${GRAFANA_PORT:-3000}"
EXPORTER="http://127.0.0.1:${SNMP_EXPORTER_PORT:-9116}"
log() { echo "[deploy] $*"; }

wait_http() {  # wait_http <url> <name> [秒]
  for _ in $(seq 1 "${3:-60}"); do curl -sf -o /dev/null "$1" && { log "$2: ready"; return 0; }; sleep 1; done
  log "$2: $1 が応答しません"; return 1
}

case "${1:-up}" in
  up)
    command -v docker >/dev/null || { log "docker がありません"; exit 1; }
    docker info >/dev/null 2>&1 || { log "docker デーモンが動いていません (systemd が無い環境は: nohup dockerd &)"; exit 1; }
    wait_http "$EMU/api/health" "emulator" 5 || { log "先にエミュレータを起動してください: NETLAB_AUTH_DISABLE=1 python app.py"; exit 1; }
    ./lab/provision_cisco_lab.sh
    # ニルヴァーナの /metrics をヴァルハラ(Prometheus)が読む先と、認証トークン(無ければ空)
    mkdir -p prometheus/nirvana_targets
    hp="${EMU#*://}"; hp="${hp%%/*}"
    printf -- '- targets: [%s]\n  labels: {component: nirvana}\n' "$hp" > prometheus/nirvana_targets/nirvana.yml
    printf '%s' "${NETLAB_METRICS_TOKEN:-}" > prometheus/nirvana.token
    docker compose up -d
    wait_http "$EXPORTER/-/healthy" "snmp_exporter"
    wait_http "$PROM/-/ready" "prometheus"
    wait_http "$GRAFANA/api/health" "grafana"
    log "Grafana:    $GRAFANA/d/cisco-traffic  (admin/admin)"
    log "Prometheus: $PROM/targets"
    log "トラフィックを流す: ./lab/traffic_gen.sh"
    log "ニルヴァーナ側: PROMETHEUS_URL=$PROM でエミュレータを起動すると 再生・予測・警報の取り込みが有効になる"
    ;;
  status)
    docker compose ps
    curl -sf "$PROM/api/v1/query" --data-urlencode 'query=up{job="cisco-snmp"}' \
      | python3 -c 'import json,sys
for r in json.load(sys.stdin)["data"]["result"]:
    print("  {:<6} {:<14} up={}".format(r["metric"].get("router",""), r["metric"]["instance"], r["value"][1]))'
    ;;
  down)    docker compose down ;;
  destroy) docker compose down -v ;;
  *) echo "usage: $0 {up|status|down|destroy}"; exit 2 ;;
esac

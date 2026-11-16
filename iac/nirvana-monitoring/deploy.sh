#!/usr/bin/env bash
# ニルヴァーナ監視スタックのデプロイ(IaC エントリポイント)
#
#   ./deploy.sh up       エミュレータにラボを作り、snmp_exporter / ヴァルハラ / Loki / syslog 中継 / ニルヴァーナを起動
#   ./deploy.sh status   各コンポーネントのヘルスと、ニルヴァーナが見ているもの
#   ./deploy.sh down     コンテナ停止(データは残す)
#   ./deploy.sh destroy  コンテナとデータを削除
#
# 前提: エミュレータが起動済み(例: NETLAB_AUTH_DISABLE=1 python app.py)、docker compose v2
# 実機だけを見る場合は SKIP_LAB=1 ./deploy.sh up(nirvana/config.yaml と prometheus/targets/devices.yml を実機に書き換えておく)
set -euo pipefail
cd "$(dirname "$0")"
EMU="${EMU_URL:-http://localhost:8000}"
VALHALLA="http://127.0.0.1:9091"
NIRVANA="http://127.0.0.1:8090"
log() { echo "[deploy] $*"; }
wait_http() {  # wait_http <url> <name> [秒]
  for _ in $(seq 1 "${3:-90}"); do curl -sf -o /dev/null "$1" && { log "$2: ready"; return 0; }; sleep 1; done
  log "$2: $1 が応答しません"; return 1
}

case "${1:-up}" in
  up)
    command -v docker >/dev/null || { log "docker がありません"; exit 1; }
    docker info >/dev/null 2>&1 || { log "docker デーモンが動いていません (systemd が無い環境は: nohup dockerd &)"; exit 1; }
    if [ -z "${SKIP_LAB:-}" ]; then
      wait_http "$EMU/api/health" "emulator" 5 || { log "先にエミュレータを起動してください: NETLAB_AUTH_DISABLE=1 python app.py"; exit 1; }
      ./lab/provision_nirvana_lab.sh
    fi
    mkdir -p nirvana/data
    # 社内プロキシの独自 CA があればニルヴァーナのビルドに渡す(../../cyber-view-standalone/extra-ca/*.crt)
    docker compose up -d --build
    wait_http "http://127.0.0.1:9117/-/healthy" "snmp_exporter"
    wait_http "$VALHALLA/-/ready" "valhalla(prometheus)"
    wait_http "http://127.0.0.1:3101/ready" "loki" 120
    wait_http "$NIRVANA/api/health" "nirvana"
    log "ニルヴァーナ:   $NIRVANA/  (NETWORK / ROUTER LINKS / ATT&CK / WAN / DARKNET / BOARD)"
    log "ヴァルハラ:     $VALHALLA/targets"
    log "トラフィックを流す: ../cisco-traffic-monitoring/lab/traffic_gen.sh"
    ;;
  status)
    docker compose ps
    curl -sf "$NIRVANA/api/health" && echo
    curl -sf "$NIRVANA/api/traffic/links" | python3 -c 'import json,sys
for l in json.load(sys.stdin)["links"]:
    print("  {} {} ⇄ {} {}  {}  {:.0f} bps".format(l["host_a"], l["iface_a"], l["host_b"], l["iface_b"], l["state"], l["ab_bps"] + l["ba_bps"]))' || true
    curl -sf "$NIRVANA/api/darknet" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  darknet:", d["sensor"]["label"], d["hour"]["cats"])' || true
    curl -sf "$NIRVANA/api/wan" | python3 -c 'import json,sys
for p in json.load(sys.stdin)["paths"]: print("  wan: {} {} ms loss {:.0%}".format(p["label"], p["rtt_ms"], p["loss"]))' || true
    ;;
  down)    docker compose down ;;
  destroy) docker compose down -v; rm -rf nirvana/data/* ;;
  *) echo "usage: $0 {up|status|down|destroy}"; exit 2 ;;
esac

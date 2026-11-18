#!/usr/bin/env bash
# ニルヴァーナ監視スタックのデプロイ(IaC エントリポイント)
#
#   ./deploy.sh up       エミュレータにラボを作り、snmp_exporter / ヴァルハラ / Loki / syslog 中継 / ニルヴァーナを起動
#   ./deploy.sh status   各コンポーネントのヘルスと、ニルヴァーナが見ているもの
#   ./deploy.sh check    4つの経路(SNMP ポーリング・TRAP・syslog・フロー)と SNMP の許可制限を実際に試す
#                        (CR1 の Gi0/0/0 を一度 shutdown して戻す。ラボ専用)
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
  check)
    pass=0; fail=0
    ok() { log "OK   $*"; pass=$((pass+1)); }
    ng() { log "NG   $*"; fail=$((fail+1)); }
    emu_cli() { curl -sf -H 'Content-Type: application/json' -X POST "$EMU/api/cli" \
                  -d "$(python3 -c 'import json,sys;print(json.dumps({"device_id":sys.argv[1],"command":sys.argv[2]}))' "$1" "$2")" >/dev/null; }
    # 1) SNMP ポーリング: ヴァルハラが3台とも取れているか
    up=$(curl -sf "$VALHALLA/api/v1/query" --data-urlencode 'query=sum(up{job="cisco-snmp"})' \
         | python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(int(float(r[0]["value"][1])) if r else 0)' || echo 0)
    [ "$up" -ge 3 ] && ok "SNMP ポーリング: ヴァルハラが $up 台から取得" || ng "SNMP ポーリング: up=$up"
    # 2) SNMP の許可制限: 許可した範囲からは応答、それ以外(10.99.0.9)からは無応答
    if command -v snmpget >/dev/null; then
      if snmpget -v2c -c netlab-ro -t1 -r0 10.240.12.1 1.3.6.1.2.1.1.5.0 >/dev/null 2>&1; then ok "SNMP 許可: 10.240.0.0/16 からは応答"; else ng "SNMP 許可: 許可した送信元から応答なし"; fi
      if ip addr add 10.99.0.9/32 dev lo 2>/dev/null || ip addr show dev lo | grep -q 10.99.0.9; then
        if snmpget -v2c -c netlab-ro -t1 -r0 --clientaddr=10.99.0.9 10.240.12.1 1.3.6.1.2.1.1.5.0 >/dev/null 2>&1; then
          ng "SNMP 制限: 許可していない 10.99.0.9 から読めてしまう"; else ok "SNMP 制限: 許可していない 10.99.0.9 からは無応答"; fi
      else log "SKIP SNMP 制限(lo に 10.99.0.9 を足す権限がありません)"; fi
    else log "SKIP SNMP 許可/制限(snmpget がありません: apt-get install snmp)"; fi
    # 3) フロー: CR2 から ping を流し、ニルヴァーナが NetFlow / sFlow を受けているか
    emu_cli cr2 "ping 10.240.1.1 repeat 20"; emu_cli nx1 "ping 10.240.12.1 count 20"; sleep 5
    fl=$(curl -sf "$NIRVANA/api/darknet" | python3 -c 'import json,sys;print(json.load(sys.stdin)["sensor"]["label"])' || echo '')
    case "$fl" in *フロー*) ok "フロー: $fl" ;; *) ng "フロー: ニルヴァーナがフローを受けていません ($fl)" ;; esac
    # 4) TRAP / syslog / POLL: CR1 ⇄ CR2 を一度落として、ニルヴァーナがどの経路で断を見たか
    emu_cli cr1 "configure terminal"; emu_cli cr1 "interface GigabitEthernet0/0/0"; emu_cli cr1 "shutdown"; emu_cli cr1 "end"
    log "CR1 Gi0/0/0 を shutdown しました。ヴァルハラの次の取得(15秒)を待ちます"
    sleep 25
    ev=$(curl -sf "$NIRVANA/api/traffic/links" | python3 -c 'import json,sys
for l in json.load(sys.stdin)["links"]:
    if {l["host_a"], l["host_b"]} == {"CR1", "CR2"}: print(" ".join(l.get("down_sources") or []))' || echo '')
    for src in TRAP SYSLOG POLL; do
      case " $ev " in *" $src "*) ok "リンク断を $src で検知" ;; *) ng "リンク断を $src で検知できていません (検知: ${ev:-なし})" ;; esac
    done
    emu_cli cr1 "configure terminal"; emu_cli cr1 "interface GigabitEthernet0/0/0"; emu_cli cr1 "no shutdown"; emu_cli cr1 "end"
    log "CR1 Gi0/0/0 を戻しました"
    lk=$(curl -sfG "http://127.0.0.1:3101/loki/api/v1/query_range" --data-urlencode 'query={job="netlab-syslog"} |= "UPDOWN"' \
         --data-urlencode "start=$(( $(date +%s) - 300 ))000000000" | python3 -c 'import json,sys;print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))' || echo 0)
    [ "$lk" -gt 0 ] && ok "syslog: Loki に UPDOWN が $lk 行" || ng "syslog: Loki に UPDOWN がありません"
    log "結果: OK $pass / NG $fail"
    [ "$fail" -eq 0 ]
    ;;
  down)    docker compose down ;;
  destroy) docker compose down -v; rm -rf nirvana/data/* ;;
  *) echo "usage: $0 {up|status|check|down|destroy}"; exit 2 ;;
esac

#!/usr/bin/env bash
# CR1 → CR2 へ IOS の ping (repeat/size) を繰り返し、WAN リンクにトラフィックを流す。
# 量を時間で変動させてグラフに山谷が出るようにしている。Ctrl-C で停止。
#   RATE_SCALE=4 ./lab/traffic_gen.sh   # 量を4倍に
set -euo pipefail
EMU="${EMU_URL:-http://localhost:8000}"
SCALE="${RATE_SCALE:-1}"
TOKEN_HDR=()
[ -n "${NETLAB_TOKEN:-}" ] && TOKEN_HDR=(-H "X-Session-Token: ${NETLAB_TOKEN}")
cli() { curl -sf "${TOKEN_HDR[@]}" -H 'Content-Type: application/json' -X POST "$EMU/api/cli" \
          -d "{\"device_id\":\"$1\",\"command\":\"$2\"}" -o /dev/null; }
i=0
while :; do
  # 0〜59 の周期で 200〜2000 回の 1500byte ping (≒ 0.3〜3 MB / 5秒)
  phase=$(( i % 60 )); rep=$(( (200 + (phase < 30 ? phase : 60 - phase) * 60) * SCALE ))
  cli cr1 "ping 10.240.2.1 repeat ${rep} size 1500"
  # 逆方向は少なめ
  cli cr2 "ping 10.240.1.1 repeat $(( rep / 4 )) size 512"
  i=$(( i + 1 )); sleep 5
done

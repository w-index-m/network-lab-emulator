#!/usr/bin/env bash
# gNMI → gnmic → Prometheus(ヴァルハラ)を 1 本で立てる(Linux / WSL 用)
#
#   ./gnmi-quickstart.sh up --target 192.168.1.1 --ports Gi1/0/1,Gi1/0/2
#   ./gnmi-quickstart.sh status
#   ./gnmi-quickstart.sh down
#
# up がやること:
#   1. gnmic が無ければ入れる(公式の導入スクリプト。sudo のパスワードを聞かれることがある)
#   2. Prometheus が無ければ取ってくる(GitHub の配布物、$DIR の下に置く)
#   3. gnmic.yaml / prometheus.yml / ニルヴァーナの設定(nirvana.yaml)を $DIR に書く
#   4. gnmic と Prometheus を nohup で起動する(画面を閉じても止まらない)
#   5. 値が gnmic → Prometheus まで届いたかを確かめ、だめなら原因の目安を出す
#
# 実機の IOS XE 16.x(WS-C3650 16.12.11)で分かった癖に合わせてある:
#   - ポート名(キー)の無い購読は断られる → ポートごとに [name=...] を付ける
#   - oper-status だけの購読は打ち切られる → /state をまとめて購読する
#   - 装置の時計がずれていると値が捨てられる → export-timestamps は付けない
#
# 設定は環境変数でも渡せる: GNMI_TARGET / GNMI_PORTS / GNMI_USER / GNMI_PASSWORD / GNMI_DIR
set -euo pipefail

PROM_VERSION=2.54.1
DIR=${GNMI_DIR:-$HOME/nirvana-gnmi}
TARGET=${GNMI_TARGET:-}
PORTS=${GNMI_PORTS:-}
USER_NAME=${GNMI_USER:-}
PASSWORD=${GNMI_PASSWORD:-}
INTERVAL=10
GNMI_PORT=50052
GNMIC_LISTEN=9804
PROM_LISTEN=9090
WAIT=90

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
オプション(up / config):
  --target IP[,IP...]   装置の IP(複数はカンマ区切り。:ポート を付けなければ 50052)
  --ports  P[,P...]     監視するポート(Gi1/0/1 のような短い名前も可)
  --user NAME           gNMI のユーザー(省略時は聞く)
  --password PASS       パスワード(省略時は GNMI_PASSWORD か、聞く)
  --interval SEC        送ってもらう間隔(既定 10)
  --dir PATH            置き場所(既定 ~/nirvana-gnmi)
  --wait SEC            値が届くまで待つ秒数(既定 90)
EOF
}

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m OK\033[0m %s\n' "$*"; }
ng()   { printf '\033[31m NG\033[0m %s\n' "$*"; }
hint() { printf '    %s\n' "$*"; }

# Gi1/0/1 → GigabitEthernet1/0/1 など(ニルヴァーナと gNMI はフル名で照合する)
expand_if() {
  local n=$1
  case "$n" in
    [Gg]i[0-9]*)  echo "GigabitEthernet${n:2}" ;;
    [Tt]e[0-9]*)  echo "TenGigabitEthernet${n:2}" ;;
    [Tt]w[0-9]*)  echo "TwoGigabitEthernet${n:2}" ;;
    [Ff]a[0-9]*)  echo "FastEthernet${n:2}" ;;
    [Ff]o[0-9]*)  echo "FortyGigabitEthernet${n:2}" ;;
    [Hh]u[0-9]*)  echo "HundredGigE${n:2}" ;;
    [Ee]th[0-9]*) echo "Ethernet${n:3}" ;;
    *) echo "$n" ;;
  esac
}

split_csv() { tr ',' '\n' <<<"$1" | sed 's/^ *//; s/ *$//' | sed '/^$/d'; }

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --target) TARGET=$2; shift 2 ;;
      --ports) PORTS=$2; shift 2 ;;
      --user) USER_NAME=$2; shift 2 ;;
      --password) PASSWORD=$2; shift 2 ;;
      --interval) INTERVAL=$2; shift 2 ;;
      --dir) DIR=$2; shift 2 ;;
      --wait) WAIT=$2; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "知らないオプション: $1" >&2; usage; exit 2 ;;
    esac
  done
}

ask_missing() {
  if [ -z "$TARGET" ]; then read -r -p '装置の IP (例 192.168.1.1): ' TARGET; fi
  if [ -z "$PORTS" ]; then read -r -p '監視するポート (例 Gi1/0/1,Gi1/0/2): ' PORTS; fi
  if [ -z "$USER_NAME" ]; then read -r -p 'gNMI のユーザー (例 admin): ' USER_NAME; fi
  if [ -z "$PASSWORD" ]; then read -r -s -p 'パスワード: ' PASSWORD; echo; fi
  [ -n "$TARGET" ] && [ -n "$PORTS" ] && [ -n "$USER_NAME" ] && [ -n "$PASSWORD" ] || {
    echo '装置の IP・ポート・ユーザー・パスワードが必要です' >&2; exit 2; }
}

# YAML の 1 行文字列(" と \ をエスケープ)
yq() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

write_configs() {
  mkdir -p "$DIR"
  local t p host first_host
  # gnmic.yaml(パスワードを含むので本人だけ読める)
  umask 077
  {
    echo "# gnmi-quickstart.sh が作った設定($(date '+%F %T'))"
    echo "username: $(yq "$USER_NAME")"
    echo "password: $(yq "$PASSWORD")"
    echo "insecure: true"
    echo "encoding: json_ietf"
    echo "timeout: 10s"
    echo "targets:"
    for t in $(split_csv "$TARGET"); do
      case "$t" in *:*) ;; *) t="$t:$GNMI_PORT" ;; esac
      echo "  $t: {}"
    done
    echo "subscriptions:"
    echo "  if-state:"
    echo "    paths:"
    for p in $(split_csv "$PORTS"); do
      echo "      - \"/interfaces/interface[name=$(expand_if "$p")]/state\""
    done
    echo "    stream-mode: sample"
    echo "    sample-interval: ${INTERVAL}s"
    echo "outputs:"
    echo "  prom:"
    echo "    type: prometheus"
    echo "    listen: 0.0.0.0:$GNMIC_LISTEN"
    echo "    path: /metrics"
    echo "    expiration: $(( INTERVAL * 5 / 2 ))s"
    echo "    strings-as-labels: true"
  } > "$DIR/gnmic.yaml"
  umask 022
  cat > "$DIR/prometheus.yml" <<EOF
global:
  scrape_interval: ${INTERVAL}s
scrape_configs:
  - job_name: gnmic
    static_configs:
      - targets: ['127.0.0.1:$GNMIC_LISTEN']
EOF
  # ニルヴァーナ単体版の設定(装置とポートだけ。つながり links は手で書く)
  {
    echo "title: gNMI"
    echo "poll_interval: $INTERVAL"
    echo "devices:"
    local n=0
    for t in $(split_csv "$TARGET"); do
      n=$((n + 1)); host=${t%%:*}; first_host=${first_host:-$host}
      echo "  - id: sw$n"
      echo "    hostname: SW$n"
      echo "    type: catalyst"
      echo "    mgmt_ip: $host"
      echo "    interfaces:"
      for p in $(split_csv "$PORTS"); do
        echo "      - {name: $(expand_if "$p")}"
      done
    done
    echo "links: []   # 例: - {a: sw1, a_if: GigabitEthernet1/0/1, b: sw2, b_if: GigabitEthernet1/0/1}"
    echo "sources:"
    echo "  prometheus:"
    echo "    url: http://127.0.0.1:$PROM_LISTEN"
    echo "    flavor: gnmic"
    echo "    gnmic_job: gnmic"
  } > "$DIR/nirvana.yaml"
  ok "設定を書きました: $DIR/gnmic.yaml, prometheus.yml, nirvana.yaml"
}

install_tools() {
  if command -v gnmic >/dev/null 2>&1; then
    ok "gnmic: $(command -v gnmic)"
  else
    say 'gnmic を入れます(公式の導入スクリプト。sudo のパスワードを聞かれることがあります)'
    bash -c "$(curl -sL https://get-gnmic.openconfig.net)"
    command -v gnmic >/dev/null 2>&1 || { ng 'gnmic を入れられませんでした'; exit 1; }
  fi
  PROM_BIN="$DIR/prometheus-$PROM_VERSION.linux-amd64/prometheus"
  if [ -x "$PROM_BIN" ]; then
    ok "Prometheus: $PROM_BIN"
  else
    say "Prometheus $PROM_VERSION を取ってきます"
    mkdir -p "$DIR"
    curl -sL -o "$DIR/prom.tgz" \
      "https://github.com/prometheus/prometheus/releases/download/v$PROM_VERSION/prometheus-$PROM_VERSION.linux-amd64.tar.gz"
    tar xzf "$DIR/prom.tgz" -C "$DIR" && rm -f "$DIR/prom.tgz"
    [ -x "$PROM_BIN" ] || { ng 'Prometheus を取ってこられませんでした'; exit 1; }
    ok "Prometheus: $PROM_BIN"
  fi
}

pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

stop_all() {
  local f
  for f in gnmic prometheus; do
    if pid_alive "$DIR/$f.pid"; then
      kill "$(cat "$DIR/$f.pid")" 2>/dev/null || true
      ok "$f を止めました"
    fi
    rm -f "$DIR/$f.pid"
  done
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

start_all() {
  local p
  for p in $GNMIC_LISTEN $PROM_LISTEN; do
    if port_busy "$p"; then
      ng "ポート $p はすでに使われています(前に手で起動した gnmic / Prometheus?)"
      hint "止めてからやり直してください: pkill gnmic; pkill prometheus"
      exit 1
    fi
  done
  # cd と起動を && でつなぐと、その 2 つがまとめて裏に回り、pid が gnmic ではなく途中の shell になる
  # (down で止まらず、呼び出し元の画面も戻らない)。cd を先に済ませ、起動だけを裏に回す
  cd "$DIR"
  nohup gnmic --config "$DIR/gnmic.yaml" --log subscribe < /dev/null > "$DIR/gnmic.log" 2>&1 &
  echo $! > "$DIR/gnmic.pid"
  nohup "$PROM_BIN" --config.file="$DIR/prometheus.yml" \
      --web.listen-address="0.0.0.0:$PROM_LISTEN" --storage.tsdb.path="$DIR/prom-data" \
      < /dev/null > "$DIR/prometheus.log" 2>&1 &
  echo $! > "$DIR/prometheus.pid"
  cd - > /dev/null
  ok "gnmic(:$GNMIC_LISTEN)と Prometheus(:$PROM_LISTEN)を起動しました"
}

# gnmic のログから、よくある失敗の原因を出す
diagnose() {
  local log="$DIR/gnmic.log"
  [ -f "$log" ] || return 0
  if grep -q 'no route to host\|connection refused\|i/o timeout\|context deadline exceeded' "$log"; then
    hint '装置に届いていません。PC から ping が通るか、装置の IP・ケーブル・装置側の gNMI(gnmi-yang server / gnxi server)を確かめてください'
  fi
  if grep -q 'missing keys' "$log"; then
    hint 'ポート名の無い購読を断られています(このスクリプトの設定ではポート名を付けています。--ports を確かめてください)'
  fi
  if grep -q 'Unauthenticated\|authentication failed\|PermissionDenied' "$log"; then
    hint 'ユーザー・パスワードか権限が違います(装置側: aaa new-model / aaa authorization exec default local / privilege 15)'
  fi
  if grep -q 'Canceled by operator' "$log"; then
    hint '装置が購読を打ち切っています(装置のログ %MDT_SUBSCRIPTION-4-TERMINATED を確かめてください)'
  fi
  if grep -q 'code = NotFound' "$log"; then
    hint 'ポート名が装置に無い可能性があります(show interfaces status の名前と比べてください)'
  fi
  hint "gnmic のログ: tail -20 $DIR/gnmic.log"
}

prom_query() {
  curl -s --data-urlencode "query=$1" "http://127.0.0.1:$PROM_LISTEN/api/v1/query" 2>/dev/null
}

check() {
  local i n_gnmic=0 n_prom=0 up=''
  say "値が届くのを待ちます(最大 ${WAIT} 秒)"
  for ((i = 0; i < WAIT; i += 5)); do
    n_gnmic=$(curl -s "http://127.0.0.1:$GNMIC_LISTEN/metrics" 2>/dev/null | grep -c '^interfaces_interface_state_counters_in_octets{' || true)
    up=$(prom_query 'up{job="gnmic"}' | grep -o '"value":\[[^]]*,"[01]"' | grep -o '"[01]"$' | tr -d '"' || true)
    n_prom=$(prom_query 'interfaces_interface_state_counters_in_octets' | grep -o '"interface_name"' | wc -l || true)
    [ "${n_prom:-0}" -gt 0 ] && break
    sleep 5
  done
  if [ "${n_gnmic:-0}" -gt 0 ]; then ok "gnmic が装置から受け取ったポート: $n_gnmic"; else ng 'gnmic が装置から値を受け取れていません'; diagnose; fi
  if [ "$up" = 1 ]; then ok 'Prometheus が gnmic から取れています(up=1)'; else ng "Prometheus が gnmic を取れていません(up=${up:-なし})"; hint "Prometheus のログ: tail -20 $DIR/prometheus.log"; fi
  if [ "${n_prom:-0}" -gt 0 ]; then
    ok "Prometheus に入ったポート: $n_prom"
    prom_query 'interfaces_interface_state_counters_in_octets' \
      | grep -o '"interface_name":"[^"]*"\|"oper_status":"[^"]*"\|"source":"[^"]*"' | paste - - - | sed 's/"//g; s/^/    /'
    return 0
  fi
  return 1
}

status() {
  local f
  for f in gnmic prometheus; do
    if pid_alive "$DIR/$f.pid"; then ok "$f: 動いています(pid $(cat "$DIR/$f.pid"))"; else ng "$f: 止まっています"; fi
  done
  WAIT=5 check || true
}

next_steps() {
  cat <<EOF

ここまでできました。続きは次のとおりです。
  Prometheus の画面:   http://localhost:$PROM_LISTEN/targets
  流量を見る(Graph):   rate(interfaces_interface_state_counters_in_octets[1m]) * 8
  ニルヴァーナの設定:   $DIR/nirvana.yaml(つながり links は手で足す)

ニルヴァーナの起動(どちらか):
  WSL:         cd <nirvana-lab>/cyber-view-standalone && python3 server.py -c $DIR/nirvana.yaml --port 8091
  PowerShell:  cd <nirvana-lab>\\cyber-view-standalone; python server.py -c \\\\wsl\$\\<ディストリ名>$DIR/nirvana.yaml --port 8091
  画面:        http://localhost:8091/?view=links

止める:        $0 down
様子を見る:    $0 status
EOF
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
  up)
    parse_args "$@"; ask_missing
    install_tools; stop_all; write_configs; start_all
    # 設定画面(setup_gui.py)から呼ぶときは、手で打つ続きの案内は出さない
    if check; then [ -n "${GNMI_FROM_GUI:-}" ] || next_steps; else exit 1; fi ;;
  config)
    parse_args "$@"; ask_missing; write_configs ;;
  status)
    parse_args "$@"; status ;;
  down)
    parse_args "$@"; stop_all ;;
  *)
    usage; exit 2 ;;
esac

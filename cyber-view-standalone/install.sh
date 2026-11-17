#!/usr/bin/env bash
# Cyber View(モックニルヴァーナ単体版)のインストーラー
#
#   ./install.sh                          # 監視の仕組みを探して config.yaml を作り、venv に入れて起動の仕方を表示
#   ./install.sh --service                # systemd に登録して起動(root が必要)
#   ./install.sh --docker                 # docker compose で起動
#   ./install.sh --detect-only            # 見つけたものを表示するだけ(何も書かない)
#
# 探すもの: Prometheus(+snmp_exporter)・Loki・Zabbix・MRTG・Cacti・Ollama・AI の鍵。場所を指定するなら
#   --prometheus URL --loki URL --zabbix URL --mrtg-cfg PATH --cacti-rra DIR --ollama URL
# 既存の config.yaml は上書きしない(--force で上書き)。秘密(トークン・鍵)はファイルに書かず環境変数で渡す:
#   ZABBIX_TOKEN / ANTHROPIC_API_KEY(Claude)/ GROQ_API_KEY・OPENAI_API_KEY・NIRVANA_AI_KEY(OpenAI 互換)/ OLLAMA_URL
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
MODE=venv; SERVICE=0; DETECT_ONLY=0; FORCE=(); PORT="${CYBER_VIEW_PORT:-8090}"; CONFIG=config.yaml; ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) MODE=docker ;;
    --service) SERVICE=1 ;;
    --detect-only) DETECT_ONLY=1 ;;
    --force) FORCE=(--force) ;;
    --port) PORT="$2"; shift ;;
    --config) CONFIG="$2"; shift ;;
    --prometheus|--loki|--zabbix|--mrtg-cfg|--cacti-rra|--ollama|--title) ARGS+=("$1" "$2"); shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "不明な引数: $1" >&2; exit 2 ;;
  esac
  shift
done
say() { echo "[cyber-view] $*"; }

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 以上が必要です" >&2; exit 1; }

if [ ! -x .venv/bin/python ]; then
  say "venv を作ります(.venv)"
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -r requirements.txt
command -v rrdtool >/dev/null || say "注: rrdtool がありません(MRTG の rrdtool 形式・Cacti を読むなら apt install rrdtool)"

if [ "$DETECT_ONLY" = 1 ]; then
  exec .venv/bin/python -m cvs.installer "${ARGS[@]}"
fi
if [ -f "$CONFIG" ] && [ ${#FORCE[@]} -eq 0 ]; then
  say "$CONFIG はそのまま使います(作り直すなら --force)"
else
  .venv/bin/python -m cvs.installer "${ARGS[@]}" --write "$CONFIG" "${FORCE[@]}"
fi

if [ "$MODE" = docker ]; then
  say "docker compose で起動します"
  CYBER_VIEW_CONFIG="$CONFIG" docker compose up -d --build
  say "画面: http://<このホスト>:$PORT/"
  exit 0
fi

if [ "$SERVICE" = 1 ]; then
  if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
    say "systemd が使えません。下の「手で起動」を使ってください"
  else
    UNIT=/etc/systemd/system/cyber-view.service
    say "systemd に登録します: $UNIT"
    cat > "$UNIT" <<EOF
[Unit]
Description=Cyber View (mock Nirvana standalone)
After=network-online.target

[Service]
WorkingDirectory=$HERE
EnvironmentFile=-/etc/default/cyber-view
ExecStart=$HERE/.venv/bin/python $HERE/server.py -c $HERE/$CONFIG --port $PORT
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    [ -f /etc/default/cyber-view ] || cat > /etc/default/cyber-view <<'EOF'
# Cyber View の環境変数(秘密はここに書く。chmod 600 推奨)
# ZABBIX_TOKEN=
# ANTHROPIC_API_KEY=
# NIRVANA_AI_PROVIDER=claude      # claude / openai / ollama / rules
# NIRVANA_AI_MODEL=claude-opus-5-5
# GROQ_API_KEY=
# OLLAMA_URL=http://127.0.0.1:11434
EOF
    chmod 600 /etc/default/cyber-view
    systemctl daemon-reload
    systemctl enable --now cyber-view
    say "起動しました: http://<このホスト>:$PORT/(ログ: journalctl -u cyber-view -f)"
    exit 0
  fi
fi

say "手で起動: cd $HERE && .venv/bin/python server.py -c $CONFIG --port $PORT"
say "設定を見直す: $CONFIG(type・links・Cacti の targets)"

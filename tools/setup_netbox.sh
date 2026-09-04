#!/usr/bin/env bash
#
# NetBox(IPAM/DCIM) をローカルにセットアップするスクリプト
#
# Docker Hub / ghcr.io のイメージblobがこの実行環境ではブロックされて
# いたため、コンテナではなく素のPython venv + PostgreSQL + Redisで
# 動かす構成にした（Grafana/Prometheusと同じ「GitHub Releases/git clone
# で取得できるものは直接使う」方針の延長）。
#
# 使い方:
#   sudo bash tools/setup_netbox.sh setup    # 初回セットアップ+起動
#   sudo bash tools/setup_netbox.sh start    # 2回目以降の起動のみ
#   sudo bash tools/setup_netbox.sh status   # ヘルスチェック
#   sudo bash tools/setup_netbox.sh stop     # 停止
#
# 前提: postgresql, redis-server, libpq-dev, python3.12, git が
# apt等で導入可能なこと。NetBox本体はDjango 6系のためPython 3.12+必須。

set -uo pipefail

NETBOX_DIR="${NETBOX_DIR:-/opt/netbox}"
NETBOX_VENV="${NETBOX_VENV:-/opt/netbox-venv}"
NETBOX_TAG="${NETBOX_TAG:-v4.6.9}"
NETBOX_PORT="${NETBOX_PORT:-8080}"
DB_NAME="netbox"
DB_USER="netbox"
DB_PASS="netbox"
ADMIN_USER="admin"
ADMIN_PASS="admin12345"
LOG_DIR="/tmp/netlab-stack"
mkdir -p "$LOG_DIR"

log() { echo "[setup_netbox] $*"; }

cmd_setup() {
    log "PostgreSQL / Redis をインストール"
    apt-get install -y postgresql redis-server libpq-dev python3.12 python3.12-venv git > /dev/null

    service postgresql start 2>&1 | grep -v "^$" || true
    service redis-server start 2>&1 | grep -v "^$" || true

    if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        log "DBユーザー/データベースを作成"
        sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"
        sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
        sudo -u postgres psql -d "$DB_NAME" -c "GRANT CREATE ON SCHEMA public TO $DB_USER;"
    fi

    if [ ! -d "$NETBOX_DIR" ]; then
        log "NetBoxを取得 ($NETBOX_TAG)"
        GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 --branch "$NETBOX_TAG" \
            https://github.com/netbox-community/netbox "$NETBOX_DIR"
    fi

    if [ ! -d "$NETBOX_VENV" ]; then
        log "venv作成 (Python 3.12)"
        python3.12 -m venv "$NETBOX_VENV"
    fi
    # shellcheck disable=SC1091
    source "$NETBOX_VENV/bin/activate"
    log "依存関係をインストール（数分かかります）"
    pip install -q --upgrade pip
    pip install -q -r "$NETBOX_DIR/requirements.txt"

    _write_configuration

    log "マイグレーション実行"
    (cd "$NETBOX_DIR/netbox" && python3 manage.py migrate)

    log "管理者ユーザー作成"
    (cd "$NETBOX_DIR/netbox" && python3 manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='$ADMIN_USER').exists():
    User.objects.create_superuser('$ADMIN_USER', '${ADMIN_USER}@example.com', '$ADMIN_PASS')
    print('admin created')
else:
    print('admin already exists')
")

    log "APIトークン発行"
    (cd "$NETBOX_DIR/netbox" && python3 manage.py shell -c "
from users.models import Token
from django.contrib.auth import get_user_model
User = get_user_model()
admin = User.objects.get(username='$ADMIN_USER')
tok = Token.objects.filter(user=admin).first()
if not tok:
    tok = Token(user=admin)
    tok.save()
    print('NETBOX_TOKEN=nbt_' + tok.key + '.' + tok.token)
else:
    print('既存トークンあり（key=' + tok.key + '、平文は再取得不可。再発行するには'
          ' Token.objects.filter(user=admin).delete() 後に再実行してください）')
")

    cmd_start
}

_write_configuration() {
    local cfg="$NETBOX_DIR/netbox/netbox/configuration.py"
    if [ -f "$cfg" ] && grep -q "^DATABASES" "$cfg" && grep -q "'NAME': 'netbox'" "$cfg"; then
        log "configuration.py は設定済みのためスキップ"
        return
    fi
    log "configuration.py を生成"
    cp "$NETBOX_DIR/netbox/netbox/configuration_example.py" "$cfg"

    local secret pepper
    secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
    pepper=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    python3 - "$cfg" "$secret" "$pepper" "$DB_USER" "$DB_PASS" <<'PYEOF'
import sys
path, secret, pepper, db_user, db_pass = sys.argv[1:6]
with open(path) as f:
    content = f.read()

content = content.replace("ALLOWED_HOSTS = []", "ALLOWED_HOSTS = ['*']")
content = content.replace("SECRET_KEY = ''", f"SECRET_KEY = '{secret}'")
content = content.replace("API_TOKEN_PEPPERS = {}", f"API_TOKEN_PEPPERS = {{1: '{pepper}'}}")

lines = content.split("\n")
out = []
in_db_block = False
for line in lines:
    if line.strip().startswith("DATABASES = {"):
        in_db_block = True
    if in_db_block and "'USER': ''," in line:
        line = line.replace("'USER': '',", f"'USER': '{db_user}',")
    if in_db_block and "'PASSWORD': ''," in line and "PostgreSQL password" in line:
        line = line.replace("'PASSWORD': '',", f"'PASSWORD': '{db_pass}',")
    if in_db_block and "'PORT': ''," in line:
        line = line.replace("'PORT': '',", "'PORT': '5432',")
        in_db_block = False
    out.append(line)

with open(path, "w") as f:
    f.write("\n".join(out))
PYEOF
}

cmd_start() {
    if pgrep -f "manage.py runserver" > /dev/null 2>&1; then
        log "既に起動しています"
        return
    fi
    log "NetBoxサーバー起動 (:$NETBOX_PORT)"
    # shellcheck disable=SC1091
    source "$NETBOX_VENV/bin/activate"
    (cd "$NETBOX_DIR/netbox" && \
        nohup python3 manage.py runserver "0.0.0.0:$NETBOX_PORT" \
        > "$LOG_DIR/netbox.log" 2>&1 &)
    sleep 3
    cmd_status
}

cmd_status() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$NETBOX_PORT/api/status/" 2>/dev/null || echo "000")
    if [ "$code" = "200" ] || [ "$code" = "403" ]; then
        log "NetBox: 稼働中 (http://localhost:$NETBOX_PORT/, HTTP $code)"
    else
        log "NetBox: 応答なし (HTTP $code)"
    fi
    pg_lsclusters 2>/dev/null | tail -1 | awk '{print "[setup_netbox] PostgreSQL: " $4}'
    if redis-cli ping > /dev/null 2>&1; then
        log "Redis: 稼働中"
    else
        log "Redis: 応答なし"
    fi
}

cmd_stop() {
    log "NetBoxサーバー停止"
    pkill -f "manage.py runserver" 2>/dev/null || true
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

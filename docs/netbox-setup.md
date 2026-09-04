# NetBox (IPAM/DCIM) セットアップ

`tools/setup_netbox.sh`

## これは何か

このラボで実際に発生した「`sir-b`と`apresia`のSNMPエージェントが
IPアドレス重複で起動失敗する」という問題は、まさにNetBox（IPAM=IP
Address Management）が管理対象とする領域。監視ツール（Prometheus/
Grafana/Zabbix系）とは役割が異なり、「今何が動いているか」ではなく
「何がどこにあるべきか」を記録する台帳ツール。

## なぜコンテナ版ではなく素のインストールなのか

NetBox公式はDocker Composeでの配布が主流だが、この実行環境では
Docker Hub (`registry-1.docker.io`)・GHCR (`ghcr.io`のblob配信元
`pkg-containers.githubusercontent.com`) ともにブロックされており
`docker pull`が失敗する。一方で:

- NetBox本体は `git clone` で取得できる（GitHub、匿名clone許可）
- PostgreSQL / Redis は apt（OS標準リポジトリ）から入る

ため、コンテナを使わずPython venv + PostgreSQL + Redisの直接構成で
動かした。VyOS/Prometheus/Grafana/TACACS+と同じ「ブロックされた配布
経路を迂回し、別の入手経路が使えるものは直接使う」というこのラボの
一貫した方針に沿っている。

## 実際に確認した動作（実機構築済み）

```bash
sudo bash tools/setup_netbox.sh setup
# → PostgreSQL/Redisインストール・起動
#    NetBox v4.6.9 を git clone
#    Python 3.12 venv 作成、依存関係インストール
#    configuration.py 自動生成（DB接続情報、SECRET_KEY、API_TOKEN_PEPPERS）
#    migrate 実行（400以上のマイグレーションを適用）
#    管理者ユーザー(admin/admin12345)作成
#    APIトークン発行
#    開発サーバー起動 (:8080)

sudo bash tools/setup_netbox.sh status
# [setup_netbox] NetBox: 稼働中 (http://localhost:8080/, HTTP 403)
# [setup_netbox] PostgreSQL: online
# [setup_netbox] Redis: 稼働中
```

APIトークン認証・データ投入も実際に確認済み:

```bash
TOKEN="nbt_<key>.<plaintext>"   # setup時に出力されるNETBOX_TOKEN

curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/status/
# → {"netbox-version": "4.6.9", ...}

curl -X POST http://localhost:8080/api/dcim/manufacturers/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Cisco", "slug": "cisco"}'
# → 201、実際にDBへ登録される
```

## つまずいた点（このスクリプトが吸収済み）

- **psycopg-cのビルドエラー**（`pg_config.h: No such file or directory`）
  → `libpq-dev` が必要
- **Django 6系はPython 3.12+必須**（3.11では依存解決に失敗する）
  → `python3.12 -m venv` を明示使用
- **configuration.pyのDATABASES/REDISブロックは正規表現で一括置換
  すると別ブロックまで巻き込む**（PostgreSQLパスワードとRedis
  パスワードが同じ`'PASSWORD': ''`という行のため）→ 行単位で
  コメント文言(`PostgreSQL password`)を目印に判定
- **API_TOKEN_PEPPERSのキーは文字列ではなく整数**でなければ
  `ImproperlyConfigured`になる（`{1: '...'}`であって`{'1': '...'}`
  ではない）
- **v2トークンの認証形式は`Token <plaintext>`ではなく
  `Bearer nbt_<key>.<plaintext>`**。`Token.objects.create()`が返す
  `.key`はkey_id(12文字)のみで、実際に送る平文全体は`.token`
  プロパティ（インスタンス生成直後、`save()`前後でしか取得できない
  ―― 事後に`Token.objects.get()`しても平文は再取得不可なので、
  トークンは発行直後に必ず控えておくこと）

## QwenによるNetBox操作について

Qwen自体（Ollama）はコマンドを実行する能力を持たない（テキスト生成の
み）。NetBoxを「Qwen経由で自然文操作する」ためには、
`tools/nl_route_control.py` / `tools/nl_grafana_dashboard.py` と同じ
パターン（Qwenは自然文→構造化パラメータの変換役に徹し、実際の
API呼び出しは決まったPythonコードが担う）を、NetBox REST APIに対して
別途実装する必要がある。今回はまずNetBox本体の導入・実機確認までを
実施した。

## 制約

- 開発用サーバー（`manage.py runserver`）で稼働。実運用では
  gunicorn/nginx + systemdでの常駐化が必要（本番構成は未実施）
- RQワーカー（バックグラウンドジョブ処理）は未起動。webhookや
  スクリプト実行などバックグラウンドタスクを使う機能は動作しない
- コンテナ版と異なりNetBoxのアップグレードは手動（`git pull` +
  `migrate`の手動実行が必要）

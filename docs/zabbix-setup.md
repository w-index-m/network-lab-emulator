# Zabbix Server セットアップ（実験・検証用）

## 結論を先に

**このラボへの追加は非推奨**（既存スタックとの機能重複が大きい）が、
「IaCでどこまで自動構築できるか」を確認する実験として実際に
ソースからビルド・起動・DB初期化まで完了させた。以下はその記録。

## 既存スタックとの重複整理

| 機能 | 既存スタック | Zabbix |
|---|---|---|
| SNMP polling | Prometheus + snmp_exporter | Zabbix Server（内蔵） |
| アラート | Alertmanager | Zabbix Server（内蔵トリガー） |
| ダッシュボード | Grafana | Zabbix frontend（PHP、未構築） |
| ログ | Loki | 別途Zabbix Agent必要（未構築） |

新規に得られる価値は薄いが、単一プロダクトで完結する運用スタイルを
体感したい場合には選択肢になる。

## Docker Hub / packages.zabbix.com がブロックされているための回避

公式インストール手順（`packages.zabbix.com`のaptリポジトリ、または
Docker Hubのコンテナイメージ）はどちらもこの実行環境ではブロックされて
いた。GitHub本体リポジトリ（`zabbix/zabbix`）は匿名`git clone`できるが、
**GitHub Releasesにビルド済みバイナリは無く、ソースからのビルドが必須**
（NetBoxはPython/Djangoで比較的軽量だったが、ZabbixはC実装のため
ビルド時間・依存関係が重い）。

## 実際に確認した手順

```bash
# 1. ソース取得
git clone --depth 1 --branch 7.4.14 https://github.com/zabbix/zabbix /opt/zabbix

# 2. ビルド依存関係
apt-get install -y build-essential libpcre2-dev libevent-dev pkg-config \
    zlib1g-dev libssl-dev libpq-dev postgresql redis-server

# 3. configure + ビルド（PostgreSQL + Agent2、SNMP/IPMI/Web監視は無効）
cd /opt/zabbix
autoreconf -ivf
./configure --enable-server --enable-agent2 --with-postgresql \
    --with-libpcre2 --with-openssl
make -j$(nproc)      # 数分かかる
make install          # /usr/local/sbin/zabbix_server 等に配置

# 4. DB作成
sudo -u postgres psql -c "CREATE USER zabbix WITH PASSWORD 'zabbix';"
sudo -u postgres psql -c "CREATE DATABASE zabbix OWNER zabbix;"
```

## つまずいた点（重要）

**Zabbix 7.x系はDB初期化用の静的SQLファイルを同梱していない。**
ソースツリー内の`create/src/schema.tmpl` / `data.tmpl`という中間形式
から、`create/bin/gen_schema.pl` / `gen_data.pl`（Perl）でSQLを
生成する必要がある（サーバーバイナリが自動でスキーマを作ってくれる
わけではない）:

```bash
cd /opt/zabbix/create/bin
perl gen_schema.pl postgresql > /tmp/zabbix_schema.sql
# gen_data.pl は data.tmpl を標準入力から読む点に注意
# （gen_schema.pl とは引数の渡し方が違う）
perl gen_data.pl postgresql < ../src/data.tmpl > /tmp/zabbix_data.sql

sudo -u postgres psql -d zabbix -f /tmp/zabbix_schema.sql
sudo -u postgres psql -d zabbix -f /opt/zabbix/database/postgresql/images.sql
sudo -u postgres psql -d zabbix -f /tmp/zabbix_data.sql
```

**superuser(postgres)経由でスキーマを流し込むと、テーブルの所有者が
`postgres`になり、`zabbix`ロールに権限が無くて起動時に
`permission denied for table users`で失敗する。** 明示的にGRANTが必要:

```bash
sudo -u postgres psql -d zabbix -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zabbix;"
sudo -u postgres psql -d zabbix -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zabbix;"
```

**zabbix_serverはroot実行を拒否する。** 専用ユーザーが必要:

```bash
useradd -r -s /usr/sbin/nologin zabbix
chown zabbix:zabbix /tmp/zabbix_server.log
su -s /bin/bash zabbix -c "zabbix_server -f"
```

## 実際に確認した動作（2026-09-02）

```
Starting Zabbix Server. Zabbix 7.4.14 (revision {ZABBIX_REVISION}).
current database version (mandatory/optional): 07040000/07040011
required mandatory version: 07040000
HA manager started in active mode
server #0 started [main process]
... (poller/trapper/alert manager等、約50プロセスが全て正常起動)
```

- `ss -tlnp`でTCP/10051（trapperポート）がLISTEN状態であることを確認
- `psql`で`users`テーブルに`Admin`/`guest`ユーザーが投入されていることを確認
- DBエラーログなしで安定稼働

## 未実施

- PHPフロントエンド（`ui/`ディレクトリ、Web UI）の構築
- Zabbix Agent側からの実データ収集・グラフ表示確認
- IaC（Ansible等）ロール化（今回は手順の実行確認のみ。ロール化する
  場合はこのドキュメントの手順をそのまま`ansible/roles/zabbix/`に
  移植すればよい）

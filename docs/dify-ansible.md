# Dify(LLMアプリ開発プラットフォーム)をAnsibleで構築

`ansible/roles/dify/`

## これは何か

[Dify](https://github.com/langgenius/dify)（LLMアプリ/チャットボット/RAG
アプリをノーコード/ローコードで構築できるOSSプラットフォーム）を、
docker composeで構築する`monitoring_stack`/`tacacs_syslog`と同じ
パターンのAnsibleロールにしたもの。network-lab-emulator本体とは
完全に独立したスタックで、依存関係は無い。

ユーザーから「この中にDifyサーバも立てれますか？」「GitHub経由で
ダウンロードできるか確認してデプロイ」という依頼を受け、まず手動で
実際にdocker composeスタック(16コンテナ)を構築・動作確認し、その手順を
Ansible化した。

## 見つけた問題と回避策

Dify公式の`docker-compose.yaml`は、ベクトルDB(Weaviate)のイメージを
専用レジストリ`cr.weaviate.io`から取得する設定になっているが、
このサンドボックス環境のegressポリシーでは`cr.weaviate.io`への
接続がブロックされていた(`403 Forbidden`。`download.pytorch.org`や
`api.groq.com`と同じ種類の制限)。

同じイメージはDocker Hub上にも`semitechnologies/weaviate`として
公開されているため、そちらを取得してから`cr.weaviate.io/...`宛の
タグをローカルで付け直す（`docker tag`）ことで回避した。
`ansible/roles/dify/tasks/main.yml`の該当タスクが、実際に
`docker-compose.yaml`に書かれているバージョンタグを`grep`で検出して
から同じ手順を自動で行う(`dify_weaviate_registry_blocked: true`が
デフォルト。到達できる環境では`-e dify_weaviate_registry_blocked=false`
で無効化できる)。

## 構成

```
ansible/roles/dify/
├── defaults/main.yml   # dify_dir, dify_repo_version(検証済みコミットに固定)等
└── tasks/main.yml
```

`tasks/main.yml`の流れ(既存ロールと同じ、既に動いていれば何もしない
べき等パターン):

1. `docker`コマンドが無ければ`apt-get install docker.io
   docker-compose-v2`
2. Dockerデーモンに到達できなければ、まず`systemd`経由で起動を試み
   (systemdが無い/使えない環境では無視)、それでも届かなければ
   `nohup dockerd &`で直接起動するフォールバック(**このロールを
   検証したサンドボックス環境はまさにこのケース**:
   `systemctl`は"System has not been booted with systemd as init
   system"で使えず、フォールバックで実際に起動できることを確認した)
3. Difyをクローン(`dify_repo_version`に固定したコミット。`-e
   dify_repo_version=main`等で上書き可能)
4. `.env.example`を`.env`にコピー(無ければ)
5. 前述のWeaviateイメージレジストリ回避策
6. Web UI(`/apps`)が既に200を返していなければ`docker compose up -d`
7. Web UIが応答するまで待機(最大5分)

## 実際に動かして確認した結果

手動で構築済みのDifyスタックを一旦完全に停止し(`docker compose down`
+ dockerdプロセスをkill)、Ansibleロールから実際にゼロから起動して
検証した。

```bash
$ ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags dify \
    -e dify_dir=/tmp/claude-0/dify
...
TASK [dify : Start dockerd directly as a background process (no-systemd fallback)] ***
changed: [localhost]
...
TASK [dify : Start Dify (docker compose up -d)] ***
changed: [localhost]
TASK [dify : Wait for the Dify web UI to become reachable] ***
ok: [localhost]
PLAY RECAP ***
localhost : ok=11 changed=2 unreachable=0 failed=1 ...
```

(1回目は`when`条件の文字列truthy評価でAnsibleのバージョンに
引っかかるバグがあり、`| length > 0`を明示して修正。修正後は
以下の通り成功)

```bash
$ ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags dify \
    -e dify_dir=/tmp/claude-0/dify
...
PLAY RECAP ***
localhost : ok=12 changed=2 unreachable=0 failed=0 skipped=8 ...

$ curl http://localhost/apps -w "%{http_code}"
200
$ curl http://localhost/console/api/setup
{"step":"not_started","setup_at":null}
```

Web UI・APIとも実際に応答することを確認。さらにもう一度同じ
playbookを実行し、べき等性も確認した(dockerd起動・クローン・
`.env`コピー・compose起動が全てskip、Dockerイメージ操作系タスクも
既存イメージを検出してskip)。

## 使い方

```bash
# monitoring_stack + tacacs_syslog + Difyを一括構築
ansible-playbook -i ansible/inventory.ini ansible/site.yml

# Difyだけ実行
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags dify

# インストール先やバージョンを変える
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags dify \
  -e dify_dir=/opt/dify -e dify_repo_version=main

# cr.weaviate.ioに到達できる環境では回避策を無効化
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags dify \
  -e dify_weaviate_registry_blocked=false
```

## 制約・今後の拡張余地

- `dify_repo_version`はこのロールを検証した時点のコミットに固定して
  いる。Difyの更新に追従する場合は`main`や特定タグへ明示的に上書きする
  必要がある
- 初期セットアップ(管理者アカウント作成、`/console/api/setup`が
  `not_started`から先の手順)は未自動化。Web UIから手動で行う
- network-lab-emulator本体との連携(例: `tools/network_ontology_query.py`
  のAI要約バックエンドとしてDify経由のAPIを使う等)は未実装。今回は
  「立てられるか・IaC化」までがスコープ
- ポート80/443を使うため、同じホストで他にWebサーバーが動いている
  場合は競合する

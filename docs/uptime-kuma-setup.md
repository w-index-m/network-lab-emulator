# Uptime Kuma のセットアップ記録（このリポジトリの実行環境向け）

`network-lab-emulator` を動かしているコンテナ環境に、監視ダッシュボード
Uptime Kuma を追加でセットアップした記録。DockerHubからのイメージ取得が
組織ポリシーでブロックされたため、ソースからビルドして動かす方式に
切り替えている。同じ制約を持つ環境で再現する場合の手順として残す。

対象読者はClaude以外のLLM（Qwen等）でもよい想定。

---

## つまずいた点: Docker Hub からのpullが403で拒否される

```
docker run -d --name uptime-kuma -p 3001:3001 louislam/uptime-kuma:1
```

これは以下のエラーで失敗する:

```
failed to do request: Get "https://production.cloudfront.docker.com/...": Forbidden
```

このセッションのプロキシ状態を確認すると原因が分かる:

```bash
curl -sS http://127.0.0.1:42095/__agentproxy/status
```

`recentRelayFailures` に以下が出る:

```json
{
  "kind": "connect_rejected",
  "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
  "host": "production.cloudfront.docker.com:443"
}
```

**組織のネットワークポリシーでDocker Hub（正確にはそのCDN経由の
イメージレイヤー取得）が明示的に拒否されている。** これは技術的な
設定ミスではなくポリシーによる意図的な制限なので、迂回を試みない
（プロキシを無効化する、直接接続を試みる等は禁止されている）。

一方で `registry.npmjs.org` や GitHub は許可されている
（`no_proxy`環境変数に列挙されている、またはプロキシ経由で通る）。

## 対処: GitHubソースから直接ビルド・起動する

Dockerイメージの代わりに、Node.jsで直接動かす。

```bash
# dockerデーモン自体は動かせるが(root権限があれば)、
# イメージ取得(pull)がブロックされるためコンテナ化は使わない
git clone --depth 1 https://github.com/louislam/uptime-kuma.git
cd uptime-kuma
npm ci --no-audit --no-fund      # 依存関係インストール(1200+パッケージ、40秒程度)
npm run build                     # フロントエンドをビルド(vite build)
UPTIME_KUMA_PORT=3001 UPTIME_KUMA_HOST=0.0.0.0 node server/server.js
```

**注意**: `npm run setup`（公式のセットアップコマンド）は
`git checkout 2.5.3 && npm ci --omit dev && npm run download-dist` を
実行するが、`--depth 1`（shallow clone）だとタグ`2.5.3`が存在せず
`git checkout`が失敗する。タグ済みリリースのビルド済みdistを
ダウンロードする代わりに、`npm run build`でソースから直接ビルドすれば
この問題を回避できる（フルクローンにしてタグを取得しても良い）。

## 起動確認

```
Welcome to Uptime Kuma
Your Node.js version: 22.22.2
[SERVER] INFO: Uptime Kuma Version: 2.5.3
[SETUP-DATABASE] INFO: Listening on:  http://0.0.0.0:3001
[SETUP-DATABASE] INFO: Waiting for user action...
```

初回アクセス時は `/setup-database` にリダイレクトされ、管理者アカウント
（ユーザー名・パスワード）を作成する画面が出る。**この作成はセキュリティ
上、実際にアクセスする本人が行うべきで、他者が代行してパスワードを
知る状態にしない方がよい。**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3001/
# => 302 (setup-databaseへリダイレクト。起動できていることの確認)
```

## 運用メモ

- データは `uptime-kuma/data/` 配下のSQLiteに保存される
  （`db-config.json`が無い状態から初回起動すると自動生成される）
- ログは起動時のリダイレクト先やプロセスの標準出力に出る。
  バックグラウンド起動時は `nohup ... > /tmp/uptime-kuma.log 2>&1 &`
  のようにリダイレクトしておくと後から確認しやすい
- 本番運用するなら `npm run build` ではなく公式配布のDockerイメージか
  リリースの `dist-node`（`npm run download-dist`で取得できるビルド済み
  Node実行ファイル）を使う方が起動が速く再現性も高い。今回はDocker Hub
  アクセスがブロックされている環境向けの回避策であることに留意
- ネットワーク監視の対象として、この`network-lab-emulator`自体の
  `/api/status`エンドポイントや、SNMPトラップ受信ツール
  （`tools/snmp_trap_receiver.py`が出すPrometheusメトリクス）を
  監視項目に追加することもできる

## 今後の課題

- このセッションのコンテナからポート3001が外部（ユーザーのブラウザ）に
  転送されているかは未確認。転送されていない場合、動作確認は
  ヘッドレスブラウザでのスクリーンショット取得等、間接的な方法に限られる
- 永続化（コンテナ/セッション再起動後もデータを残す）の要否を確認していない
